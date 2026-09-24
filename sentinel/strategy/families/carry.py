"""Carry family.

Carry is the dominant factor in FX, and a system that does not model it will
hold it by accident. That is the first reason this family exists.

The second is that carry is the clearest example in the whole library of a
return that is compensation for risk rather than a free lunch. The payoff is
"gradual gains punctuated by rare, violent losses" -- short volatility in every
respect except the name -- and a naive Sharpe flatters it precisely because the
losses are rare. Brunnermeier, Nagel and Pedersen (2008) tie the unwinds to
funding liquidity; August 2024 demonstrated it again on JPY.

So every strategy here is gated on a stress proxy, and every one of them
carries a skew failure condition. A carry strategy with an attractive Sharpe
and a skew below -1 has not found an edge; it has found the premium and the
risk it is paid for.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ...core.types import Side, Signal
from ..base import (
    Strategy, StrategyMeta, atr, realised_vol, rolling_percentile,
)
from ._common import atr_signal, finite



_VOL_QUANTILE_WINDOW = 500


def _rolling_quantile(series, q: float):
    """Trailing-window quantile, closing at the current bar.

    NOT `expanding()`. An expanding quantile is causal -- it uses no future
    data -- but it is not REPRODUCIBLE between the backtest, which prepares
    over the whole frame, and the live loop, which sees a trailing window. The
    two produced different thresholds for the same bar (3.4x apart across a
    volatility regime shift), so a strategy was validated on one gate and
    traded on another. A fixed trailing window gives the same answer in both.
    """
    return series.rolling(_VOL_QUANTILE_WINDOW,
                          min_periods=60).quantile(q)

class CarryTilt(Strategy):
    """Hold the higher-yielding side, sized down and gated on stress.

    Included because carry is the dominant factor in FX and a system that does
    not model it will accidentally hold it. Explicitly gated on a stress proxy:
    Brunnermeier, Nagel and Pedersen (2008) show carry unwinds when funding
    liquidity tightens, and August 2024 showed it again.
    """

    meta = StrategyMeta(
        name="carry_tilt", version="1.0.0", family="carry",
        timeframe="D1", horizon_bars=40,
        required_history=200, lifecycle="hypothesis",
        description="Long the higher-yielding currency, flat under stress.",
        hypothesis="The forward premium is a biased predictor and the bias is "
                   "harvestable outside stress regimes.",
        failure_conditions=[
            "Residual skew below -1 (the crash risk is the return).",
            "Drawdown in a stress episode exceeds the pre-declared ceiling.",
            "No alpha after the carry factor control -- i.e. it IS the factor.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"vol_window": 60, "vol_stress_quantile": 0.85,
                "atr_window": 20, "stop_atr": 3.0, "target_atr": 6.0,
                "min_carry_bp": 50.0}

    def _validate(self) -> None:
        p = self.params
        if not 0 < p["vol_stress_quantile"] < 1:
            raise ValueError("vol_stress_quantile must be a quantile inside (0, 1)")
        if p["min_carry_bp"] < 0:
            raise ValueError("min_carry_bp cannot be negative")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop: a sub-1R target cannot clear cost")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        vol = realised_vol(df["close"], p["vol_window"])
        return pd.DataFrame({
            "atr": atr(df, p["atr_window"]),
            "vol": vol,
            "vol_stress": _rolling_quantile(vol, p["vol_stress_quantile"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < max(p["vol_window"], p["atr_window"]) + 5:
            return None
        if "carry_bp" not in df.columns:
            return None  # no rate data: refuse to guess the sign of the carry
        carry = float(df["carry_bp"].iloc[index])
        if not np.isfinite(carry) or abs(carry) < p["min_carry_bp"]:
            return None
        f = self.features_at(instrument, df, index)
        vol_now, stress = float(f["vol"]), float(f["vol_stress"])
        if np.isfinite(vol_now) and np.isfinite(stress) and vol_now >= stress:
            return None  # stress regime: stand aside
        a = float(f["atr"])
        if not np.isfinite(a) or a <= 0:
            return None
        side = Side.BUY if carry > 0 else Side.SELL
        sign = 1 if side is Side.BUY else -1
        close = float(df["close"].iloc[index])
        return Signal(
            strategy=self.meta.name, instrument=instrument, side=side,
            strength=float(min(1.0, 0.3 + abs(carry) / 400.0)),
            stop_price=close - sign * p["stop_atr"] * a,
            target_price=close + sign * p["target_atr"] * a,
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            features={"carry_bp": carry, "atr": a},
            rationale=f"carry {carry:.0f}bp, outside the stress regime",
        )


class CarryVolatilityFiltered(Strategy):
    """Carry, but only in the quiet part of the volatility distribution.

    ``carry_tilt`` stands aside in the top decile of realised volatility. This
    version is stricter and asymmetric: it requires the CURRENT volatility to
    sit in the lower part of its own trailing distribution AND requires
    volatility not to be rising. The asymmetry is deliberate -- a carry unwind
    announces itself as rising volatility before it announces itself as a
    level, and by the time the level is extreme the position is already lost.

    Whether that is an improvement or just a later entry is an empirical
    question, and the failure conditions are written so the answer is
    falsifiable rather than rhetorical.
    """

    meta = StrategyMeta(
        name="carry_vol_filter", version="1.0.0", family="carry",
        timeframe="D1", horizon_bars=40, required_history=260,
        lifecycle="hypothesis",
        description="Carry tilt gated on volatility percentile AND volatility slope.",
        hypothesis="The carry premium is compensation for crash risk, and crashes "
                   "are preceded by rising volatility more reliably than by high "
                   "volatility. Conditioning on the slope should therefore remove "
                   "more of the left tail per unit of forgone carry than "
                   "conditioning on the level alone.",
        failure_conditions=[
            "Skew no better than the unfiltered version: the filter removed "
            "return, not risk -- the outcome that should be expected by default.",
            "The worst single drawdown is the same episode as the unfiltered "
            "version's, meaning the filter did not fire when it mattered.",
            "Volatility slope has no predictive relation to subsequent carry "
            "drawdowns on this sample (check directly, not through the P&L).",
            "No alpha after the carry factor control.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"vol_window": 60, "percentile_window": 250, "max_vol_percentile": 0.60,
                "vol_slope_window": 10, "atr_window": 20, "stop_atr": 3.0,
                "target_atr": 6.0, "min_carry_bp": 50.0}

    def _validate(self) -> None:
        p = self.params
        if not 0 < p["max_vol_percentile"] <= 1:
            raise ValueError("max_vol_percentile must lie inside (0, 1]")
        if p["min_carry_bp"] < 0:
            raise ValueError("min_carry_bp cannot be negative")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        vol = realised_vol(df["close"], p["vol_window"])
        return pd.DataFrame({
            "vol": vol,
            "vol_pct": rolling_percentile(vol, p["percentile_window"]),
            "vol_slope": vol.diff(p["vol_slope_window"]),
            "atr": atr(df, p["atr_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["percentile_window"] + p["vol_window"]:
            return None
        if "carry_bp" not in df.columns:
            return None
        carry = float(df["carry_bp"].iloc[index])
        if not np.isfinite(carry) or abs(carry) < p["min_carry_bp"]:
            return None
        f = self.features_at(instrument, df, index)
        pct, slope, a = float(f["vol_pct"]), float(f["vol_slope"]), float(f["atr"])
        if not finite(pct, slope, a) or a <= 0:
            return None
        if pct > p["max_vol_percentile"] or slope > 0:
            return None

        side = Side.BUY if carry > 0 else Side.SELL
        close = float(df["close"].iloc[index])
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.25 + abs(carry) / 500.0)),
            features={"carry_bp": carry, "vol_percentile": pct,
                      "vol_slope": slope, "atr": a},
            rationale=(f"carry {carry:.0f}bp with volatility in the {pct * 100:.0f}th "
                       f"percentile and falling"),
        )


class CarryMomentum(Strategy):
    """Take the carry only when price momentum agrees with it.

    The combination is well motivated: carry tells you which side is paid to be
    held, momentum tells you whether the market is currently willing to hold
    it. A high-carry currency that is depreciating faster than the interest
    differential is the definition of a carry trade going wrong, and the
    momentum condition exits that state early.

    The cost is obvious and should be measured, not assumed away: requiring two
    conditions to agree cuts the number of positions sharply, and a strategy
    with a third of the trades needs a proportionally larger edge per trade to
    reach the same statistical power.
    """

    meta = StrategyMeta(
        name="carry_momentum", version="1.0.0", family="carry",
        timeframe="D1", horizon_bars=35, required_history=300,
        lifecycle="hypothesis",
        description="Carry tilt confirmed by the sign of trailing price momentum.",
        hypothesis="Carry and momentum are the two factors with the strongest "
                   "documented presence in FX and their drawdowns are not "
                   "contemporaneous -- carry unwinds on funding shocks, momentum "
                   "fails in reversals. Requiring agreement should keep the "
                   "positions where both mechanisms point the same way and avoid "
                   "the ones where the carry is being paid for a reason.",
        failure_conditions=[
            "Trade count too low to reach MinTRL: agreement is rare, and an "
            "underpowered strategy cannot be accepted however good it looks.",
            "No alpha after BOTH the carry and momentum factor controls -- the "
            "combination is then a mechanical blend of two known premia.",
            "Skew still below -1: the momentum filter did not remove the crash "
            "exposure, it only delayed the entry into it.",
            "Performance is entirely attributable to one currency pair.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"momentum_window": 60, "skip": 5, "vol_window": 60,
                "vol_stress_quantile": 0.85, "atr_window": 20,
                "stop_atr": 3.0, "target_atr": 6.0, "min_carry_bp": 40.0}

    def _validate(self) -> None:
        p = self.params
        if p["skip"] >= p["momentum_window"]:
            raise ValueError("skip must be shorter than the momentum window")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        logc = np.log(df["close"].astype(float))
        mom = logc.shift(p["skip"]) - logc.shift(p["skip"] + p["momentum_window"])
        vol = realised_vol(df["close"], p["vol_window"])
        return pd.DataFrame({
            "mom": mom, "vol": vol,
            "vol_stress": _rolling_quantile(vol, p["vol_stress_quantile"]),
            "atr": atr(df, p["atr_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["momentum_window"] + p["skip"] + p["vol_window"] + 5:
            return None
        if "carry_bp" not in df.columns:
            return None
        carry = float(df["carry_bp"].iloc[index])
        if not np.isfinite(carry) or abs(carry) < p["min_carry_bp"]:
            return None
        f = self.features_at(instrument, df, index)
        mom, vol_now, stress, a = (float(f["mom"]), float(f["vol"]),
                                   float(f["vol_stress"]), float(f["atr"]))
        if not finite(mom, a) or a <= 0:
            return None
        if np.isfinite(vol_now) and np.isfinite(stress) and vol_now >= stress:
            return None

        carry_side = 1 if carry > 0 else -1
        mom_side = 1 if mom > 0 else -1
        if carry_side != mom_side:
            return None  # the market is not paying to hold the paid side

        side = Side.BUY if carry_side > 0 else Side.SELL
        close = float(df["close"].iloc[index])
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.3 + abs(carry) / 500.0)),
            features={"carry_bp": carry, "momentum": mom, "atr": a},
            rationale=(f"carry {carry:.0f}bp and {p['momentum_window']}-bar momentum "
                       f"{mom:+.4f} pointing the same way"),
        )
