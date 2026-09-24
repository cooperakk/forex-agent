"""TradingView reference data: protocol, stream, history, ratings, the guard.

No test here touches the network. The websocket is a scripted fake that
speaks the framing TradingView uses, so each test pins down one thing the
port must get right -- a heartbeat not echoed is a dropped connection; a
crossed bid/ask stored is a wrong mid; a stale reference that blocks is an
outage handed to a website.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from decimal import Decimal as D

import pyotp
import pytest
from fastapi.testclient import TestClient

from sentinel.agent.memory import MemoryStore
from sentinel.agent.orchestrator import Agent
from sentinel.agent.proposals import ProposalQueue
from sentinel.api.main import create_app
from sentinel.api.security import SecurityManager
from sentinel.api.state import Runtime
from sentinel.brokers.paper import PaperBroker, SimProfile
from sentinel.core.audit import AuditLog
from sentinel.core.config import (
    AgentConfig,
    AgentMode,
    ExecutionConfig,
    OpsConfig,
    ReferenceConfig,
    SentinelConfig,
)
from sentinel.core.money import Instrument
from sentinel.core.types import Quote
from sentinel.data import tradingview as tv
from sentinel.data.feed import BarStore, MarketFeed
from sentinel.data.reference import ReferenceDesk, ReferenceGuard
from sentinel.research.verdicts import runtime_policy

EU = Instrument("EUR_USD", "EUR", "USD")
T0 = int(dt.datetime(2026, 3, 3, 10, tzinfo=dt.timezone.utc).timestamp() * 1e9)
SEC = 1_000_000_000


# --------------------------------------------------------------------------- #
# a scripted websocket
# --------------------------------------------------------------------------- #


def frame(*packets):
    return "".join(tv.format_packet(p) for p in packets)


class FakeConn:
    """Replays ``script`` (frames, or callables given the sent log). When the
    script runs out it calls ``on_empty`` (default: stop the stream) and then
    behaves like a quiet socket."""

    def __init__(self, script, *, on_empty=None):
        self.script = list(script)
        self.sent = []
        self.closed = False
        self.on_empty = on_empty

    def send(self, data):
        self.sent.append(data)

    def recv(self, timeout):
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item(self.sent) if callable(item) else item
        if self.on_empty is not None:
            self.on_empty()
        raise TimeoutError

    def close(self):
        self.closed = True

    def messages(self):
        out = []
        for raw in self.sent:
            packets, _ = tv.parse_frame(raw)
            out.extend(packets)
        return out

    def session(self, prefix):
        for p in self.messages():
            if isinstance(p, dict) and isinstance(p.get("p"), list) and p["p"] \
                    and str(p["p"][0]).startswith(prefix + "_"):
                return p["p"][0]
        raise AssertionError(f"no {prefix} session was created")


def qsd(symbol, values, *, status="ok"):
    def make(sent):
        qs = next(p["p"][0] for raw in sent for p in tv.parse_frame(raw)[0]
                  if isinstance(p, dict) and p.get("m") == "quote_create_session")
        return frame({"m": "qsd", "p": [qs, {"n": tv._quote_key(symbol), "s": status,
                                             "v": values}]})
    return make


# --------------------------------------------------------------------------- #
# framing
# --------------------------------------------------------------------------- #


class TestFraming:
    def test_the_length_prefix_is_the_body_length(self):
        raw = tv.message("quote_create_session", ["qs_abc"])
        body = '{"m":"quote_create_session","p":["qs_abc"]}'
        assert raw == f"~m~{len(body)}~m~{body}"

    def test_non_ascii_is_escaped_so_characters_equal_bytes(self):
        raw = tv.message("x", ["é€"])
        body = raw.split("~m~", 2)[2]
        assert body.isascii() and len(body) == len(body.encode("utf-8"))

    def test_a_frame_with_several_packets_heartbeats_and_junk(self):
        raw = (tv.format_packet({"m": "a", "p": [1]}) + tv.format_packet("~h~7")
               + "~m~5~m~{bad}" + tv.format_packet({"m": "b", "p": [2]}))
        packets, dropped = tv.parse_frame(raw)
        assert packets[0] == {"m": "a", "p": [1]}
        assert isinstance(packets[1], tv.Heartbeat) and packets[1].n == 7
        assert packets[2] == {"m": "b", "p": [2]}
        assert dropped == 1

    def test_an_oversized_frame_is_refused(self):
        with pytest.raises(tv.TradingViewError):
            tv.parse_frame("~m~1~m~" + "x" * (tv.MAX_FRAME_CHARS + 1))

    def test_session_ids(self):
        a, b = tv.session_id("qs"), tv.session_id("qs")
        assert a.startswith("qs_") and len(a) == 15 and a != b


class TestSymbols:
    @pytest.mark.parametrize("sym", ["OANDA:EURUSD", "FX_IDC:USDJPY", "CME_MINI:ES1!",
                                     "tvc:dxy"])
    def test_valid(self, sym):
        assert tv.validate_symbol(sym) == sym.upper()

    @pytest.mark.parametrize("sym", ["EURUSD", "OANDA:", ":EURUSD", 'OANDA:EUR"USD',
                                     "OANDA:EUR USD", "A" * 30 + ":X", "OANDA:EURUSD\n"])
    def test_invalid(self, sym):
        with pytest.raises(ValueError):
            tv.validate_symbol(sym if not sym.endswith("\n") else sym + "}")

    def test_default_mapping(self):
        assert tv.default_symbol("EUR_USD") == "OANDA:EURUSD"
        assert tv.default_symbol("XAU_USD", "FX_IDC") == "FX_IDC:XAUUSD"
        assert tv.default_symbol("EUR_USD", "bad exchange") is None


class TestQuoteUpdates:
    def test_prices_are_validated_one_by_one(self):
        q = tv.TVQuote("OANDA:EURUSD")
        moved = tv.apply_quote_update(q, {"lp": 1.1, "bid": "nan", "ask": -1,
                                          "high_price": True}, T0)
        assert moved and q.last == 1.1 and q.bid is None and q.ask is None
        assert q.high is None
        assert q.mid == 1.1

    def test_a_crossed_book_drops_both_sides(self):
        q = tv.TVQuote("OANDA:EURUSD")
        tv.apply_quote_update(q, {"bid": 1.1002, "ask": 1.1000, "lp": 1.1001}, T0)
        assert q.bid is None and q.ask is None and q.mid == 1.1001

    def test_price_time_moves_only_with_the_price(self):
        q = tv.TVQuote("OANDA:EURUSD")
        tv.apply_quote_update(q, {"bid": 1.1, "ask": 1.1002}, T0)
        assert q.price_ns == T0 and q.mid == pytest.approx(1.1001)
        tv.apply_quote_update(q, {"description": "Euro <em>x</em>"}, T0 + 5 * SEC)
        assert q.price_ns == T0 and q.received_ns == T0 + 5 * SEC
        assert q.description == "Euro x"

    def test_delay_is_read_from_the_update_mode(self):
        q = tv.TVQuote("NASDAQ:AAPL", update_mode="delayed_streaming_900")
        assert q.delayed


# --------------------------------------------------------------------------- #
# the stream
# --------------------------------------------------------------------------- #


def _stream(conn=None, **kw):
    kw.setdefault("clock", lambda: T0)
    return tv.TradingViewStream(connect=lambda: conn, **kw)


class TestStream:
    def test_the_handshake_and_the_subscription(self):
        s = _stream()
        s.set_symbols(["OANDA:EURUSD", "bad symbol", "OANDA:EURUSD"])
        conn = FakeConn([frame({"session_id": "x", "release": "r-1"})],
                        on_empty=s._stop.set)
        s.run_session(conn)
        msgs = [p for p in conn.messages() if isinstance(p, dict)]
        assert [m["m"] for m in msgs] == ["set_auth_token", "quote_create_session",
                                         "quote_set_fields", "quote_add_symbols"]
        assert msgs[0]["p"] == ["unauthorized_user_token"]
        assert msgs[3]["p"][1:] == [tv._quote_key("OANDA:EURUSD")]
        assert "lp" in msgs[2]["p"] and "update_mode" in msgs[2]["p"]
        assert s.status.server_release == "r-1"

    def test_heartbeats_are_echoed(self):
        s = _stream()
        conn = FakeConn([tv.format_packet("~h~42")], on_empty=s._stop.set)
        s.run_session(conn)
        assert tv.format_packet("~h~42") in conn.sent

    def test_quotes_are_stored_and_foreign_sessions_ignored(self):
        s = _stream()
        s.set_symbols(["OANDA:EURUSD"])
        conn = FakeConn([
            qsd("OANDA:EURUSD", {"bid": 1.1, "ask": 1.1002, "update_mode": "streaming",
                                 "current_session": "market"}),
            frame({"m": "qsd", "p": ["qs_other", {"n": "OANDA:EURUSD", "s": "ok",
                                                  "v": {"lp": 9.9}}]}),
            qsd("OANDA:GBPUSD", {"lp": 1.3}),        # never asked for
        ], on_empty=s._stop.set)
        s.run_session(conn)
        q = s.quote("OANDA:EURUSD")
        assert q.mid == pytest.approx(1.1001) and q.last is None
        assert s.quote("OANDA:GBPUSD") is None

    def test_a_symbol_error_is_reported_not_raised(self):
        s = _stream()
        s.set_symbols(["OANDA:NOPE"])
        conn = FakeConn([qsd("OANDA:NOPE", {}, status="error")], on_empty=s._stop.set)
        s.run_session(conn)
        assert "OANDA:NOPE" in s.status.symbol_errors
        assert s.quote("OANDA:NOPE").mid is None

    def test_a_protocol_error_ends_the_session(self):
        s = _stream()
        conn = FakeConn([frame({"m": "protocol_error", "p": ["wrong data"]})])
        with pytest.raises(tv.TradingViewError):
            s.run_session(conn)

    def test_silence_ends_the_session(self):
        s = _stream(silence_timeout=0.0)
        conn = FakeConn([TimeoutError()])
        with pytest.raises(tv.TradingViewError, match="no message"):
            s.run_session(conn)

    def test_unsubscribing_removes_the_symbol_and_its_quote(self):
        s = _stream()
        s.set_symbols(["OANDA:EURUSD", "OANDA:USDJPY"])

        def drop(sent):
            s.set_symbols(["OANDA:USDJPY"])
            return tv.format_packet("~h~1")
        conn = FakeConn([qsd("OANDA:EURUSD", {"lp": 1.1}), drop, tv.format_packet("~h~2")],
                        on_empty=s._stop.set)
        s.run_session(conn)
        removes = [p for p in conn.messages()
                   if isinstance(p, dict) and p.get("m") == "quote_remove_symbols"]
        assert removes and removes[0]["p"][1] == tv._quote_key("OANDA:EURUSD")
        assert s.quote("OANDA:EURUSD") is None

    def test_the_symbol_count_is_capped(self):
        s = _stream(max_symbols=3)
        kept = s.set_symbols([f"OANDA:X{i}" for i in range(10)])
        assert len(kept) == 3

    def test_the_worker_survives_a_failing_connect_and_stops_promptly(self):
        calls = []

        def boom():
            calls.append(1)
            raise OSError("proxy rejected connection: HTTP 403")
        s = tv.TradingViewStream(connect=boom, backoff_min=0.01, backoff_max=0.02)
        s.start()
        deadline = time.monotonic() + 3
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        s.stop()
        assert len(calls) >= 2 and "403" in s.status.last_error
        assert not s.running and s.status.reconnects >= 1


    def test_a_quick_off_and_on_never_leaves_two_workers(self):
        import threading

        opened = []

        def slow_connect():
            opened.append(1)
            time.sleep(0.3)                     # stuck in a connect
            return FakeConn([])
        s = tv.TradingViewStream(connect=slow_connect, backoff_min=0.01, backoff_max=0.02,
                                 silence_timeout=0.05, recv_timeout=0.01)
        s.start()
        time.sleep(0.05)
        s.stop(join=0.01)                       # returns while the worker still connects
        s.start()
        time.sleep(0.8)
        workers = [t for t in threading.enumerate() if t.name == "tradingview"]
        s.stop()
        assert len(workers) == 1


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #


def _bars(start, n, step=14400, price=1.1):
    rows = []
    for i in range(n):
        o = price + i * 1e-4
        rows.append({"i": i, "v": [start + i * step, o, o + 5e-4, o - 5e-4, o + 1e-4, 100]})
    return rows


def chart(kind, *rest):
    def make(sent):
        cs = next(p["p"][0] for raw in sent for p in tv.parse_frame(raw)[0]
                  if isinstance(p, dict) and p.get("m") == "chart_create_session")
        return frame({"m": kind, "p": [cs, *rest]})
    return make


class TestHistory:
    START = 1_767_225_600      # 2026-01-01T00:00Z

    def test_bars_become_a_validated_frame_without_the_open_bar(self):
        rows = _bars(self.START, 5)
        rows.insert(2, {"i": 99, "v": [self.START + 99, 1.1, 1.0, 1.2, 1.1, 1]})  # h < l
        conn = FakeConn([
            chart("symbol_resolved", "ser_1", {"description": "EUR/USD", "pricescale": 100000,
                                               "evil": {"x": 1}}),
            chart("timescale_update", {"$prices": {"s": rows}}),
            chart("series_completed", "s1"),
        ])
        now = self.START + 4 * 14400 + 60            # the fifth bar is still open
        series = tv.fetch_bars("OANDA:EURUSD", "H4", 10, connect=lambda: conn, now_s=now)
        assert len(series.bars) == 4 and series.dropped_incomplete == 1
        assert series.info == {"description": "EUR/USD", "pricescale": 100000}
        frame_ = series.to_frame()
        assert str(frame_.index.tz) == "UTC" and list(frame_.columns) == [
            "open", "high", "low", "close", "volume"]
        sent = [p for p in conn.messages() if isinstance(p, dict)]
        assert [m["m"] for m in sent][:4] == ["set_auth_token", "chart_create_session",
                                              "resolve_symbol", "create_series"]
        assert sent[3]["p"][4] == "240" and sent[3]["p"][5] == 10
        assert conn.closed

    def test_more_is_requested_until_a_round_comes_back_short(self, monkeypatch):
        monkeypatch.setattr(tv, "BATCH_BARS", 3)
        first, second = _bars(self.START + 5 * 14400, 3), _bars(self.START + 2 * 14400, 3)
        third = _bars(self.START, 2)
        conn = FakeConn([
            chart("timescale_update", {"$prices": {"s": first}}),
            chart("series_completed", "s1"),
            chart("timescale_update", {"$prices": {"s": second}}),
            chart("series_completed", "s1"),
            chart("timescale_update", {"$prices": {"s": third}}),
            chart("series_completed", "s1"),      # asked for 3, got 2: exhausted
        ])
        series = tv.fetch_bars("OANDA:EURUSD", "H4", 50, connect=lambda: conn,
                               now_s=self.START + 10 * 86400)
        assert len(series.bars) == 8
        assert [b[0] for b in series.bars] == sorted(b[0] for b in series.bars)
        more = [p for p in conn.messages() if isinstance(p, dict)
                and p.get("m") == "request_more_data"]
        assert [m["p"][2] for m in more] == [3, 3]

    def test_a_short_first_answer_is_the_whole_history(self):
        conn = FakeConn([chart("timescale_update", {"$prices": {"s": _bars(self.START, 4)}}),
                         chart("series_completed", "s1")])
        series = tv.fetch_bars("OANDA:EURUSD", "H4", 100, connect=lambda: conn,
                               now_s=self.START + 10 * 86400)
        assert len(series.bars) == 4
        assert not [p for p in conn.messages() if isinstance(p, dict)
                    and p.get("m") == "request_more_data"]

    def test_a_series_error_raises(self):
        conn = FakeConn([chart("series_error", "s1", "invalid symbol")])
        with pytest.raises(tv.TradingViewError, match="series_error"):
            tv.fetch_bars("OANDA:EURUSD", "H4", 10, connect=lambda: conn)

    def test_no_answer_times_out(self):
        conn = FakeConn([])
        with pytest.raises(tv.TradingViewError, match="no complete answer"):
            tv.fetch_bars("OANDA:EURUSD", "H4", 10, connect=lambda: conn, timeout=0.05)

    def test_bad_arguments_are_refused_before_connecting(self):
        with pytest.raises(ValueError):
            tv.fetch_bars("OANDA:EURUSD", "H3", 10, connect=lambda: 1 / 0)
        with pytest.raises(ValueError):
            tv.fetch_bars("not a symbol", "H4", 10, connect=lambda: 1 / 0)


# --------------------------------------------------------------------------- #
# ratings and search
# --------------------------------------------------------------------------- #


class TestRatingsAndSearch:
    def test_the_scanner_request_and_its_answer(self):
        seen = {}

        def request(method, url, *, json_body=None, params=None, timeout=15.0):
            seen.update(method=method, url=url, body=json_body)
            cols = json_body["columns"]
            values = [0.6 if c.startswith("Recommend.All") else -0.3 for c in cols]
            values[0] = 7.0                                   # out of range
            return {"data": [{"s": "OANDA:EURUSD", "d": values},
                             {"s": "evil symbol", "d": values}]}
        out = tv.fetch_ta(["OANDA:EURUSD", "not valid", "OANDA:EURUSD"], request=request)
        assert seen["method"] == "POST" and seen["url"] == tv.SCAN_URL
        assert seen["body"]["symbols"]["tickers"] == ["OANDA:EURUSD"]
        assert "Recommend.All|240" in seen["body"]["columns"]
        assert "Recommend.All" in seen["body"]["columns"]          # daily has no suffix
        eu = out["OANDA:EURUSD"]
        assert eu["15"]["all"] is None and eu["15"]["label"] == "unknown"
        assert eu["240"] == {"all": 0.6, "ma": -0.3, "other": -0.3, "label": "strong_buy"}
        assert list(out) == ["OANDA:EURUSD"]

    @pytest.mark.parametrize("value,label", [(-0.8, "strong_sell"), (-0.3, "sell"),
                                             (0.0, "neutral"), (0.3, "buy"),
                                             (0.9, "strong_buy")])
    def test_labels(self, value, label):
        assert tv.rating_label(value) == label

    def test_search_cleans_what_it_returns(self):
        def request(method, url, *, json_body=None, params=None, timeout=15.0):
            assert params["exchange"] == "OANDA" and params["text"] == "EURUSD"
            return {"symbols": [
                {"symbol": "<em>EURUSD</em>", "description": "Euro / U.S. Dollar",
                 "type": "forex", "exchange": "OANDA"},
                {"symbol": "BAD SYMBOL", "exchange": "X"},
            ]}
        out = tv.search_symbols("oanda:eur/usd", request=request)
        assert out == [{"id": "OANDA:EURUSD", "symbol": "EURUSD", "exchange": "OANDA",
                        "description": "Euro / U.S. Dollar", "type": "forex"}]

    def test_search_refuses_unknown_types(self):
        with pytest.raises(ValueError):
            tv.search_symbols("EURUSD", "weird", request=lambda *a, **k: {})


# --------------------------------------------------------------------------- #
# the guard
# --------------------------------------------------------------------------- #


class FakeStream:
    def __init__(self):
        self.q = {}
        self.running = False
        self.symbols = []
        self.starts = 0

    def put(self, symbol, mid, *, spread=0.0002, at=T0, **kw):
        self.q[symbol] = tv.TVQuote(symbol, bid=mid - spread / 2, ask=mid + spread / 2,
                                    price_ns=at, received_ns=at,
                                    update_mode=kw.get("update_mode", "streaming"),
                                    session=kw.get("session", "market"))

    def quote(self, symbol):
        return self.q.get(symbol)

    def set_symbols(self, symbols):
        self.symbols = list(symbols)
        return self.symbols

    def start(self):
        self.running = True
        self.starts += 1

    def stop(self):
        self.running = False

    def snapshot(self):
        return {"status": {"running": self.running},
                "quotes": {k: v.to_dict() for k, v in self.q.items()}}


def _guard(**cfg):
    config = ReferenceConfig(enabled=True, **cfg)
    stream = FakeStream()
    events = []
    guard = ReferenceGuard(lambda: config, stream, audit=events.append)
    return guard, stream, events


def _q(mid, spread=D("0.00006")):
    mid = D(str(mid))
    return Quote("EUR_USD", mid - spread / 2, mid + spread / 2, ts_ns=T0, received_ns=T0)


class TestGuard:
    def test_off_means_nothing(self):
        guard = ReferenceGuard(lambda: ReferenceConfig(), FakeStream())
        assert guard.assess(T0, {"EUR_USD": _q(1.1)}) == {}

    def test_agreement_is_ok(self):
        guard, stream, _ = _guard()
        stream.put("OANDA:EURUSD", 1.10002)
        c = guard.assess(T0, {"EUR_USD": _q(1.1)}, {"EUR_USD": EU})["EUR_USD"]
        assert c.status == "ok" and c.size_multiplier == 1.0
        assert c.divergence_pips == pytest.approx(0.2, abs=0.01)

    def test_shrink_then_block(self):
        guard, stream, _ = _guard()
        stream.put("OANDA:EURUSD", 1.1008)                       # ~7.3 bp
        c = guard.assess(T0, {"EUR_USD": _q(1.1)})["EUR_USD"]
        assert c.status == "shrink" and c.size_multiplier == 0.5
        stream.put("OANDA:EURUSD", 1.1020)                       # ~18 bp
        c = guard.assess(T0, {"EUR_USD": _q(1.1)})["EUR_USD"]
        assert c.blocked and c.size_multiplier == 0.0

    def test_a_wide_broker_spread_widens_the_thresholds(self):
        guard, stream, _ = _guard()
        stream.put("OANDA:EURUSD", 1.1008)                       # 7.3 bp away
        c = guard.assess(T0, {"EUR_USD": _q(1.1, spread=D("0.0010"))})["EUR_USD"]
        # the spread is ~9 bp, so shrink starts at 18 bp and block at 36 bp
        assert c.status == "ok" and c.shrink_at_bp == pytest.approx(18.18, abs=0.05)

    @pytest.mark.parametrize("setup,status", [
        (lambda s: None, "unavailable"),
        (lambda s: s.put("OANDA:EURUSD", 1.2, at=T0 - 600 * SEC), "stale"),
        (lambda s: s.put("OANDA:EURUSD", 1.2, update_mode="delayed_streaming_900"),
         "delayed"),
        (lambda s: s.put("OANDA:EURUSD", 1.2, session="out_of_session"), "closed"),
    ])
    def test_a_missing_reference_changes_nothing(self, setup, status):
        guard, stream, events = _guard()
        setup(stream)
        c = guard.assess(T0, {"EUR_USD": _q(1.1)})["EUR_USD"]
        assert c.status == status and not c.blocked and c.size_multiplier == 1.0
        assert events == []

    def test_an_explicit_mapping_wins(self):
        guard, stream, _ = _guard(symbol_map={"EUR_USD": "fx_idc:eurusd"})
        stream.put("FX_IDC:EURUSD", 1.1)
        stream.put("OANDA:EURUSD", 1.3)
        c = guard.assess(T0, {"EUR_USD": _q(1.1)})["EUR_USD"]
        assert c.symbol == "FX_IDC:EURUSD" and c.status == "ok"

    def test_a_block_is_journalled_once_and_its_clearing_too(self):
        guard, stream, events = _guard()
        stream.put("OANDA:EURUSD", 1.1020)
        for _ in range(3):
            guard.assess(T0, {"EUR_USD": _q(1.1)})
        stream.put("OANDA:EURUSD", 1.1)
        guard.assess(T0, {"EUR_USD": _q(1.1)})
        assert [list(e)[0] for e in events] == ["reference_block", "reference_block_cleared"]
        view = guard.view()["checks"][0]
        assert view["blocks"] == 3 and view["checks"] == 4 and len(view["recent_gap_bp"]) == 4


class TestConfig:
    def test_block_below_shrink_is_refused(self):
        with pytest.raises(ValueError):
            ReferenceConfig(shrink_bp=10, block_bp=5)

    def test_the_map_is_normalised_and_checked(self):
        assert ReferenceConfig(symbol_map={"EUR_USD": "oanda:eurusd"}).symbol_map == {
            "EUR_USD": "OANDA:EURUSD"}
        for bad in ({"EUR_USD": "EURUSD"}, {"eur usd": "OANDA:EURUSD"},
                    {"EUR_USD": 'OANDA:X"}'}):
            with pytest.raises(ValueError):
                ReferenceConfig(symbol_map=bad)

    def test_the_reference_is_not_part_of_the_verdict_policy(self):
        a = SentinelConfig()
        b = SentinelConfig(reference=ReferenceConfig(enabled=True, block_bp=40))
        assert runtime_policy(a) == runtime_policy(b)


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #


def _agent(tmp_path, reference_cfg=None):
    broker = PaperBroker(instruments={"EUR_USD": EU}, starting_balance=D("10000"),
                         profile=SimProfile(last_look_reject_prob=0.0,
                                            slippage_pips_mean=D("0"),
                                            slippage_pips_sigma=D("0")),
                         seed=3, start_ns=T0)
    broker.on_quote(Quote("EUR_USD", D("1.08497"), D("1.08503"), ts_ns=T0))
    cfg = SentinelConfig(
        agent=AgentConfig(mode=AgentMode.ADVISORY, session_windows_utc=[[0, 24]],
                          trade_days=[0, 1, 2, 3, 4, 5, 6]),
        execution=ExecutionConfig(broker="paper"),
        reference=reference_cfg or ReferenceConfig(enabled=True),
        ops=OpsConfig(state_dir=str(tmp_path), killswitch_file=str(tmp_path / "KILL"),
                      audit_log=str(tmp_path / "audit.jsonl")))
    agent = Agent(cfg, broker, MarketFeed(broker, BarStore(tmp_path / "m.db")),
                  AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False),
                  MemoryStore(tmp_path / "mem.db"),
                  proposals=ProposalQueue(str(tmp_path / "p.json")),
                  clock_fn=lambda: broker.now_ns)
    from sentinel.core.types import DataQuality
    agent.feed.store.passport = lambda *a, **k: type(
        "P", (), {"quality": DataQuality.OK, "age_sec": 0.0})()
    stream = FakeStream()
    agent.reference = ReferenceGuard(lambda: agent.config.reference, stream)
    agent.start()
    return agent, broker, stream


TICKET = dict(instrument="EUR_USD", side="BUY", stop_loss=D("1.08003"),
              take_profit=D("1.09503"), by="owner")


class TestEngine:
    def test_a_diverging_broker_price_is_vetoed(self, tmp_path):
        agent, broker, stream = _agent(tmp_path)
        stream.put("OANDA:EURUSD", 1.0870)                       # ~18 bp away
        d = agent.manual_order(**TICKET, preview=True)
        assert d.action == "vetoed"
        assert any(v["rule"] == "reference_divergence" for v in d.vetoes), d.vetoes

    def test_agreement_lets_the_ticket_through_at_full_size(self, tmp_path):
        agent, _, stream = _agent(tmp_path)
        stream.put("OANDA:EURUSD", 1.08501)
        full = agent.manual_order(**TICKET, preview=True)
        assert full.action == "preview", full.vetoes

        agent2, _, stream2 = _agent(tmp_path / "b")
        stream2.put("OANDA:EURUSD", 1.0858)                      # ~7 bp: shrink
        half = agent2.manual_order(**TICKET, preview=True)
        assert half.action == "preview", half.vetoes
        assert D(half.lots) <= D(full.lots) / 2 + D("0.01")
        assert half.diagnostics["caution_multiplier"] == 0.5

    def test_no_reference_means_no_effect(self, tmp_path):
        agent, _, _ = _agent(tmp_path)
        d = agent.manual_order(**TICKET, preview=True)
        assert d.action == "preview", d.vetoes

    def test_switching_the_reference_off_lifts_a_standing_block(self, tmp_path):
        agent, _, stream = _agent(tmp_path)
        stream.put("OANDA:EURUSD", 1.0870)
        assert agent.manual_order(**TICKET, preview=True).action == "vetoed"
        agent.config = agent.config.model_copy(
            update={"reference": ReferenceConfig(enabled=False)})
        d = agent.manual_order(**TICKET, preview=True)
        assert d.action == "preview", d.vetoes

    def test_a_guard_that_raises_changes_nothing(self, tmp_path):
        agent, _, _ = _agent(tmp_path)

        def boom(*a, **k):
            raise RuntimeError("bug")
        agent.reference.assess = boom
        assert agent.manual_order(**TICKET, preview=True).action == "preview"


class TestDesk:
    def test_off_stops_and_unsubscribes(self):
        cfg = ReferenceConfig(enabled=False)
        stream = FakeStream()
        stream.running = True
        desk = ReferenceDesk(lambda: cfg, ReferenceGuard(lambda: cfg, stream), stream,
                             lambda: ["EUR_USD"], ta_fetch=lambda s: 1 / 0)
        desk.tick(T0)
        assert not stream.running and stream.symbols == []

    def test_on_subscribes_and_rates_on_schedule(self):
        cfg = ReferenceConfig(enabled=True, ta_every_min=15)
        stream = FakeStream()
        calls = []

        def ta(symbols):
            calls.append(list(symbols))
            return {"OANDA:EURUSD": {"240": {"all": 0.2, "label": "buy"}}}
        desk = ReferenceDesk(lambda: cfg, ReferenceGuard(lambda: cfg, stream), stream,
                             lambda: ["EUR_USD", "EUR_USD", "USD_JPY"], ta_fetch=ta)
        desk.tick(T0)
        desk.tick(T0 + 60 * SEC)
        assert stream.running and stream.symbols == ["OANDA:EURUSD", "OANDA:USDJPY"]
        assert len(calls) == 1 and desk.ta == {"EUR_USD": {"240": {"all": 0.2,
                                                                   "label": "buy"}}}
        desk.tick(T0 + 16 * 60 * SEC)
        assert len(calls) == 2

    def test_a_ratings_failure_is_reported(self):
        cfg = ReferenceConfig(enabled=True)
        stream = FakeStream()

        def ta(symbols):
            raise tv.TradingViewError("scanner.tradingview.com answered 403")
        desk = ReferenceDesk(lambda: cfg, ReferenceGuard(lambda: cfg, stream), stream,
                             lambda: ["EUR_USD"], ta_fetch=ta)
        desk.tick(T0)
        assert "403" in desk.ta_error and desk.view()["enabled"] is True


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(tmp_path):
    os.environ["SENTINEL_JWT_SECRET"] = "t" * 48
    agent, broker, stream = _agent(tmp_path, ReferenceConfig())
    runtime = Runtime(agent, tmp_path / "config.json")
    runtime.reference = ReferenceDesk(lambda: agent.config.reference, agent.reference,
                                      stream, runtime.reference_instruments,
                                      ta_fetch=lambda s: {})
    security = SecurityManager(agent.audit, secret="t" * 48)
    owner, _ = security.add_user("owner1", "a-sufficiently-long-password", "owner")
    security.add_user("viewer1", "another-long-password-x", "viewer")
    client = TestClient(create_app(runtime, security))

    def login(name, pw):
        r = client.post("/api/auth/login", json={"username": name, "password": pw})
        return {"Authorization": f"Bearer {r.json()['token']}"}
    oh = login("owner1", "a-sufficiently-long-password")

    def totp():
        # A code is single-use; tests that write twice inside one 30-second
        # window would otherwise be refused as a replay -- correctly.
        security._used_totp.clear()
        return dict(oh, **{"X-TOTP": pyotp.TOTP(owner.totp_secret).now()})
    return {"client": client, "runtime": runtime, "stream": stream, "oh": oh,
            "vh": login("viewer1", "another-long-password-x"), "totp": totp}


class TestApi:
    def test_everyone_signed_in_may_read(self, api):
        r = api["client"].get("/api/reference", headers=api["vh"])
        assert r.status_code == 200 and r.json()["enabled"] is False

    def test_settings_need_the_owner_and_the_second_factor(self, api):
        c = api["client"]
        body = {"enabled": True, "symbol_map": {"EUR_USD": "FX_IDC:EURUSD"}}
        assert c.post("/api/reference/settings", json=body,
                      headers=api["vh"]).status_code == 403
        assert c.post("/api/reference/settings", json=body,
                      headers=api["oh"]).status_code == 403
        r = c.post("/api/reference/settings", json=body, headers=api["totp"]())
        assert r.status_code == 200, r.text
        cfg = api["runtime"].agent.config.reference
        assert cfg.enabled and cfg.symbol_map == {"EUR_USD": "FX_IDC:EURUSD"}
        # the strategies' instruments follow the default exchange; the mapped
        # one uses its explicit symbol
        assert api["stream"].running and "FX_IDC:EURUSD" in api["stream"].symbols
        assert "OANDA:EURUSD" not in api["stream"].symbols
        # the map is REPLACED: an emptied table really is empty
        r = c.post("/api/reference/settings", json={"symbol_map": {}},
                   headers=api["totp"]())
        assert r.status_code == 200 and api["runtime"].agent.config.reference.symbol_map == {}

    def test_invalid_settings_are_refused(self, api):
        r = api["client"].post("/api/reference/settings", headers=api["totp"](),
                               json={"symbol_map": {"EUR_USD": "not a symbol"}})
        assert r.status_code == 400
        r = api["client"].post("/api/reference/settings", headers=api["totp"](),
                               json={"shrink_bp": 20, "block_bp": 10})
        assert r.status_code == 400

    def test_search_is_for_the_owner_and_rate_limited(self, api, monkeypatch):
        monkeypatch.setattr(tv, "search_symbols",
                            lambda q, kind: [{"id": "OANDA:EURUSD", "symbol": "EURUSD"}])
        c = api["client"]
        assert c.get("/api/reference/search?q=EUR", headers=api["vh"]).status_code == 403
        codes = [c.get("/api/reference/search?q=EUR", headers=api["oh"]).status_code
                 for _ in range(11)]
        assert codes[:10] == [200] * 10 and codes[10] == 429

    def test_saving_settings_does_not_wait_for_the_ratings(self, api):
        calls = []
        api["runtime"].reference._ta_fetch = lambda symbols: calls.append(1) or {}
        r = api["client"].post("/api/reference/settings", headers=api["totp"](),
                               json={"enabled": True})
        assert r.status_code == 200 and calls == []
        api["runtime"].background_tick()
        assert calls == [1]

    def test_the_background_worker_never_calls_the_venue(self, api):
        runtime = api["runtime"]
        runtime.agent._position_meta["AUD_USD"] = {"strategy": "manual"}

        def forbidden():
            raise AssertionError("broker.positions() called off the decision thread")
        runtime.agent.broker.positions = forbidden
        assert "AUD_USD" in runtime.reference_instruments()

    def test_refresh_refuses_while_off(self, api):
        h = api["totp"]()
        assert api["client"].post("/api/reference/refresh", headers=h).status_code == 409
