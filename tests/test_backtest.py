"""The backtester and the simulator it runs on."""

import datetime as dt
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from sentinel.brokers.paper import PaperBroker, SimProfile
from sentinel.core.config import RiskConfig
from sentinel.core.errors import PermanentError
from sentinel.core.ids import client_order_id
from sentinel.core.money import Instrument
from sentinel.core.types import OrderIntent, OrderState, Quote, Side
from sentinel.data.synthetic import generate_universe
from sentinel.research.backtest import BacktestConfig, _bars_to_ns, run_backtest
from sentinel.strategy.baselines import CoinFlip, NoTrade
from sentinel.strategy.library import DonchianTrend

EU = Instrument("EUR_USD", "EUR", "USD")
INSTRUMENTS = {
    "EUR_USD": EU,
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
    "AUD_USD": Instrument("AUD_USD", "AUD", "USD"),
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001")),
    "USD_CHF": Instrument("USD_CHF", "USD", "CHF"),
}
CONVERSIONS = {"USD": D("1"), "JPY": D("1") / D("150"), "CHF": D("1") / D("0.88")}


class TestTimeUnits:
    def test_bar_timestamps_are_nanoseconds(self):
        """pandas 3 gives date_range a microsecond dtype; a bare astype('int64')
        would make every duration 1000x too small while still looking plausible."""
        idx = pd.date_range("2026-01-01", periods=5, freq="4h", tz="UTC")
        ns = _bars_to_ns(idx)
        assert np.all(np.diff(ns) == 4 * 3600 * 10 ** 9)

    def test_naive_index_is_refused(self):
        with pytest.raises(TypeError, match="timezone-aware"):
            _bars_to_ns(pd.date_range("2026-01-01", periods=3, freq="h"))


class TestPaperBroker:
    @pytest.fixture
    def broker(self):
        t0 = int(dt.datetime(2026, 3, 2, 10, tzinfo=dt.timezone.utc).timestamp() * 1e9)
        b = PaperBroker(instruments={"EUR_USD": EU}, starting_balance=D("10000"),
                        seed=3, start_ns=t0)
        b.on_quote(Quote("EUR_USD", D("1.08500"), D("1.08506"), ts_ns=t0))
        return b

    def test_a_duplicate_client_id_is_deduplicated(self, broker):
        i = OrderIntent(client_order_id="X1", strategy="s", instrument="EUR_USD",
                        side=Side.BUY, lots=D("0.1"), stop_loss=D("1.0800"),
                        risk_amount=D("50"))
        first = broker.submit(i)
        second = broker.submit(i)
        assert first.state is OrderState.FILLED
        assert second.reject_reason == "DUPLICATE_CLIENT_ORDER_ID"
        assert len(broker.positions()) == 1

    def test_fills_are_never_at_the_mid(self, broker):
        i = OrderIntent(client_order_id="X2", strategy="s", instrument="EUR_USD",
                        side=Side.BUY, lots=D("0.1"), stop_loss=D("1.0800"))
        res = broker.submit(i)
        assert res.fills[0].price >= broker.quote("EUR_USD").ask

    def test_widening_a_stop_is_refused(self, broker):
        broker.submit(OrderIntent(client_order_id="X3", strategy="s",
                                  instrument="EUR_USD", side=Side.BUY, lots=D("0.1"),
                                  stop_loss=D("1.0800")))
        with pytest.raises(PermanentError, match="widen"):
            broker.modify_position("EUR_USD", stop_loss=D("1.0750"))
        assert broker.modify_position("EUR_USD", stop_loss=D("1.0830")) is True

    def test_a_stop_fills_at_the_worse_of_stop_and_market(self, broker):
        t = broker.now_ns
        broker.submit(OrderIntent(client_order_id="X4", strategy="s",
                                  instrument="EUR_USD", side=Side.BUY, lots=D("0.1"),
                                  stop_loss=D("1.08000"), risk_amount=D("50")))
        # A gap straight through the stop.
        t += 60_000_000_000
        broker.set_time(t)
        broker.on_quote(Quote("EUR_USD", D("1.07500"), D("1.07506"), ts_ns=t))
        trades = broker.closed_trades
        assert len(trades) == 1
        assert trades[0].exit_reason == "stop_loss"
        # Filled at the gapped price, not at the stop: worse than -1R.
        assert float(trades[0].r_multiple) < -1.0

    def test_asian_session_costs_more_than_london(self, broker):
        london = int(dt.datetime(2026, 3, 2, 10, tzinfo=dt.timezone.utc).timestamp() * 1e9)
        asia = int(dt.datetime(2026, 3, 3, 1, tzinfo=dt.timezone.utc).timestamp() * 1e9)
        assert broker._spread_pips(EU, asia) > broker._spread_pips(EU, london)

    def test_stress_profile_multiplies_cost(self):
        base = SimProfile()
        stressed = base.stressed(2.0, 2.0)
        assert stressed.cost_multiplier == D("2.0")
        assert stressed.latency_multiplier == 2.0
        assert base.cost_multiplier == D("1.0")   # the original is untouched


