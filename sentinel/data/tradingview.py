"""TradingView market data: a reference price, technical ratings and history.

What this is
------------
A small, read-only Python port of the parts of TradingView's web protocol that
the open-source TradingView-API project (github.com/Mathieu2301/TradingView-API,
ISC licence) documents: the chart websocket's framing, its quote session and
its chart session, plus the public scanner and symbol-search endpoints. No code
is copied; the packet shapes are the reference.

What it is for, and what it is NOT for
--------------------------------------
TradingView is a SECOND OPINION on price, never the price the engine trades on.
Every order is sized, priced and protected on the broker's own bid/ask. The
uses, in order of importance:

1. **An independent price check** (``sentinel.data.reference``). A broker feed
   that freezes, a bad tick, a symbol mapped to the wrong contract, a quote
   from a server that silently lost its upstream: each makes the broker's
   price disagree with the rest of the market. The reference guard compares
   the two and can only SHRINK or BLOCK a new entry. It cannot open, enlarge
   or keep open anything.
2. **Market context for the owner**: TradingView's technical ratings (the
   "Recommend.All/MA/Other" summaries) per timeframe, shown on the dashboard.
   They are displayed and never traded: a summary of 26 textbook indicators
   has no demonstrated edge, and the acceptance protocol is the only door
   through which a signal reaches money.
3. **History for research** (``fetch_bars`` and ``scripts/tv_history.py``).
   Bars are single-price (no bid/ask) from TradingView's provider, not the
   broker, so a backtest on them needs an explicit cost model and is labelled
   exploratory until it is re-run on broker data.

Caveats the owner must know (also in docs/TRADINGVIEW.md)
---------------------------------------------------------
* This is an UNOFFICIAL, reverse-engineered interface. TradingView's terms of
  use restrict automated access to its data; using it is the owner's decision
  and the feature is OFF by default. It can break without notice when
  TradingView changes its protocol -- which is why every failure here
  degrades to "no reference", never to "no trading" or "more risk".
* It runs WITHOUT an account (the protocol's ``unauthorized_user_token``).
  Signing in would put a personal TradingView password or session cookie on a
  trading server and put that account at risk of suspension, for data that is
  already free for FX. Quotes TradingView marks as delayed are ignored.

Safety
------
* Symbols are validated against a strict pattern before they are sent.
* Inbound frames are size-capped (by the websocket library and again here),
  parsed as JSON only, and every price is checked finite, positive and
  uncrossed before it is stored. A malformed packet is counted and dropped.
* HTTPS requests follow no redirects and are size- and time-capped.
* The stream runs on its own daemon thread with bounded, jittered reconnects;
  nothing here is ever called from the decision thread except dictionary
  reads of the latest quote.
"""

from __future__ import annotations

import json
import math
import random
import re
import secrets
import string
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.clock import wall_ns

WS_URL = "wss://data.tradingview.com/socket.io/websocket?from=chart&type=chart"
ORIGIN = "https://www.tradingview.com"
SCAN_URL = "https://scanner.tradingview.com/global/scan"
SEARCH_URL = "https://symbol-search.tradingview.com/symbol_search/v3/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

MAX_FRAME_CHARS = 2_000_000
MAX_HTTP_BYTES = 1_000_000
MAX_SYMBOLS = 40
MAX_BARS = 20_000
BATCH_BARS = 5_000          # what one create_series / request_more_data asks for

# EXCHANGE:SYMBOL, e.g. OANDA:EURUSD, FX_IDC:USDJPY, TVC:DXY, CME_MINI:ES1!
SYMBOL_RE = re.compile(r"^[A-Z0-9_]{1,24}:[A-Z0-9._!&\-]{1,40}$")
EXCHANGE_RE = re.compile(r"^[A-Z0-9_]{1,24}$")
_SPLIT_RE = re.compile(r"~m~\d+~m~")
_TAG_RE = re.compile(r"<[^>]{0,20}>")

# Sentinel timeframe -> TradingView resolution.
TIMEFRAMES = {"M1": "1", "M5": "5", "M15": "15", "M30": "30", "H1": "60",
              "H4": "240", "D1": "1D", "W1": "1W"}
TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600,
                     "H4": 14400, "D1": 86400, "W1": 604800}

