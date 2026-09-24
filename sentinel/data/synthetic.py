"""Synthetic market generator for tests, demos and stress runs.

Used everywhere a deterministic market is needed and NEVER as evidence about
the real one. It reproduces four stylised facts that matter for a trading
system -- volatility clustering, fat tails, weekend gaps and regime switching
between trending and ranging -- so that code paths are exercised realistically.
It does not reproduce the thing that decides profitability, which is the actual
joint distribution of returns and costs at a specific venue.

Any result produced on this data is labelled ``synthetic`` in the run registry
and can never satisfy an acceptance gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence

import numpy as np
import pandas as pd


@dataclass
class MarketSpec:
    symbol: str
    start_price: float
    annual_vol: float = 0.08
    trend_strength: float = 0.15
    carry_bp: float = 0.0
    pip: float = 0.0001


DEFAULT_UNIVERSE = [
    MarketSpec("EUR_USD", 1.0850, 0.070, 0.18, 120.0),
    MarketSpec("GBP_USD", 1.2700, 0.082, 0.20, 95.0),
    MarketSpec("AUD_USD", 0.6600, 0.095, 0.22, 45.0),
    MarketSpec("USD_JPY", 150.00, 0.088, 0.25, -280.0, pip=0.01),
    MarketSpec("USD_CHF", 0.8800, 0.068, 0.12, 180.0),
]


def generate_series(
    spec: MarketSpec,
    n_bars: int,
    bars_per_day: int = 6,
    seed: int = 0,
    regime_switch_prob: float = 0.004,
    common_shocks: Optional[np.ndarray] = None,
    factor_loading: float = 0.0,
    start: str = "2022-01-03",
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    bars_per_year = bars_per_day * 252
    base_sigma = spec.annual_vol / np.sqrt(bars_per_year)

    # GARCH(1,1) with a fat-tailed innovation. omega is set so the
    # unconditional variance equals base_sigma^2 exactly, otherwise the
    # realised volatility drifts away from the requested value.
    alpha, beta = 0.13, 0.84
    omega = base_sigma ** 2 * (1.0 - alpha - beta)
    var = base_sigma ** 2
    lam = float(np.clip(factor_loading, 0.0, 0.95))
    trending = rng.random() < 0.5
    drift = 0.0

    logret = np.zeros(n_bars)
    regimes = np.empty(n_bars, dtype=object)
    for t in range(n_bars):
        if rng.random() < regime_switch_prob:
            trending = not trending
            drift = (rng.normal(0, base_sigma * spec.trend_strength) if trending else 0.0)
        idio = rng.standard_t(df=5) / np.sqrt(5 / 3)  # unit-variance fat tail
        if common_shocks is not None and lam > 0 and t < len(common_shocks):
            # sqrt(1-lam^2) keeps total variance at 1, so the requested annual
            # volatility survives the addition of the common factor and the
            # pairwise correlation is exactly lam_i * lam_j.
            shock = np.sqrt(1.0 - lam ** 2) * idio + lam * common_shocks[t]
        else:
            shock = idio
        eps = np.sqrt(var) * shock
        mean_rev = -0.02 * logret[t - 1] if (t > 0 and not trending) else 0.0
        logret[t] = drift + mean_rev + eps
        var = omega + alpha * eps ** 2 + beta * var
        regimes[t] = "trend" if trending else "range"

    price = spec.start_price * np.exp(np.cumsum(logret))
    index = pd.date_range(start, periods=n_bars, freq=f"{24 // bars_per_day}h", tz="UTC")

    # Weekend gap: Friday close to Monday open jumps without intervening prices.
    is_monday_open = (index.dayofweek == 0) & (index.hour < 24 // bars_per_day)
    gap = np.where(is_monday_open, rng.normal(0, base_sigma * 2.2, n_bars), 0.0)
    price = price * np.exp(np.cumsum(gap))

    close = pd.Series(price, index=index)
    open_ = close.shift(1).fillna(close.iloc[0])
    intrabar = np.abs(rng.normal(0, base_sigma * 0.8, n_bars))
    high = np.maximum(open_, close) * (1 + intrabar)
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, base_sigma * 0.8, n_bars)))
    volume = rng.lognormal(9.0, 0.55, n_bars)

    return pd.DataFrame({
        "open": open_.to_numpy(), "high": high, "low": low, "close": close.to_numpy(),
        "volume": volume, "carry_bp": spec.carry_bp,
        "regime": regimes, "source": "synthetic",
    }, index=index)


def generate_universe(
    specs: Optional[Sequence[MarketSpec]] = None,
    n_bars: int = 4000,
    bars_per_day: int = 6,
    seed: int = 20260914,
    dollar_factor_strength: float = 0.45,
) -> Dict[str, pd.DataFrame]:
    """Correlated universe: a shared dollar shock plus idiosyncratic noise.

    Without the common factor every pair is independent, the portfolio limits
    never bind, and the correlation machinery is never exercised -- which would
    make the tests pass for the wrong reason.
    """
    specs = list(specs or DEFAULT_UNIVERSE)
    rng = np.random.default_rng(seed)
    common = rng.standard_normal(n_bars)
    out: Dict[str, pd.DataFrame] = {}
    for i, spec in enumerate(specs):
        # USD-base pairs load on the dollar factor with the opposite sign, which
        # is what makes "long EUR_USD + short USD_JPY" one position rather than two.
        sign = -1.0 if spec.symbol.startswith("USD_") else 1.0
        out[spec.symbol] = generate_series(
            spec, n_bars, bars_per_day, seed=seed + i * 101,
            common_shocks=sign * common, factor_loading=dollar_factor_strength)
    return out


def realised_stats(df: pd.DataFrame, bars_per_year: int = 1512) -> Dict[str, float]:
    r = np.log(df["close"]).diff().dropna()
    from scipy import stats as sps
    return {
        "annual_vol": float(r.std(ddof=1) * np.sqrt(bars_per_year)),
        "skew": float(sps.skew(r, bias=False)),
        "kurtosis": float(sps.kurtosis(r, fisher=False, bias=False)),
        "autocorr_1": float(r.autocorr(1)),
        "abs_autocorr_1": float(r.abs().autocorr(1)),
    }
