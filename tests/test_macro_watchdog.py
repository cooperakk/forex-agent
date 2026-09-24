"""The dollar index, CFTC positioning, the macro desk, and the MetaTrader watchdog."""

from __future__ import annotations

import datetime as dt
import io
import json
import math
import os
import zipfile
from decimal import Decimal as D
from types import SimpleNamespace

import pandas as pd
import pyotp
import pytest
from fastapi.testclient import TestClient

from sentinel.core.audit import NullAudit
from sentinel.core.config import MacroConfig, TerminalWatchdogConfig
from sentinel.core.types import Side, Signal
from sentinel.data.cot import (
    COT_CODES, CotClient, CotError, CotReport, CotStore, available_from, parse_file,
    parse_rows, positioning,
)
from sentinel.data.dxy import DXY_CONSTANT, build_dxy, dxy_state, usd_sign
from sentinel.data.macro import MacroDesk
from sentinel.notify.messages import render
from sentinel.ops.terminal_watchdog import TerminalWatchdog
from tests.fake_mt5 import FakeMT5

H = 3600 * 10**9
T0 = int(dt.datetime(2026, 3, 3, 10, tzinfo=dt.timezone.utc).timestamp() * 1e9)
PRICES = {"EUR_USD": 1.10, "USD_JPY": 150.0, "GBP_USD": 1.27, "USD_CAD": 1.36,
          "USD_SEK": 10.5, "USD_CHF": 0.88}


def closes(n=120, drift=None, start=T0):
    idx = pd.to_datetime([start + i * H for i in range(n)], utc=True)
    out = {}
    for sym, p in PRICES.items():
        d = (drift or {}).get(sym, 0.0)
        out[sym] = pd.Series([p * math.exp(d * i) for i in range(n)], index=idx)
    return out


# --------------------------------------------------------------------------- #
# the dollar index
# --------------------------------------------------------------------------- #


class TestDxy:
    def test_the_published_formula(self):
        s = build_dxy(closes(5))
        expected = (DXY_CONSTANT * 1.10 ** -0.576 * 150.0 ** 0.136 * 1.27 ** -0.119
                    * 1.36 ** 0.091 * 10.5 ** 0.042 * 0.88 ** 0.036)
        assert s.complete and float(s.level.iloc[-1]) == pytest.approx(expected, rel=1e-12)

    def test_without_sek_it_is_a_flagged_proxy(self):
        c = closes(5)
        c.pop("USD_SEK")
        s = build_dxy(c)
        assert s is not None and not s.complete and s.missing == ["USD_SEK"]
        assert s.notes

    def test_without_the_euro_there_is_no_dollar_index(self):
        c = closes(5)
        c.pop("EUR_USD")
        assert build_dxy(c) is None

    def test_which_way_the_trade_faces_the_dollar(self):
        assert usd_sign("USD", "JPY", +1) == 1       # buy USD/JPY: long dollar
        assert usd_sign("EUR", "USD", +1) == -1      # buy EUR/USD: short dollar
        assert usd_sign("XAU", "USD", -1) == 1       # sell gold: long dollar
        assert usd_sign("EUR", "GBP", +1) == 0

    def test_state_is_causal(self):
        c = closes(120, drift={"EUR_USD": -0.001})     # euro falling: dollar rising
        s = build_dxy(c)
        as_of = T0 + 80 * H                            # bars 0..79 have closed
        before = dxy_state(s, as_of)
        assert before["dxy_mom"] > 0
        # A shock in bars that close AFTER as_of must not change the reading.
        c2 = {k: v.copy() for k, v in c.items()}
        c2["EUR_USD"].iloc[85:] *= 1.5
        assert dxy_state(build_dxy(c2), as_of) == before

    def test_too_little_history_is_unknown(self):
        assert dxy_state(build_dxy(closes(10)), T0 + 20 * H) == {}


# --------------------------------------------------------------------------- #
# COT
# --------------------------------------------------------------------------- #

API_ROW = {"report_date_as_yyyy_mm_dd": "2026-02-24T00:00:00.000",
           "cftc_contract_market_code": "099741",
           "market_and_exchange_names": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
           "noncomm_positions_long_all": "250000", "noncomm_positions_short_all": "100000",
           "comm_positions_long_all": "300000", "comm_positions_short_all": "450000",
           "open_interest_all": "700000"}
