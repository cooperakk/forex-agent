"""Mean-reversion family.

The premise is the mirror image of the trend family's, which is why running
both without a regime gate is a reliable way to pay the spread twice: inside a
range, transient order-flow imbalance pushes price away from a local fair value
and it comes back within hours.

Two things are true of this family and of no other:

* **The reward is bounded.** A trend trade can run; a fade ends at the mean. So
  the reward/risk ratio is not a free parameter -- it is dictated by how far
  the mean is -- and every strategy here has to refuse the setups where that
  distance cannot pay for the stop. ``level_signal`` enforces it.
* **The payoff is negatively skewed by construction.** Many small wins, rare
  large losses. That is also the payoff of an unhedged short option, it flatters
  the naive Sharpe, and the probabilistic Sharpe in ``research/stats.py``
  exists to take the flattery back.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ...core.types import Side, Signal
from ..base import (
    Strategy, StrategyMeta, adx, atr, bollinger, rolling_corr, rsi, sma, zscore,
)
from ._common import atr_signal, finite, level_signal


class VolatilityAdjustedReversion(Strategy):
    """Fade an extreme z-score, but only in a confirmed range.

    Mean reversion and trend following are the same trade with opposite signs,
    so running both without a regime gate is a reliable way to pay the spread
    twice. The ADX ceiling here is that gate.
    """

    meta = StrategyMeta(
        name="vol_reversion", version="1.0.0", family="mean_reversion",
        timeframe="H1", horizon_bars=18,
        required_history=200, lifecycle="hypothesis",
        description="Fade a z-score extreme when ADX indicates no trend.",
        hypothesis="Inside a range, order-flow imbalance pushes price away from "
                   "fair value and it reverts within hours.",
        failure_conditions=[
            "Negative expectancy once the realised spread is charged.",
            "Loses in the highest-volatility decile (regime gate not working).",
            "Payoff skew below -1: many small wins and rare large losses.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"z_window": 60, "z_entry": 2.2, "rsi_window": 14,
                "rsi_long_max": 32.0, "rsi_short_min": 68.0,
                "atr_window": 14, "stop_atr": 2.0, "target_atr": 2.6,
                "adx_window": 14, "adx_max": 22.0}

    def _validate(self) -> None:
        if self.params["z_entry"] < 1.0:
            raise ValueError("a z-entry below 1.0 is not an extreme")
        if self.params["target_atr"] / self.params["stop_atr"] < 1.2:
            raise ValueError("reward/risk below 1.2 cannot survive the cost barrier")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        return pd.DataFrame({
            "z": zscore(df["close"], p["z_window"]),
            "atr": atr(df, p["atr_window"]),
            "adx": adx(df, p["adx_window"]),
            "rsi": rsi(df["close"], p["rsi_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        need = max(p["z_window"], p["atr_window"], p["adx_window"]) + 5
        if index < need:
            return None
        f = self.features_at(instrument, df, index)
        z, a = float(f["z"]), float(f["atr"])
        trend, r = float(f["adx"]), float(f["rsi"])
        if not all(np.isfinite(v) for v in (z, a, trend, r)) or a <= 0:
            return None
        if trend > p["adx_max"]:
            return None  # a trend is in force: do not fade it

        side: Optional[Side] = None
        if z <= -p["z_entry"] and r <= p["rsi_long_max"]:
            side = Side.BUY
        elif z >= p["z_entry"] and r >= p["rsi_short_min"]:
            side = Side.SELL
        if side is None:
            return None

        close = float(df["close"].iloc[index])
        sign = 1 if side is Side.BUY else -1
        strength = float(min(1.0, 0.35 + 0.15 * (abs(z) - p["z_entry"] + 1)))
        return Signal(
            strategy=self.meta.name, instrument=instrument, side=side, strength=strength,
            stop_price=close - sign * p["stop_atr"] * a,
            target_price=close + sign * p["target_atr"] * a,
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            features={"zscore": z, "rsi": r, "adx": trend, "atr": a},
            rationale=f"z={z:.2f}, RSI={r:.0f}, ADX={trend:.0f} (range regime)",
        )


class BollingerFade(Strategy):
    """Fade a close outside the band, targeting the band mid.

    The target is structural, not a multiple of ATR: the trade's premise is
    that price returns to its own recent mean, so the mean is where it ends.
    That makes the reward/risk ratio an OUTPUT of the setup rather than a
    parameter, and most setups fail it -- which is the point. A fade with the
    mean three pips away and a stop thirty pips out looks like a high win rate
    and is a guaranteed loser after cost.
    """

    meta = StrategyMeta(
        name="bollinger_fade", version="1.0.0", family="mean_reversion",
        timeframe="H1", horizon_bars=16, required_history=200,
        lifecycle="hypothesis",
        description="Fade a close beyond the Bollinger band back to the mid, in a range.",
        hypothesis="Over a horizon of hours, a close more than k standard "
                   "deviations from the recent mean mostly reflects temporary "
                   "liquidity demand rather than news. If so, the mean is a "
                   "reasonable exit and the excursion beyond the band is a "
                   "reasonable entry.",
        failure_conditions=[
            "Expectancy negative once the realised spread is charged -- the most "
            "likely outcome, because the target is small by construction.",
            "Losses concentrated on days with scheduled macro releases: the "
            "excursions being faded were information, not liquidity.",
            "Skew below -1, or a single loss larger than the sum of 20 wins.",
            "No better than the z-score version, i.e. the band adds nothing.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"window": 20, "k": 2.2, "atr_window": 14, "stop_atr": 1.6,
                "adx_window": 14, "adx_max": 22.0, "min_reward_risk": 1.3}

    def _validate(self) -> None:
        p = self.params
        if p["k"] <= 0:
            raise ValueError("the band multiple must be positive")
        if p["window"] < 5:
            raise ValueError("a band over fewer than 5 bars is not a distribution")
        if p["min_reward_risk"] < 1.0:
            raise ValueError("accepting reward/risk below 1.0 on a bounded target "
                             "guarantees a negative expectancy after cost")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        mid, upper, lower = bollinger(df["close"], p["window"], p["k"])
        return pd.DataFrame({
            "mid": mid, "upper": upper, "lower": lower,
            "atr": atr(df, p["atr_window"]),
            "adx": adx(df, p["adx_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["window"] + p["adx_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        mid, up, lo = float(f["mid"]), float(f["upper"]), float(f["lower"])
        a, dx = float(f["atr"]), float(f["adx"])
        if not finite(mid, up, lo, a, dx) or a <= 0:
            return None
        if dx > p["adx_max"]:
            return None

        close = float(df["close"].iloc[index])
        if close < lo:
            side = Side.BUY
        elif close > up:
            side = Side.SELL
        else:
            return None

        sign = side.sign
        stop = close - sign * p["stop_atr"] * a
        excursion = abs(close - mid) / a
        return level_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            stop_price=stop, target_price=mid,
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.35 + 0.2 * excursion)),
            min_reward_risk=p["min_reward_risk"],
            features={"excursion_atr": excursion, "adx": dx, "atr": a,
                      "band_width_atr": (up - lo) / a},
            rationale=f"close {excursion:.2f}xATR beyond the {p['k']}-sigma band, "
                      f"ADX {dx:.0f}; target is the band mid",
        )


class RSI2Reversion(Strategy):
    """Connors-style short-horizon reversion, filtered by a long trend.

    The published version of this rule is an equity-index strategy, and there
    is a reason it works there: a long-only investor base plus index-level
    liquidity provision means someone is paid to buy two-day weakness. FX has
    no equivalent natural buyer, so the mechanism does NOT transfer intact.
    It is included because the rule is widely copied into FX and deserves to be
    measured rather than assumed, and the expectation should be that it is much
    weaker here than in its original market.
    """

    meta = StrategyMeta(
        name="rsi2_reversion", version="1.0.0", family="mean_reversion",
        timeframe="H1", horizon_bars=12, required_history=260,
        lifecycle="hypothesis",
        description="Very short RSI extreme taken only in the direction of a long average.",
        hypothesis="Short-horizon overreaction: a two-bar collapse inside an "
                   "otherwise intact uptrend is usually liquidation rather than "
                   "information, and is bought back within hours. Transplanted "
                   "from equity indices, where a structural long-only buyer base "
                   "supplies the mechanism; that buyer base does not exist in FX, "
                   "so this is a test of whether the pattern survives without it.",
        failure_conditions=[
            "No edge relative to the same entries without the RSI condition: the "
            "trend filter was doing all the work.",
            "Edge present only before the most recent third of the sample.",
            "Average holding period collapses toward the stop distance, meaning "
            "the exits are stops rather than reversions.",
            "Expectancy below the break-even win rate implied by the spread at "
            "this horizon -- likely, since the horizon is short.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"rsi_window": 2, "entry_low": 8.0, "entry_high": 92.0,
                "trend_window": 200, "atr_window": 14,
                "stop_atr": 2.0, "target_atr": 2.8, "both_sides": True}

    def _validate(self) -> None:
        p = self.params
        if not 0 < p["entry_low"] < p["entry_high"] < 100:
            raise ValueError("entry_low must sit below entry_high inside (0, 100)")
        if p["rsi_window"] < 2:
            raise ValueError("Wilder's RSI needs at least 2 periods")
        if p["target_atr"] / p["stop_atr"] < 1.2:
            raise ValueError("reward/risk below 1.2 cannot survive the cost barrier")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        return pd.DataFrame({
            "rsi": rsi(df["close"], p["rsi_window"]),
            "trend": sma(df["close"], p["trend_window"]),
            "atr": atr(df, p["atr_window"]),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["trend_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        r, trend, a = float(f["rsi"]), float(f["trend"]), float(f["atr"])
        if not finite(r, trend, a) or a <= 0:
            return None

        close = float(df["close"].iloc[index])
        # The trend filter is the difference between "buying weakness" and
        # "catching a falling knife". It is also, honestly, the most likely
        # source of whatever performance this shows.
        if r <= p["entry_low"] and close > trend:
            side = Side.BUY
        elif p["both_sides"] and r >= p["entry_high"] and close < trend:
            side = Side.SELL
        else:
            return None

        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.4 + abs(50.0 - r) / 120.0)),
            features={"rsi": r, "atr": a, "distance_to_trend_atr": (close - trend) / a},
            rationale=f"RSI({p['rsi_window']})={r:.0f} against a "
                      f"{p['trend_window']}-bar trend",
        )


class PairsSpreadReversion(Strategy):
    """Fade the spread between two correlated pairs, not the pair itself.

    The point of a spread trade is that the shared factor -- for the majors,
    overwhelmingly the dollar -- cancels, leaving the relative move. If the two
    legs really are co-moving, the residual is a smaller, faster-reverting
    series than either leg.

    Three honest limitations, all of them load-bearing:

    * **The hedge ratio here is 1:1 in log space.** A fitted beta would be a
      rolling regression, i.e. another estimated parameter to overfit, and the
      z-score normalisation already absorbs a constant volatility difference.
      It does not absorb a time-varying one.
    * **Only one leg is traded.** The system's risk layer nets exposure by
      currency (``risk/exposure.py``); a genuine two-legged spread would need
      the order layer to treat the pair as one position, which it does not.
      What is traded is therefore the signal from a spread, with single-leg
      risk. That is a real weakening of the hypothesis and is stated here
      rather than hidden.
    * **Correlation breaks exactly when it matters.** The trailing correlation
      gate is an estimate that will be highest just before a divergence caused
      by a central bank meeting.
    """

    meta = StrategyMeta(
        name="pairs_spread", version="1.0.0", family="mean_reversion",
        timeframe="H4", horizon_bars=24, required_history=300,
        lifecycle="hypothesis",
        description="Z-score reversion on the log spread between the two most "
                    "correlated instruments in the universe.",
        hypothesis="Two pairs sharing a dominant common factor have a residual "
                   "that is stationary over weeks. When the residual is several "
                   "standard deviations from its own mean, the cheaper leg has "
                   "been oversold relative to the more expensive one for reasons "
                   "that are usually flow rather than information.",
        failure_conditions=[
            "The spread is not stationary: an ADF test on the residual fails, or "
            "the z-score spends long runs beyond the entry threshold.",
            "Losses cluster around central bank meetings for either leg -- the "
            "common factor stopped being common.",
            "Single-leg risk dominates: the realised correlation of the trade's "
            "P&L with the traded leg's own return exceeds 0.8, meaning the "
            "spread framing added nothing.",
            "Partner selection flips frequently, which means the correlation "
            "ranking is noise.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"spread_window": 60, "corr_window": 120, "min_corr": 0.55,
                "z_entry": 2.0, "atr_window": 20, "stop_atr": 2.0,
                "target_atr": 2.8, "min_universe": 2}

    def _validate(self) -> None:
        p = self.params
        if p["z_entry"] < 1.0:
            raise ValueError("a z-entry below 1.0 is not an extreme")
        if not 0.0 <= p["min_corr"] <= 1.0:
            raise ValueError("min_corr must be a correlation, i.e. within [0, 1]")
        if p["target_atr"] / p["stop_atr"] < 1.2:
            raise ValueError("reward/risk below 1.2 cannot survive the cost barrier")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"atr": atr(df, self.params["atr_window"]),
                             "logc": np.log(df["close"].astype(float))},
                            index=df.index)

    def prepare(self, data: Dict[str, pd.DataFrame]) -> None:
        """Precompute every pairwise spread ONCE.

        This override exists for a performance reason that is also a
        correctness one: the pairwise z-scores and correlations are rolling
        windows over two series, and computing them inside the bar loop would
        make the backtest O(n^2) and would tempt a future maintainer into
        slicing the frame in a way that leaks. Everything below is computed on
        full series with trailing windows, so bar ``t`` sees only bars <= ``t``.
        """
        super().prepare(data)
        p = self.params
        self._pairs: Dict[tuple, pd.DataFrame] = {}
        symbols = sorted(data)
        for i, a in enumerate(symbols):
            for b in symbols[i + 1:]:
                da, db = data[a], data[b]
                if len(da) < 10 or len(db) < 10:
                    continue
                la = np.log(da["close"].astype(float))
                lb = np.log(db["close"].astype(float)).reindex(la.index)
                spread = la - lb
                mean = spread.rolling(p["spread_window"], min_periods=p["spread_window"]).mean()
                sd = spread.rolling(p["spread_window"],
                                    min_periods=p["spread_window"]).std(ddof=1)
                z = (spread - mean) / sd.replace(0.0, np.nan)
                corr = rolling_corr(da["close"], db["close"], p["corr_window"])
                self._pairs[(a, b)] = pd.DataFrame({"z": z, "corr": corr}, index=la.index)

    def _partner(self, instrument: str, index: int) -> Optional[tuple]:
        """Most correlated counterpart at this bar, with its spread z-score.

        Chosen per bar from TRAILING correlation only. Choosing the partner
        once from full-sample correlation would be look-ahead through the back
        door: the best partner over the whole history is knowledge of the whole
        history.
        """
        best = None
        for (a, b), frame in getattr(self, "_pairs", {}).items():
            if instrument not in (a, b) or len(frame) <= index:
                continue
            row = frame.iloc[index]
            c, z = float(row["corr"]), float(row["z"])
            if not finite(c, z):
                continue
            # The spread is log(a) - log(b); flip the sign when we are the
            # second leg so that a positive z always means "this instrument is
            # rich relative to its partner".
            signed_z = z if instrument == a else -z
            other = b if instrument == a else a
            if best is None or abs(c) > abs(best[1]):
                best = (other, c, signed_z)
        return best

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if len(data) < p["min_universe"]:
            return None
        if index < max(p["spread_window"], p["corr_window"]) + 5:
            return None
        if not getattr(self, "_pairs", None):
            # Not prepared: refuse rather than silently computing a spread on a
            # window, which would be slow and easy to get wrong.
            return None
        found = self._partner(instrument, index)
        if found is None:
            return None
        partner, corr, z = found
        if abs(corr) < p["min_corr"]:
            return None
        if abs(z) < p["z_entry"]:
            return None

        f = self.features_at(instrument, df, index)
        a = float(f["atr"])
        if not finite(a) or a <= 0:
            return None
        # Rich relative to the partner -> sell this leg.
        side = Side.SELL if z > 0 else Side.BUY
        close = float(df["close"].iloc[index])
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.35 + 0.15 * (abs(z) - p["z_entry"] + 1))),
            features={"spread_z": z, "partner_corr": corr, "atr": a},
            rationale=(f"spread against {partner} at z={z:+.2f} with trailing "
                       f"correlation {corr:+.2f}; single-leg execution"),
        )
