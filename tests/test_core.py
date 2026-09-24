"""Core invariants: money, clock, ids, audit, config."""

from decimal import Decimal as D

import numpy as np
import pytest

from sentinel.core.audit import AuditLog, EventType
from sentinel.core.clock import ClockMonitor, Deadline, MonotonicGuard, Stopwatch, wall_ns
from sentinel.core.config import RiskConfig, SentinelConfig, diff_configs
from sentinel.core.ids import client_order_id, intent_hash
from sentinel.core.money import (
    CostModel, Instrument, annual_cost_pct_of_equity, break_even_win_rate, dec,
    expectancy_pips, min_equity_for_granularity, pip_value_account,
)
from sentinel.core.types import OrderIntent, Quote, Side

EU = Instrument("EUR_USD", "EUR", "USD")
UJ = Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001"))


class TestMoney:
    @pytest.mark.parametrize("value,expected", [
        (np.float64(1.085), D("1.085")), (np.int64(5), D("5")),
        (0.1, D("0.1")), ("1.2345", D("1.2345")), (7, D("7")),
    ])
    def test_decimal_coercion(self, value, expected):
        assert dec(value) == expected

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), True])
    def test_non_finite_and_bool_are_refused(self, bad):
        with pytest.raises(ValueError):
            dec(bad)

    def test_lot_rounding_never_increases_risk(self):
        for raw in ["0.037", "0.019", "0.999", "0.01"]:
            assert EU.round_lots_down(D(raw)) <= D(raw)

    @pytest.mark.parametrize("target,stop,cost,expected", [
        (3, 3, "1.0", "0.667"), (3, 3, "0.45", "0.575"),
        (10, 10, "0.45", "0.5225"), (100, 100, "0.45", "0.50225"),
    ])
    def test_break_even_matches_the_research_table(self, target, stop, cost, expected):
        got = break_even_win_rate(target, stop, cost)
        assert abs(got - D(expected)) < D("0.001")

    def test_break_even_handles_asymmetric_targets(self):
        # 2:1 reward/risk with no cost needs 1/3 accuracy.
        assert abs(break_even_win_rate(20, 10, 0) - D("0.3333")) < D("0.001")

    def test_annual_cost_reproduces_the_brief(self):
        got = annual_cost_pct_of_equity(round_trip_cost_bp=D("0.39"), leverage=D("30"),
                                        trades_per_year=2500)
        assert D("290") < got < D("295")

    def test_expectancy_sign(self):
        assert expectancy_pips("0.55", 10, 10, "0.45") > 0
        assert expectancy_pips("0.45", 10, 10, "0.45") < 0

    def test_min_equity_matches_the_brief(self):
        got = min_equity_for_granularity(30, D("0.01"), EU, D("10"))
        assert abs(got - D("1500")) < D("1")

    def test_missing_conversion_is_an_error_not_a_one(self):
        with pytest.raises(ValueError, match="refusing to assume 1.0"):
            pip_value_account(UJ, D("0.1"), "USD")

    def test_jpy_pip_geometry(self):
        assert UJ.pip_value_quote(D("1")) == D("1000")   # 100k units x 0.01
        assert UJ.price_to_pips(D("1.50")) == D("150")


class TestClock:
    def test_deadline_uses_the_monotonic_clock(self):
        import time
        d = Deadline(30)
        time.sleep(0.05)
        assert d.expired and d.remaining_ms == 0

    def test_stopwatch_measures(self):
        import time
        with Stopwatch() as sw:
            time.sleep(0.02)
        assert 10 < sw.elapsed_ms < 500

    def test_skew_estimate(self):
        cm = ClockMonitor(window=32)
        for _ in range(20):
            cm.observe(wall_ns() - 5_000_000, round_trip_ns=2_000_000)
        assert 3.5 < cm.median_skew_ms < 4.5
        assert cm.healthy(50) and not cm.healthy(1)

    def test_monotonic_guard_starts_clean(self):
        g = MonotonicGuard()
        assert g.check() is None and g.regressions == 0


class TestIdempotencyKeys:
    def test_a_retry_reuses_the_same_key(self):
        a = client_order_id(strategy="s", instrument="EUR_USD", side="BUY",
                            decision_ns=1, seq=1)
        b = client_order_id(strategy="s", instrument="EUR_USD", side="BUY",
                            decision_ns=1, seq=1)
        assert a == b

    @pytest.mark.parametrize("kwargs", [
        {"seq": 2}, {"decision_ns": 2}, {"side": "SELL"}, {"instrument": "GBP_USD"},
        {"strategy": "t"}, {"attempt": 1},
    ])
    def test_a_different_intent_produces_a_different_key(self, kwargs):
        base = dict(strategy="s", instrument="EUR_USD", side="BUY", decision_ns=1, seq=1)
        assert client_order_id(**base) != client_order_id(**{**base, **kwargs})

    def test_key_fits_the_tightest_venue_limit(self):
        key = client_order_id(strategy="a" * 80, instrument="EUR_USD", side="BUY",
                              decision_ns=1, seq=1)
        assert len(key) <= 32 and key.isalnum()

    def test_intent_hash_is_order_independent(self):
        assert intent_hash({"a": 1, "b": 2}) == intent_hash({"b": 2, "a": 1})


