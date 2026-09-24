"""Order lifecycle: the guarantees that stop one intent becoming two positions."""

from decimal import Decimal as D

import pytest

from sentinel.brokers.base import Broker, BrokerCapabilities, SubmitResult
from sentinel.core.audit import AuditLog, EventType
from sentinel.core.errors import PermanentError, TransientError, UnknownOutcomeError
from sentinel.core.money import Instrument
from sentinel.core.types import (
    AccountState, Fill, OrderIntent, OrderState, Position, Quote, Side,
)
from sentinel.execution.oms import IllegalTransition, OrderManager
from sentinel.execution.reconcile import Reconciler

EU = Instrument("EUR_USD", "EUR", "USD")


class FakeBroker(Broker):
    """Scriptable venue. Each entry in ``script`` is what the next submit does."""

    def __init__(self, script=None, *, supports_client_id=True):
        self.script = list(script or [])
        self.submitted: list[str] = []
        self.queries: list[str] = []
        self._positions: list[Position] = []
        self._orders: dict[str, SubmitResult] = {}
        self.query_result: SubmitResult | None = None
        self.capabilities = BrokerCapabilities(
            supports_client_order_id=supports_client_id, supports_server_side_stop=True,
            supports_transaction_stream=True, supports_partial_close=True,
            supports_fractional_lots=True, min_lot=D("0.01"), lot_step=D("0.01"),
            name="fake")

    def instruments(self):
        return {"EUR_USD": EU}

    def quote(self, symbol):
        return Quote(symbol, D("1.08500"), D("1.08506"), ts_ns=1)

    def conversion_rate(self, q, a):
        return D("1")

    def account(self):
        return AccountState("F1", "USD", D("10000"), D("10000"),
                            margin_available=D("10000"))

    def positions(self):
        return list(self._positions)

    def open_orders(self):
        return []

    def submit(self, intent, *, timeout_ms=5000):
        self.submitted.append(intent.client_order_id)
        action = self.script.pop(0) if self.script else "fill"
        if isinstance(action, Exception):
            raise action
        if action == "reject":
            return SubmitResult(state=OrderState.REJECTED, reject_reason="TEST_REJECT")
        fill = Fill(client_order_id=intent.client_order_id, venue_order_id="V1",
                    instrument=intent.instrument, side=intent.side, lots=intent.lots,
                    price=D("1.08510"), ts_ns=2)
        res = SubmitResult(state=OrderState.FILLED, venue_order_id="V1", fills=[fill])
        self._orders[intent.client_order_id] = res
        return res

    def query_order(self, client_order_id):
        self.queries.append(client_order_id)
        return self.query_result or self._orders.get(client_order_id)

    def cancel(self, client_order_id):
        return True

    def close_position(self, instrument, lots=None, *, reason=""):
        self._positions = [p for p in self._positions if p.instrument != instrument]
        return SubmitResult(state=OrderState.FILLED)

    def modify_position(self, instrument, *, stop_loss=None, take_profit=None):
        for p in self._positions:
            if p.instrument == instrument and stop_loss is not None:
                p.stop_loss = stop_loss
                p.broker_stop_confirmed = True
        return True

    def transactions_since(self, last_id):
        return []


def intent(coid="C1", lots=D("0.1")):
    return OrderIntent(client_order_id=coid, strategy="s", instrument="EUR_USD",
                       side=Side.BUY, lots=lots, stop_loss=D("1.0820"),
                       take_profit=D("1.0910"), risk_amount=D("50"))


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "a.jsonl", fsync_every_record=False)


class TestIdempotency:
    def test_resubmitting_the_same_intent_never_reaches_the_venue_twice(self, audit):
        broker = FakeBroker()
        oms = OrderManager(broker, audit)
        a = oms.submit(intent())
        b = oms.submit(intent())
        assert a is b
        assert broker.submitted == ["C1"]
        events = [r["event"] for r in audit.read()]
        assert EventType.ORDER_DUPLICATE_BLOCKED.value in events

    def test_a_timeout_is_resolved_by_query_not_by_resend(self, audit):
        broker = FakeBroker(script=[UnknownOutcomeError("timeout on a write")])
        # The venue did in fact fill it; only our response was lost.
        broker.query_result = SubmitResult(
            state=OrderState.FILLED, venue_order_id="V9",
            fills=[Fill("C1", "V9", "EUR_USD", Side.BUY, D("0.1"), D("1.0851"), ts_ns=3)])
        oms = OrderManager(broker, audit, max_retries=2)
        order = oms.submit(intent())
        assert broker.submitted == ["C1"]          # exactly one send
        assert broker.queries == ["C1"]            # resolved by asking
        assert order.state is OrderState.FILLED
        assert order.filled_lots == D("0.1")

    def test_an_unresolved_timeout_quarantines_the_instrument(self, audit):
        broker = FakeBroker(script=[UnknownOutcomeError("timeout")])
        broker.query_result = None
        oms = OrderManager(broker, audit)
        order = oms.submit(intent())
        assert order.state is OrderState.UNKNOWN
        assert "EUR_USD" in oms.quarantined
        assert len(oms.unresolved) == 1
        with pytest.raises(UnknownOutcomeError, match="quarantined"):
            oms.submit(intent("C2"))

    def test_pending_risk_counts_unresolved_orders(self, audit):
        broker = FakeBroker(script=[UnknownOutcomeError("timeout")])
        broker.query_result = None
        oms = OrderManager(broker, audit)
        oms.submit(intent())
        assert oms.pending_risk() == D("50")


