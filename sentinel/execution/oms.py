"""Order management: the state machine that stands between a decision and a venue.

Its single job is to make sure that one intent produces at most one position,
even when the network lies. Everything here follows from one fact: a timeout on
a write tells you *nothing* about whether the venue acted.

Rules, enforced rather than documented:

1. **Deterministic client order id.** Derived from the intent, so a retry
   reuses it byte-for-byte and the venue rejects the duplicate. On a venue
   without that capability the OMS switches to the degraded protocol and says
   so in the journal.
2. **A retry is only ever allowed after a query.** ``UNKNOWN`` is resolved by
   asking, never by resending.
3. **Quarantine.** While an instrument has an unresolved order, no new order
   for it is accepted. The risk engine also refuses new entries globally --
   two independent locks, because this is the failure that costs the most.
4. **Illegal transitions raise.** ``FILLED -> SENT`` is a bug, and a bug that
   silently succeeds here would produce a phantom position.
5. **The journal is written BEFORE the socket.** If the process dies between
   the write and the send, recovery finds an intent in ``SENT`` and resolves it
   by query. If it were written after, recovery would find nothing and the
   position would be invisible.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Set

from ..brokers.base import Broker, SubmitResult
from ..core.audit import AuditLog, EventType
from ..core.clock import Stopwatch, wall_ns
from ..core.errors import (
    BrokerError, PermanentError, TransientError, UnknownOutcomeError,
)
from ..core.money import ZERO
from ..core.types import Fill, Order, OrderIntent, OrderState, Side

# state -> the states it may legally move to
LEGAL: Dict[OrderState, Set[OrderState]] = {
    OrderState.PENDING: {OrderState.SENT, OrderState.REJECTED, OrderState.CANCELLED},
    OrderState.SENT: {OrderState.ACKED, OrderState.FILLED, OrderState.PARTIAL,
                      OrderState.REJECTED, OrderState.UNKNOWN, OrderState.CANCELLED},
    OrderState.ACKED: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED,
                       OrderState.REJECTED, OrderState.UNKNOWN},
    OrderState.PARTIAL: {OrderState.FILLED, OrderState.CANCELLED, OrderState.UNKNOWN},
    # UNKNOWN can only be left via reconciliation, and only to a terminal state.
    OrderState.UNKNOWN: {OrderState.FILLED, OrderState.PARTIAL, OrderState.REJECTED,
                         OrderState.CANCELLED},
    OrderState.FILLED: set(),
    OrderState.REJECTED: set(),
    OrderState.CANCELLED: set(),
}


class IllegalTransition(RuntimeError):
    pass


@dataclass
class ExecutionQuality:
    """Per-fill execution measurements (research brief, section B-7)."""

    decision_price: Decimal
    fill_price: Decimal
    slippage_pips: Decimal
    latency_ms: float
    rejected: bool
    reject_favoured_client: Optional[bool] = None
    venue_ts_ns: Optional[int] = None
    local_ts_ns: int = field(default_factory=wall_ns)

    @property
    def clock_delta_ms(self) -> Optional[float]:
        if self.venue_ts_ns is None:
            return None
        return (self.local_ts_ns - self.venue_ts_ns) / 1e6

    def to_dict(self) -> dict:
        return {"decision_price": str(self.decision_price), "fill_price": str(self.fill_price),
                "slippage_pips": str(self.slippage_pips), "latency_ms": round(self.latency_ms, 2),
                "rejected": self.rejected,
                "reject_favoured_client": self.reject_favoured_client,
                "clock_delta_ms": (round(self.clock_delta_ms, 2)
                                   if self.clock_delta_ms is not None else None)}


class OrderManager:
    def __init__(self, broker: Broker, audit: AuditLog, *,
                 submit_timeout_ms: int = 5000, max_retries: int = 2,
                 retry_backoff_ms: int = 750,
                 max_slippage_pips: Optional[Decimal] = None,
                 on_fill: Optional[Callable[[Order, Fill], None]] = None) -> None:
        self.broker = broker
        self.audit = audit
        self.submit_timeout_ms = submit_timeout_ms
        # A venue without caller-supplied order ids cannot deduplicate a retry,
        # so the retry budget is forced to zero there. Retrying blind on such a
        # venue is how one intent becomes three positions.
        if not broker.capabilities.supports_client_order_id and max_retries > 0:
            audit.append(EventType.ORDER_INTENT, {
                "degradation": "retries disabled",
                "reason": f"{broker.capabilities.name} cannot reject a duplicate "
                          "client order id, so an automatic retry could open a "
                          "second position"})
            max_retries = 0
        self.max_retries = max_retries
        self.retry_backoff_ms = retry_backoff_ms
        self.max_slippage_pips = max_slippage_pips
        self.slippage_breaches: List[dict] = []
        self.on_fill = on_fill
        self.orders: Dict[str, Order] = {}
        self.quality: List[ExecutionQuality] = []
        self._quarantine: Set[str] = set()
        self._lock = threading.RLock()

    # -- state machine ------------------------------------------------------ #

    def _transition(self, order: Order, new: OrderState, **context) -> None:
        old = order.state
        if new is old:
            return
        if new not in LEGAL.get(old, set()):
            self.audit.append(EventType.ORDER_UNKNOWN, {
                "client_order_id": order.intent.client_order_id,
                "illegal_transition": f"{old.value}->{new.value}", **context})
            raise IllegalTransition(
                f"{order.intent.client_order_id}: {old.value} -> {new.value} is not a legal "
                "order transition")
        order.state = new

    # -- properties --------------------------------------------------------- #

    @property
    def unresolved(self) -> List[Order]:
        with self._lock:
            return [o for o in self.orders.values() if o.state is OrderState.UNKNOWN]

    @property
    def quarantined(self) -> Set[str]:
        with self._lock:
            return set(self._quarantine)

    def pending_risk(self) -> Decimal:
        with self._lock:
            return sum((o.intent.risk_amount for o in self.orders.values()
                        if o.state in (OrderState.SENT, OrderState.ACKED,
                                       OrderState.PARTIAL, OrderState.UNKNOWN)), ZERO)

    # -- submission --------------------------------------------------------- #

    def submit(self, intent: OrderIntent, *, decision_price: Optional[Decimal] = None) -> Order:
        with self._lock:
            if intent.instrument in self._quarantine:
                raise UnknownOutcomeError(
                    f"{intent.instrument} is quarantined by an unresolved order; "
                    "reconcile before submitting",
                    instrument=intent.instrument)
            existing = self.orders.get(intent.client_order_id)
            if existing is not None:
                # Same intent, already known. Never send it twice.
                self.audit.append(EventType.ORDER_DUPLICATE_BLOCKED, {
                    "client_order_id": intent.client_order_id, "state": existing.state.value})
                return existing

            order = Order(intent=intent, state=OrderState.PENDING)
            self.orders[intent.client_order_id] = order
            # Journal BEFORE the socket, so a crash mid-send is recoverable.
            self.audit.append(EventType.ORDER_INTENT, {
                "client_order_id": intent.client_order_id, "strategy": intent.strategy,
                "instrument": intent.instrument, "side": intent.side.value,
                "lots": str(intent.lots), "stop_loss": str(intent.stop_loss),
                "take_profit": str(intent.take_profit),
                "risk_amount": str(intent.risk_amount),
                "venue_supports_client_id": self.broker.capabilities.supports_client_order_id})

        attempt = 0
        last_error: Optional[Exception] = None
        while attempt <= self.max_retries:
            attempt += 1
            order.attempts = attempt
            with self._lock:
                self._transition(order, OrderState.SENT, attempt=attempt)
                order.sent_ns = wall_ns()
            self.audit.append(EventType.ORDER_SENT, {
                "client_order_id": intent.client_order_id, "attempt": attempt})
            sw = Stopwatch()
            try:
                result = self.broker.submit(intent, timeout_ms=self.submit_timeout_ms)
            except UnknownOutcomeError as exc:
                last_error = exc
                # THE dangerous branch. Do not resend. Ask.
                with self._lock:
                    self._transition(order, OrderState.UNKNOWN, reason=str(exc))
                    self._quarantine.add(intent.instrument)
                    order.last_error = str(exc)
                self.audit.append(EventType.ORDER_UNKNOWN, {
                    "client_order_id": intent.client_order_id, "error": str(exc),
                    "action": "quarantined; resolution is by query only"})
                resolved = self.resolve_unknown(intent.client_order_id)
                if resolved is not None and resolved.state.terminal:
                    return resolved
                return order
            except TransientError as exc:
                last_error = exc
                order.last_error = str(exc)
                if attempt > self.max_retries:
                    break
                # A transient failure on a *read-like* rejection is safe to retry
                # because the id is deterministic: a duplicate is refused by the
                # venue rather than doubling the position.
                import time

                time.sleep(self.retry_backoff_ms / 1000 * attempt)
                continue
            except PermanentError as exc:
                with self._lock:
                    self._transition(order, OrderState.REJECTED)
                    order.reject_reason = str(exc)
                self.audit.append(EventType.ORDER_REJECTED, {
                    "client_order_id": intent.client_order_id, "reason": str(exc),
                    "permanent": True})
                return order
            except BrokerError as exc:
                last_error = exc
                order.last_error = str(exc)
                break

            latency = sw.elapsed_ms
            self._apply_result(order, result, latency, decision_price)
            return order

        with self._lock:
            self._transition(order, OrderState.UNKNOWN,
                             reason=str(last_error) if last_error else "retries exhausted")
            self._quarantine.add(intent.instrument)
        self.audit.append(EventType.ORDER_UNKNOWN, {
            "client_order_id": intent.client_order_id,
            "error": str(last_error) if last_error else "retries exhausted"})
        return order

    def _apply_result(self, order: Order, result: SubmitResult, latency_ms: float,
                      decision_price: Optional[Decimal]) -> None:
        intent = order.intent
        with self._lock:
            order.venue_order_id = result.venue_order_id or order.venue_order_id
            order.acked_ns = wall_ns()
            if result.state is OrderState.REJECTED:
                self._transition(order, OrderState.REJECTED)
                order.reject_reason = result.reject_reason
                self.quality.append(ExecutionQuality(
                    decision_price=decision_price or ZERO, fill_price=ZERO,
                    slippage_pips=ZERO, latency_ms=latency_ms, rejected=True,
                    reject_favoured_client=result.raw.get("favourable_to_client"),
                    venue_ts_ns=result.venue_ts_ns))
                self.audit.append(EventType.ORDER_REJECTED, {
                    "client_order_id": intent.client_order_id,
                    "reason": result.reject_reason, "latency_ms": round(latency_ms, 1)})
                return

            if result.state is OrderState.UNKNOWN:
                self._transition(order, OrderState.UNKNOWN)
                self._quarantine.add(intent.instrument)
                self.audit.append(EventType.ORDER_UNKNOWN, {
                    "client_order_id": intent.client_order_id,
                    "reason": result.reject_reason})
                return

            for fill in result.fills:
                if any(f.venue_order_id == fill.venue_order_id and f.lots == fill.lots
                       and f.price == fill.price for f in order.fills):
                    continue  # idempotent re-application during reconciliation
                order.fills.append(fill)
                if decision_price is not None:
                    inst = self.broker.instrument(intent.instrument)
                    slip = ((fill.price - decision_price) / inst.pip) * Decimal(intent.side.sign)
                else:
                    slip = fill.slippage_pips
                self.quality.append(ExecutionQuality(
                    decision_price=decision_price or fill.price, fill_price=fill.price,
                    slippage_pips=slip, latency_ms=latency_ms, rejected=False,
                    venue_ts_ns=fill.ts_ns))
                self.audit.append(EventType.ORDER_FILLED, {
                    "client_order_id": intent.client_order_id,
                    "venue_order_id": fill.venue_order_id, "price": str(fill.price),
                    "lots": str(fill.lots), "slippage_pips": str(slip),
                    "commission": str(fill.commission), "latency_ms": round(latency_ms, 1),
                    "venue_ts_ns": fill.ts_ns, "local_ts_ns": fill.received_ns})
                # The tolerance cannot un-fill an executed order, but a breach is
                # a measurable degradation of the venue and must be visible
                # rather than merely recorded as a number nobody reads.
                if self.max_slippage_pips is not None and slip > self.max_slippage_pips:
                    breach = {"client_order_id": intent.client_order_id,
                              "instrument": intent.instrument,
                              "slippage_pips": str(slip),
                              "tolerance_pips": str(self.max_slippage_pips)}
                    self.slippage_breaches.append(breach)
                    self.audit.append(EventType.LIMIT_BREACH,
                                      {"rule": "max_slippage_pips", **breach})
                if self.on_fill:
                    self.on_fill(order, fill)

            target = (OrderState.FILLED if result.state is OrderState.FILLED
                      else OrderState.PARTIAL if result.state is OrderState.PARTIAL
                      else OrderState.ACKED)
            # A FILLED result that carries no fill is incoherent: the venue holds
            # a position the OMS would believe has zero size, and on_fill never
            # runs. Treat it as UNKNOWN and resolve it by query rather than
            # trusting the adapter to be well-behaved.
            if target in (OrderState.FILLED, OrderState.PARTIAL) and not order.fills:
                self._transition(order, OrderState.UNKNOWN)
                self._quarantine.add(intent.instrument)
                self.audit.append(EventType.ORDER_UNKNOWN, {
                    "client_order_id": intent.client_order_id,
                    "reason": f"venue reported {result.state.value} with no fill; "
                              "the filled size is unknown"})
                return
            # The venue may have FILLED the order and REJECTED the protective
            # stop in the same response. Re-assert the stop before returning:
            # otherwise the position is naked until the next reconciliation,
            # and there is no worse moment to be unprotected than immediately
            # after opening.
            if (target in (OrderState.FILLED, OrderState.PARTIAL)
                    and not getattr(result, "stop_confirmed", True)
                    and intent.stop_loss is not None):
                order.stop_confirmed = False
                self.audit.append(EventType.RECONCILE_MISMATCH, {
                    "client_order_id": intent.client_order_id,
                    "instrument": intent.instrument,
                    "venue_rejected_protective_stop":
                        getattr(result, "stop_reject_reason", None) or "unspecified",
                    "action": "re-asserting the stop immediately"})
                try:
                    reasserted = self.broker.modify_position(
                        intent.instrument, stop_loss=intent.stop_loss)
                except Exception as exc:  # noqa: BLE001
                    reasserted = False
                    self.audit.append(EventType.RECONCILE_MISMATCH, {
                        "client_order_id": intent.client_order_id,
                        "stop_reassert_failed": str(exc)})
                order.stop_confirmed = bool(reasserted)
                if not reasserted:
                    # Freeze new entries on this instrument. An unprotected
                    # position is the condition the `unprotected_book` veto
                    # exists for, and it must not be quietly tolerated.
                    self._quarantine.add(intent.instrument)
                    self.audit.append(EventType.HALT, {
                        "instrument": intent.instrument,
                        "reason": "the venue filled the order but refused its stop, and "
                                  "the stop could not be re-asserted; this position is "
                                  "unprotected"})

            self._transition(order, target)
            if target is not OrderState.ACKED:
                self.audit.append(EventType.ORDER_ACK, {
                    "client_order_id": intent.client_order_id, "state": target.value,
                    "filled_lots": str(order.filled_lots),
                    "stop_confirmed": getattr(order, "stop_confirmed", True)})

    # -- resolution --------------------------------------------------------- #

    def resolve_unknown(self, client_order_id: str) -> Optional[Order]:
        """Ask the venue what happened. The only legal exit from UNKNOWN."""
        with self._lock:
            order = self.orders.get(client_order_id)
        if order is None or order.state is not OrderState.UNKNOWN:
            return order
        try:
            result = self.broker.query_order(client_order_id)
        except BrokerError as exc:
            self.audit.append(EventType.ORDER_UNKNOWN, {
                "client_order_id": client_order_id, "query_failed": str(exc)})
            return order
        if result is None:
            # No trace at the venue. Absence of evidence is weak evidence here:
            # treat it as cancelled but keep the instrument quarantined until a
            # full reconciliation confirms the position book.
            self.audit.append(EventType.ORDER_UNKNOWN, {
                "client_order_id": client_order_id,
                "query_result": "not found at venue; awaiting full reconciliation"})
            return order
        self._apply_result(order, result, latency_ms=0.0, decision_price=None)
        with self._lock:
            if order.state.terminal:
                self._quarantine.discard(order.intent.instrument)
        self.audit.append(EventType.RECONCILE, {
            "client_order_id": client_order_id, "resolved_to": order.state.value})
        return order

    def resolve_all_unknown(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for order in list(self.unresolved):
            resolved = self.resolve_unknown(order.intent.client_order_id)
            out[order.intent.client_order_id] = resolved.state.value if resolved else "unresolved"
        return out

    def release_quarantine(self, instrument: str, *, reason: str) -> None:
        with self._lock:
            if instrument in self._quarantine:
                self._quarantine.discard(instrument)
                self.audit.append(EventType.RECONCILE, {
                    "instrument": instrument, "quarantine": "released", "reason": reason})

    # -- reporting ---------------------------------------------------------- #

    def execution_report(self) -> Dict[str, object]:
        if not self.quality:
            return {"n": 0}
        filled = [q for q in self.quality if not q.rejected]
        rejected = [q for q in self.quality if q.rejected]
        slips = [float(q.slippage_pips) for q in filled]
        lats = sorted(q.latency_ms for q in self.quality)
        favoured = [q.reject_favoured_client for q in rejected
                    if q.reject_favoured_client is not None]
        report: Dict[str, object] = {
            "n": len(self.quality), "fills": len(filled), "rejects": len(rejected),
            "reject_rate": round(len(rejected) / len(self.quality), 4),
            "median_latency_ms": round(lats[len(lats) // 2], 1) if lats else 0.0,
            "p95_latency_ms": round(lats[min(len(lats) - 1, int(0.95 * len(lats)))], 1)
            if lats else 0.0,
        }
        if slips:
            slips_sorted = sorted(slips)
            report.update({
                "mean_slippage_pips": round(sum(slips) / len(slips), 4),
                "median_slippage_pips": round(slips_sorted[len(slips) // 2], 4),
                "adverse_slippage_share": round(
                    sum(1 for s in slips if s > 0) / len(slips), 4),
            })
        if self.slippage_breaches:
            report["slippage_breaches"] = len(self.slippage_breaches)
            report["slippage_tolerance_pips"] = str(self.max_slippage_pips)
        if favoured:
            share = sum(1 for f in favoured if f) / len(favoured)
            report["last_look_asymmetry"] = round(share, 4)
            report["last_look_note"] = (
                "share of rejections that happened when the move favoured us. "
                "Materially above 0.5 means the venue is exercising a free option "
                "at our expense.")
        return report
