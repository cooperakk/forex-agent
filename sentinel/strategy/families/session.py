"""Session and time-of-day family.

The one thing in this family that is definitely true: FX liquidity is not
uniform across the day. The London open and the London/New York overlap carry
most of the volume, the Asian session is quieter, and the spread at 22:00 UTC
is several times the spread at 09:00. A strategy that trades a fixed rule
around the clock is averaging over regimes that differ by more than most of the
effects in this library.

What does NOT follow is that any particular hour has a directional edge. The
session strategies below are all conditional versions of rules that appear
elsewhere in the library, and each of them is at risk of being a story fitted
to a handful of hours. ``day_of_week`` goes further and is included precisely
because it has no mechanism: it belongs in the ledger as a measured trial
rather than in a forum post as folklore.

A note on granularity that applies to the whole module: on H4 bars there are
six bars a day, so "the London session" is one or two bars. These strategies
declare intraday timeframes and will produce few or no signals on coarse data,
which is the correct behaviour -- silently becoming a different strategy on
different bar sizes is not.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ...core.types import Side, Signal
from ..base import (
    Strategy, StrategyMeta, atr, ema, opening_range, session_of, zscore,
)
from ._common import atr_signal, finite


def _hour_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Hour, weekday and session label per bar. Pure functions of the index."""
    idx = pd.DatetimeIndex(df.index)
    return pd.DataFrame({
        "hour": idx.hour.astype(float),
        "weekday": idx.dayofweek.astype(float),
        "session": pd.Series([session_of(ts) for ts in idx], index=df.index),
    }, index=df.index)


