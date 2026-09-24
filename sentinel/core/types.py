"""Domain types shared by every layer.

All monetary and price fields are ``Decimal``. All timestamps are integer
nanoseconds UTC. Every record that crosses a boundary carries provenance
(``source``, ``received_ns``) so the dashboard can display where a number came
from and how old it is -- the research brief's "data passport" requirement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from .clock import wall_ns
from .money import D, Instrument, ZERO


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderState(str, Enum):
    """Explicit state machine. UNKNOWN is a first-class state, not an error.

    Legal transitions are enforced in ``execution/oms.py``:

        PENDING -> SENT -> {ACKED -> {FILLED, PARTIAL -> FILLED, CANCELLED},
                            REJECTED, UNKNOWN}
        UNKNOWN -> {FILLED, REJECTED, CANCELLED}   (only via reconciliation)
    """

    PENDING = "pending"
    SENT = "sent"
    ACKED = "acked"
    PARTIAL = "partial"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in (OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED)


class DataQuality(str, Enum):
    OK = "ok"
    STALE = "stale"
    GAP = "gap"
    SUSPECT = "suspect"


@dataclass(frozen=True)
class Quote:
    instrument: str
    bid: Decimal
    ask: Decimal
    ts_ns: int                    # venue timestamp
    received_ns: int = field(default_factory=wall_ns)
    source: str = "unknown"
    liquidity: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if self.ask < self.bid:
            raise ValueError(f"crossed quote on {self.instrument}: ask {self.ask} < bid {self.bid}")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / D("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def spread_pips(self, inst: Instrument) -> Decimal:
        return self.spread / inst.pip

    def age_ns(self, now_ns: Optional[int] = None) -> int:
        return (now_ns or wall_ns()) - self.received_ns

    def price_for(self, side: Side) -> Decimal:
        """Executable price: you buy at the ask, you sell at the bid. Always."""
        return self.ask if side is Side.BUY else self.bid


@dataclass(frozen=True)
class Bar:
    instrument: str
    timeframe: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    start_ns: int
    end_ns: int
    complete: bool = True
    source: str = "unknown"
    quality: DataQuality = DataQuality.OK

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"bar high < low on {self.instrument}@{self.start_ns}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"bar open outside range on {self.instrument}@{self.start_ns}")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"bar close outside range on {self.instrument}@{self.start_ns}")
        if self.end_ns <= self.start_ns:
            raise ValueError("bar end must follow start")


@dataclass
class Signal:
    """A strategy's opinion. Carries no authority to trade."""

    strategy: str
    instrument: str
    side: Optional[Side]
    strength: float                     # [0, 1] -- calibrated or explicitly not
    entry_hint: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    target_price: Optional[Decimal] = None
    horizon_bars: int = 0
    timeframe: str = "H1"
    decision_ns: int = field(default_factory=wall_ns)
    features: Dict[str, float] = field(default_factory=dict)
    rationale: str = ""
    calibrated: bool = False
    data_quality: DataQuality = DataQuality.OK

    def __post_init__(self) -> None:
        if not (0.0 <= self.strength <= 1.0):
            raise ValueError("signal strength must be within [0, 1]")


@dataclass
class OrderIntent:
    """A fully specified, risk-approved instruction, before it reaches a venue."""

    client_order_id: str
    strategy: str
    instrument: str
    side: Side
    lots: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    decision_ns: int = field(default_factory=wall_ns)
    risk_amount: Decimal = ZERO
    risk_pct: Decimal = ZERO
    expected_cost_pips: Decimal = ZERO
    signal_ref: Optional[str] = None
    reason: str = ""
    approvals: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.lots <= 0:
            raise ValueError("order lots must be positive")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("market order must not carry a limit price")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP) and self.limit_price is None:
            raise ValueError(f"{self.order_type.value} order requires a price")
        if self.stop_loss is not None and self.take_profit is not None:
            if self.side is Side.BUY and not (self.stop_loss < self.take_profit):
                raise ValueError("BUY: stop must sit below target")
            if self.side is Side.SELL and not (self.stop_loss > self.take_profit):
                raise ValueError("SELL: stop must sit above target")


@dataclass
class Fill:
    client_order_id: str
    venue_order_id: str
    instrument: str
    side: Side
    lots: Decimal
    price: Decimal
    ts_ns: int                       # venue stamp
    received_ns: int = field(default_factory=wall_ns)
    commission: Decimal = ZERO
    financing: Decimal = ZERO
    slippage_pips: Decimal = ZERO
    liquidity_flag: str = ""


