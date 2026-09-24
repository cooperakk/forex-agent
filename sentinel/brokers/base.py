"""The broker boundary.

Every venue is reduced to this one interface so that the risk engine, the OMS
and the agent never learn which broker they are talking to. Three capability
flags exist because the differences that matter are *capability* differences,
not API-shape differences:

``supports_client_order_id``
    OANDA and FIX venues accept a caller-generated id and deduplicate on it.
    MetaTrader does not. Without it, a lost response cannot be resolved safely
    and the OMS must fall back to a strictly weaker protocol (query-before-
    resend plus a blackout window). The research brief calls this out as a
    documented *degradation*, not something to paper over -- so the flag is
    surfaced all the way to the dashboard.

``supports_server_side_stop``
    A stop that lives only in our process is not a stop when the link drops.

``supports_transaction_stream``
    An ordered, resumable event stream is what makes reconciliation exact
    rather than approximate.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..core.money import Instrument
from ..core.types import (
    AccountState,
    Bar,
    ClosedTrade,
    Fill,
    Order,
    OrderIntent,
    OrderState,
    Position,
    Quote,
    Side,
)


@dataclass(frozen=True)
class BrokerCapabilities:
    supports_client_order_id: bool
    supports_server_side_stop: bool
    supports_transaction_stream: bool
    supports_partial_close: bool
    supports_fractional_lots: bool
    min_lot: Decimal
    lot_step: Decimal
    name: str
    notes: str = ""
    #: Whether ``Broker.fetch_bars`` returns the venue's own candle history.
    #: Three-valued on purpose: ``None`` means the adapter never said, which is
    #: how every adapter written before this field existed reads, and it must
    #: not be reported as a degradation it never claimed. ``False`` is an
    #: explicit statement that strategies cannot run on this venue without an
    #: imported bar file, and IS reported.
    supports_bar_history: Optional[bool] = None

    def degradation_report(self) -> List[str]:
        """Plain statements of what this venue cannot guarantee."""
        out: List[str] = []
        if self.supports_bar_history is False:
            out.append(
                "No bar history from the venue: the strategies see no candles "
                "and produce no signals until bars are imported into the store."
            )
        if not self.supports_client_order_id:
            out.append(
                "No client-side order id: duplicate suppression falls back to "
                "an application lock plus a mandatory state query before any "
                "resend. A hard guarantee is replaced by a race that is merely "
                "narrow. Keep the blackout window and treat UNKNOWN as fatal."
            )
        if not self.supports_server_side_stop:
            out.append(
                "No venue-side stop: a dropped link leaves positions unprotected. "
                "Do not run this venue unattended."
            )
        if not self.supports_transaction_stream:
            out.append(
                "No ordered transaction stream: reconciliation is a snapshot "
                "comparison and can miss a fill that opened and closed between "
                "two polls."
            )
        if not self.supports_partial_close:
            out.append("No partial close: scale-out profit protection is unavailable.")
        return out


@dataclass
class SubmitResult:
    """Outcome of one submission attempt.

    ``state`` is deliberately allowed to be ``UNKNOWN``; callers must not treat
    that as failure.
    """

    state: OrderState
    venue_order_id: Optional[str] = None
    fills: List[Fill] = None  # type: ignore[assignment]
    reject_reason: Optional[str] = None
    raw: Dict[str, Any] = None  # type: ignore[assignment]
    venue_ts_ns: Optional[int] = None
    # Whether the venue accepted the protective stop attached to this order.
    # A venue can fill the market order and reject the stop in the SAME
    # response, leaving a position that reports as filled and protected while
    # actually being naked. False means: re-assert the stop before returning.
    stop_confirmed: bool = True
    stop_reject_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.fills is None:
            self.fills = []
        if self.raw is None:
            self.raw = {}


class Broker(abc.ABC):
    """Synchronous broker interface.

    Implementations must be safe to call from one trading thread. They must
    never raise for a *business* rejection (that is a ``SubmitResult`` with
    ``state=REJECTED``); they raise only for transport and protocol problems,
    using the taxonomy in ``core.errors``.
    """

    capabilities: BrokerCapabilities

    # -- reference data ----------------------------------------------------- #

    @abc.abstractmethod
    def instruments(self) -> Dict[str, Instrument]: ...

    def instrument(self, symbol: str) -> Instrument:
        try:
            return self.instruments()[symbol]
        except KeyError as exc:
            raise KeyError(f"instrument {symbol!r} not offered by {self.capabilities.name}") from exc

    # -- market data -------------------------------------------------------- #

    @abc.abstractmethod
    def quote(self, symbol: str) -> Quote: ...

    def quotes(self, symbols: Sequence[str]) -> Dict[str, Quote]:
        return {s: self.quote(s) for s in symbols}

    @abc.abstractmethod
    def conversion_rate(self, quote_ccy: str, account_ccy: str) -> Decimal:
        """Quote-currency -> account-currency. Must raise, never return 1.0 blindly."""

    # -- bar history --------------------------------------------------------- #

    def fetch_bars(self, symbol: str, timeframe: str, count: int, *,
                   end_ns: Optional[int] = None) -> List[Bar]:
        """The most recent ``count`` bars of ``timeframe`` for ``symbol``, oldest first.

        This is how candles reach ``data.feed.BarStore`` on a live venue. For a
        long time nothing implemented it, so the store was only ever written by
        the synthetic paper simulation: on OANDA or MetaTrader every strategy
        saw an empty frame, never reached its warm-up, and never produced a
        signal -- while the dashboard, the heartbeat and the reconciler all
        reported a perfectly healthy system doing nothing.

        Contract:

        * Timestamps are UTC nanoseconds. An adapter whose venue reports a
          server-local clock (MetaTrader does) converts BEFORE returning; a
          bar stamped in the wrong zone lands on the wrong H4 boundary and every
          session-aware strategy then trades the wrong hour.
        * The bar in progress MAY be included, with ``complete=False``. The
          store never lets an incomplete bar overwrite a complete one, and the
          strategies never see incomplete bars, so including it is safe and
          omitting it is fine.
        * ``end_ns`` bounds the request when given; the default is "now".
        * Returns ``[]`` when unsupported, and the capability flag says so.
        """
        return []

    @property
    def supports_bar_history(self) -> bool:
        return bool(getattr(self.capabilities, "supports_bar_history", False))

    # -- account ------------------------------------------------------------ #

    @abc.abstractmethod
    def account(self) -> AccountState: ...

    @abc.abstractmethod
    def positions(self) -> List[Position]: ...

    @abc.abstractmethod
    def open_orders(self) -> List[Order]: ...

    # -- trading ------------------------------------------------------------ #

    @abc.abstractmethod
    def submit(self, intent: OrderIntent, *, timeout_ms: int) -> SubmitResult: ...

    @abc.abstractmethod
    def query_order(self, client_order_id: str) -> Optional[SubmitResult]:
        """Resolve an UNKNOWN outcome. THE only legal way out of UNKNOWN."""

    @abc.abstractmethod
    def cancel(self, client_order_id: str) -> bool: ...

    @abc.abstractmethod
    def close_position(self, instrument: str, lots: Optional[Decimal] = None,
                       *, reason: str = "") -> SubmitResult: ...

    @abc.abstractmethod
    def modify_position(self, instrument: str, *, stop_loss: Optional[Decimal] = None,
                        take_profit: Optional[Decimal] = None) -> bool: ...

    # -- reconciliation ----------------------------------------------------- #

    @abc.abstractmethod
    def transactions_since(self, last_id: str) -> Iterable[Dict[str, Any]]:
        """Ordered events after ``last_id``. Empty iterable if unsupported."""

    # -- realised history --------------------------------------------------- #

    def fetch_closed_trades(self, since_id: str = "") -> tuple[List["ClosedTrade"], str]:
        """Completed round trips after ``since_id``, and the new cursor.

        Every performance statistic, every post-mortem and the entire learning
        loop are built on closed trades. The simulator keeps them in memory, and
        for a long time this method did not exist -- so on any real venue the
        agent saw zero trades forever: no autopsies, no lessons, no proposals,
        and a dashboard that looked like it was working while showing nothing.

        The default reads an in-memory ``closed_trades`` list when an adapter
        exposes one; a live adapter overrides this and reads the venue's own
        history, which is the only authoritative record.
        """
        from ..core.types import ClosedTrade  # noqa: F401 - typing only

        held = getattr(self, "closed_trades", None)
        if not isinstance(held, list):
            return [], since_id
        try:
            start = int(since_id) if since_id else 0
        except (TypeError, ValueError):
            start = 0
        fresh = held[start:]
        return list(fresh), str(len(held))

    @property
    def supports_closed_trade_history(self) -> bool:
        """Whether realised history is available. False disables the learning loop
        rather than letting it silently report nothing."""
        return isinstance(getattr(self, "closed_trades", None), list)

    def ping(self) -> float:
        """Round-trip latency in ms. Used by the connectivity monitor."""
        from ..core.clock import Stopwatch

        with Stopwatch() as sw:
            self.account()
        return sw.elapsed_ms

    def close(self) -> None:
        return