# The quote fields asked for. Deliberately few: every field is a value that
# has to be validated, and these are the ones the guard and the dashboard use.
QUOTE_FIELDS = ("lp", "lp_time", "bid", "ask", "ch", "chp", "description",
                "short_name", "pro_name", "exchange", "type", "update_mode",
                "current_session", "pricescale", "minmov", "status",
                "open_price", "high_price", "low_price", "prev_close_price")
_PRICE_FIELDS = ("lp", "bid", "ask", "open_price", "high_price", "low_price",
                 "prev_close_price")
_TEXT_FIELDS = ("description", "short_name", "pro_name", "exchange", "type",
                "update_mode", "current_session", "status")

# Technical-rating timeframes shown on the dashboard.
TA_TIMEFRAMES = ("15", "60", "240", "1D", "1W")
_TA_KINDS = (("all", "Recommend.All"), ("ma", "Recommend.MA"),
             ("other", "Recommend.Other"))


class TradingViewError(RuntimeError):
    """Anything that went wrong talking to TradingView. Never fatal to trading."""


# ---------------------------------------------------------------------------- #
# framing
# ---------------------------------------------------------------------------- #


def format_packet(payload: Any) -> str:
    """``~m~<length>~m~<body>``. The body is ASCII-only JSON (or a heartbeat
    string), so the character count equals the byte count TradingView expects
    -- the JavaScript client measures UTF-16 units, which agree for ASCII."""
    body = payload if isinstance(payload, str) else json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True)
    return f"~m~{len(body)}~m~{body}"


def message(method: str, params: Sequence[Any]) -> str:
    return format_packet({"m": method, "p": list(params)})


@dataclass
class Heartbeat:
    n: int