@dataclass
class Order:
    intent: OrderIntent
    state: OrderState = OrderState.PENDING
    # False when the venue filled the order but refused the protective stop.
    stop_confirmed: bool = True
    venue_order_id: Optional[str] = None
    sent_ns: Optional[int] = None
    acked_ns: Optional[int] = None
    fills: List[Fill] = field(default_factory=list)
    reject_reason: Optional[str] = None
    attempts: int = 0
    last_error: Optional[str] = None

    @property
    def filled_lots(self) -> Decimal:
        return sum((f.lots for f in self.fills), ZERO)

    @property
    def avg_fill_price(self) -> Optional[Decimal]:
        total = self.filled_lots
        if total <= 0:
            return None
        return sum((f.price * f.lots for f in self.fills), ZERO) / total

    @property
    def latency_ms(self) -> Optional[float]:
        if self.sent_ns and self.acked_ns:
            return (self.acked_ns - self.sent_ns) / 1_000_000
        return None


@dataclass
class Position:
    instrument: str
    side: Side
    lots: Decimal
    entry_price: Decimal
    opened_ns: int
    strategy: str = ""
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    broker_stop_confirmed: bool = False
    client_order_id: Optional[str] = None
    venue_position_id: Optional[str] = None
    realised_pnl: Decimal = ZERO
    financing_paid: Decimal = ZERO
    commission_paid: Decimal = ZERO
    initial_risk: Decimal = ZERO
    max_favourable: Decimal = ZERO
    max_adverse: Decimal = ZERO
    partial_taken: bool = False
    breakeven_moved: bool = False
    tags: List[str] = field(default_factory=list)

    def unrealised(self, quote: Quote, inst: Instrument,
                   quote_to_account: Decimal = D("1")) -> Decimal:
        """Mark to the price you could actually close at, not the mid."""
        exit_price = quote.price_for(self.side.opposite)
        delta = (exit_price - self.entry_price) * D(self.side.sign)
        return delta * inst.units(self.lots) * quote_to_account

    def r_multiple(self, quote: Quote, inst: Instrument,
                   quote_to_account: Decimal = D("1")) -> Optional[Decimal]:
        if self.initial_risk <= 0:
            return None
        return self.unrealised(quote, inst, quote_to_account) / self.initial_risk


@dataclass
class AccountState:
    account_id: str
    currency: str
    balance: Decimal
    equity: Decimal
    margin_used: Decimal = ZERO
    margin_available: Decimal = ZERO
    unrealised_pnl: Decimal = ZERO
    open_positions: int = 0
    last_transaction_id: str = ""
    ts_ns: int = field(default_factory=wall_ns)
    source: str = "broker"
    #: "demo" | "live" | "" when the venue does not say. This crosses the
    #: broker boundary because it is the single most consequential fact about
    #: an account and it was previously known only inside each adapter -- so
    #: nothing downstream, including the connection test and the dashboard,
    #: could tell a customer they were about to trade real money.
    account_type: str = ""
    #: As the venue reports it. A venue offering 1:500 to an account the risk
    #: engine sized for 1:30 will accept orders that were assumed impossible.
    leverage: int = 0
    #: Free-text venue/company name, for display and for profile matching.
    venue_name: str = ""

    @property
    def margin_level_pct(self) -> Optional[Decimal]:
        if self.margin_used <= 0:
            return None
        return (self.equity / self.margin_used) * D("100")

    @property
    def is_live(self) -> Optional[bool]:
        """True, False, or None for "the venue did not say"."""
        if self.account_type == "live":
            return True
        if self.account_type == "demo":
            return False
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id, "currency": self.currency,
            "balance": str(self.balance), "equity": str(self.equity),
            "margin_used": str(self.margin_used),
            "margin_available": str(self.margin_available),
            "unrealised_pnl": str(self.unrealised_pnl),
            "open_positions": self.open_positions,
            "margin_level_pct": (str(self.margin_level_pct)
                                 if self.margin_level_pct is not None else None),
            "last_transaction_id": self.last_transaction_id,
            "ts_ns": self.ts_ns, "source": self.source,
            "account_type": self.account_type, "leverage": self.leverage,
            "venue_name": self.venue_name,
        }


@dataclass
class ClosedTrade:
    """One completed round trip. The atom of every performance statistic."""

    trade_id: str
    strategy: str
    instrument: str
    side: Side
    lots: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_ns: int
    closed_ns: int
    pnl: Decimal
    pnl_pips: Decimal
    commission: Decimal = ZERO
    financing: Decimal = ZERO
    initial_risk: Decimal = ZERO
    r_multiple: Decimal = ZERO
    exit_reason: str = ""
    max_favourable_r: Decimal = ZERO
    max_adverse_r: Decimal = ZERO
    entry_slippage_pips: Decimal = ZERO
    exit_slippage_pips: Decimal = ZERO
    regime: str = ""
    news_context: Dict[str, Any] = field(default_factory=dict)
    signal_features: Dict[str, float] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    @property
    def duration_sec(self) -> float:
        return (self.closed_ns - self.opened_ns) / 1e9

    @property
    def won(self) -> bool:
        return self.pnl > 0
