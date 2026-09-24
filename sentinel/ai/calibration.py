"""Is a model's probability worth anything? Measured, not assumed.

A vendor saying its probabilities are "calibrated" is a claim about ITS data.
What matters here is whether, on the headlines this system actually reads,
"0.8" happens about 80% of the time -- and, separately, whether the model
tells the cases apart at all. Those are different questions:

* **Calibration** (reliability, Brier score): do the numbers mean what they
  say? A model that answers 0.5 to every coin flip is perfectly calibrated.
* **Discrimination** (AUC, Brier SKILL against the base rate): does the model
  know anything the base rate does not? The coin-flip model scores exactly 0
  skill and an AUC of 0.5. Calibration without discrimination is a
  well-behaved way of knowing nothing, and it is the failure a "calibrated"
  label hides.

Everything here is pure and small-sample honest: a statistic that cannot be
computed (no positives, no negatives) is ``None``, never a flattering zero.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

Pair = Tuple[float, bool]          # (predicted probability, what was true)


def brier(pairs: Sequence[Pair]) -> Optional[float]:
    if not pairs:
        return None
    return sum((p - (1.0 if y else 0.0)) ** 2 for p, y in pairs) / len(pairs)


def base_rate_brier(pairs: Sequence[Pair]) -> Optional[float]:
    """The Brier score of always answering the observed base rate."""
    if not pairs:
        return None
    rate = sum(1 for _, y in pairs if y) / len(pairs)
    return rate * (1.0 - rate)


def skill(pairs: Sequence[Pair]) -> Optional[float]:
    """1 - Brier / base-rate Brier. >0 beats the base rate; None if undefined."""
    b, ref = brier(pairs), base_rate_brier(pairs)
    if b is None or ref is None or ref <= 0.0:
        return None
    return 1.0 - b / ref


def auc(pairs: Sequence[Pair]) -> Optional[float]:
    """Probability a true case scores above a false one (ties count half)."""
    pos = [p for p, y in pairs if y]
    neg = [p for p, y in pairs if not y]
    if not pos or not neg:
        return None
    wins = 0.0
    for a in pos:
        for b in neg:
            wins += 1.0 if a > b else 0.5 if a == b else 0.0
    return wins / (len(pos) * len(neg))


def reliability(pairs: Sequence[Pair], bins: int = 5) -> List[Dict[str, Any]]:
    """Predicted vs observed frequency per probability band."""
    out: List[Dict[str, Any]] = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        inside = [(p, y) for p, y in pairs
                  if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        out.append({
            "lo": round(lo, 2), "hi": round(hi, 2), "n": len(inside),
            "mean_p": round(sum(p for p, _ in inside) / len(inside), 3) if inside else None,
            "observed": round(sum(1 for _, y in inside if y) / len(inside), 3)
            if inside else None,
        })
    return out


def at_threshold(pairs: Sequence[Pair], threshold: float) -> Dict[str, Any]:
    """What the decision rule (p >= threshold) actually did."""
    tp = sum(1 for p, y in pairs if p >= threshold and y)
    fp = sum(1 for p, y in pairs if p >= threshold and not y)
    fn = sum(1 for p, y in pairs if p < threshold and y)
    tn = sum(1 for p, y in pairs if p < threshold and not y)
    n = len(pairs)
    return {"threshold": threshold, "n": n, "true_positive": tp, "false_positive": fp,
            "false_negative": fn, "true_negative": tn,
            "accuracy": round((tp + tn) / n, 3) if n else None}


def summarise(pairs: Sequence[Pair], threshold: float) -> Dict[str, Any]:
    b, s, a = brier(pairs), skill(pairs), auc(pairs)
    return {
        "n": len(pairs),
        "positives": sum(1 for _, y in pairs if y),
        "brier": round(b, 4) if b is not None else None,
        "base_rate_brier": (round(base_rate_brier(pairs), 4)
                            if pairs else None),
        "skill": round(s, 3) if s is not None else None,
        "auc": round(a, 3) if a is not None else None,
        "reliability": reliability(pairs),
        "decision": at_threshold(pairs, threshold),
    }
