"""The ensemble layer.

Two things are being asserted. First that combining strategies behaves as a
combination should -- disagreement produces silence, agreement produces a
signal with conservative levels. Second, and more important, that the ensemble
is not a way around anything: it emits ordinary Signals, they go through the
risk engine on the ordinary path, and the vetoes still fire.
"""

from decimal import Decimal as D

import pytest

from sentinel.core.config import RiskConfig
from sentinel.core.money import Instrument
from sentinel.core.types import OrderIntent, Side, Signal
from sentinel.data.synthetic import generate_universe
from sentinel.research.backtest import BacktestConfig, run_backtest
from sentinel.strategy.base import Strategy, StrategyMeta
from sentinel.strategy.ensemble import Ensemble
from sentinel.strategy.registry import build

INSTRUMENTS = {
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
    "AUD_USD": Instrument("AUD_USD", "AUD", "USD"),
}
CONVERSIONS = {"USD": D("1")}


class _Fixed(Strategy):
    """A member with a scripted opinion, so the combination logic is testable
    without depending on what a real strategy happens to do on a given bar."""

    def __init__(self, name, side, stop_atr=2.0, target_atr=4.0, strength=0.6):
        self.meta = StrategyMeta(name=name, family="trend", timeframe="H4",
                                 horizon_bars=20, required_history=30,
                                 lifecycle="hypothesis", hypothesis="test double")
        self._side, self._stop, self._target, self._strength = (
            side, stop_atr, target_atr, strength)
        super().__init__()

    def generate(self, data, instrument, index):
        if self._side is None or index < 30:
            return None
        close = float(data[instrument]["close"].iloc[index])
        unit = close * 0.002
        sign = self._side.sign
        return Signal(strategy=self.meta.name, instrument=instrument, side=self._side,
                      strength=self._strength,
                      stop_price=close - sign * self._stop * unit,
                      target_price=close + sign * self._target * unit,
                      horizon_bars=20, timeframe="H4")


@pytest.fixture(scope="module")
def universe():
    u = generate_universe(n_bars=700, bars_per_day=6, seed=7171,
                          dollar_factor_strength=0.7)
    return {k: v for k, v in u.items() if k in INSTRUMENTS}


class TestConstruction:
    def test_it_needs_members(self):
        with pytest.raises(ValueError, match="at least one member"):
            Ensemble([])

    def test_duplicate_members_are_refused(self):
        with pytest.raises(ValueError, match="duplicate"):
            Ensemble([build("donchian_trend"), build("donchian_trend")])

    def test_a_baseline_cannot_be_a_member(self):
        """The benchmark cannot be inside the thing being benchmarked."""
        with pytest.raises(ValueError, match="reference baseline"):
            Ensemble([build("donchian_trend"), build("baseline_coin_flip")])

    def test_it_ships_as_a_hypothesis(self):
        e = Ensemble([build("donchian_trend"), build("vol_reversion")])
        assert e.meta.lifecycle == "hypothesis"

    def test_the_member_list_is_part_of_its_identity(self):
        """Two ensembles over different members are different trials. If the
        member list were not in `params`, every combination anyone tried would
        collapse into one ledger entry and the search would be invisible."""
        a = Ensemble([build("donchian_trend"), build("vol_reversion")])
        b = Ensemble([build("donchian_trend"), build("carry_tilt")])
        assert a.params["members"] != b.params["members"]
        assert a.meta.name != b.meta.name

    def test_weights_are_normalised_and_must_be_positive(self):
        e = Ensemble([build("donchian_trend"), build("vol_reversion")],
                     weights={"donchian_trend": 3.0, "vol_reversion": 1.0})
        assert sum(e.weights.values()) == pytest.approx(1.0)
        assert e.weights["donchian_trend"] == pytest.approx(0.75)
        with pytest.raises(ValueError, match="negative"):
            Ensemble([build("donchian_trend")], weights={"donchian_trend": -1.0})

    def test_an_unreachable_vote_threshold_is_refused(self):
        """The net vote is normalised to [-1, 1]; a threshold above 1 would
        silently produce a strategy that can never fire."""
        with pytest.raises(ValueError, match="min_net_vote"):
            Ensemble([build("donchian_trend")], min_net_vote=1.5)

    def test_min_agreement_cannot_exceed_the_member_count(self):
        with pytest.raises(ValueError, match="min_agreement"):
            Ensemble([build("donchian_trend")], min_agreement=3)


