"""Money, instruments and the cost arithmetic that decides whether a strategy
is even *allowed* to exist.

Design rules enforced here:

* Money is ``Decimal``. Binary floats accumulate error across thousands of
  fills; a P&L that disagrees with the broker statement destroys every
  downstream statistic. Prices stay ``Decimal`` too, quantised to the
  instrument's own tick.
* Rounding of trade size is always **toward less risk** (ROUND_DOWN), and the
  resulting *rounding error* is reported, not swallowed. When the error is
  large the position is refused, because a 0.01-lot floor on a small account
  silently turns a 1% risk budget into a 3% one.
* Break-even win rate is a first-class function. Section B-3 of the research
  brief is the reason this module exists at all: at a 3-pip target with a
  1-pip round trip you need 66.7% accuracy merely to break even, which closes
  the whole scalping family before a single line of strategy code is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from enum import Enum
from typing import Optional

D = Decimal
ZERO = D("0")


def dec(value) -> Decimal:
    """Coerce to Decimal without ever going through binary float text.

    numpy scalars need care: in numpy 2.x ``repr(np.float64(1.085))`` is
    ``'np.float64(1.085)'``, which Decimal cannot parse. ``float(...)`` first,
    then ``repr``, which keeps the shortest round-trippable form (0.1 stays
    "0.1" rather than becoming 0.1000000000000000055511151231257827).
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"cannot use non-finite Decimal {value!r} as a monetary value")
        return value
    if isinstance(value, bool):
        raise ValueError("refusing to convert a bool to a monetary value")
    if isinstance(value, int):
        return Decimal(int(value))
    if isinstance(value, float):
        # np.float64 IS a subclass of float, and its repr() in numpy 2.x is
        # "np.float64(1.085)". float() first normalises it.
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"cannot convert non-finite value {value!r} to Decimal")
        return Decimal(repr(v))
    # numpy scalars, Fractions, and anything else exposing __float__
    if hasattr(value, "item"):
        try:
            return dec(value.item())
        except (AttributeError, TypeError, ValueError):
            pass
    if isinstance(value, str):
        try:
            return dec(Decimal(value.strip()))
        except InvalidOperation as exc:
            raise ValueError(f"cannot convert {value!r} to Decimal") from exc
    try:
        return dec(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError) as exc:
        try:
            return dec(float(value))
        except (TypeError, ValueError):
            raise ValueError(f"cannot convert {value!r} to Decimal") from exc


def quantize(value: Decimal, exp: Decimal, rounding=ROUND_HALF_EVEN) -> Decimal:
    """Snap ``value`` to the grid ``exp``.

    A grid is a STEP, not a number of decimals: an index CFD ticks in 0.25 and
    a lot step can be 0.05, and ``Decimal.quantize`` only knows about powers of
    ten -- it would put 1.30 on a 0.25 grid. Divide, round to an integer count
    of steps, multiply back, then quantize to the step's own precision so the
    representation is canonical.
    """
    value, exp = dec(value), dec(exp)
    if exp <= 0:
        raise ValueError("a price or lot grid must be positive")
    with localcontext() as ctx:
        ctx.prec = 34
        steps = (value / exp).to_integral_value(rounding=rounding)
        return (steps * exp).quantize(exp)


class AssetClass(str, Enum):
    FX_SPOT = "fx_spot"
    FX_CFD = "fx_cfd"
    METAL = "metal"
    INDEX_CFD = "index_cfd"
    CRYPTO = "crypto"


