"""The trial ledger, and the gate it feeds.

The point of these tests is not that a database round-trips. It is that the
number the deflated-Sharpe gate uses goes UP when more searching happens, and
does not go up when nothing new was searched. Everything else in this file
exists to pin down which is which.
"""

import sqlite3
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from sentinel.research.trials import (
    LedgerUnreadable, TrialLedger, TrialSummary, default_ledger,
    effective_trial_count, record_trial, set_default_ledger, trial_key, window_key,
)


@pytest.fixture
def ledger(tmp_path):
    return TrialLedger(tmp_path / "trials.db")


@pytest.fixture
def no_default_ledger():
    """Leave the process-wide default exactly as it was found.

    The default is global state, and a test that installs one without putting
    it back would make every later backtest in the suite write into a temporary
    directory that no longer exists.
    """
    yield
    set_default_ledger(None)


BASE = dict(strategy="donchian_trend", family="trend", instruments=["EUR_USD", "GBP_USD"],
            timeframe="H4", data_window="synthetic|a|b|3000")


class TestTrialKey:
    def test_the_same_configuration_has_the_same_key(self):
        a = trial_key("s", {"channel": 55}, ["EUR_USD"], "H4", "w")
        b = trial_key("s", {"channel": 55}, ["EUR_USD"], "H4", "w")
        assert a == b

    def test_instrument_order_does_not_matter(self):
        a = trial_key("s", {}, ["EUR_USD", "GBP_USD"], "H4", "w")
        b = trial_key("s", {}, ["GBP_USD", "EUR_USD"], "H4", "w")
        assert a == b

    @pytest.mark.parametrize("field,value", [
        ("params", {"channel": 56}),
        ("instruments", ["USD_JPY"]),
        ("timeframe", "H1"),
        ("data_window", "other"),
    ])
    def test_every_component_changes_the_key(self, field, value):
        """Each of these is a genuinely different trial. The parameter one is
        the component that grows fastest and the one people forget."""
        base = dict(params={"channel": 55}, instruments=["EUR_USD"],
                    timeframe="H4", data_window="w")
        assert trial_key("s", **base) != trial_key("s", **{**base, field: value})

    def test_window_key_is_coarse_on_purpose(self):
        """A vendor revising two ticks must not reset the trial count to zero,
        so the window is described rather than hashed."""
        assert window_key("2022-01-01", "2023-01-01", 3000, "live-quality") == \
            window_key("2022-01-01", "2023-01-01", 3000, "live-quality")


