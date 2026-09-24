"""Candidate strategies -- compatibility surface over the family package.

The strategies themselves now live in ``sentinel/strategy/families/``, one
module per economic premise. This module re-exports them so that existing
imports (``from sentinel.strategy.library import DonchianTrend``) keep working,
and so that there is still one obvious place to look for "what is in the
library".

Nothing is defined here. If you are adding a strategy, add it to the family
module it belongs to -- ``registry.discover_builtin()`` will find it without
any edit to this file or to the registry, and the trial ledger will charge it
to the right family.

The principle every strategy in the package obeys, restated because it is the
one that matters: each states a hypothesis and the conditions under which it
should be abandoned. Both are stored on the strategy object, shown in the
dashboard, and copied into every research card, so a strategy can be
*falsified* rather than quietly re-tuned. None of these is claimed to be
profitable; they are the first things to test, and the first things to discard.

Every one targets a horizon long enough to clear the cost barrier of section
B-3. Nothing here aims below 10 pips, because that family is closed by
arithmetic before any data is consulted.
"""

from __future__ import annotations

from .families.breakout import OpeningRangeBreakout, PreviousDayBreak, SqueezeBreakout
from .families.carry import CarryMomentum, CarryTilt, CarryVolatilityFiltered
from .families.mean_reversion import (
    BollingerFade, PairsSpreadReversion, RSI2Reversion, VolatilityAdjustedReversion,
)
from .families.momentum import CrossSectionalMomentum, DualMomentum, TimeSeriesMomentum
from .families.pattern import EngulfingWithTrend, FailedBreakoutReversal, InsideBarBreak
from .families.session import (
    AsianRangeFade, DayOfWeekEffect, LondonOpenMomentum, NewYorkOpenContinuation,
)
from .families.trend import (
    ADXTrendFollow, DonchianTrend, IchimokuCloudBreak, KaufmanAdaptiveTrend,
    MovingAverageCrossATR, SupertrendFlip,
)
from .families.volatility import (
    ATRPercentileRegime, RealisedVolatilityRatio, VolatilityTargetedTrend,
)

__all__ = [
    # trend
    "DonchianTrend", "MovingAverageCrossATR", "ADXTrendFollow",
    "KaufmanAdaptiveTrend", "SupertrendFlip", "IchimokuCloudBreak",
    # mean reversion
    "VolatilityAdjustedReversion", "BollingerFade", "RSI2Reversion",
    "PairsSpreadReversion",
    # breakout
    "OpeningRangeBreakout", "SqueezeBreakout", "PreviousDayBreak",
    # momentum
    "CrossSectionalMomentum", "TimeSeriesMomentum", "DualMomentum",
    # carry
    "CarryTilt", "CarryVolatilityFiltered", "CarryMomentum",
    # volatility
    "VolatilityTargetedTrend", "ATRPercentileRegime", "RealisedVolatilityRatio",
    # session / time
    "LondonOpenMomentum", "NewYorkOpenContinuation", "AsianRangeFade",
    "DayOfWeekEffect",
    # pattern
    "InsideBarBreak", "EngulfingWithTrend", "FailedBreakoutReversal",
]
