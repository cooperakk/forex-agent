"""Helpers shared by the family modules.

Two jobs, both of them about failure modes rather than convenience.

**Every signal carries a stop.** ``RiskEngine.evaluate_entry`` vetoes an entry
with no stop, so a strategy that forgets one produces nothing but veto counts
-- a silent, confusing zero. Worse is a stop that exists but sits *at* the
entry: the intent then fails ``stop < target`` validation deep in the order
path, or, if it survives rounding, asks the sizing code to divide by a zero
risk distance. Both helpers below refuse to build such a signal and return
``None`` instead, which is the outcome the bar loop already handles.

**Reward has to clear cost.** A stop and target are not free parameters. With a
round-trip cost of ``c`` pips the break-even win rate is ``(S + c) / (T + S)``,
so a 0.8R target needs to be right about 58% of the time before costs and more
after. ``min_reward_risk`` is enforced here rather than left to each strategy,
because the family that gets this wrong (mean reversion into a nearby mean) is
exactly the family where it is tempting to skip the check.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np

from ...core.types import Side, Signal


def atr_signal(
    *,
    strategy: str,
    instrument: str,
    side: Side,
    close: float,
    atr_value: float,
    stop_atr: float,
    target_atr: float,
    horizon_bars: int,
    timeframe: str,
    strength: float,
    features: Optional[Dict[str, float]] = None,
    rationale: str = "",
) -> Optional[Signal]:
    """Signal with an ATR-proportional stop and target, or ``None``.

    ATR-proportional rather than fixed-pip because a 20-pip stop is a wide stop
    on EUR/USD in a quiet week and a tick of noise on USD/JPY around a Bank of
    Japan meeting. Fixed distances make a strategy's risk a function of which
    instrument and which decade it is looking at.
    """
    if not np.isfinite(atr_value) or atr_value <= 0:
        return None
    if not np.isfinite(close) or close <= 0:
        return None
    sign = side.sign
    stop = close - sign * stop_atr * atr_value
    target = close + sign * target_atr * atr_value
    if stop <= 0:
        # A stop through zero is arithmetically possible on a low-priced pair
        # with a wide ATR and means the distance is nonsense, not that the trade
        # is attractive.
        return None
    return Signal(
        strategy=strategy, instrument=instrument, side=side,
        strength=float(np.clip(strength, 0.0, 1.0)),
        stop_price=stop, target_price=target,
        horizon_bars=horizon_bars, timeframe=timeframe,
        features={k: float(v) for k, v in (features or {}).items()
                  if np.isfinite(v)},
        calibrated=False, rationale=rationale,
    )


def level_signal(
    *,
    strategy: str,
    instrument: str,
    side: Side,
    close: float,
    stop_price: float,
    target_price: float,
    horizon_bars: int,
    timeframe: str,
    strength: float,
    min_reward_risk: float = 1.2,
    features: Optional[Dict[str, float]] = None,
    rationale: str = "",
) -> Optional[Signal]:
    """Signal with explicit stop and target LEVELS, or ``None`` if they are unusable.

    Used where the levels come from structure -- a band mid, a Supertrend line,
    the other side of an opening range -- rather than from a multiple of ATR.
    Structure-derived levels need three checks that ATR-derived ones get for
    free: the stop must be on the losing side of the entry, the target on the
    winning side, and the ratio between them must clear ``min_reward_risk``.

    The third check is the one that matters. A fade back to a mean two pips
    away, with a stop forty pips out, is a perfectly good-looking rule and a
    guaranteed loser; it is refused here rather than discovered in the equity
    curve.
    """
    for v in (close, stop_price, target_price):
        if not np.isfinite(v) or v <= 0:
            return None
    sign = side.sign
    risk = (close - stop_price) * sign
    reward = (target_price - close) * sign
    if risk <= 0 or reward <= 0:
        return None
    if reward / risk < min_reward_risk:
        return None
    return Signal(
        strategy=strategy, instrument=instrument, side=side,
        strength=float(np.clip(strength, 0.0, 1.0)),
        stop_price=stop_price, target_price=target_price,
        horizon_bars=horizon_bars, timeframe=timeframe,
        features={k: float(v) for k, v in (features or {}).items()
                  if np.isfinite(v)},
        calibrated=False, rationale=rationale,
    )


def finite(*values: float) -> bool:
    """All values present and finite. The warmup guard every strategy needs."""
    return all(np.isfinite(v) for v in values)