class TestCounting:
    def test_rerunning_the_same_thing_does_not_double_count(self, ledger):
        for _ in range(5):
            ledger.record(**BASE, params={"channel": 55})
        assert ledger.count(family="trend") == 1
        assert ledger.list(strategy="donchian_trend")[0]["runs"] == 5

    def test_each_parameter_set_is_its_own_trial(self, ledger):
        for ch in (30, 40, 55, 70, 90):
            ledger.record(**BASE, params={"channel": ch})
        assert ledger.count(strategy="donchian_trend") == 5

    def test_a_sibling_strategy_raises_the_family_count(self, ledger):
        """The property the whole module exists for.

        Searching a second trend system is a sixth draw from the trend idea,
        and the first system's bar has to rise to reflect that -- otherwise a
        growing library raises the chance of a flattering winner while leaving
        the bar exactly where it was.
        """
        ledger.record(**BASE, params={"channel": 55})
        before = ledger.summary("donchian_trend", "trend").charge
        ledger.record(strategy="supertrend_flip", family="trend", params={},
                      instruments=["EUR_USD"], timeframe="H4", data_window="w")
        after = ledger.summary("donchian_trend", "trend").charge
        assert after == before + 1

    def test_a_different_family_does_not_raise_the_charge(self, ledger):
        """Charging a trend system for a carry search would be as dishonest in
        the other direction; the correction has to track the actual selection."""
        ledger.record(**BASE, params={"channel": 55})
        before = ledger.summary("donchian_trend", "trend").charge
        ledger.record(strategy="carry_tilt", family="carry", params={},
                      instruments=["EUR_USD"], timeframe="D1", data_window="w")
        assert ledger.summary("donchian_trend", "trend").charge == before

    def test_validation_runs_are_recorded_but_not_counted(self, ledger):
        """A CPCV fold or a stress pass re-runs a configuration already
        counted. Counting them would penalise thorough validation."""
        ledger.record(**BASE, params={"channel": 55})
        for i in range(6):
            ledger.record(**{**BASE, "data_window": f"cpcv{i}"},
                          params={"channel": 55}, kind="validation")
        assert ledger.count(family="trend") == 1
        assert ledger.count(family="trend", kind=None) == 7

    def test_a_validation_pass_cannot_downgrade_a_search(self, ledger):
        ledger.record(**BASE, params={"channel": 55}, kind="search")
        ledger.record(**BASE, params={"channel": 55}, kind="validation")
        assert ledger.count(family="trend") == 1

    def test_an_unknown_kind_is_refused(self, ledger):
        with pytest.raises(ValueError, match="kind"):
            ledger.record(**BASE, params={}, kind="whatever")

    def test_sessions_are_counted_separately_from_trials(self, ledger):
        ledger.record(**BASE, params={"channel": 30}, session_id="s1")
        ledger.record(**BASE, params={"channel": 40}, session_id="s1")
        ledger.record(**BASE, params={"channel": 50}, session_id="s2")
        assert ledger.count(family="trend") == 3
        assert ledger.session_count(family="trend") == 2

    def test_the_summary_says_it_is_only_a_floor(self, ledger):
        """The ledger cannot see a notebook, another machine, or a chart
        glanced at and discarded. Saying so is part of the output."""
        ledger.record(**BASE, params={"channel": 55})
        summary = ledger.summary("donchian_trend", "trend")
        assert any("FLOOR" in n for n in summary.notes)
        assert "searched 1 times" in summary.sentence()


class TestPersistence:
    def test_the_count_survives_a_restart(self, tmp_path):
        """A ledger that forgot on restart would reset the correction to its
        weakest setting every time the process bounced."""
        path = tmp_path / "trials.db"
        first = TrialLedger(path)
        for ch in (30, 40, 55):
            first.record(**BASE, params={"channel": ch})
        first.close()
        assert TrialLedger(path).count(family="trend") == 3

    def test_a_corrupt_ledger_refuses_to_open(self, tmp_path):
        """Distinct from an empty one. 'Nothing was searched' and 'the record
        of the search is gone' must not be conflated -- the second silently
        weakens every subsequent verdict."""
        path = tmp_path / "trials.db"
        path.write_bytes(b"this is not a database" * 100)
        with pytest.raises(LedgerUnreadable, match="not a readable database"):
            TrialLedger(path)

    def test_a_ledger_without_the_kind_column_is_migrated_as_search(self, tmp_path):
        """History written before the search/validation split is all search,
        and must stay counted rather than dropping to zero."""
        path = tmp_path / "old.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("""
            CREATE TABLE trials (
                trial_key TEXT PRIMARY KEY, strategy TEXT NOT NULL, family TEXT NOT NULL,
                timeframe TEXT NOT NULL, instruments TEXT NOT NULL, params TEXT NOT NULL,
                data_window TEXT NOT NULL, data_label TEXT NOT NULL DEFAULT 'unknown',
                first_seen_ns INTEGER NOT NULL, last_seen_ns INTEGER NOT NULL,
                runs INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '');
            INSERT INTO trials VALUES ('k1','donchian_trend','trend','H4','[]','{}','w',
                'synthetic',1,1,1,'');
        """)
        conn.commit()
        conn.close()
        assert TrialLedger(path).count(family="trend") == 1


class TestEffectiveTrialCount:
    def test_it_is_the_largest_of_every_lower_bound(self):
        assert effective_trial_count(declared=1, ledger=17, variants=5) == 17
        assert effective_trial_count(declared=200, ledger=17, variants=5) == 200
        assert effective_trial_count(declared=1, ledger=0, variants=9) == 9

    def test_it_never_drops_below_two(self):
        """expected_max_sharpe returns 0 for n < 2, so a count of 1 sets the
        bar to zero and turns the multiple-testing gate into 'is the Sharpe
        positive' -- in the run whose entire purpose is multiple testing."""
        assert effective_trial_count(declared=1, ledger=0, variants=0) == 2
        assert effective_trial_count(declared=0, ledger=0, variants=0) == 2

    def test_understating_the_declaration_cannot_lower_the_bar(self, ledger):
        ledger.record(**BASE, params={"channel": 55})
        for name in ("supertrend_flip", "kama_trend", "adx_trend", "ichimoku_break"):
            ledger.record(strategy=name, family="trend", params={},
                          instruments=["EUR_USD"], timeframe="H4", data_window="w")
        charge = ledger.summary("donchian_trend", "trend").charge
        assert effective_trial_count(declared=1, ledger=charge, variants=0) == 5


