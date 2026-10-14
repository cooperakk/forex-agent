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
from ...data.feed import TIMEFRAME_SECONDS
from ..base import (
    FX_ROLLOVER_NY_MINUTE, Strategy, StrategyMeta, adx, atr, donchian, ema, engulfing,
    inside_bar, new_york_clock,
)
from ._common import atr_signal, finite, level_signal


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
        # The range to break is the PREVIOUS bar's -- which is the inside bar
        # itself when the inside bar is the last completed bar, and the bar
        # after it when the break comes one bar later. (This comment used to
        # say "the mother bar", which is not what the code has ever done; the
        # hypothesis below describes the code.) Both are strictly in the past
        # at the breaking bar.
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


# --------------------------------------------------------------------------- #
# Execution Signals (v10.5), adapted from index futures to FX
# --------------------------------------------------------------------------- #

#: Currencies whose pairs follow the usual pip convention. Anything else
#: (metals, crypto, indices) gets no pip floor from its name, only the ATR one.
_FIAT = frozenset({"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK",
                   "NOK", "DKK", "SGD", "HKD", "ZAR", "MXN", "TRY", "PLN", "CZK",
                   "HUF", "CNH", "ILS"})


def _pip_hint(instrument: str) -> Optional[float]:
    """Pip size implied by an FX symbol's name, or None when it is not one."""
    parts = instrument.upper().replace("/", "_").split("_")
    if len(parts) != 2 or not all(p in _FIAT for p in parts):
        return None
    return 0.01 if parts[1] in ("JPY", "HUF") else 0.0001


def _ny_minutes(value: Any, name: str) -> int:
    """'HH:MM' New York time as minutes after midnight.

    Session times are strings on purpose. The acceptance script perturbs every
    NUMERIC parameter by +/-15% and +/-30% to build the robustness family, and
    "15:59 plus 15%" is not a neighbouring strategy, it is a different clock.
    """
    try:
        hh, mm = str(value).split(":")
        hours, minutes = int(hh), int(mm)
    except ValueError:
        raise ValueError(f"{name} must be 'HH:MM' New York time, got {value!r}") from None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"{name} must be 'HH:MM' New York time, got {value!r}")
    return hours * 60 + minutes