def parse_frame(raw: Any) -> Tuple[List[Any], int]:
    """Split one websocket frame into packets.

    Returns ``(packets, dropped)``: a heartbeat becomes ``Heartbeat(n)``, a JSON
    body its decoded value, and anything that is neither is counted in
    ``dropped`` instead of raising -- one malformed packet must not tear down a
    connection that is otherwise delivering good data.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return [], 1
    if len(raw) > MAX_FRAME_CHARS:
        raise TradingViewError(f"frame of {len(raw)} characters exceeds the cap")
    out: List[Any] = []
    dropped = 0
    for part in _SPLIT_RE.split(raw):
        if not part:
            continue
        if part.startswith("~h~"):
            try:
                out.append(Heartbeat(int(part[3:])))
            except ValueError:
                dropped += 1
            continue
        try:
            out.append(json.loads(part))
        except (ValueError, RecursionError):
            dropped += 1
    return out, dropped


def session_id(prefix: str) -> str:
    alphabet = string.ascii_letters + string.digits
    return prefix + "_" + "".join(secrets.choice(alphabet) for _ in range(12))


def validate_symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if not SYMBOL_RE.match(s):
        raise ValueError(f"not a TradingView symbol (EXCHANGE:SYMBOL): {symbol!r}")
    return s


def default_symbol(instrument: str, exchange: str = "OANDA") -> Optional[str]:
    """EUR_USD -> OANDA:EURUSD. None when the result would not be a valid id."""
    if not EXCHANGE_RE.match(exchange or ""):
        return None
    candidate = f"{exchange}:{str(instrument).upper().replace('_', '').replace('/', '')}"
    return candidate if SYMBOL_RE.match(candidate) else None


def _quote_key(symbol: str) -> str:
    # The form the reference client uses; TradingView echoes it back as `n`.
    return "=" + json.dumps({"session": "regular", "symbol": symbol},
                            separators=(",", ":"))


def _symbol_from_key(key: Any) -> Optional[str]:
    if not isinstance(key, str):
        return None
    if key.startswith("="):
        try:
            data = json.loads(key[1:])
        except ValueError:
            return None
        sym = data.get("symbol") if isinstance(data, dict) else None
        return sym if isinstance(sym, str) else None
    return key


def _price(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _text(value: Any, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    return _TAG_RE.sub("", value).strip()[:limit]


# ---------------------------------------------------------------------------- #
# quotes
# ---------------------------------------------------------------------------- #


@dataclass
class TVQuote:
    symbol: str
    last: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    last_time: Optional[int] = None        # exchange time of the last trade, seconds
    change: Optional[float] = None
    change_pct: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    prev_close: Optional[float] = None
    description: str = ""
    exchange: str = ""
    kind: str = ""
    update_mode: str = ""
    session: str = ""
    status: str = ""
    error: str = ""
    received_ns: int = 0                   # when ANY field last arrived
    price_ns: int = 0                      # when a PRICE last changed

    @property
    def delayed(self) -> bool:
        return "delayed" in self.update_mode.lower()

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.ask >= self.bid:
            return (self.bid + self.ask) / 2.0
        return self.last

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["mid"] = self.mid
        d["delayed"] = self.delayed
        return d


def apply_quote_update(quote: TVQuote, values: Dict[str, Any], now_ns: int) -> bool:
    """Merge one ``qsd`` value block into ``quote``. Returns True if a price moved.

    Every price is validated independently; a crossed bid/ask drops BOTH sides
    (keeping one side of a crossed book would make ``mid`` a guess).
    """
    moved = False
    mapping = {"lp": "last", "bid": "bid", "ask": "ask", "open_price": "open",
               "high_price": "high", "low_price": "low", "prev_close_price": "prev_close"}
    for src, dst in mapping.items():
        if src in values:
            p = _price(values[src])
            if p is not None and getattr(quote, dst) != p:
                setattr(quote, dst, p)
                if src in ("lp", "bid", "ask"):
                    moved = True
    if quote.bid is not None and quote.ask is not None and quote.ask < quote.bid:
        quote.bid = quote.ask = None
    if "lp_time" in values:
        t = _number(values["lp_time"])
        if t is not None and 0 < t < 32_503_680_000:     # before year 3000
            quote.last_time = int(t)
    if "ch" in values:
        quote.change = _number(values["ch"])
    if "chp" in values:
        quote.change_pct = _number(values["chp"])
    for src, dst in (("description", "description"), ("exchange", "exchange"),
                     ("type", "kind"), ("update_mode", "update_mode"),
                     ("current_session", "session"), ("status", "status")):
        if src in values:
            setattr(quote, dst, _text(values[src]))
    quote.received_ns = now_ns
    if moved:
        quote.price_ns = now_ns
    return moved


# ---------------------------------------------------------------------------- #
# connections
# ---------------------------------------------------------------------------- #


class Connection:
    """The three calls this module needs from a websocket. Tests pass a fake."""

    def send(self, data: str) -> None: ...                   # pragma: no cover

    def recv(self, timeout: float) -> str: ...               # pragma: no cover

    def close(self) -> None: ...                             # pragma: no cover


class _WebsocketConnection(Connection):
    def __init__(self, url: str, open_timeout: float) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover - a packaging fault
            raise TradingViewError("the 'websockets' package is not installed") from exc
        try:
            self._ws = connect(url, origin=ORIGIN, open_timeout=open_timeout,
                               close_timeout=3, max_size=MAX_FRAME_CHARS,
                               user_agent_header=USER_AGENT,
                               additional_headers={"Accept-Language": "en-US,en;q=0.9",
                                                   "Cache-Control": "no-cache",
                                                   "Pragma": "no-cache"})
        except Exception as exc:  # noqa: BLE001 - one error type for every caller
            raise TradingViewError(f"connect failed: {type(exc).__name__}: {exc}"[:300]) \
                from exc

    def send(self, data: str) -> None:
        try:
            self._ws.send(data)
        except Exception as exc:  # noqa: BLE001
            raise TradingViewError(f"send failed: {type(exc).__name__}: {exc}"[:300]) from exc

    def recv(self, timeout: float) -> str:
        try:
            return self._ws.recv(timeout=timeout)
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. ConnectionClosed
            raise TradingViewError(f"receive failed: {type(exc).__name__}: {exc}"[:300]) \
                from exc

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - closing a dead socket is not an error
            pass


def default_connect(url: str = WS_URL, open_timeout: float = 10.0) -> Connection:
    return _WebsocketConnection(url, open_timeout)


def _check_error_packet(pkt: Dict[str, Any]) -> None:
    m = pkt.get("m")
    if m in ("protocol_error", "critical_error"):
        raise TradingViewError(f"{m}: {str(pkt.get('p'))[:200]}")


# ---------------------------------------------------------------------------- #
# the quote stream
# ---------------------------------------------------------------------------- #


@dataclass
class StreamStatus:
    enabled: bool = False
    running: bool = False
    connected: bool = False
    connected_since_ns: int = 0
    last_message_ns: int = 0
    connects: int = 0
    reconnects: int = 0
    dropped_packets: int = 0
    last_error: str = ""
    last_error_ns: int = 0
    next_attempt_ns: int = 0
    server_release: str = ""
    symbols: List[str] = field(default_factory=list)
    symbol_errors: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TradingViewStream:
    """One websocket, one quote session, the symbols the owner mapped.

    ``set_symbols`` is safe to call from any thread at any time; the worker
    adds and removes subscriptions on the live session. Quotes are read with
    ``quote``/``quotes``, which return copies.
    """

    def __init__(self, *, connect: Optional[Callable[[], Connection]] = None,
                 clock: Callable[[], int] = wall_ns, recv_timeout: float = 1.0,
                 silence_timeout: float = 90.0, backoff_min: float = 5.0,
                 backoff_max: float = 300.0, max_symbols: int = MAX_SYMBOLS) -> None:
        self._connect = connect or default_connect
        self._clock = clock
        self.recv_timeout = recv_timeout
        self.silence_timeout = silence_timeout
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self.max_symbols = max_symbols
        self._lock = threading.RLock()
        self._wanted: List[str] = []
        self._quotes: Dict[str, TVQuote] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conn: Optional[Connection] = None
        self.status = StreamStatus()

    # -- control ---------------------------------------------------------------- #

    def set_symbols(self, symbols: Iterable[str]) -> List[str]:
        """Validate and store the wanted set. Invalid ids are refused, not sent."""
        clean: List[str] = []
        for s in symbols:
            try:
                v = validate_symbol(s)
            except ValueError:
                continue
            if v not in clean:
                clean.append(v)
        clean = clean[: self.max_symbols]
        with self._lock:
            self._wanted = clean
            self.status.symbols = list(clean)
            for gone in set(self._quotes) - set(clean):
                self._quotes.pop(gone, None)
        return clean

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            # A FRESH event per run. A worker that outlived stop()'s join (it
            # was blocked in a connect) keeps the old, set event and exits;
            # clearing a shared event here would have revived it beside the
            # new one -- two sockets, two writers.
            self._stop = threading.Event()
            self.status.enabled = True
            self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                            name="tradingview", daemon=True)
            self._thread.start()

    def stop(self, join: float = 5.0) -> None:
        self._stop.set()
        self.status.enabled = False
        conn = self._conn
        if conn is not None:
            conn.close()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=join)
        with self._lock:
            self._thread = None
            self.status.running = False
            self.status.connected = False

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    # -- reads -------------------------------------------------------------------- #

    def quote(self, symbol: str) -> Optional[TVQuote]:
        with self._lock:
            q = self._quotes.get(symbol)
            return TVQuote(**asdict(q)) if q is not None else None

    def quotes(self) -> Dict[str, TVQuote]:
        with self._lock:
            return {k: TVQuote(**asdict(v)) for k, v in self._quotes.items()}

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"status": self.status.to_dict(),
                    "quotes": {k: v.to_dict() for k, v in self._quotes.items()}}

    # -- the worker --------------------------------------------------------------- #

    def _run(self, stop: threading.Event) -> None:
        self.status.running = True
        delay = self.backoff_min
        try:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    self.status.connects += 1
                    conn = self._connect()
                    if stop.is_set():          # stopped while connecting
                        conn.close()
                        break
                    self._conn = conn
                    try:
                        self.run_session(conn, stop)
                    finally:
                        self._conn = None
                        conn.close()
                except Exception as exc:  # noqa: BLE001 - the stream must survive anything
                    self._record_error(exc)
                finally:
                    self.status.connected = False
                if stop.is_set():
                    break
                # A session that delivered data for a minute was healthy; start
                # the back-off again from the bottom.
                if time.monotonic() - started > 60 and self.status.last_message_ns:
                    delay = self.backoff_min
                wait = delay * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not crypto
                self.status.next_attempt_ns = self._clock() + int(wait * 1e9)
                self.status.reconnects += 1
                if stop.wait(wait):
                    break
                delay = min(self.backoff_max, delay * 2)
        finally:
            if stop is self._stop:
                self.status.running = False

    def _record_error(self, exc: BaseException) -> None:
        self.status.last_error = f"{type(exc).__name__}: {exc}"[:300]
        self.status.last_error_ns = self._clock()

    def run_session(self, conn: Connection, stop: Optional[threading.Event] = None) -> None:
        """Drive one connection until it fails, goes silent, or ``stop``."""
        stop = stop or self._stop
        qs = session_id("qs")
        conn.send(message("set_auth_token", ["unauthorized_user_token"]))
        conn.send(message("quote_create_session", [qs]))
        conn.send(message("quote_set_fields", [qs, *QUOTE_FIELDS]))
        self.status.connected = True
        self.status.connected_since_ns = self._clock()
        subscribed: List[str] = []
        last_seen = time.monotonic()
        while not stop.is_set():
            with self._lock:
                wanted = list(self._wanted)
            add = [s for s in wanted if s not in subscribed]
            remove = [s for s in subscribed if s not in wanted]
            if add:
                conn.send(message("quote_add_symbols", [qs, *[_quote_key(s) for s in add]]))
                subscribed.extend(add)
            for s in remove:
                conn.send(message("quote_remove_symbols", [qs, _quote_key(s)]))
                subscribed.remove(s)
            try:
                raw = conn.recv(self.recv_timeout)
            except TimeoutError:
                if time.monotonic() - last_seen > self.silence_timeout:
                    raise TradingViewError(
                        f"no message for {self.silence_timeout:.0f}s; reconnecting") from None
                continue
            last_seen = time.monotonic()
            self.status.last_message_ns = self._clock()
            packets, dropped = parse_frame(raw)
            self.status.dropped_packets += dropped
            for pkt in packets:
                self._handle(conn, qs, pkt)

    def _handle(self, conn: Connection, qs: str, pkt: Any) -> None:
        if isinstance(pkt, Heartbeat):
            conn.send(format_packet(f"~h~{pkt.n}"))
            return
        if not isinstance(pkt, dict):
            self.status.dropped_packets += 1
            return
        if "m" not in pkt:
            # The server's hello: session id, release, protocol.
            self.status.server_release = _text(pkt.get("release"), 60)
            return
        _check_error_packet(pkt)
        params = pkt.get("p")
        if not isinstance(params, list) or not params or params[0] != qs:
            return
        if pkt["m"] == "qsd" and len(params) > 1 and isinstance(params[1], dict):
            block = params[1]
            symbol = _symbol_from_key(block.get("n"))
            now = self._clock()
            with self._lock:
                if symbol not in self._wanted:
                    return
                if block.get("s") == "error":
                    reason = _text(str(block.get("errmsg") or block.get("v") or "error"), 160)
                    self.status.symbol_errors[symbol] = reason
                    q = self._quotes.setdefault(symbol, TVQuote(symbol=symbol))
                    q.error = reason
                    return
                values = block.get("v")
                if not isinstance(values, dict):
                    self.status.dropped_packets += 1
                    return
                q = self._quotes.setdefault(symbol, TVQuote(symbol=symbol))
                q.error = ""
                self.status.symbol_errors.pop(symbol, None)
                apply_quote_update(q, values, now)


# ---------------------------------------------------------------------------- #
# history (chart session), one short-lived connection per request
# ---------------------------------------------------------------------------- #


@dataclass
class BarSeries:
    symbol: str
    timeframe: str
    bars: List[Tuple[int, float, float, float, float, float]]   # (t_sec, o, h, l, c, v)
    info: Dict[str, Any]
    dropped_incomplete: int = 0

    def to_frame(self):
        import pandas as pd

        from .validation import validate_frame

        if not self.bars:
            raise TradingViewError(f"{self.symbol} {self.timeframe}: no bars returned")
        frame = pd.DataFrame(self.bars, columns=["t", "open", "high", "low", "close", "volume"])
        frame.index = pd.to_datetime(frame.pop("t"), unit="s", utc=True)
        frame.index.name = "time"
        return validate_frame(frame, name=f"tradingview:{self.symbol}:{self.timeframe}")


def _collect_bars(target: Dict[int, tuple], series: Any) -> int:
    n = 0
    if not isinstance(series, dict):
        return 0
    rows = series.get("s")
    if not isinstance(rows, list):
        return 0
    for row in rows:
        v = row.get("v") if isinstance(row, dict) else None
        if not isinstance(v, list) or len(v) < 5:
            continue
        t = _number(v[0])
        o, hi, lo, c = (_price(x) for x in v[1:5])
        vol = _number(v[5]) if len(v) > 5 else 0.0
        if t is None or None in (o, hi, lo, c):
            continue
        if hi < max(o, c) or lo > min(o, c) or hi < lo:
            continue      # impossible geometry: refused, never repaired
        target[int(t)] = (int(t), o, hi, lo, c, max(0.0, vol or 0.0))
        n += 1
    return n


def fetch_bars(symbol: str, timeframe: str, count: int = 1000, *,
               connect: Optional[Callable[[], Connection]] = None,
               timeout: float = 30.0, now_s: Optional[float] = None,
               include_incomplete: bool = False) -> BarSeries:
    """Closed bars for ``symbol`` at a Sentinel timeframe (``H4``, ``D1``...).

    The bar still forming is dropped unless ``include_incomplete``: acting on
    an unfinished bar is the look-ahead bug the feed module exists to prevent.
    """
    symbol = validate_symbol(symbol)
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unsupported timeframe {timeframe!r}; use one of {sorted(TIMEFRAMES)}")
    count = max(1, min(int(count), MAX_BARS))
    first = min(count, BATCH_BARS)
    conn = (connect or default_connect)()
    cs = session_id("cs")
    bars: Dict[int, tuple] = {}
    info: Dict[str, Any] = {}
    deadline = time.monotonic() + timeout
    try:
        conn.send(message("set_auth_token", ["unauthorized_user_token"]))
        conn.send(message("chart_create_session", [cs]))
        conn.send(message("resolve_symbol", [
            cs, "ser_1", "=" + json.dumps({"symbol": symbol, "adjustment": "splits"},
                                          separators=(",", ":"))]))
        conn.send(message("create_series", [cs, "$prices", "s1", "ser_1",
                                             TIMEFRAMES[timeframe], first]))
        # Each round ends with `series_completed`. Another round is asked for
        # only while the previous one delivered everything it asked for: a
        # short round means the history is exhausted, and an instrument with
        # less history than `count` ends the loop instead of spinning on it.
        round_start, asked = 0, first
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TradingViewError(f"{symbol}: no complete answer in {timeout:.0f}s")
            try:
                raw = conn.recv(min(left, 5.0))
            except TimeoutError:
                continue
            packets, _ = parse_frame(raw)
            completed = False
            for pkt in packets:
                if isinstance(pkt, Heartbeat):
                    conn.send(format_packet(f"~h~{pkt.n}"))
                    continue
                if not isinstance(pkt, dict) or "m" not in pkt:
                    continue
                _check_error_packet(pkt)
                p = pkt.get("p")
                if not isinstance(p, list) or not p or p[0] != cs:
                    continue
                m = pkt["m"]
                if m == "symbol_resolved" and len(p) > 2 and isinstance(p[2], dict):
                    src = p[2]
                    info = {k: src.get(k) for k in (
                        "name", "full_name", "pro_name", "description", "exchange",
                        "type", "currency_code", "timezone", "session", "pricescale",
                        "minmov") if isinstance(src.get(k), (str, int, float))}
                elif m in ("symbol_error", "series_error"):
                    raise TradingViewError(f"{symbol}: {m} {str(p[1:])[:200]}")
                elif m in ("timescale_update", "du") and len(p) > 1 and isinstance(p[1], dict):
                    _collect_bars(bars, p[1].get("$prices"))
                elif m == "series_completed":
                    completed = True
            if completed:
                added = len(bars) - round_start
                if len(bars) >= count or added < asked:
                    break
                round_start, asked = len(bars), min(BATCH_BARS, count - len(bars))
                conn.send(message("request_more_data", [cs, "$prices", asked]))
        try:
            conn.send(message("chart_delete_session", [cs]))
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()
    ordered = [bars[t] for t in sorted(bars)]
    dropped = 0
    if not include_incomplete and ordered:
        now = now_s if now_s is not None else time.time()
        span = TIMEFRAME_SECONDS[timeframe]
        while ordered and ordered[-1][0] + span > now:
            ordered.pop()
            dropped += 1
    return BarSeries(symbol=symbol, timeframe=timeframe, bars=ordered[-count:],
                     info=info, dropped_incomplete=dropped)


# ---------------------------------------------------------------------------- #
# HTTP: technical ratings and symbol search
# ---------------------------------------------------------------------------- #


def _https(method: str, url: str, *, json_body: Any = None,
           params: Optional[Dict[str, Any]] = None, timeout: float = 15.0) -> Any:
    import httpx

    headers = {"Origin": ORIGIN, "Referer": ORIGIN + "/", "User-Agent": USER_AGENT,
               "Accept": "application/json"}
    try:
        with httpx.stream(method, url, json=json_body, params=params, headers=headers,
                          timeout=timeout, follow_redirects=False) as resp:
            if resp.status_code != 200:
                raise TradingViewError(f"{url.split('/')[2]} answered {resp.status_code}")
            chunks, total = [], 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_HTTP_BYTES:
                    raise TradingViewError(f"{url.split('/')[2]} sent more than "
                                           f"{MAX_HTTP_BYTES} bytes")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise TradingViewError(f"{type(exc).__name__}: {exc}"[:200]) from exc
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except ValueError as exc:
        raise TradingViewError("the answer was not JSON") from exc


def rating_label(value: Optional[float]) -> str:
    """TradingView's own buckets for a -1..+1 summary."""
    if value is None:
        return "unknown"
    if value < -0.5:
        return "strong_sell"
    if value < -0.1:
        return "sell"
    if value <= 0.1:
        return "neutral"
    if value <= 0.5:
        return "buy"
    return "strong_buy"


