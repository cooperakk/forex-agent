"""Account reconciliation.

The venue is the source of truth. Always, without exception. Our local view is
a cache, and after any restart, disconnection or unknown order outcome it is
assumed stale until proven otherwise.

What reconciliation produces is a *diff*, and every difference is recorded even
when it is repaired automatically. A pattern of small mismatches is the early
warning that something in the order path is wrong; silently correcting them
throws that signal away.

Four classes of mismatch, each with a different response:

``phantom``     we think we hold something the venue does not.
                -> drop it locally, record, and investigate.
``orphan``      the venue holds something we do not know about.
                -> adopt it, attach a protective stop IMMEDIATELY, halt new
                   entries. An unknown position is the worst state to be in.
``size_drift``  same instrument and direction, different size.
                -> adopt the venue's size.
``unprotected`` the venue holds a position with no stop.
                -> attach one at once; this outranks everything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from ..brokers.base import Broker
from ..core.audit import AuditLog, EventType
from ..core.clock import wall_ns
from ..core.money import D, ZERO, dec
from ..core.types import AccountState, Position, Side


@dataclass
class Mismatch:
    kind: str
    instrument: str
    local: Optional[str]
    venue: Optional[str]
    action: str
    severity: str = "warning"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "instrument": self.instrument, "local": self.local,
                "venue": self.venue, "action": self.action, "severity": self.severity}


@dataclass
class ReconcileReport:
    ts_ns: int
    ok: bool
    mismatches: List[Mismatch] = field(default_factory=list)
    venue_positions: int = 0
    local_positions: int = 0
    unprotected: List[str] = field(default_factory=list)
    unresolved_orders: List[str] = field(default_factory=list)
    equity: Optional[str] = None
    last_transaction_id: str = ""
    halt_required: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ts_ns": self.ts_ns, "ok": self.ok,
                "mismatches": [m.to_dict() for m in self.mismatches],
                "venue_positions": self.venue_positions,
                "local_positions": self.local_positions,
                "unprotected": self.unprotected,
                "unresolved_orders": self.unresolved_orders,
                "equity": self.equity, "last_transaction_id": self.last_transaction_id,
                "halt_required": self.halt_required, "notes": self.notes}


class Reconciler:
    def __init__(self, broker: Broker, audit: AuditLog, *,
                 auto_protect: bool = True,
                 default_stop_atr_multiple: Decimal = D("2.0")) -> None:
        self.broker = broker
        self.audit = audit
        self.auto_protect = auto_protect
        self.default_stop_atr_multiple = default_stop_atr_multiple
        self.last_report: Optional[ReconcileReport] = None

    def reconcile(self, local_positions: Sequence[Position], *,
                  unresolved_order_ids: Sequence[str] = (),
                  emergency_stop_pips: Decimal = D("50"),
                  conversions: Optional[Dict[str, Decimal]] = None) -> ReconcileReport:
        report = ReconcileReport(ts_ns=wall_ns(), ok=True)
        try:
            account: AccountState = self.broker.account()
            venue_positions = self.broker.positions()
        except Exception as exc:
            report.ok = False
            report.halt_required = True
            report.notes.append(f"could not read the venue state: {exc}")
            self.audit.append(EventType.RECONCILE_MISMATCH, {"error": str(exc)})
            self.last_report = report
            return report

        report.equity = str(account.equity)
        report.last_transaction_id = account.last_transaction_id
        report.venue_positions = len(venue_positions)
        report.local_positions = len(local_positions)
        report.unresolved_orders = list(unresolved_order_ids)
        if unresolved_order_ids:
            report.ok = False
            report.halt_required = True
            report.notes.append(
                f"{len(unresolved_order_ids)} order(s) in an unknown state; entries stay "
                "frozen until each is resolved by query")

        local_map: Dict[str, Position] = {p.instrument: p for p in local_positions}
        venue_map: Dict[str, Position] = {p.instrument: p for p in venue_positions}

        for symbol, local in local_map.items():
            if symbol not in venue_map:
                report.mismatches.append(Mismatch(
                    "phantom", symbol, f"{local.side.value} {local.lots}", None,
                    "dropped locally; the venue is authoritative", "error"))
                report.ok = False

        for symbol, venue in venue_map.items():
            local = local_map.get(symbol)
            if local is None:
                report.mismatches.append(Mismatch(
                    "orphan", symbol, None, f"{venue.side.value} {venue.lots}",
                    "adopted; a protective stop is attached immediately and new entries "
                    "are frozen", "critical"))
                report.ok = False
                report.halt_required = True
            else:
                if local.side is not venue.side:
                    report.mismatches.append(Mismatch(
                        "direction", symbol, local.side.value, venue.side.value,
                        "adopted the venue direction", "critical"))
                    report.ok = False
                    report.halt_required = True
                elif abs(local.lots - venue.lots) > D("0.0001"):
                    report.mismatches.append(Mismatch(
                        "size_drift", symbol, str(local.lots), str(venue.lots),
                        "adopted the venue size", "error"))
                    report.ok = False

            if venue.stop_loss is None or not venue.broker_stop_confirmed:
                report.unprotected.append(symbol)
                report.ok = False
                if self.auto_protect:
                    self._attach_emergency_stop(
                        venue, emergency_stop_pips, report,
                        quote_to_account=(conversions or {}).get(
                            self.broker.instrument(symbol).quote)
                        if conversions else None)

        if report.mismatches or report.unprotected:
            self.audit.append(EventType.RECONCILE_MISMATCH, report.to_dict())
        else:
            self.audit.append(EventType.RECONCILE, {
                "ok": True, "venue_positions": len(venue_positions),
                "equity": str(account.equity),
                "last_transaction_id": account.last_transaction_id})
        self.last_report = report
        return report

    def _attach_emergency_stop(self, position: Position, stop_pips: Decimal,
                               report: ReconcileReport,
                               *, quote_to_account: Optional[Decimal] = None) -> None:
        """An unprotected position gets a stop before anything else happens.

        The distance is derived from the position's OWN recorded risk wherever
        that is known, not from a flat 50 pips. A fixed distance repaired an
        "unprotected" position by silently multiplying the book's real risk:
        four positions sized for a 12-pip stop, repaired to 50 pips, turned a
        2.0% book into an 8.2% one while every limit still read green, because
        `initial_risk` was left untouched and the engine kept quoting it.

        Whatever distance is used, `initial_risk` is rewritten to match, so the
        portfolio limits describe the book that now exists rather than the one
        that was intended.
        """
        try:
            inst = self.broker.instrument(position.instrument)
            quote = self.broker.quote(position.instrument)
        except Exception as exc:
            report.notes.append(
                f"{position.instrument} is unprotected and no price is available "
                f"to place a stop: {exc}")
            report.halt_required = True
            return
        ref = quote.price_for(position.side.opposite)

        distance_pips = dec(stop_pips)
        derived = False
        conv = dec(quote_to_account) if quote_to_account is not None else None
        if position.initial_risk and position.initial_risk > 0 and conv and conv > 0:
            pip_val = inst.pip_value_quote(position.lots) * conv
            if pip_val > 0:
                want = position.initial_risk / pip_val
                if want > 0:
                    # Never WIDER than the fallback: a huge derived distance
                    # would be worse than the safety net it replaces.
                    distance_pips = min(want, dec(stop_pips))
                    derived = True

        offset = inst.pip * distance_pips * D(position.side.sign)
        stop = inst.round_price(ref - offset)
        try:
            ok = self.broker.modify_position(position.instrument, stop_loss=stop)
        except Exception as exc:
            ok = False
            report.notes.append(f"failed to attach a stop to {position.instrument}: {exc}")
        if ok:
            position.stop_loss = stop
            position.broker_stop_confirmed = True
            # Rewrite the risk to what the book now actually carries, so the
            # portfolio ceilings are not quoting a number that stopped being
            # true the moment this stop was placed.
            if conv and conv > 0:
                pip_val = inst.pip_value_quote(position.lots) * conv
                if pip_val > 0:
                    position.initial_risk = distance_pips * pip_val
            report.notes.append(
                f"emergency stop attached to {position.instrument} at {stop} "
                f"({distance_pips:.1f}p, "
                f"{'derived from the recorded risk' if derived else 'fallback distance'}); "
                "this is a safety net, not the strategy's stop")
            self.audit.append(EventType.POSITION_MODIFY, {
                "instrument": position.instrument, "emergency_stop": str(stop),
                "reason": "position found unprotected during reconciliation"})
        else:
            report.halt_required = True
            report.notes.append(
                f"{position.instrument} remains UNPROTECTED and could not be modified; "
                "the agent halts rather than run naked risk")

    def replay_transactions(self, last_id: str) -> List[dict]:
        """Ordered venue events since ``last_id``; the exact recovery path."""
        try:
            events = list(self.broker.transactions_since(last_id))
        except Exception as exc:
            self.audit.append(EventType.RECONCILE_MISMATCH, {"transaction_replay_error": str(exc)})
            return []
        if events:
            self.audit.append(EventType.RECONCILE, {
                "replayed_transactions": len(events), "since_id": last_id})
        return events
