"""Trend family.

One idea, six implementations: exchange rates sometimes move in the same
direction for longer than a random walk would, because the repricing of
relative monetary policy is slow and because real-money flows are executed over
weeks rather than at once. Every strategy here is a different change-point
detector over that same premise, so they are NOT six independent bets -- which
is precisely why the trial ledger charges a candidate with the search effort
spent on its whole family (``research/trials.py``).

The shared failure mode is also one thing: in a range, every detector fires
late in both directions and pays the spread twice. Each strategy below carries
its own gate against that, and each one states in ``failure_conditions`` what
would tell you the gate is not working.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ...core.types import Side, Signal
from ..base import (
    Strategy, StrategyMeta, adx, atr, donchian, ema, ichimoku, kama,
    realised_vol, supertrend,
)
from ._common import atr_signal, finite, level_signal



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

class DonchianTrend(Strategy):
    """Channel breakout with an ADX regime gate and an ATR stop."""

    meta = StrategyMeta(
        name="donchian_trend", version="1.1.0", family="trend",
        timeframe="H4", horizon_bars=60,
        required_history=250, lifecycle="hypothesis",
        description="Long on an N-bar high, short on an N-bar low, only when ADX "
                    "says a trend exists.",
        hypothesis="Exchange rates exhibit slow-moving trends driven by gradual "
                   "repricing of rate expectations; a breakout captures the "
                   "continuation with a stop wide enough to survive noise.",
        failure_conditions=[
            "Alpha not significant after controlling for the dollar and carry factors.",
            "Fails the Clark-West test against a random walk.",
            "Positive only when the cost assumption is at its optimistic end.",
            "Win rate below the break-even rate implied by the realised spread.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"channel": 55, "exit_channel": 20, "atr_window": 20,
                "stop_atr": 2.0, "target_atr": 5.0, "adx_window": 14,
                "adx_min": 20.0, "vol_filter_quantile": 0.90}

    def _validate(self) -> None:
        if self.params["channel"] < 10:
            raise ValueError("channel below 10 bars is noise, not a breakout")
        if self.params["stop_atr"] <= 0 or self.params["target_atr"] <= 0:
            raise ValueError("stop_atr and target_atr must be positive")
        if self.params["target_atr"] <= self.params["stop_atr"]:
            raise ValueError("target must exceed stop: a sub-1R target cannot clear cost")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        upper, lower = donchian(df, p["channel"])
        vol = realised_vol(df["close"], 50)
        # Expanding quantile: the volatility ceiling at bar i uses only the
        # volatility observed up to bar i. A full-sample quantile here would be
        # look-ahead of the subtlest and most damaging kind.
        vol_ceiling = _rolling_quantile(vol, p["vol_filter_quantile"])
        return pd.DataFrame({
            "upper": upper, "lower": lower,
            "atr": atr(df, p["atr_window"]),
            "adx": adx(df, p["adx_window"]),
            "vol": vol, "vol_ceiling": vol_ceiling,
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        need = max(p["channel"], p["atr_window"], p["adx_window"]) + 5
        if index < need:
            return None
        f = self.features_at(instrument, df, index)
        u, l, a_now, adx_now = (float(f["upper"]), float(f["lower"]),
                                float(f["atr"]), float(f["adx"]))
        if not all(np.isfinite(v) for v in (u, l, a_now, adx_now)) or a_now <= 0:
            return None
        if adx_now < p["adx_min"]:
            return None

        # Volatility circuit breaker: a breakout in the top decile of realised
        # volatility is usually the *end* of a move, and the spread there is
        # widest.
        vol_now, ceiling = float(f["vol"]), float(f["vol_ceiling"])
        if np.isfinite(vol_now) and np.isfinite(ceiling) and vol_now > ceiling:
            return None

        row = df.iloc[index]
        close = float(row["close"])
        high, low = float(row["high"]), float(row["low"])
        side: Optional[Side] = None
        if high > u:
            side = Side.BUY
        elif low < l:
            side = Side.SELL
        if side is None:
            return None

        sign = 1 if side is Side.BUY else -1
        # Strength scales with how far beyond the channel price closed, capped
        # at 1. This is an ordering, not a probability, and is flagged as such.
        excess = abs(close - (u if side is Side.BUY else l)) / a_now
        strength = float(min(1.0, 0.35 + 0.3 * excess))
        return Signal(
            strategy=self.meta.name, instrument=instrument, side=side, strength=strength,
            stop_price=close - sign * p["stop_atr"] * a_now,
            target_price=close + sign * p["target_atr"] * a_now,
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            features={"adx": adx_now, "atr": a_now, "channel_excess_atr": float(excess),
                      "close": close},
            calibrated=False,
            rationale=(f"{p['channel']}-bar {'high' if side is Side.BUY else 'low'} broken "
                       f"with ADX {adx_now:.0f}; stop {p['stop_atr']}xATR, "
                       f"target {p['target_atr']}xATR"),
        )


class MovingAverageCrossATR(Strategy):
    """Fast/slow EMA cross, taken only when the averages have actually separated.

    The plain crossover is the oldest system in the book and it loses money in
    the standard way: around a flat mean the two averages cross back and forth
    every few bars, and each round trip pays the spread. Requiring a separation
    of ``min_separation_atr`` before acting removes most of those crossings --
    at the cost of entering later, which is the trade this strategy exists to
    measure.
    """

    meta = StrategyMeta(
        name="ma_cross_atr", version="1.0.0", family="trend",
        timeframe="H4", horizon_bars=48, required_history=260,
        lifecycle="hypothesis",
        description="EMA crossover confirmed by an ATR-scaled separation filter.",
        hypothesis="A crossover is a crude change-point detector on the drift of "
                   "the exchange rate. If the drift really does persist after "
                   "monetary-policy repricing, entering on a confirmed change of "
                   "sign captures the remainder of it. The ATR filter exists "
                   "because the detector's false-positive rate is highest exactly "
                   "where the drift is zero.",
        failure_conditions=[
            "Whipsaw rate (trades closed inside 2 bars) above 30%: the separation "
            "filter is not removing the noise crossings it was added for.",
            "Profit concentrated in fewer than 5% of trades -- the payoff is a "
            "lottery ticket on one move, not a repeatable edge.",
            "No alpha after the dollar factor: it is a levered dollar view.",
            "Negative in the lowest ADX tercile even with the filter on.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"fast": 20, "slow": 60, "atr_window": 20, "min_separation_atr": 0.35,
                "stop_atr": 2.2, "target_atr": 5.0, "slope_window": 5}

    def _validate(self) -> None:
        p = self.params
        if p["fast"] >= p["slow"]:
            raise ValueError("the fast average must be faster than the slow one")
        if p["min_separation_atr"] < 0:
            raise ValueError("min_separation_atr cannot be negative")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop: a sub-1R target cannot clear cost")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        fast, slow = ema(df["close"], p["fast"]), ema(df["close"], p["slow"])
        a = atr(df, p["atr_window"])
        gap = (fast - slow) / a.replace(0.0, np.nan)
        return pd.DataFrame({
            "fast": fast, "slow": slow, "atr": a, "gap": gap,
            "gap_prev": gap.shift(1),
            "slope": slow.diff(p["slope_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["slow"] + p["atr_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        gap, gap_prev, a, slope = (float(f["gap"]), float(f["gap_prev"]),
                                   float(f["atr"]), float(f["slope"]))
        if not finite(gap, gap_prev, a, slope) or a <= 0:
            return None

        thr = p["min_separation_atr"]
        # The entry is the bar on which the separation crosses the threshold,
        # not every bar the averages happen to be apart. Without the `gap_prev`
        # condition this fires continuously for the whole length of a trend and
        # the position limit, not the signal, decides what gets traded.
        if gap >= thr > gap_prev and slope > 0:
            side = Side.BUY
        elif gap <= -thr < gap_prev and slope < 0:
            side = Side.SELL
        else:
            return None

        close = float(df["close"].iloc[index])
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.4 + 0.25 * abs(gap))),
            features={"gap_atr": gap, "atr": a, "slow_slope": slope},
            rationale=(f"EMA{p['fast']}/{p['slow']} separated by {abs(gap):.2f}xATR "
                       f"with the slow average sloping {'up' if slope > 0 else 'down'}"),
        )


class ADXTrendFollow(Strategy):
    """Trade the direction of the slow average, but only while ADX is rising.

    ADX above a threshold says a trend exists; ADX *rising* says it is still
    being fed. The distinction matters because a high, falling ADX is the
    signature of a trend that has just finished -- the classic late entry, and
    the most expensive one.
    """

    meta = StrategyMeta(
        name="adx_trend", version="1.0.0", family="trend",
        timeframe="H4", horizon_bars=50, required_history=250,
        lifecycle="hypothesis",
        description="Directional entry gated on ADX level and slope.",
        hypothesis="Directional persistence in FX is concentrated in the minority "
                   "of periods when one currency is being repriced against the "
                   "other. ADX is a crude measure of whether that is happening "
                   "now; its slope is a crude measure of whether it is still "
                   "happening. Trading only in that subset should raise the hit "
                   "rate enough to pay for the entries it gives up.",
        failure_conditions=[
            "Performance no better than the same entries without the ADX gate: "
            "the filter costs trades and buys nothing.",
            "Most losses occur at the highest ADX readings -- the gate is "
            "selecting exhaustion rather than persistence.",
            "Fails Clark-West against a random walk.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"trend_window": 50, "adx_window": 14, "adx_min": 25.0,
                "adx_slope_window": 5, "atr_window": 20,
                "stop_atr": 2.0, "target_atr": 4.5}

    def _validate(self) -> None:
        if not 0 < self.params["adx_min"] < 100:
            raise ValueError("adx_min must lie inside (0, 100)")
        if self.params["target_atr"] <= self.params["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        trend = ema(df["close"], p["trend_window"])
        a_dx = adx(df, p["adx_window"])
        return pd.DataFrame({
            "trend": trend,
            "trend_slope": trend.diff(p["adx_slope_window"]),
            "adx": a_dx,
            "adx_slope": a_dx.diff(p["adx_slope_window"]),
            "atr": atr(df, p["atr_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["trend_window"] + p["adx_window"] + 10:
            return None
        f = self.features_at(instrument, df, index)
        trend, slope = float(f["trend"]), float(f["trend_slope"])
        dx, dx_slope, a = float(f["adx"]), float(f["adx_slope"]), float(f["atr"])
        if not finite(trend, slope, dx, dx_slope, a) or a <= 0:
            return None
        if dx < p["adx_min"] or dx_slope <= 0:
            return None

        close = float(df["close"].iloc[index])
        if slope > 0 and close > trend:
            side = Side.BUY
        elif slope < 0 and close < trend:
            side = Side.SELL
        else:
            return None

        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.35 + dx / 100.0)),
            features={"adx": dx, "adx_slope": dx_slope, "atr": a,
                      "distance_atr": abs(close - trend) / a},
            rationale=f"ADX {dx:.0f} and rising, price {'above' if side is Side.BUY else 'below'} "
                      f"the {p['trend_window']}-bar average",
        )


class KaufmanAdaptiveTrend(Strategy):
    """Follow a KAMA that speeds up in a directional move and flattens in chop.

    The adaptive smoothing is the whole hypothesis: a fixed-length average is
    either too slow to enter or too fast to hold, and which one it is depends
    on a regime nobody can observe in advance. KAMA lets the data choose, using
    the efficiency ratio -- net travel over gross travel -- as the switch.
    """

    meta = StrategyMeta(
        name="kama_trend", version="1.0.0", family="trend",
        timeframe="H4", horizon_bars=45, required_history=240,
        lifecycle="hypothesis",
        description="Entry when price crosses a rising/falling Kaufman adaptive MA.",
        hypothesis="The optimal lookback for a trend filter varies with how "
                   "directional the market currently is. Making the smoothing "
                   "constant a function of the efficiency ratio removes one "
                   "hand-tuned parameter and should reduce whipsaw without the "
                   "lag cost of simply lengthening the average.",
        failure_conditions=[
            "No improvement over a fixed-length EMA of comparable average speed: "
            "the adaptation is decoration.",
            "Efficiency ratio distribution is nearly constant on this data, which "
            "means the adaptive constant never actually adapts.",
            "Performance depends on the er_window choice, i.e. the removed "
            "parameter came back as a different one.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"er_window": 10, "fast": 2, "slow": 30, "slope_window": 4,
                "atr_window": 20, "stop_atr": 2.0, "target_atr": 4.0,
                "min_distance_atr": 0.15}

    def _validate(self) -> None:
        p = self.params
        if p["fast"] >= p["slow"]:
            raise ValueError("the fast constant must be faster than the slow one")
        if p["er_window"] < 2:
            raise ValueError("an efficiency ratio needs at least 2 bars")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        k = kama(df["close"], p["er_window"], p["fast"], p["slow"])
        a = atr(df, p["atr_window"])
        close = df["close"].astype(float)
        dist = (close - k) / a.replace(0.0, np.nan)
        return pd.DataFrame({
            "kama": k, "atr": a, "dist": dist, "dist_prev": dist.shift(1),
            "kama_slope": k.diff(p["slope_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["slow"] + p["atr_window"] + 10:
            return None
        f = self.features_at(instrument, df, index)
        dist, dist_prev = float(f["dist"]), float(f["dist_prev"])
        slope, a = float(f["kama_slope"]), float(f["atr"])
        if not finite(dist, dist_prev, slope, a) or a <= 0:
            return None

        thr = p["min_distance_atr"]
        if dist >= thr > dist_prev and slope > 0:
            side = Side.BUY
        elif dist <= -thr < dist_prev and slope < 0:
            side = Side.SELL
        else:
            return None

        close = float(df["close"].iloc[index])
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.4 + 0.2 * abs(dist))),
            features={"kama_distance_atr": dist, "kama_slope": slope, "atr": a},
            rationale=f"price crossed the KAMA by {abs(dist):.2f}xATR with the "
                      f"average sloping {'up' if slope > 0 else 'down'}",
        )


class SupertrendFlip(Strategy):
    """Enter on a Supertrend direction flip, stop at the line itself.

    The interesting property is the stop: it comes from the indicator rather
    than from a multiple of ATR, so the risk per trade is whatever the ratchet
    says it is. That is honest but dangerous -- right after a flip the line can
    sit a few pips away, giving a stop so tight that the spread alone is a
    meaningful fraction of the risk. ``min_stop_atr`` floors it, and the
    reward/risk check in ``level_signal`` refuses what is left over.
    """

    meta = StrategyMeta(
        name="supertrend_flip", version="1.0.0", family="trend",
        timeframe="H4", horizon_bars=40, required_history=200,
        lifecycle="hypothesis",
        description="Trade the Supertrend flip with the line as the protective stop.",
        hypothesis="A volatility-scaled trailing band that only ever moves in the "
                   "favourable direction distinguishes a genuine give-back from "
                   "noise, so a flip marks a change in the sign of the drift "
                   "rather than a single adverse bar.",
        failure_conditions=[
            "Average risk per trade drifts with volatility regime so far that "
            "position sizing becomes the dominant source of return variance.",
            "Most flips reverse within a few bars: the band is tracking noise.",
            "Removing the multiplier sensitivity check changes the sign of the "
            "result -- the number 3.0 was doing the work.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"atr_window": 10, "multiplier": 3.0, "stop_atr_window": 20,
                "min_stop_atr": 1.0, "target_r": 2.5, "adx_window": 14,
                "adx_min": 18.0}

    def _validate(self) -> None:
        p = self.params
        if p["multiplier"] <= 0:
            raise ValueError("the Supertrend multiplier must be positive")
        if p["target_r"] < 1.2:
            raise ValueError("a target below 1.2R cannot clear the cost barrier")
        if p["min_stop_atr"] <= 0:
            raise ValueError("min_stop_atr must be positive: a zero-width stop is "
                             "an unbounded position")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        line, direction = supertrend(df, p["atr_window"], p["multiplier"])
        return pd.DataFrame({
            "line": line, "dir": direction, "dir_prev": direction.shift(1),
            "atr": atr(df, p["stop_atr_window"]),
            "adx": adx(df, p["adx_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["stop_atr_window"] + p["atr_window"] + 10:
            return None
        f = self.features_at(instrument, df, index)
        line, d, d_prev = float(f["line"]), float(f["dir"]), float(f["dir_prev"])
        a, dx = float(f["atr"]), float(f["adx"])
        if not finite(line, d, d_prev, a, dx) or a <= 0:
            return None
        if d == d_prev:
            return None  # no flip on this bar
        if dx < p["adx_min"]:
            return None

        side = Side.BUY if d > 0 else Side.SELL
        close = float(df["close"].iloc[index])
        sign = side.sign
        # Floor the structural stop at min_stop_atr. Without this the first bar
        # after a flip can produce a stop a fraction of a pip away, which sizes
        # into an enormous position for a nominal risk budget.
        raw_distance = abs(close - line)
        distance = max(raw_distance, p["min_stop_atr"] * a)
        stop = close - sign * distance
        target = close + sign * p["target_r"] * distance
        return level_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            stop_price=stop, target_price=target,
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.35 + dx / 120.0)),
            min_reward_risk=1.2,
            features={"supertrend_distance_atr": raw_distance / a, "adx": dx, "atr": a,
                      "stop_floored": float(distance > raw_distance)},
            rationale=f"Supertrend flipped {'long' if side is Side.BUY else 'short'} "
                      f"with ADX {dx:.0f}; stop at the line ({distance / a:.2f}xATR)",
        )


class IchimokuCloudBreak(Strategy):
    """Close beyond the cloud with the conversion line confirming.

    Ichimoku is popular for reasons that have nothing to do with evidence, and
    most published rules for it use the chikou span -- a series shifted 26 bars
    into the past, which when read at the current bar is a look at 26 bars of
    the future. ``base.ichimoku`` deliberately does not compute it. What is
    left is a displaced breakout system with a built-in support/resistance
    band, which is at least a testable claim.
    """

    meta = StrategyMeta(
        name="ichimoku_break", version="1.0.0", family="trend",
        timeframe="H4", horizon_bars=52, required_history=300,
        lifecycle="hypothesis",
        description="Break of the displaced cloud, confirmed by tenkan/kijun.",
        hypothesis="The cloud is a mid-channel of the previous 52 bars displaced "
                   "forward, so a close beyond it is a breakout of a range that "
                   "was established well before the current bar -- a slower and "
                   "therefore less noise-prone version of a channel break. Any "
                   "edge should come from that, not from the indicator's "
                   "popularity.",
        failure_conditions=[
            "Indistinguishable from a Donchian break of similar length, in which "
            "case it is the same trial counted twice.",
            "The edge disappears when the displacement is varied by +/-30%, "
            "meaning 26 was fitted rather than structural.",
            "Fails Clark-West against a random walk.",
            "Signals cluster in the same weeks as the other trend strategies "
            "(correlation of trade timing above 0.7) -- no diversification.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"tenkan": 9, "kijun": 26, "senkou_b": 52, "displacement": 26,
                "atr_window": 20, "stop_atr": 2.2, "target_atr": 5.0,
                "min_cloud_thickness_atr": 0.0}

    def _validate(self) -> None:
        p = self.params
        if not (p["tenkan"] < p["kijun"] < p["senkou_b"]):
            raise ValueError("Ichimoku windows must be strictly increasing")
        if p["displacement"] < 1:
            raise ValueError("displacement must be at least 1 bar forward")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        ich = ichimoku(df, p["tenkan"], p["kijun"], p["senkou_b"], p["displacement"])
        close = df["close"].astype(float)
        a = atr(df, p["atr_window"])
        ich["atr"] = a
        ich["above_prev"] = (close.shift(1) > ich["cloud_top"].shift(1)).astype(float)
        ich["below_prev"] = (close.shift(1) < ich["cloud_bottom"].shift(1)).astype(float)
        ich["thickness_atr"] = (ich["cloud_top"] - ich["cloud_bottom"]) / a.replace(0.0, np.nan)
        return ich

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["senkou_b"] + p["displacement"] + 10:
            return None
        f = self.features_at(instrument, df, index)
        top, bottom = float(f["cloud_top"]), float(f["cloud_bottom"])
        tenkan, kijun, a = float(f["tenkan"]), float(f["kijun"]), float(f["atr"])
        above_prev, below_prev = float(f["above_prev"]), float(f["below_prev"])
        thickness = float(f["thickness_atr"])
        if not finite(top, bottom, tenkan, kijun, a, thickness) or a <= 0:
            return None
        if thickness < p["min_cloud_thickness_atr"]:
            return None

        close = float(df["close"].iloc[index])
        # Entry on the bar that CROSSES the cloud, not on every bar spent beyond
        # it: `above_prev` is computed from the previous bar's close against the
        # previous bar's cloud, both of which are known at this bar.
        if close > top and above_prev < 0.5 and tenkan > kijun:
            side = Side.BUY
        elif close < bottom and below_prev < 0.5 and tenkan < kijun:
            side = Side.SELL
        else:
            return None

        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.35 + 0.15 * thickness)),
            features={"cloud_thickness_atr": thickness, "atr": a,
                      "tenkan_minus_kijun_atr": (tenkan - kijun) / a},
            rationale=f"close broke {'above' if side is Side.BUY else 'below'} a cloud "
                      f"{thickness:.2f}xATR thick, tenkan/kijun agreeing",
        )