class ExecutionSignals(Strategy):
    """Execution Signals v10.5, moved from MNQ futures to the FX majors.

    The source is a TradingView strategy written for micro Nasdaq futures on
    five-minute signals and one-minute execution. Its logic, stripped of the
    instrument:

    1. On every completed higher-timeframe (HTF) bar, ask whether it is an
       *inside bar* (high and low strictly within the previous bar's) or,
       optionally, a *failure bar* (it poked beyond the previous bar's
       extreme and closed back inside, leaving a wick). Either one *arms* the
       setup with that bar's high and low.
    2. During the next HTF period, on the execution timeframe, a CLOSE above
       the armed high is a long and a close below the armed low is a short.
       Direction comes only from which side breaks, never from the signal type.
    3. The stop is the prior bar's extreme, pushed out to a minimum distance;
       the target is ``r_multiple`` times the distance actually risked.
    4. New entries only inside a New York trading window, never in the hour
       either side of the daily roll, and everything is flat at 15:59 New York.
    5. An optional prior-day high/low filter: above yesterday's high only longs,
       below yesterday's low only shorts, and in between either -- or none.

    What changed for FX, and why -- each one is a decision, not a translation:

    * **Timeframes.** The paper runs 5m signals on 1m bars. On EUR/USD a
      one-minute prior-bar stop is one to three pips, and a round trip costs
      about one; the cost barrier closes that family before any data is read
      (library rule: nothing aims below 10 pips). Default here: H1 signals on
      M15 bars, the paper's 4-5x ratio one level up the scale.
    * **Sizing.** The paper sizes ``min(30, floor(1500 / (ATR * $2)))``
      contracts: a dollar figure per ATR, unrelated to the stop or to the
      account, which returns one contract when the ATR is missing. That is
      removed entirely. A strategy here never sizes; the risk engine sizes
      every entry from equity, ``risk_per_trade_pct`` and the stop distance.
    * **Minimum stop.** 10 index points becomes 10 pips (``min_stop_pips``,
      the risk engine's own floor, so a structure stop is widened to a
      tradeable one rather than vetoed), and 0.10% of price
      (``min_stop_pct``): at the default 0.5% risk, a tighter stop is a
      position above the 5x gross-leverage cap, which the engine refuses. An
      optional floor in HTF ATR as well. On M15 the floor, not the bar, sets
      most stops; the structure only ever widens them.
    * **The clock.** The futures session (RTH 09:30, flat 15:59, Globex reopen
      18:00 New York) maps onto FX almost exactly: the gap from 15:59 to 18:00
      New York brackets the 17:00 roll, where FX spreads are widest of the day
      and swap is charged. The times are kept, and remain parameters.
    * **Higher-timeframe bars** are built from the execution bars and anchored
      on the 17:00 New York roll, so an H4 or D1 signal bar is the same candle
      a GMT+2/+3 MetaTrader chart shows. A bar with too few execution bars
      inside it (``min_bucket_coverage``) is not a bar, and two HTF bars that
      are not adjacent in time -- a weekend between them -- are not compared;
      that is the paper's ``maxSkew`` contiguity rule.
    * **Flat at 15:59** is delivered through the signal's own horizon: each
      signal says how many bars remain until 15:59 New York, and the agent's
      time stop closes the trade then. A signal with too little day left is
      not raised, and one whose next 15:59 falls on a weekend is not either.
    * **Position awareness.** The paper arms only while flat. A strategy here
      cannot see positions; the risk engine's one-position-per-instrument
      limit is what refuses the overlap instead.

    One reading of the paper had to be chosen. In its "Break of Signal Bar"
    pseudo-code ``armed <- false`` sits at the level of ``IF armed`` -- so the
    setup is disarmed after the FIRST execution bar of the next period whether
    or not it broke. That literal reading is the default
    (``trigger_window="first_bar"``) and is consistent with the very low trade
    count the paper reports. ``"period"`` keeps the setup armed for the whole
    next HTF period, which the paper's own reset-at-new-period line implies.
    They are different strategies, and the trial ledger counts both if both
    are run. (The LTF mode's pseudo-code disarms only on a break, so the
    setting does not apply there.)

    The second mode, "LTF Signal Bar", is here too: the HTF break only sets a
    bias, and entry waits for an execution-timeframe inside or failure bar in
    that direction to be broken in turn. The paper leaves its two expiry
    counters and its "ltfMatchDir" rule unspecified; the choices made are
    parameters (``bias_expiry_bars``, ``ltf_expiry_bars``,
    ``ltf_match_direction``) and stated in ``default_params``.

    Everything is computed in ``indicators()`` as one forward pass over the
    bars, like ``kama`` and ``supertrend``: state at bar ``t`` depends on bars
    up to ``t`` and nothing later, and the strategy-library truncation test
    holds it to that.
    """

    meta = StrategyMeta(
        name="execution_signals", version="1.0.0", family="pattern",
        timeframe="M15", horizon_bars=24, required_history=300,
        lifecycle="hypothesis",
        description="Execution Signals v10.5 for FX: break of an H1 inside "
                    "(or failure) bar, entered on an M15 close, flat 15:59 NY.",
        hypothesis="A higher-timeframe inside bar is a period in which neither "
                   "side could extend the previous period's range. The first "
                   "close beyond that compressed range on a faster timeframe "
                   "shows which side gave way, and acting on it inside the "
                   "next period -- with a stop just behind the breaking bar and "
                   "a target several times the risk -- captures the expansion "
                   "that tends to follow compression, while the session rules "
                   "keep the trade out of the thin, expensive hours around the "
                   "17:00 New York roll.",
        failure_conditions=[
            "No better than inside_bar_break, which trades the same compression "
            "on H4 without the faster entry: the execution timeframe would then "
            "be adding cost, not information.",
            "No better than breaking the previous HTF bar's range WITHOUT "
            "requiring it to be an inside bar -- the comparison that shows "
            "whether the pattern carries anything at all.",
            "Expectancy disappears once the real M15 spread and commission are "
            "charged: a 10-pip stop leaves little room for a 1-1.5 pip round "
            "trip.",
            "Results depend on the trigger reading (first_bar vs period) or on "
            "the minimum stop, which would mean a definition was fitted rather "
            "than an effect found.",
            "Most trades end at the 15:59 flat rather than at stop or target: "
            "the horizon, not the setup, is then deciding the outcome.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {
            # signal (HTF) bars and which patterns arm the setup
            "signal_timeframe": "H1",
            "trade_inside_bars": True,
            "trade_failures": False,
            "htf_anchor": "fx_day",          # or "utc"
            "min_bucket_coverage": 0.75,
            "atr_window": 14,
            # execution
            "execution_mode": "break",       # or "ltf"
            "trigger_window": "first_bar",   # or "period"
            "allow_long": True,
            "allow_short": True,
            # risk geometry (sizing is the risk engine's, never this class's)
            "stop_model": "prior_bar",       # or "signal_bar", "setup_bar"
            "min_stop_pips": 10.0,
            # The agent's gross-leverage cap, as a stop floor: at risk r% per
            # trade and a cap of L x, any stop under r/L % of price makes ONE
            # position breach the cap. 0.5% / 5x = 0.10%: ~11 pips on EUR/USD,
            # ~15 on USD/JPY at 150. Below it the risk engine would refuse the
            # entry anyway (and rightly -- that is a large position behind a
            # stop a gap walks through), so proposing it only adds vetoes.
            "min_stop_pct": 0.10,
            "min_stop_atr": 0.0,             # in signal-timeframe ATR; 0 = off
            "max_stop_pips": 0.0,            # 0 = off
            "r_multiple": 3.0,
            # the New York clock
            "session": "rth_overnight",      # or "rth", "full"
            "rth_open_ny": "09:30",
            "flat_ny": "15:59",
            "reopen_ny": "18:00",
            "min_bars_before_flat": 2,
            # prior FX day high/low
            "pdhl_filter": False,
            "pdhl_inside": "allow",          # or "block"
            # "LTF Signal Bar" mode. The paper does not give these numbers:
            # 0 = the bias lasts one signal period; 4 M15 bars = one hour.
            "bias_expiry_bars": 0,
            "ltf_expiry_bars": 4,
            "ltf_match_direction": True,
        }

    def _validate(self) -> None:
        p = self.params
        tf = p["signal_timeframe"]
        if tf not in TIMEFRAME_SECONDS:
            raise ValueError(f"unknown signal_timeframe {tf!r}")
        htf_sec, exec_sec = TIMEFRAME_SECONDS[tf], TIMEFRAME_SECONDS[self.meta.timeframe]
        # Paper guards G2/G3: the signal timeframe is at least two minutes and
        # strictly coarser than the chart. Here it must also be a whole number
        # of execution bars and divide the FX day, or "the HTF bar" is not a
        # well-defined set of execution bars.
        if htf_sec <= exec_sec or htf_sec % exec_sec or 86400 % htf_sec:
            raise ValueError(f"signal_timeframe {tf} must be coarser than "
                             f"{self.meta.timeframe}, a whole multiple of it, and "
                             "divide the day (M30, H1, H4 or D1)")
        if not (p["trade_inside_bars"] or p["trade_failures"]):
            raise ValueError("enable at least one of trade_inside_bars / trade_failures")
        if not (p["allow_long"] or p["allow_short"]):
            raise ValueError("enable at least one direction")
        choices = {"htf_anchor": ("fx_day", "utc"), "execution_mode": ("break", "ltf"),
                   "trigger_window": ("first_bar", "period"),
                   "stop_model": ("prior_bar", "signal_bar", "setup_bar"),
                   "session": ("rth_overnight", "rth", "full"),
                   "pdhl_inside": ("allow", "block")}
        for key, allowed in choices.items():
            if p[key] not in allowed:
                raise ValueError(f"{key} must be one of {allowed}, got {p[key]!r}")
        if p["r_multiple"] < 1.2:
            raise ValueError("reward/risk below 1.2 cannot survive the cost barrier")
        if (p["min_stop_pips"] < 0 or p["min_stop_atr"] < 0 or p["max_stop_pips"] < 0
                or p["min_stop_pct"] < 0):
            raise ValueError("stop distances cannot be negative")
        if p["min_stop_pips"] <= 0 and p["min_stop_atr"] <= 0:
            raise ValueError("a minimum stop is required (min_stop_pips or "
                             "min_stop_atr): a one-bar structure stop on M15 can be "
                             "a pip wide, and the risk engine sizes off that distance")
        if p["max_stop_pips"] and p["max_stop_pips"] <= p["min_stop_pips"]:
            raise ValueError("max_stop_pips must exceed min_stop_pips")
        if not (0.0 < p["min_bucket_coverage"] <= 1.0):
            raise ValueError("min_bucket_coverage must be in (0, 1]")
        if p["atr_window"] < 2:
            raise ValueError("atr_window must be at least 2")
        if p["min_bars_before_flat"] < 1:
            raise ValueError("a trade needs at least one bar before the flat")
        if p["bias_expiry_bars"] < 0 or p["ltf_expiry_bars"] < 1:
            raise ValueError("bias_expiry_bars >= 0 and ltf_expiry_bars >= 1")
        open_m = _ny_minutes(p["rth_open_ny"], "rth_open_ny")
        flat_m = _ny_minutes(p["flat_ny"], "flat_ny")
        reopen_m = _ny_minutes(p["reopen_ny"], "reopen_ny")
        if not (open_m < flat_m < reopen_m):
            raise ValueError("New York times must run rth_open_ny < flat_ny < reopen_ny")
        if not (flat_m < FX_ROLLOVER_NY_MINUTE <= reopen_m):
            raise ValueError("flat_ny must fall before the 17:00 New York roll and "
                             "reopen_ny at or after it; holding through the roll is "
                             "what the session rules exist to prevent")

    # -- sizes ------------------------------------------------------------ #

    def _ratio(self) -> int:
        return (TIMEFRAME_SECONDS[self.params["signal_timeframe"]]
                // TIMEFRAME_SECONDS[self.meta.timeframe])

    def warmup(self) -> int:
        # The signal-timeframe ATR needs its window of HTF bars, and the
        # prior-day filter a whole FX day before the first one it reads.
        bars_per_day = 86400 // TIMEFRAME_SECONDS[self.meta.timeframe]
        need = self._ratio() * (int(self.params["atr_window"]) + 3) + 2 * bars_per_day
        return max(self.meta.required_history, need)

    # -- the forward pass -------------------------------------------------- #

    def signal_periods(self, index: pd.DatetimeIndex,
                       clock: Optional[pd.DataFrame] = None) -> np.ndarray:
        """The signal-timeframe period each timestamp falls in, as a period NUMBER.

        Consecutive periods have consecutive numbers whether or not any bars
        exist in them, so "the previous period" is ``k - 1`` and a weekend or
        an outage shows up as a jump rather than as two neighbouring rows.
        Anchored on the 17:00 New York roll by default (``htf_anchor``), which
        is where a GMT+2/+3 MetaTrader server starts its H4 and D1 candles.
        """
        idx = pd.DatetimeIndex(index)
        htf_min = TIMEFRAME_SECONDS[self.params["signal_timeframe"]] // 60
        if self.params["htf_anchor"] == "fx_day":
            clock = clock if clock is not None else new_york_clock(idx)
            since_roll = (clock["ny_minute"].to_numpy() - FX_ROLLOVER_NY_MINUTE) % 1440
            return clock["fx_day"].to_numpy() * (1440 // htf_min) + since_roll // htf_min
        utc = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        return (utc.as_unit("ns").asi8 // 60_000_000_000) // htf_min

    _COLUMNS = ("exec_ok", "entry", "sig_code", "sig_high", "sig_low", "setup_high",
                "setup_low", "prior_high", "prior_low", "htf_atr", "pdh", "pdl",
                "bars_to_flat", "ny_minute")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        n = len(df)
        out = pd.DataFrame({c: np.full(n, np.nan) for c in self._COLUMNS}, index=df.index)
        out["exec_ok"] = 0.0
        out["entry"] = 0.0
        if n < 3:
            return out
        idx = pd.DatetimeIndex(df.index)
        t_ns = (idx.tz_localize("UTC") if idx.tz is None
                else idx.tz_convert("UTC")).as_unit("ns").asi8
        exec_sec = TIMEFRAME_SECONDS[self.meta.timeframe]
        # The bars must BE the declared execution timeframe. Handed H4 bars,
        # this would silently become a different strategy (every "M15 close"
        # four hours apart); it produces nothing instead, like the session
        # family does on bars too coarse for it.
        spacing = float(np.median(np.diff(t_ns))) / 1e9
        if abs(spacing - exec_sec) > 1.0:
            return out
        out["exec_ok"] = 1.0

        o = df["open"].to_numpy(dtype=float)
        h = df["high"].to_numpy(dtype=float)
        lo = df["low"].to_numpy(dtype=float)
        c = df["close"].to_numpy(dtype=float)
        clock = new_york_clock(idx)
        ny_min = clock["ny_minute"].to_numpy()
        fx_day = clock["fx_day"].to_numpy()

        # ---- signal-timeframe bars, built from the execution bars ---------
        bucket = self.signal_periods(idx, clock)
        frame = pd.DataFrame({"b": bucket, "o": o, "h": h, "l": lo, "c": c})
        agg = frame.groupby("b", sort=True).agg(
            open=("o", "first"), high=("h", "max"), low=("l", "min"),
            close=("c", "last"), bars=("o", "size"))
        covered = agg["bars"] >= p["min_bucket_coverage"] * self._ratio()
        agg_atr = atr(agg[["high", "low", "close"]], int(p["atr_window"]))
        # For a bar in bucket k: the signal bar is k-1 and its mother is k-2.
        # Both are complete -- every execution bar in them is in the past --
        # and keying by period number, not by row, is what refuses to compare
        # two periods with a weekend or an outage between them.
        sig = agg.reindex(bucket - 1)
        mom = agg.reindex(bucket - 2)
        ok = (covered.reindex(bucket - 1, fill_value=False).to_numpy(dtype=bool)
              & covered.reindex(bucket - 2, fill_value=False).to_numpy(dtype=bool))
        s_o, s_h = sig["open"].to_numpy(float), sig["high"].to_numpy(float)
        s_l, s_c = sig["low"].to_numpy(float), sig["close"].to_numpy(float)
        m_h, m_l = mom["high"].to_numpy(float), mom["low"].to_numpy(float)
        with np.errstate(invalid="ignore"):
            inside = ok & (s_h < m_h) & (s_l > m_l) & bool(p["trade_inside_bars"])
            fail_hi = (ok & (s_h > m_h) & (s_c <= m_h)
                       & (s_h - np.maximum(s_o, s_c) > 0) & bool(p["trade_failures"]))
            fail_lo = (ok & (s_l < m_l) & (s_c >= m_l)
                       & (np.minimum(s_o, s_c) - s_l > 0) & bool(p["trade_failures"]))
        code = np.where(inside, 1, np.where(fail_hi & fail_lo, 4,
                                            np.where(fail_hi, 2, np.where(fail_lo, 3, 0))))
        htf_atr = agg_atr.reindex(bucket - 1).to_numpy(dtype=float)

        # ---- the state machine (paper steps 1-5), one forward pass --------
        entry, e_code, e_sh, e_sl, e_uh, e_ul = self._run(
            o, h, lo, c, ny_min, bucket, code, s_h, s_l)

        # ---- prior FX day's range ----------------------------------------
        days = frame.assign(d=fx_day).groupby("d", sort=True).agg(
            high=("h", "max"), low=("l", "min"))
        prev = days.shift(1)
        pdh = prev["high"].reindex(fx_day).to_numpy(dtype=float)
        pdl = prev["low"].reindex(fx_day).to_numpy(dtype=float)

        out["entry"] = entry.astype(float)
        out["sig_code"] = e_code.astype(float)
        out["sig_high"], out["sig_low"] = e_sh, e_sl
        out["setup_high"], out["setup_low"] = e_uh, e_ul
        out["prior_high"] = np.concatenate([[np.nan], h[:-1]])
        out["prior_low"] = np.concatenate([[np.nan], lo[:-1]])
        out["htf_atr"] = htf_atr
        out["pdh"], out["pdl"] = pdh, pdl
        out["bars_to_flat"] = self._bars_to_flat(idx, exec_sec)
        out["ny_minute"] = ny_min.astype(float)
        return out

    def _run(self, o, h, lo, c, ny_min, bucket, code, s_h, s_l):
        """The paper's per-bar algorithm, steps 1 to 5, as a forward recursion."""
        p = self.params
        n = len(c)
        open_m = _ny_minutes(p["rth_open_ny"], "rth_open_ny")
        flat_m = _ny_minutes(p["flat_ny"], "flat_ny")
        reopen_m = _ny_minutes(p["reopen_ny"], "reopen_ny")
        mode, session = p["execution_mode"], p["session"]
        first_bar_only = p["trigger_window"] == "first_bar"
        allow_long, allow_short = bool(p["allow_long"]), bool(p["allow_short"])
        bias_expiry = int(p["bias_expiry_bars"]) or self._ratio()
        ltf_expiry = int(p["ltf_expiry_bars"])
        match_dir = bool(p["ltf_match_direction"])

        entry = np.zeros(n, dtype=np.int8)
        e_code = np.zeros(n, dtype=np.int8)
        e_sh, e_sl = np.full(n, np.nan), np.full(n, np.nan)
        e_uh, e_ul = np.full(n, np.nan), np.full(n, np.nan)

        armed, sig_hi, sig_lo, sig_code = False, np.nan, np.nan, 0
        bias, bias_left, b_hi, b_lo, b_code = 0, 0, np.nan, np.nan, 0
        ltf_hi, ltf_lo, ltf_left = np.nan, np.nan, 0
        for i in range(n):
            m = int(ny_min[i])
            # Step 1: session classification.
            in_day = open_m <= m < flat_m
            overnight = m >= reopen_m or m < open_m
            if session == "rth":
                window = in_day
            elif session == "rth_overnight":
                window = in_day or overnight
            else:
                window = True
            no_new = not window
            close_bar = i > 0 and m >= flat_m and int(ny_min[i - 1]) < flat_m
            # Step 2: expiry counters tick only while the session is active.
            if window:
                if bias_left > 0:
                    bias_left -= 1
                    if bias_left == 0:
                        bias = 0
                if ltf_left > 0:
                    ltf_left -= 1
                    if ltf_left == 0:
                        ltf_hi = ltf_lo = np.nan
            # Step 3: a new signal period re-reads the last completed HTF bar.
            if i == 0 or bucket[i] != bucket[i - 1]:
                armed = False
                if code[i] and not no_new:
                    armed, sig_hi, sig_lo, sig_code = True, s_h[i], s_l[i], int(code[i])
            # Step 4: execution.
            if not no_new:
                if mode == "break":
                    if armed:
                        d = 0
                        if c[i] > sig_hi and allow_long:
                            d = 1
                        elif c[i] < sig_lo and allow_short:
                            d = -1
                        if d:
                            entry[i], e_code[i] = d, sig_code
                            e_sh[i], e_sl[i] = sig_hi, sig_lo
                            e_uh[i], e_ul[i] = sig_hi, sig_lo
                        if d or first_bar_only:
                            armed = False
                else:
                    if armed and bias == 0:
                        if c[i] > sig_hi and allow_long:
                            bias, bias_left = 1, bias_expiry
                        elif c[i] < sig_lo and allow_short:
                            bias, bias_left = -1, bias_expiry
                        # No first-bar disarm here: in the paper's LTF mode
                        # the setup stays armed until it breaks or the next
                        # period re-reads it. trigger_window is a Break-mode
                        # reading only.
                        if bias:
                            b_hi, b_lo, b_code = sig_hi, sig_lo, sig_code
                            armed = False
                    if bias and np.isfinite(ltf_hi):
                        if (bias == 1 and c[i] > ltf_hi) or (bias == -1 and c[i] < ltf_lo):
                            entry[i], e_code[i] = bias, b_code
                            e_sh[i], e_sl[i] = b_hi, b_lo
                            e_uh[i], e_ul[i] = ltf_hi, ltf_lo
                            bias, bias_left = 0, 0
                            ltf_hi = ltf_lo = np.nan
                    if bias and i > 0:
                        up_fail = (h[i] > h[i - 1] and c[i] <= h[i - 1]
                                   and h[i] - max(o[i], c[i]) > 0)
                        down_fail = (lo[i] < lo[i - 1] and c[i] >= lo[i - 1]
                                     and min(o[i], c[i]) - lo[i] > 0)
                        inside_bar_now = h[i] < h[i - 1] and lo[i] > lo[i - 1]
                        if match_dir:
                            arm = inside_bar_now or (down_fail if bias == 1 else up_fail)
                        else:
                            arm = inside_bar_now or up_fail or down_fail
                        if arm:
                            ltf_hi, ltf_lo, ltf_left = h[i], lo[i], ltf_expiry
            # Step 5: the end-of-day flat resets every pending setup.
            if close_bar:
                armed, bias, bias_left = False, 0, 0
                ltf_hi = ltf_lo = np.nan
                ltf_left = 0
        return entry, e_code, e_sh, e_sl, e_uh, e_ul

    def _bars_to_flat(self, idx: pd.DatetimeIndex, exec_sec: int) -> np.ndarray:
        """Whole execution bars between each bar's CLOSE and the next flat time.

        NaN where that flat time is on a Saturday or Sunday in New York: the
        trade would be carried into the weekend, which the agent forbids.
        """
        flat_m = _ny_minutes(self.params["flat_ny"], "flat_ny")
        closes = pd.DatetimeIndex(idx) + pd.Timedelta(seconds=exec_sec)
        clock = new_york_clock(closes)
        close_m = clock["ny_minute"].to_numpy()
        day = clock["ny_day"].to_numpy() + (close_m > flat_m)
        weekday = (day + 3) % 7          # 1970-01-01 was a Thursday
        minutes = (day - clock["ny_day"].to_numpy()) * 1440 + flat_m - close_m
        bars = np.floor(minutes * 60 / exec_sec)
        return np.where(weekday >= 5, np.nan, bars)

    # -- the signal --------------------------------------------------------- #

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < self.warmup() or index >= len(df):
            return None
        f = self.features_at(instrument, df, index)
        if float(f["exec_ok"]) < 1.0:
            return None
        d = int(f["entry"])
        if d == 0:
            return None
        bars_left = float(f["bars_to_flat"])
        if not np.isfinite(bars_left) or bars_left < p["min_bars_before_flat"]:
            return None
        close = float(df["close"].iloc[index])
        sig_hi, sig_lo = float(f["sig_high"]), float(f["sig_low"])
        htf_atr = float(f["htf_atr"])
        if not finite(close, sig_hi, sig_lo) or close <= 0:
            return None

        # Prior-day high/low filter (paper section 10).
        if p["pdhl_filter"]:
            pdh, pdl = float(f["pdh"]), float(f["pdl"])
            if finite(pdh, pdl):
                if close > pdh and d != 1:
                    return None
                if close < pdl and d != -1:
                    return None
                if pdl <= close <= pdh and p["pdhl_inside"] == "block":
                    return None

        # Stop (paper section 8): the model's level, pushed out to the minimum.
        model = p["stop_model"]
        if model == "prior_bar":
            raw = float(f["prior_low"]) if d == 1 else float(f["prior_high"])
        elif model == "signal_bar":
            raw = sig_lo if d == 1 else sig_hi
        else:
            raw = float(f["setup_low"]) if d == 1 else float(f["setup_high"])
        pip = _pip_hint(instrument)
        floor = 0.0
        if pip is not None:
            floor = max(floor, p["min_stop_pips"] * pip)
        if p["min_stop_atr"] > 0 and np.isfinite(htf_atr) and htf_atr > 0:
            floor = max(floor, p["min_stop_atr"] * htf_atr)
        if p["min_stop_pct"] > 0:
            floor = max(floor, close * p["min_stop_pct"] / 100.0)
        if floor <= 0:
            return None      # no usable minimum for this symbol: refuse, never guess
        if d == 1:
            stop = min(raw, close - floor) if np.isfinite(raw) else close - floor
        else:
            stop = max(raw, close + floor) if np.isfinite(raw) else close + floor
        risk = abs(close - stop)
        if risk <= 0 or stop <= 0:
            return None
        if p["max_stop_pips"] and pip is not None and risk > p["max_stop_pips"] * pip:
            return None
        target = close + d * risk * p["r_multiple"]

        side = Side.BUY if d == 1 else Side.SELL
        kind = {1: "inside", 2: "failed-high", 3: "failed-low", 4: "two-sided failure"}.get(
            int(f["sig_code"]), "signal")
        features = {"sig_code": float(f["sig_code"]), "bars_to_flat": bars_left,
                    "r_multiple": float(p["r_multiple"]),
                    "ny_minute": float(f["ny_minute"]),
                    "sig_range_atr": ((sig_hi - sig_lo) / htf_atr
                                      if np.isfinite(htf_atr) and htf_atr > 0 else np.nan),
                    "stop_pips": risk / pip if pip else np.nan,
                    "stop_widened": float(not np.isfinite(raw) or abs(close - raw) < risk)}
        return level_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            stop_price=stop, target_price=target,
            horizon_bars=int(bars_left), timeframe=self.meta.timeframe,
            strength=0.4 if int(f["sig_code"]) == 1 else 0.35,
            min_reward_risk=1.2, features=features,
            rationale=(f"{p['signal_timeframe']} {kind} bar "
                       f"[{sig_lo:.5g}-{sig_hi:.5g}] broken "
                       f"{'up' if d == 1 else 'down'} on the {self.meta.timeframe} close"
                       f"{' after an LTF trigger' if p['execution_mode'] == 'ltf' else ''}; "
                       f"{p['r_multiple']:g}R target, flat in {int(bars_left)} bars"),
        )
