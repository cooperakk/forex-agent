"""Gap stress, Telegram/Bale notifications, the MT5 connection check, cent accounts."""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal as D
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from sentinel.core.audit import AuditLog, NullAudit
from sentinel.core.money import Instrument
from sentinel.core.types import Position, Side
from sentinel.notify import Notifier
from sentinel.notify.channels import BotChannel, ChannelError, split_text, validate_chat, \
    validate_token
from sentinel.notify.messages import CATEGORIES, render
from sentinel.risk.stress import DEFAULT_SCENARIOS, book_stress_loss, max_lots_within, \
    scenario_gap
from tests.fake_mt5 import FakeMT5
from tests.test_risk_engine import intent, rules

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz_0123"
CHAT = "987654321"


# --------------------------------------------------------------------------- #
# gap stress
# --------------------------------------------------------------------------- #


class TestStressMath:
    def test_a_cross_takes_the_worse_leg(self):
        eurchf = Instrument("EUR_CHF", "EUR", "CHF")
        assert scenario_gap(eurchf, DEFAULT_SCENARIOS) == D("0.3")
        eurusd = Instrument("EUR_USD", "EUR", "USD")
        assert scenario_gap(eurusd, DEFAULT_SCENARIOS) == D("0.02")

    def test_book_loss_and_the_lot_cap(self):
        eu = Instrument("EUR_USD", "EUR", "USD")
        pos = [Position("EUR_USD", Side.BUY, D("1"), D("1.10"), 0)]
        loss, unpriced = book_stress_loss(pos, {"EUR_USD": eu}, {"EUR_USD": D("1.10")},
                                          {}, "USD", DEFAULT_SCENARIOS)
        assert loss == D("100000") * D("1.10") * D("0.02") and unpriced == []
        cap = max_lots_within(D("2200"), eu, D("1.10"), D("1"), DEFAULT_SCENARIOS)
        assert cap == D("1")

    def test_an_unpriceable_position_is_named(self):
        pos = [Position("XYZ_ABC", Side.BUY, D("1"), D("1"), 0)]
        _, unpriced = book_stress_loss(pos, {}, {}, {}, "USD", DEFAULT_SCENARIOS)
        assert unpriced == ["XYZ_ABC"]


class TestStressInTheEngine:
    def test_off_by_default_in_a_bare_context(self, engine, ctx):
        d = engine.evaluate_entry(intent(), ctx)
        assert d.approved and "stress_cap_lots" not in d.diagnostics

    def test_the_new_position_is_shrunk_to_fit(self, engine, ctx):
        full = engine.evaluate_entry(intent(), ctx).approved_lots
        ctx.stress_scenarios = dict(DEFAULT_SCENARIOS)
        ctx.stress_limit_pct = D("3")          # 300 USD of a 10 000 account
        d = engine.evaluate_entry(intent(), ctx)
        assert d.approved and d.approved_lots < full
        assert any(w.rule == "stress_shrunk" for w in d.warnings)
        # 0.13 lots x 108 500 x 2% = 282 <= 300
        assert d.approved_lots == D("0.13")

    def test_open_positions_use_up_the_budget(self, engine, ctx):
        ctx.stress_scenarios = dict(DEFAULT_SCENARIOS)
        ctx.stress_limit_pct = D("3")
        ctx.positions = [Position("USD_CHF", Side.BUY, D("0.02"), D("0.88"), 0)]
        # 2 000 USD notional x 30% (CHF) = 600 > 300: nothing new fits.
        d = engine.evaluate_entry(intent(), ctx)
        assert not d.approved and "stress_gap" in rules(d)

    def test_a_cooldown_blocks_the_account_or_one_strategy(self, engine, ctx):
        ctx.cooldowns = {"other": "rest"}
        assert engine.evaluate_entry(intent(), ctx).approved
        ctx.cooldowns = {"test": "3 losses"}
        assert "loss_streak_cooldown" in rules(engine.evaluate_entry(intent(), ctx))
        ctx.cooldowns = {"*": "3 losses"}
        assert "loss_streak_cooldown" in rules(engine.evaluate_entry(intent(), ctx))


# --------------------------------------------------------------------------- #
# the journal listener
# --------------------------------------------------------------------------- #


