"""Gap stress: what the book loses if a currency jumps THROUGH every stop.

A stop-loss bounds the loss of a market that trades through it. It does not
bound the loss of a market that GAPS past it, and FX has done exactly that
often enough to have names:

* **15 Jan 2015, the Swiss franc.** The SNB removed the EUR/CHF 1.20 floor
  without warning; EUR/CHF traded from 1.20 to about 0.85 within minutes
  (roughly -30%) with almost no prices in between. Stops were filled hundreds
  of pips away. Retail clients ended with NEGATIVE balances; FXCM needed a
  $300m rescue, and Alpari (UK) and Excel Markets went insolvent the same week.
* **7 Oct 2016, the pound "flash crash".** GBP/USD fell about 6% in two
  minutes of thin Asian liquidity (some venues printed near -9%).
* **3 Jan 2019, the yen flash crash.** AUD/JPY fell about 7% and USD/JPY
  about 4% in minutes, again in a holiday-thinned Asian session.
* **23 Apr 2017, the French election.** EUR/USD opened the week about 2%
  above Friday's close -- the largest weekend gap in the most liquid pair.
* **Weekend gaps** (elections, referendums, surprise policy) routinely open a
  percent or more away from Friday's close; the engine already flattens
  before the weekend, and this covers what can still happen intraday.

What the default budget means in practice: at 25% of equity and a 2% EUR/USD
scenario, the book may hold about 12x equity in EUR/USD notional (a $100
account: one 0.01-lot position), about 3.5x in yen crosses and under 1x in
franc crosses. That is the price of surviving a 2015.

The sizing layer already bounds each trade's loss AT ITS STOP. This module
asks the other question: if the worst historically-observed gap hit every
open position and the new one at once, what fraction of equity would be
lost? If the answer exceeds ``stress_loss_limit_pct``, the new position is
SHRUNK until the book fits, and refused only if even the minimum lot does
not. A gap-stress budget is how a stop-based system stays solvent through the
days its stops do not work.

The scenario table is per currency: a position's gap is the larger of its two
legs' gaps (a EUR/CHF position is a CHF bet as far as 2015 is concerned).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ..core.money import D, Instrument, dec
from ..core.types import Position

ZERO = D("0")

#: Documented worst gaps, as a fraction of price (see the module docstring).
#: "*" is the default for any currency not listed.
DEFAULT_SCENARIOS: Dict[str, float] = {
    "CHF": 0.30,       # SNB, 15 Jan 2015
    "GBP": 0.09,       # flash crash, 7 Oct 2016 (widest venue prints)
    "JPY": 0.07,       # flash crash, 3 Jan 2019 (AUD/JPY)
    "AUD": 0.07,       # same event
    "NZD": 0.06,
    "EUR": 0.02,       # French election weekend gap, 23 Apr 2017
    "USD": 0.02,
    "TRY": 0.20, "ZAR": 0.10, "MXN": 0.10, "RUB": 0.30,   # emerging-market episodes
    "XAU": 0.06, "XAG": 0.10,
    "*": 0.03,
}


def scenario_gap(instrument: Instrument, scenarios: Mapping[str, float]) -> Decimal:
    default = float(scenarios.get("*", 0.03))
    worst = max(float(scenarios.get(instrument.base, default)),
                float(scenarios.get(instrument.quote, default)))
    return dec(max(0.0, min(1.0, worst)))


def position_notional(instrument: Instrument, lots: Decimal, mid: Decimal,
                      conversion: Optional[Decimal]) -> Optional[Decimal]:
    """Notional in the account currency, or None when it cannot be priced."""
    if conversion is None or dec(conversion) <= 0 or mid is None or dec(mid) <= 0:
        return None
    return instrument.units(lots) * dec(mid) * dec(conversion)


def book_stress_loss(positions: Sequence[Position], instruments: Dict[str, Instrument],
                     mids: Mapping[str, Decimal], conversions: Mapping[str, Decimal],
                     account_currency: str, scenarios: Mapping[str, float]
                     ) -> Tuple[Decimal, List[str]]:
    """(stress loss in account currency, instruments that could not be priced)."""
    total = ZERO
    unpriced: List[str] = []
    for p in positions:
        inst = instruments.get(p.instrument)
        if inst is None:
            unpriced.append(p.instrument)
            continue
        conv = D("1") if inst.quote == account_currency else conversions.get(inst.quote)
        notional = position_notional(inst, p.lots, mids.get(p.instrument, p.entry_price), conv)
        if notional is None:
            unpriced.append(p.instrument)
            continue
        total += notional * scenario_gap(inst, scenarios)
    return total, unpriced


def max_lots_within(budget: Decimal, instrument: Instrument, mid: Decimal,
                    conversion: Decimal, scenarios: Mapping[str, float]) -> Decimal:
    """Largest position whose stress loss fits in ``budget`` (never negative)."""
    one_lot = position_notional(instrument, D("1"), mid, conversion)
    if one_lot is None or one_lot <= 0:
        return ZERO
    per_lot = one_lot * scenario_gap(instrument, scenarios)
    if per_lot <= 0:
        return D("1e9")
    return max(ZERO, budget / per_lot)