class TestDefaultLedger:
    def test_recording_is_a_no_op_with_no_ledger_installed(self, no_default_ledger):
        set_default_ledger(None)
        assert default_ledger() is None
        assert record_trial(strategy="x", family="trend") is None

    def test_installing_one_makes_recording_automatic(self, tmp_path, no_default_ledger):
        installed = set_default_ledger(tmp_path / "auto.db")
        assert record_trial(strategy="x", family="trend", params={"a": 1},
                            instruments=["EUR_USD"], timeframe="H1",
                            data_window="w") is not None
        assert installed.count(family="trend") == 1

    def test_a_write_failure_never_aborts_the_run(self, tmp_path, no_default_ledger):
        """Losing a research run to a disk error while writing bookkeeping
        would be the wrong trade. The cost is an undercount, which is visible."""
        installed = set_default_ledger(tmp_path / "auto.db")
        installed.close()          # every write from here raises
        assert record_trial(strategy="x", family="trend") is None


class TestBacktestRecordsItsOwnTrial:
    """The automatic half: running a backtest IS the trial."""

    @pytest.fixture
    def universe(self):
        from sentinel.data.synthetic import generate_universe

        return generate_universe(n_bars=600, bars_per_day=6, seed=99,
                                 dollar_factor_strength=0.5)

    def _run(self, strategy, universe, **cfg):
        from sentinel.core.config import RiskConfig
        from sentinel.core.money import Instrument
        from sentinel.research.backtest import BacktestConfig, run_backtest

        instruments = {
            "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
            "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
        }
        data = {k: v for k, v in universe.items() if k in instruments}
        return run_backtest(strategy, data, instruments, RiskConfig(),
                            BacktestConfig(periods_per_year=1512, **cfg),
                            conversions={"USD": D("1")})

    def test_a_backtest_records_itself(self, tmp_path, universe, no_default_ledger):
        from sentinel.strategy.registry import build

        installed = set_default_ledger(tmp_path / "auto.db")
        self._run(build("donchian_trend"), universe, data_label="synthetic")
        assert installed.count(strategy="donchian_trend") == 1
        row = installed.list(strategy="donchian_trend")[0]
        assert row["family"] == "trend"
        assert row["data_label"] == "synthetic"
        assert "EUR_USD" in row["instruments"]

    def test_two_parameter_sets_are_two_trials(self, tmp_path, universe,
                                               no_default_ledger):
        from sentinel.strategy.registry import build

        installed = set_default_ledger(tmp_path / "auto.db")
        self._run(build("donchian_trend", channel=40), universe)
        self._run(build("donchian_trend", channel=55), universe)
        assert installed.count(strategy="donchian_trend") == 2

    def test_rerunning_an_identical_backtest_is_still_one_trial(
            self, tmp_path, universe, no_default_ledger):
        from sentinel.strategy.registry import build

        installed = set_default_ledger(tmp_path / "auto.db")
        self._run(build("donchian_trend"), universe)
        self._run(build("donchian_trend"), universe)
        assert installed.count(strategy="donchian_trend") == 1

    def test_a_validation_run_is_not_a_search(self, tmp_path, universe,
                                              no_default_ledger):
        from sentinel.strategy.registry import build

        installed = set_default_ledger(tmp_path / "auto.db")
        self._run(build("donchian_trend"), universe, trial_kind="validation",
                  cost_multiplier=2.0)
        assert installed.count(strategy="donchian_trend") == 0
        assert installed.count(strategy="donchian_trend", kind=None) == 1


