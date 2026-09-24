"""Volatility family.

Volatility is the one thing in this library that is genuinely predictable.
Clustering is among the most robust facts in finance: today's realised
volatility forecasts tomorrow's far better than today's return forecasts
tomorrow's return.

What that does NOT give you is a direction, and that gap is where most
volatility-based trading strategies quietly fail. The three here are therefore
honest about what they are:

* ``vol_target_trend`` is an OVERLAY, not an edge. It changes sizing, not the
  side, and its claim is only that the risk-adjusted return improves.
* ``atr_regime_filter`` is a FILTER. Its claim is that an existing entry rule
  does better in a particular part of the volatility distribution.
* ``realised_vol_ratio`` is the closest thing here to a directional volatility
  trade, and it is explicitly a PROXY for something this system cannot see.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ...core.types import Side, Signal
from ..base import (
    Strategy, StrategyMeta, adx, atr, donchian, ema, realised_vol,
    rolling_percentile,
)
from ._common import atr_signal, finite


class VolatilityTargetedTrend(Strategy):
    """A trend entry whose declared strength targets a constant volatility.

    Read the limitation before the hypothesis. In this system, position size is
    decided by ``RiskEngine.evaluate_entry`` from the risk-per-trade budget and
    the stop distance -- NOT by ``Signal.strength``. Since the stop is already
    ATR-proportional, a large part of volatility targeting is happening whether
    this strategy exists or not.

    What ``strength`` does here is expose the inverse-volatility weight so that
    a caller which passes a ``size_scaler`` (the backtester supports one, and
    the meta-labelling layer uses it) can act on it. Without such a scaler this
    strategy is the trend entry alone, and its result should be compared
    against exactly that. Claiming a vol-targeting benefit that the sizing path
    never applied would be a fiction.
    """

    meta = StrategyMeta(
        name="vol_target_trend", version="1.0.0", family="volatility",
        timeframe="H4", horizon_bars=40, required_history=260,
        lifecycle="hypothesis",
        description="Trend entry with an inverse-volatility strength for a size overlay.",
        hypothesis="Realised volatility is persistent and forecastable, while "
                   "returns are not. Scaling exposure inversely to forecast "
                   "volatility therefore produces a more stable risk profile and "
                   "a higher risk-adjusted return for the same underlying signal, "
                   "without claiming to improve the signal itself.",
        failure_conditions=[
            "Sharpe no better than the same entries at constant size -- with an "
            "ATR-proportional stop already in place, this is the likely result.",
            "Drawdown concentrated in the high-volatility periods the overlay was "
            "supposed to shrink exposure into.",
            "Volatility forecast has no out-of-sample skill on this data (test "
            "the forecast directly; do not infer it from the P&L).",
            "Turnover from size adjustment costs more than the variance saved.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"trend_window": 50, "vol_window": 40, "target_vol_annual": 0.08,
                "bars_per_year": 1512, "atr_window": 20, "stop_atr": 2.2,
                "target_atr": 4.5, "max_leverage": 2.0, "min_leverage": 0.25}

    def _validate(self) -> None:
        p = self.params
        if p["target_vol_annual"] <= 0:
            raise ValueError("the volatility target must be positive")
        if p["min_leverage"] <= 0 or p["max_leverage"] < p["min_leverage"]:
            raise ValueError("leverage bounds must be positive and ordered")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        trend = ema(df["close"], p["trend_window"])
        vol = realised_vol(df["close"], p["vol_window"]) * np.sqrt(p["bars_per_year"])
        scale = (p["target_vol_annual"] / vol.replace(0.0, np.nan)).clip(
            p["min_leverage"], p["max_leverage"])
        return pd.DataFrame({
            "trend": trend, "trend_prev": trend.shift(1),
            "close_prev": df["close"].astype(float).shift(1),
            "vol_annual": vol, "scale": scale,
            "atr": atr(df, p["atr_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["trend_window"] + p["vol_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        trend, trend_prev = float(f["trend"]), float(f["trend_prev"])
        prev_close, vol, scale, a = (float(f["close_prev"]), float(f["vol_annual"]),
                                     float(f["scale"]), float(f["atr"]))
        if not finite(trend, trend_prev, prev_close, vol, scale, a) or a <= 0:
            return None

        close = float(df["close"].iloc[index])
        # Cross of the trend line, computed from the previous bar's close
        # against the previous bar's trend value -- both known at this bar.
        if close > trend and prev_close <= trend_prev:
            side = Side.BUY
        elif close < trend and prev_close >= trend_prev:
            side = Side.SELL
        else:
            return None

        # strength IS the overlay weight, normalised into [0, 1] against the
        # leverage cap. It is not a probability and is flagged uncalibrated.
        weight = float(np.clip(scale / p["max_leverage"], 0.0, 1.0))
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=max(0.05, weight),
            features={"vol_annual": vol, "vol_scale": scale, "atr": a},
            rationale=(f"trend cross with realised volatility {vol * 100:.1f}% "
                       f"annual; overlay weight {scale:.2f}x"),
        )


class ATRPercentileRegime(Strategy):
    """Break out, but only from the middle of the volatility distribution.

    Both tails are excluded for different reasons. In the bottom decile the
    average move is smaller than the round-trip cost, so even a correct
    direction loses. In the top decile the spread widens, slippage multiplies
    and the move being joined is usually finishing rather than starting.

    The percentile is trailing, which is the whole trick: an absolute ATR
    threshold means something different on every instrument and in every year,
    and tuning one per instrument is a fast route to a fitted result.
    """

    meta = StrategyMeta(
        name="atr_regime_filter", version="1.0.0", family="volatility",
        timeframe="H1", horizon_bars=24, required_history=300,
        lifecycle="hypothesis",
        description="Channel break restricted to a middle band of the trailing "
                    "ATR percentile distribution.",
        hypothesis="Signal quality is not uniform across volatility regimes. Too "
                   "little volatility and the move cannot clear the spread; too "
                   "much and the entry is a late join of a completed move at the "
                   "worst available price. If that is right, restricting an "
                   "otherwise unchanged entry rule to the middle band should "
                   "raise expectancy per trade.",
        failure_conditions=[
            "Expectancy per trade no better than the unfiltered entry: the "
            "regime story is wrong, or the bands are in the wrong place.",
            "The chosen band edges cannot be moved by +/-0.1 without changing "
            "the conclusion -- that is fitting, not a regime.",
            "The filter's benefit disappears out of sample in CPCV, which is "
            "the expected outcome for any threshold chosen on the same data.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"atr_window": 14, "percentile_window": 250,
                "min_percentile": 0.35, "max_percentile": 0.85,
                "channel": 30, "stop_atr": 1.8, "target_atr": 4.0,
                "adx_window": 14, "adx_min": 18.0}

    def _validate(self) -> None:
        p = self.params
        if not 0 <= p["min_percentile"] < p["max_percentile"] <= 1:
            raise ValueError("percentile band must be ordered inside [0, 1]")
        if p["channel"] < 10:
            raise ValueError("channel below 10 bars is noise, not a breakout")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        a = atr(df, p["atr_window"])
        upper, lower = donchian(df, p["channel"])
        return pd.DataFrame({
            "atr": a, "atr_pct": rolling_percentile(a, p["percentile_window"]),
            "upper": upper, "lower": lower, "adx": adx(df, p["adx_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["percentile_window"] + p["atr_window"]:
            return None
        f = self.features_at(instrument, df, index)
        a, pct = float(f["atr"]), float(f["atr_pct"])
        up, lo, dx = float(f["upper"]), float(f["lower"]), float(f["adx"])
        if not finite(a, pct, up, lo, dx) or a <= 0:
            return None
        if not (p["min_percentile"] <= pct <= p["max_percentile"]):
            return None
        if dx < p["adx_min"]:
            return None

        row = df.iloc[index]
        close = float(row["close"])
        if float(row["high"]) > up and close > up:
            side = Side.BUY
        elif float(row["low"]) < lo and close < lo:
            side = Side.SELL
        else:
            return None

        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.4 + 0.25 * (1.0 - abs(pct - 0.6)))),
            features={"atr_percentile": pct, "adx": dx, "atr": a},
            rationale=(f"channel break with ATR in the {pct * 100:.0f}th percentile "
                       f"(band {p['min_percentile']:.2f}-{p['max_percentile']:.2f})"),
        )


class RealisedVolatilityRatio(Strategy):
    """Short-horizon realised volatility against long-horizon, as a PROXY.

    What this would like to be is a variance risk premium trade: sell implied,
    hold realised, collect the well-documented gap. That trade needs an options
    surface, and this system has spot bars and nothing else. So it is not that
    trade and must not be described as one.

    What it actually is: a bet that a short-window realised volatility far
    above its own long-window level is a transient dislocation, and that price
    behaves defensively afterwards. The mechanism is thin -- volatility
    mean-reverts, which is well established, but the link from "volatility is
    reverting" to "price will move in this direction" is not. Expect this to
    fail. It is in the library so the failure is measured at the cost of one
    trial rather than argued about.
    """

    meta = StrategyMeta(
        name="realised_vol_ratio", version="1.0.0", family="volatility",
        timeframe="H4", horizon_bars=20, required_history=260,
        lifecycle="hypothesis",
        description="Fade an extreme short/long realised-volatility ratio, with a "
                    "trend filter. A proxy for the variance risk premium, not the "
                    "premium itself.",
        hypothesis="A short-window realised volatility several times its "
                   "long-window level reflects a liquidity event rather than a "
                   "change in the fundamental distribution. Volatility "
                   "mean-reverts from there; if the price excursion that produced "
                   "the spike also partly reverts, fading it in the direction of "
                   "the longer trend has positive expectancy. The second half of "
                   "that sentence is the weak link and is what is being tested.",
        failure_conditions=[
            "No relation between the volatility ratio and subsequent RETURN, "
            "only between it and subsequent volatility -- the expected result, "
            "and enough on its own to abandon the strategy.",
            "Entries cluster on scheduled macro releases, where the excursion is "
            "information and does not revert.",
            "Removing the trend filter destroys the result, meaning the trend "
            "filter was the strategy.",
            "Negative expectancy after cost at this horizon.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"short_window": 10, "long_window": 60, "ratio_entry": 1.8,
                "trend_window": 100, "atr_window": 14, "stop_atr": 2.0,
                "target_atr": 3.0}

    def _validate(self) -> None:
        p = self.params
        if p["short_window"] >= p["long_window"]:
            raise ValueError("the short window must be shorter than the long one")
        if p["ratio_entry"] <= 1.0:
            raise ValueError("a ratio at or below 1.0 is not an elevated reading")
        if p["target_atr"] / p["stop_atr"] < 1.2:
            raise ValueError("reward/risk below 1.2 cannot survive the cost barrier")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        short = realised_vol(df["close"], p["short_window"])
        long_ = realised_vol(df["close"], p["long_window"])
        trend = ema(df["close"], p["trend_window"])
        return pd.DataFrame({
            "ratio": short / long_.replace(0.0, np.nan),
            "short_vol": short, "long_vol": long_, "trend": trend,
            "atr": atr(df, p["atr_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["trend_window"] + p["long_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        ratio, trend, a = float(f["ratio"]), float(f["trend"]), float(f["atr"])
        if not finite(ratio, trend, a) or a <= 0:
            return None
        if ratio < p["ratio_entry"]:
            return None

        close = float(df["close"].iloc[index])
        # Direction comes entirely from the longer trend: the volatility ratio
        # says "a dislocation happened", it does not say which way it resolves.
        # This is the honest ordering of what each input can support.
        if close < trend:
            side = Side.BUY
        elif close > trend:
            side = Side.SELL
        else:
            return None
        # Only fade back TOWARD the trend, and only when the excursion is
        # meaningful relative to the noise.
        if abs(close - trend) < 0.5 * a:
            return None

        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.3 + 0.15 * (ratio - p["ratio_entry"] + 1))),
            features={"vol_ratio": ratio, "atr": a,
                      "distance_to_trend_atr": (close - trend) / a},
            rationale=(f"short/long realised volatility ratio {ratio:.2f}, fading "
                       f"back toward the {p['trend_window']}-bar trend"),
        )
