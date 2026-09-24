"""Breakout family.

A breakout system is a bet on one specific thing: that the distribution of
returns conditional on having just travelled X is fatter-tailed than the
unconditional one. That is a real, measurable property of FX intraday data --
and it is also exactly what a market maker widening the spread is pricing in,
which is why the family's whole result usually lives inside the cost term.

Every strategy here therefore states its cost exposure in the failure
conditions, and every one of them measures the breakout level from bars that
closed BEFORE the breaking bar. A level that includes the bar breaking it is
not a level.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ...core.types import Side, Signal
from ..base import (
    Strategy, StrategyMeta, adx, atr, donchian, opening_range,
    previous_day_extremes, squeeze_ratio,
)
from ._common import atr_signal, finite


class OpeningRangeBreakout(Strategy):
    """Break of the first N bars of a named session.

    The mechanism, such as it is: the open of a major session brings a burst of
    accumulated overnight order flow, and the first range is a rough measure of
    where the two sides are willing to trade. A break of it within the same
    session is a statement that one side ran out.

    The honest caveat is granularity. On H4 bars "the first 1 bar of London" is
    a four-hour range, which is not what anyone means by an opening range. This
    strategy declares an M15/H1 timeframe for that reason, and on coarser bars
    it will simply produce very few signals rather than silently produce a
    different strategy.
    """

    meta = StrategyMeta(
        name="opening_range_break", version="1.0.0", family="breakout",
        timeframe="M15", horizon_bars=16, required_history=200,
        lifecycle="hypothesis",
        description="Break of the session's opening range, traded inside the session.",
        hypothesis="Orders accumulated while a session was closed are executed in "
                   "its first minutes. The resulting range brackets the price both "
                   "sides accepted; a decisive break of it within the session "
                   "indicates the flow was one-sided and has not finished.",
        failure_conditions=[
            "No edge after charging the session-open spread, which is several "
            "times the quiet-hours spread -- the most likely failure by far.",
            "Both sides of the range break on the same day more often than the "
            "win rate implies: the 'break' is noise around an undefined level.",
            "Result depends on which session; a genuine mechanism should show in "
            "London and New York, not in one cherry-picked window.",
            "Edge disappears when the range length is varied by one bar.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"session": "london", "range_bars": 2, "max_bars_after": 8,
                "atr_window": 14, "stop_atr": 1.5, "target_atr": 3.0,
                "min_range_atr": 0.3}

    def _validate(self) -> None:
        p = self.params
        if p["session"] not in ("london", "newyork", "asia", "overlap"):
            raise ValueError("session must be one of london, newyork, asia, overlap")
        if p["range_bars"] < 1:
            raise ValueError("an opening range needs at least one bar")
        if p["max_bars_after"] <= p["range_bars"]:
            raise ValueError("max_bars_after must leave bars in which to break")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        hi, lo, pos = opening_range(df, p["range_bars"], p["session"])
        a = atr(df, p["atr_window"])
        return pd.DataFrame({
            "or_high": hi, "or_low": lo, "bars_in": pos, "atr": a,
            "range_atr": (hi - lo) / a.replace(0.0, np.nan),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["atr_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        hi, lo, pos, a = (float(f["or_high"]), float(f["or_low"]),
                          float(f["bars_in"]), float(f["atr"]))
        if not finite(hi, lo, pos, a) or a <= 0:
            return None
        # The range must be COMPLETE. While pos < range_bars the values are the
        # running extremes of a range that includes this bar, and breaking your
        # own high is not a breakout.
        if pos < p["range_bars"] or pos > p["max_bars_after"]:
            return None
        range_atr = (hi - lo) / a
        if range_atr < p["min_range_atr"]:
            return None  # a range narrower than the noise is not a level

        row = df.iloc[index]
        close = float(row["close"])
        if float(row["high"]) > hi and close > hi:
            side = Side.BUY
        elif float(row["low"]) < lo and close < lo:
            side = Side.SELL
        else:
            return None

        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.4 + 0.2 * range_atr)),
            features={"range_atr": range_atr, "bars_since_open": pos, "atr": a},
            rationale=(f"{p['session']} opening range ({range_atr:.2f}xATR wide) broken "
                       f"{'up' if side is Side.BUY else 'down'} {pos:.0f} bars in"),
        )


class SqueezeBreakout(Strategy):
    """Break out of a volatility contraction.

    The claim is that volatility is persistent and mean-reverting at once: a
    period of unusually low realised volatility is likely to be followed by a
    higher-volatility one, and the direction of the first decisive move out of
    the contraction has more information than a move out of a normal-volatility
    range.

    The first half of that claim is one of the most robust facts in finance.
    The second half -- that the DIRECTION is predictable -- is not established
    at all, and is what this strategy actually tests.
    """

    meta = StrategyMeta(
        name="squeeze_break", version="1.0.0", family="breakout",
        timeframe="H1", horizon_bars=30, required_history=220,
        lifecycle="hypothesis",
        description="Donchian break taken only after a Bollinger-inside-Keltner squeeze.",
        hypothesis="Volatility clusters, so a contraction is followed by an "
                   "expansion with better-than-chance probability. If the "
                   "expansion's direction is set by whichever side of the "
                   "contraction range gives way first, entering there captures "
                   "the expansion; if direction is a coin flip, this strategy "
                   "pays for two spreads to find that out.",
        failure_conditions=[
            "Directional hit rate indistinguishable from 50%: volatility "
            "expansion was real, its direction was not predictable.",
            "Realised volatility after entry is no higher than the "
            "unconditional average -- the squeeze detector does not detect.",
            "Edge disappears when the squeeze threshold is moved slightly, i.e. "
            "1.0 was fitted.",
            "Losses cluster on the second break after a failed first break.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"bb_window": 20, "atr_window": 20, "bb_k": 2.0, "kc_k": 1.5,
                "squeeze_max": 1.0, "squeeze_bars": 6, "channel": 20,
                "stop_atr": 1.8, "target_atr": 4.0}

    def _validate(self) -> None:
        p = self.params
        if p["squeeze_max"] <= 0:
            raise ValueError("squeeze_max must be positive")
        if p["squeeze_bars"] < 1:
            raise ValueError("a squeeze must persist for at least one bar")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        ratio = squeeze_ratio(df, p["bb_window"], p["atr_window"], p["bb_k"], p["kc_k"])
        squeezed = (ratio < p["squeeze_max"]).astype(float)
        # "Was in a squeeze over the PRECEDING n bars, and is no longer": the
        # shift is what makes this a release rather than a state.
        held = squeezed.rolling(p["squeeze_bars"],
                                min_periods=p["squeeze_bars"]).min().shift(1)
        upper, lower = donchian(df, p["channel"])
        return pd.DataFrame({
            "ratio": ratio, "held": held, "upper": upper, "lower": lower,
            "atr": atr(df, p["atr_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < max(p["bb_window"], p["channel"], p["atr_window"]) + p["squeeze_bars"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        ratio, held = float(f["ratio"]), float(f["held"])
        up, lo, a = float(f["upper"]), float(f["lower"]), float(f["atr"])
        if not finite(ratio, held, up, lo, a) or a <= 0:
            return None
        if held < 1.0:
            return None  # the squeeze was not sustained

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
            strength=float(min(1.0, 0.4 + 0.3 * max(0.0, 1.0 - ratio))),
            features={"squeeze_ratio": ratio, "atr": a,
                      "channel_width_atr": (up - lo) / a},
            rationale=(f"{p['squeeze_bars']}-bar squeeze (ratio {ratio:.2f}) released "
                       f"through the {p['channel']}-bar channel"),
        )


class PreviousDayBreak(Strategy):
    """Break of the previous calendar day's high or low.

    The level is the most widely watched intraday reference there is, which
    cuts both ways: a self-fulfilling reaction is plausible, and so is the
    opposite -- stop-hunting through an obvious level. The strategy takes the
    continuation side; ``failed_breakout_reversal`` in the pattern family takes
    the other, and the two are deliberately both in the library so the pair can
    be measured against each other rather than argued about.
    """

    meta = StrategyMeta(
        name="prev_day_break", version="1.0.0", family="breakout",
        timeframe="H1", horizon_bars=24, required_history=200,
        lifecycle="hypothesis",
        description="Trade a decisive break of yesterday's range.",
        hypothesis="Yesterday's extremes are the reference points most resting "
                   "orders are placed against. Clearing one removes a block of "
                   "supply or demand, so the move that follows meets less "
                   "resistance than an equivalent move elsewhere in the range.",
        failure_conditions=[
            "Break-and-reverse is more common than break-and-continue, which "
            "would mean the level attracts stop-hunting rather than continuation.",
            "Edge only present when the break happens in one specific session.",
            "No edge after cost: the level is watched, so the spread around it "
            "is not the quiet-hours spread.",
            "Result is materially different when the day boundary is moved from "
            "UTC midnight to 17:00 New York -- the level was arbitrary.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"atr_window": 14, "min_break_atr": 0.15, "stop_atr": 1.8,
                "target_atr": 3.6, "adx_window": 14, "adx_min": 15.0}

    def _validate(self) -> None:
        p = self.params
        if p["min_break_atr"] < 0:
            raise ValueError("min_break_atr cannot be negative")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        prev_high, prev_low = previous_day_extremes(df)
        return pd.DataFrame({
            "prev_high": prev_high, "prev_low": prev_low,
            "atr": atr(df, p["atr_window"]),
            "adx": adx(df, p["adx_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["atr_window"] + 10:
            return None
        f = self.features_at(instrument, df, index)
        ph, pl, a, dx = (float(f["prev_high"]), float(f["prev_low"]),
                         float(f["atr"]), float(f["adx"]))
        if not finite(ph, pl, a, dx) or a <= 0:
            return None
        if dx < p["adx_min"]:
            return None

        close = float(df["close"].iloc[index])
        margin = p["min_break_atr"] * a
        # Requiring the CLOSE beyond the level by a margin, not just the high,
        # is what separates a break from a wick through a resting order block.
        if close > ph + margin:
            side, level = Side.BUY, ph
        elif close < pl - margin:
            side, level = Side.SELL, pl
        else:
            return None

        excess = abs(close - level) / a
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.4 + 0.2 * excess)),
            features={"break_excess_atr": excess, "adx": dx, "atr": a,
                      "prev_range_atr": (ph - pl) / a},
            rationale=(f"close {excess:.2f}xATR beyond yesterday's "
                       f"{'high' if side is Side.BUY else 'low'}, ADX {dx:.0f}"),
        )