class TestAuditChain:
    def test_tampering_is_detected(self, tmp_path):
        import json
        path = tmp_path / "a.jsonl"
        log = AuditLog(path, fsync_every_record=False)
        for i in range(6):
            log.append(EventType.SIGNAL, {"i": i})
        log.close()
        assert AuditLog(path).verify()[0] is True

        lines = path.read_text().splitlines()
        rec = json.loads(lines[3])
        rec["payload"]["i"] = 999
        lines[3] = json.dumps(rec, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")
        ok, bad, msg = AuditLog(path).verify()
        assert ok is False and bad == 4

    def test_deleting_a_record_breaks_the_chain(self, tmp_path):
        path = tmp_path / "a.jsonl"
        log = AuditLog(path, fsync_every_record=False)
        for i in range(5):
            log.append(EventType.SIGNAL, {"i": i})
        log.close()
        lines = path.read_text().splitlines()
        del lines[2]
        path.write_text("\n".join(lines) + "\n")
        assert AuditLog(path).verify()[0] is False

    def test_reopening_continues_the_chain(self, tmp_path):
        path = tmp_path / "a.jsonl"
        a = AuditLog(path, fsync_every_record=False)
        a.append(EventType.SIGNAL, {"x": 1})
        a.close()
        b = AuditLog(path, fsync_every_record=False)
        b.append(EventType.SIGNAL, {"x": 2})
        b.close()
        assert AuditLog(path).verify()[0] is True


class TestConfigGuards:
    def test_live_refuses_unaccepted_strategies(self):
        from sentinel.core.config import ExecutionConfig, ExecutionVenueMode, StrategyAllocation
        with pytest.raises(ValueError, match="not in 'accepted' lifecycle"):
            SentinelConfig(
                execution=ExecutionConfig(venue_mode=ExecutionVenueMode.LIVE, broker="oanda"),
                strategies=[StrategyAllocation(name="x", enabled=True, lifecycle="hypothesis")])

    def test_accepted_requires_a_run_id(self):
        from sentinel.core.config import StrategyAllocation
        with pytest.raises(ValueError, match="validation run"):
            StrategyAllocation(name="x", lifecycle="accepted")

    def test_ladder_must_be_monotone(self):
        with pytest.raises(ValueError, match="ascending"):
            RiskConfig(ladder=[{"drawdown_pct": 5.0, "risk_multiplier": 0.5},
                               {"drawdown_pct": 3.0, "risk_multiplier": 0.7}])
        with pytest.raises(ValueError, match="non-increasing"):
            RiskConfig(ladder=[{"drawdown_pct": 3.0, "risk_multiplier": 0.5},
                               {"drawdown_pct": 5.0, "risk_multiplier": 0.9}])

    def test_loss_budget_ordering(self):
        with pytest.raises(ValueError, match="daily loss limit"):
            RiskConfig(daily_loss_limit_pct=D("9"), weekly_loss_limit_pct=D("4"))

    def test_a_single_trade_must_always_be_able_to_pass(self):
        with pytest.raises(ValueError, match="no single"):
            RiskConfig(risk_per_trade_pct=D("1.5"), max_currency_exposure_pct=D("0.5"))

    def test_round_trip_and_diff(self, tmp_path):
        c = SentinelConfig()
        p = c.save(tmp_path / "cfg.json")
        loaded = SentinelConfig.load(p)
        assert loaded.version == c.version
        assert diff_configs(c, loaded) == []
        bumped = c.bump("tester")
        bumped.risk.max_open_positions = 2
        changes = {d.path for d in diff_configs(c, bumped)}
        assert "risk.max_open_positions" in changes


class TestTypeInvariants:
    def test_crossed_quote_is_refused(self):
        with pytest.raises(ValueError, match="crossed"):
            Quote("X", D("2"), D("1"), ts_ns=1)

    def test_executable_price_is_never_the_mid(self):
        q = Quote("EUR_USD", D("1.0850"), D("1.0852"), ts_ns=1)
        assert q.price_for(Side.BUY) == q.ask
        assert q.price_for(Side.SELL) == q.bid
        assert q.price_for(Side.BUY) != q.mid

    def test_stop_and_target_must_bracket_the_side(self):
        with pytest.raises(ValueError, match="stop must sit below"):
            OrderIntent(client_order_id="c", strategy="s", instrument="EUR_USD",
                        side=Side.BUY, lots=D("0.1"),
                        stop_loss=D("1.09"), take_profit=D("1.08"))

    def test_zero_lots_is_refused(self):
        with pytest.raises(ValueError, match="positive"):
            OrderIntent(client_order_id="c", strategy="s", instrument="EUR_USD",
                        side=Side.BUY, lots=D("0"))