@dataclass(frozen=True)
class Instrument:
    """Contract specification.

    ``asset_class`` is kept explicit because spot, CFD and futures on the same
    currency pair have different financing, tax and counterparty treatment and
    must never be pooled in one P&L series.
    """

    symbol: str                 # canonical: EUR_USD
    base: str                   # EUR
    quote: str                  # USD
    asset_class: AssetClass = AssetClass.FX_CFD
    pip: Decimal = D("0.0001")  # 1 pip in price terms
    tick: Decimal = D("0.00001")  # minimum price increment
    contract_size: Decimal = D("100000")  # units per 1.0 lot
    min_lot: Decimal = D("0.01")
    lot_step: Decimal = D("0.01")
    max_lot: Decimal = D("100")
    margin_rate: Decimal = D("0.033")  # 1:30 retail default
    venue: str = "sim"

    def __post_init__(self) -> None:
        for name in ("pip", "tick", "contract_size", "min_lot", "lot_step", "max_lot"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{self.symbol}: {name} must be > 0")
        if self.lot_step > self.min_lot:
            raise ValueError(f"{self.symbol}: lot_step cannot exceed min_lot")
        if self.pip < self.tick:
            raise ValueError(f"{self.symbol}: pip cannot be smaller than tick")

    @property
    def pip_decimals(self) -> int:
        return max(0, -self.pip.as_tuple().exponent)

    @property
    def price_decimals(self) -> int:
        return max(0, -self.tick.as_tuple().exponent)

    def round_price(self, price) -> Decimal:
        return quantize(dec(price), self.tick)

    def price_to_pips(self, delta) -> Decimal:
        return dec(delta) / self.pip

    def pips_to_price(self, pips) -> Decimal:
        return dec(pips) * self.pip

    def units(self, lots) -> Decimal:
        return dec(lots) * self.contract_size

    def round_lots_down(self, lots) -> Decimal:
        """Round toward zero on the lot grid. Never rounds risk upward."""
        l = dec(lots)
        if l <= 0:
            return ZERO
        steps = (l / self.lot_step).to_integral_value(rounding=ROUND_DOWN)
        return quantize(steps * self.lot_step, self.lot_step)

    def pip_value_quote(self, lots) -> Decimal:
        """Value of one pip, expressed in the QUOTE currency."""
        return self.units(lots) * self.pip


@dataclass(frozen=True)
class ConversionRate:
    """Quote-currency -> account-currency conversion, with provenance.

    A missing conversion rate is an error, never a silent 1.0. Substituting
    1.0 for an unknown rate is how a JPY-quoted risk budget becomes ~150x too
    large.
    """

    pair: str
    rate: Decimal
    as_of_ns: int
    source: str

    def convert(self, amount: Decimal) -> Decimal:
        return amount * self.rate


def pip_value_account(
    instrument: Instrument,
    lots,
    account_ccy: str,
    quote_to_account: Optional[ConversionRate] = None,
) -> Decimal:
    """Pip value in the *account* currency."""
    v = instrument.pip_value_quote(lots)
    if instrument.quote == account_ccy:
        return v
    if quote_to_account is None:
        raise ValueError(
            f"conversion {instrument.quote}->{account_ccy} required for "
            f"{instrument.symbol}; refusing to assume 1.0"
        )
    return quote_to_account.convert(v)


# --------------------------------------------------------------------------- #
# Cost arithmetic  (research brief, section B-3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CostModel:
    """Realistic round-trip cost for one instrument at one venue.

    ``spread_pips`` should be the *session-appropriate* spread, not the
    marketing headline. ``news_spread_multiplier`` models the 2-10x blow-out
    around scheduled releases.
    """

    spread_pips: Decimal = D("0.6")
    commission_per_lot_round_turn: Decimal = D("7.0")  # account currency
    swap_long_per_lot_per_day: Decimal = D("0")
    swap_short_per_lot_per_day: Decimal = D("0")
    news_spread_multiplier: Decimal = D("4.0")
    slippage_pips_median: Decimal = D("0.1")

    def round_trip_pips(
        self,
        instrument: Instrument,
        lots: Decimal,
        pip_val_account: Decimal,
        *,
        during_news: bool = False,
    ) -> Decimal:
        """Total round-trip cost expressed in PIPS of the instrument."""
        if pip_val_account <= 0:
            raise ValueError("pip value must be positive")
        spread = self.spread_pips * (self.news_spread_multiplier if during_news else D("1"))
        commission_pips = (self.commission_per_lot_round_turn * dec(lots)) / pip_val_account
        return spread + commission_pips + (self.slippage_pips_median * D("2"))


def break_even_win_rate(target_pips, stop_pips, cost_pips) -> Decimal:
    """Minimum accuracy required to break even.

    Generalised beyond the symmetric case in the brief: with take-profit ``T``,
    stop ``S`` and round-trip cost ``c`` (all in pips), the expected value is

        p*(T - c) - (1-p)*(S + c) = 0
        =>  p = (S + c) / (T + S)

    For ``T == S`` this reduces to the brief's ``1/2 + c/2T``.
    """
    T, S, c = dec(target_pips), dec(stop_pips), dec(cost_pips)
    if T <= 0 or S <= 0:
        raise ValueError("target and stop must both be positive")
    if (T + S) == 0:
        raise ValueError("degenerate target/stop")
    return (S + c) / (T + S)


def expectancy_pips(win_rate, target_pips, stop_pips, cost_pips) -> Decimal:
    """Expected pips per trade after cost. Negative => do not trade."""
    p = dec(win_rate)
    if not (ZERO <= p <= D("1")):
        raise ValueError("win_rate must be in [0, 1]")
    T, S, c = dec(target_pips), dec(stop_pips), dec(cost_pips)
    return p * (T - c) - (D("1") - p) * (S + c)


# The single tolerance for "can this stop distance be expressed in whole lots?".
# money.min_equity_for_granularity and risk.sizing both consult it, so the
# acceptance gate (L0.2) and the sizing veto quote the SAME minimum equity.
# Previously they used 20% and 25% and disagreed by 300 account units.
LOT_GRANULARITY_TOLERANCE = D("0.20")


def annual_cost_pct_of_equity(
    *, round_trip_cost_bp: Decimal, leverage: Decimal, trades_per_year: int
) -> Decimal:
    """Annual trading cost as a percentage of equity.

    cost% = round_trip_bp/100 * leverage * trades_per_year / 100  ... expressed
    directly as a percent. This is the calculation that turns "10 trades a day
    at 30x" into ~293% of equity paid away per year -- which is why trade
    frequency is a risk limit in ``risk/engine.py`` and not a tuning knob.
    """
    if trades_per_year < 0 or leverage < 0:
        raise ValueError("leverage and trade count must be non-negative")
    bp = dec(round_trip_cost_bp)
    if bp < 0:
        # A negative round-trip cost is a rebate, and this function is a risk
        # guard: guarding against it was omitted while leverage and trade count
        # were both checked, so bp=-5 returned "-150% of equity per year" and
        # any caller treating a low number as safe would have been reassured by
        # a sign error.
        raise ValueError("round_trip_cost_bp must be non-negative")
    return bp * dec(leverage) * D(trades_per_year) / D("100")


def min_equity_for_granularity(stop_pips, risk_fraction, instrument: Instrument,
                               pip_value_per_lot: Decimal,
                               max_rounding_error: Decimal = LOT_GRANULARITY_TOLERANCE) -> Decimal:
    """Smallest account that can express this risk without gross rounding.

    Brief, section B-10: the tradable size is ``V = risk*E / (pipval*S)``. For
    the rounding error to stay under ``max_rounding_error`` the size must be at
    least ``lot_step / max_rounding_error`` lots, hence

        E >= (lot_step / err) * pip_value_per_lot * S / risk_fraction
    """
    S = dec(stop_pips)
    r = dec(risk_fraction)
    if S <= 0 or r <= 0:
        raise ValueError("stop_pips and risk_fraction must be positive")
    if not (ZERO < dec(max_rounding_error) <= D("1")):
        raise ValueError("max_rounding_error must be in (0, 1]")
    min_lots = instrument.lot_step / dec(max_rounding_error)
    return (min_lots * dec(pip_value_per_lot) * S) / r