def ta_columns(timeframes: Sequence[str] = TA_TIMEFRAMES) -> List[str]:
    cols = []
    for tf in timeframes:
        for _, name in _TA_KINDS:
            cols.append(name if tf == "1D" else f"{name}|{tf}")
    return cols


def parse_ta(data: Any, timeframes: Sequence[str] = TA_TIMEFRAMES
             ) -> Dict[str, Dict[str, Dict[str, Any]]]:
    cols = ta_columns(timeframes)
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise TradingViewError("the scanner answer has no data rows")
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = row.get("s")
        values = row.get("d")
        if not isinstance(sym, str) or not SYMBOL_RE.match(sym) or not isinstance(values, list):
            continue
        per: Dict[str, Dict[str, Any]] = {}
        for i, col in enumerate(cols):
            if i >= len(values):
                break
            name, _, tf = col.partition("|")
            tf = tf or "1D"
            kind = next(k for k, n in _TA_KINDS if n == name)
            v = _number(values[i])
            if v is not None and not -1.0001 <= v <= 1.0001:
                v = None
            per.setdefault(tf, {})[kind] = None if v is None else round(v, 4)
        for d in per.values():
            d["label"] = rating_label(d.get("all"))
        out[sym] = per
    return out


def fetch_ta(symbols: Sequence[str], timeframes: Sequence[str] = TA_TIMEFRAMES, *,
             request: Optional[Callable[..., Any]] = None, timeout: float = 15.0
             ) -> Dict[str, Dict[str, Dict[str, Any]]]:
    tickers = []
    for s in symbols:
        try:
            v = validate_symbol(s)
        except ValueError:
            continue
        if v not in tickers:
            tickers.append(v)
    if not tickers:
        return {}
    body = {"symbols": {"tickers": tickers[:MAX_SYMBOLS]}, "columns": ta_columns(timeframes)}
    data = (request or _https)("POST", SCAN_URL, json_body=body, timeout=timeout)
    return parse_ta(data, timeframes)


