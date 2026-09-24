"""Mandatory baselines.

A strategy is never evaluated against zero. It is evaluated against these,
because "made money" is not the question -- "made more money than doing
something trivial, after cost" is.

``NoTrade``       the cost of doing nothing is exactly zero. Any strategy whose
                  risk-adjusted return does not clear this is worse than the
                  savings account it was funded from.
``RandomWalk``    the null that has defeated exchange-rate forecasting since
                  Meese and Rogoff (1983). Beating it requires the Clark-West
                  test in ``research/stats.py``, not a nicer equity curve.
``CoinFlip``      random entries with the *same* stop, target, sizing and cost
                  as the candidate. It isolates the contribution of the entry
                  signal from the contribution of the exit and risk rules --
                  and it is remarkable how often the exit rules were doing all
                  the work.
``BuyAndHold``    the passive alternative, with financing charged.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ..core.types import Side, Signal
from .base import Strategy, StrategyMeta, atr


class NoTrade(Strategy):
    meta = StrategyMeta(
        name="baseline_no_trade", family="baseline", description="Never trades. The zero-cost floor.",
        lifecycle="reference", required_history=1,
        hypothesis="Doing nothing has a Sharpe of 0 and a maximum drawdown of 0.",
    )

    def generate(self, data, instrument, index) -> Optional[Signal]:
        return None


class RandomWalk(Strategy):
    """Forecasts zero return. Produces no trades; used as a forecast benchmark."""

    meta = StrategyMeta(
        name="baseline_random_walk", family="baseline",
        description="Tomorrow's price equals today's. The Meese-Rogoff null.",
        lifecycle="reference", required_history=2,
        hypothesis="No model of the exchange rate beats the current price as a "
                   "forecast of the next one, out of sample.",
    )

    def generate(self, data, instrument, index) -> Optional[Signal]:
        return None

    @staticmethod
    def forecast(close: pd.Series, index: int) -> float:
        return 0.0


class CoinFlip(Strategy):
    """Random entries with identical risk mechanics to the candidate."""

    meta = StrategyMeta(
        name="baseline_coin_flip", family="baseline",
        description="Random direction, same stop/target/sizing as the candidate.",
        lifecycle="reference", required_history=30,
        hypothesis="If a candidate cannot beat random entries under the same "
                   "exit and risk rules, the edge is in the rules, not the signal.",
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {"entry_probability": 0.05, "atr_window": 14,
                "stop_atr": 1.5, "target_atr": 3.0, "seed": 20260914}

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self._rng = np.random.default_rng(self.params["seed"])

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"atr": atr(df, self.params["atr_window"])}, index=df.index)

    def generate(self, data, instrument, index) -> Optional[Signal]:
        df = data[instrument]
        if index < self.params["atr_window"] + 2:
            return None
        if self._rng.random() > self.params["entry_probability"]:
            return None
        a = float(self.features_at(instrument, df, index)["atr"])
        if not np.isfinite(a) or a <= 0:
            return None
        close = float(df["close"].iloc[index])
        side = Side.BUY if self._rng.random() < 0.5 else Side.SELL
        sign = 1 if side is Side.BUY else -1
        return Signal(
            strategy=self.meta.name, instrument=instrument, side=side,
            strength=0.5, entry_hint=None,
            stop_price=close - sign * self.params["stop_atr"] * a,
            target_price=close + sign * self.params["target_atr"] * a,
            horizon_bars=self.meta.horizon_bars, calibrated=False,
            rationale="random entry (baseline)",
        )


class BuyAndHold(Strategy):
    meta = StrategyMeta(
        name="baseline_buy_and_hold", family="baseline",
        description="Long from the first bar, financing charged.",
        lifecycle="reference", required_history=2,
        hypothesis="Passive exposure to the base currency.",
    )

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self._entered: set[str] = set()

    def generate(self, data, instrument, index) -> Optional[Signal]:
        if instrument in self._entered or index < 2:
            return None
        self._entered.add(instrument)
        df = data[instrument].iloc[: index + 1]
        close = float(df["close"].iloc[-1])
        return Signal(strategy=self.meta.name, instrument=instrument, side=Side.BUY,
                      strength=0.5, stop_price=close * 0.85, target_price=close * 1.60,
                      horizon_bars=10_000, rationale="passive long")