class TestBacktest:
    @pytest.fixture(scope="class")
    def universe(self):
        return generate_universe(n_bars=900, bars_per_day=6, dollar_factor_strength=0.6)

    def test_no_trade_baseline_produces_nothing(self, universe):
        res = run_backtest(NoTrade(), universe, INSTRUMENTS, RiskConfig(),
                           BacktestConfig(label="nt", periods_per_year=1512),
                           conversions=CONVERSIONS)
        assert res.performance.n_trades == 0
        assert res.performance.sharpe == 0.0          # the degeneracy guard holds
        assert res.performance.max_drawdown_pct == 0.0

    def test_a_run_produces_coherent_accounting(self, universe):
        res = run_backtest(DonchianTrend(), universe, INSTRUMENTS, RiskConfig(),
                           BacktestConfig(label="d", periods_per_year=1512),
                           conversions=CONVERSIONS)
        p = res.performance
        assert p.n_trades > 0
        assert not res.diagnostics["generation_errors"]
        # The accounting identity is exact: a closed trade's P&L is already net
        # of its own cost, so gross minus cost is the realised ledger.
        assert abs(p.gross_profit - p.total_cost - p.realised_pnl) < 1e-6
        # And the equity curve agrees with the trade ledger once everything is
        # flat -- if these drift apart, a fill is being lost somewhere.
        assert abs(p.realised_pnl - p.net_profit) < 0.05
        # Holding times must be on the scale of the bar, not a fraction of it.
        holds = [t.duration_sec for t in res.trades]
        assert np.median(holds) > 4 * 3600
        assert p.total_cost > 0

    def test_the_stress_run_is_worse_than_the_base_run(self, universe):
        base = run_backtest(DonchianTrend(), universe, INSTRUMENTS, RiskConfig(),
                            BacktestConfig(label="b", periods_per_year=1512),
                            conversions=CONVERSIONS)
        stress = run_backtest(DonchianTrend(), universe, INSTRUMENTS, RiskConfig(),
                              BacktestConfig(label="s", periods_per_year=1512,
                                             cost_multiplier=2.0, latency_multiplier=2.0),
                              conversions=CONVERSIONS)
        assert stress.performance.cost_drag_pct > base.performance.cost_drag_pct

    def test_the_risk_engine_is_in_the_backtest_path(self, universe):
        """A research harness that skips the live risk rules tests a system that
        will never be deployed."""
        tight = RiskConfig(max_open_positions=1, max_trades_per_day=1)
        res = run_backtest(DonchianTrend(), universe, INSTRUMENTS, tight,
                           BacktestConfig(label="t", periods_per_year=1512),
                           conversions=CONVERSIONS)
        assert res.vetoes, "no vetoes recorded: the risk engine was bypassed"
        assert {"max_positions", "frequency_day"} & set(res.vetoes)

    def test_a_run_is_reproducible(self, universe):
        cfg = dict(label="r", periods_per_year=1512, seed=42)
        a = run_backtest(DonchianTrend(), universe, INSTRUMENTS, RiskConfig(),
                         BacktestConfig(**cfg), conversions=CONVERSIONS)
        b = run_backtest(DonchianTrend(), universe, INSTRUMENTS, RiskConfig(),
                         BacktestConfig(**cfg), conversions=CONVERSIONS)
        assert a.performance.net_profit == b.performance.net_profit
        assert a.performance.n_trades == b.performance.n_trades
