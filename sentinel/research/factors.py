"""Factor attribution.

A strategy that makes money is not necessarily doing anything new. Lustig,
Roussanov and Verdelhan (2011) showed that most of the cross-section of
currency returns is spanned by two factors: a *dollar* factor (the average
return of a basket against USD) and a *carry* factor (high-yield minus
low-yield). Brunnermeier, Nagel and Pedersen (2008) showed that carry returns
are negatively skewed and crash with funding liquidity.

So before a strategy is called an edge, its returns are regressed on those
factors. If alpha is not significant after the controls, what was discovered
was a repackaged risk premium -- available more cheaply, and with the same
crash exposure, from a plain carry basket.

Skewness is reported alongside alpha because a positive mean with strong
negative skew is how "steady profits" precede August 2024.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt
from typing import Dict, List, Optional, Sequence

import numpy as np
from scipy import stats as sps

from .stats import newey_west_se


@dataclass
class FactorExposure:
    name: str
    beta: float
    t_stat: float
    p_value: float


@dataclass
class AttributionResult:
    alpha_per_period: float
    alpha_annual: float
    alpha_t: float
    alpha_p: float
    r_squared: float
    n_obs: int
    exposures: List[FactorExposure] = field(default_factory=list)
    residual_skew: float = 0.0
    residual_kurtosis: float = 3.0
    downside_beta: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    def significant(self, alpha_level: float) -> bool:
        return self.alpha_p < alpha_level and self.alpha_per_period > 0

    def to_dict(self) -> dict:
        return {
            "alpha_annual": round(self.alpha_annual, 5),
            "alpha_t": round(self.alpha_t, 3),
            "alpha_p": round(self.alpha_p, 6),
            "r_squared": round(self.r_squared, 4),
            "n_obs": self.n_obs,
            "residual_skew": round(self.residual_skew, 3),
            "residual_kurtosis": round(self.residual_kurtosis, 3),
            "downside_beta": (round(self.downside_beta, 3)
                              if self.downside_beta is not None else None),
            "exposures": [
                {"factor": e.name, "beta": round(e.beta, 4),
                 "t": round(e.t_stat, 3), "p": round(e.p_value, 6)}
                for e in self.exposures
            ],
            "notes": self.notes,
        }


def dollar_factor(returns_vs_usd: Dict[str, Sequence[float]]) -> np.ndarray:
    """Equal-weighted average return of a basket against USD."""
    if not returns_vs_usd:
        return np.array([])
    mat = np.array([np.asarray(v, dtype=float) for v in returns_vs_usd.values()])
    return mat.mean(axis=0)


def carry_factor(returns_vs_usd: Dict[str, Sequence[float]],
                 interest_differentials: Dict[str, Sequence[float]],
                 quantile: float = 0.33) -> np.ndarray:
    """High-minus-low: long top-yield currencies, short bottom-yield.

    Rebalanced every period from the *contemporaneous* rate differential, which
    is observable at the start of the period -- so this is a tradable factor,
    not a hindsight construction.
    """
    names = [n for n in returns_vs_usd if n in interest_differentials]
    if len(names) < 3:
        return np.array([])
    R = np.array([np.asarray(returns_vs_usd[n], dtype=float) for n in names])
    F = np.array([np.asarray(interest_differentials[n], dtype=float) for n in names])
    T = min(R.shape[1], F.shape[1])
    R, F = R[:, :T], F[:, :T]
    out = np.zeros(T)
    k = max(1, int(round(len(names) * quantile)))
    for t in range(T):
        order = np.argsort(F[:, t])
        low, high = order[:k], order[-k:]
        out[t] = R[high, t].mean() - R[low, t].mean()
    return out


def momentum_factor(returns_vs_usd: Dict[str, Sequence[float]],
                    lookback: int = 60, quantile: float = 0.33) -> np.ndarray:
    """Cross-sectional currency momentum (Menkhoff et al. 2011)."""
    names = list(returns_vs_usd)
    if len(names) < 3:
        return np.array([])
    R = np.array([np.asarray(returns_vs_usd[n], dtype=float) for n in names])
    T = R.shape[1]
    out = np.zeros(T)
    k = max(1, int(round(len(names) * quantile)))
    for t in range(lookback, T):
        past = R[:, t - lookback:t].sum(axis=1)
        order = np.argsort(past)
        out[t] = R[order[-k:], t].mean() - R[order[:k], t].mean()
    return out


def _ols_with_hac(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """OLS with Newey-West standard errors. Returns (beta, se, r_squared)."""
    n, p = X.shape
    XtX = X.T @ X
    try:
        XtX_inv = np.linalg.pinv(XtX)
    except np.linalg.LinAlgError:  # pragma: no cover
        return np.zeros(p), np.full(p, np.inf), 0.0
    beta = XtX_inv @ (X.T @ y)
    resid = y - X @ beta
    ss_res = float(resid @ resid)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    lags = max(1, int(np.floor(4 * (n / 100.0) ** (2.0 / 9.0))))
    S = np.zeros((p, p))
    u = X * resid[:, None]
    S += u.T @ u
    for l in range(1, lags + 1):
        w = 1.0 - l / (lags + 1.0)
        G = u[l:].T @ u[:-l]
        S += w * (G + G.T)
    cov = XtX_inv @ S @ XtX_inv
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    return beta, se, r2


def attribute(
    strategy_returns: Sequence[float],
    factors: Dict[str, Sequence[float]],
    periods_per_year: int = 252,
) -> AttributionResult:
    """Regress strategy returns on the supplied factors with HAC errors."""
    y = np.asarray(strategy_returns, dtype=float)
    notes: List[str] = []
    usable = {k: np.asarray(v, dtype=float) for k, v in factors.items()
              if len(v) > 0 and np.isfinite(np.asarray(v, dtype=float)).all()}
    dropped = set(factors) - set(usable)
    if dropped:
        notes.append(f"factors dropped for missing/invalid data: {sorted(dropped)}")
    if not usable:
        # No controls available: report the raw mean, and say so loudly.
        n = y.size
        se = newey_west_se(y)
        t = float(y.mean() / se) if np.isfinite(se) and se > 0 else 0.0
        notes.append("no factors supplied: this is a raw mean test, NOT alpha")
        return AttributionResult(
            alpha_per_period=float(y.mean()), alpha_annual=float(y.mean() * periods_per_year),
            alpha_t=t, alpha_p=float(1 - sps.norm.cdf(t)), r_squared=0.0, n_obs=n,
            residual_skew=float(sps.skew(y, bias=False)) if n > 2 else 0.0,
            residual_kurtosis=float(sps.kurtosis(y, fisher=False, bias=False)) if n > 3 else 3.0,
            notes=notes)

    T = min([y.size] + [v.size for v in usable.values()])
    if T < 30:
        notes.append(f"only {T} aligned observations: the regression is not informative")
    y = y[-T:]
    names = list(usable)
    X = np.column_stack([np.ones(T)] + [usable[n][-T:] for n in names])

    beta, se, r2 = _ols_with_hac(y, X)
    with np.errstate(divide="ignore", invalid="ignore"):
        t_stats = np.where(se > 0, beta / se, 0.0)
    dof = max(1, T - X.shape[1])
    p_two = 2 * (1 - sps.t.cdf(np.abs(t_stats), dof))

    resid = y - X @ beta
    skew = float(sps.skew(resid, bias=False)) if T > 2 else 0.0
    kurt = float(sps.kurtosis(resid, fisher=False, bias=False)) if T > 3 else 3.0
    if skew < -0.5:
        notes.append(
            f"residual skew {skew:.2f}: the payoff is 'many small wins, rare large loss'. "
            "A high Sharpe on this shape understates the risk of ruin."
        )

    downside_beta = None
    if "dollar" in usable:
        dfac = usable["dollar"][-T:]
        mask = dfac < np.quantile(dfac, 0.2)
        if mask.sum() > 10 and np.var(dfac[mask]) > 0:
            downside_beta = float(np.cov(y[mask], dfac[mask])[0, 1] / np.var(dfac[mask]))

    exposures = [FactorExposure(names[i], float(beta[i + 1]), float(t_stats[i + 1]),
                                float(p_two[i + 1])) for i in range(len(names))]
    alpha_t = float(t_stats[0])
    return AttributionResult(
        alpha_per_period=float(beta[0]), alpha_annual=float(beta[0] * periods_per_year),
        alpha_t=alpha_t,
        # One-sided: we only care about positive alpha.
        alpha_p=float(1 - sps.t.cdf(alpha_t, dof)),
        r_squared=r2, n_obs=T, exposures=exposures,
        residual_skew=skew, residual_kurtosis=kurt, downside_beta=downside_beta, notes=notes,
    )