class TestAuditListener:
    def test_listeners_see_every_record_and_cannot_break_an_append(self, tmp_path):
        log = AuditLog(tmp_path / "a.jsonl", fsync_every_record=False)
        seen = []
        log.add_listener(lambda rec: seen.append(rec.event))
        log.add_listener(lambda rec: 1 / 0)
        rec = log.append("risk.halt", {"reason": "x"})
        assert rec.seq == 1 and seen == ["risk.halt"]
        assert log.verify()[0], "the hash chain is intact after a failing listener"

    def test_null_audit_notifies_too(self):
        log = NullAudit()
        seen = []
        log.add_listener(lambda rec: seen.append(rec.event))
        log.append("x.y", {})
        assert seen == ["x.y"]


# --------------------------------------------------------------------------- #
# channels and messages
# --------------------------------------------------------------------------- #


class FakeBot:
    """Both bot APIs, recorded."""

    def __init__(self):
        self.calls = []
        self.updates = []
        self.fail = None

    def __call__(self, url, *, json_body, timeout):
        self.calls.append((url, json_body))
        if self.fail:
            return self.fail
        method = url.rsplit("/", 1)[1]
        if method == "getMe":
            return 200, json.dumps({"ok": True, "result": {"username": "my_bot"}})
        if method == "getUpdates":
            off = json_body.get("offset", 0)
            return 200, json.dumps({"ok": True, "result": [u for u in self.updates
                                                           if u["update_id"] >= off]})
        return 200, json.dumps({"ok": True, "result": {"message_id": len(self.calls)}})

    def sent(self):
        return [b for u, b in self.calls if u.endswith("/sendMessage")]


class TestChannels:
    def test_tokens_and_chats_are_validated(self):
        assert validate_token(TOKEN) == TOKEN
        for bad in ("", "abc", "123:short", "123456:" + "x" * 30 + "/../"):
            with pytest.raises(ValueError):
                validate_token(bad)
        assert validate_chat("-1001234567890") == "-1001234567890"
        assert validate_chat("@my_channel") == "@my_channel"
        with pytest.raises(ValueError):
            validate_chat("12; DROP")

    def test_the_host_is_fixed_per_messenger(self):
        bot = FakeBot()
        BotChannel("telegram", TOKEN, post=bot).send(CHAT, "hi")
        BotChannel("bale", TOKEN, post=bot).send(CHAT, "hi")
        assert bot.calls[0][0].startswith("https://api.telegram.org/bot")
        assert bot.calls[1][0].startswith("https://tapi.bale.ai/bot")

    def test_errors_never_carry_the_token(self):
        def leak(url, **kw):
            raise OSError(f"connect failed for {url}")
        with pytest.raises(ChannelError) as exc:
            BotChannel("telegram", TOKEN, post=leak).send(CHAT, "hi")
        assert TOKEN not in str(exc.value) and "***" in str(exc.value)

    def test_redirects_and_api_errors_are_refused(self):
        with pytest.raises(ChannelError):
            BotChannel("telegram", TOKEN, post=lambda u, **k: (302, "")).send(CHAT, "x")
        bad = lambda u, **k: (401, json.dumps({"ok": False, "error_code": 401,  # noqa: E731
                                               "description": "Unauthorized"}))
        with pytest.raises(ChannelError) as exc:
            BotChannel("bale", TOKEN, post=bad).send(CHAT, "x")
        assert exc.value.status == 401

    def test_long_text_is_split(self):
        parts = split_text("سطر\n" * 3000, limit=1000)
        assert len(parts) > 1 and all(len(p) <= 1000 for p in parts)
        assert "".join(parts) == "سطر\n" * 3000


class TestMessages:
    @pytest.mark.parametrize("event,payload", [
        ("risk.halt", {"reason": "daily loss"}),
        ("risk.halt", {"performance_guard": True, "strategy": "s1", "reason": "x"}),
        ("ops.kill_switch", {"engaged": True, "reason": "x"}),
        ("ops.kill_switch", {"engaged": False, "released_by": "owner"}),
        ("ops.deadman", {"reason": "x"}),
        ("ops.reconcile_mismatch", {}),
        ("config.mode_change", {"from": "advisory", "to": "autonomous"}),
        ("risk.ladder_step", {"direction": "tighten", "to_rung": 1, "drawdown_pct": "5"}),
        ("data.divergence", {"reference_block": "EUR_USD", "divergence_bp": 40}),
        ("brain.cooldown", {"scope": "*", "losses": 3}),
        ("position.open", {"instrument": "EUR_USD", "side": "BUY", "lots": "0.1",
                           "client_order_id": "C1"}),
        ("learn.postmortem", {"trade_id": "T1", "outcome": "loss", "r_multiple": -1.0}),
        ("order.rejected", {"reason": "no money"}),
        ("decision.proposal", {"instrument": "EUR_USD", "side": "SELL"}),
        ("learn.param_proposal", {"path": "risk.x", "current_value": 1, "proposed_value": 2}),
        ("brain.drift", {"strategy": "s1", "alarm": True}),
        ("brain.lab", {"strategies": [{"strategy": "s1", "status": "alive", "mean_r": 0.2,
                                       "n": 40}], "meta_candidate": "m1"}),
        ("brain.report", {"week_ending": "2026-03-08", "trades": {"n": 3, "sum_r": 1.2,
                                                                  "win_rate": 0.66}}),
        ("brain.model", {"approved": "m1"}),
        ("sec.write_denied", {"username": "x", "reason": "totp", "action": "halt"}),
    ])
    def test_every_event_renders_persian_in_a_known_category(self, event, payload):
        msg = render(event, payload)
        assert msg is not None and msg.category in CATEGORIES and msg.text
        assert any("؀" <= ch <= "ۿ" for ch in msg.text)

    def test_unknown_events_are_silent(self):
        assert render("system.heartbeat", {}) is None


