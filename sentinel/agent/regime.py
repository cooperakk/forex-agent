"""Market regime classification.

Used for three things, in descending order of confidence:

1. **Attribution.** Every trade is tagged with the regime it was taken in, so
   the post-mortem can answer "does this strategy only work in a trend?"
   without anyone having to guess.
2. **Risk modulation.** Stress regimes reduce the risk budget. This is a
   defensive use: being wrong costs opportunity, not capital.
3. **Gating.** Optional, off by default, and the most dangerous use --
   regime labels are noisy and late, and a strategy tuned per regime has
   multiplied its effective trial count without telling anyone.

Every input is causal and uses an *expanding* quantile, so the percentile of
today's volatility is computed only against volatility observed up to today.
A full-sample quantile is a look-ahead bug that survives almost every review
because the code looks identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd


class Regime(str, Enum):
    QUIET_RANGE = "quiet_range"
    TRENDING = "trending"
    VOLATILE_RANGE = "volatile_range"
    STRESS = "stress"
    UNKNOWN = "unknown"


@dataclass
class RegimeState:
    regime: Regime
    confidence: float
    vol_percentile: float
    trend_strength: float
    correlation_dispersion: float
    risk_multiplier: float
    inputs: Dict[str, float] = field(default_factory=dict)
    explanation: str = ""

    def to_dict(self) -> dict:
        return {
            "regime": self.regime.value, "confidence": round(self.confidence, 3),
            "vol_percentile": round(self.vol_percentile, 3),
            "trend_strength": round(self.trend_strength, 2),
            "correlation_dispersion": round(self.correlation_dispersion, 3),
            "risk_multiplier": round(self.risk_multiplier, 3),
            "inputs": {k: round(v, 4) for k, v in self.inputs.items()},
            "explanation": self.explanation,
        }


@dataclass
class RegimeConfig:
    vol_window: int = 60
    adx_window: int = 14
    trend_adx: float = 24.0
    range_adx: float = 18.0
    high_vol_pct: float = 0.80
    stress_vol_pct: float = 0.95
    stress_correlation: float = 0.75
    stress_risk_multiplier: float = 0.35
    volatile_risk_multiplier: float = 0.70
    min_history: int = 120


def _expanding_percentile(series: pd.Series, min_periods: int = 60) -> float:
    """Rank of the latest value within the history observed up to now."""
    s = series.dropna()
    if len(s) < min_periods:
        return 0.5
    latest = float(s.iloc[-1])
    return float((s <= latest).mean())


def classify(
    frames: Dict[str, pd.DataFrame],
    index: int,
    config: Optional[RegimeConfig] = None,
    *,
    adx_values: Optional[Dict[str, float]] = None,
    correlation_window: int = 120,
) -> RegimeState:
    cfg = config or RegimeConfig()
    if not frames:
        return RegimeState(Regime.UNKNOWN, 0.0, 0.5, 0.0, 0.0, 1.0,
                           explanation="no market data")

    vol_pcts: List[float] = []
    returns: Dict[str, np.ndarray] = {}
    for sym, df in frames.items():
        window = df.iloc[: index + 1]
        if len(window) < cfg.min_history:
            continue
        logret = np.log(window["close"].astype(float)).diff()
        vol = logret.rolling(cfg.vol_window, min_periods=cfg.vol_window // 2).std(ddof=1)
        vol_pcts.append(_expanding_percentile(vol, cfg.vol_window))
        returns[sym] = logret.tail(correlation_window).to_numpy()

    if not vol_pcts:
        return RegimeState(Regime.UNKNOWN, 0.0, 0.5, 0.0, 0.0, 1.0,
                           explanation="insufficient history")

    vol_pct = float(np.mean(vol_pcts))

    # Average absolute pairwise correlation. When everything moves together,
    # diversification has stopped working -- the signature of a stress episode
    # (BIS Bulletin 90, August 2024).
    corr_values: List[float] = []
    syms = [s for s in returns if np.isfinite(returns[s]).sum() > 30]
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            ra, rb = returns[a], returns[b]
            n = min(len(ra), len(rb))
            xa, xb = ra[-n:], rb[-n:]
            mask = np.isfinite(xa) & np.isfinite(xb)
            if mask.sum() < 30:
                continue
            sa, sb = xa[mask].std(), xb[mask].std()
            if sa <= 0 or sb <= 0:
                continue
            corr_values.append(abs(float(np.corrcoef(xa[mask], xb[mask])[0, 1])))
    avg_corr = float(np.mean(corr_values)) if corr_values else 0.0

    trend = float(np.mean(list(adx_values.values()))) if adx_values else 0.0

    if vol_pct >= cfg.stress_vol_pct and avg_corr >= cfg.stress_correlation:
        regime, mult = Regime.STRESS, cfg.stress_risk_multiplier
        expl = (f"volatility in the {vol_pct * 100:.0f}th percentile of its own history "
                f"with average cross-pair correlation {avg_corr:.2f}: positions that "
                "look diversified are one position")
        conf = min(1.0, 0.5 + 0.5 * (vol_pct - cfg.stress_vol_pct) / max(1e-6, 1 - cfg.stress_vol_pct))
    elif trend >= cfg.trend_adx and vol_pct < cfg.stress_vol_pct:
        regime, mult = Regime.TRENDING, 1.0
        expl = f"ADX {trend:.0f} with volatility at the {vol_pct * 100:.0f}th percentile"
        conf = min(1.0, (trend - cfg.trend_adx) / 20 + 0.55)
    elif vol_pct >= cfg.high_vol_pct:
        regime, mult = Regime.VOLATILE_RANGE, cfg.volatile_risk_multiplier
        expl = (f"high volatility ({vol_pct * 100:.0f}th percentile) without a trend "
                f"(ADX {trend:.0f}): wide stops, poor fills")
        conf = 0.6
    else:
        regime, mult = Regime.QUIET_RANGE, 1.0
        expl = f"volatility at the {vol_pct * 100:.0f}th percentile, ADX {trend:.0f}"
        conf = 0.6 if trend <= cfg.range_adx else 0.45

    return RegimeState(
        regime=regime, confidence=float(conf), vol_percentile=vol_pct,
        trend_strength=trend, correlation_dispersion=avg_corr, risk_multiplier=mult,
        inputs={"vol_percentile": vol_pct, "avg_abs_correlation": avg_corr,
                "mean_adx": trend, "n_pairs": float(len(corr_values))},
        explanation=expl,
    )


def regime_history(frames: Dict[str, pd.DataFrame], step: int = 20,
                   config: Optional[RegimeConfig] = None) -> pd.DataFrame:
    """Regime label over time. Used by the dashboard's regime ribbon."""
    from ..strategy.base import adx as _adx

    cfg = config or RegimeConfig()
    if not frames:
        return pd.DataFrame()
    length = min(len(df) for df in frames.values())
    # ADX is computed once per instrument, causally, then read per step --
    # recomputing it inside the loop would be O(n^2) for no benefit.
    adx_frames = {sym: _adx(df.iloc[:length], cfg.adx_window) for sym, df in frames.items()}
    rows = []
    for i in range(cfg.min_history, length, step):
        adx_now = {}
        for sym, series in adx_frames.items():
            v = series.iloc[i]
            if np.isfinite(v):
                adx_now[sym] = float(v)
        st = classify(frames, i, cfg, adx_values=adx_now or None)
        idx = next(iter(frames.values())).index[i]
        rows.append({"ts": idx, "regime": st.regime.value,
                     "vol_percentile": st.vol_percentile,
                     "avg_correlation": st.correlation_dispersion,
                     "risk_multiplier": st.risk_multiplier})
    return pd.DataFrame(rows).set_index("ts") if rows else pd.DataFrame()
