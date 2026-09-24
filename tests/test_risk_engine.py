"""Risk engine behaviour. Every rule gets a test that makes it fire."""

from decimal import Decimal as D

import pytest

from sentinel.core.types import DataQuality, OrderIntent, Position, Side
from sentinel.risk.engine import RiskEngine, Severity


def intent(symbol="EUR_USD", side=Side.BUY, lots=D("0.1"), stop=D("1.0820"),
           target=D("1.0910"), strategy="test", coid="C1"):
    return OrderIntent(client_order_id=coid, strategy=strategy, instrument=symbol,
                       side=side, lots=lots, stop_loss=stop, take_profit=target)


def rules(decision):
    return {v.rule for v in decision.vetoes}


class TestHappyPath:
    def test_clean_entry_is_approved(self, engine, ctx):
        d = engine.evaluate_entry(intent(), ctx)
        assert d.approved, [v.to_dict() for v in d.vetoes]
        assert d.approved_lots > 0
        assert d.risk_pct <= ctx.account.equity and d.risk_pct > 0

    def test_sizing_respects_the_risk_budget(self, engine, ctx):
        d = engine.evaluate_entry(intent(), ctx)
        # 0.5% of 10000 = 50, minus lot-granularity rounding (downward only)
        assert D("40") <= d.risk_amount <= D("50")


class TestAbsoluteStops:
    def test_kill_switch_halts(self, engine, ctx):
        ctx.kill_switch = True
        d = engine.evaluate_entry(intent(), ctx)
        assert not d.approved and d.halting and "kill_switch" in rules(d)

    def test_halt_flag_blocks(self, engine, ctx):
        ctx.halted, ctx.halt_reason = True, "operator"
        assert "halted" in rules(engine.evaluate_entry(intent(), ctx))

    def test_unresolved_order_blocks_new_risk(self, engine, ctx):
        ctx.unresolved_orders = 1
        assert "unresolved_orders" in rules(engine.evaluate_entry(intent(), ctx))

    def test_offline_blocks(self, engine, ctx):
        ctx.connectivity_ok, ctx.offline_seconds = False, 600
        assert "connectivity" in rules(engine.evaluate_entry(intent(), ctx))


class TestStopDiscipline:
    def test_missing_stop_is_refused(self, engine, ctx):
        i = OrderIntent(client_order_id="C", strategy="test", instrument="EUR_USD",
                        side=Side.BUY, lots=D("0.1"))
        assert "no_stop" in rules(engine.evaluate_entry(i, ctx))

    def test_stop_on_the_wrong_side_is_refused(self, engine, ctx):
        i = OrderIntent(client_order_id="C", strategy="test", instrument="EUR_USD",
                        side=Side.BUY, lots=D("0.1"), stop_loss=D("1.0900"))
        assert "stop_side" in rules(engine.evaluate_entry(i, ctx))

    def test_stop_below_the_floor_is_refused(self, engine, ctx):
        d = engine.evaluate_entry(intent(stop=D("1.08470"), target=D("1.08560")), ctx)
        assert "stop_too_tight" in rules(d)

    def test_poor_reward_risk_is_refused(self, engine, ctx):
        d = engine.evaluate_entry(intent(stop=D("1.0820"), target=D("1.0860")), ctx)
        assert "reward_risk" in rules(d)


class TestCostBarrier:
    def test_scalp_target_is_closed_by_cost(self, engine, ctx):
        """A 3-pip target needs ~66% accuracy after cost. The engine says no."""
        eu = ctx.instruments["EUR_USD"]
        mid = ctx.quotes["EUR_USD"].mid
        d = engine.evaluate_entry(
            intent(stop=eu.round_price(mid - eu.pip * 3),
                   target=eu.round_price(mid + eu.pip * 3)), ctx)
        assert not d.approved
        assert {"stop_too_tight", "cost_barrier"} & rules(d)


class TestLossBudgets:
    def test_daily_loss_limit(self, engine, ctx):
        ctx.day_pnl = D("-250")  # 2.5% of 10k, limit is 2%
        assert "daily_loss" in rules(engine.evaluate_entry(intent(), ctx))

    def test_weekly_loss_limit(self, engine, ctx):
        ctx.week_pnl = D("-450")
        assert "weekly_loss" in rules(engine.evaluate_entry(intent(), ctx))

    def test_drawdown_halt(self, engine, ctx):
        ctx.equity_peak = D("12000")   # equity 10000 => 16.7% drawdown
        d = engine.evaluate_entry(intent(), ctx)
        assert "max_drawdown" in rules(d) and d.halting

    def test_profit_lock_pauses_new_entries(self, engine, ctx):
        ctx.day_pnl = D("400")   # +4%, lock is 3%
        assert "profit_lock" in rules(engine.evaluate_entry(intent(), ctx))

    def test_ladder_reduces_size(self, engine, ctx):
        ctx.equity_peak = D("10600")   # ~5.66% drawdown => x0.50
        d = engine.evaluate_entry(intent(), ctx)
        assert d.risk_multiplier == D("0.5")
        assert d.risk_amount < D("30")


