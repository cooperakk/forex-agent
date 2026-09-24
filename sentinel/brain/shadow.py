"""The shadow book: what every signal WOULD have done, taken or not.

Why this is honest where most "what if" analysis is not
-------------------------------------------------------
A counterfactual needs prices that were never traded. Here they exist: the
bars after a signal are recorded by the feed whether or not the agent acted,
so "would this vetoed BUY have hit its stop or its target first?" has a
factual answer. The rules are the backtester's own:

* the path starts at the bar AFTER the signal bar (the signal was raised on a
  completed bar; acting on it is only possible afterwards);
* if one bar touches both the stop and the target, the STOP is assumed first
  (the adverse-first convention -- optimism about intrabar order is the most
  common way a backtest lies);
* a bar that OPENS beyond the stop fills at the open, not at the stop: a gap
  is a loss larger than 1R, and pretending otherwise is how the 2015 franc
  shock surprised people;
* the round-trip cost is charged in R;
* a signal whose horizon runs out is marked at the close of its last bar.

What it is for
--------------
1. **The veto scorecard.** Every vetoed or skipped signal is an observation
   of what that rule prevented. "news_blackout skipped 22 signals averaging
   -0.31R (90% CI -0.55..-0.07)" is evidence the rule earns its keep;
   "spread skipped 40 signals averaging +0.2R" is evidence it costs money.
2. **The brain's own scorecard.** A layer that shrank a taken trade by 0.5
   saved 0.5 x |R| if the trade lost and cost 0.5 x R if it won. Summed, that
   says whether the layer helps THIS account.
3. **Training and validating the meta-label filter**, and the similar-
   situation memory: several times more observations than closed trades.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .stats import summarise_r, verdict_of

TAKEN = ("executed", "queued", "proposed")
NOT_TAKEN = ("vetoed", "skipped")

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400,
              "D1": 86400, "W1": 604800}


def shadow_key(strategy: str, instrument: str, side: str, decision_ns: int) -> str:
    return f"{strategy}|{instrument}|{side}|{int(decision_ns)}"


def resolve_path(*, side: str, entry: float, stop: float, target: Optional[float],
                 horizon: int, bars: pd.DataFrame, cost_r: float = 0.0
                 ) -> Tuple[bool, Optional[float], str, int]:
    """(resolved, R after cost, exit kind, bars used) along ``bars`` (the bars
    AFTER the signal bar, oldest first)."""
    risk = abs(entry - stop)
    if not (math.isfinite(entry) and math.isfinite(stop)) or risk <= 0:
        return True, None, "invalid", 0
    buy = side.upper() == "BUY"
    if (buy and stop >= entry) or (not buy and stop <= entry):
        return True, None, "invalid", 0
    used = 0
    for _, bar in bars.iloc[:horizon].iterrows():
        used += 1
        o, h, low, c = (float(bar["open"]), float(bar["high"]), float(bar["low"]),
                        float(bar["close"]))
        if buy:
            if o <= stop:                      # gapped through the stop
                return True, (o - entry) / risk - cost_r, "gap", used
            if low <= stop:
                return True, (stop - entry) / risk - cost_r, "stop", used
            if target is not None and h >= target:
                fill = max(target, o) if o >= target else target
                return True, (fill - entry) / risk - cost_r, "target", used
        else:
            if o >= stop:
                return True, (entry - o) / risk - cost_r, "gap", used
            if h >= stop:
                return True, (entry - stop) / risk - cost_r, "stop", used
            if target is not None and low <= target:
                fill = min(target, o) if o <= target else target
                return True, (entry - fill) / risk - cost_r, "target", used
        if used >= horizon:
            r = (c - entry) / risk if buy else (entry - c) / risk
            return True, r - cost_r, "horizon", used
    return False, None, "open", used


def scorecard(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The veto scorecard, per-strategy outcomes and the brain's layer effects."""
    by_rule: Dict[str, List[float]] = {}
    taken: List[float] = []
    by_strategy: Dict[str, Dict[str, List[float]]] = {}
    layer_effect: Dict[str, List[float]] = {}
    for r in rows:
        out = r.get("outcome_r")
        if out is None:
            continue
        out = float(out)
        strat = by_strategy.setdefault(r["strategy"], {"taken": [], "not_taken": []})
        if r["action"] in TAKEN:
            taken.append(out)
            strat["taken"].append(out)
            for name, m in (r.get("layers") or {}).items():
                try:
                    m = float(m)
                except (TypeError, ValueError):
                    continue
                if m < 1.0:
                    # Positive = the shrink saved R (the trade lost);
                    # negative = it cost R (the trade won).
                    layer_effect.setdefault(name, []).append(-(1.0 - m) * out)
        elif r["action"] in NOT_TAKEN:
            strat["not_taken"].append(out)
            by_rule.setdefault(r.get("rule") or "unknown", []).append(out)
    rules = []
    for rule, values in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        s = summarise_r(values)
        rules.append({"rule": rule, **s, "verdict": verdict_of(s)})
    layers = []
    for name, effects in sorted(layer_effect.items()):
        s = summarise_r(effects)
        s["verdict"] = ("insufficient" if s["n"] < 15 else
                        "helped" if (s["ci_low"] or 0) > 0 else
                        "hurt" if (s["ci_high"] or 0) < 0 else "unclear")
        layers.append({"layer": name, "saved_r": s["sum_r"], **s})
    return {
        "taken": summarise_r(taken),
        "rules": rules,
        "layers": layers,
        "strategies": {k: {"taken": summarise_r(v["taken"]),
                           "not_taken": summarise_r(v["not_taken"])}
                       for k, v in sorted(by_strategy.items())},
    }


def meta_live_check(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How the deployed meta-label filter's probabilities fared on live signals."""
    pairs = [(float(r["meta_p"]), float(r["outcome_r"]) > 0) for r in rows
             if r.get("meta_p") is not None and r.get("outcome_r") is not None]
    if not pairs:
        return {"n": 0}
    from ..ai import calibration as cal
    s = cal.summarise(pairs, 0.5)
    return {"n": s["n"], "brier": s["brier"], "skill": s["skill"], "auc": s["auc"]}


def feature_matrix(rows: List[Dict[str, Any]], names: Optional[List[str]] = None
                   ) -> Tuple[np.ndarray, List[str]]:
    if names is None:
        names = sorted({k for r in rows for k in (r.get("features") or {})})
    X = np.array([[float((r.get("features") or {}).get(n, np.nan)) for n in names]
                  for r in rows], dtype=float) if rows else np.zeros((0, len(names)))
    return X, names
