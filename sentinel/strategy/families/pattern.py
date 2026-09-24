"""Candlestick and price-pattern family.

This is the part of the library where honesty costs the most, so it is worth
being explicit at the top: the published evidence for candlestick patterns as
standalone signals is weak to negative. Every serious test of them finds that
the apparent effect disappears once the trend the pattern occurred in is
controlled for, and that is what the filters below exist to isolate.

So the patterns here are NOT used as signals. They are used as *timing* on top
of a condition that is doing the work -- a trend, a level, a failed breakout --
and each strategy's first failure condition is the comparison that would show
the pattern added nothing. If the pattern-free version of the rule performs the
same, the correct conclusion is that the pattern is decoration and the strategy
should leave the library rather than be re-tuned.

The one genuine mechanism in this family is in ``failed_breakout_reversal``:
stops resting beyond an obvious level get filled, and the resulting flow is
forced rather than informed. That is a structural claim about order placement,
not a claim about the shape of a bar.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ...core.types import Side, Signal
from ..base import (
    Strategy, StrategyMeta, adx, atr, donchian, ema, engulfing, inside_bar,
)
from ._common import atr_signal, finite


class InsideBarBreak(Strategy):
    """Break of an inside bar, taken with the prevailing trend.

    An inside bar is a compression: the market spent a whole period without
    exceeding the previous period's range. That is a genuine, if small, piece
    of information -- it is the same volatility-contraction idea as
    ``squeeze_break``, measured over two bars instead of twenty, which also
    means it is far noisier.
    """

    meta = StrategyMeta(
        name="inside_bar_break", version="1.0.0", family="pattern",
        timeframe="H4", horizon_bars=18, required_history=200,
        lifecycle="hypothesis",
        description="Trade the break of an inside bar in the direction of the trend.",
        hypothesis="An inside bar marks a brief balance between buyers and "
                   "sellers. A break of the preceding bar's range from that "
                   "balance indicates one side withdrew, and the trend filter "
                   "selects the breaks that agree with the larger flow. The "
                   "compression is the same effect the squeeze strategy trades, "
                   "measured over two bars.",
        failure_conditions=[
            "No better than taking every trend-aligned break without requiring "
            "the inside bar -- the first thing to check, and the most likely "
            "outcome.",
            "Two-bar compression has no measurable relation to subsequent range "
            "expansion on this data.",
            "Signal frequency so high that cost dominates the result.",
            "Edge disappears when the inside-bar definition is loosened slightly "
            "(e.g. allowing an equal high), meaning the exact definition was "
            "fitted.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"trend_window": 50, "atr_window": 14, "stop_atr": 1.5,
                "target_atr": 3.0, "max_bars_after": 2, "min_break_atr": 0.1}

    def _validate(self) -> None:
        p = self.params
        if p["max_bars_after"] < 1:
            raise ValueError("the break needs at least one bar after the pattern")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        inside = inside_bar(df)
        # The range to break belongs to the MOTHER bar -- the one before the
        # inside bar -- and both are strictly in the past at the breaking bar.
        recent_inside = inside.rolling(p["max_bars_after"],
                                       min_periods=1).max().shift(1).fillna(0.0)
        return pd.DataFrame({
            "inside_recent": recent_inside.astype(float),
            "ref_high": df["high"].shift(1), "ref_low": df["low"].shift(1),
            "trend": ema(df["close"], p["trend_window"]),
            "atr": atr(df, p["atr_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["trend_window"] + p["atr_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        recent = float(f["inside_recent"])
        hi, lo = float(f["ref_high"]), float(f["ref_low"])
        trend, a = float(f["trend"]), float(f["atr"])
        if not finite(recent, hi, lo, trend, a) or a <= 0:
            return None
        if recent < 1.0:
            return None

        close = float(df["close"].iloc[index])
        margin = p["min_break_atr"] * a
        if close > hi + margin and close > trend:
            side = Side.BUY
        elif close < lo - margin and close < trend:
            side = Side.SELL
        else:
            return None

        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.35 + 0.2 * (hi - lo) / a)),
            features={"pattern_range_atr": (hi - lo) / a, "atr": a,
                      "distance_to_trend_atr": (close - trend) / a},
            rationale="inside-bar range broken in the direction of the trend",
        )


class EngulfingWithTrend(Strategy):
    """Engulfing bar as a pullback-entry trigger, never as a signal on its own.

    The construction matters more than the pattern. The setup is a pullback
    within an intact trend -- which is a claim with some support -- and the
    engulfing bar is only the trigger that says the pullback has stopped. Used
    alone, in either direction, this pattern has repeatedly tested as noise.
    """

    meta = StrategyMeta(
        name="engulfing_trend", version="1.0.0", family="pattern",
        timeframe="H4", horizon_bars=20, required_history=220,
        lifecycle="hypothesis",
        description="Engulfing bar taken only as a pullback trigger inside a trend.",
        hypothesis="Within an established trend, a pullback that ends in a bar "
                   "fully reversing the previous one indicates the counter-trend "
                   "flow was exhausted rather than sustained. The claim is about "
                   "the pullback ending, not about the candle: the candle is "
                   "timing.",
        failure_conditions=[
            "Same performance from entering the pullback WITHOUT the engulfing "
            "condition -- the standard result in the literature, and the first "
            "comparison to run.",
            "Most entries occur after the trend has already turned, i.e. the "
            "trend filter is lagging enough to invert the setup.",
            "Edge concentrated in one instrument or one year.",
            "Body-overlap definition changes the result materially, which would "
            "mean the definition, not the phenomenon, is producing the number.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"trend_window": 60, "pullback_window": 5, "atr_window": 14,
                "stop_atr": 1.6, "target_atr": 3.2, "min_body_atr": 0.4}

    def _validate(self) -> None:
        p = self.params
        if p["pullback_window"] < 2:
            raise ValueError("a pullback needs at least two bars to be visible")
        if p["min_body_atr"] < 0:
            raise ValueError("min_body_atr cannot be negative")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        a = atr(df, p["atr_window"])
        close = df["close"].astype(float)
        trend = ema(close, p["trend_window"])
        body = (close - df["open"].astype(float)).abs() / a.replace(0.0, np.nan)
        # A pullback is the recent move AGAINST the trend, measured over the
        # bars before this one so the trigger bar is not part of the pullback.
        pullback = (close.shift(1) - close.shift(1 + p["pullback_window"])) \
            / a.replace(0.0, np.nan)
        return pd.DataFrame({
            "engulf": engulfing(df), "trend": trend, "atr": a,
            "body_atr": body, "pullback_atr": pullback,
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["trend_window"] + p["pullback_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        eng, trend, a = float(f["engulf"]), float(f["trend"]), float(f["atr"])
        body, pullback = float(f["body_atr"]), float(f["pullback_atr"])
        if not finite(eng, trend, a, body, pullback) or a <= 0:
            return None
        if eng == 0.0 or body < p["min_body_atr"]:
            return None

        close = float(df["close"].iloc[index])
        # Long: uptrend (price above the average), a DOWN pullback into it, and
        # a bullish engulfing bar ending it. All three, or nothing.
        if eng > 0 and close > trend and pullback < 0:
            side = Side.BUY
        elif eng < 0 and close < trend and pullback > 0:
            side = Side.SELL
        else:
            return None

        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.35 + 0.2 * body)),
            features={"body_atr": body, "pullback_atr": pullback, "atr": a,
                      "distance_to_trend_atr": (close - trend) / a},
            rationale=(f"{'bullish' if side is Side.BUY else 'bearish'} engulfing "
                       f"({body:.2f}xATR body) ending a {abs(pullback):.2f}xATR "
                       "pullback inside the trend"),
        )


class FailedBreakoutReversal(Strategy):
    """Trade back through a level that was broken and did not hold.

    The one strategy in this family with a mechanism that does not depend on
    the shape of a candle. Stop orders cluster just beyond obvious levels --
    yesterday's high, an N-bar channel edge. When price trades through and
    immediately closes back inside, two things have happened: the resting stops
    were filled, and the flow that filled them was forced rather than informed.
    The participants who are now offside are the ones who bought the break.

    It is the deliberate counterpart to ``prev_day_break`` and
    ``donchian_trend``. Both cannot be right about the same bar, and having
    both in the library means the question gets an answer from the acceptance
    protocol instead of from whoever argues hardest.
    """

    meta = StrategyMeta(
        name="failed_breakout_reversal", version="1.0.0", family="pattern",
        timeframe="H4", horizon_bars=22, required_history=220,
        lifecycle="hypothesis",
        description="Fade a breakout that closed back inside the range.",
        hypothesis="Resting stop orders cluster beyond widely watched levels. A "
                   "penetration that closes back inside the range indicates the "
                   "move was stop-driven rather than flow-driven: the liquidity "
                   "beyond the level has been consumed and the traders who "
                   "entered on the break are now holding losing positions they "
                   "must exit against the reversal.",
        failure_conditions=[
            "Continuation beats reversal on the same bars, i.e. "
            "``prev_day_break`` and ``donchian_trend`` are right and this is not. "
            "Both cannot be accepted on the same instrument and timeframe.",
            "No edge once the cost of entering against a fast move is charged; "
            "the spread on a failed break is not the resting spread.",
            "Result depends on how 'closed back inside' is defined by a margin "
            "-- a genuine structural effect should be robust to that margin.",
            "Losses have a long tail: a break that fails, then succeeds, runs "
            "against the position exactly when it is largest.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"channel": 30, "atr_window": 14, "min_penetration_atr": 0.25,
                "reentry_margin_atr": 0.05, "stop_atr": 1.5, "target_atr": 3.0,
                "adx_window": 14, "adx_max": 30.0}

    def _validate(self) -> None:
        p = self.params
        if p["channel"] < 10:
            raise ValueError("channel below 10 bars is noise, not a level")
        if p["min_penetration_atr"] <= 0:
            raise ValueError("a failed break requires a real penetration first")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        upper, lower = donchian(df, p["channel"])
        return pd.DataFrame({
            "upper": upper, "lower": lower,
            "atr": atr(df, p["atr_window"]),
            "adx": adx(df, p["adx_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["channel"] + p["atr_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        up, lo, a, dx = (float(f["upper"]), float(f["lower"]),
                         float(f["atr"]), float(f["adx"]))
        if not finite(up, lo, a, dx) or a <= 0:
            return None
        # A strong trend is the condition under which a "failed" break is most
        # likely to be a pause rather than a failure.
        if dx > p["adx_max"]:
            return None

        row = df.iloc[index]
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        pen_up = (high - up) / a
        pen_down = (lo - low) / a
        margin = p["reentry_margin_atr"] * a
        # Broke the high intrabar, closed back below it: fade down.
        if pen_up >= p["min_penetration_atr"] and close < up - margin:
            side, level, penetration = Side.SELL, up, pen_up
        elif pen_down >= p["min_penetration_atr"] and close > lo + margin:
            side, level, penetration = Side.BUY, lo, pen_down
        else:
            return None

        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.35 + 0.25 * penetration)),
            features={"penetration_atr": penetration, "adx": dx, "atr": a,
                      "level": level},
            rationale=(f"{p['channel']}-bar level penetrated by {penetration:.2f}xATR "
                       f"and reclaimed on the close; ADX {dx:.0f}"),
        )