class LondonOpenMomentum(Strategy):
    """Take the direction of the first London bars, inside London only.

    London is where the overnight order book is cleared. If the accumulated
    flow has a direction, the first hours of the session should show it and the
    remainder of the session should continue it.
    """

    meta = StrategyMeta(
        name="london_open_momentum", version="1.0.0", family="session",
        timeframe="H1", horizon_bars=8, required_history=200,
        lifecycle="hypothesis",
        description="Continuation of the London open move, within the London session.",
        hypothesis="Orders accumulated during Asian hours by European real-money "
                   "accounts are executed at the London open. Execution of a "
                   "large one-directional flow takes hours, so the first part of "
                   "the session's direction predicts the rest of it.",
        failure_conditions=[
            "No edge after charging the real open-of-session spread.",
            "The effect is present in one year and absent in the next: flow "
            "patterns are not stable across the sample.",
            "Reversal, not continuation, is the more common outcome -- in which "
            "case the flow was finished, not starting.",
            "Result depends on the exact UTC hour boundary used for 'London', "
            "which would mean the session label, not the flow, was the signal.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"lookback_bars": 2, "min_move_atr": 0.5, "max_bars_after": 5,
                "atr_window": 14, "stop_atr": 1.5, "target_atr": 2.5}

    def _validate(self) -> None:
        p = self.params
        if p["lookback_bars"] < 1:
            raise ValueError("the open move needs at least one bar to measure")
        if p["max_bars_after"] <= p["lookback_bars"]:
            raise ValueError("max_bars_after must leave bars in which to trade")
        if p["target_atr"] / p["stop_atr"] < 1.2:
            raise ValueError("reward/risk below 1.2 cannot survive the cost barrier")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        hf = _hour_frame(df)
        hi, lo, pos = opening_range(df, p["lookback_bars"], "london")
        close = df["close"].astype(float)
        a = atr(df, p["atr_window"])
        # Move over the opening window, measured from the close `lookback_bars`
        # ago to the current close. At pos == lookback_bars that window is
        # exactly the completed opening range and nothing later.
        move = (close - close.shift(p["lookback_bars"])) / a.replace(0.0, np.nan)
        return pd.DataFrame({
            "bars_in": pos, "atr": a, "move_atr": move,
            "or_high": hi, "or_low": lo,
            "is_london": (hf["session"] == "london").astype(float),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["atr_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        pos, a, move = float(f["bars_in"]), float(f["atr"]), float(f["move_atr"])
        if float(f["is_london"]) < 1.0:
            return None
        if not finite(pos, a, move) or a <= 0:
            return None
        if pos < p["lookback_bars"] or pos > p["max_bars_after"]:
            return None
        if abs(move) < p["min_move_atr"]:
            return None

        side = Side.BUY if move > 0 else Side.SELL
        close = float(df["close"].iloc[index])
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.35 + 0.25 * abs(move))),
            features={"open_move_atr": move, "bars_since_open": pos, "atr": a},
            rationale=f"London opened {move:+.2f}xATR over {p['lookback_bars']} bars",
        )


class NewYorkOpenContinuation(Strategy):
    """Join the London direction as New York arrives, during the overlap.

    Distinct from the London strategy in what it claims: not that new flow has
    a direction, but that the arrival of the deepest liquidity of the day lets
    an existing European move be completed rather than reversed. The overlap is
    also the cheapest hour to trade, which matters more than usual for a
    strategy with a horizon of hours.
    """

    meta = StrategyMeta(
        name="newyork_open_continuation", version="1.0.0", family="session",
        timeframe="H1", horizon_bars=8, required_history=200,
        lifecycle="hypothesis",
        description="Continuation of the European move into the New York session.",
        hypothesis="US participants arriving into an established European move "
                   "add liquidity on the same side rather than fading it, because "
                   "the flow driving the move is executed against the deepest "
                   "book of the day. The overlap's tight spread makes the "
                   "continuation cheaper to trade than the same rule elsewhere.",
        failure_conditions=[
            "The overlap systematically reverses the European move instead -- a "
            "documented pattern on some pairs, and the direct opposite of this "
            "hypothesis.",
            "No edge once the spread at the actual entry hour is charged.",
            "Result driven entirely by days with scheduled US data, which is a "
            "news strategy, not a session one.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"lookback_bars": 4, "min_move_atr": 0.7, "atr_window": 14,
                "stop_atr": 1.5, "target_atr": 2.6, "sessions": ("overlap",)}

    def _validate(self) -> None:
        p = self.params
        if p["lookback_bars"] < 1:
            raise ValueError("the prior move needs at least one bar to measure")
        if p["target_atr"] / p["stop_atr"] < 1.2:
            raise ValueError("reward/risk below 1.2 cannot survive the cost barrier")
        for s in p["sessions"]:
            if s not in ("london", "newyork", "asia", "overlap"):
                raise ValueError(f"unknown session {s!r}")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        hf = _hour_frame(df)
        close = df["close"].astype(float)
        a = atr(df, p["atr_window"])
        move = (close - close.shift(p["lookback_bars"])) / a.replace(0.0, np.nan)
        allowed = hf["session"].isin(list(p["sessions"])).astype(float)
        # First bar of the allowed window: the session label changed on this
        # bar. Using the previous bar's label keeps this causal.
        first = (allowed > 0) & (hf["session"].shift(1) != hf["session"])
        return pd.DataFrame({"atr": a, "move_atr": move,
                             "allowed": allowed, "first_bar": first.astype(float)},
                            index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["atr_window"] + p["lookback_bars"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        if float(f["allowed"]) < 1.0 or float(f["first_bar"]) < 1.0:
            return None
        a, move = float(f["atr"]), float(f["move_atr"])
        if not finite(a, move) or a <= 0:
            return None
        if abs(move) < p["min_move_atr"]:
            return None

        side = Side.BUY if move > 0 else Side.SELL
        close = float(df["close"].iloc[index])
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.35 + 0.2 * abs(move))),
            features={"prior_move_atr": move, "atr": a},
            rationale=(f"European move {move:+.2f}xATR carried into the "
                       f"{'/'.join(p['sessions'])} session"),
        )


class AsianRangeFade(Strategy):
    """Fade the edges of the Asian range, inside the Asian session.

    The clearest mechanism in this family. Outside Japanese and Australian data
    releases the Asian session for European crosses is thin and directionless;
    a move to the edge of the session's range is more often an absence of
    liquidity than an arrival of information, and it reverts.

    The same thinness is the reason to be sceptical: thin markets have wide
    spreads, and this strategy's target is small by construction. It is very
    plausible that the pattern is real and untradable, which is a result worth
    having and is exactly what the cost model will show.
    """

    meta = StrategyMeta(
        name="asian_range_fade", version="1.0.0", family="session",
        timeframe="H1", horizon_bars=6, required_history=220,
        lifecycle="hypothesis",
        description="Mean reversion at the extremes of the Asian session range.",
        hypothesis="For European and dollar crosses, Asian-hours order flow is "
                   "thin and largely uninformed. A push to the extreme of the "
                   "session's range is therefore usually a liquidity gap rather "
                   "than repricing, and fades back into the range.",
        failure_conditions=[
            "Negative expectancy after the ASIAN-HOURS spread, which is the one "
            "that applies and is several times the London figure.",
            "Losses concentrated around Tokyo fixes and RBA/BoJ releases: those "
            "excursions are information.",
            "The range breaks and trends more often than the win rate can carry "
            "-- a fade has bounded reward and unbounded regret.",
            "No better than the general z-score reversion strategy restricted to "
            "the same hours, which would make the session framing redundant.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"z_window": 40, "z_entry": 1.8, "atr_window": 14,
                "stop_atr": 1.4, "target_atr": 1.9, "min_bars_into_session": 2}

    def _validate(self) -> None:
        p = self.params
        if p["z_entry"] < 1.0:
            raise ValueError("a z-entry below 1.0 is not an extreme")
        if p["target_atr"] / p["stop_atr"] < 1.2:
            raise ValueError("reward/risk below 1.2 cannot survive the cost barrier")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        hf = _hour_frame(df)
        _, _, pos = opening_range(df, 1, "asia")
        return pd.DataFrame({
            "z": zscore(df["close"], p["z_window"]),
            "atr": atr(df, p["atr_window"]),
            "is_asia": (hf["session"] == "asia").astype(float),
            "bars_in": pos,
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["z_window"] + p["atr_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        if float(f["is_asia"]) < 1.0:
            return None
        z, a, pos = float(f["z"]), float(f["atr"]), float(f["bars_in"])
        if not finite(z, a, pos) or a <= 0:
            return None
        if pos < p["min_bars_into_session"]:
            return None  # the session's own range has not formed yet
        if abs(z) < p["z_entry"]:
            return None

        side = Side.BUY if z < 0 else Side.SELL
        close = float(df["close"].iloc[index])
        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, 0.3 + 0.15 * (abs(z) - p["z_entry"] + 1))),
            features={"zscore": z, "atr": a, "bars_into_session": pos},
            rationale=f"Asian session z={z:+.2f}, {pos:.0f} bars in",
        )


class DayOfWeekEffect(Strategy):
    """A weekday tilt. This one is folklore, and is labelled as such.

    Read the hypothesis field: there is no mechanism here worth the name. It is
    implemented because "Mondays reverse" and "don't trade Fridays" are among
    the most repeated claims in retail FX, and the cheapest way to deal with an
    untestable claim is to make it testable. Running it costs one trial in the
    ledger and produces a number.

    The expected outcome is failure, and failure is the useful result. If it
    somehow passes every gate in the acceptance protocol, the correct response
    is still suspicion: with five weekdays and two directions there are ten
    variants of this idea, and the trial ledger will charge all of them.
    """

    meta = StrategyMeta(
        name="day_of_week", version="1.0.0", family="session",
        timeframe="D1", horizon_bars=5, required_history=200,
        lifecycle="hypothesis",
        description="Directional tilt on a chosen weekday. Folklore, implemented "
                    "so that it can be falsified cheaply.",
        hypothesis="NONE THAT SURVIVES INSPECTION. There is no credible mechanism "
                   "for a persistent, exploitable weekday effect in the most "
                   "liquid market in the world: any such pattern is visible to "
                   "everyone, costs nothing to trade, and would be arbitraged "
                   "within weeks. The closest thing to a real effect is the "
                   "weekend gap and the Wednesday triple-swap roll, both of which "
                   "are mechanical and already priced. This strategy exists to "
                   "put a number on a claim that is usually asserted, not to "
                   "propose that the claim is true.",
        failure_conditions=[
            "Any of the usual gates -- and it should fail them.",
            "In particular: the deflated Sharpe, because there are ten obvious "
            "variants (five days, two directions) and the ledger counts them all.",
            "Effect changes sign between the first and second half of the sample, "
            "which is what a fitted weekday pattern does.",
            "Effect disappears when the timezone convention for 'day' changes.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"weekday": 0, "direction": 1, "trend_window": 50,
                "atr_window": 14, "stop_atr": 1.8, "target_atr": 3.0,
                "require_trend_agreement": True}

    def _validate(self) -> None:
        p = self.params
        if p["weekday"] not in (0, 1, 2, 3, 4):
            raise ValueError("weekday must be Monday..Friday (0..4); the FX week "
                             "does not trade on 5 or 6")
        if p["direction"] not in (1, -1):
            raise ValueError("direction must be +1 or -1")
        if p["target_atr"] <= p["stop_atr"]:
            raise ValueError("target must exceed stop")

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        hf = _hour_frame(df)
        trend = ema(df["close"], p["trend_window"])
        return pd.DataFrame({
            "weekday": hf["weekday"], "trend": trend,
            "atr": atr(df, p["atr_window"]),
            # First bar of the weekday: on daily data every bar is; on intraday
            # data this keeps the tilt from firing on every bar of the day.
            "first_of_day": (hf["weekday"] != hf["weekday"].shift(1)).astype(float),
        }, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        p = self.params
        df = data[instrument]
        if index < p["trend_window"] + p["atr_window"] + 5:
            return None
        f = self.features_at(instrument, df, index)
        wd, trend, a = float(f["weekday"]), float(f["trend"]), float(f["atr"])
        if not finite(wd, trend, a) or a <= 0:
            return None
        if int(wd) != int(p["weekday"]) or float(f["first_of_day"]) < 1.0:
            return None

        side = Side.BUY if p["direction"] > 0 else Side.SELL
        close = float(df["close"].iloc[index])
        if p["require_trend_agreement"]:
            # Even folklore gets a trend filter, so that what is measured is the
            # weekday tilt on top of a known effect rather than a blind coin flip
            # -- and so that a failure cannot be blamed on the absence of one.
            if (side is Side.BUY) != (close > trend):
                return None

        return atr_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            atr_value=a, stop_atr=p["stop_atr"], target_atr=p["target_atr"],
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=0.3,
            features={"weekday": wd, "atr": a},
            rationale=(f"weekday tilt (day {int(wd)}, direction "
                       f"{'long' if side is Side.BUY else 'short'}) -- folklore "
                       "under test"),
        )
