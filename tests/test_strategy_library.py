"""The strategy library, family by family, and the plugin mechanism.

Three properties are asserted for EVERY registered strategy, because with a
library this size a property that is only checked for the strategies someone
remembered is not checked at all:

1. **Causality.** ``generate`` at bar ``i`` must return the same signal whether
   or not bars after ``i`` exist. The indicator tests cover the toolkit; this
   covers the strategies, which is where a stray ``.iloc[index + 1]`` or an
   un-shifted comparison actually gets written.
2. **Every signal carries a usable stop.** The risk engine vetoes an entry
   without one, and ``OrderIntent`` refuses a stop on the wrong side of the
   target, so a strategy that gets this wrong produces nothing but veto counts.
   The assertion goes all the way to constructing the intent, because that is
   the contract the signal actually has to satisfy.
3. **Parameters validate.** A strategy whose reward/risk cannot clear the cost
   barrier must refuse to be constructed rather than quietly trade.

And one property of the library as a whole: nothing declares itself accepted.
"""

from decimal import Decimal as D

import pytest

from sentinel.core.money import Instrument
from sentinel.core.types import OrderIntent, Side
from sentinel.data.synthetic import generate_universe
from sentinel.strategy import registry as R
from sentinel.strategy.families._common import atr_signal, level_signal

# The full five-pair universe, not a subset. The cross-sectional strategies
# refuse to rank fewer than four instruments -- correctly, since a "top decile"
# of three is not a cross-section -- and running them on a short universe would
# make their tests pass with zero signals.
INSTRUMENTS = {
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
    "AUD_USD": Instrument("AUD_USD", "AUD", "USD"),
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001")),
    "USD_CHF": Instrument("USD_CHF", "USD", "CHF"),
}

# Hourly bars, not H4. The session family needs several bars inside London and
# Asia to produce anything at all, and a causality test over a strategy that
# never fires is a test that passes for the wrong reason.
N_BARS = 700

#: Strategies, excluding the reference baselines. The baselines are held to a
#: different contract on purpose -- CoinFlip is meant to be run with whatever
#: stop and target the candidate uses, so it does not validate its own.
CANDIDATES = sorted(n for n in R.available() if not n.startswith("baseline_"))


@pytest.fixture(scope="module")
def universe():
    # A dollar factor strong enough to give the majors realistic pairwise
    # correlations (0.6-0.8, which is where EUR/USD and GBP/USD actually live).
    # Below that the spread strategy never sees a co-moving pair and its tests
    # would pass having generated nothing.
    u = generate_universe(n_bars=N_BARS, bars_per_day=24, seed=424242,
                          dollar_factor_strength=0.7)
    return {k: v for k, v in u.items() if k in INSTRUMENTS}


def _signal_fingerprint(sig):
    """Comparable form of a signal, tolerant of float noise but nothing else."""
    if sig is None:
        return None
    return (sig.side, round(float(sig.stop_price), 12),
            round(float(sig.target_price), 12) if sig.target_price is not None else None,
            round(float(sig.strength), 12))


class TestLibraryShape:
    def test_every_declared_family_has_members(self):
        for family in ("trend", "mean_reversion", "breakout", "momentum", "carry",
                       "volatility", "session", "pattern"):
            assert R.available(family=family), f"{family} family is empty"

    def test_the_library_is_large_enough_to_need_trial_accounting(self):
        """Not a vanity metric. Past a handful of strategies the deflated-Sharpe
        bar has to come from the ledger rather than from a declared count, and
        this asserts the library is in fact in that regime."""
        assert len(CANDIDATES) >= 25

    def test_library_re_exports_match_the_registry(self):
        from sentinel.strategy import library

        exported = {getattr(library, n).meta.name for n in library.__all__}
        assert exported == set(CANDIDATES)