FILE_ROW = {"As of Date in Form YYYY-MM-DD": "2026-02-24",
            "CFTC Contract Market Code": "099741",
            "Noncommercial Positions-Long (All)": "250,000",
            "Noncommercial Positions-Short (All)": "100000",
            "Commercial Positions-Long (All)": "300000",
            "Commercial Positions-Short (All)": "450000", "Open Interest (All)": "700000"}


def weekly(ccy, nets, start="2023-01-03"):
    d0 = dt.date.fromisoformat(start)
    out = []
    for i, net in enumerate(nets):
        day = (d0 + dt.timedelta(weeks=i)).isoformat()
        out.append(CotReport(ccy, COT_CODES[ccy], day, available_from(day),
                             spec_long=100_000 + max(net, 0), spec_short=100_000 - min(net, 0),
                             comm_long=1, comm_short=1, open_interest=500_000))
    return out


class TestCot:
    def test_api_and_file_spellings_agree(self):
        a, f = parse_rows([API_ROW]), parse_rows([FILE_ROW])
        assert len(a) == len(f) == 1
        assert a[0].to_dict() == f[0].to_dict()
        assert a[0].currency == "EUR" and a[0].spec_net == 150_000

    def test_a_report_is_public_three_days_after_its_tuesday(self):
        tue = dt.datetime(2026, 2, 24, tzinfo=dt.timezone.utc)
        assert available_from("2026-02-24") == int(
            (tue + dt.timedelta(days=3, hours=21)).timestamp()) * 10**9

    def test_broken_and_unknown_rows_are_skipped(self):
        rows = [dict(API_ROW, cftc_contract_market_code="999999"),
                dict(API_ROW, open_interest_all="0"),
                dict(API_ROW, noncomm_positions_long_all="n/a")]
        assert parse_rows(rows) == []

    def test_the_index_ranks_against_three_years_and_never_peeks(self):
        hist = weekly("EUR", list(range(-60, 60)))          # rising net long
        last = hist[-1]
        pos = positioning(hist, last.available_ns)
        assert pos["index"] == 100.0 and pos["report_date"] == last.report_date
        # One second before the newest report is public, it is not used.
        earlier = positioning(hist, last.available_ns - 1)
        assert earlier["report_date"] == hist[-2].report_date
        assert positioning(hist[:30], last.available_ns) is None     # < 52 weeks

    def test_the_client_asks_for_codes_and_refuses_redirects(self):
        seen = {}

        def get(url, *, params, timeout):
            seen.update(url=url, params=params)
            return 200, json.dumps([API_ROW])
        out = CotClient(get=get).fetch(["099741", "097741"], "2026-01-01")
        assert seen["url"].startswith("https://publicreporting.cftc.gov/")
        assert "'099741'" in seen["params"]["$where"] and len(out) == 1
        with pytest.raises(CotError):
            CotClient(get=lambda u, **k: (302, "")).fetch(["099741"], "2026-01-01")
        with pytest.raises(CotError):
            CotClient(get=lambda u, **k: (500, "boom")).fetch(["099741"], "2026-01-01")
        with pytest.raises(ValueError):
            CotClient(get=get).fetch(["099741"], "2026-01-01' OR 1=1")

    def test_the_yearly_zip_imports(self, tmp_path):
        buf = io.StringIO()
        import csv
        w = csv.DictWriter(buf, fieldnames=list(FILE_ROW))
        w.writeheader()
        w.writerow(FILE_ROW)
        path = tmp_path / "deacot2026.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("annual.txt", buf.getvalue())
        assert parse_file(path)[0].spec_long == 250_000

    def test_store_round_trip(self, tmp_path):
        store = CotStore(tmp_path / "macro.db")
        store.upsert(weekly("JPY", [0, 5, 10]))
        assert [r.spec_net for r in store.history("JPY")] == [0, 5, 10]
        assert store.latest_date() == weekly("JPY", [0, 0, 0])[-1].report_date
        assert oct(os.stat(tmp_path / "macro.db").st_mode & 0o777) == "0o600"


# --------------------------------------------------------------------------- #
# the macro desk
# --------------------------------------------------------------------------- #


class FakeBars:
    def __init__(self, series):
        self.series = series

    def frame(self, sym, tf, limit=5000):
        s = self.series.get(sym)
        return None if s is None else pd.DataFrame({"close": s}).tail(limit)