class TestAcceptanceUsesTheLedger:
    """The enforcement point: a bigger ledger must mean a higher bar."""

    @pytest.fixture
    def candidate(self):
        from sentinel.research.backtest import BacktestResult
        from sentinel.research.metrics import compute_performance

        rng = np.random.default_rng(11)
        idx = pd.date_range("2022-01-03", periods=800, freq="4h", tz="UTC")
        rets = pd.Series(rng.normal(0.0004, 0.004, 800), index=idx)
        equity = 10000 * (1 + rets).cumprod()
        return BacktestResult(
            label="candidate", trades=[], equity_curve=equity,
            performance=compute_performance([], equity, 1512, 10000.0),
            vetoes={}, signals_generated=0, orders_submitted=0, orders_rejected=0,
            config_snapshot={}, per_bar_returns=rets, diagnostics={})

    def _gate(self, candidate, **kw):
        from sentinel.research.acceptance import evaluate

        verdict = evaluate(run_id="R1", strategy_name="donchian_trend",
                           candidate=candidate, baselines={}, periods_per_year=1512,
                           data_label="synthetic", **kw)
        gate = next(g for g in verdict.gates if g.id == "L5.1")
        return verdict, gate

    def test_the_bar_rises_with_the_ledger(self, candidate):
        """Same equity curve, more recorded search, higher required Sharpe."""
        _, low = self._gate(candidate, declared_trials=1)
        summary = TrialSummary(strategy="donchian_trend", family="trend",
                               strategy_trials=4, family_trials=40, sessions=6)
        _, high = self._gate(candidate, declared_trials=1, trial_summary=summary)
        low_bar = float(low.observed.split("bar ")[1].rstrip(")"))
        high_bar = float(high.observed.split("bar ")[1].rstrip(")"))
        assert high_bar > low_bar

    def test_the_report_says_how_much_searching_happened(self, candidate):
        summary = TrialSummary(strategy="donchian_trend", family="trend",
                               strategy_trials=4, family_trials=40, sessions=6)
        verdict, gate = self._gate(candidate, declared_trials=1, trial_summary=summary)
        assert "'trend' family has been searched 40 times across 6 session(s)" in gate.detail
        assert "the Sharpe bar is therefore" in gate.detail
        assert verdict.evidence["trial_ledger"]["charge"] == 40
        assert verdict.evidence["deflated_sharpe"]["effective_trials"] == 40

    def test_a_larger_declaration_still_wins(self, candidate):
        """The ledger cannot see what was done elsewhere, so an honest larger
        declaration is never overridden by a smaller ledger count."""
        summary = TrialSummary(strategy="donchian_trend", family="trend",
                               strategy_trials=4, family_trials=10, sessions=2)
        verdict, _ = self._gate(candidate, declared_trials=500, trial_summary=summary)
        assert verdict.evidence["deflated_sharpe"]["effective_trials"] == 500

    def test_variants_are_counted_by_column_not_by_observation(self, candidate):
        """The variant matrices are T x K, so the variant count is K.

        Reading the row count instead declared one trial per BAR: a 1200-bar
        run over five configurations reported 1200 trials. That is strict, so
        nothing looked broken, but it made the gate's stated reasoning false
        and it swamped every honest input -- the declared count and the ledger
        both became irrelevant next to a number that was really the sample
        length.
        """
        rng = np.random.default_rng(3)
        variants = rng.normal(0.0002, 0.004, (800, 5))    # 800 bars, 5 variants
        verdict, gate = self._gate(candidate, declared_trials=1,
                                   variant_returns=variants, pbo_matrix=variants)
        assert verdict.evidence["deflated_sharpe"]["variants_this_run"] == 5
        assert verdict.effective_trials == 5

    def test_the_verdict_records_what_was_actually_used(self, candidate):
        """`declared_trials` is what someone typed; `effective_trials` is what
        the gate ran against. Storing only the first would let a verdict be
        read as having been earned against the smaller number."""
        summary = TrialSummary(strategy="donchian_trend", family="trend",
                               strategy_trials=4, family_trials=40, sessions=6)
        verdict, _ = self._gate(candidate, declared_trials=1, trial_summary=summary)
        assert verdict.declared_trials == 1
        assert verdict.effective_trials == 40
        assert verdict.to_dict()["effective_trials"] == 40
