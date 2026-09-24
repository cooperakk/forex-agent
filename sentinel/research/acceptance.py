"""The acceptance protocol.

A strategy may not touch real money until it passes every gate below. The
thresholds are fixed in ``ResearchConfig`` *before* a run and are recorded with
the verdict, so nobody can discover a threshold after seeing the result.

The gates, in the order the research brief puts them -- cheapest failure first:

  L0  environment      cost arithmetic, capital granularity, venue capability
  L1  baselines        beat no-trade, coin-flip and buy-and-hold on the same data
  L2  random walk      Clark-West against "tomorrow = today"
  L3  factor alpha     significant after the dollar and carry controls
  L4  overfitting      PBO below the ceiling
  L5  multiple testing SPA / deflated Sharpe over every variant tried
  L6  stability        positive on a declared fraction of CPCV paths
  L7  stress           still positive at 2x cost and 2x latency
  L8  drawdown         worst CPCV path inside the pre-declared ceiling
  L9  power            history at least the computed MinTRL

A failure at any gate is a *result*, and it is recorded with the same weight as
a pass. The point of the protocol is to make it cheap to discover that
something does not work.

On L5 and the size of the strategy library
------------------------------------------

L5.1 is the gate that prices in how much searching produced the candidate, and
it is therefore the gate that decides whether a large library is an asset or a
liability. Thirty strategies searched over one history will hand you a
flattering winner whether or not any of them has an edge; the only defence is
that the trial count rises with the library.

So the count used here is never taken on faith. It is the largest of four
figures, each a lower bound on the real search: what the operator declared, how
many variants this run evaluated, what the persistent trial ledger
(``research/trials.py``) has recorded for the candidate's whole FAMILY, and a
floor of 2. Adding a strategy to a family therefore raises the bar every member
of that family has to clear, automatically, which is the property that makes
the library safe to grow.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..core.clock import wall_ns
from ..core.config import ResearchConfig, RiskConfig
from ..core.money import (
    D, Instrument, break_even_win_rate, dec, min_equity_for_granularity,
)
from .backtest import BacktestConfig, BacktestResult, run_backtest
from .factors import attribute, carry_factor, dollar_factor, momentum_factor
from .stats import (
    deflated_sharpe_ratio, directional_accuracy, hansen_spa, min_track_record_length,
    posterior_given_significant, probability_of_backtest_overfitting, sharpe_ratio,
)
from .trials import TrialSummary, effective_trial_count

#: The gates a verdict MUST contain, with the evidence each one needs. A gate
#: whose evidence was not supplied is added as a FAILURE, not left out: a
#: verdict with five gates present used to read `accepted=True`, because
#: "every gate that ran passed" is a statement about the gates that ran. The
#: protocol is a fixed list; "not evaluated" is one of the ways to fail it.
REQUIRED_GATES = {
    "L1": ("beats the baselines", "baseline backtests"),
    "L2": ("directional content vs a random walk", "the candidate's signal log"),
    "L3": ("alpha after factor controls", "factor series (dollar, carry, momentum)"),
    "L4": ("backtest overfitting", "the variant returns matrix (PBO)"),
    "L5.1": ("deflated Sharpe", "the candidate returns"),
    "L5.2": ("superior predictive ability", "the variant returns matrix (SPA)"),
    "L6": ("stability across CPCV paths", "combinatorial purged CV paths"),
    "L7": ("cost / latency stress", "the stressed backtest"),
    "L8": ("drawdown ceiling", "the candidate and the CPCV paths"),
    "L9": ("statistical power", "the candidate returns"),
    "L10": ("verified data and costs", "a dataset manifest verified by content"),
    "L11": ("shared runtime", "an agent-replay candidate at production cadence"),
    "L12": ("forward validation", "a verified forward record under this fingerprint"),
    "L13": ("generation integrity", "the candidate's diagnostics"),
}
#: Closed forward trades before L12 can pass. A floor, not a statistical
#: guarantee: fifty trades distinguish a disaster from a candidate, not a
#: candidate from luck.
FORWARD_MIN_TRADES = 50


@dataclass
class Gate:
    id: str
    name: str
    passed: bool
    observed: str
    threshold: str
    detail: str = ""
    blocking: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Verdict:
    run_id: str
    strategy: str
    created_at_ns: int
    accepted: bool
    config_hash: str = ""
    gates: List[Gate] = field(default_factory=list)
    thresholds: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)
    declared_trials: int = 1
    # What L5.1 actually used: max(declared, ledger, variants, 2). Stored
    # separately from `declared_trials` so a verdict cannot later be read as
    # having been earned against the smaller number someone typed.
    effective_trials: int = 2
    prior: float = 0.03
    posterior_if_passed: float = 0.0
    data_label: str = "unknown"
    summary: str = ""

    @property
    def failed_gates(self) -> List[str]:
        return [g.id for g in self.gates if g.blocking and not g.passed]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "strategy": self.strategy,
            "created_at_ns": self.created_at_ns, "accepted": self.accepted,
            "gates": [g.to_dict() for g in self.gates],
            "failed_gates": self.failed_gates,
            "thresholds": self.thresholds, "evidence": self.evidence,
            "config_hash": self.config_hash,
            "declared_trials": self.declared_trials,
            "effective_trials": self.effective_trials, "prior": self.prior,
            "posterior_if_passed": round(self.posterior_if_passed, 4),
            "data_label": self.data_label, "summary": self.summary,
        }


# --------------------------------------------------------------------------- #
# Layer 0: the cheap eliminations
# --------------------------------------------------------------------------- #


def environment_gates(
    *,
    instrument: Instrument,
    equity: Decimal,
    risk_pct: Decimal,
    stop_pips: Decimal,
    target_pips: Decimal,
    round_trip_cost_pips: Decimal,
    pip_value_per_lot: Decimal,
    broker_supports_client_order_id: bool,
    broker_supports_server_stop: bool,
    connectivity_uptime_pct: Optional[float] = None,
    withdrawal_tested: Optional[bool] = None,
) -> List[Gate]:
    """Run before a line of strategy code. Each of these can end a project in a
    week, and each costs almost nothing to check."""
    gates: List[Gate] = []

    be = break_even_win_rate(target_pips, stop_pips, round_trip_cost_pips)
    gates.append(Gate(
        "L0.1", "cost barrier", be < D("0.60"), f"{be * 100:.1f}% break-even win rate",
        "< 60%",
        f"target {target_pips}p / stop {stop_pips}p with {round_trip_cost_pips}p round-trip cost. "
        "Above 60% the required accuracy is not plausibly attainable and the whole "
        "family should be abandoned rather than optimised."))

    required = min_equity_for_granularity(stop_pips, dec(risk_pct) / D("100"),
                                          instrument, pip_value_per_lot)
    gates.append(Gate(
        "L0.2", "capital granularity", dec(equity) >= required,
        f"{equity} account", f">= {required:.0f}",
        "Below this the 0.01-lot floor distorts the risk budget by more than 20%; "
        "the stop distance or the project's goal has to change."))

    gates.append(Gate(
        "L0.3", "venue idempotency", broker_supports_client_order_id,
        "supported" if broker_supports_client_order_id else "NOT supported", "required",
        "Without a venue-deduplicated client order id, a lost response cannot be "
        "resolved safely. The degraded protocol narrows the race; it does not close it.",
        blocking=False))

    gates.append(Gate(
        "L0.4", "venue-side stop", broker_supports_server_stop,
        "supported" if broker_supports_server_stop else "NOT supported", "required",
        "A stop held only in our process does not exist during a disconnection."))

    if connectivity_uptime_pct is not None:
        gates.append(Gate(
            "L0.5", "connectivity", connectivity_uptime_pct >= 99.0,
            f"{connectivity_uptime_pct:.2f}% uptime", ">= 99.00%",
            "Measured over weeks BEFORE any real trade. Poor connectivity eliminates "
            "short horizons independently of any financial analysis."))

    if withdrawal_tested is not None:
        gates.append(Gate(
            "L0.6", "full capital cycle", withdrawal_tested,
            "deposit->hold->withdraw completed" if withdrawal_tested else "NOT completed",
            "required",
            "No strategy compensates for counterparty risk. Withdraw the minimum "
            "deposit successfully before anything else is discussed."))
    return gates


# --------------------------------------------------------------------------- #
# Full protocol
# --------------------------------------------------------------------------- #


def evaluate(
    *,
    run_id: str,
    strategy_name: str,
    candidate: BacktestResult,
    baselines: Dict[str, BacktestResult],
    cpcv_path_returns: Optional[List[pd.Series]] = None,
    stressed: Optional[BacktestResult] = None,
    variant_returns: Optional[np.ndarray] = None,
    pbo_matrix: Optional[np.ndarray] = None,
    factor_data: Optional[Dict[str, Sequence[float]]] = None,
    research_config: Optional[ResearchConfig] = None,
    declared_trials: int = 1,
    trial_summary: Optional[TrialSummary] = None,
    max_drawdown_ceiling_pct: float = 10.0,
    periods_per_year: int = 252,
    data_label: str = "synthetic",
    environment: Optional[List[Gate]] = None,
    instruments: Optional[Sequence[str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeframe: str = "",
    cpcv_report=None,
    venue_profile: str = "",
    provenance: Optional[Dict[str, Any]] = None,
    forward: Optional[Dict[str, Any]] = None,
    runtime_config=None,
    store=None,
) -> Verdict:
    rc = research_config or ResearchConfig()
    gates: List[Gate] = list(environment or [])
    evidence: Dict[str, Any] = {}
    if venue_profile:
        evidence["venue_profile"] = venue_profile
    if cpcv_report is not None and hasattr(cpcv_report, "to_dict"):
        evidence["cpcv_procedure"] = cpcv_report.to_dict()
    if cpcv_report is not None and getattr(cpcv_report, "n_unique_variants", 2) < 2:
        # One configuration under several names is not a family; the paths
        # would all be the same series and L6 would be measuring nothing.
        cpcv_path_returns = None
    returns = candidate.per_bar_returns.to_numpy(dtype=float)
    cand_sharpe = sharpe_ratio(returns, periods_per_year)
    evidence["candidate"] = candidate.performance.to_dict()

    # ---- L1 baselines ---------------------------------------------------- #
    beat_all = True
    base_detail = []
    for name, res in baselines.items():
        b_sharpe = res.performance.sharpe
        better = cand_sharpe > b_sharpe
        beat_all = beat_all and better
        base_detail.append(f"{name}: {b_sharpe:.2f}")
    gates.append(Gate(
        "L1", "beats the baselines", beat_all and cand_sharpe > 0,
        f"candidate Sharpe {cand_sharpe:.2f}",
        "> every baseline and > 0",
        "; ".join(base_detail) or "no baselines supplied"))
    evidence["baselines"] = {k: v.performance.to_dict() for k, v in baselines.items()}

    # ---- L2 directional content ------------------------------------------ #
    #
    # The random-walk null, asked of the thing the strategy actually produces.
    # A rule-based strategy emits DIRECTIONS, not numeric forecasts, so the
    # question is whether its calls are followed by moves in the called
    # direction more than chance -- on every signal it raised, whether or not
    # the risk engine let it trade. (This used to run Clark-West on the
    # strategy's own equity returns against half its lagged equity return,
    # which is a comparison of two things neither of which is a forecast of
    # the exchange rate; see stats.directional_accuracy.)
    if rc.require_random_walk_beat:
        signed = candidate.signed_forward_returns("h") \
            if hasattr(candidate, "signed_forward_returns") else np.asarray([])
        da = directional_accuracy(signed)
        n_sig = int(da.detail.get("n", 0))
        hit = da.detail.get("hit_rate", float("nan"))
        gates.append(Gate(
            "L2", "directional content vs a random walk",
            da.p_value < rc.alpha and n_sig >= 30,
            (f"{n_sig} signals, hit rate {hit * 100:.1f}%, mean signed return "
             f"{da.detail.get('mean_signed_return', 0.0) * 1e4:.2f} bp, p = {da.p_value:.4f}"
             if n_sig else "no signals were recorded by the backtest"),
            f"p < {rc.alpha} on >= 30 signals",
            "The Meese-Rogoff null, asked of the rule's own calls: over its declared "
            "horizon, does price go the way it said? Newey-West errors because "
            "consecutive signals overlap. A rule that fails this has no forecasting "
            "content, whatever its equity curve did."))
        evidence["directional_accuracy"] = da.to_dict()

    # ---- L3 factor alpha -------------------------------------------------- #
    if rc.require_factor_alpha and factor_data:
        attr = attribute(returns, factor_data, periods_per_year)
        gates.append(Gate(
            "L3", "alpha after factor controls", attr.significant(rc.alpha),
            f"alpha t = {attr.alpha_t:.2f}, p = {attr.alpha_p:.4f}", f"p < {rc.alpha}",
            "Without this, what was found is a repackaged dollar or carry premium, "
            "available more cheaply and with the same crash exposure."))
        evidence["factor_attribution"] = attr.to_dict()

    # ---- L4 overfitting --------------------------------------------------- #
    if pbo_matrix is not None and np.asarray(pbo_matrix).size:
        pbo = probability_of_backtest_overfitting(pbo_matrix, periods_per_year=periods_per_year)
        gates.append(Gate(
            "L4", "backtest overfitting", pbo.pbo <= rc.pbo_max,
            f"PBO = {pbo.pbo:.2f}", f"<= {rc.pbo_max}",
            f"OOS degradation slope {pbo.performance_degradation_slope:.2f}; "
            f"P(OOS loss) {pbo.prob_oos_loss:.2f} over {pbo.n_splits} splits."))
        evidence["pbo"] = pbo.to_dict()

    # ---- L5 multiple testing ---------------------------------------------- #
    #
    # The trial count is the WHOLE gate. With one declared trial the bar is
    # zero (expected_max_sharpe returns 0 for n < 2) and L5.1 degenerates into
    # "is the Sharpe positive" -- the multiple-testing gate switched off, in
    # the run whose entire purpose is to price in multiple testing.
    #
    # So the count is never taken on faith. It is the LARGEST of: what the
    # operator declared, how many variants this run actually evaluated, and a
    # floor of 2. Understating it is the single cheapest way to make a
    # strategy look acceptable, and it is the one number an optimistic
    # operator will understate without noticing.
    #
    # Both matrices are T x K -- time down the rows, one column per variant --
    # which is the convention `hansen_spa` and
    # `probability_of_backtest_overfitting` document and require. So the
    # variant count is the number of COLUMNS.
    #
    # This read `variant_returns.shape[0]` and therefore counted OBSERVATIONS
    # as variants: a 1200-bar run over 5 configurations declared 1200 trials.
    # It failed in the strict direction, so nothing ever looked wrong, but the
    # gate's stated reasoning was false ("1200 trials" when five were tried),
    # and an observation count swamps every honest input -- the declared
    # figure, the ledger, all of it -- which made the whole trial-accounting
    # path dead weight. A number nobody can act on is not conservatism.
    observed_variants = 0
    for matrix in (variant_returns, pbo_matrix):
        if matrix is None:
            continue
        arr = np.asarray(matrix)
        if arr.ndim > 1 and arr.size:
            observed_variants = max(observed_variants, int(arr.shape[1]))
    # The fourth source, and the one a growing strategy library makes
    # indispensable: the persistent ledger of everything ever backtested. A
    # candidate is charged with its whole FAMILY's search, because the best of
    # six trend systems was selected from six. Without this, adding strategies
    # would raise the chance of a flattering winner while leaving the bar it
    # has to clear exactly where it was.
    ledger_trials = int(trial_summary.charge) if trial_summary is not None else 0
    effective_trials = effective_trial_count(
        declared_trials, ledger_trials, observed_variants, floor=2)

    # The dispersion of Sharpe across the variants actually tried is the
    # honest input to E[max SR]; the asymptotic fallback is used only when no
    # family was evaluated.
    trial_sharpes = None
    if variant_returns is not None and np.asarray(variant_returns).ndim == 2 \
            and np.asarray(variant_returns).shape[1] >= 3:
        candidates = [sharpe_ratio(col, periods_per_year)
                      for col in np.asarray(variant_returns, dtype=float).T]
        # A family whose members are (near-)identical -- a filter that blocked
        # almost everything, a parameter that changes nothing -- has no
        # dispersion to measure, and E[max] over zero variance is zero: a bar
        # of 0.00 that any positive Sharpe clears. Fall back to the asymptotic
        # variance rather than certify against nothing.
        if len({round(c, 6) for c in candidates}) >= 3 and \
                float(np.var(candidates, ddof=1)) > 1e-9:
            trial_sharpes = candidates
    dsr = deflated_sharpe_ratio(returns, effective_trials, trial_sharpes=trial_sharpes,
                                periods_per_year=periods_per_year)
    parts = [f"{effective_trials} trials"]
    if trial_summary is not None:
        parts.append(trial_summary.sentence())
    if effective_trials > max(1, declared_trials):
        parts.append(f"you declared {declared_trials}; this run itself evaluated "
                     f"{observed_variants} and the ledger holds {ledger_trials}, so "
                     "the largest figure is used -- a trial you ran is a trial that "
                     "counts")
    parts.append(f"the Sharpe bar is therefore {dsr['sr_star']:.2f}: what pure noise "
                 "is expected to produce as the best of that many attempts")
    gates.append(Gate(
        "L5.1", "deflated Sharpe", dsr["dsr"] >= rc.min_dsr,
        f"DSR = {dsr['dsr']:.3f} (SR {dsr['sr']:.2f} vs bar {dsr['sr_star']:.2f})",
        f">= {rc.min_dsr}",
        ". ".join(parts) + "."))
    evidence["deflated_sharpe"] = dsr
    evidence["deflated_sharpe"]["declared_trials"] = int(declared_trials)
    evidence["deflated_sharpe"]["ledger_trials"] = ledger_trials
    evidence["deflated_sharpe"]["variants_this_run"] = observed_variants
    evidence["deflated_sharpe"]["effective_trials"] = effective_trials
    if trial_summary is not None:
        evidence["trial_ledger"] = trial_summary.to_dict()

    if variant_returns is not None and np.asarray(variant_returns).size:
        spa = hansen_spa(np.asarray(variant_returns, dtype=float), n_boot=500)
        gates.append(Gate(
            "L5.2", "superior predictive ability", spa.p_value < rc.alpha,
            f"SPA p = {spa.p_value:.4f}", f"< {rc.alpha}",
            "Bootstrap over every variant tried, with dependence preserved."))
        evidence["hansen_spa"] = spa.to_dict()

    # ---- L6 stability ------------------------------------------------------ #
    if cpcv_path_returns:
        path_sharpes = [sharpe_ratio(p.to_numpy(dtype=float), periods_per_year)
                        for p in cpcv_path_returns]
        positive = float(np.mean([s > 0 for s in path_sharpes]))
        gates.append(Gate(
            "L6", "stability across CPCV paths", positive >= rc.cpcv_positive_path_fraction,
            f"{positive * 100:.0f}% of {len(path_sharpes)} paths positive",
            f">= {rc.cpcv_positive_path_fraction * 100:.0f}%",
            f"Sharpe distribution: p10 {np.percentile(path_sharpes, 10):.2f}, "
            f"median {np.median(path_sharpes):.2f}, p90 {np.percentile(path_sharpes, 90):.2f}."))
        evidence["cpcv"] = {
            "n_paths": len(path_sharpes),
            "sharpes": [round(s, 4) for s in path_sharpes],
            "positive_fraction": round(positive, 4),
        }

    # ---- L7 stress --------------------------------------------------------- #
    if stressed is not None:
        s_sharpe = stressed.performance.sharpe
        gates.append(Gate(
            "L7", f"{rc.cost_stress_multiple:.0f}x cost / "
                  f"{rc.latency_stress_multiple:.0f}x latency stress",
            s_sharpe > 0 and stressed.performance.net_return_pct > 0,
            f"stressed Sharpe {s_sharpe:.2f}, return "
            f"{stressed.performance.net_return_pct:+.2f}%",
            "> 0",
            "A strategy that is positive only at the optimistic end of the cost "
            "assumption will not be positive in production: the spread widens on "
            "news and the link slows exactly when it matters."))
        evidence["stress"] = stressed.performance.to_dict()

    # ---- L8 drawdown -------------------------------------------------------- #
    worst_dd = candidate.performance.max_drawdown_pct
    if cpcv_path_returns:
        for p in cpcv_path_returns:
            curve = (1 + p.fillna(0)).cumprod()
            peaks = curve.cummax()
            worst_dd = max(worst_dd, float(((peaks - curve) / peaks).max() * 100))
    gates.append(Gate(
        "L8", "drawdown ceiling", worst_dd <= max_drawdown_ceiling_pct,
        f"worst path drawdown {worst_dd:.2f}%", f"<= {max_drawdown_ceiling_pct:.2f}%",
        "Declared before the run. The worst observed path, not the average one."))
    evidence["worst_drawdown_pct"] = round(worst_dd, 4)

    # ---- L9 power ----------------------------------------------------------- #
    mintrl = min_track_record_length(returns, 0.0, 1 - rc.alpha, periods_per_year)
    n_obs = len(returns)
    if mintrl is None:
        gates.append(Gate("L9", "statistical power", False,
                          "Sharpe is not above zero", "MinTRL computable",
                          "No amount of additional history settles a Sharpe that is "
                          "not positive in the first place."))
    else:
        gates.append(Gate(
            "L9", "statistical power", n_obs >= mintrl,
            f"{n_obs} observations", f">= {mintrl:.0f} (MinTRL)",
            "Minimum history for this Sharpe, at this skew and kurtosis, to be "
            "distinguishable from zero at the declared confidence."))
        evidence["min_track_record_length"] = round(mintrl, 1)

    posterior = posterior_given_significant(rc.declared_prior, rc.alpha, 0.5)
    accepted = all(g.passed for g in gates if g.blocking)
    # ---- L10: data and costs, verified by CONTENT ------------------------- #
    # A label is not provenance. What passes is a manifest whose file hashes
    # matched, whose files carry the venue's own bid/ask OHLC, and whose cost
    # schedule is complete -- research/evidence.verify_dataset produces it.
    prov = provenance or {}
    quality_ok = bool(data_label == "live-quality" and prov.get("verified") is True
                      and prov.get("bid_ask") is True and prov.get("sha256")
                      and prov.get("cost_schedule") and prov.get("broker"))
    gates = [g for g in gates if g.id != "L10"]
    gates.append(Gate(
        "L10", "verified data and costs", quality_ok,
        (f"{data_label}, verified against {len(prov.get('sha256') or {})} file hashes, "
         f"broker {prov.get('broker')}" if quality_ok else
         f"{data_label}" + ("" if data_label == "live-quality" else " (cannot promote)")),
        "live-quality: venue bid/ask OHLC + file hashes + complete cost schedule",
        "Acceptance requires the venue's own historical bid/ask and the real cost "
        "schedule, checked by content. Synthetic or third-party mid prices can "
        "falsify a strategy but can never accept one; a typed label proves nothing."))
    if not quality_ok:
        accepted = False
    evidence["provenance"] = prov

    # ---- L11: the thing validated is the thing that runs ------------------ #
    diag = candidate.diagnostics or {}
    parity = bool(diag.get("engine", "").startswith("agent-replay")
                  and int(diag.get("execution_cadence_sec", 10**9))
                  <= int(diag.get("decision_interval_sec", 60))
                  and diag.get("runtime_context_complete") is True)
    gates.append(Gate(
        "L11", "shared runtime", parity,
        (f"{diag.get('engine', 'unknown')}, cadence {diag.get('execution_cadence_sec', '?')}s, "
         f"news {'replayed' if diag.get('news_replayed') else 'off'}"),
        "agent-replay at production cadence, news context complete",
        "The production Agent/OMS/risk loop itself, driven by execution bars no "
        "coarser than its decision interval, with the news calendar replayed at "
        "the simulated clock (or disabled in the runtime). A rules harness "
        "re-implements the agent and validates a program that never runs."))
    if not parity:
        accepted = False

    # ---- L12: it worked on the account it is for --------------------------- #
    fwd = forward or {}
    from .verdicts import config_fingerprint as _fp
    expected_hash = _fp(
        instruments if instruments is not None else candidate.diagnostics.get("instruments", []),
        params if params is not None
        else (candidate.config_snapshot.get("strategy") or {}).get("params", {}),
        timeframe or (candidate.config_snapshot.get("strategy") or {}).get("timeframe", ""),
        runtime_config=runtime_config)
    forward_ok = bool(fwd.get("verified") is True
                      and int(fwd.get("n_trades", 0)) >= FORWARD_MIN_TRADES
                      and float(fwd.get("net_pnl", 0)) > 0
                      and fwd.get("runtime_hash") == expected_hash
                      and float(fwd.get("max_drawdown_pct", 101)) <= max_drawdown_ceiling_pct)
    gates.append(Gate(
        "L12", "forward validation", forward_ok,
        (f"{fwd.get('n_trades')} closed trades, net {float(fwd.get('net_pnl', 0)):+.2f}, "
         f"max DD {float(fwd.get('max_drawdown_pct', 0)):.2f}%, fingerprint "
         f"{'matches' if fwd.get('runtime_hash') == expected_hash else 'MISMATCH'}"
         if fwd.get("verified") else "no verified forward record"),
        f">= {FORWARD_MIN_TRADES} closed trades under THIS fingerprint, positive net "
        f"P&L, drawdown <= {max_drawdown_ceiling_pct:.1f}%",
        "A demo or live run of this exact configuration, reconciled to the cent "
        "against the account, with floating-equity drawdown. It is the only gate "
        "that touches a real venue's fills, spreads and rejections."))
    if not forward_ok:
        accepted = False
    evidence["forward"] = fwd

    # ---- L13: the run was clean ------------------------------------------- #
    clean = (not diag.get("generation_errors")
             and int(diag.get("generation_error_count", 0)) == 0
             and not diag.get("halted")
             and int(candidate.performance.n_trades) >= 30)
    gates.append(Gate(
        "L13", "generation integrity", bool(clean),
        (f"{candidate.performance.n_trades} trades, "
         f"{int(diag.get('generation_error_count', 0))} generation errors"
         + (", HALTED: " + str(diag.get("halt_reason", ""))[:60] if diag.get("halted") else "")),
        ">= 30 trades, no strategy errors, no halt",
        "A candidate whose strategy raised exceptions on some bars, or that "
        "halted mid-run, produced statistics about a different run than the one "
        "that would trade."))
    if not clean:
        accepted = False

    # ---- the fixed list: anything not evaluated is a failure ---------------- #
    present = {g.id for g in gates}
    required = dict(REQUIRED_GATES)
    if not rc.require_random_walk_beat:
        required.pop("L2", None)
    if not rc.require_factor_alpha:
        required.pop("L3", None)
    for gate_id, (name, needs) in required.items():
        if gate_id in present:
            continue
        gates.append(Gate(
            gate_id, name, False, "not evaluated", "required",
            f"No evidence was supplied for this gate ({needs}). The protocol is a "
            "fixed list, and a gate that did not run did not pass. Supply the "
            "evidence -- scripts/run_acceptance.py produces all of it -- or the "
            "verdict stays negative."))
    evidence["gates_not_evaluated"] = sorted(
        g for g in required if g not in present)

    accepted = all(g.passed for g in gates if g.blocking)

    failed = [g.id for g in gates if g.blocking and not g.passed]
    summary = ("accepted: every declared gate passed" if accepted
               else f"not accepted: failed {', '.join(failed)}")

    from .verdicts import config_fingerprint

    fingerprint = config_fingerprint(
        instruments if instruments is not None
        else candidate.diagnostics.get("instruments", []),
        params if params is not None
        else (candidate.config_snapshot.get("strategy") or {}).get("params", {}),
        timeframe or (candidate.config_snapshot.get("strategy") or {}).get("timeframe", ""))

    verdict = Verdict(
        run_id=run_id, strategy=strategy_name, created_at_ns=wall_ns(),
        accepted=accepted, config_hash=fingerprint, gates=gates,
        thresholds={
            "alpha": rc.alpha, "pbo_max": rc.pbo_max, "min_dsr": rc.min_dsr,
            "cpcv_positive_path_fraction": rc.cpcv_positive_path_fraction,
            "cost_stress_multiple": rc.cost_stress_multiple,
            "latency_stress_multiple": rc.latency_stress_multiple,
            "max_drawdown_ceiling_pct": max_drawdown_ceiling_pct,
            "declared_prior": rc.declared_prior,
        },
        evidence=evidence, declared_trials=declared_trials,
        effective_trials=effective_trials, prior=rc.declared_prior,
        posterior_if_passed=posterior, data_label=data_label, summary=summary,
    )
    # Record every verdict, pass or fail. A failure is a result and belongs in
    # the registry; and without this write the promotion guard has no legal way
    # to ever be satisfied, which would leave hand-editing the database as the
    # only route -- worse than the endpoint it replaced.
    if store is not None:
        store.record(verdict, config_hash=fingerprint)
    return verdict


def promote(allocation, verdict: Verdict):
    """Move a strategy allocation to ``accepted``. The only legal path."""
    from ..core.config import StrategyAllocation

    if not isinstance(allocation, StrategyAllocation):
        raise TypeError("promote expects a StrategyAllocation")
    if not verdict.accepted:
        raise ValueError(
            f"cannot promote {allocation.name!r}: verdict {verdict.run_id} failed "
            f"{', '.join(verdict.failed_gates)}")
    if verdict.strategy != allocation.name:
        raise ValueError("verdict belongs to a different strategy")
    from .verdicts import config_fingerprint

    data = allocation.model_dump()
    fingerprint = config_fingerprint(allocation.instruments, allocation.params,
                                     allocation.timeframe)
    if verdict.config_hash and verdict.config_hash != fingerprint:
        raise ValueError(
            f"verdict {verdict.run_id} was earned on a different configuration; "
            "re-run the protocol on the configuration you intend to trade")
    data.update({"lifecycle": "accepted", "accepted_at_ns": wall_ns(),
                 "acceptance_run_id": verdict.run_id})
    return StrategyAllocation.model_validate(data)


def suspend(allocation, reason: str):
    from ..core.config import StrategyAllocation

    data = allocation.model_dump()
    data.update({"lifecycle": "suspended", "enabled": False})
    out = StrategyAllocation.model_validate(data)
    out.params = {**out.params, "suspension_reason": reason}
    return out