@pytest.fixture
def desk(tmp_path):
    def make(cfg=None, series=None, reports=(), client=None):
        conf = cfg or MacroConfig()
        store = CotStore(tmp_path / "macro.db")
        store.upsert(list(reports))
        audit = NullAudit()
        d = MacroDesk(lambda: conf, store, FakeBars(series or {}), audit,
                      client=client, clock=lambda: T0 + 400 * 7 * 24 * H)
        return d, audit
    return make


class TestMacroDesk:
    def test_crowded_speculators_shrink_a_trade_on_their_side_only(self, desk):
        hist = weekly("EUR", list(range(-60, 60)))
        d, _ = desk(reports=hist)
        now = hist[-1].available_ns
        m, why, layers = d.layers("EUR_USD", +1, now)
        assert layers == {"cot_crowding": 0.75} and m == 0.75 and "EUR index 100" in why[0]
        assert d.layers("EUR_USD", -1, now)[2] == {}             # selling: not crowded
        f = d.features("EUR_USD", +1, now)
        assert f["cot_base"] == 1.0 and f["cot_with_trade"] > 0

    def test_a_dollar_moving_hard_against_the_trade_is_a_headwind(self, desk):
        series = closes(200, drift={"EUR_USD": -0.002})          # dollar rising fast
        d, _ = desk(series=series)
        d.rebuild_dxy()
        as_of = T0 + 200 * H
        _, why, layers = d.layers("EUR_USD", +1, as_of)            # buying EUR: short USD
        assert layers.get("dxy_headwind") == 0.75 and "dollar headwind" in why[0]
        assert "dxy_headwind" not in d.layers("EUR_USD", -1, as_of)[2]
        assert d.features("EUR_USD", +1, as_of)["dxy_align"] < 0

    def test_nothing_known_means_no_effect_and_no_features(self, desk):
        d, _ = desk()
        assert d.layers("EUR_USD", +1, T0) == (1.0, [], {})
        assert set(d.features("EUR_USD", +1, T0)) <= {"usd_side"}

    def test_disabled_means_nothing(self, desk):
        d, _ = desk(cfg=MacroConfig(enabled=False), reports=weekly("EUR", list(range(80))))
        assert d.features("EUR_USD", 1, T0 + 10**18) == {}
        assert d.layers("EUR_USD", 1, T0 + 10**18) == (1.0, [], {})

    def test_prices_refresh_through_the_feed_on_the_agents_thread(self, desk):
        d, _ = desk(series=closes(120))
        calls = []
        feed = SimpleNamespace(refresh=lambda syms, now, timeframes: calls.append(
            (tuple(syms), tuple(timeframes))))
        d.refresh_prices(feed, T0, {s: object() for s in ("EUR_USD", "USD_JPY", "GBP_USD",
                                                           "XAU_USD")})
        assert calls == [(("EUR_USD", "USD_JPY", "GBP_USD"), ("H1",))]
        view = d.view()
        assert view["dxy"]["available"] and view["dxy"]["complete"] is True

    def test_cot_refresh_announces_new_data_and_backs_off_on_error(self, desk):
        hist = weekly("EUR", list(range(-60, 60)))
        rows = [{"report_date_as_yyyy_mm_dd": r.report_date,
                 "cftc_contract_market_code": r.code,
                 "noncomm_positions_long_all": r.spec_long,
                 "noncomm_positions_short_all": r.spec_short,
                 "comm_positions_long_all": 1, "comm_positions_short_all": 1,
                 "open_interest_all": r.open_interest} for r in hist]
        ok_client = CotClient(get=lambda u, **k: (200, json.dumps(rows)))
        d, audit = desk(client=ok_client)
        out = d.refresh_cot()
        assert out["ok"] and out["reports"] == len(hist)
        events = [r for r in audit.records if r.event == "macro.cot"]
        assert events and events[0].payload["positions"][0]["currency"] == "EUR"
        d.client = CotClient(get=lambda u, **k: (503, "down"))
        d.cot_last_fetch_ns = 0
        bad = d.refresh_cot()
        assert not bad["ok"] and d.cot_error
        d.tick()                                   # backed off: no second attempt yet
        assert d.cot_error == bad["error"]

    def test_the_lab_joins_macro_as_of_each_signal(self, desk, tmp_path):
        from sentinel.brain.lab import LabDeps, ResearchLab
        hist = weekly("EUR", list(range(-60, 60)))
        d, _ = desk(reports=hist)
        lab = ResearchLab(LabDeps(config=lambda: None, bar_store=None, instruments=dict,
                                  conversions=dict, macro=d))
        after = {"ts_ns": hist[-1].available_ns, "bar_ns": H, "instrument": "EUR_USD",
                 "side_sign": 1, "features": {"x": 1.0}}
        before = dict(after, ts_ns=hist[40].available_ns - 2 * H, features={"x": 1.0})
        assert lab._join_macro([before, after]) == 2
        assert after["features"]["cot_base"] == 1.0
        # 40 reports were public at `before`: fewer than the 52-week minimum.
        assert "cot_base" not in before["features"]


