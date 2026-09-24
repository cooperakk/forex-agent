"""MetaTrader 5 adapter -- and an explicit record of what it cannot do.

MT5 has no caller-supplied order identifier that the server deduplicates on.
``magic`` is a strategy tag, not a unique key, and ``comment`` is advisory and
frequently truncated or rewritten by the broker. That means the idempotency
guarantee the OMS relies on is **unavailable** here.

Rather than pretend otherwise, this adapter implements the second-best
protocol and reports the downgrade through ``capabilities``:

1. A process-level lock serialises submissions per instrument.
2. Before any resend, ``query_order`` scans recent deals and orders for our
   comment token *and* for a matching (instrument, side, volume, time-window)
   signature.
3. A blackout window after a timeout: no new order on that instrument until
   the state is proven.

This narrows the race. It does not close it. ``docs/SECURITY.md`` and the
dashboard's environment panel both show this as a standing degradation, and
the acceptance protocol refuses to promote a strategy to live on a venue whose
capabilities do not meet the declared requirement.

The ``MetaTrader5`` package is Windows-only, so import is lazy and failure is
reported as a configuration problem rather than crashing the service.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from ..core.clock import wall_ns
from ..core.errors import BrokerError, ConfigError, ConversionMissingError, UnknownOutcomeError
from ..core.money import D, Instrument, dec
from ..core.types import (
    AccountState, Bar, ClosedTrade, Fill, Order, OrderIntent, OrderState, OrderType,
    Position, Quote, Side,
)
from .base import Broker, BrokerCapabilities, SubmitResult

if TYPE_CHECKING:  # the annotation on MT5Broker.__init__
    from .profiles.base import BrokerProfile

_BLACKOUT_NS = 30 * 1_000_000_000
#: How hard to try before refusing to build an adapter with no symbol table.
_SYMBOL_TABLE_RETRIES = 3
_SYMBOL_TABLE_RETRY_SEC = 1.0

#: Our timeframe names -> the constant the MetaTrader5 module exposes.
_MT5_TIMEFRAME_ATTR = {
    "M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1", "W1": "TIMEFRAME_W1",
}
_TIMEFRAME_SEC = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800,
}
#: A measured server offset is believed only when the tick it was measured
#: from is this fresh. A Friday tick read on Sunday is days old, and its
#: "offset" would be minus two days.
_OFFSET_MAX_STALE_SEC = 900
#: Server clocks sit on a half-hour boundary from UTC (UTC+2, +3, +5:30...).
_OFFSET_QUANTUM_SEC = 1800
_OFFSET_MAX_ABS_SEC = 14 * 3600


#: MT5 reports the account kind as an integer. Mapped once, here, so no caller
#: has to remember that 1 ("contest") is play money too.
_MT5_TRADE_MODE = {0: "demo", 1: "demo", 2: "live"}


def _field(row: Any, name: str, default: Any = None) -> Any:
    """One column of a rates row.

    The real terminal returns a numpy structured array (``row["time"]``); the
    test double returns plain objects or dicts. Reading both the same way keeps
    the adapter testable without a Windows terminal.
    """
    try:
        return row[name]
    except (TypeError, KeyError, IndexError, ValueError):
        pass
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _exit_reason_from_comment(comment: str) -> str:
    """The short exit code the agent wrote into the closing deal's comment.

    The agent closes with ``reason=code[:32]`` (weekend_flat, time_stop,
    giveback, partial_take ...); the terminal also writes its own markers
    ("[sl]", "[tp]", "so:" for stop-out). Map what can be mapped and keep
    the rest verbatim so the post-mortem never confuses a venue stop-out
    with a strategy exit.
    """
    text = (comment or "").strip()
    if text.lower().startswith("close:"):
        text = text[6:].strip()                 # the adapter's own close marker
    low = text.lower()
    if "[sl]" in low or low.startswith("sl "):
        return "stop_loss"
    if "[tp]" in low or low.startswith("tp "):
        return "take_profit"
    if low.startswith("so:") or "stop out" in low:
        return "margin_stop_out"
    return text[:32] or "venue_history"


def _trade_mode(account: Any) -> int:
    """The terminal's ACCOUNT_TRADE_MODE, or -1 when it did not say.

    Written out rather than inlined because `getattr(a, "trade_mode", -1) or -1`
    reads as a default and is not one: 0 is a real, meaningful value here.
    """
    value = getattr(account, "trade_mode", None)
    if value is None:
        return -1
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


class MT5Broker(Broker):
    def __init__(self, *, account_currency: str = "USD", magic: int = 770914,
                 instruments: Optional[Dict[str, Instrument]] = None,
                 deviation_points: int = 20,
                 profile: Optional["BrokerProfile"] = None,
                 profile_name: Optional[str] = None,
                 login: Optional[int | str] = None,
                 password: Optional[str] = None,
                 server: Optional[str] = None,
                 terminal_path: Optional[str] = None,
                 connect_timeout_ms: int = 30_000,
                 mt5_module: Any = None,
                 state_path: Optional[str] = None) -> None:
        # The intent journal. MT5 has no client order id, so the only way to
        # answer "did my order go through?" after a restart is to remember what
        # was sent -- instrument, side, lots, time, risk -- and match deals
        # against it. Held only in memory, a crash between order_send and the
        # response lost the one record that could resolve the UNKNOWN state,
        # and the realised-history attribution (which strategy, what risk)
        # with it.
        self._state_path = str(state_path) if state_path else None
        if mt5_module is not None:
            mt5 = mt5_module
        else:
            try:
                import MetaTrader5 as mt5  # type: ignore
            except ImportError as exc:  # pragma: no cover - platform dependent
                raise ConfigError(
                    "MetaTrader5 package unavailable (Windows-only). On this host "
                    "run the terminal on a Windows machine with "
                    "scripts/mt5_bridge.py, and set SENTINEL_MT5_BRIDGE and "
                    "SENTINEL_MT5_BRIDGE_TOKEN here -- see docs/UBUNTU-FA.md."
                ) from exc
        self._mt5 = mt5

        # TWO connection styles, and the difference matters operationally.
        #
        #   attach  -- initialize() with no credentials joins a terminal that a
        #              human already signed in. Nothing secret is held by this
        #              process, which is the safest arrangement available on
        #              MetaTrader, and it is what `discover()` proposes.
        #   sign-in -- initialize(login=..., password=..., server=...) makes
        #              the terminal switch accounts. Needed for an unattended
        #              server, and it means this process holds the password for
        #              the duration of the call.
        #
        # The password is passed positionally into MetaTrader5 and never stored
        # on the instance, never logged, and never placed in an exception
        # message -- the failure text below is built from last_error() and the
        # SERVER name only.
        kwargs: Dict[str, Any] = {}
        if terminal_path:
            kwargs["path"] = str(terminal_path)
        if login not in (None, ""):
            try:
                kwargs["login"] = int(login)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"MetaTrader account numbers are numeric; got {login!r}."
                ) from exc
        if password:
            kwargs["password"] = str(password)
        if server:
            kwargs["server"] = str(server)
        if kwargs:
            kwargs["timeout"] = int(connect_timeout_ms)

        # Sign-in needs all three or none. Two of three silently attaches to
        # whatever account the terminal already holds -- which is how a
        # carefully typed live login ends up trading someone's demo, or worse,
        # the reverse.
        supplied = {k for k in ("login", "password", "server") if k in kwargs}
        if supplied and supplied != {"login", "password", "server"}:
            missing = {"login", "password", "server"} - supplied
            raise ConfigError(
                "a MetaTrader sign-in needs the account number, the password "
                "and the server name together; missing: "
                f"{', '.join(sorted(missing))}. Supply all three, or none of "
                "them to attach to the terminal that is already signed in.")

        if not mt5.initialize(**kwargs):
            detail = mt5.last_error()
            raise BrokerError(
                f"MT5 initialize failed: {detail}"
                + (f" (server {server!r}, account {str(login)[-3:]!r})"
                   if supplied else " (attaching to a running terminal)"),
                code="INIT_FAILED")

        # An initialize() that succeeds does not mean the terminal is LOGGED
        # IN: it returns True for a terminal sitting at the login dialog, and
        # every later call then fails with a message about the symbol rather
        # than about the session.
        if mt5.account_info() is None:
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass
            raise BrokerError(
                "the MetaTrader terminal started but is not signed in to any "
                "account. Open the terminal and log in, or supply the account "
                "number, password and server so this service can sign in.",
                code="NOT_LOGGED_IN")
        self._magic = magic
        self._ccy = account_currency

        # The profile is a PRIOR about this venue's behaviour -- symbol
        # spelling, minimum stop distance, filling mode, contract size. It is
        # reconciled against the terminal in _resolve_profile() and the
        # terminal always wins. Without one, every venue quirk is discovered
        # in production, one rejected order at a time.
        from .profiles import get_profile, resolve_profile
        from .profiles import venues as _venues  # noqa: F401 - registers profiles
        # DEEP COPY. get_profile() hands back the singleton in the registry,
        # and instruments() writes an inferred symbol map straight through it
        # -- so one broker's inferred ".m" suffix became every later broker's
        # declared suffix, process-wide, and leaked between tests in file
        # order. A second account on plain symbols then resolved every quote
        # to "EURUSD.m" and every order came back NO_PRICE.
        import copy as _copy
        base_profile = (profile or resolve_profile(profile_name)
                        or get_profile("generic_mt5"))
        self.profile = _copy.deepcopy(base_profile)
        self.profile_mismatches: List[Any] = []
        #: canonical -> venue symbol, filled by instruments()
        self._venue_symbol: Dict[str, str] = {}
        self._canonical: Dict[str, str] = {}
        self._symbol_lock = threading.RLock()
        self._building = False
        #: Venue stop floor in POINTS, per canonical symbol. Points are not
        #: comparable across instruments, so one profile-wide integer applied
        #: to every instrument's own tick made an index's 300-point floor into
        #: a 30-pip floor on EUR/USD and vetoed every entry.
        self._stop_points: Dict[str, int] = {}

        self._deviation = deviation_points
        self._locks: Dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._blackout: Dict[str, int] = {}
        self._sent: Dict[str, Dict[str, Any]] = {}
        self._instruments = instruments or {}

        # Build the symbol table NOW, not on first use. Orchestrator.start()
        # replays the journal and reconciles BEFORE anything calls
        # instruments(), so a lazy table meant the canonical map was empty
        # exactly when the first positions were read -- and a venue spelling
        # ("EURUSD.m" mangled to "EURUSD.M") reached the reconciler, the risk
        # engine, _position_meta and the audit journal. An ordinary known
        # position was then classified as a critical orphan and the agent
        # halted on every restart at any suffixed broker.
        if not self._instruments:
            try:
                built = self.instruments()
            except Exception as exc:  # noqa: BLE001
                built = {}
                self._symbol_table_error = str(exc)
            if not built:
                # The table is EMPTY, either because symbols_get() raised or --
                # the silent variant -- because it returned nothing. MT5's
                # symbols_get genuinely can do either for a few seconds after
                # initialize().
                #
                # Carrying on with an empty table is what produced the original
                # defect: _canon() falls back to the profile rule, uppercases a
                # name it cannot split, and "EURUSD.m" becomes "EURUSD.M" --
                # neither the canonical name nor the venue's. That string then
                # reached the reconciler, which classified a perfectly ordinary
                # known position as a CRITICAL ORPHAN and halted the agent, and
                # it was written verbatim into the hash-chained audit journal.
                #
                # Retry, and if the terminal still will not answer, REFUSE TO
                # BUILD. A broker adapter that cannot name its own instruments
                # is not a degraded adapter, it is a broken one, and failing
                # here is recoverable in a way that corrupting the audit trail
                # is not.
                import time
                for attempt in range(_SYMBOL_TABLE_RETRIES):
                    time.sleep(_SYMBOL_TABLE_RETRY_SEC)
                    try:
                        built = self.instruments()
                    except Exception as exc:  # noqa: BLE001
                        self._symbol_table_error = str(exc)
                        continue
                    if built:
                        break
                if not built:
                    raise BrokerError(
                        "the terminal returned no symbols, so instrument names "
                        "cannot be translated. Refusing to start rather than "
                        "write venue-specific spellings into the audit journal "
                        "and report known positions as orphans."
                        + (f" Last error: {self._symbol_table_error}"
                           if getattr(self, "_symbol_table_error", None) else ""),
                        code="NO_SYMBOLS")
        else:
            # Instruments supplied by the caller bypass instruments(), which
            # also bypasses profile verification -- so min_stop_distance() would
            # silently return 0 and the venue-floor veto would be disabled.
            # Populate the translation table from what was supplied.
            for canonical in self._instruments:
                venue_name = self.profile.symbols.to_venue(canonical)
                self._venue_symbol[canonical] = venue_name
                self._canonical[venue_name] = canonical

        self.capabilities = BrokerCapabilities(
            supports_client_order_id=False,
            supports_server_side_stop=True,
            supports_transaction_stream=False,
            supports_partial_close=True,
            supports_fractional_lots=True,
            min_lot=D("0.01"), lot_step=D("0.01"), name="mt5",
            notes="No server-side duplicate rejection. Degraded anti-duplication "
                  "protocol in force: per-instrument lock + mandatory state query "
                  "+ 30s blackout after any unknown outcome.",
            supports_bar_history=True,
        )
        #: Server clock minus UTC, in seconds, as last MEASURED from a fresh
        #: tick. None until one has been seen; the profile's declaration is the
        #: fallback. See _server_offset_sec.
        self._server_offset_sec: Optional[int] = None
        self._server_offset_measured_ns: int = 0
        if self._state_path:
            import json as _json
            from pathlib import Path as _Path
            journal = _Path(self._state_path)
            if journal.exists():
                try:
                    loaded = _json.loads(journal.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        self._sent.update(loaded)
                except (ValueError, OSError) as exc:
                    # Unreadable is not empty: an empty journal would make every
                    # in-flight order from before the restart invisible.
                    raise ConfigError(
                        f"the MT5 intent journal at {journal} is unreadable ({exc}); "
                        "restore it or move it aside deliberately") from exc

    # -- helpers ------------------------------------------------------------ #

    # -- symbol translation -------------------------------------------- #
    #
    # The system speaks canonical names (EUR_USD) everywhere. The venue speaks
    # whatever it speaks (EURUSD, EURUSD.m, EURUSDmicro). Translation happens
    # HERE and nowhere else: a venue suffix that leaks into the risk engine
    # turns one instrument into two, and a netting calculation that thinks
    # EURUSD and EURUSD.m are unrelated under-counts exposure.

    def _venue(self, canonical: str) -> str:
        """Canonical -> venue. Falls back to the profile's rule when the
        symbol table has not been built yet."""
        if canonical in self._venue_symbol:
            return self._venue_symbol[canonical]
        return self.profile.symbols.to_venue(canonical)

    def _canon(self, venue_symbol: str) -> str:
        """Venue -> canonical, building the table first if it is not there.

        Never guesses from an empty table. The profile's rule is only a
        fallback once the terminal's own list has been consulted and the
        symbol is genuinely absent from it -- at which point returning the
        venue name unchanged is more honest than a mangled derivation.
        """
        if venue_symbol in self._canonical:
            return self._canonical[venue_symbol]
        # Build the table on demand -- but NOT while we are already inside the
        # builder, which would recurse and hand back half-built names.
        if not self._canonical and not self._building:
            try:
                self.instruments()
            except Exception:  # noqa: BLE001
                pass
            if venue_symbol in self._canonical:
                return self._canonical[venue_symbol]
        return self._canon_raw(venue_symbol)

    def _canon_raw(self, venue_symbol: str) -> str:
        """Apply the profile's rule, refusing to invent a name it cannot form.

        A derivation that keeps venue punctuation means the rule did not apply
        to this symbol -- which is how "EURUSD.m" became "EURUSD.M", a string
        matching nothing at the venue and nothing in this system. Returning the
        venue name unchanged is worse than a correct translation and far better
        than a mangled one: it at least names something real.
        """
        derived = self.profile.symbols.to_canonical(venue_symbol)
        # "_" is the CANONICAL separator (EUR_USD), not venue punctuation --
        # treating it as a sign of failure rejected every correct translation.
        # "." and "-" are what a venue suffix looks like, and their survival
        # means the rule did not apply to this name.
        if derived != venue_symbol and any(c in derived for c in ".-"):
            return venue_symbol
        return derived

    def _resolve_profile(self, observed: Dict[str, Dict[str, Any]]) -> None:
        """Reconcile the declared profile with what the terminal reports.

        Every disagreement is recorded and the OBSERVED value wins. A profile
        that silently overrode the venue would be a confident lie, and the
        specific lie -- "the minimum stop is 0 points" when the venue enforces
        20 -- produces an order the venue rejects after the risk engine has
        already sized the position for a stop that cannot exist.
        """
        corrected, mismatches = self.profile.verify_against(observed)
        self.profile = corrected
        self.profile_mismatches = mismatches
        for mismatch in mismatches:
            import logging
            logging.getLogger(__name__).warning(
                "broker profile %s: %s", self.profile.name, mismatch)

    def min_stop_distance(self, canonical: str) -> Decimal:
        """Venue-enforced minimum distance between price and stop, in PRICE.

        PER SYMBOL. Points are not comparable between instruments: 300 points
        on an index and 300 points on EUR/USD are different distances, and
        collapsing the venue's whole universe into one profile-wide maximum
        made an index's floor into a 30-pip floor on EUR/USD -- vetoing every
        ordinary FX entry, and looking like a strategy problem rather than a
        unit error. The profile-wide value survives only as the fallback for a
        symbol the terminal did not report.
        """
        inst = self.instruments().get(canonical)
        if inst is None:
            return D("0")
        points = self._stop_points.get(canonical, self.profile.min_stop_level_points)
        return dec(points) * inst.tick

    def _filling_for(self, canonical: str) -> int:
        """The filling mode this venue accepts for this symbol.

        Order of preference: what the terminal reports for the symbol, then
        what the profile declares, then IOC. Asking the terminal is not
        optional -- the same broker can accept IOC on its Standard book and
        only FOK on its ECN book.
        """
        mt5 = self._mt5
        info = None
        try:
            info = mt5.symbol_info(self._venue(canonical))
        except Exception:  # noqa: BLE001 - a missing symbol is handled elsewhere
            info = None
        reported = getattr(info, "filling_mode", None) if info is not None else None
        if reported is not None:
            # `symbol_info().filling_mode` is a BITMASK of the modes the venue
            # permits -- SYMBOL_FILLING_FOK=1, SYMBOL_FILLING_IOC=2 -- while
            # the value we must SEND is ORDER_FILLING_FOK=0, IOC=1, RETURN=2.
            # The two namespaces collide numerically, so an earlier version
            # that tried an exact-value match first read a FOK-only broker
            # (bitmask 1) as IOC and an IOC-only broker (bitmask 2) as RETURN
            # -- and every single order came back "Unsupported filling mode",
            # which is the exact first-run failure this function exists to
            # prevent. Treat it as a bitmask, and only as a bitmask.
            mask = int(reported)
            if mask & 1:
                return int(mt5.ORDER_FILLING_FOK)
            if mask & 2:
                return int(mt5.ORDER_FILLING_IOC)
            if mask & 4:
                return int(mt5.ORDER_FILLING_RETURN)

        from .profiles import FillingMode
        declared = self.profile.default_filling
        if declared is FillingMode.FOK:
            return int(mt5.ORDER_FILLING_FOK)
        if declared is FillingMode.RETURN:
            return int(mt5.ORDER_FILLING_RETURN)
        return int(mt5.ORDER_FILLING_IOC)

    def _lock_for(self, symbol: str) -> threading.Lock:
        with self._global_lock:
            return self._locks.setdefault(symbol, threading.Lock())

    def _in_blackout(self, symbol: str) -> bool:
        until = self._blackout.get(symbol, 0)
        return wall_ns() < until

    def instruments(self) -> Dict[str, Instrument]:
        # One builder at a time: a concurrent caller could otherwise observe a
        # half-built translation table and resolve a symbol two different ways
        # inside one cycle.
        with self._symbol_lock:
            return self._build_instruments()

    def _build_instruments(self) -> Dict[str, Instrument]:
        if self._instruments:
            return dict(self._instruments)
        self._building = True
        try:
            return self._build_instruments_inner()
        finally:
            self._building = False

    def _build_instruments_inner(self) -> Dict[str, Instrument]:
        # SYMBOL_TRADE_MODE_DISABLED == 0: an indicative or retired symbol. It
        # still has a name and digits, so without this filter "EURUSD" on a
        # book where only "EURUSD.m" is tradeable could win the translation and
        # every order would be refused with a message about the symbol.
        raw = [s for s in (self._mt5.symbols_get() or [])
               if int(getattr(s, "trade_mode", 4) or 0) != 0]

        # A profile that declares no symbol convention infers one from the
        # terminal's own list, rather than guessing a suffix and failing on
        # every order. Loud guessing that gets checked is fine; silent
        # guessing is not.
        if not self.profile.symbols.suffix and not self.profile.symbols.prefix \
                and not self.profile.symbols.overrides:
            from .profiles.base import infer_symbol_map
            inferred = infer_symbol_map([s.name for s in raw])
            if inferred.suffix:
                import logging
                logging.getLogger(__name__).info(
                    "inferred symbol suffix %r for %s from the terminal's "
                    "symbol list", inferred.suffix, self.profile.name)
                self.profile.symbols = inferred

        out: Dict[str, Instrument] = {}
        observed: Dict[str, Dict[str, Any]] = {}
        for s in raw:
            venue_name = s.name
            canonical = self._canon(venue_name)
            # Derive the currency pair from the CANONICAL name, never the
            # venue name. Slicing the venue string kept the suffix: "US30.m"
            # became base "US3" / quote "0.M", and that quote currency flowed
            # into the conversion table, into sizing, and into the reconciler's
            # emergency-stop arithmetic.
            if "_" in canonical and len(canonical) == 7:
                base, quote = canonical.split("_", 1)
            elif len(canonical) == 6 and canonical.isalpha():
                base, quote = canonical[:3].upper(), canonical[3:].upper()
            else:
                # Not a currency pair -- an index, a metal, an energy. Its P&L
                # is denominated in the account currency; inventing a
                # three-letter code from the name would be worse than saying so.
                base, quote = canonical, self._ccy
            digits = int(s.digits)
            pip_exp = -(digits - 1) if digits in (3, 5) else -digits
            out[canonical] = Instrument(
                symbol=canonical, base=base, quote=quote,
                pip=D("10") ** pip_exp, tick=D("10") ** (-digits),
                contract_size=dec(s.trade_contract_size),
                min_lot=dec(s.volume_min), lot_step=dec(s.volume_step),
                max_lot=dec(s.volume_max), venue="mt5",
            )
            self._venue_symbol[canonical] = venue_name
            self._canonical[venue_name] = canonical
            self._stop_points[canonical] = int(getattr(s, "trade_stops_level", 0) or 0)
            observed[canonical] = {
                "stop_level_points": int(getattr(s, "trade_stops_level", 0) or 0),
                "freeze_level_points": int(getattr(s, "trade_freeze_level", 0) or 0),
                "contract_size": dec(s.trade_contract_size),
                "lot_step": dec(s.volume_step),
                "filling_mode": getattr(s, "filling_mode", None),
            }
        self._instruments = out
        # Verify against the ORIGINAL declaration, not against an
        # already-corrected profile: re-verifying a corrected profile finds no
        # disagreement and resets profile_mismatches to [], erasing the
        # operator-visible evidence while keeping the correction.
        if not self.profile_mismatches:
            self._resolve_profile(observed)
        return dict(out)

    def quote(self, symbol: str) -> Quote:
        t = self._mt5.symbol_info_tick(self._venue(symbol))
        if t is None:
            raise BrokerError(f"no tick for {symbol}", code="NO_QUOTE")
        self._observe_server_clock(t)
        return Quote(instrument=symbol, bid=dec(t.bid), ask=dec(t.ask),
                     ts_ns=int(t.time_msc) * 1_000_000, received_ns=wall_ns(), source="mt5")

    # -- bar history ---------------------------------------------------------- #
    #
    # MetaTrader stamps every bar and tick in the BROKER'S SERVER CLOCK and the
    # Python package hands that number over as though it were UTC. Most FX
    # servers run on Eastern European time so that the daily candle closes at
    # 17:00 New York, which puts every bar two or three hours ahead of UTC
    # depending on the season. Stored uncorrected, an H4 bar labelled 08:00
    # actually covers 05:00-09:00 UTC: the session strategies trade the wrong
    # hour, the weekend-flat rule fires late, and the bar index disagrees with
    # the quote clock the risk engine measures staleness against.
    #
    # The offset is MEASURED rather than declared: a fresh tick's server time
    # against the wall clock, rounded to the half hour. The profile's
    # declaration is only the fallback for the first minutes after a weekend
    # start, when no fresh tick exists to measure from.

    def _observe_server_clock(self, tick: Any) -> None:
        """Calibrate the server offset from a tick, if the tick is fresh."""
        try:
            server_sec = int(getattr(tick, "time_msc", 0) or 0) / 1000.0
        except (TypeError, ValueError):
            return
        if server_sec <= 0:
            return
        import time as _time
        raw = server_sec - _time.time()
        # Snap to the half-hour grid; the residual is the tick's own age plus
        # the round trip, and must be small or the tick is not fresh.
        snapped = int(round(raw / _OFFSET_QUANTUM_SEC)) * _OFFSET_QUANTUM_SEC
        if abs(snapped) > _OFFSET_MAX_ABS_SEC or abs(raw - snapped) > _OFFSET_MAX_STALE_SEC:
            return
        if self._server_offset_sec != snapped:
            import logging
            logging.getLogger(__name__).info(
                "MetaTrader server clock measured at UTC%+d:%02d",
                snapped // 3600, abs(snapped) % 3600 // 60)
        self._server_offset_sec = snapped
        self._server_offset_measured_ns = wall_ns()

    def _server_offset_seconds(self) -> int:
        """Server clock minus UTC, in seconds: measured if possible, declared otherwise."""
        if self._server_offset_sec is not None:
            return self._server_offset_sec
        base = int(getattr(self.profile, "server_utc_offset_hours", 0) or 0)
        if getattr(self.profile, "server_observes_dst", False):
            from datetime import datetime, timezone
            from ..core.tzrules import _eu_dst
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if _eu_dst(now):
                base += 1
        return base * 3600

    def fetch_bars(self, symbol: str, timeframe: str, count: int, *,
                   end_ns: Optional[int] = None) -> List[Bar]:
        attr = _MT5_TIMEFRAME_ATTR.get(timeframe)
        if attr is None or not hasattr(self._mt5, attr):
            raise BrokerError(f"MetaTrader has no {timeframe} timeframe", code="BAD_TIMEFRAME")
        if count <= 0:
            return []
        tf_const = getattr(self._mt5, attr)
        venue = self._venue(symbol)
        interval_ns = _TIMEFRAME_SEC[timeframe] * 1_000_000_000
        now_ns = end_ns if end_ns is not None else wall_ns()

        # Calibrate from a tick BEFORE reading bars, so the first bars a fresh
        # process stores are already on the right clock.
        try:
            tick = self._mt5.symbol_info_tick(venue)
            if tick is not None:
                self._observe_server_clock(tick)
        except Exception:  # noqa: BLE001 - a tick is a convenience here, not a need
            pass

        # +1: the terminal's newest bar is the one still forming.
        rates = self._mt5.copy_rates_from_pos(venue, tf_const, 0, int(count) + 1)
        if rates is None or len(rates) == 0:
            # A symbol not in Market Watch returns nothing. Select it and try
            # once more; a second empty answer is a real answer.
            try:
                self._mt5.symbol_select(venue, True)
            except Exception:  # noqa: BLE001
                pass
            rates = self._mt5.copy_rates_from_pos(venue, tf_const, 0, int(count) + 1)
        if rates is None or len(rates) == 0:
            detail = self._mt5.last_error()
            raise BrokerError(f"no {timeframe} bars for {symbol} ({venue}): {detail}",
                              code="NO_BARS")

        offset_ns = self._server_offset_seconds() * 1_000_000_000
        out: List[Bar] = []
        for row in rates:
            try:
                start_ns = int(_field(row, "time")) * 1_000_000_000 - offset_ns
                end_bar = start_ns + interval_ns
                out.append(Bar(
                    instrument=symbol, timeframe=timeframe,
                    open=dec(float(_field(row, "open"))), high=dec(float(_field(row, "high"))),
                    low=dec(float(_field(row, "low"))), close=dec(float(_field(row, "close"))),
                    volume=dec(float(_field(row, "tick_volume", 0) or 0)),
                    start_ns=start_ns, end_ns=end_bar,
                    complete=end_bar <= now_ns, source="mt5",
                ))
            except (TypeError, ValueError, KeyError, IndexError):
                # A malformed row is skipped, never repaired: a bar with a high
                # below its low is not data.
                continue
        out.sort(key=lambda b: b.start_ns)
        if end_ns is not None:
            out = [b for b in out if b.start_ns < end_ns]
        return out[-int(count):] if len(out) > count else out

    def conversion_rate(self, quote_ccy: str, account_ccy: str) -> Decimal:
        if quote_ccy == account_ccy:
            return D("1")
        for name, invert in ((f"{quote_ccy}{account_ccy}", False),
                             (f"{account_ccy}{quote_ccy}", True)):
            # Through _venue(), which consults the table the terminal actually
            # reported. Going straight to the profile's RULE is wrong whenever
            # the rule and the terminal disagree -- overrides, an inferred
            # suffix, or any call before the table exists.
            t = self._mt5.symbol_info_tick(
                self._venue(f"{name[:3]}_{name[3:]}") if len(name) == 6 else name)
            if t and t.bid:
                mid = (dec(t.bid) + dec(t.ask)) / D("2")
                return (D("1") / mid) if invert else mid
        # No direct pair -- the normal case on a CENT account (USC, EUC...),
        # where no "USDUSC" symbol exists. Ask the terminal instead: a symbol
        # quoted in `quote_ccy` reports the value of one tick of one lot in the
        # ACCOUNT currency, so tick_value / (tick_size x contract_size) IS the
        # quote -> account rate, by the broker's own definition of its cent
        # account (whatever that is: some brokers price a cent account at 1
        # USC per 1 USD of P&L on a micro contract, others at 100).
        rate = self._rate_from_tick_value(quote_ccy)
        if rate is not None:
            return rate
        raise ConversionMissingError(f"no {quote_ccy}->{account_ccy} symbol",
                                     quote_ccy=quote_ccy, account_ccy=account_ccy)

    def _rate_from_tick_value(self, quote_ccy: str) -> Optional[Decimal]:
        instruments = self._instruments or {}
        for canonical, inst in instruments.items():
            if inst.quote != quote_ccy:
                continue
            try:
                info = self._mt5.symbol_info(self._venue(canonical))
            except Exception:  # noqa: BLE001 - try the next symbol
                continue
            if info is None:
                continue
            tv = dec(getattr(info, "trade_tick_value", 0) or 0)
            ts = dec(getattr(info, "trade_tick_size", 0) or 0)
            cs = dec(getattr(info, "trade_contract_size", 0) or 0)
            if tv > 0 and ts > 0 and cs > 0:
                rate = tv / (ts * cs)
                if rate > 0:
                    return rate
        return None

    def account(self) -> AccountState:
        a = self._mt5.account_info()
        if a is None:
            raise BrokerError("account_info unavailable")
        return AccountState(
            account_id=str(a.login), currency=a.currency,
            balance=dec(a.balance), equity=dec(a.equity),
            margin_used=dec(a.margin), margin_available=dec(a.margin_free),
            unrealised_pnl=dec(a.profit),
            open_positions=len(self._mt5.positions_get() or []),
            last_transaction_id="", ts_ns=wall_ns(), source="mt5",
            # MT5 ACCOUNT_TRADE_MODE: 0 demo, 1 contest, 2 real. A contest
            # account is not real money, so it groups with demo. Anything the
            # terminal does not report stays "" -- an unknown account type must
            # never be reported as "demo", which is the reassuring answer.
            # `or -1` erased the value for ACCOUNT_TRADE_MODE_DEMO, which is
            # 0 and therefore falsy -- so every ordinary MT5 demo account
            # reported its type as unknown, and the probe's demo/live check
            # (the most consequential one it makes) degraded to "could not
            # determine" for the most common account in the field.
            account_type=_MT5_TRADE_MODE.get(_trade_mode(a), ""),
            leverage=int(getattr(a, "leverage", 0) or 0),
            venue_name=str(getattr(a, "company", "") or ""),
        )

    def positions(self) -> List[Position]:
        out: List[Position] = []
        book = self._mt5.positions_get()
        if book is None:
            # None is "the terminal could not answer", which is not "no
            # positions". Returning [] here made the reconciler drop every
            # known position as a phantom on the first blink of the link.
            raise BrokerError("MT5 position book unavailable", code="BOOK_UNAVAILABLE")
        symbols = [p.symbol for p in book]
        if len(symbols) != len(set(symbols)):
            # A hedging account can hold two tickets on one symbol. The agent
            # keys everything on (instrument, side) and the reconciler maps one
            # position per instrument, so a second ticket would be silently
            # collapsed into the first. Refuse rather than guess; the account
            # this engine trades must be its own.
            dupes = sorted({s for s in symbols if symbols.count(s) > 1})
            raise BrokerError(
                f"multiple tickets on {', '.join(dupes)}: this engine needs a dedicated "
                "account with at most one position per symbol", code="MULTI_TICKET")
        for p in book:
            out.append(Position(
                instrument=self._canon(p.symbol),
                side=Side.BUY if p.type == self._mt5.POSITION_TYPE_BUY else Side.SELL,
                lots=dec(p.volume), entry_price=dec(p.price_open),
                opened_ns=int(p.time_msc) * 1_000_000,
                stop_loss=dec(p.sl) if p.sl else None,
                take_profit=dec(p.tp) if p.tp else None,
                broker_stop_confirmed=bool(p.sl),
                venue_position_id=str(p.ticket),
                financing_paid=dec(p.swap),
                strategy=str(p.magic),
            ))
        return out

    def open_orders(self) -> List[Order]:
        return []

    # -- trading ------------------------------------------------------------ #

    def submit(self, intent: OrderIntent, *, timeout_ms: int = 5000) -> SubmitResult:
        if self._in_blackout(intent.instrument):
            raise UnknownOutcomeError(
                "instrument is in post-timeout blackout; resolve the previous "
                "order's state before submitting again",
                instrument=intent.instrument,
            )
        lock = self._lock_for(intent.instrument)
        if not lock.acquire(timeout=timeout_ms / 1000):
            raise UnknownOutcomeError("could not acquire submission lock in time",
                                      instrument=intent.instrument)
        try:
            # Degraded duplicate check: a matching deal within the window means
            # the previous attempt landed.
            prior = self.query_order(intent.client_order_id)
            if prior is not None and prior.state is OrderState.FILLED:
                return prior

            mt5 = self._mt5
            tick = mt5.symbol_info_tick(self._venue(intent.instrument))
            if tick is None:
                return SubmitResult(state=OrderState.REJECTED, reject_reason="NO_PRICE")
            price = float(tick.ask if intent.side is Side.BUY else tick.bid)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self._venue(intent.instrument),
                "volume": float(intent.lots),
                "type": mt5.ORDER_TYPE_BUY if intent.side is Side.BUY else mt5.ORDER_TYPE_SELL,
                "price": price,
                "deviation": self._deviation,
                "magic": self._magic,
                # The only place our id can live. Brokers may truncate it, which
                # is exactly why this is a degraded path.
                "comment": intent.client_order_id[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                # NOT a hard-coded IOC. Filling mode is per-symbol and
                # per-broker, and a venue that only accepts FOK rejects every
                # single order with "Unsupported filling mode" -- the most
                # common first-run failure at a new broker, and one that looks
                # like the software is broken rather than mis-configured.
                "type_filling": self._filling_for(intent.instrument),
            }
            if intent.stop_loss is not None:
                request["sl"] = float(intent.stop_loss)
            if intent.take_profit is not None:
                request["tp"] = float(intent.take_profit)

            self._sent[intent.client_order_id] = {
                "instrument": intent.instrument, "side": intent.side.value,
                "lots": float(intent.lots), "ts_ns": wall_ns(),
                "initial_risk": str(intent.risk_amount), "strategy": intent.strategy,
            }
            self._persist_sent()
            try:
                result = mt5.order_send(request)
            except Exception as exc:  # noqa: BLE001
                # Over the bridge, an envelope refusal is a certain "nothing
                # happened" -> a rejection. Anything else from order_send --
                # a dropped socket, a terminal exception -- is an outcome the
                # adapter cannot know, and MetaTrader cannot deduplicate.
                name = exc.__class__.__name__
                if name == "BridgeRefused":
                    self._sent.pop(intent.client_order_id, None)
                    self._persist_sent()
                    return SubmitResult(state=OrderState.REJECTED,
                                        reject_reason=f"BRIDGE_REFUSED: {exc}"[:200],
                                        raw={"bridge": True})
                self._blackout[intent.instrument] = wall_ns() + _BLACKOUT_NS
                raise UnknownOutcomeError(f"order_send failed mid-flight: {exc}",
                                          instrument=intent.instrument) from exc
            if result is None:
                self._blackout[intent.instrument] = wall_ns() + _BLACKOUT_NS
                raise UnknownOutcomeError("order_send returned no result",
                                          instrument=intent.instrument,
                                          last_error=str(mt5.last_error()))
            # TIMEOUT (10012) and CONNECTION (10031) mean the request left
            # and no answer came back. Treating them as rejections told the OMS
            # nothing happened while the venue may have filled -- the exact
            # state UNKNOWN exists for. PARTIAL (10010) is a fill of some of it.
            partial = int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010))
            ambiguous = {int(getattr(mt5, "TRADE_RETCODE_TIMEOUT", 10012)),
                         int(getattr(mt5, "TRADE_RETCODE_CONNECTION", 10031))}
            if int(result.retcode) in ambiguous:
                self._blackout[intent.instrument] = wall_ns() + _BLACKOUT_NS
                raise UnknownOutcomeError(
                    f"MT5 returned retcode {result.retcode}: the order may have executed",
                    instrument=intent.instrument, retcode=int(result.retcode))
            if int(result.retcode) not in (int(mt5.TRADE_RETCODE_DONE), partial):
                return SubmitResult(state=OrderState.REJECTED,
                                    reject_reason=f"RETCODE_{result.retcode}",
                                    raw={"comment": getattr(result, "comment", "")})
            fill = Fill(
                client_order_id=intent.client_order_id,
                venue_order_id=str(result.order),
                instrument=intent.instrument, side=intent.side,
                lots=dec(result.volume), price=dec(result.price),
                ts_ns=wall_ns(), received_ns=wall_ns(),
            )
            # Read the stop BACK from the venue. MT5 accepts the order and
            # applies its own stop-level rules to `sl`, so a stop too close to
            # the market is zeroed while the deal still reports success --
            # leaving a filled, unprotected position that reports as protected.
            stop_confirmed = True
            stop_reason = None
            if intent.stop_loss is not None:
                try:
                    live = self._mt5.positions_get(
                        symbol=self._venue(intent.instrument)) or ()
                    applied = next((float(p.sl) for p in live
                                    if str(p.ticket) == str(result.order)
                                    or str(p.identifier) == str(result.order)), None)
                    if applied is None:
                        # Only the ticket this order opened counts. Reading the
                        # stop off ANY position on the symbol reported an older
                        # ticket's stop as this one's.
                        applied = 0.0
                    if not applied:
                        stop_confirmed = False
                        stop_reason = ("the terminal applied no stop-loss: the "
                                       "requested level is probably inside the "
                                       "broker's minimum stop distance")
                except Exception as exc:  # noqa: BLE001
                    stop_confirmed = False
                    stop_reason = f"could not read the stop back from MT5: {exc}"
            return SubmitResult(state=(OrderState.PARTIAL if int(result.retcode) == partial
                                       else OrderState.FILLED),
                                venue_order_id=str(result.order),
                                fills=[fill], venue_ts_ns=fill.ts_ns,
                                stop_confirmed=stop_confirmed,
                                stop_reject_reason=stop_reason)
        finally:
            lock.release()

    def _persist_sent(self) -> None:
        """Write the intent journal atomically, before the socket is touched."""
        if not self._state_path:
            return
        import json as _json
        import os as _os
        from pathlib import Path as _Path
        journal = _Path(self._state_path)
        journal.parent.mkdir(parents=True, exist_ok=True)
        tmp = journal.with_suffix(journal.suffix + ".tmp")
        fd = _os.open(tmp, _os.O_CREAT | _os.O_TRUNC | _os.O_WRONLY, 0o600)
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as fh:
                _json.dump(self._sent, fh)
                fh.flush()
                _os.fsync(fh.fileno())
        except Exception:
            try:
                _os.close(fd)
            except OSError:
                pass
            raise
        _os.replace(tmp, journal)

    def query_order(self, client_order_id: str) -> Optional[SubmitResult]:
        """Signature match over recent deals -- the best MT5 allows."""
        sent = self._sent.get(client_order_id)
        if sent is None:
            return None
        from datetime import datetime, timedelta, timezone

        since = datetime.fromtimestamp(sent["ts_ns"] / 1e9, tz=timezone.utc) - timedelta(seconds=5)
        deals = self._mt5.history_deals_get(since, datetime.now(timezone.utc)) or []
        for d in deals:
            comment_match = client_order_id[:31] in str(getattr(d, "comment", ""))
            signature_match = (
                self._canon(d.symbol) == sent["instrument"]
                and abs(float(d.volume) - sent["lots"]) < 1e-9
                and int(d.magic) == self._magic
            )
            if comment_match or signature_match:
                return SubmitResult(
                    state=OrderState.FILLED, venue_order_id=str(d.order),
                    fills=[Fill(client_order_id=client_order_id, venue_order_id=str(d.order),
                                instrument=self._canon(d.symbol),
                                side=Side.BUY if d.type == 0 else Side.SELL,
                                lots=dec(d.volume), price=dec(d.price),
                                ts_ns=int(d.time_msc) * 1_000_000,
                                commission=dec(d.commission))],
                )
        return None

    def cancel(self, client_order_id: str) -> bool:
        return False  # market-only adapter

    def close_position(self, instrument: str, lots: Optional[Decimal] = None,
                       *, reason: str = "") -> SubmitResult:
        mt5 = self._mt5
        venue_symbol = self._venue(instrument)
        closed_any = False
        for p in self._mt5.positions_get(symbol=venue_symbol) or []:
            tick = mt5.symbol_info_tick(venue_symbol)
            volume = float(min(dec(lots), dec(p.volume))) if lots else float(p.volume)
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": venue_symbol, "volume": volume,
                "type": mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY
                        else mt5.ORDER_TYPE_BUY,
                "position": p.ticket,
                "price": float(tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask),
                "deviation": self._deviation, "magic": self._magic,
                "comment": f"close:{reason}"[:31],
                "type_filling": self._filling_for(instrument),
            }
            res = mt5.order_send(req)
            if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
                return SubmitResult(state=OrderState.REJECTED,
                                    reject_reason=f"CLOSE_FAILED_{getattr(res,'retcode','none')}")
            closed_any = True
        if not closed_any:
            # NOTHING MATCHED, so nothing was closed. Returning FILLED here was
            # the single most dangerous defect in this adapter: the dead-man
            # watchdog calls close_position() on every open position and
            # journals a successful flatten on the strength of the return
            # value. With a symbol the terminal does not recognise -- which is
            # every symbol at a suffixed broker before the symbol table is
            # built -- the loop body never ran, the watchdog recorded
            # "flatten: done", and the entire book was still on.
            return SubmitResult(state=OrderState.REJECTED,
                                reject_reason="NO_POSITION",
                                raw={"instrument": instrument,
                                     "venue_symbol": venue_symbol})
        return SubmitResult(state=OrderState.FILLED)

    def modify_position(self, instrument: str, *, stop_loss: Optional[Decimal] = None,
                        take_profit: Optional[Decimal] = None) -> bool:
        mt5 = self._mt5
        ok = True
        touched = False
        venue_symbol = self._venue(instrument)
        for p in mt5.positions_get(symbol=venue_symbol) or []:
            touched = True
            req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": venue_symbol,
                   "position": p.ticket,
                   "sl": float(stop_loss) if stop_loss is not None else float(p.sl),
                   "tp": float(take_profit) if take_profit is not None else float(p.tp)}
            res = mt5.order_send(req)
            ok = ok and res is not None and res.retcode == mt5.TRADE_RETCODE_DONE
        # `True` with the loop never entered told the caller a stop had been
        # placed on a position that does not exist here. reconcile.py acts on
        # that by marking the position protected and rewriting its risk.
        return ok and touched

    # -- realised history ------------------------------------------------- #
    #
    # Every post-mortem, lesson and proposal is built on closed trades, and
    # on MetaTrader none of that ran: the adapter exposed no history, so the
    # learning loop was disabled with a one-line audit note and the dashboard
    # looked perfectly healthy. The terminal keeps every deal; a round trip is
    # the set of deals sharing a position id, and its P&L is the sum of their
    # profit, commission, fee and swap -- the venue's own numbers, which is
    # the only P&L worth learning from.

    @property
    def supports_closed_trade_history(self) -> bool:
        return True

    def fetch_closed_trades(self, since_id: str = "") -> tuple[List[ClosedTrade], str]:
        from collections import defaultdict
        from datetime import datetime, timedelta, timezone

        try:
            cursor_ms, cursor_pid = (int(x) for x in (since_id or "0:0").split(":"))
        except ValueError as exc:
            raise ValueError(f"invalid MT5 history cursor {since_id!r}") from exc
        # Deals since the cursor, with a day of margin so a round trip whose
        # opening deal predates the cursor is still assembled whole. Reading
        # from 1970 on every cycle would grow with the account's age.
        lower = (datetime.fromtimestamp(cursor_ms / 1000, tz=timezone.utc)
                 - timedelta(days=45)) if cursor_ms else datetime(2000, 1, 1, tzinfo=timezone.utc)
        deals = self._mt5.history_deals_get(lower, datetime.now(timezone.utc) + timedelta(days=1))
        if deals is None:
            raise BrokerError("MT5 realised history unavailable", code="HISTORY_UNAVAILABLE")
        book = self._mt5.positions_get()
        if book is None:
            raise BrokerError("MT5 position book unavailable", code="BOOK_UNAVAILABLE")
        open_ids = {int(getattr(p, "identifier", p.ticket) or p.ticket) for p in book}

        grouped: Dict[int, list] = defaultdict(list)
        for deal in deals:
            pid = int(getattr(deal, "position_id", 0) or 0)
            if pid:
                grouped[pid].append(deal)

        by_comment = {k[:31]: (k, v) for k, v in self._sent.items()}
        out: List[ClosedTrade] = []
        cursor = (cursor_ms, cursor_pid)
        for pid, values in grouped.items():
            if pid in open_ids:
                continue                      # still open: not a round trip yet
            values.sort(key=lambda d: (int(d.time_msc), int(d.ticket)))
            end = (int(values[-1].time_msc), pid)
            if end <= (cursor_ms, cursor_pid):
                continue
            # DEAL_ENTRY_IN=0, OUT=1, INOUT=2 (reversal), OUT_BY=3.
            entries = [d for d in values if int(getattr(d, "entry", 0)) == 0]
            exits = [d for d in values if int(getattr(d, "entry", 0)) in (1, 3)]
            if not entries or not exits or any(int(getattr(d, "entry", 0)) == 2 for d in values):
                continue                      # a reversal needs a deal ledger; never guess
            if any(int(getattr(d, "magic", 0) or 0) != self._magic for d in entries):
                continue                      # not this engine's trade
            vol_in = sum((dec(d.volume) for d in entries), D("0"))
            vol_out = sum((dec(d.volume) for d in exits), D("0"))
            if vol_in <= 0 or abs(vol_in - vol_out) > D("0.000001"):
                continue                      # partially closed: wait for the rest
            sym = self._canon(entries[0].symbol)
            inst = self.instruments().get(sym)
            if inst is None:
                continue
            entry_px = sum((dec(d.price) * dec(d.volume) for d in entries), D("0")) / vol_in
            exit_px = sum((dec(d.price) * dec(d.volume) for d in exits), D("0")) / vol_out
            side = Side.BUY if int(entries[0].type) == int(self._mt5.ORDER_TYPE_BUY) else Side.SELL
            comment = str(getattr(entries[0], "comment", "") or "")
            coid, meta = by_comment.get(comment[:31], ("", {}))
            initial = dec(meta.get("initial_risk", 0) or 0)
            # Venue amounts arrive as floats and are money in the account
            # currency: quantise to the cent, which is what the statement
            # shows, rather than carry binary noise into R.
            cent = D("0.01")
            profit = sum((dec(getattr(d, "profit", 0) or 0) for d in values), D("0")).quantize(cent)
            fees = sum((dec(getattr(d, "commission", 0) or 0)
                        + dec(getattr(d, "fee", 0) or 0) for d in values), D("0")).quantize(cent)
            swap = sum((dec(getattr(d, "swap", 0) or 0) for d in values), D("0")).quantize(cent)
            net = profit + fees + swap          # fees and swap are negative when charged
            exit_comment = str(getattr(exits[-1], "comment", "") or "")
            out.append(ClosedTrade(
                trade_id=f"MT5-{pid}", strategy=str(meta.get("strategy") or "unattributed"),
                instrument=sym, side=side, lots=vol_in,
                entry_price=entry_px, exit_price=exit_px,
                opened_ns=int(entries[0].time_msc) * 1_000_000, closed_ns=end[0] * 1_000_000,
                pnl=net, pnl_pips=(exit_px - entry_px) * D(side.sign) / inst.pip,
                commission=-fees, financing=swap, initial_risk=initial,
                r_multiple=(net / initial) if initial > 0 else D("0"),
                exit_reason=_exit_reason_from_comment(exit_comment),
                tags=([] if initial > 0 else ["risk_attribution_unavailable"])
                + ([f"coid:{coid}"] if coid else []),
            ))
            cursor = max(cursor, end)
        out.sort(key=lambda t: (t.closed_ns, t.trade_id))
        return out, f"{cursor[0]}:{cursor[1]}"

    def swap_pips_per_day(self, symbol: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """(long, short) overnight swap for one lot, in PIPS, as the venue quotes it.

        The cost model used to carry a zero swap unless the operator typed one.
        The terminal states the real figure per symbol; a carry strategy whose
        whole premise is the swap must read this, not a default.
        """
        info = self._mt5.symbol_info(self._venue(symbol))
        if info is None:
            return None, None
        inst = self.instruments().get(symbol)
        if inst is None:
            return None, None
        mode = int(getattr(info, "swap_mode", 1) or 1)
        long_raw = getattr(info, "swap_long", None)
        short_raw = getattr(info, "swap_short", None)
        if long_raw is None or short_raw is None:
            return None, None
        # SYMBOL_SWAP_MODE_POINTS == 1: swap is in points of the symbol.
        # Other modes (currency, percent) need the contract and the price;
        # report None rather than a number in the wrong unit.
        if mode != 1:
            return None, None
        point = inst.tick
        return (dec(long_raw) * point / inst.pip, dec(short_raw) * point / inst.pip)

    def fetch_ticks(self, symbol: str, start_ns: int, end_ns: int,
                    max_ticks: int = 2_000_000) -> List[tuple]:
        """Raw (utc_ms, bid, ask) ticks between two instants, oldest first.

        This is the venue's own bid AND ask history -- the input the research
        protocol demands for a live-quality dataset, which a bid candle plus a
        stored spread can never reconstruct. ``data/ticks.py`` turns these into
        bid/ask OHLC bars with the manifest the acceptance script verifies.
        """
        from datetime import datetime, timezone
        getter = getattr(self._mt5, "copy_ticks_range", None)
        if getter is None:
            raise BrokerError("this terminal module has no copy_ticks_range", code="NO_TICKS")
        offset = self._server_offset_seconds()
        flags = int(getattr(self._mt5, "COPY_TICKS_INFO", 1))
        lo = datetime.fromtimestamp(start_ns / 1e9 + offset, tz=timezone.utc)
        hi = datetime.fromtimestamp(end_ns / 1e9 + offset, tz=timezone.utc)
        ticks = getter(self._venue(symbol), lo, hi, flags)
        if ticks is None:
            raise BrokerError(f"no ticks for {symbol}: {self._mt5.last_error()}", code="NO_TICKS")
        out: List[tuple] = []
        for row in ticks:
            try:
                ms = int(_field(row, "time_msc"))
                bid = float(_field(row, "bid"))
                ask = float(_field(row, "ask"))
            except (TypeError, ValueError, KeyError):
                continue
            if bid <= 0 or ask <= 0:
                continue
            out.append((ms - offset * 1000, bid, ask))
            if len(out) >= max_ticks:
                break
        out.sort()
        return out

    def transactions_since(self, last_id: str) -> Iterable[Dict[str, Any]]:
        return []  # no ordered stream; reconciliation is snapshot-based

    def close(self) -> None:
        try:
            self._mt5.shutdown()
        except Exception:  # pragma: no cover
            pass
