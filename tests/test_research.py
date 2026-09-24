"""The statistics have to be right, or every verdict built on them is noise."""

from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from sentinel.research.cv import CombinatorialPurgedCV, PurgedKFold, walk_forward
from sentinel.research.factors import attribute, carry_factor, dollar_factor
from sentinel.research.labeling import (
    BarrierConfig, average_uniqueness, effective_sample_size, realised_volatility,
    triple_barrier_labels,
)
from sentinel.research.stats import (
    clark_west, deflated_sharpe_ratio, expected_max_sharpe, hansen_spa,
    min_track_record_length, posterior_given_significant,
    probability_of_backtest_overfitting, probabilistic_sharpe_ratio, sharpe_ratio,
    required_alpha_for_posterior, whites_reality_check,
)

RNG = np.random.default_rng(20260914)


@pytest.fixture(scope="module")
def series():
    n = 1500
    px = pd.Series(1.08 * np.exp(np.cumsum(RNG.normal(0, 0.0008, n))))
    hi = px * (1 + np.abs(RNG.normal(0, 0.0004, n)))
    lo = px * (1 - np.abs(RNG.normal(0, 0.0004, n)))
    return px, hi, lo


class TestLabeling:
    def test_random_walk_hits_the_near_barrier_about_twice_as_often(self, series):
        px, hi, lo = series
        sigma = realised_volatility(px, 50)
        events = list(range(60, len(px) - 40, 7))
        lab = triple_barrier_labels(px, events, sigma,
                                    BarrierConfig(profit_mult=2.0, stop_mult=1.0,
                                                  max_hold_bars=24), high=hi, low=lo)
        counts = lab["touched"].value_counts()
        # A 2-sigma target against a 1-sigma stop: the stop is roughly twice as
        # likely on a driftless walk. If this inverts, the path logic is wrong.
        assert counts["stop"] > counts["target"]
        assert 1.4 < counts["stop"] / counts["target"] < 3.2

    def test_an_ambiguous_bar_is_scored_as_the_stop(self):
        px = pd.Series([1.0, 1.0, 1.0, 1.0])
        hi = pd.Series([1.0, 2.0, 1.0, 1.0])     # touches both barriers on bar 1
        lo = pd.Series([1.0, 0.5, 1.0, 1.0])
        sigma = pd.Series([0.01] * 4)
        lab = triple_barrier_labels(px, [0], sigma,
                                    BarrierConfig(max_hold_bars=3), high=hi, low=lo)
        assert lab.iloc[0]["touched"] == "stop"

    def test_effective_sample_size_falls_when_labels_overlap(self):
        n = 400
        px = pd.Series(np.linspace(1.0, 1.1, n))
        sigma = pd.Series([0.02] * n)
        # Events every bar with a 30-bar horizon: heavy overlap.
        lab = triple_barrier_labels(px, list(range(50, 300)), sigma,
                                    BarrierConfig(max_hold_bars=30))
        eff = effective_sample_size(lab, n)
        assert eff < len(lab) * 0.5
        u = average_uniqueness(lab, n)
        assert (u <= 1.0).all() and (u > 0).all()


class TestCrossValidation:
    def test_purged_kfold_leaks_nothing(self):
        n = 1000
        idx = np.arange(0, n - 30, 5)
        t1 = pd.Series({int(i): int(i) + 20 for i in idx})
        for split in PurgedKFold(5, embargo_pct=0.01).split(idx, t1, n):
            lo, hi = split.test.min(), split.test.max()
            leaks = [i for i in split.train if i <= hi and t1[int(i)] >= lo]
            assert leaks == []

    def test_cpcv_leaks_nothing_per_block(self):
        n = 1000
        idx = np.arange(0, n - 30, 5)
        t1 = pd.Series({int(i): int(i) + 20 for i in idx})
        cv = CombinatorialPurgedCV(6, 2, embargo_pct=0.01)
        groups = np.array_split(np.sort(idx), 6)
        for split in cv.split(idx, t1, n):
            for g in split.test_groups:
                blk = groups[g]
                leaks = [i for i in split.train
                         if i <= blk.max() and t1[int(i)] >= blk.min()]
                assert leaks == []

    def test_cpcv_counts(self):
        cv = CombinatorialPurgedCV(6, 2)
        assert cv.n_splits == 15 and cv.n_paths == 5

    def test_cpcv_assembles_complete_paths(self):
        n = 600
        idx = np.arange(0, n - 20, 5)
        t1 = pd.Series({int(i): int(i) + 10 for i in idx})
        cv = CombinatorialPurgedCV(6, 2)
        results = [(s.test_groups, {g: {"group": g} for g in s.test_groups})
                   for s in cv.split(idx, t1, n)]
        paths = cv.assemble_paths(results)
        assert len(paths) == cv.n_paths
        for p in paths:
            assert sorted(r["group"] for r in p) == list(range(6))

    def test_walk_forward_never_trains_on_the_future(self):
        for s in walk_forward(1000, 400, 100):
            assert s.train.max() < s.test.min()