class TestMacroInTheAgent:
    def test_macro_layers_shrink_and_are_recorded_for_the_scorecard(self, tmp_path):
        from sentinel.core.config import AgentMode
        from tests.test_release_150 import _agent

        class Macro:
            def features(self, *a, **k):
                return {"cot_with_trade": 0.9}

            def layers(self, *a, **k):
                return 0.75, ["crowded: test"], {"cot_crowding": 0.75}

            def refresh_prices(self, *a, **k):
                pass

        agent, _ = _agent(tmp_path, mode=AgentMode.OBSERVE)
        agent.macro = Macro()
        agent.start()
        snap = agent.feed.snapshot(["EUR_USD"], now_ns=T0)
        ctx = agent._build_context(T0, agent.broker.account(), [], snap, True,
                                   agent.health.snapshot())
        signal = Signal(strategy="donchian_trend", instrument="EUR_USD", side=Side.BUY,
                        strength=0.6, stop_price=D("1.08003"), target_price=D("1.09503"),
                        decision_ns=T0, timeframe="H4")
        d = agent._act_on_signal(signal, ctx, snap, "")
        assert d.diagnostics["brain_layers"]["cot_crowding"] == 0.75
        assert d.diagnostics["caution_multiplier"] <= 0.75
        assert "crowded: test" in d.lessons


# --------------------------------------------------------------------------- #
# the MetaTrader terminal watchdog
# --------------------------------------------------------------------------- #

PATH = "C:/Program Files/Alpari MT5/terminal64.exe"


def mt5_broker(fake, *, sign_in=True):
    from sentinel.brokers.mt5 import MT5Broker
    from sentinel.brokers.profiles import get_profile
    kw = dict(login=1000001, password="pw", server="FakeBroker-Demo",
              credential_fn=lambda: "pw", terminal_path=PATH) if sign_in else {}
    return MT5Broker(profile=get_profile("generic_mt5"), mt5_module=fake, **kw)


def events(audit):
    return [r.payload["action"] for r in audit.records if r.event == "ops.mt5_watchdog"]


@pytest.fixture
def watch():
    def make(fake=None, sign_in=True, cfg=None):
        fake = fake or FakeMT5()
        broker = mt5_broker(fake, sign_in=sign_in)
        audit = NullAudit()
        conf = cfg or TerminalWatchdogConfig()
        wd = TerminalWatchdog(broker, audit, lambda: conf)
        return wd, fake, broker, audit
    return make


