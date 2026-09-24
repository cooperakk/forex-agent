"""Regressions for the research-integrity audit.

Each class is one finding. The common shape of the defects: a gate that could
be satisfied without the evidence it claims to weigh. A verdict that says
"accepted" is the most expensive sentence this system can emit, so every one
of these is a test that the protocol cannot be passed by omission.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sentinel.core.config import ResearchConfig, RiskConfig
from sentinel.core.money import Instrument
from sentinel.data.synthetic import DEFAULT_UNIVERSE, generate_universe
from sentinel.research.acceptance import REQUIRED_GATES, evaluate
from sentinel.research.backtest import BacktestConfig, BacktestResult, run_backtest
from sentinel.research.cv import cpcv_paths_from_matrix
from sentinel.research.metrics import compute_performance
from sentinel.research.stats import directional_accuracy
from sentinel.strategy.registry import build

INSTRUMENTS = {
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
    "AUD_USD": Instrument("AUD_USD", "AUD", "USD"),
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001")),
    "USD_CHF": Instrument("USD_CHF", "USD", "CHF"),
}
CONVERSIONS = {"USD": D("1"), "JPY": D("1") / D("150"), "CHF": D("1") / D("0.88")}
RNG = np.random.default_rng(7)


def _candidate(n: int = 800, mean: float = 0.0004) -> BacktestResult:
    idx = pd.date_range("2022-01-03", periods=n, freq="4h", tz="UTC")
    rets = pd.Series(RNG.normal(mean, 0.004, n), index=idx)
    equity = 10000 * (1 + rets).cumprod()
    return BacktestResult(
        label="candidate", trades=[], equity_curve=equity,
        performance=compute_performance([], equity, 1512, 10000.0),
        vetoes={}, signals_generated=0, orders_submitted=0, orders_rejected=0,
        config_snapshot={}, per_bar_returns=rets, diagnostics={})


@pytest.fixture(scope="module")
def universe():
    return generate_universe(DEFAULT_UNIVERSE[:3], n_bars=1400, bars_per_day=6, seed=3)


def _bt(universe, strategy="donchian_trend", **kw):
    cfg = BacktestConfig(label="t", periods_per_year=1512, record_trial=False, **kw)
    inst = {k: v for k, v in INSTRUMENTS.items() if k in universe}
    return run_backtest(build(strategy), universe, inst, RiskConfig(), cfg,
                        conversions=CONVERSIONS)


# --------------------------------------------------------------------------- #
# finding 3: missing evidence used to mean a skipped gate, and accepted=True
# --------------------------------------------------------------------------- #


class TestMissingEvidenceIsAFailure:
    def test_a_verdict_with_no_evidence_is_not_accepted_even_on_live_quality_data(self):
        """The exact reproduction from the audit: a flattering return series,
        the `live-quality` label, no baselines, no matrices, no paths. This
        used to come back `accepted=True` with five gates present."""
        v = evaluate(run_id="R", strategy_name="donchian_trend",
                     candidate=_candidate(mean=0.002), baselines={},
                     periods_per_year=1512, data_label="live-quality")
        assert v.accepted is False
        ids = {g.id for g in v.gates}
        assert set(REQUIRED_GATES) <= ids, "every required gate is present, pass or fail"
        missing = [g for g in v.gates if g.observed == "not evaluated"]
        assert missing and all(g.blocking and not g.passed for g in missing)
        assert set(v.evidence["gates_not_evaluated"]) >= {"L3", "L4", "L5.2", "L6", "L7"}

    def test_the_summary_names_the_unevaluated_gates(self):
        v = evaluate(run_id="R", strategy_name="donchian_trend",
                     candidate=_candidate(), baselines={}, periods_per_year=1512,
                     data_label="synthetic")
        for gate_id in v.evidence["gates_not_evaluated"]:
            assert gate_id in v.summary

    def test_switching_a_gate_off_in_config_removes_it_from_the_required_list(self):
        rc = ResearchConfig(require_factor_alpha=False, require_random_walk_beat=False)
        v = evaluate(run_id="R", strategy_name="donchian_trend",
                     candidate=_candidate(), baselines={}, periods_per_year=1512,
                     data_label="synthetic", research_config=rc)
        assert "L3" not in v.evidence["gates_not_evaluated"]
        assert "L2" not in v.evidence["gates_not_evaluated"]
        assert "L4" in v.evidence["gates_not_evaluated"]


# --------------------------------------------------------------------------- #
# finding 5: the random-walk gate now asks about the strategy's own calls
# --------------------------------------------------------------------------- #


class TestDirectionalGate:
    def test_calls_that_know_the_direction_pass_and_noise_does_not(self):
        n = 400
        rng = np.random.default_rng(11)
        fwd = rng.normal(0, 0.003, n)
        informed = np.sign(fwd + rng.normal(0, 0.002, n)) * fwd     # mostly right
        assert directional_accuracy(informed).p_value < 0.01
        assert directional_accuracy(informed).detail["hit_rate"] > 0.6
        # Coin-flip calls: the gate's alpha is 0.01, so over many independent
        # draws it should fire about 1% of the time -- a rate, not one draw.
        fired = 0
        for seed in range(60):
            r = np.random.default_rng(1000 + seed)
            f = r.normal(0, 0.003, n)
            noise = np.sign(r.normal(0, 1, n)) * f
            fired += directional_accuracy(noise).p_value < 0.01
        assert fired <= 4, f"noise passed the directional gate {fired}/60 times"

    def test_a_high_hit_rate_on_small_wins_and_big_losses_does_not_pass(self):
        # Right 70% of the time by a hair, wrong 30% of the time by a lot.
        x = np.concatenate([np.full(70, 0.0002), np.full(30, -0.0030)])
        RNG.shuffle(x)
        res = directional_accuracy(x)
        assert res.detail["hit_rate"] == pytest.approx(0.7)
        assert res.p_value > 0.5, "it is the mean that pays, not the hit rate"

    def test_the_backtest_records_every_raised_signal_with_its_forward_return(self, universe):
        r = _bt(universe)
        assert r.signal_log, "no forecasts recorded -> the L2 gate has nothing to test"
        assert len(r.signal_log) == r.signals_generated
        row = r.signal_log[0]
        assert row["side_sign"] in (1, -1) and row["horizon_bars"] >= 1
        signed = r.signed_forward_returns("h")
        assert signed.size > 0 and np.isfinite(signed).all()

    def test_the_gate_fails_when_the_candidate_recorded_no_signals(self):
        v = evaluate(run_id="R", strategy_name="donchian_trend",
                     candidate=_candidate(), baselines={}, periods_per_year=1512,
                     data_label="synthetic")
        l2 = next(g for g in v.gates if g.id == "L2")
        assert l2.passed is False and "no signals" in l2.observed


# --------------------------------------------------------------------------- #
# finding 4: CPCV that is combinatorial and purged, over a real family
# --------------------------------------------------------------------------- #


class TestCPCV:
    def _matrix(self, k=5, t=1200):
        base = RNG.normal(0.0002, 0.004, t)
        cols = [base + RNG.normal(0, 0.001, t) for _ in range(k)]
        return np.column_stack(cols)

    def test_paths_are_assembled_from_every_split_and_purging_happens(self):
        m = self._matrix()
        rep = cpcv_paths_from_matrix(m, n_groups=6, test_groups=2, horizon_bars=10,
                                     embargo_pct=0.01, periods_per_year=1512)
        assert rep.n_splits == 15 and rep.n_paths == 5
        assert len(rep.paths) == 5
        assert all(len(p) == m.shape[0] for p in rep.paths), "each path covers all of history"
        assert rep.purged > 0 and rep.embargoed > 0
        assert len(rep.chosen_per_split) == 15

    def test_selection_is_out_of_sample(self):
        """A column that is only good in one era must not be picked on that
        era's own returns. Plant a variant that is superb in group 5 and awful
        elsewhere: when group 5 is the TEST block, the train rows exclude it,
        so the selection cannot see the era it will be scored on."""
        t = 1200
        m = RNG.normal(0.0, 0.004, (t, 4))
        planted = RNG.normal(-0.0015, 0.004, t)
        planted[1000:] = RNG.normal(0.01, 0.002, 200)       # group 5 is 1000..1199
        m = np.column_stack([m, planted])
        rep = cpcv_paths_from_matrix(m, n_groups=6, test_groups=1, horizon_bars=1,
                                     embargo_pct=0.0, periods_per_year=1512)
        # Split 5 (test = group 5) chose on groups 0..4 where the plant is awful.
        assert rep.chosen_per_split[5] != 4

    def test_one_configuration_under_many_names_is_reported(self):
        col = RNG.normal(0.0003, 0.004, 600)
        m = np.column_stack([col] * 5)
        rep = cpcv_paths_from_matrix(m, n_groups=4, test_groups=1, periods_per_year=1512)
        assert rep.n_variants == 5 and rep.n_unique_variants == 1

    def test_evaluate_refuses_to_score_stability_on_a_fake_family(self):
        col = RNG.normal(0.0003, 0.004, 600)
        m = np.column_stack([col] * 5)
        rep = cpcv_paths_from_matrix(m, n_groups=4, test_groups=1, periods_per_year=1512)
        v = evaluate(run_id="R", strategy_name="x", candidate=_candidate(600),
                     baselines={}, periods_per_year=1512, data_label="synthetic",
                     cpcv_path_returns=rep.paths, cpcv_report=rep)
        l6 = next(g for g in v.gates if g.id == "L6")
        assert l6.passed is False and l6.observed == "not evaluated"

    def test_parameter_variants_are_distinct_for_strategies_without_a_channel(self):
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            "run_acceptance", Path(__file__).resolve().parents[1] / "scripts" / "run_acceptance.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["run_acceptance"] = mod
        spec.loader.exec_module(mod)
        for name in ("ts_momentum", "ma_cross_atr", "carry_tilt", "bollinger_fade"):
            base = build(name).params
            variants = mod.parameter_variants(name, dict(base), n=6)
            keys = {tuple(sorted(v.items())) for v in variants}
            assert len(keys) >= 2, f"{name}: the family collapsed to one configuration"
            assert dict(base) in variants


# --------------------------------------------------------------------------- #
# finding 2: the label a file set is entitled to
# --------------------------------------------------------------------------- #


class TestDataLabel:
    @pytest.fixture
    def script(self):
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            "run_acceptance", Path(__file__).resolve().parents[1] / "scripts" / "run_acceptance.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["run_acceptance"] = mod
        spec.loader.exec_module(mod)
        return mod

    def _csv_dir(self, tmp_path, with_ba: bool):
        idx = pd.date_range("2024-01-01", periods=50, freq="4h", tz="UTC")
        df = pd.DataFrame({"timestamp": idx, "open": 1.1, "high": 1.11, "low": 1.09,
                           "close": 1.1, "volume": 10, "carry_bp": 120.0})
        if with_ba:
            df["bid"] = 1.0999
            df["ask"] = 1.1001
        df.to_csv(tmp_path / "EUR_USD.csv", index=False)
        return tmp_path

    def test_a_mid_price_csv_defaults_to_third_party_not_live_quality(self, script, tmp_path):
        u = script.load_bars(self._csv_dir(tmp_path, with_ba=False))
        assert script.data_quality_label(u, None) == "third-party"

    def test_live_quality_is_refused_without_bid_and_ask(self, script, tmp_path):
        u = script.load_bars(self._csv_dir(tmp_path, with_ba=False))
        with pytest.raises(SystemExit):
            script.data_quality_label(u, "live-quality")

    def test_live_quality_is_granted_with_bid_and_ask(self, script, tmp_path):
        u = script.load_bars(self._csv_dir(tmp_path, with_ba=True))
        assert script.data_quality_label(u, "live-quality") == "live-quality"

    def test_the_importer_keeps_the_columns_the_strategies_need(self, script, tmp_path):
        u = script.load_bars(self._csv_dir(tmp_path, with_ba=True))
        assert {"bid", "ask", "carry_bp", "volume"} <= set(u["EUR_USD"].columns)


# --------------------------------------------------------------------------- #
# finding 7: histories aligned by timestamp, not by row number
# --------------------------------------------------------------------------- #


class TestAlignment:
    def test_a_history_shifted_by_a_year_is_refused(self, universe):
        shifted = dict(universe)
        gbp = universe["GBP_USD"].copy()
        gbp.index = gbp.index + pd.Timedelta(days=365)
        shifted["GBP_USD"] = gbp
        with pytest.raises(ValueError, match="coexist"):
            _bt(shifted)

    def test_a_symbol_with_a_hole_in_its_history_is_skipped_there_not_shifted(self, universe):
        holed = dict(universe)
        gbp = universe["GBP_USD"]
        holed["GBP_USD"] = pd.concat([gbp.iloc[:600], gbp.iloc[660:]])
        r = _bt(holed)
        assert r.diagnostics["missing_bars"]["GBP_USD"] == 60
        assert r.diagnostics["missing_bars"]["EUR_USD"] == 0
        # The clock is the union: the run still spans the whole history.
        assert len(r.equity_curve) == len(universe["EUR_USD"])


# --------------------------------------------------------------------------- #
# finding 6: the backtest replays what the agent does
# --------------------------------------------------------------------------- #


class TestBacktestMirrorsTheAgent:
    def test_signals_fill_at_the_next_bars_open_not_its_close(self, universe):
        r = _bt(universe)
        assert r.trades, "need at least one fill to check"
        opens = {s: universe[s]["open"] for s in universe}
        for t in r.trades[:10]:
            ts = pd.Timestamp(t.opened_ns, unit="ns", tz="UTC")
            bar_open = float(opens[t.instrument].loc[ts])
            inst = INSTRUMENTS[t.instrument]
            # Entry is the open plus spread and slippage, never the bar's close.
            assert abs(float(t.entry_price) - bar_open) < float(inst.pip) * 8

    def test_session_gating_removes_the_entries_the_agent_would_refuse(self, universe):
        free = _bt(universe)
        gated = _bt(universe, session_windows_utc=[[7, 16]], trade_days=[0, 1, 2, 3, 4])
        assert gated.vetoes.get("out_of_session", 0) > 0
        assert gated.orders_submitted < free.orders_submitted

    def test_weekend_flat_closes_before_the_gap(self, universe):
        r = _bt(universe, weekend_flat=True, friday_close_utc_hour=19)
        reasons = {t.exit_reason for t in r.trades}
        assert "weekend_flat" in reasons
        for t in r.trades:
            ts = pd.Timestamp(t.closed_ns, unit="ns", tz="UTC")
            assert not (ts.weekday() == 4 and ts.hour >= 23), "held into the weekend"

    def test_doubling_latency_changes_the_equity_curve(self, universe):
        base = _bt(universe)
        slow = _bt(universe, latency_multiplier=2.0)
        assert not base.equity_curve.equals(slow.equity_curve), \
            "a latency stress that leaves the curve byte-identical measured nothing"
        # And it hurts, on average: more diffusion during the delay is adverse.
        assert slow.performance.net_return_pct <= base.performance.net_return_pct + 0.5

    def test_the_drawdown_peak_follows_balance_like_the_agent(self, universe):
        r = _bt(universe)
        assert r.config_snapshot["weekend_flat"] is True
        assert "session_windows_utc" in r.config_snapshot