class TestErrorTaxonomy:
    def test_a_permanent_rejection_is_not_retried(self, audit):
        broker = FakeBroker(script=[PermanentError("INSUFFICIENT_MARGIN")])
        oms = OrderManager(broker, audit, max_retries=3)
        order = oms.submit(intent())
        assert order.state is OrderState.REJECTED
        assert len(broker.submitted) == 1

    def test_a_transient_failure_is_retried_with_the_same_id(self, audit):
        broker = FakeBroker(script=[TransientError("502"), "fill"])
        oms = OrderManager(broker, audit, max_retries=2, retry_backoff_ms=1)
        order = oms.submit(intent())
        assert order.state is OrderState.FILLED
        assert broker.submitted == ["C1", "C1"]    # identical key both times

    def test_a_business_rejection_is_terminal(self, audit):
        oms = OrderManager(FakeBroker(script=["reject"]), audit)
        order = oms.submit(intent())
        assert order.state is OrderState.REJECTED and order.state.terminal


class TestStateMachine:
    def test_illegal_transitions_raise(self, audit):
        oms = OrderManager(FakeBroker(), audit)
        order = oms.submit(intent())
        assert order.state is OrderState.FILLED
        with pytest.raises(IllegalTransition):
            oms._transition(order, OrderState.SENT)

    def test_terminal_states_have_no_exits(self):
        from sentinel.execution.oms import LEGAL
        for state in (OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED):
            assert LEGAL[state] == set()

    def test_unknown_only_leaves_to_a_terminal_state(self):
        from sentinel.execution.oms import LEGAL
        assert OrderState.SENT not in LEGAL[OrderState.UNKNOWN]
        assert OrderState.FILLED in LEGAL[OrderState.UNKNOWN]


class TestExecutionQuality:
    def test_slippage_is_measured_against_the_decision_price(self, audit):
        oms = OrderManager(FakeBroker(), audit)
        oms.submit(intent(), decision_price=D("1.08500"))
        q = oms.quality[0]
        assert q.slippage_pips == D("1")     # filled 1.0851 on a 1.0850 decision
        assert q.rejected is False

    def test_report_summarises(self, audit):
        oms = OrderManager(FakeBroker(script=["fill", "reject", "fill"]), audit)
        for i in range(3):
            oms.submit(intent(f"C{i}"), decision_price=D("1.08500"))
        rep = oms.execution_report()
        assert rep["n"] == 3 and rep["rejects"] == 1
        assert 0.3 < rep["reject_rate"] < 0.4


class TestReconciliation:
    def test_an_orphan_position_halts_and_gets_a_stop(self, audit):
        broker = FakeBroker()
        broker._positions = [Position("EUR_USD", Side.BUY, D("0.1"), D("1.0850"), 0,
                                      broker_stop_confirmed=False)]
        rec = Reconciler(broker, audit)
        report = rec.reconcile(local_positions=[])
        assert report.halt_required is True
        kinds = {m.kind for m in report.mismatches}
        assert "orphan" in kinds
        # An unprotected position gets a stop before anything else happens.
        assert broker._positions[0].broker_stop_confirmed is True

    def test_a_phantom_position_is_reported(self, audit):
        broker = FakeBroker()
        rec = Reconciler(broker, audit)
        local = [Position("EUR_USD", Side.BUY, D("0.1"), D("1.0850"), 0,
                          broker_stop_confirmed=True)]
        report = rec.reconcile(local_positions=local)
        assert "phantom" in {m.kind for m in report.mismatches}

    def test_size_drift_adopts_the_venue(self, audit):
        broker = FakeBroker()
        broker._positions = [Position("EUR_USD", Side.BUY, D("0.20"), D("1.0850"), 0,
                                      stop_loss=D("1.08"), broker_stop_confirmed=True)]
        rec = Reconciler(broker, audit)
        local = [Position("EUR_USD", Side.BUY, D("0.10"), D("1.0850"), 0)]
        report = rec.reconcile(local_positions=local)
        drift = [m for m in report.mismatches if m.kind == "size_drift"]
        assert drift and drift[0].venue == "0.20"

    def test_unresolved_orders_force_a_halt(self, audit):
        rec = Reconciler(FakeBroker(), audit)
        report = rec.reconcile(local_positions=[], unresolved_order_ids=["C1"])
        assert report.halt_required is True and report.ok is False

    def test_a_clean_book_reconciles_quietly(self, audit):
        rec = Reconciler(FakeBroker(), audit)
        report = rec.reconcile(local_positions=[])
        assert report.ok is True and report.halt_required is False


class TestDegradedVenues:
    def test_capabilities_report_what_is_missing(self):
        caps = BrokerCapabilities(False, False, False, False, True,
                                  D("0.01"), D("0.01"), "mt5")
        report = caps.degradation_report()
        assert len(report) == 4
        assert any("client-side order id" in r for r in report)
        assert any("venue-side stop" in r for r in report)
