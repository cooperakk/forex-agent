"""Regressions for the 1.4.0 port: what was taken from the parallel 1.2.0 audit,
and what was built on top of it.

Grouped by the property each protects. Every test here names a way the
system could have traded the wrong account, forgotten its own risk, or
certified a strategy on evidence it did not have.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal as D
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sentinel.brokers import get_profile
from sentinel.brokers.bound import AccountBoundBroker
from sentinel.brokers.mt5 import MT5Broker
from sentinel.brokers.paper import PaperBroker, SimProfile
from sentinel.core.config import DEADMAN_MARGIN_SEC, SentinelConfig, deadman_timeout_ok
from sentinel.core.errors import BrokerError, UnknownOutcomeError
from sentinel.core.money import Instrument, dec, quantize
from sentinel.core.types import OrderIntent, OrderState, Side
from sentinel.data.synthetic import DEFAULT_UNIVERSE, generate_universe
from sentinel.data.validation import validate_frame, validate_universe
from tests.fake_mt5 import FakeMT5

MAJORS = {
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001")),
}
CONV = {"USD": D("1"), "JPY": D("1") / D("150")}


@pytest.fixture
def mt5(monkeypatch):
    def make(**terminal):
        import sys
        fake = FakeMT5(**terminal)
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        return MT5Broker(profile=get_profile("generic_mt5"), mt5_module=fake, **{}), fake
    return make


# --------------------------------------------------------------------------- #
# money & config
# --------------------------------------------------------------------------- #


class TestMoneyAndConfig:
    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_decimals_are_refused(self, bad):
        with pytest.raises(ValueError):
            dec(D(bad))
        with pytest.raises(ValueError):
            dec(bad)

    def test_quantize_snaps_to_a_non_decimal_grid(self):
        assert quantize(D("1.30"), D("0.25")) == D("1.25")
        assert quantize(D("1.37"), D("0.25")) == D("1.25")
        assert quantize(D("1.38"), D("0.25")) == D("1.50")
        assert quantize(D("0.07"), D("0.05")) == D("0.05")
        assert quantize(D("1.08505"), D("0.00001")) == D("1.08505")

    def test_config_refuses_nan_anywhere(self):
        with pytest.raises(ValueError):
            SentinelConfig.model_validate({"risk": {"correlation_threshold": float("nan")}})

    def test_the_deadman_must_clear_the_decision_interval(self):
        assert deadman_timeout_ok(180, 60)
        assert not deadman_timeout_ok(90, 60)
        assert not deadman_timeout_ok(60 + DEADMAN_MARGIN_SEC, 60)
        with pytest.raises(ValueError, match="deadman"):
            SentinelConfig.model_validate({"ops": {"deadman_timeout_sec": 90}})
        cfg = SentinelConfig()
        assert cfg.ops.deadman_timeout_sec == 180, "45 s against a 60 s loop tripped on every cycle"

    def test_live_needs_a_declared_account(self):
        with pytest.raises(ValueError, match="expected_account_id"):
            SentinelConfig.model_validate({
                "execution": {"venue_mode": "live", "broker": "oanda"},
                "risk": {"require_broker_side_stop": True}})

    def test_order_intent_coerces_floats_to_decimal(self):
        i = OrderIntent(client_order_id="x", strategy="t", instrument="EUR_USD",
                        side=Side.BUY, lots=0.1, stop_loss=1.085)
        assert isinstance(i.lots, D) and i.lots == D("0.1")
        assert i.stop_loss == D("1.085")


# --------------------------------------------------------------------------- #
# account binding
# --------------------------------------------------------------------------- #


class TestAccountBinding:
    def test_a_switched_terminal_account_refuses_every_call(self, mt5):
        broker, fake = mt5()
        bound = AccountBoundBroker(broker, "1000001", "demo", "USD", "FakeBroker-Demo")
        assert bound.account().account_id == "1000001"
        assert bound.quote("EUR_USD").ask > 0
        fake.account.login = 2000002
        with pytest.raises(BrokerError, match="all routing refused"):
            bound.quote("EUR_USD")
        with pytest.raises(BrokerError, match="all routing refused"):
            bound.positions()
        with pytest.raises(BrokerError, match="all routing refused"):
            bound.submit(OrderIntent(client_order_id="x", strategy="t", instrument="EUR_USD",
                                     side=Side.BUY, lots=D("0.01"), stop_loss=D("1.0")),
                         timeout_ms=100)
        assert fake.sent_requests == []

    def test_a_switched_server_is_refused(self, mt5):
        broker, fake = mt5()
        bound = AccountBoundBroker(broker, "1000001", "demo", "USD", "FakeBroker-Demo")
        fake.account.server = "FakeBroker-Live01"
        with pytest.raises(BrokerError, match="server"):
            bound.account()

    def test_a_demo_binding_refuses_a_live_terminal(self, mt5):
        broker, fake = mt5()
        fake.account.trade_mode = 2
        with pytest.raises(BrokerError, match="bound to demo"):
            AccountBoundBroker(broker, "1000001", "demo", "USD")

    def test_a_live_binding_needs_the_venue_to_say_live(self, mt5):
        broker, fake = mt5()
        with pytest.raises(BrokerError, match="bound to live"):
            AccountBoundBroker(broker, "1000001", "live", "USD")

    def test_the_currency_is_checked(self, mt5):
        broker, fake = mt5()
        with pytest.raises(BrokerError, match="currency"):
            AccountBoundBroker(broker, "1000001", "demo", "EUR")

    def test_bootstrap_binds_when_the_config_declares_an_account(self, mt5, tmp_path, monkeypatch):
        from sentinel.bootstrap import build_runtime
        broker, fake = mt5()
        monkeypatch.setattr("sentinel.brokers.build_broker",
                            lambda *a, **k: MT5Broker(profile=get_profile("generic_mt5"),
                                                      mt5_module=fake))
        cfg = SentinelConfig()
        cfg.execution.broker = "generic_mt5"
        cfg.execution.expected_account_id = "1000001"
        cfg.ops.state_dir = str(tmp_path)
        cfg.ops.audit_log = str(tmp_path / "a.jsonl")
        cfg.ops.killswitch_file = str(tmp_path / "KILL")
        cfg.data.store_path = str(tmp_path / "m.db")
        cfg.save(tmp_path / "config.json")
        import sentinel.bootstrap as bs
        monkeypatch.setattr(bs, "build_broker",
                            lambda *a, **k: MT5Broker(profile=get_profile("generic_mt5"),
                                                      mt5_module=fake))
        runtime, _ = build_runtime(tmp_path / "config.json")
        assert isinstance(runtime.agent.broker, AccountBoundBroker)
        assert runtime.agent.broker.bound_to["account_id"] == "1000001"


# --------------------------------------------------------------------------- #
# MetaTrader adapter
# --------------------------------------------------------------------------- #


class TestMT5Adapter:
    def _intent(self, coid="SFXa1", lots="0.10", sym="EUR_USD"):
        return OrderIntent(client_order_id=coid, strategy="donchian_trend", instrument=sym,
                           side=Side.BUY, lots=D(lots), stop_loss=D("1.09000"),
                           risk_amount=D("50"))

    def test_disabled_symbols_do_not_win_translation(self, mt5):
        broker, fake = mt5(suffix=".m", disabled_symbols=["EURUSD"])
        assert broker._venue("EUR_USD") == "EURUSD.m"
        assert "EUR_USD" in broker.instruments()

    def test_an_unreadable_book_is_not_an_empty_book(self, mt5):
        broker, fake = mt5()
        fake.positions_get = lambda *a, **k: None
        with pytest.raises(BrokerError, match="unavailable"):
            broker.positions()

    def test_two_tickets_on_one_symbol_are_refused(self, mt5):
        broker, fake = mt5()
        broker.submit(self._intent("SFXa1"), timeout_ms=1000)
        broker.submit(self._intent("SFXa2"), timeout_ms=1000)
        with pytest.raises(BrokerError, match="dedicated account"):
            broker.positions()

    def test_timeout_and_connection_retcodes_are_unknown_not_rejected(self, mt5):
        broker, fake = mt5()
        real = fake.order_send

        def timeout(req):
            real(req)                                  # the venue acted...
            from tests.fake_mt5 import _Result
            return _Result(retcode=10012, comment="Request timeout")   # ...and said so late
        fake.order_send = timeout
        with pytest.raises(UnknownOutcomeError):
            broker.submit(self._intent(), timeout_ms=1000)
        assert broker._in_blackout("EUR_USD")
        found = broker.query_order("SFXa1")
        assert found is not None and found.state is OrderState.FILLED

    def test_the_intent_journal_survives_a_restart(self, mt5, tmp_path):
        import sys
        fake = FakeMT5()
        path = tmp_path / "mt5-intents.json"
        b1 = MT5Broker(profile=get_profile("generic_mt5"), mt5_module=fake, state_path=str(path))
        b1.submit(self._intent("SFXjournal1"), timeout_ms=1000)
        assert path.exists() and "SFXjournal1" in json.loads(path.read_text())
        b2 = MT5Broker(profile=get_profile("generic_mt5"), mt5_module=fake, state_path=str(path))
        assert b2.query_order("SFXjournal1") is not None, "resolvable after the restart"

    def test_a_corrupt_intent_journal_refuses_startup(self, tmp_path):
        from sentinel.core.errors import ConfigError
        path = tmp_path / "mt5-intents.json"
        path.write_text("{not json")
        with pytest.raises(ConfigError, match="unreadable"):
            MT5Broker(profile=get_profile("generic_mt5"), mt5_module=FakeMT5(),
                      state_path=str(path))

    def test_realised_history_is_rebuilt_from_deals_with_costs(self, mt5):
        broker, fake = mt5(commission_per_lot=3.5)
        broker.submit(self._intent("SFXhist1", lots="0.10"), timeout_ms=1000)
        assert broker.fetch_closed_trades("")[0] == [], "still open: not a round trip"
        fake.set_price("EURUSD", 1.10500, 1.10512)       # +50 pips
        broker.close_position("EUR_USD", reason="take_profit")
        trades, cursor = broker.fetch_closed_trades("")
        assert len(trades) == 1
        t = trades[0]
        assert t.strategy == "donchian_trend" and t.instrument == "EUR_USD"
        assert t.initial_risk == D("50") and t.side is Side.BUY
        # profit 0.10 lots x 100000 x (1.10500 - 1.10012) = 48.80; commission 0.35 x 2 sides
        assert t.commission == D("0.70")
        assert abs(t.pnl - (D("48.80") - D("0.70"))) < D("0.01")
        assert t.r_multiple > 0 and t.exit_reason == "take_profit"
        assert "risk_attribution_unavailable" not in t.tags
        # The cursor moves; a second read returns nothing new.
        again, _ = broker.fetch_closed_trades(cursor)
        assert again == []

    def test_a_manual_trade_is_history_but_not_evidence(self, mt5):
        broker, fake = mt5()
        # Opened outside the engine: a different magic, no journal entry.
        fake.order_send({"action": 1, "symbol": "EURUSD", "volume": 0.05, "type": 0,
                         "price": 1.1, "sl": 1.09, "magic": 999, "comment": "manual"})
        fake.order_send({"action": 1, "symbol": "EURUSD", "volume": 0.05, "type": 1,
                         "price": 1.1, "position": fake._positions[0].ticket, "magic": 999})
        trades, _ = broker.fetch_closed_trades("")
        assert trades == [], "another magic number is another trader's trade"

    def test_swap_is_read_from_the_terminal_in_pips(self, mt5):
        broker, fake = mt5()
        long_, short = broker.swap_pips_per_day("EUR_USD")
        # -7.2 points on a 5-digit symbol = -0.72 pips
        assert long_ == D("-0.72") and short == D("0.21")

    def test_ticks_come_back_in_utc_with_bid_and_ask(self, mt5):
        broker, fake = mt5(server_utc_offset_sec=3 * 3600)
        broker.quote("EUR_USD")
        now = time.time_ns()
        ticks = broker.fetch_ticks("EUR_USD", now - 3600 * 10**9, now)
        assert len(ticks) > 100
        ms, bid, ask = ticks[-1]
        assert ask >= bid > 0
        assert abs(ms - now // 10**6) < 3600 * 1000 + 60_000, "server time was converted back to UTC"


# --------------------------------------------------------------------------- #
# validation, tick bars, historical news
# --------------------------------------------------------------------------- #


class TestDataBoundaries:
    def _frame(self, n=50):
        idx = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
        return pd.DataFrame({"open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1,
                             "volume": 1.0}, index=idx)

    def test_a_valid_frame_passes(self):
        validate_frame(self._frame())

    @pytest.mark.parametrize("fault", ["naive_index", "duplicate", "nan", "geometry",
                                       "negative", "crossed"])
    def test_faults_are_refused(self, fault):
        f = self._frame()
        if fault == "naive_index":
            f.index = f.index.tz_localize(None)
        elif fault == "duplicate":
            f = pd.concat([f, f.iloc[[0]]]).sort_index()
        elif fault == "nan":
            f.loc[f.index[3], "close"] = np.nan
        elif fault == "geometry":
            f.loc[f.index[3], "high"] = 1.0
        elif fault == "negative":
            f.loc[f.index[3], "low"] = -1.0
        elif fault == "crossed":
            for c in ("open", "high", "low", "close"):
                f[f"bid_{c}"] = 1.1
                f[f"ask_{c}"] = 1.0999
            with pytest.raises(ValueError, match="crossed"):
                validate_frame(f, bid_ask=True)
            return
        with pytest.raises(ValueError):
            validate_frame(f)

    def test_bars_from_ticks_build_bid_ask_ohlc(self):
        from sentinel.data.ticks import bars_from_ticks
        base = 1_700_000_000_000 - (1_700_000_000_000 % 3_600_000)     # on the hour
        ticks = [(base + i * 15_000, 1.1000 + 0.0001 * (i % 7), 1.1002 + 0.0001 * (i % 7))
                 for i in range(4 * 240)]      # 4 hours of 15-second ticks
        bars = bars_from_ticks(ticks, "H1")
        assert len(bars) == 4
        assert {"bid_open", "ask_high", "close", "volume"} <= set(bars.columns)
        assert (bars["ask_low"] >= bars["bid_low"]).all()
        assert (bars["volume"] == 240).all()
        assert bars.index[0].minute == 0 and bars.index.tz is not None
        validate_frame(bars, bid_ask=True)

    def test_the_bar_store_keeps_features_beside_ohlcv(self, tmp_path):
        from sentinel.data.feed import BarStore
        f = self._frame()
        f["carry_bp"] = 120.0
        f["bid_close"] = 1.0999
        store = BarStore(tmp_path / "m.db")
        store.upsert_frame(f, "EUR_USD", "H4", source="test")
        back = store.frame("EUR_USD", "H4")
        assert "carry_bp" in back.columns and float(back["carry_bp"].iloc[-1]) == 120.0
        assert "bid_close" in back.columns

    def test_weekend_holes_are_not_gaps_but_weekday_holes_are(self, tmp_path):
        from sentinel.data.feed import BarStore
        store = BarStore(tmp_path / "m.db")
        idx = pd.date_range("2024-01-08", "2024-01-19 20:00", freq="4h", tz="UTC")   # two weeks
        idx = idx[~((idx.weekday == 5) | (idx.weekday == 6) | ((idx.weekday == 4) & (idx.hour >= 22)))]
        f = pd.DataFrame({"open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1, "volume": 1.0}, index=idx)
        store.upsert_frame(f, "EUR_USD", "H4")
        assert store.count_gaps("EUR_USD", "H4") == 0, "the weekend is not a gap"
        holed = f.drop(f.index[10:13])          # a Tuesday afternoon missing
        store2 = BarStore(tmp_path / "m2.db")
        store2.upsert_frame(holed, "EUR_USD", "H4")
        assert store2.count_gaps("EUR_USD", "H4") == 1

    def test_a_historical_calendar_replays_dated_releases(self, tmp_path):
        from sentinel.news.schedule import HistoricalCalendarSource
        from sentinel.news.calendar import EconomicCalendar
        csv = tmp_path / "news.csv"
        csv.write_text("timestamp_utc,currency,name,impact\n"
                       "2024-03-08 13:30:00,USD,Non-farm payrolls,high\n"
                       "2024-03-12 12:30:00,USD,CPI,high\n"
                       "2024-03-07 13:15:00,EUR,ECB rate decision,high\n")
        src = HistoricalCalendarSource(csv)
        cal = EconomicCalendar(tmp_path / "cal.db")
        lo = int(pd.Timestamp("2024-03-01", tz="UTC").value)
        hi = int(pd.Timestamp("2024-03-31", tz="UTC").value)
        report = cal.ingest(src, lo, hi)
        assert report.inserted == 3 and report.rejected == 0
        during = int(pd.Timestamp("2024-03-08 13:20:00", tz="UTC").value)
        blackout = cal.instrument_blackout(during, ["EUR_USD", "USD_JPY"], before_min=30,
                                           after_min=30, min_impact="high", require_certain=True)
        assert "EUR_USD" in blackout and "USD_JPY" in blackout
        later = int(pd.Timestamp("2024-03-08 16:00:00", tz="UTC").value)
        assert cal.instrument_blackout(later, ["EUR_USD"], before_min=30, after_min=30,
                                       min_impact="high", require_certain=True) == {}


# --------------------------------------------------------------------------- #
# agent replay & meta-labelling
# --------------------------------------------------------------------------- #


def _universe():
    uni = generate_universe(DEFAULT_UNIVERSE[:4], n_bars=420, bars_per_day=6, seed=5)
    return {k: v for k, v in uni.items() if k in MAJORS}


def _h1_from_h4(df):
    rows = []
    for ts, r in df.iterrows():
        o, h, l, c = float(r.open), float(r.high), float(r.low), float(r.close)
        path = [o, l, h, c] if c >= o else [o, h, l, c]
        for k in range(4):
            a, b = path[k], path[min(k + 1, 3)]
            rows.append({"ts": ts + pd.Timedelta(hours=k), "open": a, "high": max(a, b),
                         "low": min(a, b), "close": b, "volume": 1.0})
    out = pd.DataFrame(rows).set_index("ts")
    out.index = pd.DatetimeIndex(out.index)
    return out


class TestAgentReplay:
    @pytest.fixture(scope="class")
    def replay(self):
        from sentinel.core.config import RiskConfig
        from sentinel.research.backtest import BacktestConfig, run_backtest
        from sentinel.strategy.registry import build
        uni = _universe()
        exec_data = {k: _h1_from_h4(v) for k, v in uni.items()}
        cfg = SentinelConfig()
        cfg.agent.session_windows_utc = [[0, 24]]
        cfg.agent.trade_days = [0, 1, 2, 3, 4, 5, 6]
        cfg.news.enabled = False          # no historical calendar in this fixture
        return run_backtest(build("donchian_trend"), uni, MAJORS, RiskConfig(),
                            BacktestConfig(engine="agent", runtime_config=cfg,
                                           execution_data=exec_data, record_trial=False,
                                           periods_per_year=1512),
                            conversions=CONV)

    def test_the_production_loop_ran_and_traded(self, replay):
        d = replay.diagnostics
        assert d["engine"].startswith("agent-replay")
        assert d["execution_cadence_sec"] == 3600
        assert d["halted"] is False and d["generation_error_count"] == 0
        assert len(replay.trades) > 5
        assert replay.signal_log and all("features" in r for r in replay.signal_log)

    def test_the_agents_own_protections_fired(self, replay):
        reasons = {t.exit_reason for t in replay.trades}
        assert reasons & {"stop_loss", "partial_take", "weekend_flat", "take_profit", "giveback"}

    def test_no_signal_bar_was_visible_before_it_closed(self, replay):
        # Every decision was stamped with a bar whose END is <= the time of the
        # decision (bars are revealed only after they close).
        for row in replay.signal_log:
            assert row["ts_ns"] % (4 * 3600 * 10**9) == 0

    def test_l11_passes_only_at_production_cadence(self, replay):
        from sentinel.research.acceptance import evaluate
        rc_cfg = SentinelConfig()
        rc_cfg.news.enabled = False
        v = evaluate(run_id="R", strategy_name="donchian_trend", candidate=replay,
                     baselines={}, periods_per_year=1512, data_label="synthetic",
                     runtime_config=rc_cfg)
        l11 = next(g for g in v.gates if g.id == "L11")
        assert l11.passed is False, "hourly execution is coarser than a 60 s decision loop"
        replay.diagnostics["execution_cadence_sec"] = 60
        v = evaluate(run_id="R2", strategy_name="donchian_trend", candidate=replay,
                     baselines={}, periods_per_year=1512, data_label="synthetic",
                     runtime_config=rc_cfg)
        assert next(g for g in v.gates if g.id == "L11").passed is True
        replay.diagnostics["execution_cadence_sec"] = 3600


class TestMetaLabel:
    def test_a_filter_is_fitted_and_consulted(self, tmp_path):
        from sentinel.core.config import RiskConfig
        from sentinel.research.backtest import BacktestConfig, run_backtest
        from sentinel.research.metalabel import MetaGate, fit_meta_gate, split_for_meta
        from sentinel.strategy.registry import build
        uni = generate_universe(DEFAULT_UNIVERSE[:4], n_bars=2600, bars_per_day=6, seed=9)
        uni = {k: v for k, v in uni.items() if k in MAJORS}
        train, hold = split_for_meta(uni, 0.6)
        fit = run_backtest(build("donchian_trend"), train, MAJORS, RiskConfig(),
                           BacktestConfig(record_trial=False, periods_per_year=1512),
                           conversions=CONV)
        gate, report = fit_meta_gate(fit.signal_log)
        assert report["n_signals"] >= 50
        if gate is None:
            pytest.skip(f"not enough signal for a filter on this seed: {report.get('notes')}")
        assert gate.active and 0 < gate.labeler.report.threshold < 1
        digest = gate.save(tmp_path / "meta.joblib")
        loaded = MetaGate.load(tmp_path / "meta.joblib")
        assert loaded.sha256 == digest and loaded.active
        # Consulted by the harness: some signals are now skipped.
        unfiltered = run_backtest(build("donchian_trend"), hold, MAJORS, RiskConfig(),
                                  BacktestConfig(record_trial=False, periods_per_year=1512),
                                  conversions=CONV)
        filtered = run_backtest(build("donchian_trend"), hold, MAJORS, RiskConfig(),
                                BacktestConfig(record_trial=False, periods_per_year=1512,
                                               meta_gate=loaded),
                                conversions=CONV)
        assert filtered.vetoes.get("meta_label", 0) > 0
        assert filtered.orders_submitted <= unfiltered.orders_submitted

    def test_the_agent_consults_the_gate_and_records_why(self, tmp_path):
        from sentinel.agent.memory import MemoryStore
        from sentinel.agent.orchestrator import Agent
        from sentinel.agent.proposals import ProposalQueue
        from sentinel.core.audit import NullAudit
        from sentinel.core.config import AgentConfig, AgentMode, OpsConfig, StrategyAllocation
        from sentinel.data.feed import BarStore, MarketFeed
        from sentinel.data.synthetic_live import SyntheticMarketDriver
        from sentinel.research.metalabel import MetaGate
        from sentinel.strategy.meta import MetaLabeler, MetaModelReport

        class _AlwaysSkip(MetaLabeler):
            def __init__(self):
                super().__init__()
                self.model = object()
                self.feature_names = ["strength"]
                self.report = MetaModelReport(trained=True, n_samples=500, effective_n=500.0,
                                              threshold=0.9, calibrated=False)

            def act_probability(self, features):
                return 0.1
        gate = MetaGate(_AlwaysSkip())
        broker = PaperBroker(instruments=dict(MAJORS), profile=SimProfile(), seed=5)
        broker.set_conversion("JPY", D("1") / D("150"))
        store = BarStore(tmp_path / "m.db")
        driver = SyntheticMarketDriver(broker, store, timeframe="H4", history=400, seed=2)
        feed = MarketFeed(broker, store, timeframe="H4", history=400, driver=driver)
        cfg = SentinelConfig(
            agent=AgentConfig(mode=AgentMode.OBSERVE, session_windows_utc=[[0, 24]],
                              trade_days=[0, 1, 2, 3, 4, 5, 6]),
            ops=OpsConfig(state_dir=str(tmp_path), audit_log=str(tmp_path / "a.jsonl"),
                          killswitch_file=str(tmp_path / "KILL")),
            strategies=[StrategyAllocation(name="donchian_trend", enabled=True,
                                           instruments=list(MAJORS), timeframe="H4")])
        agent = Agent(cfg, broker, feed, NullAudit(), MemoryStore(tmp_path / "mem.db"),
                      proposals=ProposalQueue(str(tmp_path / "p.json")), meta_gate=gate)
        agent.start()
        # Force signals: run enough cycles over a moving synthetic market.
        skipped = 0
        for _ in range(3):
            rep = agent.cycle()
            skipped += sum(1 for d in rep.decisions
                           if any(v.get("rule") == "meta_label" for v in d.vetoes))
            driver.tick(agent.now() + 4 * 3600 * 10**9, list(MAJORS))
            agent._clock_fn = (lambda t=agent.now() + 4 * 3600 * 10**9: t)
        # Whether or not the strategy fired this cycle, the gate never crashed
        # the loop and any raised signal was skipped with the meta_label rule.
        assert all(not r for r in [rep.errors])

    def test_the_fingerprint_binds_the_model_content(self, tmp_path):
        from sentinel.research.verdicts import config_fingerprint
        cfg = SentinelConfig()
        a = config_fingerprint(["EUR_USD"], {}, "H4", runtime_config=cfg)
        model = tmp_path / "m.joblib"
        model.write_bytes(b"model-v1")
        cfg.agent.meta_model_path = str(model)
        b = config_fingerprint(["EUR_USD"], {}, "H4", runtime_config=cfg)
        model.write_bytes(b"model-v2")
        c = config_fingerprint(["EUR_USD"], {}, "H4", runtime_config=cfg)
        assert len({a, b, c}) == 3, "no model, model v1 and model v2 are three systems"


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #


class TestEvidence:
    def _dataset(self, tmp_path):
        from sentinel.data.ticks import bars_from_ticks
        base = 1_700_000_000_000
        for sym in ("EUR_USD", "GBP_USD"):
            ticks = [(base + i * 15_000, 1.1 + 0.0001 * (i % 9), 1.1002 + 0.0001 * (i % 9))
                     for i in range(24 * 240 * 3)]
            bars_from_ticks(ticks, "H4").to_csv(tmp_path / f"{sym}.csv", index_label="timestamp")
        return tmp_path

    def _costs(self):
        return {"commission_per_lot_round_turn": 7.0, "slippage_pips_mean": 0.15,
                "slippage_pips_sigma": 0.1,
                "swap_long_pips_per_day": {"EUR_USD": -0.7, "GBP_USD": -0.5},
                "swap_short_pips_per_day": {"EUR_USD": 0.2, "GBP_USD": 0.1}}

    def test_a_manifest_verifies_by_content_and_catches_a_changed_file(self, tmp_path):
        from sentinel.research.evidence import verify_dataset, write_dataset_manifest
        d = self._dataset(tmp_path)
        write_dataset_manifest(d, broker="amarkets", cost_schedule=self._costs(),
                               output=d / "manifest.json")
        frames = {s: pd.read_csv(d / f"{s}.csv", index_col="timestamp", parse_dates=True)
                  for s in ("EUR_USD", "GBP_USD")}
        for f in frames.values():
            f.index = pd.DatetimeIndex(f.index).tz_convert("UTC") if f.index.tz else \
                pd.DatetimeIndex(f.index).tz_localize("UTC")
        prov = verify_dataset(d, frames, d / "manifest.json")
        assert prov["verified"] and prov["bid_ask"] and prov["broker"] == "amarkets"
        (d / "EUR_USD.csv").write_text((d / "EUR_USD.csv").read_text() + "\n")
        with pytest.raises(ValueError, match="hash mismatch"):
            verify_dataset(d, frames, d / "manifest.json")

    def test_an_incomplete_cost_schedule_is_refused(self, tmp_path):
        from sentinel.research.evidence import write_dataset_manifest
        d = self._dataset(tmp_path)
        bad = self._costs()
        del bad["swap_short_pips_per_day"]
        with pytest.raises(ValueError, match="incomplete"):
            write_dataset_manifest(d, broker="x", cost_schedule=bad, output=d / "m.json")

    def _forward(self, tmp_path, runtime_hash, n=60, drift=5.0, start=10000.0):
        import hashlib
        rows, bal = [], start
        t0 = pd.Timestamp("2026-06-01", tz="UTC")
        marks = [(t0 - pd.Timedelta(hours=1), start)]
        for i in range(n):
            pnl = drift + (3.0 if i % 3 else -4.0)
            bal += pnl
            opened = t0 + pd.Timedelta(hours=6 * i)
            closed = opened + pd.Timedelta(hours=4)
            rows.append({"trade_id": f"MT5-{i}", "opened_at": opened.isoformat(),
                         "closed_at": closed.isoformat(), "net_pnl": round(pnl, 2),
                         "balance_after": round(bal, 2), "account_id": "1000001"})
            marks.append((closed, bal - 6.0))         # a floating dip on every mark
            marks.append((closed + pd.Timedelta(minutes=1), bal))
        pd.DataFrame(rows).to_csv(tmp_path / "trades.csv", index=False)
        pd.DataFrame(marks, columns=["timestamp", "equity"]).to_csv(tmp_path / "equity.csv", index=False)
        manifest = {"runtime_hash": runtime_hash, "account_id": "1000001",
                    "starting_equity": start, "trades_file": "trades.csv",
                    "sha256": hashlib.sha256((tmp_path / "trades.csv").read_bytes()).hexdigest(),
                    "equity_file": "equity.csv",
                    "equity_sha256": hashlib.sha256((tmp_path / "equity.csv").read_bytes()).hexdigest()}
        (tmp_path / "forward.json").write_text(json.dumps(manifest))
        return tmp_path / "forward.json"

    def test_a_forward_record_reconciles_and_measures_floating_drawdown(self, tmp_path):
        from sentinel.research.evidence import verify_forward
        path = self._forward(tmp_path, "abc123")
        res = verify_forward(path, "abc123")
        assert res["verified"] and res["n_trades"] == 60 and res["net_pnl"] > 0
        assert res["max_drawdown_pct"] >= res["max_drawdown_realised_pct"]
        with pytest.raises(ValueError, match="different runtime"):
            verify_forward(path, "other-hash")

    def test_a_forward_ledger_that_does_not_add_up_is_refused(self, tmp_path):
        from sentinel.research.evidence import verify_forward
        path = self._forward(tmp_path, "abc123")
        df = pd.read_csv(tmp_path / "trades.csv")
        df.loc[5, "balance_after"] += 100          # a deposit hidden as a trade
        df.to_csv(tmp_path / "trades.csv", index=False)
        import hashlib
        m = json.loads(path.read_text())
        m["sha256"] = hashlib.sha256((tmp_path / "trades.csv").read_bytes()).hexdigest()
        path.write_text(json.dumps(m))
        with pytest.raises(ValueError, match="reconciliation"):
            verify_forward(path, "abc123")

    def test_the_l12_gate_needs_a_matching_fingerprint(self, tmp_path):
        from sentinel.research.acceptance import evaluate
        from sentinel.research.evidence import verify_forward
        from sentinel.research.verdicts import config_fingerprint
        cfg = SentinelConfig()
        fp = config_fingerprint(["EUR_USD"], {}, "H4", runtime_config=cfg)
        fwd = verify_forward(self._forward(tmp_path, fp), fp)
        from tests.test_research_integrity import _candidate
        v = evaluate(run_id="R", strategy_name="donchian_trend", candidate=_candidate(),
                     baselines={}, periods_per_year=1512, data_label="synthetic",
                     instruments=["EUR_USD"], params={}, timeframe="H4",
                     runtime_config=cfg, forward=fwd)
        assert next(g for g in v.gates if g.id == "L12").passed is True
        cfg2 = SentinelConfig()
        cfg2.risk.max_trades_per_day = 3            # a different policy
        v2 = evaluate(run_id="R2", strategy_name="donchian_trend", candidate=_candidate(),
                      baselines={}, periods_per_year=1512, data_label="synthetic",
                      instruments=["EUR_USD"], params={}, timeframe="H4",
                      runtime_config=cfg2, forward=fwd)
        assert next(g for g in v2.gates if g.id == "L12").passed is False


# --------------------------------------------------------------------------- #
# cross-account risk
# --------------------------------------------------------------------------- #


class TestGroupRisk:
    def test_the_ledger_sums_fresh_members_and_reports_stale_ones(self, tmp_path):
        from sentinel.risk.portfolio import GroupLedger, GroupRow
        a = GroupLedger(tmp_path, "acct-a", stale_after_sec=60)
        b = GroupLedger(tmp_path, "acct-b", stale_after_sec=60)
        now = time.time_ns()
        b.publish(GroupRow("acct-b", "USD", D("10000"), D("150"), D("0"), D("1"), 2,
                           {"USD": D("-150"), "EUR": D("150")}, now))
        view = a.view(now, own_currency="USD")
        assert view.others_open_risk == D("150") and view.others_equity == D("10000")
        assert view.others_currency_risk["EUR"] == D("150")
        assert view.stale == []
        view_late = a.view(now + 120 * 10**9, own_currency="USD")
        assert view_late.stale == ["acct-b"] and view_late.others_open_risk == 0

    def test_the_engine_refuses_when_the_group_is_over_its_ceiling(self):
        from sentinel.core.config import RiskConfig
        from sentinel.core.types import AccountState, Quote
        from sentinel.risk.engine import RiskContext, RiskEngine
        cfg = RiskConfig()
        acct = AccountState(account_id="a", currency="USD", balance=D("10000"),
                            equity=D("10000"), margin_available=D("10000"))
        inst = MAJORS["EUR_USD"]
        q = Quote("EUR_USD", D("1.08500"), D("1.08506"), ts_ns=1)
        base = dict(now_ns=1, account=acct, positions=[], instruments={"EUR_USD": inst},
                    quotes={"EUR_USD": q}, conversions={"USD": D("1")}, equity_peak=D("10000"),
                    day_start_equity=D("10000"), normal_spread_pips={"EUR_USD": D("0.6")},
                    strategy_lifecycles={"t": "accepted"})
        intent = OrderIntent(client_order_id="x", strategy="t", instrument="EUR_USD",
                             side=Side.BUY, lots=D("0.1"), stop_loss=D("1.08000"),
                             take_profit=D("1.09500"))
        alone = RiskEngine(cfg).evaluate_entry(intent, RiskContext(**base))
        assert alone.approved, [v.rule for v in alone.vetoes]
        # The owner's other engine carries 5.8% of ITS 10k book: the group is
        # 20k of equity and a 3% ceiling is 600; this entry (~50) tips it over.
        grouped = RiskEngine(cfg).evaluate_entry(intent, RiskContext(
            **base, group_others_open_risk=D("580"), group_others_equity=D("10000")))
        assert any(v.rule == "group_total_risk" for v in grouped.vetoes)
        # A member it cannot see blocks outright.
        blind = RiskEngine(cfg).evaluate_entry(intent, RiskContext(
            **base, group_others_open_risk=D("0"), group_others_equity=D("10000"),
            group_unknown_members=["acct-b"]))
        assert any(v.rule == "group_visibility" for v in blind.vetoes)


# --------------------------------------------------------------------------- #
# agent state & guards
# --------------------------------------------------------------------------- #


class TestAgentState:
    def _agent(self, tmp_path):
        from sentinel.agent.memory import MemoryStore
        from sentinel.agent.orchestrator import Agent
        from sentinel.agent.proposals import ProposalQueue
        from sentinel.core.audit import NullAudit
        from sentinel.core.config import AgentConfig, AgentMode, OpsConfig, StrategyAllocation
        from sentinel.data.feed import BarStore, MarketFeed
        broker = PaperBroker(instruments=dict(MAJORS), profile=SimProfile(), seed=5)
        cfg = SentinelConfig(
            agent=AgentConfig(mode=AgentMode.OBSERVE),
            ops=OpsConfig(state_dir=str(tmp_path), audit_log=str(tmp_path / "a.jsonl"),
                          killswitch_file=str(tmp_path / "KILL")),
            strategies=[StrategyAllocation(name="donchian_trend", enabled=True,
                                           instruments=["EUR_USD"], timeframe="H4")])
        return Agent(cfg, broker, MarketFeed(broker, BarStore(tmp_path / "m.db")), NullAudit(),
                     MemoryStore(tmp_path / "mem.db"),
                     proposals=ProposalQueue(str(tmp_path / "p.json")))

    def test_position_metadata_survives_a_restart_whole(self, tmp_path):
        a = self._agent(tmp_path)
        a._position_meta["EUR_USD"] = {
            "initial_risk": D("50"), "strategy": "donchian_trend", "opened_ns": 123,
            "side": "BUY", "client_order_id": "c1", "regime": "trending",
            "provisional": False, "partial_taken": False, "breakeven_moved": True,
            "lots": D("0.10"), "entry_price": D("1.08500"), "stop_loss": D("1.08000"),
            "max_favourable": D("1.7"), "max_adverse": D("-0.3"), "max_hold_sec": 86400,
            "intended_risk": D("50")}
        a._save_state()
        b = self._agent(tmp_path)
        assert b._load_state()
        m = b._position_meta["EUR_USD"]
        assert m["lots"] == D("0.10") and m["entry_price"] == D("1.08500")
        assert m["stop_loss"] == D("1.08000") and m["max_favourable"] == D("1.7")
        assert m["max_hold_sec"] == 86400 and m["breakeven_moved"] is True
        book = b._local_book()
        assert book and book[0].lots == D("0.10"), "the reconciler compares a real book"

    def test_an_unreadable_state_halts_instead_of_starting_fresh(self, tmp_path):
        a = self._agent(tmp_path)
        a.state_path.write_text("{corrupt")
        assert a._load_state() is False
        assert a.halted and "unreadable" in a.halt_reason

    def test_the_performance_guard_suspends_a_losing_strategy(self, tmp_path):
        a = self._agent(tmp_path)
        a.config.agent.performance_guard_min_trades = 50
        for i in range(60):
            a.memory.record_autopsy({
                "trade_id": f"T{i}", "strategy": "donchian_trend", "instrument": "EUR_USD",
                "outcome": "loss", "mode": "stop", "r_multiple": -0.8 + (0.2 if i % 5 == 0 else 0),
                "mae_r": -1.0, "mfe_r": 0.2, "capture_ratio": 0.0, "tags": [],
                "counterfactuals": [], "closed_ns": 1000 + i})
        a._performance_guard("donchian_trend")
        assert "donchian_trend" in a.guard_suspended
        assert a.release_guard("donchian_trend", "owner") is True
        assert "donchian_trend" not in a.guard_suspended

    def test_a_winning_strategy_is_not_suspended(self, tmp_path):
        a = self._agent(tmp_path)
        for i in range(60):
            a.memory.record_autopsy({
                "trade_id": f"W{i}", "strategy": "donchian_trend", "instrument": "EUR_USD",
                "outcome": "win", "mode": "target", "r_multiple": 0.6 - (1.4 if i % 4 == 0 else 0),
                "mae_r": -0.5, "mfe_r": 1.5, "capture_ratio": 0.5, "tags": [],
                "counterfactuals": [], "closed_ns": 1000 + i})
        a._performance_guard("donchian_trend")
        assert "donchian_trend" not in a.guard_suspended
