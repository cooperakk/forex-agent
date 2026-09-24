"""MetaTrader 5 over a socket, for an engine that does not run on Windows.

The ``MetaTrader5`` Python package is a Windows-only binary that talks to a
running terminal through shared memory. On an Ubuntu server it does not
exist, and every MetaTrader broker -- AMarkets, Alpari, the whole family the
project is built for -- becomes unreachable. The alternatives are all bad in
the same way: Wine plus a Windows Python is fragile and undocumented; OANDA is
not available where this system is meant to run.

So the terminal stays on Windows -- a desktop, a small VPS, or a Wine prefix
on the same box -- and this module carries the package's *surface* across a
socket:

* ``BridgeServer`` runs next to the terminal. It wraps the real module, or
  the test double, and answers one JSON request at a time (the package is not
  thread-safe; neither is the server, on purpose).
* ``BridgeMT5`` runs inside the engine and looks exactly like the module the
  adapter already imports: the same method names, the same constants, results
  with the same attribute access. ``MT5Broker(mt5_module=BridgeMT5(...))``
  is the whole integration, and ``build_broker`` does it from the environment.

Security, stated plainly. The server binds loopback and refuses anything else
unless told ``--allow-remote``; the intended transport is an SSH tunnel, in
whichever direction the network allows. Every request carries a shared token
compared in constant time. The socket carries the account password once, at
``initialize`` -- which is why the transport must be the tunnel and not a
port on the internet. Nothing here encrypts; SSH does.

The wire format is JSON lines. It is deliberately boring: any future bridge
(a Wine helper, an MT4 EA, another language) can speak it in an afternoon.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_PORT = 5555
_MAX_LINE = 32 * 1024 * 1024   # a 5000-bar rates answer is ~1 MB; leave room
_CALL_TIMEOUT_S = 30.0

#: The subset of the package the adapter, discovery and the probe use. An
#: allow-list rather than "anything the module has": the server must not be a
#: generic RPC into a process that holds a trading session.
ALLOWED_METHODS = frozenset({
    "initialize", "shutdown", "last_error", "terminal_info", "account_info",
    "symbols_get", "symbol_info", "symbol_info_tick", "symbol_select",
    "copy_rates_from_pos", "copy_ticks_range", "positions_get", "orders_get",
    "history_deals_get", "order_send", "version",
})


# --------------------------------------------------------------------------- #
# encoding
# --------------------------------------------------------------------------- #


def _encode(value: Any) -> Any:
    """Anything the MetaTrader5 package returns -> plain JSON."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, datetime):
        return {"__dt__": value.astimezone(timezone.utc).isoformat()}
    if hasattr(value, "_asdict"):                      # namedtuple (AccountInfo, Tick, ...)
        return {k: _encode(v) for k, v in value._asdict().items()}
    import dataclasses
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if hasattr(value, "dtype") and hasattr(value, "tolist"):   # numpy array / scalar
        names = getattr(getattr(value, "dtype", None), "names", None)
        if names:                                       # structured array (rates)
            return [{n: _encode(row[n]) for n in names} for row in value]
        return _encode(value.tolist())
    if hasattr(value, "item") and not isinstance(value, (list, tuple, dict)):
        try:
            return _encode(value.item())
        except (AttributeError, TypeError, ValueError):
            pass
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    # A plain object with public attributes (the real package's result types
    # are namedtuples; anything else record-like still crosses as a record).
    public = {k: v for k, v in vars(value).items() if not k.startswith("_")} \
        if hasattr(value, "__dict__") else {}
    if public:
        return {k: _encode(v) for k, v in public.items()}
    return str(value)


