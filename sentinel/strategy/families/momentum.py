"""Momentum family.

The best-documented anomaly in FX, and the one with the strongest reason to be
suspicious of it. Menkhoff, Sarno, Schmeling and Schrimpf (2011) establish
cross-sectional currency momentum; Moskowitz, Ooi and Pedersen (2012) establish
the time-series version across asset classes including FX. Both papers' samples
end around 2010.

That matters more here than anywhere else in the library. Post-publication
decay is the norm rather than the exception, the effect was always concentrated
in less liquid currencies with higher transaction costs, and fifteen years have
passed. Every strategy in this family therefore carries the same failure
condition: absent in the most recent third of the sample.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ...core.types import Side, Signal
from ..base import Strategy, StrategyMeta, atr, realised_vol
from ._common import atr_signal, finite


class CrossSectionalMomentum(Strategy):
    """Rank currencies by past return; long the winners, short the losers.

    A direct reconstruction of Menkhoff et al. (2011) at a tradable horizon.
    The brief's warning applies in full: the published evidence ends around
    2010, the sample is 15+ years stale, and post-publication decay is the
    norm, not the exception. This exists to be tested, not to be trusted.
    """

    meta = StrategyMeta(
        name="xs_momentum", version="1.0.0", family="momentum",
        timeframe="D1", horizon_bars=20,
        required_history=300, lifecycle="hypothesis",
        description="Cross-sectional currency momentum over a formation window.",
        hypothesis="Currencies that outperformed over the formation window "
                   "continue to outperform over the holding window.",
        failure_conditions=[
            "No significant alpha after the dollar and carry controls.",
            "Turnover cost exceeds the gross spread at realistic frequency.",
            "Effect absent in the most recent third of the sample "
            "(post-publication decay).",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"formation": 60, "skip": 1, "top_n": 1, "atr_window": 20,
                "stop_atr": 2.5, "target_atr": 5.0, "min_universe": 4}

    def _validate(self) -> None:
        p = self.params
        if p["skip"] >= p["formation"]:
            raise ValueError("skip must be shorter than the formation window")
        if p["top_n"] < 1:
            raise ValueError("top_n must select at least one instrument")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop: a sub-1R target cannot clear cost")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        logc = np.log(df["close"].astype(float))
        # Formation return ending `skip` bars ago, so the most recent bar's
        # short-term reversal does not contaminate the score.
        score = logc.shift(p["skip"]) - logc.shift(p["skip"] + p["formation"])
        return pd.DataFrame({"atr": atr(df, p["atr_window"]), "score": score}, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        if index < p["formation"] + p["skip"] + 5:
            return None
        scores: Dict[str, float] = {}
        for sym, d in data.items():
            if len(d) <= index:
                continue
            v = float(self.features_at(sym, d, index)["score"])
            if np.isfinite(v):
                scores[sym] = v
        if instrument not in scores or len(scores) < p["min_universe"]:
            return None

        ranked = sorted(scores, key=scores.get, reverse=True)
        top = set(ranked[: p["top_n"]])
        bottom = set(ranked[-p["top_n"]:])
        if instrument in top and instrument not in bottom:
            side = Side.BUY
        elif instrument in bottom and instrument not in top:
            side = Side.SELL
        else:
            return None

        a = float(self.features_at(instrument, data[instrument], index)["atr"])
        if not np.isfinite(a) or a <= 0:
            return None
        close = float(data[instrument]["close"].iloc[index])
        sign = 1 if side is Side.BUY else -1
        spread = max(scores.values()) - min(scores.values())
        strength = float(min(1.0, 0.4 + 2.0 * abs(scores[instrument]) / (spread + 1e-9) * 0.3))
        return Signal(
            strategy=self.meta.name, instrument=instrument, side=side, strength=strength,
            stop_price=close - sign * p["stop_atr"] * a,
            target_price=close + sign * p["target_atr"] * a,
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            features={"momentum_score": scores[instrument], "rank": float(ranked.index(instrument)),
                      "universe": float(len(scores))},
            rationale=(f"rank {ranked.index(instrument) + 1}/{len(ranked)} over a "
                       f"{p['formation']}-bar formation window"),
        )


class TimeSeriesMomentum(Strategy):
    """Own past return, with a skip, decides the side. No cross-section.

    The skip window is not a detail. At short horizons the immediate past is
    dominated by reversal -- bid/ask bounce and inventory effects -- so the
    classic 12-1 construction deliberately drops the most recent month. Leaving
    it in is the single most common way to turn a momentum study into a noise
    study, and it makes the result look better in sample because the reversal
    is real.
    """

    meta = StrategyMeta(
        name="ts_momentum", version="1.0.0", family="momentum",
        timeframe="D1", horizon_bars=25, required_history=320,
        lifecycle="hypothesis",
        description="Time-series momentum over a formation window with a skip.",
        hypothesis="Investors under-react to gradual information about relative "
                   "monetary policy, and institutional flows are executed over "
                   "weeks rather than at once, so the sign of an exchange rate's "
                   "own trailing return carries information about its next move "
                   "independently of how other currencies are doing.",
        failure_conditions=[
            "Effect absent in the most recent third of the sample.",
            "Sign of the result flips when the skip window is removed -- the "
            "strategy was trading short-horizon reversal, not momentum.",
            "No alpha after the dollar factor: with USD on one side of every "
            "major, a time-series signal is mostly a dollar view.",
            "Volatility scaling of the entry does all the work (compare against "
            "the unscaled version).",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"formation": 120, "skip": 10, "min_abs_move_vol": 1.0,
                "vol_window": 60, "atr_window": 20,
                "stop_atr": 2.5, "target_atr": 5.0}

    def _validate(self) -> None:
        p = self.params
        if p["formation"] < 10:
            raise ValueError("a formation window under 10 bars measures noise")
        if p["skip"] < 0:
            raise ValueError("skip cannot be negative")
        if p["skip"] >= p["formation"]:
            raise ValueError("skip must be shorter than the formation window")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        logc = np.log(df["close"].astype(float))
        score = logc.shift(p["skip"]) - logc.shift(p["skip"] + p["formation"])
        vol = realised_vol(df["close"], p["vol_window"])
        # Score expressed in units of the instrument's own volatility over the
        # formation window, so the threshold means the same thing on EUR/USD
        # and USD/JPY.
        scaled = score / (vol.replace(0.0, np.nan) * np.sqrt(p["formation"]))
        return pd.DataFrame({"score": score, "scaled": scaled, "vol": vol,
                             "atr": atr(df, p["atr_window"])}, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["formation"] + p["skip"] + p["vol_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        scaled, a = float(f["scaled"]), float(f["atr"])
        if not finite(scaled, a) or a <= 0:
            return None
        if abs(scaled) < p["min_abs_move_vol"]:
            return None

        side = Side.BUY if scaled > 0 else Side.SELL
        close = float(df["close"].iloc[index])
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.35 + 0.2 * abs(scaled))),
            features={"ts_momentum_vol_units": scaled, "atr": a},
            rationale=(f"{p['formation']}-bar return (skipping {p['skip']}) is "
                       f"{scaled:+.2f} volatility units"),
        )


class DualMomentum(Strategy):
    """Require BOTH absolute and relative momentum to agree.

    Antonacci's construction. The economic argument is that relative momentum
    picks the strongest candidate while absolute momentum keeps you out of a
    market where everything is falling -- the second filter is what turned a
    long-only equity rotation into something with a survivable drawdown.

    In FX the argument is weaker and should be stated as such: there is no
    "everything falls" state, because a currency pair is a relative price by
    construction. What absolute momentum adds here is a filter against taking
    the best of a set of uniformly directionless markets, which is a real but
    much smaller benefit than in its original application.
    """

    meta = StrategyMeta(
        name="dual_momentum", version="1.0.0", family="momentum",
        timeframe="D1", horizon_bars=25, required_history=320,
        lifecycle="hypothesis",
        description="Cross-sectional rank confirmed by the instrument's own trend.",
        hypothesis="Combining the two momentum forms removes the trades where a "
                   "currency is merely the least bad of a directionless set. The "
                   "cross-section chooses, the absolute filter vetoes.",
        failure_conditions=[
            "No improvement over cross-sectional momentum alone, in which case "
            "this is the same trial wearing a second name and should be dropped "
            "from the library rather than kept as a variant.",
            "Number of trades falls so far that the sample cannot reach the "
            "MinTRL required to call the Sharpe positive.",
            "Effect absent in the most recent third of the sample.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"formation": 90, "skip": 5, "absolute_window": 60, "top_n": 1,
                "atr_window": 20, "stop_atr": 2.5, "target_atr": 5.0,
                "min_universe": 3}

    def _validate(self) -> None:
        p = self.params
        if p["top_n"] < 1:
            raise ValueError("top_n must select at least one instrument")
        if p["skip"] >= p["formation"]:
            raise ValueError("skip must be shorter than the formation window")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        logc = np.log(df["close"].astype(float))
        rel = logc.shift(p["skip"]) - logc.shift(p["skip"] + p["formation"])
        absolute = logc - logc.shift(p["absolute_window"])
        return pd.DataFrame({"rel": rel, "abs": absolute,
                             "atr": atr(df, p["atr_window"])}, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        need = max(p["formation"] + p["skip"], p["absolute_window"]) + 5
        if index < need:
            return None
        scores: Dict[str, float] = {}
        for sym, d in data.items():
            if len(d) <= index:
                continue
            v = float(self.features_at(sym, d, index)["rel"])
            if np.isfinite(v):
                scores[sym] = v
        if instrument not in scores or len(scores) < p["min_universe"]:
            return None

        ranked = sorted(scores, key=scores.get, reverse=True)
        f = self.features_at(instrument, data[instrument], index)
        absolute, a = float(f["abs"]), float(f["atr"])
        if not finite(absolute, a) or a <= 0:
            return None

        top = set(ranked[: p["top_n"]])
        bottom = set(ranked[-p["top_n"]:])
        # Both conditions, or nothing. Relaxing this to "either" turns the
        # strategy back into cross-sectional momentum with extra steps.
        if instrument in top and instrument not in bottom and absolute > 0:
            side = Side.BUY
        elif instrument in bottom and instrument not in top and absolute < 0:
            side = Side.SELL
        else:
            return None

        close = float(data[instrument]["close"].iloc[index])
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.4 + 0.2 * abs(absolute) / (a / close + 1e-12) * 0.01)),
            features={"relative_score": scores[instrument], "absolute_score": absolute,
                      "rank": float(ranked.index(instrument)), "atr": a},
            rationale=(f"rank {ranked.index(instrument) + 1}/{len(ranked)} and own "
                       f"{p['absolute_window']}-bar return {absolute:+.4f} agreeing"),
        )