class TestTerminalWatchdog:
    def test_a_closed_terminal_is_restarted_and_signed_in(self, watch):
        wd, fake, _, audit = watch()
        fake.terminal_down = True
        wd.check(T0)                                     # grace: nothing yet
        assert events(audit) == [] and wd.state == "terminal_down"
        wd.check(T0 + 10**9)
        assert events(audit) == ["down", "reconnected"]
        assert wd.state == "ok" and wd.recoveries == 1
        call = fake.init_calls[-1]
        assert call["login"] == 1000001 and call["password"] == "***"
        assert call["path"] == PATH

    def test_attach_mode_never_uses_credentials(self, watch):
        wd, fake, _, _ = watch(sign_in=False)
        fake.terminal_down = True
        wd.check(T0)
        wd.check(T0 + 10**9)
        assert "login" not in fake.init_calls[-1] and "password" not in fake.init_calls[-1]

    def test_failures_back_off_and_a_frozen_terminal_is_ended(self, watch, monkeypatch):
        import platform

        import sentinel.brokers.mt5 as mt5mod
        ended = []
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(mt5mod, "_end_terminal_process", lambda p: ended.append(p) or [4242])
        wd, fake, _, audit = watch()
        fake.terminal_down = True
        fake.fail_initialize = 3
        t = T0
        wd.check(t)
        wd.check(t := t + 10**9)                         # attempt 1 fails -> retry in 30 s
        assert wd.failures == 1 and wd.next_attempt_ns == t + 30 * 10**9
        wd.check(t := t + 10 * 10**9)                    # too early: no attempt
        assert wd.attempts == 1
        wd.check(t := t + 30 * 10**9)                    # attempt 2 fails -> 60 s
        wd.check(t := t + 61 * 10**9)                    # attempt 3 fails -> 120 s
        assert wd.failures == 3 and ended == []
        wd.check(t := t + 121 * 10**9)                   # 3 failures: end it, then retry
        assert ended == [PATH] and wd.state == "ok"
        assert events(audit) == ["down", "reconnect_failed", "reconnect_failed",
                                 "reconnect_failed", "killed", "reconnected"]

    def test_only_our_terminal_path_is_ever_ended(self, watch, monkeypatch):
        import platform
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        _, fake, broker, _ = watch(sign_in=False)            # no path configured
        ok, why = broker.kill_terminal()
        assert not ok and "path" in why
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        _, _, broker2, _ = watch()
        assert broker2.kill_terminal()[0] is False

    def test_a_lost_broker_link_is_retried_not_killed(self, watch, monkeypatch):
        import sentinel.brokers.mt5 as mt5mod
        monkeypatch.setattr(mt5mod, "_end_terminal_process",
                            lambda p: pytest.fail("must not end a connected terminal"))
        wd, fake, _, audit = watch(cfg=TerminalWatchdogConfig(kill_hung_after_failures=1))
        fake.terminal.connected = False
        fake.fail_initialize = 1
        wd.check(T0)
        wd.check(T0 + 10**9)
        wd.check(T0 + 40 * 10**9)
        assert "killed" not in events(audit) and wd.state == "ok"

    def test_a_person_switching_accounts_is_not_overridden_in_attach_mode(self, watch):
        wd, fake, _, audit = watch(sign_in=False)
        calls_at_start = len(fake.init_calls)            # the adapter's own attach
        fake.account.login = 5555555
        wd.check(T0)
        wd.check(T0 + 10**9)
        assert wd.state == "wrong_account" and events(audit) == ["down"]
        assert len(fake.init_calls) == calls_at_start

    def test_the_services_own_account_is_restored_in_sign_in_mode(self, watch):
        wd, fake, _, audit = watch()
        fake.account.login = 5555555
        wd.check(T0)
        wd.check(T0 + 10**9)
        assert fake.account.login == 1000001 and wd.state == "ok"

    def test_algo_trading_off_is_reported_once(self, watch):
        wd, fake, _, audit = watch()
        fake.terminal.trade_allowed = False
        wd.check(T0)
        wd.check(T0 + 10**9)
        fake.terminal.trade_allowed = True
        wd.check(T0 + 2 * 10**9)
        assert events(audit) == ["algo_off", "algo_on"]

    def test_a_blip_is_forgiven_and_a_real_outage_that_heals_is_reported(self, watch):
        wd, fake, _, audit = watch(cfg=TerminalWatchdogConfig(grace_checks=3))
        fake.terminal_down = True
        wd.check(T0)
        fake.terminal_down = False
        wd.check(T0 + 10**9)
        assert events(audit) == []                       # one bad read: nothing said
        fake.terminal_down = True
        fake.fail_initialize = 10
        for i in range(3):
            wd.check(T0 + (2 + i) * 10**9)
        fake.terminal_down = False
        wd.check(T0 + 100 * 10**9)
        assert events(audit)[0] == "down" and events(audit)[-1] == "recovered"

    def test_it_looks_through_the_account_binding(self, watch):
        from sentinel.brokers.bound import AccountBoundBroker
        _, fake, broker, _ = watch()
        bound = AccountBoundBroker(broker, "1000001", "demo", "USD")
        wd = TerminalWatchdog(bound, NullAudit(), lambda: TerminalWatchdogConfig())
        assert wd.broker is broker and wd.applicable

    def test_paper_trading_has_no_terminal(self):
        from sentinel.brokers.paper import PaperBroker
        from sentinel.core.money import Instrument
        paper = PaperBroker(instruments={"EUR_USD": Instrument("EUR_USD", "EUR", "USD")})
        wd = TerminalWatchdog(paper, NullAudit(), lambda: TerminalWatchdogConfig())
        assert not wd.applicable and wd.check(T0)["applicable"] is False

    def test_after_a_recovery_the_agent_reconciles_at_once(self, tmp_path):
        from tests.test_release_150 import _agent
        agent, broker = _agent(tmp_path)
        agent.start()

        class Stub:
            recoveries = 0

            def check(self, now):
                self.recoveries += 1

        agent.terminal_watchdog = Stub()
        agent.last_reconcile_ns = broker.now_ns - 10**9           # not due
        agent.cycle()
        assert agent.last_reconcile_ns >= broker.now_ns - 10**9 + 1

    def test_messages(self):
        for action in ("down", "reconnected", "recovered", "reconnect_failed", "killed",
                       "algo_off", "algo_on"):
            msg = render("ops.mt5_watchdog", {"action": action, "since_ns": 7,
                                              "down_sec": 150, "retry_in_sec": 60})
            assert msg is not None and msg.category == "critical"
        assert render("ops.mt5_watchdog", {"action": "kill_skipped"}) is None
        a = render("ops.mt5_watchdog", {"action": "down", "since_ns": 1})
        b = render("ops.mt5_watchdog", {"action": "down", "since_ns": 2})
        assert a.key != b.key                    # a second outage is not a duplicate


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(tmp_path):
    from sentinel.api.main import create_app
    from sentinel.api.security import SecurityManager
    from sentinel.api.state import Runtime
    from tests.test_ai_news_manual import _agent
    os.environ["SENTINEL_JWT_SECRET"] = "t" * 48
    agent, _ = _agent(tmp_path)
    runtime = Runtime(agent, tmp_path / "config.json")
    runtime.macro = MacroDesk(lambda: agent.config.macro, CotStore(tmp_path / "macro.db"),
                              agent.feed.store, agent.audit,
                              client=CotClient(get=lambda u, **k: (200, "[]")))
    security = SecurityManager(agent.audit, secret="t" * 48)
    owner, _ = security.add_user("owner1", "a-sufficiently-long-password", "owner")
    security.add_user("viewer1", "another-long-password-x", "viewer")
    client = TestClient(create_app(runtime, security))

    def login(name, pw):
        r = client.post("/api/auth/login", json={"username": name, "password": pw})
        return {"Authorization": f"Bearer {r.json()['token']}"}
    oh = login("owner1", "a-sufficiently-long-password")

    def totp():
        security._used_totp.clear()
        return dict(oh, **{"X-TOTP": pyotp.TOTP(owner.totp_secret).now()})
    return {"client": client, "runtime": runtime, "totp": totp, "oh": oh,
            "vh": login("viewer1", "another-long-password-x")}