class TestCombination:
    def _prepared(self, members, universe, **params):
        e = Ensemble(members, **params)
        e.prepare(universe)
        return e

    def test_members_that_cancel_produce_nothing(self, universe):
        """Silence on disagreement is the correct output. An ensemble that
        always trades something has hidden the disagreement, not resolved it."""
        e = self._prepared([_Fixed("a", Side.BUY), _Fixed("b", Side.SELL)], universe)
        assert e.generate(universe, "EUR_USD", 400) is None

    def test_members_that_agree_produce_a_signal_with_a_stop(self, universe):
        e = self._prepared([_Fixed("a", Side.BUY), _Fixed("b", Side.BUY)], universe)
        sig = e.generate(universe, "EUR_USD", 400)
        assert sig is not None and sig.side is Side.BUY
        close = float(universe["EUR_USD"]["close"].iloc[400])
        assert float(sig.stop_price) < close < float(sig.target_price)

    def test_the_combined_stop_is_the_widest_and_the_target_the_nearest(self, universe):
        """Conservative on both ends: never stopped out of a position a member
        would still hold, never held for a target a member has abandoned. The
        cost is a worse reward/risk, which `level_signal` then polices."""
        wide = _Fixed("wide", Side.BUY, stop_atr=3.0, target_atr=9.0)
        tight = _Fixed("tight", Side.BUY, stop_atr=1.0, target_atr=5.0)
        e = self._prepared([wide, tight], universe)
        sig = e.generate(universe, "EUR_USD", 400)
        close = float(universe["EUR_USD"]["close"].iloc[400])
        unit = close * 0.002
        assert float(sig.stop_price) == pytest.approx(close - 3.0 * unit)
        assert float(sig.target_price) == pytest.approx(close + 5.0 * unit)

    def test_a_combination_whose_reward_cannot_pay_for_the_risk_is_dropped(self, universe):
        """Widest stop plus nearest target can push reward/risk under the cost
        barrier. Emitting it anyway would be the ensemble quietly making its
        members worse."""
        wide = _Fixed("wide", Side.BUY, stop_atr=5.0, target_atr=9.0)
        near = _Fixed("near", Side.BUY, stop_atr=1.0, target_atr=2.0)
        e = self._prepared([wide, near], universe, min_reward_risk=1.2)
        assert e.generate(universe, "EUR_USD", 400) is None

    def test_min_agreement_requires_that_many_members_on_the_same_bar(self, universe):
        members = [_Fixed("a", Side.BUY), _Fixed("b", None), _Fixed("c", None)]
        lenient = self._prepared(members, universe, min_agreement=1)
        strict = Ensemble([_Fixed("a", Side.BUY), _Fixed("b", None), _Fixed("c", None)],
                          min_agreement=2)
        strict.prepare(universe)
        assert lenient.generate(universe, "EUR_USD", 400) is not None
        assert strict.generate(universe, "EUR_USD", 400) is None

    def test_equal_risk_weighting_discounts_a_wider_stop(self, universe):
        """A member with a 4xATR stop and one with a 1xATR stop are not making
        comparable statements; equal-weighting their strengths would hand the
        wider-stopped member more influence for the same nominal confidence."""
        wide = Ensemble([_Fixed("w", Side.BUY, stop_atr=4.0, target_atr=9.0)],
                        weighting="equal_risk", min_net_vote=0.01)
        tight = Ensemble([_Fixed("t", Side.BUY, stop_atr=1.0, target_atr=3.0)],
                         weighting="equal_risk", min_net_vote=0.01)
        for e in (wide, tight):
            e.prepare(universe)
        w = wide.generate(universe, "EUR_USD", 400)
        t = tight.generate(universe, "EUR_USD", 400)
        assert t.features["net_vote"] > w.features["net_vote"]

    def test_a_broken_member_does_not_silence_the_rest(self, universe):
        class _Broken(_Fixed):
            def generate(self, data, instrument, index):
                raise RuntimeError("member is broken")

        e = self._prepared([_Broken("bad", Side.BUY), _Fixed("good", Side.BUY)],
                           universe, min_agreement=1)
        assert e.generate(universe, "EUR_USD", 400) is not None

    def test_correlated_instruments_firing_together_are_damped(self, universe):
        """The ensemble reports the cluster and lowers its own confidence. It
        does NOT net exposure -- that lives in risk/exposure.py, and a limit
        implemented in two places is a limit that is wrong in one of them."""
        e = self._prepared([_Fixed("a", Side.BUY)], universe,
                           correlation_threshold=0.2, min_net_vote=0.01)
        sig = e.generate(universe, "EUR_USD", 400)
        assert sig is not None
        assert sig.features.get("correlation_cluster_size", 1.0) > 1.0
        solo = self._prepared([_Fixed("a", Side.BUY)], universe,
                              correlation_threshold=0.999, min_net_vote=0.01)
        assert solo.generate(universe, "EUR_USD", 400).strength > sig.strength


class TestItGoesThroughTheRiskEngine:
    def test_an_ensemble_backtest_is_risk_checked_like_any_strategy(self, universe):
        """The safety property, asserted end to end: the ensemble's intents are
        evaluated by RiskEngine.evaluate_entry, and its vetoes are recorded in
        the same place a single strategy's would be."""
        e = Ensemble([build("donchian_trend"), build("ma_cross_atr"),
                      build("supertrend_flip")])
        res = run_backtest(e, universe, INSTRUMENTS, RiskConfig(),
                           BacktestConfig(label="ensemble", periods_per_year=1512,
                                          data_label="synthetic"),
                           conversions=CONVERSIONS)
        assert res.signals_generated > 0
        assert res.diagnostics["generation_error_count"] == 0
        # Some entry must have been refused: the risk engine is in the path,
        # not bypassed. (Position, exposure and sizing limits all apply.)
        assert sum(res.vetoes.values()) > 0
        assert res.orders_submitted <= res.signals_generated

    def test_every_ensemble_signal_builds_a_valid_order_intent(self, universe):
        e = Ensemble([build("donchian_trend"), build("vol_reversion")])
        e.prepare(universe)
        seen = 0
        for sym in universe:
            for i in range(e.warmup(), 690, 7):
                sig = e.generate(universe, sym, i)
                if sig is None:
                    continue
                seen += 1
                OrderIntent(client_order_id=f"E{sym}{i}", strategy=e.meta.name,
                            instrument=sym, side=sig.side, lots=D("0.01"),
                            stop_loss=INSTRUMENTS[sym].round_price(sig.stop_price),
                            take_profit=INSTRUMENTS[sym].round_price(sig.target_price))
        assert seen > 0

    def test_it_is_not_in_the_registry(self):
        """An ensemble is one specific combination that has to earn its own
        verdict. A registry entry buildable with no members would invite
        treating 'the ensemble' as a thing with a track record."""
        from sentinel.strategy import registry as R

        assert "ensemble" not in R.available()