def _decode_args(value: Any) -> Any:
    """Plain JSON -> what the package expects (datetimes in particular)."""
    if isinstance(value, dict):
        if set(value) == {"__dt__"}:
            return datetime.fromisoformat(value["__dt__"])
        return {k: _decode_args(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_args(v) for v in value]
    return value


def _encode_args(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__dt__": value.astimezone(timezone.utc).isoformat()
                if value.tzinfo else value.replace(tzinfo=timezone.utc).isoformat()}
    if isinstance(value, dict):
        return {k: _encode_args(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_args(v) for v in value]
    return value


class Obj(dict):
    """A dict that also answers attribute access, so ``tick.bid`` and
    ``row["time"]`` both work -- the two spellings the adapter uses."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return Obj({k: _wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #


def _is_loopback(host: str) -> bool:
    import ipaddress
    h = (host or "").strip().strip("[]")
    if h in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


class BridgeServer:
    """Serve one MetaTrader5-shaped module over JSON lines.

    ``module`` is the real package on Windows, or ``tests.fake_mt5.FakeMT5``
    in the test suite. Calls are serialised with a lock: the package is not
    thread-safe, and two engines must not share one terminal anyway.
    """

    def __init__(self, module: Any, *, token: str, host: str = "127.0.0.1",
                 port: int = DEFAULT_PORT, allow_remote: bool = False,
                 log: Optional[Callable[[str], None]] = None,
                 envelope: Optional["BridgeEnvelope"] = None) -> None:
        if not token or len(token) < 16:
            raise ValueError("the bridge token must be at least 16 characters")
        #: The second lock on the venue, enforced HERE, beside the terminal:
        #: account binding, a lot ceiling, a live gate and a write journal.
        #: The engine has its own risk engine and OMS; this one exists for the
        #: day the engine is wrong, compromised, or simply another process.
        self.envelope = envelope or BridgeEnvelope()
        if not _is_loopback(host) and not allow_remote:
            raise ValueError(
                f"refusing to bind {host}: the bridge carries the account password. "
                "Bind loopback and reach it through an SSH tunnel, or pass "
                "allow_remote=True if something else provides the isolation.")
        self.module = module
        self._token = token
        self.host, self.port = host, port
        self._lock = threading.Lock()
        self._log = log or (lambda s: None)
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.calls = 0
        self.rejected = 0

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> Tuple[str, int]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(4)
        s.settimeout(0.5)
        self._sock = s
        self.port = s.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name="mt5-bridge",
                                        daemon=True)
        self._thread.start()
        self._log(f"mt5 bridge listening on {self.host}:{self.port}")
        return self.host, self.port

    def serve_forever(self) -> None:
        if self._thread is None:
            self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _accept_loop(self) -> None:
        while not self._stop.is_set() and self._sock is not None:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_conn, args=(conn, addr), daemon=True).start()

    # -- one connection ---------------------------------------------------- #

    def _serve_conn(self, conn: socket.socket, addr) -> None:
        conn.settimeout(120.0)
        buf = b""
        try:
            while not self._stop.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > _MAX_LINE:
                    self._log(f"{addr}: request too large; closing")
                    return
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    reply = self._handle(line)
                    conn.sendall(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")
        except (OSError, ValueError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle(self, line: bytes) -> Dict[str, Any]:
        try:
            req = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"id": None, "ok": False, "error": "malformed request"}
        rid = req.get("id")
        token = str(req.get("token", ""))
        if not hmac.compare_digest(token, self._token):
            self.rejected += 1
            return {"id": rid, "ok": False, "error": "unauthorised"}
        method = str(req.get("method", ""))
        if method == "constants":
            consts = {k: v for k, v in vars(self.module).items()
                      if k.isupper() and isinstance(v, (int, float, str))}
            # The test double keeps its constants on the class.
            for k in dir(type(self.module)):
                if k.isupper():
                    v = getattr(self.module, k, None)
                    if isinstance(v, (int, float, str)):
                        consts.setdefault(k, v)
            return {"id": rid, "ok": True, "result": consts}
        if method not in ALLOWED_METHODS:
            self.rejected += 1
            return {"id": rid, "ok": False, "error": f"method {method!r} is not bridged"}
        fn = getattr(self.module, method, None)
        if fn is None:
            return {"id": rid, "ok": False, "error": f"terminal module has no {method!r}"}
        args = _decode_args(req.get("args") or [])
        kwargs = _decode_args(req.get("kwargs") or {})
        with self._lock:
            self.calls += 1
            # Every call after the first re-reads the terminal's identity. A
            # terminal signed into another account answers every method for
            # that account, and nothing in the protocol says so.
            refusal = self.envelope.check_account(self.module, method)
            if refusal:
                self.rejected += 1
                return {"id": rid, "ok": False, "error": refusal}
            journal_key = None
            if method == "order_send":
                refusal, journal_key, replay = self.envelope.check_order(self.module, args, kwargs)
                if refusal:
                    self.rejected += 1
                    return {"id": rid, "ok": False, "error": refusal}
                if replay is not None:
                    return {"id": rid, "ok": True, "result": replay, "replayed": True}
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - the caller gets the text
                if journal_key:
                    self.envelope.record_outcome(journal_key, None)
                return {"id": rid, "ok": False,
                        "error": f"{exc.__class__.__name__}: {exc}"}
            try:
                encoded = _encode(result)
            except Exception as exc:  # noqa: BLE001
                if journal_key:
                    self.envelope.record_outcome(journal_key, None)
                return {"id": rid, "ok": False, "error": f"unencodable result: {exc}"}
            if journal_key:
                self.envelope.record_outcome(journal_key, encoded)
            return {"id": rid, "ok": True, "result": encoded}


class BridgeEnvelope:
    """What the bridge will do for the engine, and what it refuses.

    * **Account binding.** On the first call the terminal's account id and
      server are recorded; every later call that finds them changed is refused
      ("all routing refused"). Set ``account_id``/``server`` explicitly to
      bind before any call.
    * **Order envelope.** ``order_send`` is refused above ``max_lots``, without
      a stop, for a symbol outside ``symbols`` when that set is given, and on
      a LIVE account unless ``allow_live`` is set.
    * **Write journal.** Every ``order_send`` is journalled by the request's
      ``comment`` (the engine's client order id) BEFORE the terminal is
      called, with a hash of the request. A resend with the same id and the
      same body replays the recorded outcome; the same id with a different
      body is refused; an id whose first attempt has no recorded outcome is
      refused until an operator clears it -- because "the socket dropped
      between order_send and the reply" is exactly the state where a resend
      opens a second position, and MetaTrader cannot deduplicate it.
    """

    def __init__(self, *, account_id: Optional[str] = None, server: Optional[str] = None,
                 max_lots: float = 0.5, allow_live: bool = False,
                 symbols: Optional[set] = None, journal_path: Optional[str] = None) -> None:
        self.account_id = str(account_id) if account_id else None
        self.server = str(server) if server else None
        self.max_lots = float(max_lots)
        self.allow_live = bool(allow_live)
        self.symbols = set(symbols) if symbols else None
        self._journal_path = journal_path
        self._journal: Dict[str, Dict[str, Any]] = {}
        self._account_type: Optional[str] = None
        if journal_path:
            try:
                with open(journal_path, encoding="utf-8") as fh:
                    self._journal = json.load(fh)
            except FileNotFoundError:
                self._journal = {}
            except (OSError, ValueError) as exc:
                raise ValueError(f"the bridge write journal at {journal_path} is unreadable "
                                 f"({exc}); an empty journal would forget every in-flight "
                                 "order") from exc

    # -- binding ---------------------------------------------------------- #

    def check_account(self, module: Any, method: str) -> Optional[str]:
        if method in ("initialize", "shutdown", "last_error", "version", "terminal_info"):
            return None
        try:
            info = module.account_info()
        except Exception as exc:  # noqa: BLE001
            return f"account unreadable; routing refused: {exc}"
        if info is None:
            return "terminal is not signed in; routing refused"
        login = str(getattr(info, "login", "") or "")
        server = str(getattr(info, "server", "") or "")
        mode = getattr(info, "trade_mode", None)
        try:
            self._account_type = {0: "demo", 1: "demo", 2: "live"}.get(int(mode), "") \
                if mode is not None else ""
        except (TypeError, ValueError):
            self._account_type = ""
        if self.account_id is None:
            self.account_id, self.server = login, server
            return None
        if login != self.account_id:
            return (f"terminal account {login!r} is not the bound account "
                    f"{self.account_id!r}; all routing refused")
        if self.server and server and server != self.server:
            return (f"terminal server {server!r} is not the bound server "
                    f"{self.server!r}; all routing refused")
        return None

    # -- orders ----------------------------------------------------------- #

    def check_order(self, module: Any, args: list, kwargs: dict):
        request = args[0] if args else kwargs.get("request")
        if not isinstance(request, dict):
            return "order_send needs a request dict", None, None
        deal = getattr(module, "TRADE_ACTION_DEAL", 1)
        action = request.get("action")
        if action == deal and "position" not in request:
            # A NEW position: the envelope applies. Closes and stop changes
            # (SLTP, or a deal carrying a position id) reduce risk and pass.
            if self._account_type == "live" and not self.allow_live:
                return "live account: the bridge was started without --allow-live", None, None
            if self._account_type not in ("demo", "live"):
                return "account type unknown; the bridge will not open a position", None, None
            try:
                lots = float(request.get("volume", 0))
            except (TypeError, ValueError):
                return "order volume is not a number", None, None
            if lots <= 0 or lots > self.max_lots:
                return f"volume {lots} outside the bridge ceiling of {self.max_lots} lots", None, None
            if not request.get("sl"):
                return "a new position without a stop-loss is refused by the bridge", None, None
            symbol = str(request.get("symbol", ""))
            if self.symbols is not None and symbol not in self.symbols:
                return f"{symbol!r} is outside the bridge's symbol envelope", None, None
        key = str(request.get("comment") or "")
        if not key:
            return None, None, None          # no id: nothing to journal against
        body = json.dumps(request, sort_keys=True, default=str)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        prior = self._journal.get(key)
        if prior is not None:
            if prior.get("hash") != digest:
                return f"order id {key!r} reused with a different request", None, None
            if "outcome" not in prior or prior["outcome"] is None:
                return (f"order id {key!r} was sent before and its outcome is unknown; "
                        "do not resend -- reconcile against the terminal's deals"), None, None
            return None, None, prior["outcome"]
        self._journal[key] = {"hash": digest, "sent_at": time.time()}
        self._persist()
        return None, key, None

    def record_outcome(self, key: str, outcome: Any) -> None:
        entry = self._journal.get(key)
        if entry is None:
            return
        entry["outcome"] = outcome
        entry["done_at"] = time.time()
        self._persist()

    def clear(self, key: str) -> bool:
        """An operator, having reconciled, releases a stuck id."""
        if key in self._journal:
            del self._journal[key]
            self._persist()
            return True
        return False

    def _persist(self) -> None:
        if not self._journal_path:
            return
        tmp = self._journal_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._journal, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._journal_path)


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #


class BridgeError(RuntimeError):
    """Transport or protocol failure talking to the bridge."""


class BridgeRefused(BridgeError):
    """The bridge's envelope refused the request BEFORE the terminal saw it.

    Distinct from a transport failure on purpose: a refusal is a certain
    'nothing happened' and the adapter reports it as a rejection, while a
    dropped socket during ``order_send`` is an unknown outcome.
    """


class BridgeMT5:
    """A stand-in for ``import MetaTrader5 as mt5`` that lives across a socket.

    Constants are fetched once and become attributes, so ``mt5.TIMEFRAME_H4``
    and ``hasattr(mt5, "ORDER_FILLING_FOK")`` behave as they do with the real
    module. Every method call is one round trip. The socket is reopened once
    on failure; a second failure is the caller's problem, reported as a
    ``BridgeError`` which the adapter surfaces as a broker error.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, *,
                 token: str, timeout_s: float = _CALL_TIMEOUT_S) -> None:
        if not token:
            raise ValueError("a bridge token is required")
        self._host, self._port = host, int(port)
        self._token = token
        self._timeout = timeout_s
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._seq = 0
        self._lock = threading.RLock()
        for k, v in self._call("constants").items():
            self.__dict__[k] = v

    def __repr__(self) -> str:
        return f"<BridgeMT5 {self._host}:{self._port}>"

    # -- transport --------------------------------------------------------- #

    def _connect(self) -> None:
        self.close()
        s = socket.create_connection((self._host, self._port), timeout=self._timeout)
        s.settimeout(self._timeout)
        self._sock = s
        self._buf = b""

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _roundtrip(self, payload: bytes) -> Dict[str, Any]:
        assert self._sock is not None
        self._sock.sendall(payload)
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("bridge closed the connection")
            self._buf += chunk
            if len(self._buf) > _MAX_LINE:
                raise ConnectionError("bridge reply too large")
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self._seq += 1
            req = {"id": self._seq, "token": self._token, "method": method,
                   "args": _encode_args(list(args)), "kwargs": _encode_args(dict(kwargs))}
            payload = json.dumps(req, separators=(",", ":")).encode("utf-8") + b"\n"
            last: Optional[Exception] = None
            for attempt in range(2):
                try:
                    if self._sock is None:
                        self._connect()
                    reply = self._roundtrip(payload)
                    break
                except (OSError, ConnectionError, ValueError) as exc:
                    last = exc
                    self.close()
                    if attempt == 1:
                        raise BridgeError(
                            f"mt5 bridge at {self._host}:{self._port} unreachable: {exc}"
                        ) from exc
            else:  # pragma: no cover
                raise BridgeError(str(last))
            if not reply.get("ok"):
                err = str(reply.get("error", "bridge error"))
                if err == "unauthorised":
                    raise BridgeError("mt5 bridge rejected the token")
                if _looks_like_refusal(err):
                    raise BridgeRefused(err)
                raise BridgeError(err)
            return reply.get("result")

    # -- the MetaTrader5 surface ------------------------------------------- #

    def initialize(self, *args: Any, **kwargs: Any) -> bool:
        return bool(self._call("initialize", *args, **kwargs))

    def shutdown(self) -> None:
        try:
            self._call("shutdown")
        except BridgeError:
            pass

    def version(self) -> Any:
        return _wrap(self._call("version"))

    def last_error(self) -> Tuple[Any, ...]:
        res = self._call("last_error")
        return tuple(res) if isinstance(res, list) else (res,)

    def terminal_info(self) -> Any:
        return _wrap(self._call("terminal_info"))

    def account_info(self) -> Any:
        return _wrap(self._call("account_info"))

    def symbols_get(self, *args: Any, **kwargs: Any) -> Any:
        res = self._call("symbols_get", *args, **kwargs)
        return _wrap(res) if res is not None else None

    def symbol_info(self, name: str) -> Any:
        return _wrap(self._call("symbol_info", name))

    def symbol_info_tick(self, name: str) -> Any:
        return _wrap(self._call("symbol_info_tick", name))

    def symbol_select(self, name: str, enable: bool = True) -> bool:
        return bool(self._call("symbol_select", name, enable))

    def copy_rates_from_pos(self, name: str, timeframe: int, start: int, count: int) -> Any:
        res = self._call("copy_rates_from_pos", name, int(timeframe), int(start), int(count))
        return _wrap(res) if res is not None else None

    def positions_get(self, *args: Any, **kwargs: Any) -> Any:
        res = self._call("positions_get", *args, **kwargs)
        return _wrap(res) if res is not None else None

    def orders_get(self, *args: Any, **kwargs: Any) -> Any:
        res = self._call("orders_get", *args, **kwargs)
        return _wrap(res) if res is not None else None

    def history_deals_get(self, *args: Any, **kwargs: Any) -> Any:
        res = self._call("history_deals_get", *args, **kwargs)
        return _wrap(res) if res is not None else None

    def order_send(self, request: Dict[str, Any]) -> Any:
        """Send an order. A refusal by the envelope raises ``BridgeRefused``
        (nothing reached the terminal); a transport failure raises
        ``BridgeError``, and the caller must treat that as UNKNOWN."""
        return _wrap(self._call("order_send", dict(request)))


_REFUSAL_MARKERS = ("routing refused", "ceiling", "refused by the bridge", "allow-live",
                    "envelope", "reused with a different request", "outcome is unknown",
                    "account type unknown", "not signed in", "not bridged", "needs a request")


def _looks_like_refusal(message: str) -> bool:
    return any(m in message for m in _REFUSAL_MARKERS)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

ENV_ADDRESS = "SENTINEL_MT5_BRIDGE"
ENV_TOKEN = "SENTINEL_MT5_BRIDGE_TOKEN"


def parse_address(value: str) -> Tuple[str, int]:
    text = (value or "").strip()
    for prefix in ("tcp://", "bridge://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    host, _, port = text.rpartition(":")
    if not host:
        host, port = text, str(DEFAULT_PORT)
    return host.strip("[]") or "127.0.0.1", int(port or DEFAULT_PORT)


def bridge_from_env(environ: Optional[Dict[str, str]] = None) -> Optional[BridgeMT5]:
    """A connected ``BridgeMT5`` when the environment names one, else None.

    ``SENTINEL_MT5_BRIDGE=127.0.0.1:5555`` and ``SENTINEL_MT5_BRIDGE_TOKEN``
    are the two variables. Unset means "use the local package", which is the
    Windows behaviour and stays the default.
    """
    env = os.environ if environ is None else environ
    address = env.get(ENV_ADDRESS, "").strip()
    if not address:
        return None
    token = env.get(ENV_TOKEN, "").strip()
    if not token:
        raise BridgeError(f"{ENV_ADDRESS} is set but {ENV_TOKEN} is empty")
    host, port = parse_address(address)
    return BridgeMT5(host, port, token=token)
