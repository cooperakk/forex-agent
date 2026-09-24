"""Per-trade autopsy and an honest counterfactual engine.

Every closed trade is classified into a failure or success mode and given a set
of counterfactuals -- "what if we had banked half at +1R" -- computed from the
data that actually exists.

The discipline that makes this useful rather than dangerous: a single trade
*never* produces a rule. A pattern produces a *candidate* lesson; a lesson
becomes a *proposal* only with a minimum sample, an effect size, a significance
test corrected for the number of patterns tested, and a diagnosis that
distinguishes a wrong parameter from a changed regime from ordinary bad luck; a
proposal changes live behaviour only after a validation run and a human
approval. That chain is what separates learning from the much more common
activity of reacting to the last loss.

Two things in here were previously dishonest, and both are worth naming because
they are the natural way to write this code.

**1. Counterfactuals were computed only where they helped.** Each alternative
rule was marked ``applicable`` exactly on the trades it improved -- "bank half
at +1R" was applicable when ``r < 1.0``, where the delta is positive by
construction, and skipped when ``r > 1.0``, where banking half costs you the
runner. The aggregate then ran a one-sided t-test on a population selected for
having a positive value. That test cannot fail. With enough trades it returns
p < 0.001 on a rule with no merit whatsoever, and the proposal engine downstream
treats that as evidence. The fix is that a counterfactual is evaluated over
*every* trade where the alternative rule would have DONE something, including
all the trades where it does damage.

**2. Counterfactuals invented price paths they never observed.** The old
``wider_stop_1.5x`` scored a stopped-out trade as ``min(mfe, 2.0) * 0.667 +
1.0``. But the trade ended at the stop: there is no recorded price after that
instant, in this system or anywhere else it could be fetched from. Whether a
wider stop would have survived and gone on to make money is not a hard question,
it is an unanswerable one, and the formula answered it favourably every time.
Anything that needs price after the actual exit is now marked
``computable=False`` with the reason attached, and it is excluded from the
aggregate rather than filled in.

What remains is smaller and true. ``partial_at_1R`` is computable with no price
path at all, because taking a partial does not change where the rest of the
position exits. Everything else needs the bars, and gets them when the caller
supplies them.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..core.types import ClosedTrade, Side

# Exit reasons that mean "the clock closed this, not the thesis". The venue
# side of this is a free-text string that gets truncated to 32 characters, so
# matching it exactly against "time_stop" silently never fired -- and
# `slow_bleed`, the mode that tells you the holding horizon is wrong, was
# structurally unreachable while looking perfectly well-implemented.
TIME_STOP_REASONS = ("time_stop", "horizon_stop")
TIME_STOP_PREFIXES = ("time stop", "time_stop", "horizon")
WEEKEND_FLAT_PREFIXES = ("weekend flat", "weekend_flat")


def exit_kind(exit_reason: str, *,
              time_stop_reasons: Sequence[str] = TIME_STOP_REASONS) -> str:
    """Normalise a venue's exit string into one of a few known kinds.

    OANDA reports a manual close as ``"closed"``; the paper broker records
    whatever reason string the caller passed, truncated. Neither ever produces
    the literal token the classifier was comparing against. Matching on a
    prefix, case-folded, is what makes the time-stop and weekend-flat modes
    reachable on a real venue instead of only in a unit test.
    """
    reason = (exit_reason or "").strip().lower()
    if not reason:
        return "unknown"
    if reason in {r.lower() for r in time_stop_reasons}:
        return "time_stop"
    if any(reason.startswith(p) for p in TIME_STOP_PREFIXES):
        return "time_stop"
    if any(reason.startswith(p) for p in WEEKEND_FLAT_PREFIXES):
        return "weekend_flat"
    if reason.startswith("stop_loss") or reason.startswith("stop loss"):
        return "stop_loss"
    if reason.startswith("take_profit") or reason.startswith("target"):
        return "take_profit"
    if reason.startswith("trail"):
        return "trail_stop"
    return "other"


@dataclass
class PathPoint:
    """One bar of a trade's life, denominated in R relative to the entry.

    ``high_r``/``low_r`` are the best and worst the position stood at during
    the bar. The ORDER within the bar is unknown, and that ambiguity is
    resolved adversely everywhere below: when a bar's range contains both a
    protective stop and a favourable target, the stop is assumed to have filled
    first. Assuming the opposite is the single most common way a counterfactual
    backtest manufactures money.
    """

    ts_ns: int
    high_r: float
    low_r: float
    close_r: float
    open_r: float = 0.0


def path_in_r(bars: Sequence[tuple[int, float, float, float, float]], *,
              entry_price: float, risk_price_distance: float, side: Side
              ) -> list[PathPoint]:
    """Convert ``(ts_ns, open, high, low, close)`` bars into R-space.

    ``risk_price_distance`` is the entry-to-stop distance the trade was SIZED
    for, in price terms. Deriving it from the realised P&L instead would be
    circular -- and undefined for a trade that closed near break-even, which is
    exactly the population the give-back counterfactuals care about.
    """
    if risk_price_distance <= 0:
        return []
    sign = 1.0 if side is Side.BUY else -1.0
    out: list[PathPoint] = []
    for ts, o, h, low, c in bars:
        a = (float(h) - entry_price) * sign / risk_price_distance
        b = (float(low) - entry_price) * sign / risk_price_distance
        out.append(PathPoint(
            ts_ns=int(ts), high_r=max(a, b), low_r=min(a, b),
            close_r=(float(c) - entry_price) * sign / risk_price_distance,
            open_r=(float(o) - entry_price) * sign / risk_price_distance))
    return out


@dataclass
class Counterfactual:
    """One alternative rule, scored against what actually happened.

    ``computable`` is the load-bearing field. A counterfactual that needs price
    data nobody has is not worth zero and it is not worth a guess -- it is
    unanswerable, and it must be excluded from any aggregate rather than filled
    in with something plausible.
    """

    name: str
    description: str
    delta_r: float
    applicable: bool = True
    computable: bool = True
    reason: str = ""
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "delta_r": round(self.delta_r, 3), "applicable": self.applicable,
                "computable": self.computable, "reason": self.reason,
                "assumptions": self.assumptions}

    @property
    def counts(self) -> bool:
        """Whether this observation may enter a statistic."""
        return self.applicable and self.computable


@dataclass
class TradeAutopsy:
    trade_id: str
    strategy: str
    instrument: str
    outcome: str                      # win | loss | scratch
    mode: str                         # the dominant pattern
    r_multiple: float
    mae_r: float
    mfe_r: float
    capture_ratio: float              # realised R / best available R
    tags: list[str] = field(default_factory=list)
    counterfactuals: list[Counterfactual] = field(default_factory=list)
    narrative: str = ""
    regime: str = ""
    closed_ns: int = 0
    had_path: bool = False

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id, "strategy": self.strategy,
            "instrument": self.instrument, "outcome": self.outcome, "mode": self.mode,
            "r_multiple": round(self.r_multiple, 3), "mae_r": round(self.mae_r, 3),
            "mfe_r": round(self.mfe_r, 3), "capture_ratio": round(self.capture_ratio, 3),
            "tags": self.tags,
            "counterfactuals": [c.to_dict() for c in self.counterfactuals],
            "narrative": self.narrative, "regime": self.regime,
            "closed_ns": self.closed_ns, "had_path": self.had_path,
        }


# Failure / success modes, in priority order.
#
# The test for whether a mode belongs here is not whether it describes the
# trade. It is whether knowing the trade is in this mode would change what the
# agent does next. A category that cannot change behaviour is a label, and
# labels accumulate until the list looks like understanding.
MODES = {
    "gap_loss": "loss exceeded the stop distance (gap or slippage)",
    "gave_back_open_profit": "was well in profit and closed at or below break-even",
    "stopped_at_breakeven": "reached +1R, then the break-even stop took it out flat",
    "stopped_then_reversed": "stopped out, then the trade would have worked",
    "news_shock_loss": "a loss whose adverse excursion arrived alongside a news event",
    "horizon_cut_a_winner": "the time stop closed a position that was still working",
    "slow_bleed": "closed on the time stop near break-even",
    "weekend_flat_exit": "closed by the Friday flatten rule, not by the thesis",
    "cost_dominated": "gross result was positive; cost made it negative",
    "target_too_far": "ran most of the way to the target and reversed",
    "clean_win": "reached the target without a deep adverse excursion",
    "clean_loss": "went against the position from the start",
    "scratch": "no material movement either way",
}

# What each mode could actually change. Being explicit about the ones that
# change NOTHING is the point: they are diagnostics for a human, and dressing
# them up as actionable is how a learning loop generates busywork.
MODE_ACTIONS: dict[str, str] = {
    "gap_loss": "none automatable: a gap is a property of the instrument and the "
                "session, not of a parameter. It argues for not holding through the "
                "event, which is a human decision about what to trade.",
    "gave_back_open_profit": "risk.partial_take_r, risk.breakeven_trigger_r",
    "stopped_at_breakeven": "diagnostic only. The lever would be a LATER break-even "
                            "move, which is more risk, and the proposal engine may not "
                            "propose more risk. A human decides this one.",
    "stopped_then_reversed": "risk.min_stop_pips, and only with a price path: whether "
                             "the trade would have recovered is not in the record.",
    "news_shock_loss": "risk.block_minutes_before_high_impact / _after",
    "horizon_cut_a_winner": "strategy.params horizon_bars",
    "slow_bleed": "strategy.params horizon_bars",
    "weekend_flat_exit": "none automatable: agent.trade_days is not proposable, and "
                         "holding over a weekend is an increase in risk by definition.",
    "cost_dominated": "risk.max_spread_pips_multiple, and the choice of instrument",
    "target_too_far": "risk.partial_take_r",
    "clean_win": "none: this is the system working",
    "clean_loss": "none: this is the system working. A clean loss is what a stop is "
                  "FOR, and the most expensive mistake available here is to treat a "
                  "run of them as a defect and tighten something.",
    "scratch": "none: cost without information",
}


# --------------------------------------------------------------------------- #
# Counterfactuals
# --------------------------------------------------------------------------- #
#
# Every one of these applies the same three rules:
#
#   1. Only the price path that actually occurred. Nothing after the real exit
#      exists, in this system or in any data it could fetch.
#   2. Costs are charged. An alternative rule with an extra exit leg pays for
#      that leg -- commission, half the spread, and a slippage allowance.
#   3. The fill is not free. A stop or a target is assumed to fill at its level
#      MINUS a slippage allowance, and a level the market gapped past fills at
#      the bar open. Where a single bar contains both a stop and a target, the
#      stop is assumed to have filled first.
#
# Rule 3 is what separates a counterfactual from a wish. The difference between
# "the price touched +1R" and "we sold at +1R" is the whole retail edge, twice
# over.

_DEFAULT_SLIPPAGE_R = 0.02


def _cost_r(trade: ClosedTrade) -> float:
    risk = float(trade.initial_risk) if trade.initial_risk else 0.0
    if risk <= 0:
        return 0.0
    return (float(trade.commission) + abs(float(trade.financing))) / risk


def _slippage_r(trade: ClosedTrade) -> float:
    """A per-fill slippage allowance in R, from the trade's own slippage.

    Falls back to a small fixed allowance rather than zero. Zero slippage is
    the assumption that makes every scale-out counterfactual look free, and it
    is never true.
    """
    r = abs(float(trade.r_multiple))
    pips = abs(float(trade.pnl_pips))
    observed = max(float(trade.entry_slippage_pips), float(trade.exit_slippage_pips))
    if r > 0.05 and pips > 1e-9 and observed > 0:
        pips_per_r = pips / r
        if pips_per_r > 1e-9:
            return max(_DEFAULT_SLIPPAGE_R, observed / pips_per_r)
    return _DEFAULT_SLIPPAGE_R


def _first_touch(path: Sequence[PathPoint], level_r: float,
                 from_index: int = 0) -> int | None:
    """Index of the first bar whose range reaches ``level_r`` upward."""
    for i in range(from_index, len(path)):
        if path[i].high_r >= level_r:
            return i
    return None


def _fill_at(point: PathPoint, level_r: float, *, favourable: bool,
             slip_r: float) -> float:
    """Realised R of an order at ``level_r`` filling during ``point``.

    A bar that opened beyond the level gapped through it, and the fill is the
    open, not the level. Otherwise the fill is the level, worsened by slippage
    in whichever direction hurts.
    """
    if favourable:
        if point.open_r >= level_r:
            return point.open_r - slip_r
        return level_r - slip_r
    if point.open_r <= level_r:
        return point.open_r - slip_r
    return level_r - slip_r


def counterfactuals(trade: ClosedTrade, *,
                    path: Sequence[PathPoint] | None = None,
                    partial_fraction: float = 0.5,
                    partial_r: float = 1.0,
                    breakeven_r: float = 1.0) -> list[Counterfactual]:
    """Score alternative exit rules against what actually happened."""
    r = float(trade.r_multiple)
    mfe = float(trade.max_favourable_r)
    mae = float(trade.max_adverse_r)
    cost_r = _cost_r(trade)
    slip = _slippage_r(trade)
    # One extra exit leg for a fraction of the position: half of the round-trip
    # cost, scaled by the fraction being closed early.
    extra_leg_r = 0.5 * cost_r * partial_fraction
    out: list[Counterfactual] = []

    # -- partial take: computable with no price path at all ----------------- #
    #
    # Taking a partial does not change where the REST of the position exits, so
    # the alternative outcome is fully determined by the actual exit R. That is
    # what makes this one honest without bars. Note it is evaluated on EVERY
    # trade that reached the level, winners included -- on a trade that ran to
    # +3R, banking half at +1R costs a full R, and leaving those out is how the
    # old version guaranteed itself a significant result.
    reached_partial = mfe >= partial_r
    if reached_partial:
        banked = partial_r - slip - extra_leg_r
        alt = partial_fraction * banked + (1 - partial_fraction) * r
        out.append(Counterfactual(
            "partial_at_1R",
            f"bank {partial_fraction:.0%} of the position at +{partial_r:g}R",
            delta_r=alt - r, applicable=True, computable=True,
            assumptions=[
                f"the partial fills {slip:.3f}R worse than the trigger level",
                f"an extra exit leg costs {extra_leg_r:.3f}R",
                "the remaining position exits exactly where the trade actually did, "
                "which is true of a scale-out and not of a stop change"]))
    else:
        out.append(Counterfactual(
            "partial_at_1R", f"bank {partial_fraction:.0%} at +{partial_r:g}R",
            delta_r=0.0, applicable=False, computable=True,
            reason=f"the trade never reached +{partial_r:g}R (best was {mfe:+.2f}R), so "
                   "the rule would not have fired"))

    # -- everything below needs the bars ------------------------------------ #
    if not path:
        for name, desc in (
            ("breakeven_at_1R", f"move the stop to entry once +{breakeven_r:g}R is reached"),
            ("trail_tighter", "a tighter trailing stop after the favourable excursion"),
            ("horizon_exit", "exit at the intended horizon instead of holding on"),
        ):
            out.append(Counterfactual(
                name, desc, delta_r=0.0, applicable=False, computable=False,
                reason="needs the price path inside the trade. MAE and MFE record how far "
                       "the position went, not WHEN, and a stop rule is entirely a "
                       "question of order."))
        out.append(Counterfactual(
            "wider_stop_1.5x", "a stop 50% wider, with the risk budget held constant",
            delta_r=0.0, applicable=False, computable=False,
            reason="unanswerable from any data this system holds. The trade ended at the "
                   "stop; what the price did AFTERWARDS was never recorded, because the "
                   "position was closed. Scoring it from the pre-exit MFE, as this used "
                   "to, assumes the trade recovered -- which is the conclusion, not the "
                   "evidence."))
        return out

    # -- break-even move ----------------------------------------------------- #
    trigger = _first_touch(path, breakeven_r)
    if trigger is None:
        out.append(Counterfactual(
            "breakeven_at_1R", f"move the stop to entry once +{breakeven_r:g}R is reached",
            delta_r=0.0, applicable=False, computable=True,
            reason=f"the price never reached +{breakeven_r:g}R, so the rule never armed"))
    else:
        # The break-even stop sits at entry plus the round-trip cost, matching
        # risk/protect.py: a stop at the bare entry price realises the
        # commission as a small loss.
        be_level = cost_r
        hit = None
        for i in range(trigger, len(path)):
            if path[i].low_r <= be_level:
                hit = i
                break
        if hit is None:
            out.append(Counterfactual(
                "breakeven_at_1R", "move the stop to entry once +1R is reached",
                delta_r=0.0, applicable=True, computable=True,
                reason="the stop was never touched after arming; the outcome is unchanged",
                assumptions=["the break-even stop sits at entry + round-trip cost"]))
        else:
            alt = _fill_at(path[hit], be_level, favourable=False, slip_r=slip)
            out.append(Counterfactual(
                "breakeven_at_1R", "move the stop to entry once +1R is reached",
                delta_r=alt - r, applicable=True, computable=True,
                assumptions=[
                    "the break-even stop sits at entry + round-trip cost",
                    f"it fills {slip:.3f}R worse than its level, or at the bar open on a gap",
                    "a bar containing both the stop and a better price is resolved "
                    "against the position"]))

    # -- tighter trail ------------------------------------------------------- #
    # Modelled as a give-back ratchet: once armed, exit if the position retraces
    # a fixed fraction of its peak. That is path-dependent in exactly the way an
    # ATR trail is, without needing the ATR series to be reconstructed.
    arm_r, keep = 1.0, 0.5
    peak = -1e9
    exit_i: int | None = None
    exit_level = 0.0
    for i, pt in enumerate(path):
        if peak >= arm_r:
            floor = peak * keep
            if pt.low_r <= floor:
                exit_i, exit_level = i, floor
                break
        peak = max(peak, pt.high_r)
    if exit_i is None:
        out.append(Counterfactual(
            "trail_tighter", f"give back at most {1 - keep:.0%} of the peak once armed",
            delta_r=0.0, applicable=(peak >= arm_r), computable=True,
            reason=("never armed: the position did not reach the arming level"
                    if peak < arm_r else "armed but never retraced to the floor")))
    else:
        alt = _fill_at(path[exit_i], exit_level, favourable=False, slip_r=slip)
        out.append(Counterfactual(
            "trail_tighter", f"give back at most {1 - keep:.0%} of the peak once armed",
            delta_r=alt - r, applicable=True, computable=True,
            assumptions=[f"armed at +{arm_r:g}R, floor at {keep:.0%} of the running peak",
                         f"fills {slip:.3f}R worse than the floor, or at the open on a gap",
                         "the peak is measured bar by bar, so an intrabar spike that "
                         "reversed within the same bar can arm a ratchet the live "
                         "system would also have armed"]))

    # -- wider stop: still unanswerable, even with the path ------------------ #
    out.append(Counterfactual(
        "wider_stop_1.5x", "a stop 50% wider, with the risk budget held constant",
        delta_r=0.0, applicable=False, computable=False,
        reason="the recorded path ENDS at the actual exit. A wider stop changes the "
               "exit, so scoring it needs prices from after the position was closed, "
               "which were never stored. Supply post-exit bars and this becomes "
               "answerable; until then it is a guess with a number attached."))
    return out


def autopsy(trade: ClosedTrade, *,
            time_stop_reasons: Sequence[str] = TIME_STOP_REASONS,
            cost_share_threshold: float = 0.35,
            path: Sequence[PathPoint] | None = None) -> TradeAutopsy:
    r = float(trade.r_multiple)
    mae = float(trade.max_adverse_r)
    mfe = float(trade.max_favourable_r)
    tags: list[str] = []
    risk = float(trade.initial_risk) if trade.initial_risk else 0.0
    cost = float(trade.commission) + abs(float(trade.financing))
    gross_r = r + (cost / risk if risk > 0 else 0.0)
    kind = exit_kind(trade.exit_reason, time_stop_reasons=time_stop_reasons)

    capture = (r / mfe) if mfe > 0 else (1.0 if r >= 0 else 0.0)

    if kind == "stop_loss" and r < -1.15:
        mode = "gap_loss"
        tags.append("slippage_or_gap")
    elif kind == "stop_loss" and mfe >= 1.0 and abs(r) <= 0.15:
        # Specific case of the give-back: the break-even stop did its job and
        # the trade is flat. Distinguished from the general give-back because
        # the lever is different -- and, as MODE_ACTIONS says, it is not a lever
        # the agent is allowed to pull.
        mode = "stopped_at_breakeven"
        tags += ["exit_discipline", "breakeven_fired"]
    elif kind in ("stop_loss", "trail_stop") and mfe >= 1.0 and r <= 0.15:
        mode = "gave_back_open_profit"
        tags += ["exit_discipline", "consider_breakeven_move"]
    elif kind == "stop_loss" and mae <= -0.95 and mfe >= 0.5:
        mode = "stopped_then_reversed"
        tags += ["stop_placement", "consider_wider_stop"]
    elif kind == "stop_loss" and trade.news_context and mae <= -0.8:
        mode = "news_shock_loss"
        tags += ["news_adjacent", "consider_wider_news_window"]
    elif kind == "stop_loss":
        mode = "clean_loss"
    elif kind == "time_stop" and mfe >= 1.0 and r < mfe - 0.5:
        # The horizon, not the market, ended this. The distinction from
        # slow_bleed matters: one says the horizon is too SHORT for the edge,
        # the other says there was no edge to wait for.
        mode = "horizon_cut_a_winner"
        tags.append("horizon_too_short")
    elif kind == "time_stop" and abs(r) < 0.3:
        mode = "slow_bleed"
        tags.append("horizon_mismatch")
    elif kind == "weekend_flat":
        mode = "weekend_flat_exit"
        tags.append("weekend_flat")
    elif r > 0 and gross_r > 0 and risk > 0 and \
            (cost / risk) / max(1e-9, gross_r) > cost_share_threshold:
        mode = "cost_dominated"
        tags.append("cost_pressure")
    elif r <= 0 < gross_r:
        mode = "cost_dominated"
        tags.append("cost_pressure")
    elif r > 0 and mae > -0.45:
        mode = "clean_win"
    elif r > 0:
        mode = "clean_win"
        tags.append("survived_deep_drawdown")
    elif mfe >= 0.75 and r <= 0:
        mode = "target_too_far"
        tags.append("consider_partial_take")
    elif r <= -0.3:
        # A material loss that reached neither the stop nor a favourable
        # excursion: the thesis simply did not develop.
        mode = "clean_loss"
        tags.append("thesis_did_not_develop")
    else:
        mode = "scratch"

    if float(trade.entry_slippage_pips) > 1.0:
        tags.append("entry_slippage")
    if trade.regime:
        tags.append(f"regime:{trade.regime}")
    if trade.news_context and "news_adjacent" not in tags:
        tags.append("news_adjacent")

    cfs = counterfactuals(trade, path=path)

    direction = "long" if trade.side is Side.BUY else "short"
    narrative = (
        f"{trade.instrument} {direction} closed {r:+.2f}R via {trade.exit_reason or '?'} "
        f"({kind}). Worst excursion {mae:+.2f}R, best {mfe:+.2f}R, capture "
        f"{capture * 100:.0f}%. {MODES.get(mode, mode)}. "
        f"Lever: {MODE_ACTIONS.get(mode, 'unclassified')}"
    )
    return TradeAutopsy(
        trade_id=trade.trade_id, strategy=trade.strategy, instrument=trade.instrument,
        outcome="win" if r > 0.05 else ("loss" if r < -0.05 else "scratch"),
        mode=mode, r_multiple=r, mae_r=mae, mfe_r=mfe, capture_ratio=capture,
        tags=tags, counterfactuals=cfs, narrative=narrative,
        regime=trade.regime or "", closed_ns=int(trade.closed_ns),
        had_path=bool(path),
    )


@dataclass
class PatternFinding:
    pattern: str
    n: int
    share: float
    mean_r: float
    mean_delta_r: float
    t_stat: float
    p_value: float
    recommendation: str
    evidence: dict[str, Any] = field(default_factory=dict)
    # Set by `aggregate` once the whole family of tests is known. The raw
    # p-value is not usable on its own: twenty patterns tested at 0.05 produce
    # a winner by chance every time, and the proposal engine downstream treats
    # a p-value as if it were the probability of being wrong.
    p_value_adjusted: float = 1.0
    significant: bool = False
    n_tests_in_family: int = 0
    # Why this pattern looks the way it does. See `diagnose`. Defaults to the
    # empty string -- "nobody has diagnosed this" -- which is a different fact
    # from "diagnosed as insufficient". `aggregate` always sets one of the four
    # real values; a finding constructed by hand carries none, and the proposal
    # engine's other gates (sample size, alpha, and above all the risk-direction
    # guard) still apply to it.
    diagnosis: str = ""
    diagnosis_note: str = ""
    stability: float = 0.0
    regime_concentration: float = 0.0
    dominant_regime: str = ""

    def to_dict(self) -> dict:
        return {"pattern": self.pattern, "n": self.n, "share": round(self.share, 3),
                "mean_r": round(self.mean_r, 3), "mean_delta_r": round(self.mean_delta_r, 3),
                "t_stat": round(self.t_stat, 3), "p_value": round(self.p_value, 5),
                "p_value_adjusted": round(self.p_value_adjusted, 5),
                "significant": self.significant,
                "n_tests_in_family": self.n_tests_in_family,
                "diagnosis": self.diagnosis, "diagnosis_note": self.diagnosis_note,
                "stability": round(self.stability, 3),
                "regime_concentration": round(self.regime_concentration, 3),
                "dominant_regime": self.dominant_regime,
                "recommendation": self.recommendation, "evidence": self.evidence}


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def _stability(values: Sequence[float], order: Sequence[int]) -> float:
    """How much of the effect survives being split in half, chronologically.

    Returns the ratio of the smaller half-mean to the larger when both halves
    agree in sign, and a negative number when they do not. An effect that exists
    only in the first half of the sample is a description of a market that has
    since changed; acting on it tunes the system to a world that is gone.
    """
    if len(values) < 8:
        return 0.0
    idx = np.argsort(np.asarray(order))
    arr = np.asarray(values, dtype=float)[idx]
    mid = arr.size // 2
    a, b = float(arr[:mid].mean()), float(arr[mid:].mean())
    if a == 0 and b == 0:
        return 0.0
    if (a > 0) != (b > 0):
        return -min(abs(a), abs(b)) / max(abs(a), abs(b), 1e-9)
    return min(abs(a), abs(b)) / max(abs(a), abs(b), 1e-9)


def _regime_concentration(values: Sequence[float], regimes: Sequence[str]
                          ) -> tuple:
    """Share of the total positive effect contributed by a single regime.

    A counterfactual worth +0.4R on average, where every one of those R comes
    from the twelve stress-regime trades in the sample, is not a statement about
    the parameter. It is a statement about stress, and the correct response is a
    regime-scoped lesson, not a global parameter change that will be wrong for
    the other eighty-eight trades.
    """
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return 0.0, ""
    labels = list(regimes) + [""] * max(0, arr.size - len(regimes))
    total = float(np.sum(np.abs(arr)))
    if total <= 0:
        return 0.0, ""
    by: dict[str, float] = defaultdict(float)
    for v, lab in zip(arr, labels[:arr.size], strict=False):
        by[lab or "unknown"] += abs(float(v))
    top = max(by.items(), key=lambda kv: kv[1])
    return top[1] / total, top[0]


def diagnose(finding: PatternFinding, *, min_sample: int,
             stability_floor: float = 0.25,
             regime_ceiling: float = 0.6) -> tuple:
    """Distinguish a wrong parameter from a changed regime from bad luck.

    Ordered by how common the explanation actually is, which is the reverse of
    how tempting it is:

    ``luck``
        The default, and the right answer most of the time. A pattern that does
        not survive correction for the number of patterns tested is noise. This
        is the explanation the loop must reach for first, because it is the one
        a run of losses feels least like.

    ``regime``
        Real, but confined. The effect lives in one market state; a global
        parameter change would apply it everywhere else too. The right output is
        a regime-scoped lesson, which this system CAN represent -- lessons carry
        a regime and are recalled only in it.

    ``parameter``
        Significant after correction, present in both halves of the sample, and
        not confined to one regime. Only this justifies proposing a change.

    ``insufficient``
        Not enough observations to say anything. Distinct from ``luck``: one is
        "we looked and found nothing", the other is "we did not look".
    """
    if finding.n < min_sample:
        return "insufficient", (
            f"{finding.n} observations against a minimum of {min_sample}; nothing is "
            "being claimed either way")
    if not finding.significant:
        return "luck", (
            f"p={finding.p_value:.4f} raw, {finding.p_value_adjusted:.4f} after "
            f"correcting for {finding.n_tests_in_family} tests in this family. The "
            "most likely explanation for a pattern this size is chance, and the most "
            "expensive mistake available here is to act on it.")
    if finding.regime_concentration > regime_ceiling and finding.dominant_regime not in (
            "", "unknown"):
        return "regime", (
            f"{finding.regime_concentration * 100:.0f}% of the effect comes from the "
            f"{finding.dominant_regime!r} regime. This is a statement about that market "
            "state, not about the parameter; a global change would be applied to every "
            "other state too. Scope it as a regime lesson instead.")
    if finding.stability < stability_floor:
        return "regime", (
            f"the effect does not survive a chronological split (stability "
            f"{finding.stability:+.2f}): it is present in one half of the sample and not "
            "the other. That is the signature of a market that changed, and tuning to it "
            "fits the half that is already over.")
    return "parameter", (
        f"significant after correcting for {finding.n_tests_in_family} tests "
        f"(adjusted p={finding.p_value_adjusted:.4f}), stable across a chronological "
        f"split ({finding.stability:.2f}), and not confined to one regime "
        f"({finding.regime_concentration * 100:.0f}% concentration)")


#: Tags assigned FROM a trade's own outcome rather than from a condition that
#: existed at entry. Testing one of these against "every other trade" is a
#: tautology -- the tag selects on the very quantity being compared -- so they
#: are reported descriptively and kept out of the tested family, where they
#: would also inflate the Benjamini-Hochberg threshold for everything else.
OUTCOME_DERIVED_TAGS = frozenset({
    "consider_wider_stop", "stop_placement",
    "exit_discipline", "consider_breakeven_move", "breakeven_fired",
    "survived_deep_drawdown",
    # These two are defined by a THRESHOLD on the outcome -- `slippage_or_gap`
    # is attached precisely when r < -1.15 -- so comparing their mean R to
    # everything else is arithmetic, not discovery. They never reached a
    # proposal, but they were being reported with diagnosis="parameter", and
    # an operator reading the lessons page would reasonably take that as a
    # finding about a setting they should change.
    "slippage_or_gap", "horizon_too_short",
})


def aggregate(autopsies: Sequence[TradeAutopsy], *, min_sample: int = 25,
              alpha: float = 0.01, fdr_alpha: float | None = None
              ) -> list[PatternFinding]:
    """Turn many autopsies into statistically-supported findings.

    A finding needs four things now, not three: enough observations, a positive
    mean effect, a p-value that survives the declared alpha, AND a p-value that
    survives correction for every other pattern tested in the same pass.

    The fourth is not pedantry. This function tests one hypothesis per
    counterfactual and one per tag -- routinely fifteen to twenty-five of them
    on the same body of trades. At alpha 0.05 that produces a "significant"
    finding by chance on almost every run, and the proposal engine downstream
    cannot tell it from a real one. Benjamini-Hochberg controls the false
    discovery rate across the family, and the family is defined as everything
    tested in this call.

    Mode counts are reported but carry no test: they are descriptive, so giving
    them a p-value of 1.0 and letting them into the correction would dilute the
    correction with hypotheses nobody is testing.
    """
    from scipy import stats as sps

    from ..research.stats import benjamini_hochberg

    if not autopsies:
        return []
    total = len(autopsies)
    findings: list[PatternFinding] = []
    tested: list[PatternFinding] = []

    by_mode = Counter(a.mode for a in autopsies)
    for mode, n in by_mode.items():
        subset = [a for a in autopsies if a.mode == mode]
        rs = np.array([a.r_multiple for a in subset])
        findings.append(PatternFinding(
            pattern=f"mode:{mode}", n=n, share=n / total, mean_r=float(rs.mean()),
            mean_delta_r=0.0, t_stat=0.0, p_value=1.0,
            recommendation=MODES.get(mode, mode),
            diagnosis="descriptive",
            diagnosis_note=f"lever: {MODE_ACTIONS.get(mode, 'unclassified')}",
            evidence={"description": MODES.get(mode, mode),
                      "lever": MODE_ACTIONS.get(mode, "unclassified")}))

    # Only counterfactuals that both FIRED and could be COMPUTED enter a
    # statistic. Including the ones that did not fire would dilute the mean
    # toward zero; including the ones that could not be computed would be
    # inventing data. The two exclusions pull in opposite directions and both
    # are necessary.
    cf_deltas: dict[str, list[float]] = defaultdict(list)
    cf_order: dict[str, list[int]] = defaultdict(list)
    cf_regimes: dict[str, list[str]] = defaultdict(list)
    cf_skipped: dict[str, int] = defaultdict(int)
    for a in autopsies:
        for cf in a.counterfactuals:
            if not cf.computable:
                cf_skipped[cf.name] += 1
                continue
            if not cf.applicable:
                continue
            cf_deltas[cf.name].append(cf.delta_r)
            cf_order[cf.name].append(a.closed_ns)
            cf_regimes[cf.name].append(a.regime)

    for name, deltas in cf_deltas.items():
        arr = np.array(deltas, dtype=float)
        n = arr.size
        if n < min_sample:
            continue
        sd = arr.std(ddof=1) if n > 1 else 0.0
        if sd > 0:
            t = float(arr.mean() / (sd / np.sqrt(n)))
            # One-sided: the only interesting alternative is that the rule
            # HELPS. `sf` rather than `1 - cdf` because the latter loses every
            # digit of a small tail, and returns 0.5 rather than 1.0 for a
            # degenerate sample.
            p = float(sps.t.sf(t, n - 1))
            if not np.isfinite(p):
                t, p = 0.0, 1.0
        else:
            t, p = 0.0, 1.0
        f = PatternFinding(
            pattern=f"counterfactual:{name}", n=n, share=n / total,
            mean_r=float(np.mean([a.r_multiple for a in autopsies])),
            mean_delta_r=float(arr.mean()), t_stat=t, p_value=p,
            recommendation="",
            evidence={"n": n, "mean_delta_r": round(float(arr.mean()), 4),
                      "std": round(float(sd), 4),
                      "n_not_computable": cf_skipped.get(name, 0)})
        f.stability = _stability(deltas, cf_order[name])
        f.regime_concentration, f.dominant_regime = _regime_concentration(
            deltas, cf_regimes[name])
        tested.append(f)

    tag_counts = Counter(t for a in autopsies for t in a.tags)
    for tag, n in tag_counts.items():
        if n < min_sample:
            continue
        if tag in OUTCOME_DERIVED_TAGS:
            # These tags are assigned FROM the trade's own outcome:
            # `consider_wider_stop` is attached precisely when a trade was
            # stopped out near its worst excursion, `survived_deep_drawdown`
            # precisely when a trade won from a deep drawdown. Testing their
            # mean R against everything else is testing whether losers lose.
            # It cannot fail, and on 400 pure random walks it produced five
            # "actionable" findings at p < 1e-10 every single time.
            #
            # Worse than the false findings themselves: Benjamini-Hochberg is a
            # step-up procedure, so guaranteed rejections at the low ranks
            # raise the threshold every OTHER hypothesis is judged against.
            # Five tautologies in a family of twelve tripled the chance of a
            # false discovery among the seven genuine nulls, from 5% to 16%.
            # They are descriptive; they are reported, and not tested.
            subset = [a for a in autopsies if tag in a.tags]
            rs = np.array([a.r_multiple for a in subset])
            findings.append(PatternFinding(
                pattern=f"tag:{tag}", n=n, share=n / total,
                mean_r=float(rs.mean()), mean_delta_r=0.0, t_stat=0.0,
                p_value=1.0, diagnosis="descriptive",
                recommendation=(
                    f"{n} trades tagged '{tag}', averaging {rs.mean():+.2f}R. "
                    "This tag is assigned from the trade's own outcome, so it "
                    "describes what happened and cannot be evidence about a "
                    "parameter."),
                evidence={"n_tagged": int(rs.size), "outcome_derived": True}))
            continue
        subset = [a for a in autopsies if tag in a.tags]
        rs = np.array([a.r_multiple for a in subset])
        others = np.array([a.r_multiple for a in autopsies if tag not in a.tags])
        if others.size < 10 or rs.size < 10:
            continue
        t, p = sps.ttest_ind(rs, others, equal_var=False)
        # A degenerate comparison (zero variance on either side) returns NaN.
        # NaN is not "no evidence" to a comparison operator: `nan >= alpha` is
        # False, so an unguarded NaN sails through every significance gate
        # downstream and arrives as a finding with no p-value at all.
        t = float(t) if np.isfinite(t) else 0.0
        p = float(p) if np.isfinite(p) else 1.0
        f = PatternFinding(
            pattern=f"tag:{tag}", n=n, share=n / total, mean_r=float(rs.mean()),
            mean_delta_r=float(rs.mean() - others.mean()), t_stat=t, p_value=p,
            recommendation=(f"trades tagged '{tag}' average {rs.mean():+.2f}R against "
                            f"{others.mean():+.2f}R elsewhere"),
            evidence={"n_tagged": int(rs.size), "n_other": int(others.size)})
        f.stability = _stability([a.r_multiple for a in subset],
                                 [a.closed_ns for a in subset])
        f.regime_concentration, f.dominant_regime = _regime_concentration(
            [a.r_multiple for a in subset], [a.regime for a in subset])
        tested.append(f)

    # One correction across the whole family tested in this pass.
    #
    # Note the p-values entering it are of two kinds: counterfactual tests are
    # ONE-sided (a rule is only interesting if it would have helped) and the
    # remaining tag tests are two-sided. Benjamini-Hochberg does not require
    # them to be the same kind -- it operates on the p-values themselves -- but
    # mixing them means a one-sided p of 0.03 and a two-sided p of 0.03 are
    # treated as equally strong evidence when they are not. The one-sided
    # tests are the ones with a directional hypothesis stated in advance,
    # which is what justifies the halving, so they are left as they are and
    # the asymmetry is recorded here rather than hidden.
    #
    # Benjamini-Hochberg controls the false discovery rate under independence
    # or positive dependence. The tests here are NOT independent -- they share
    # trades, and a tag like `regime:stress` overlaps heavily with a
    # counterfactual that only fires in stress. Strictly, arbitrary dependence
    # calls for Benjamini-Yekutieli, which divides the threshold by the harmonic
    # number and is roughly three times stricter at this family size. BH is used
    # because it was measured on this exact screen shape: over null data, a
    # 16-hypothesis pass turns up at least one raw p < 0.05 about 58% of the
    # time and at least one BH-significant finding about 4% of the time. The
    # residual is small and it errs toward declaring things real, so the
    # diagnosis step downstream -- which also demands stability across a
    # chronological split -- is what carries the rest.
    fdr = fdr_alpha if fdr_alpha is not None else alpha
    if tested:
        raw = [f.p_value for f in tested]
        passed = benjamini_hochberg(raw, alpha=fdr)
        m = len(raw)
        order = np.argsort(raw)
        ranks = np.empty(m, dtype=int)
        ranks[order] = np.arange(1, m + 1)
        for f, keep, rank in zip(tested, passed, ranks, strict=False):
            # The BH-adjusted p-value (p * m / rank), made monotone by taking the
            # running minimum from the largest rank down -- otherwise a lucky
            # middle rank can report an adjusted p below a stricter neighbour's.
            f.n_tests_in_family = m
            f.p_value_adjusted = min(1.0, f.p_value * m / max(1, int(rank)))
            f.significant = bool(keep)
        adj = sorted(tested, key=lambda x: -x.p_value)
        running = 1.0
        for f in adj:
            running = min(running, f.p_value_adjusted)
            f.p_value_adjusted = running

    for f in tested:
        f.diagnosis, f.diagnosis_note = diagnose(f, min_sample=min_sample)
        if f.pattern.startswith("counterfactual:"):
            verb = ("worth proposing" if f.diagnosis == "parameter"
                    else f"not actionable ({f.diagnosis})")
            f.recommendation = (
                f"{verb}: mean {f.mean_delta_r:+.2f}R over {f.n} trades "
                f"(p={f.p_value:.4f}, adjusted {f.p_value_adjusted:.4f}). "
                f"{f.diagnosis_note}")
            f.evidence["supported"] = f.diagnosis == "parameter"
            f.evidence["diagnosis"] = f.diagnosis
        findings.append(f)

    return sorted(findings, key=lambda f: (f.p_value_adjusted, -abs(f.mean_delta_r)))