@pytest.mark.parametrize("name", CANDIDATES)
class TestEveryStrategy:
    def test_declares_an_honest_meta(self, name):
        m = R.get(name).meta
        assert m.family in R.families() and m.family != "unclassified"
        # A hypothesis has to say why something might work. The length floor is
        # a crude proxy, but it does rule out "buys when line goes up".
        assert len(m.hypothesis) >= 80, f"{name} has no real hypothesis"
        assert len(m.failure_conditions) >= 2, f"{name} cannot be falsified"
        assert all(len(f) > 20 for f in m.failure_conditions)
        assert m.horizon_bars > 0 and m.required_history >= 10
        assert m.timeframe

    def test_ships_as_a_hypothesis_not_as_accepted(self, name):
        """Nothing ships accepted. Only scripts/run_acceptance.py promotes, and
        it promotes an allocation against a stored verdict -- never a class."""
        assert R.get(name).meta.lifecycle in R.DECLARABLE_LIFECYCLES
        assert R.get(name).meta.lifecycle != "accepted"

    def test_builds_with_its_defaults(self, name):
        s = R.build(name)
        assert s.params == {**R.get(name).default_params(), **s.params}
        assert s.warmup() >= 10

    def test_rejects_a_target_that_cannot_clear_cost(self, name):
        """With a target at half the stop, the break-even win rate is above 60%
        before costs. Every strategy that sizes its exits in ATR must refuse."""
        defaults = R.get(name).default_params()
        if not {"stop_atr", "target_atr"} <= set(defaults):
            pytest.skip("this strategy's target is structural, not an ATR multiple")
        with pytest.raises(ValueError):
            R.build(name, target_atr=defaults["stop_atr"] * 0.5)

    def test_generate_is_causal(self, name, universe):
        """Truncating the future must not change the signal at bar i.

        This is the strategy-level version of the indicator assertion, and it
        catches the class of bug the indicator tests cannot: a strategy reading
        ``df.iloc[index + 1]``, comparing against an unshifted level, or
        selecting a partner instrument on full-sample statistics.
        """
        full = R.build(name)
        full.prepare(universe)
        checks = 0
        for index in range(N_BARS - 250, N_BARS - 10, 40):
            truncated_data = {s: df.iloc[: index + 1] for s, df in universe.items()}
            truncated = R.build(name)
            truncated.prepare(truncated_data)
            for sym in universe:
                a = _signal_fingerprint(full.generate(universe, sym, index))
                b = _signal_fingerprint(truncated.generate(truncated_data, sym, index))
                assert a == b, (f"{name} on {sym}@{index} changed when the future was "
                                f"removed: {a} vs {b}")
                checks += 1
        assert checks > 0

    def test_every_signal_carries_a_workable_stop(self, name, universe):
        """A signal with no stop, or a stop on the wrong side, is unusable.

        Checked by building the OrderIntent the backtester would build. That is
        the real contract: ``OrderIntent`` enforces stop-below-target for a buy
        and the reverse for a sell, and the risk engine sizes off the distance,
        so a zero-width stop is an unbounded position.
        """
        strat = R.build(name)
        strat.prepare(universe)
        seen = 0
        for sym, df in universe.items():
            inst = INSTRUMENTS[sym]
            for index in range(strat.warmup(), len(df) - 1):
                sig = strat.generate(universe, sym, index)
                if sig is None:
                    continue
                seen += 1
                assert sig.side in (Side.BUY, Side.SELL)
                assert sig.stop_price is not None, f"{name} produced a signal with no stop"
                assert sig.target_price is not None
                close = float(df["close"].iloc[index])
                stop, target = float(sig.stop_price), float(sig.target_price)
                if sig.side is Side.BUY:
                    assert stop < close < target
                else:
                    assert target < close < stop
                assert 0.0 <= sig.strength <= 1.0
                assert sig.calibrated is False, "a raw strength is not a probability"
                # The intent constructor is the gate the signal must pass.
                OrderIntent(
                    client_order_id=f"T-{name}-{index}", strategy=name, instrument=sym,
                    side=sig.side, lots=D("0.01"),
                    stop_loss=inst.round_price(sig.stop_price),
                    take_profit=inst.round_price(sig.target_price))
        # No exemptions. A strategy that cannot produce a single signal on 700
        # hourly bars of five correlated majors has an unreachable condition in
        # it, and silence is the hardest failure mode to notice.
        assert seen, f"{name} produced no signal at all on this universe"


class TestSignalHelpers:
    def test_a_zero_atr_produces_no_signal(self):
        """A flat or forward-filled feed gives ATR 0. Without this guard the
        stop lands exactly on the entry and sizing divides by zero risk."""
        assert atr_signal(strategy="x", instrument="EUR_USD", side=Side.BUY,
                          close=1.08, atr_value=0.0, stop_atr=2.0, target_atr=4.0,
                          horizon_bars=10, timeframe="H1", strength=0.5) is None

    def test_a_stop_on_the_wrong_side_is_refused(self):
        assert level_signal(strategy="x", instrument="EUR_USD", side=Side.BUY,
                            close=1.08, stop_price=1.09, target_price=1.10,
                            horizon_bars=10, timeframe="H1", strength=0.5) is None

    def test_a_bounded_target_that_cannot_pay_for_the_stop_is_refused(self):
        """The mean-reversion trap: a fade to a mean two pips away behind a
        forty-pip stop. High win rate, guaranteed loss after cost."""
        assert level_signal(strategy="x", instrument="EUR_USD", side=Side.BUY,
                            close=1.08, stop_price=1.0760, target_price=1.0802,
                            horizon_bars=10, timeframe="H1", strength=0.5,
                            min_reward_risk=1.2) is None

    def test_an_adequate_reward_risk_is_accepted(self):
        sig = level_signal(strategy="x", instrument="EUR_USD", side=Side.SELL,
                           close=1.08, stop_price=1.0820, target_price=1.0740,
                           horizon_bars=10, timeframe="H1", strength=0.5,
                           min_reward_risk=1.2)
        assert sig is not None and sig.side is Side.SELL