class TestApi:
    def test_macro_view_and_settings(self, api):
        c = api["client"]
        r = c.get("/api/macro", headers=api["vh"])
        assert r.status_code == 200 and r.json()["available"]
        assert len(r.json()["cot"]["rows"]) == len(COT_CODES)
        body = {"patch": {"cot_extreme": 95}}
        assert c.post("/api/macro/settings", json=body, headers=api["vh"]).status_code == 403
        r = c.post("/api/macro/settings", json=body, headers=api["totp"]())
        assert r.status_code == 200, r.text
        assert api["runtime"].agent.config.macro.cot_extreme == 95
        bad = c.post("/api/macro/settings", json={"patch": {"cot_extreme": 10}},
                     headers=api["totp"]())
        assert bad.status_code == 400

    def test_cot_refresh_is_owner_only(self, api):
        c = api["client"]
        assert c.post("/api/macro/cot/refresh", headers=api["oh"]).status_code == 403
        r = c.post("/api/macro/cot/refresh", headers=api["totp"]())
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_terminal_status_and_settings(self, api):
        c = api["client"]
        r = c.get("/api/terminal", headers=api["vh"])
        assert r.status_code == 200 and r.json()["available"] is False
        r = c.post("/api/terminal/settings", json={"patch": {"grace_checks": 3}},
                   headers=api["totp"]())
        assert r.status_code == 200, r.text
        assert api["runtime"].agent.config.ops.terminal_watchdog.grace_checks == 3
        r = c.post("/api/terminal/settings", json={"patch": {"grace_checks": 0}},
                   headers=api["totp"]())
        assert r.status_code == 400