# --------------------------------------------------------------------------- #
# the notifier
# --------------------------------------------------------------------------- #


class MemSecrets:
    def __init__(self):
        self.d = {}

    def put(self, k, v):
        self.d[k] = v

    def get(self, k):
        return self.d.get(k)

    def has(self, k):
        return k in self.d

    def delete(self, k):
        return self.d.pop(k, None) is not None


@pytest.fixture
def notifier(tmp_path):
    bot = FakeBot()
    audit = NullAudit()
    kills = []
    now = {"ns": 1_775_000_000 * 10**9}
    n = Notifier(tmp_path, audit, secrets=MemSecrets(), transport=bot,
                 status_fn=lambda: {"mode": "advisory", "venue_mode": "paper",
                                    "account": {"equity": "10000", "currency": "USD",
                                                "open_positions": 1},
                                    "day_pnl": "12.5", "cooldowns": {}},
                 kill_fn=lambda reason, by: kills.append((reason, by)),
                 clock=lambda: now["ns"])
    n.start = lambda: None                    # tests drive the queue by hand
    n.save_channel("telegram", enabled=True, chat_id=CHAT, token=TOKEN, commands=True)
    audit.add_listener(n.on_audit)
    return n, bot, audit, kills, now


class TestNotifier:
    def test_journal_events_reach_the_owner(self, notifier):
        n, bot, audit, _, _ = notifier
        audit.append("ops.kill_switch", {"engaged": True, "reason": "test"})
        assert n.drain() == 1
        assert "توقف اضطراری" in bot.sent()[0]["text"] and bot.sent()[0]["chat_id"] == CHAT

    def test_the_same_situation_is_sent_once(self, notifier):
        n, bot, audit, _, _ = notifier
        for _ in range(5):
            audit.append("ops.reconcile_mismatch", {})
        assert n.drain() == 1

    def test_categories_filter(self, notifier):
        n, bot, audit, _, _ = notifier
        audit.append("decision.proposal", {"client_order_id": "C1"})   # off by default
        assert n.drain() == 0
        n.save_channel("telegram", enabled=True, chat_id=CHAT, categories=["proposals"])
        audit.append("decision.proposal", {"client_order_id": "C2"})
        assert n.drain() == 1

    def test_failed_sign_in_bursts_are_reported(self, notifier):
        n, bot, audit, _, _ = notifier
        for _ in range(3):
            audit.append("sec.auth_fail", {"username": "x"})
        assert n.drain() == 1 and "ورود ناموفق" in bot.sent()[0]["text"]

    def test_the_token_is_not_in_the_settings_file(self, notifier, tmp_path):
        n, *_ = notifier
        text = (tmp_path / "notify.json").read_text()
        assert TOKEN not in text and CHAT in text
        assert oct(os.stat(tmp_path / "notify.json").st_mode & 0o777) == "0o600"
        public = n.describe()
        assert public["channels"]["telegram"]["chat_id"] == "***"
        assert public["channels"]["telegram"]["token_stored"] is True

    def test_stop_command_engages_the_kill_switch(self, notifier):
        n, bot, _, kills, now = notifier
        bot.updates = [{"update_id": 10, "message": {"chat": {"id": int(CHAT)}, "text": "/stop",
                                                     "date": now["ns"] // 10**9}}]
        assert n.poll_once("telegram", 0) == 11
        assert kills and kills[0][1] == f"telegram:{CHAT}"
        assert "توقف اضطراری فعال شد" in bot.sent()[-1]["text"]

    def test_commands_from_another_chat_are_ignored(self, notifier):
        n, bot, _, kills, now = notifier
        bot.updates = [{"update_id": 1, "message": {"chat": {"id": 555}, "text": "/stop",
                                                    "date": now["ns"] // 10**9}}]
        n.poll_once("telegram", 0)
        assert kills == [] and bot.sent() == []

    def test_a_stale_command_is_not_obeyed(self, notifier):
        n, bot, _, kills, now = notifier
        bot.updates = [{"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": "/stop",
                                                    "date": now["ns"] // 10**9 - 3600}}]
        assert n.poll_once("telegram", 0) == 2
        assert kills == []

    def test_there_is_no_command_that_takes_risk(self, notifier):
        n, *_ = notifier
        for text in ("/release", "/start_trading", "/buy EURUSD", "/resume", "/unkill",
                     "/mode autonomous"):
            assert n.command(text, source="t") is None
        assert "وضعیت" in n.command("/help", source="t")
        assert "10000" in n.command("وضعیت", source="t")

    def test_discover_lists_chats_that_messaged_the_bot(self, notifier):
        n, bot, *_ = notifier
        bot.updates = [{"update_id": 1, "message": {"chat": {"id": 42, "type": "private",
                                                             "first_name": "Rasool"},
                                                    "text": "hi"}}]
        out = n.discover("telegram")
        assert out["ok"] and out["bot"] == "my_bot" and out["chats"][0]["id"] == "42"

    def test_the_daily_summary_is_sent_once_a_day(self, notifier):
        n, bot, _, _, now = notifier
        import datetime as dt
        t = dt.datetime(2026, 3, 3, 17, 5, tzinfo=dt.timezone.utc)
        n.tick(int(t.timestamp() * 1e9))
        n.tick(int(t.timestamp() * 1e9) + 60 * 10**9)
        assert n.drain() == 1 and "گزارش روزانه" in bot.sent()[0]["text"]

    def test_a_failing_channel_is_reported_not_raised(self, notifier):
        n, bot, audit, _, _ = notifier
        bot.fail = (500, "oops")
        audit.append("ops.deadman", {"reason": "x"})
        assert n.drain() == 0
        assert n.status["telegram"]["failed"] == 1 and n.status["telegram"]["last_error"]

    def test_enabling_without_a_chat_is_refused(self, notifier):
        n, *_ = notifier
        with pytest.raises(ValueError):
            n.save_channel("bale", enabled=True, chat_id="")

    def test_settings_survive_a_restart_and_bad_files_do_not_crash(self, notifier, tmp_path):
        n, *_ = notifier
        again = Notifier(tmp_path, NullAudit(), secrets=MemSecrets())
        assert again.settings["channels"]["telegram"]["chat_id"] == CHAT
        (tmp_path / "notify.json").write_text("{not json")
        assert Notifier(tmp_path, NullAudit(), secrets=MemSecrets()).settings["channels"][
            "telegram"]["enabled"] is False


class TestNotifyApi:
    @pytest.fixture
    def api(self, tmp_path):
        from sentinel.api.main import create_app
        from sentinel.api.security import SecurityManager
        from sentinel.api.state import Runtime
        from tests.test_ai_news_manual import _agent
        os.environ["SENTINEL_JWT_SECRET"] = "t" * 48
        agent, _ = _agent(tmp_path)
        runtime = Runtime(agent, tmp_path / "config.json")
        bot = FakeBot()
        runtime.notifier = Notifier(tmp_path, agent.audit, secrets=MemSecrets(), transport=bot,
                                    status_fn=runtime.status, kill_fn=runtime.engage_kill)
        runtime.notifier.start = lambda: None
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
        return {"client": client, "bot": bot, "totp": totp, "oh": oh,
                "vh": login("viewer1", "another-long-password-x"), "runtime": runtime}

    def test_saving_a_channel_needs_the_owner_and_hides_the_token(self, api):
        c = api["client"]
        body = {"channel": "bale", "enabled": True, "chat_id": CHAT, "token": TOKEN}
        assert c.post("/api/notify/channel", json=body, headers=api["vh"]).status_code == 403
        assert c.post("/api/notify/channel", json=body, headers=api["oh"]).status_code == 403
        r = c.post("/api/notify/channel", json=body, headers=api["totp"]())
        assert r.status_code == 200, r.text
        assert TOKEN not in r.text
        view = c.get("/api/notify", headers=api["vh"]).json()
        assert view["available"] and view["channels"]["bale"]["chat_id"] == "***"
        assert TOKEN not in json.dumps(view)

    def test_a_bad_token_is_a_400(self, api):
        r = api["client"].post("/api/notify/channel",
                               json={"channel": "telegram", "token": "nope"},
                               headers=api["totp"]())
        assert r.status_code == 400

    def test_test_and_discover(self, api):
        c = api["client"]
        c.post("/api/notify/channel", json={"channel": "telegram", "enabled": True,
                                            "chat_id": CHAT, "token": TOKEN},
               headers=api["totp"]())
        r = c.post("/api/notify/test", json={"channel": "telegram"}, headers=api["totp"]())
        assert r.json() == {"ok": True}
        r = c.post("/api/notify/discover", json={"channel": "telegram"}, headers=api["totp"]())
        assert r.json()["ok"] is True

    def test_a_kill_from_the_dashboard_is_announced(self, api):
        c = api["client"]
        c.post("/api/notify/channel", json={"channel": "telegram", "enabled": True,
                                            "chat_id": CHAT, "token": TOKEN},
               headers=api["totp"]())
        # bootstrap wires this in production
        api["runtime"].agent.audit.add_listener(api["runtime"].notifier.on_audit)
        r = c.post("/api/control/kill", json={"reason": "test"}, headers=api["totp"]())
        assert r.status_code == 200, r.text
        assert api["runtime"].notifier.drain() >= 1
        assert any("توقف اضطراری" in b["text"] for b in api["bot"].sent())


# --------------------------------------------------------------------------- #
# MetaTrader 5: the connection check and cent accounts
# --------------------------------------------------------------------------- #


class TestMt5Check:
    def test_a_healthy_demo_passes(self):
        import mt5_check
        rep = mt5_check.run_checks(FakeMT5(suffix=".a"), is_windows=True)
        assert rep.ok, [(c.status, c.title_fa) for c in rep.checks]
        assert rep.facts["account"]["type"] == "دمو" and rep.facts["suffix"] == ".a"

    def test_wrong_password_explains_the_alpari_checklist(self):
        import mt5_check

        class Refuses(FakeMT5):
            def initialize(self, *a, **k):
                self._last_error = (-6, "Terminal: Authorization failed")
                return False
        rep = mt5_check.run_checks(Refuses(), login=123, password="x", server="Alpari-MT5-Demo",
                                   is_windows=True)
        assert not rep.ok
        text = " ".join(c.detail_fa for c in rep.checks)
        assert "رمز معاملاتی" in text and "MetaTrader 5" in text

    def test_investor_password_is_named(self):
        import mt5_check
        fake = FakeMT5()
        fake.account.trade_allowed = False
        rep = mt5_check.run_checks(fake, is_windows=True)
        assert not rep.ok and any("سرمایه‌گذار" in c.detail_fa for c in rep.checks)

    def test_linux_is_told_to_use_the_bridge(self):
        import mt5_check
        rep = mt5_check.run_checks(FakeMT5(), is_windows=False)
        assert not rep.ok and "پل" in rep.checks[0].detail_fa

    def test_the_report_is_escaped_html(self):
        import mt5_check
        rep = mt5_check.Report()
        rep.add("fail", "<script>alert(1)</script>")
        out = mt5_check.render_html(rep)
        assert "<script>alert" not in out and "dir='rtl'" in out

    def test_a_cent_account_is_recognised(self):
        import mt5_check
        fake = FakeMT5()
        fake.account.currency = "USC"
        rep = mt5_check.run_checks(fake, is_windows=True)
        assert any("سنتی" in c.title_fa for c in rep.checks)


class TestCentAccountConversion:
    def test_no_usd_usc_symbol_falls_back_to_tick_values(self, monkeypatch):
        fake = FakeMT5()
        fake.account.currency = "USC"
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        from sentinel.brokers.mt5 import MT5Broker
        from sentinel.brokers.profiles import get_profile
        broker = MT5Broker(profile=get_profile("generic_mt5"))
        broker.instruments()
        assert broker.conversion_rate("USD", "USC") == D("1")
        jpy = broker.conversion_rate("JPY", "USC")
        assert abs(jpy - D("1") / D("150")) < D("1e-9")


class TestSaveKeepsWhatWasNotSent:
    def test_omitting_the_chat_id_keeps_it(self, notifier):
        n, *_ = notifier
        n.save_channel("telegram", enabled=True, commands=False, daily_hour_utc=6)
        ch = n.settings["channels"]["telegram"]
        assert ch["chat_id"] == CHAT and ch["enabled"] is True
        assert n.settings["daily_hour_utc"] == 6
        n.save_channel("telegram", enabled=False, chat_id="")
        assert n.settings["channels"]["telegram"]["chat_id"] == ""
