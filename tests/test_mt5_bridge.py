"""The MetaTrader bridge: the adapter, unchanged, driven over a socket.

This is the Ubuntu path. The ``MetaTrader5`` package exists only on Windows,
so an engine on a Linux server reaches the terminal through ``BridgeServer``
(next to the terminal) and ``BridgeMT5`` (inside the engine). The tests run
the server in a thread over a real loopback socket with the fake terminal
behind it, and then use ``MT5Broker`` exactly as production does -- quotes,
bars, positions, an order, a stop modification -- so the wire format is
proven by the adapter's own code paths rather than by a mirror of them.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from sentinel.brokers import build_broker, get_profile
from sentinel.brokers.mt5 import MT5Broker
from sentinel.brokers.mt5_bridge import (
    BridgeError, BridgeMT5, BridgeServer, Obj, _encode, bridge_from_env, parse_address,
)
from sentinel.core.types import OrderIntent, OrderState, Side
from tests.fake_mt5 import FakeMT5

TOKEN = "test-token-" + secrets.token_urlsafe(16)


@pytest.fixture
def bridge():
    fake = FakeMT5(suffix=".m", stops_level=10)
    server = BridgeServer(fake, token=TOKEN, host="127.0.0.1", port=0)
    host, port = server.start()
    try:
        yield fake, server, host, port
    finally:
        server.stop()


@pytest.fixture
def broker(bridge):
    fake, server, host, port = bridge
    client = BridgeMT5(host, port, token=TOKEN)
    b = MT5Broker(profile=get_profile("generic_mt5"), mt5_module=client)
    try:
        yield b, fake, server, client
    finally:
        client.close()


class TestWireFormat:
    def test_constants_arrive_as_attributes(self, bridge):
        _, _, host, port = bridge
        m = BridgeMT5(host, port, token=TOKEN)
        assert m.TIMEFRAME_H4 == 16388
        assert m.TRADE_RETCODE_DONE == 10009
        assert hasattr(m, "ORDER_FILLING_FOK") and not hasattr(m, "nonsense")

    def test_records_answer_both_attribute_and_item_access(self):
        o = Obj({"bid": 1.1, "nested": Obj({"x": 2})})
        assert o.bid == 1.1 and o["bid"] == 1.1 and o.nested.x == 2
        with pytest.raises(AttributeError):
            o.missing

    def test_numpy_structured_rates_encode_to_rows(self):
        np = pytest.importorskip("numpy")
        arr = np.array([(1700000000, 1.1, 1.2, 1.0, 1.15, 10)],
                       dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"),
                              ("low", "f8"), ("close", "f8"), ("tick_volume", "i8")])
        rows = _encode(arr)
        assert rows == [{"time": 1700000000, "open": 1.1, "high": 1.2, "low": 1.0,
                         "close": 1.15, "tick_volume": 10}]

    def test_datetimes_survive_the_round_trip(self, bridge):
        fake, server, host, port = bridge
        seen = {}

        def deals(since, until):
            seen["since"], seen["until"] = since, until
            return []
        fake.history_deals_get = deals
        m = BridgeMT5(host, port, token=TOKEN)
        since = datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc)
        m.history_deals_get(since, datetime.now(timezone.utc))
        assert seen["since"] == since and seen["since"].tzinfo is not None

    def test_parse_address(self):
        assert parse_address("127.0.0.1:5555") == ("127.0.0.1", 5555)
        assert parse_address("tcp://10.0.0.5:6000") == ("10.0.0.5", 6000)
        assert parse_address("localhost") == ("localhost", 5555)


class TestSecurity:
    def test_a_wrong_token_is_refused(self, bridge):
        _, server, host, port = bridge
        with pytest.raises(BridgeError, match="token"):
            BridgeMT5(host, port, token="wrong-token-wrong-token")
        assert server.rejected >= 1

    def test_only_allow_listed_methods_are_bridged(self, bridge):
        _, server, host, port = bridge
        m = BridgeMT5(host, port, token=TOKEN)
        with pytest.raises(BridgeError, match="not bridged"):
            m._call("__import__", "os")
        with pytest.raises(BridgeError, match="not bridged"):
            m._call("set_price", "EURUSD", 1.0, 1.0)

    def test_a_public_bind_is_refused_by_default(self):
        with pytest.raises(ValueError, match="loopback"):
            BridgeServer(FakeMT5(), token=TOKEN, host="0.0.0.0")
        s = BridgeServer(FakeMT5(), token=TOKEN, host="0.0.0.0", allow_remote=True)
        assert s.host == "0.0.0.0"

    def test_a_short_token_is_refused(self):
        with pytest.raises(ValueError):
            BridgeServer(FakeMT5(), token="short")

    def test_an_unreachable_bridge_is_a_clear_error(self):
        with pytest.raises(BridgeError, match="unreachable"):
            BridgeMT5("127.0.0.1", 1, token=TOKEN)


class TestAdapterOverTheBridge:
    def test_symbols_are_translated_through_the_bridge(self, broker):
        b, fake, *_ = broker
        assert "EUR_USD" in b.instruments()
        assert b._venue("EUR_USD") == "EURUSD.m"

    def test_a_quote_and_bars_arrive(self, broker):
        b, fake, *_ = broker
        q = b.quote("EUR_USD")
        assert q.bid < q.ask and q.source == "mt5"
        bars = b.fetch_bars("EUR_USD", "H4", 20)
        assert 19 <= len(bars) <= 21 and bars[-1].complete is False

    def test_the_server_clock_is_measured_through_the_bridge(self, broker):
        b, fake, *_ = broker
        b.quote("EUR_USD")
        assert b._server_offset_seconds() == fake.server_utc_offset_sec

    def test_account_and_positions(self, broker):
        b, fake, *_ = broker
        a = b.account()
        assert a.account_id == "1000001" and a.currency == "USD"
        # The fake reports no trade_mode, so the type is unknown -- the same
        # answer the adapter gives with the package in-process.
        direct = MT5Broker(profile=get_profile("generic_mt5"), mt5_module=fake)
        assert a.account_type == direct.account().account_type
        assert b.positions() == []

    def test_an_order_goes_through_and_a_position_comes_back(self, broker):
        b, fake, server, client = broker
        intent = OrderIntent(client_order_id="SFXbridge0001", strategy="t",
                             instrument="EUR_USD", side=Side.BUY, lots=D("0.10"),
                             stop_loss=D("1.09000"), take_profit=D("1.12000"))
        res = b.submit(intent, timeout_ms=5000)
        assert res.state is OrderState.FILLED, res.reject_reason
        assert fake.sent_requests[-1]["symbol"] == "EURUSD.m"
        pos = b.positions()
        assert len(pos) == 1 and pos[0].instrument == "EUR_USD"
        assert pos[0].broker_stop_confirmed is True
        # A stop tightened through the bridge.
        assert b.modify_position("EUR_USD", stop_loss=D("1.09500")) is True
        assert fake._positions[0].sl == pytest.approx(1.095)
        # And a close.
        closed = b.close_position("EUR_USD")
        assert closed.state in (OrderState.FILLED, OrderState.PARTIAL)
        assert b.positions() == []
        assert server.calls > 10

    def test_query_order_scans_deals_across_the_bridge(self, broker):
        b, fake, *_ = broker
        intent = OrderIntent(client_order_id="SFXbridge0002", strategy="t",
                             instrument="GBP_USD", side=Side.SELL, lots=D("0.05"),
                             stop_loss=D("1.29000"))
        b.submit(intent, timeout_ms=5000)
        found = b.query_order("SFXbridge0002")
        assert found is not None and found.state is OrderState.FILLED

    def test_the_bridge_survives_a_dropped_connection(self, broker):
        b, fake, server, client = broker
        b.quote("EUR_USD")
        client.close()                 # simulate the tunnel blinking
        q = b.quote("EUR_USD")         # reconnects transparently
        assert q.bid > 0


class TestWiring:
    def test_build_broker_uses_the_bridge_named_in_the_environment(self, bridge, monkeypatch):
        fake, server, host, port = bridge
        monkeypatch.setenv("SENTINEL_MT5_BRIDGE", f"{host}:{port}")
        monkeypatch.setenv("SENTINEL_MT5_BRIDGE_TOKEN", TOKEN)
        # No local MetaTrader5 package on this path: the bridge must be chosen.
        import sys
        monkeypatch.delitem(sys.modules, "MetaTrader5", raising=False)
        b = build_broker("amarkets")
        assert isinstance(b, MT5Broker)
        assert isinstance(b._mt5, BridgeMT5)
        assert b.quote("EUR_USD").ask > 0

    def test_discovery_finds_the_terminal_behind_the_bridge(self, bridge):
        fake, server, host, port = bridge
        from sentinel.brokers.connection import discover
        found = discover(environ={"SENTINEL_MT5_BRIDGE": f"{host}:{port}",
                                  "SENTINEL_MT5_BRIDGE_TOKEN": TOKEN})
        mt = [f for f in found if f["source"] == "metatrader5"]
        assert mt and mt[0]["login"] == "1000001"
        assert mt[0]["declared_account_type"] == "demo"

    def test_no_environment_means_no_bridge(self):
        assert bridge_from_env({}) is None
        with pytest.raises(BridgeError, match="TOKEN"):
            bridge_from_env({"SENTINEL_MT5_BRIDGE": "127.0.0.1:5555"})

    def test_the_probe_cannot_trade_through_the_bridge_either(self, bridge, monkeypatch):
        """The read-only guard wraps the adapter, and the adapter wraps the
        bridge, so a probe over the bridge is still structurally unable to
        send an order."""
        fake, server, host, port = bridge
        from sentinel.brokers.connection import BrokerConnection, ProbeRefused, ReadOnlyBroker
        client = BridgeMT5(host, port, token=TOKEN)
        b = MT5Broker(profile=get_profile("generic_mt5"), mt5_module=client)
        ro = ReadOnlyBroker(b)
        assert ro.account().account_id == "1000001"
        with pytest.raises(ProbeRefused):
            ro.submit(OrderIntent(client_order_id="x", strategy="t", instrument="EUR_USD",
                                  side=Side.BUY, lots=D("0.01"), stop_loss=D("1.0")),
                      timeout_ms=100)
        assert fake.sent_requests == []