class TestFrequency:
    def test_daily_trade_cap(self, engine, ctx):
        ctx.trades_today = 6
        assert "frequency_day" in rules(engine.evaluate_entry(intent(), ctx))

    def test_entry_spacing(self, engine, ctx):
        ctx.last_entry_ns = ctx.now_ns - 60 * 1_000_000_000
        assert "entry_spacing" in rules(engine.evaluate_entry(intent(), ctx))

    def test_annual_cost_projection_blocks_overtrading(self, engine, ctx):
        ctx.projected_trades_per_year = 6000
        d = engine.evaluate_entry(intent(), ctx)
        assert "annual_cost" in rules(d)


class TestPortfolioLimits:
    def test_currency_leg_netting_catches_the_hidden_dollar_bet(self, engine, ctx):
        """Three separate longs are one short-USD position."""
        ctx.positions = [
            Position("EUR_USD", Side.BUY, D("0.1"), D("1.0850"), 0, initial_risk=D("50")),
            Position("GBP_USD", Side.BUY, D("0.1"), D("1.2700"), 0, initial_risk=D("50")),
            Position("AUD_USD", Side.BUY, D("0.1"), D("0.6600"), 0, initial_risk=D("50")),
        ]
        d = engine.evaluate_entry(intent("USD_CHF", side=Side.SELL,
                                         stop=D("0.88500"), target=D("0.87000")), ctx)
        assert "currency_exposure" in rules(d)

    def test_correlated_cluster_limit(self, engine, ctx):
        ctx.positions = [
            Position("EUR_USD", Side.BUY, D("0.1"), D("1.0850"), 0, initial_risk=D("50")),
        ]
        ctx.correlations = {("EUR_USD", "GBP_USD"): 0.88}
        d = engine.evaluate_entry(intent("GBP_USD", stop=D("1.2670"), target=D("1.2760")), ctx)
        assert "correlated_risk" in rules(d)

    def test_max_open_positions(self, engine, ctx):
        ctx.positions = [
            Position(s, Side.BUY, D("0.05"), ctx.quotes[s].mid, 0, initial_risk=D("10"))
            for s in ["EUR_USD", "GBP_USD", "AUD_USD", "USD_JPY"]
        ]
        assert "max_positions" in rules(engine.evaluate_entry(intent("USD_CHF",
                stop=D("0.87500"), target=D("0.89000")), ctx))

    def test_margin_buffer(self, engine, ctx):
        ctx.account.margin_available = D("50")
        assert "margin" in rules(engine.evaluate_entry(intent(), ctx))


class TestDataIntegrity:
    def test_stale_price_blocks(self, engine, ctx):
        ctx.data_age_sec["EUR_USD"] = 600
        assert "stale_data" in rules(engine.evaluate_entry(intent(), ctx))

    def test_gap_quality_blocks(self, engine, ctx):
        ctx.data_quality["EUR_USD"] = DataQuality.GAP
        assert "data_quality" in rules(engine.evaluate_entry(intent(), ctx))

    def test_clock_skew_blocks(self, engine, ctx):
        ctx.clock_skew_ms = 4000.0
        assert "clock_skew" in rules(engine.evaluate_entry(intent(), ctx))

    def test_spread_blowout_blocks(self, engine, ctx):
        eu = ctx.instruments["EUR_USD"]
        q = ctx.quotes["EUR_USD"]
        from sentinel.core.types import Quote
        ctx.quotes["EUR_USD"] = Quote("EUR_USD", q.bid, eu.round_price(q.bid + eu.pip * 5),
                                      ts_ns=q.ts_ns, source="test")
        assert "spread" in rules(engine.evaluate_entry(intent(), ctx))

    def test_news_blackout_blocks(self, engine, ctx):
        ctx.news_blackout["EUR_USD"] = "US CPI"
        assert "news_blackout" in rules(engine.evaluate_entry(intent(), ctx))


class TestLifecycleGate:
    def test_unaccepted_strategy_cannot_touch_real_money(self, engine, ctx):
        ctx.live_money = True
        ctx.strategy_lifecycles = {"test": "experimental"}
        assert "lifecycle" in rules(engine.evaluate_entry(intent(), ctx))

    def test_accepted_strategy_passes(self, engine, ctx):
        ctx.live_money = True
        ctx.strategy_lifecycles = {"test": "accepted"}
        assert "lifecycle" not in rules(engine.evaluate_entry(intent(), ctx))


class TestExitsAreNeverBlocked:
    def test_exit_allowed_even_when_halted(self, engine, ctx):
        ctx.halted = ctx.kill_switch = True
        pos = Position("EUR_USD", Side.BUY, D("0.1"), D("1.0850"), 0)
        assert engine.evaluate_exit(pos, ctx).approved


class TestPortfolioAlarms:
    def test_unprotected_position_alarms(self, engine, ctx):
        ctx.positions = [Position("EUR_USD", Side.BUY, D("0.1"), D("1.0850"), 0,
                                  broker_stop_confirmed=False)]
        assert any(v.rule == "unprotected_position" for v in engine.portfolio_alarms(ctx))
