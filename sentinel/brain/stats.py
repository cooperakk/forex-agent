"""The brain's statistics. Pure functions, small-sample honest.

Every estimate that can mislead on a handful of trades comes with its
uncertainty, and a statistic that cannot be computed is ``None`` -- never a
flattering default. References are given where a method comes from a paper
rather than from common practice.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# --------------------------------------------------------------------------- #
# means with uncertainty
# --------------------------------------------------------------------------- #


def mean_ci(values: Sequence[float], level: float = 0.95) -> Tuple[Optional[float],
                                                                    Optional[float],
                                                                    Optional[float]]:
    """(mean, lower, upper) with a Student-t interval; Nones below 2 values."""
    x = np.asarray([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if x.size == 0:
        return None, None, None
    m = float(x.mean())
    if x.size < 2:
        return m, None, None
    from scipy.stats import t as student_t
    se = float(x.std(ddof=1)) / math.sqrt(x.size)
    q = float(student_t.ppf(0.5 + level / 2.0, x.size - 1))
    return m, m - q * se, m + q * se


def block_bootstrap_ci(values: Sequence[float], *, block: int = 5, n_boot: int = 2000,
                       level: float = 0.90, seed: int = 20260924
                       ) -> Tuple[Optional[float], Optional[float]]:
    """Stationary-ish block bootstrap of the mean: losses cluster in time, so
    resampling single trades understates the uncertainty (Kunsch 1989)."""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size
    if n < max(10, 2 * block):
        return None, None
    rng = np.random.default_rng(seed)
    n_blocks = int(math.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(n_boot, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_boot, -1)[:, :n]
    means = x[idx].mean(axis=1)
    lo, hi = np.quantile(means, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


# --------------------------------------------------------------------------- #
# drift: one-sided CUSUM (Page 1954)
# --------------------------------------------------------------------------- #


def cusum_down(values: Sequence[float], *, mu0: float, sigma: float, k: float = 0.5,
               h: float = 4.0) -> Dict[str, object]:
    """Detect a DROP in mean trade R below ``mu0``.

    S_t = max(0, S_{t-1} + (mu0 - r_t) / sigma - k); an alarm when S_t > h.
    With k = 0.5 and h = 4 the in-control average run length is about 170
    trades, while a one-sigma drop in mean R is caught in about eight
    (standard CUSUM tables, e.g. Montgomery, Statistical Quality Control).
    """
    sigma = max(float(sigma), 0.25)
    s, path, first_alarm = 0.0, [], None
    for i, r in enumerate(values):
        s = max(0.0, s + (mu0 - float(r)) / sigma - k)
        path.append(round(s, 3))
        if first_alarm is None and s > h:
            first_alarm = i
    return {"stat": round(s, 3), "alarm": s > h, "recovering": s <= h / 2.0,
            "first_alarm_index": first_alarm, "path": path[-60:]}


# --------------------------------------------------------------------------- #
# equity-curve filter
# --------------------------------------------------------------------------- #


def below_equity_average(values: Sequence[float], window: int) -> Optional[bool]:
    """Is the cumulative-R curve below its own moving average? None if too short.

    Evidence on equity-curve trading is mixed: it cuts drawdowns in strategies
    whose losses cluster and costs return in ones whose losses do not. That is
    why the brain's scorecard measures this layer's effect on this account,
    and why it only ever halves size rather than stopping a strategy.
    """
    x = np.asarray(values, dtype=float)
    if x.size < window:
        return None
    curve = np.cumsum(x)
    return bool(curve[-1] < curve[-window:].mean())


# --------------------------------------------------------------------------- #
# Bayesian shrinkage of a cell's mean R
# --------------------------------------------------------------------------- #


def posterior_positive(values: Sequence[float], *, prior_mean: float, prior_sd: float
                       ) -> Dict[str, float]:
    """Normal-normal posterior of the mean R and P(mean R > 0).

    The noise scale is the sample standard deviation, floored at 0.5 R so a
    lucky streak of similar results cannot make the posterior overconfident.
    """
    x = np.asarray([v for v in values if math.isfinite(v)], dtype=float)
    n = int(x.size)
    prior_var = float(prior_sd) ** 2
    noise_sd = max(float(x.std(ddof=1)) if n >= 5 else 1.0, 0.5)
    noise_var = noise_sd ** 2
    post_var = 1.0 / (1.0 / prior_var + n / noise_var)
    post_mean = post_var * (prior_mean / prior_var + (float(x.sum()) if n else 0.0) / noise_var)
    from scipy.stats import norm
    p = float(norm.cdf(post_mean / math.sqrt(post_var)))
    return {"n": n, "posterior_mean": round(post_mean, 4),
            "posterior_sd": round(math.sqrt(post_var), 4), "p_positive": round(p, 4)}


def allocation_multiplier(p_positive: float, floor: float) -> float:
    """1.0 while the cell is more likely good than bad; shrinks toward the
    floor as the evidence turns against it. Never above 1."""
    if p_positive >= 0.5:
        return 1.0
    return max(float(floor), 2.0 * float(p_positive))


# --------------------------------------------------------------------------- #
# nearest neighbours
# --------------------------------------------------------------------------- #


def standardise(reference: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    mu = np.nanmean(reference, axis=0)
    sd = np.nanstd(reference, axis=0)
    sd[~np.isfinite(sd) | (sd < 1e-9)] = 1.0
    mu[~np.isfinite(mu)] = 0.0
    return mu, sd


def nearest_outcomes(reference: np.ndarray, outcomes: np.ndarray, query: np.ndarray,
                     k: int) -> Dict[str, object]:
    """Outcomes of the k resolved signals most like ``query`` (z-scored Euclid)."""
    if reference.shape[0] < k or reference.shape[1] == 0:
        return {"n": 0}
    mu, sd = standardise(reference)
    z_ref = np.nan_to_num((reference - mu) / sd)
    z_q = np.nan_to_num((query - mu) / sd)
    d = np.sqrt(((z_ref - z_q) ** 2).sum(axis=1))
    order = np.argsort(d)[:k]
    r = outcomes[order]
    mean, lo, hi = mean_ci(r.tolist(), level=0.90)
    return {"n": int(r.size), "mean_r": mean, "ci_low": lo, "ci_high": hi,
            "win_rate": float((r > 0).mean()), "median_distance": float(np.median(d[order]))}


# --------------------------------------------------------------------------- #
# risk of ruin (for the report and the docs)
# --------------------------------------------------------------------------- #


def probability_of_drawdown(mean_r: float, sd_r: float, risk_pct: float,
                            drawdown_pct: float, trades: int, *, n_paths: int = 4000,
                            seed: int = 7) -> Optional[float]:
    """Monte Carlo probability that equity falls ``drawdown_pct`` below its peak
    within ``trades`` trades, risking ``risk_pct`` of equity per 1R."""
    if sd_r <= 0 or trades <= 0 or risk_pct <= 0:
        return None
    rng = np.random.default_rng(seed)
    r = rng.normal(mean_r, sd_r, size=(n_paths, trades))
    equity = np.cumprod(1.0 + r * risk_pct / 100.0, axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.ones((n_paths, 1)), equity], axis=1),
                                 axis=1)[:, 1:]
    dd = 1.0 - equity / peak
    return float((dd.max(axis=1) >= drawdown_pct / 100.0).mean())


def summarise_r(values: Sequence[float]) -> Dict[str, object]:
    x = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    mean, lo, hi = mean_ci(x)
    return {"n": len(x), "mean_r": None if mean is None else round(mean, 4),
            "ci_low": None if lo is None else round(lo, 4),
            "ci_high": None if hi is None else round(hi, 4),
            "sum_r": round(sum(x), 3),
            "win_rate": round(sum(1 for v in x if v > 0) / len(x), 3) if x else None}


def verdict_of(summary: Dict[str, object], *, min_n: int = 15) -> str:
    """'helped' / 'hurt' / 'unclear' / 'insufficient' for a set of SKIPPED
    outcomes: a filter HELPED when what it skipped was significantly negative."""
    n = int(summary.get("n") or 0)
    if n < min_n:
        return "insufficient"
    hi, lo = summary.get("ci_high"), summary.get("ci_low")
    if hi is not None and hi < 0:
        return "helped"
    if lo is not None and lo > 0:
        return "hurt"
    return "unclear"


def as_list(values) -> List[float]:
    return [float(v) for v in values]