class TestSharpeFamily:
    def test_sharpe_of_a_constant_series_is_zero(self):
        assert sharpe_ratio([0.001] * 100) == 0.0

    def test_psr_rises_with_sample_size(self):
        short = RNG.normal(0.0006, 0.008, 60)
        long = RNG.normal(0.0006, 0.008, 900)
        assert probabilistic_sharpe_ratio(long) > probabilistic_sharpe_ratio(short)

    def test_expected_max_sharpe_grows_with_trials(self):
        a = expected_max_sharpe(10, 0.25)
        b = expected_max_sharpe(500, 0.25)
        assert b > a > 0

    def test_deflation_rejects_the_best_of_many_trials(self):
        r = RNG.normal(0.0006, 0.008, 750)
        one = deflated_sharpe_ratio(r, 1)
        many = deflated_sharpe_ratio(r, 500)
        assert many["dsr"] < one["dsr"]
        assert many["sr_star"] > one["sr_star"]

    def test_mintrl_is_none_when_sharpe_is_not_positive(self):
        assert min_track_record_length(RNG.normal(-0.001, 0.01, 300)) is None

    def test_mintrl_shrinks_as_sharpe_grows(self):
        weak = min_track_record_length(RNG.normal(0.0002, 0.01, 800))
        strong = min_track_record_length(RNG.normal(0.0020, 0.01, 800))
        assert weak is not None and strong is not None and strong < weak


class TestOverfitting:
    def test_pure_noise_produces_a_high_pbo(self):
        M = RNG.normal(0, 0.01, (600, 20))
        res = probability_of_backtest_overfitting(M, n_subsets=8)
        assert res.pbo > 0.4

    def test_a_real_edge_produces_a_low_pbo(self):
        M = RNG.normal(0, 0.01, (600, 20))
        M[:, 3] += 0.0013
        res = probability_of_backtest_overfitting(M, n_subsets=8)
        assert res.pbo < 0.35
        assert res.prob_oos_loss < 0.3

    def test_odd_subset_count_is_refused(self):
        with pytest.raises(ValueError, match="even"):
            probability_of_backtest_overfitting(RNG.normal(0, 1, (100, 4)), n_subsets=7)


class TestForecastTests:
    def test_clark_west_finds_a_real_signal(self):
        n = 900
        sig = RNG.normal(0, 0.004, n)
        y = np.zeros(n)
        for i in range(1, n):
            y[i] = 0.4 * sig[i - 1] + RNG.normal(0, 0.008)
        res = clark_west(y, np.zeros(n), 0.4 * np.roll(sig, 1))
        assert res.p_value < 0.01

    def test_clark_west_does_not_fire_on_noise(self):
        n = 900
        y = RNG.normal(0, 0.01, n)
        res = clark_west(y, np.zeros(n), RNG.normal(0, 0.002, n))
        assert res.p_value > 0.05

    def test_spa_controls_the_family(self):
        null = RNG.normal(0, 0.01, (500, 30))
        assert hansen_spa(null, n_boot=300).p_value > 0.1
        alt = RNG.normal(0, 0.01, (500, 30))
        alt[:, 7] += 0.002
        assert hansen_spa(alt, n_boot=300).p_value < 0.05

    def test_reality_check_agrees_on_a_clear_winner(self):
        alt = RNG.normal(0, 0.01, (500, 20))
        alt[:, 3] += 0.0025
        assert whites_reality_check(alt, n_boot=300).p_value < 0.05


class TestDecisionArithmetic:
    @pytest.mark.parametrize("alpha,expected", [
        (0.05, 0.236), (0.01, 0.607), (0.001, 0.939),
    ])
    def test_posterior_matches_the_brief(self, alpha, expected):
        got = posterior_given_significant(0.03, alpha, 0.5)
        assert abs(got - expected) < 0.005

    def test_required_alpha_inverts_cleanly(self):
        a = required_alpha_for_posterior(0.03, 0.5, 0.9)
        assert abs(posterior_given_significant(0.03, a, 0.5) - 0.9) < 1e-6


class TestFactorAttribution:
    @pytest.fixture(scope="class")
    def factors(self):
        T = 600
        names = ["EUR", "GBP", "AUD", "JPY", "CHF", "CAD"]
        dollar = RNG.normal(0, 0.005, T)
        rets = {n: 0.8 * dollar + RNG.normal(0, 0.004, T) for n in names}
        rates = {n: np.full(T, r)
                 for n, r in zip(names, [0.01, 0.02, 0.045, -0.001, 0.0, 0.03])}
        return {"dollar": dollar_factor(rets), "carry": carry_factor(rets, rates)}

    def test_repackaged_carry_shows_no_alpha(self, factors):
        strat = 1.4 * factors["carry"] + RNG.normal(0, 0.002, 600)
        res = attribute(strat, factors)
        assert not res.significant(0.01)
        beta = {e.name: e.beta for e in res.exposures}
        assert 1.2 < beta["carry"] < 1.6

    def test_genuine_alpha_survives_the_controls(self, factors):
        strat = 0.6 * factors["carry"] + 0.0009 + RNG.normal(0, 0.002, 600)
        res = attribute(strat, factors)
        assert res.significant(0.01)

    def test_negative_skew_is_flagged(self, factors):
        T = 600
        nasty = np.where(RNG.random(T) < 0.03, -0.05, 0.0015) + RNG.normal(0, 0.001, T)
        res = attribute(nasty, factors)
        assert res.residual_skew < -1
        assert any("skew" in n for n in res.notes)

    def test_no_factors_is_labelled_as_a_raw_mean_test(self):
        res = attribute(RNG.normal(0.001, 0.01, 300), {})
        assert any("NOT alpha" in n for n in res.notes)