SEARCH_TYPES = ("", "forex", "cfd", "crypto", "index", "futures", "stock", "economic")


def search_symbols(text: str, kind: str = "forex", *,
                   request: Optional[Callable[..., Any]] = None,
                   timeout: float = 15.0, limit: int = 30) -> List[Dict[str, str]]:
    query = re.sub(r"[^A-Za-z0-9:._!& \-/]", "", str(text or ""))[:40].strip().upper()
    if not query:
        return []
    if kind not in SEARCH_TYPES:
        raise ValueError(f"unknown search type {kind!r}")
    exchange = None
    if ":" in query:
        exchange, _, query = query.partition(":")
    params = {"text": query.replace("/", ""), "search_type": kind, "start": 0,
              "hl": 0, "lang": "en", "domain": "production"}
    if exchange and EXCHANGE_RE.match(exchange):
        params["exchange"] = exchange
    data = (request or _https)("GET", SEARCH_URL, params=params, timeout=timeout)
    rows = data.get("symbols") if isinstance(data, dict) else data
    out: List[Dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        sym = _text(row.get("symbol"), 40).upper()
        exch = _text(row.get("prefix") or row.get("source_id") or row.get("exchange"), 24)
        exch = exch.split(" ")[0].upper()
        full = f"{exch}:{sym}"
        if not SYMBOL_RE.match(full):
            continue
        out.append({"id": full, "symbol": sym, "exchange": exch,
                    "description": _text(row.get("description"), 120),
                    "type": _text(row.get("type"), 24)})
        if len(out) >= limit:
            break
    return out
