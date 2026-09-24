"""Broker connections: discovery, a read-only probe, and the activation gate.

Three problems this module exists to solve, in increasing order of how much
money they cost when unsolved.

**1. Configuring a venue is guesswork.** The symbol is ``EURUSD`` or
``EURUSD.m``; the terminal is at one of four paths; the server is called
"AMarkets-Demo" or "AMarkets-Live03". Getting any of it wrong produces a
rejection whose message names none of those things. So: ``discover()`` reads
what is actually on this machine and proposes a complete configuration, and
``probe()`` proves it before a single order exists.

**2. A test that can trade is not a test.** ``probe()`` runs against a
``ReadOnlyBroker`` wrapper whose ``submit``, ``close_position``,
``modify_position`` and ``cancel`` raise. That is a structural guarantee, not
a convention -- a future edit that tries to "just check the order path" fails
loudly instead of opening a position on a live account during a connection
test.

**3. Switching venues while holding positions loses the positions.** They
belong to the old venue. The new adapter reports them as orphans, the
reconciler halts, and in the worst ordering the risk engine sizes new trades
against an account that is not the one holding the exposure. ``activation_
blockers()`` refuses, and it is the caller's job to route every activation
through it.

What a probe deliberately does NOT do
-------------------------------------
It does not certify that the venue is honest, that withdrawals clear, or that
the quotes are real. Those are counterparty questions and no amount of protocol
checking touches them; ``docs/BROKERS.md`` treats them as first-class risk. A
green probe means "this software can talk to this account correctly", and the
report says exactly that.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.clock import wall_ns
from ..core.money import D, dec
from .base import Broker
from .profiles import BrokerProfile, list_profiles, resolve_profile

# A probe must never hang the dashboard behind a dead terminal.
PROBE_TIMEOUT_SEC = 25.0

#: The leverage the sizing arithmetic was written against -- 1:30, the retail
#: default that `Instrument.margin_rate` encodes. The check compares against
#: THIS, not against whatever ceiling a venue advertises for itself, because
#: the question is "does this account permit positions the risk engine assumed
#: were impossible", and an offshore profile declaring 1:1000 answers "no" to
#: every account it will ever see.
ASSUMED_MAX_LEVERAGE = 30

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,38}[a-z0-9]$")


class ConnectionError_(RuntimeError):
    """Configuration problem with a connection record."""


class ProbeRefused(RuntimeError):
    """A write was attempted during a read-only probe."""


# --------------------------------------------------------------------------- #
# the read-only guard
# --------------------------------------------------------------------------- #


class ReadOnlyBroker:
    """A broker that physically cannot trade.

    Every read is forwarded. Every write raises ``ProbeRefused``. Written as an
    explicit allow-list of forwarded names rather than a deny-list of blocked
    ones, because a deny-list silently un-blocks whatever gets added to the
    ``Broker`` interface next -- and the thing added next is as likely to be
    ``submit_bracket`` as ``describe``.
    """

    _READS = frozenset({
        "instruments", "instrument", "quote", "quotes", "conversion_rate",
        "account", "positions", "open_orders", "query_order",
        "transactions_since", "fetch_closed_trades", "ping",
        "capabilities", "profile", "profile_mismatches",
        "supports_closed_trade_history", "supports_bar_history", "close",
        # Tick and swap reads for the history export; neither can reach the
        # order path.
        "fetch_bars", "fetch_ticks", "swap_pips_per_day", "min_stop_distance",
    })
    _WRITES = frozenset({
        "submit", "cancel", "close_position", "modify_position",
    })

    def __init__(self, inner: Broker) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        if name in self._WRITES:
            def _refuse(*_a: Any, **_k: Any) -> Any:
                raise ProbeRefused(
                    f"{name}() was called during a connection test. A test that "
                    "can place or change an order is not a test. Nothing was "
                    "sent to the venue.")
            return _refuse
        if name in self._READS:
            return getattr(object.__getattribute__(self, "_inner"), name)
        raise ProbeRefused(
            f"{name!r} is not on the read-only allow-list, so it is refused "
            "during a connection test. Add it to ReadOnlyBroker._READS only "
            "after confirming it cannot reach the venue's order path.")

    def __setattr__(self, name: str, value: Any) -> None:
        raise ProbeRefused("a connection test may not mutate the adapter")


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #


@dataclass
class ProbeCheck:
    """One statement about the connection, in language a non-trader can act on."""

    id: str
    title: str
    #: True pass, False fail, None "could not be determined here".
    passed: Optional[bool]
    detail: str
    #: block  -- live trading must not be enabled while this fails
    #: warn   -- it will work, but something will surprise you
    #: info   -- a fact worth reading, neither good nor bad
    severity: str = "info"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProbeReport:
    connection_id: str
    profile: str
    adapter: str
    ok: bool
    started_ns: int
    finished_ns: int
    checks: List[ProbeCheck] = field(default_factory=list)
    account: Dict[str, Any] = field(default_factory=dict)
    symbols_total: int = 0
    symbol_examples: Dict[str, str] = field(default_factory=dict)
    mismatches: List[str] = field(default_factory=list)
    degradations: List[str] = field(default_factory=list)
    error: str = ""
    #: Whether an adapter was actually constructed. Used only by the caller to
    #: tell "the venue refused" from "we never got that far".
    opened_adapter: bool = False

    @property
    def blocking_failures(self) -> List[ProbeCheck]:
        return [c for c in self.checks if c.passed is False and c.severity == "block"]

    @property
    def duration_sec(self) -> float:
        return max(0.0, (self.finished_ns - self.started_ns) / 1e9)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["checks"] = [c.to_dict() for c in self.checks]
        out["duration_sec"] = round(self.duration_sec, 2)
        out["blocking_failures"] = [c.id for c in self.blocking_failures]
        return out


@dataclass
class BrokerConnection:
    """One configured venue.

    **No secret is ever stored in this record.** ``secret_ref`` names an entry
    in the ``SecretStore``; the password itself is sealed there. A connection
    file that leaks is then an inventory of which accounts exist, which is
    unpleasant, rather than the accounts themselves, which is terminal.
    """

    id: str
    display_name: str
    profile: str = "generic_mt5"
    #: "auto" means it was found on this machine; "manual" means typed in.
    origin: str = "manual"

    # -- non-secret connection parameters --------------------------------- #
    server: str = ""              # MT5 server name / OANDA environment
    login: str = ""               # account number
    terminal_path: str = ""       # MT5 terminal executable
    account_currency: str = ""
    exchange_id: str = ""         # ccxt only

    #: Name of the sealed credential in the SecretStore. Empty means the
    #: adapter takes its credentials from the environment instead.
    secret_ref: str = ""

    #: Declared by the operator, CHECKED by the probe. A connection the
    #: operator called "demo" that the terminal reports as live is the single
    #: most dangerous disagreement this module can find.
    declared_account_type: str = "demo"   # demo | live

    enabled: bool = False
    created_ns: int = field(default_factory=wall_ns)
    updated_ns: int = field(default_factory=wall_ns)
    last_probe: Optional[Dict[str, Any]] = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id or ""):
            raise ConnectionError_(
                f"connection id {self.id!r} is not usable: use 2-40 characters, "
                "lower-case letters, digits, '-' or '_'. The id ends up in file "
                "names and in the audit journal, so it stays boring on purpose.")
        if self.declared_account_type not in ("demo", "live"):
            raise ConnectionError_(
                "declared_account_type must be 'demo' or 'live'. There is no "
                "'unknown': an operator who does not know which kind of account "
                "this is must not be configuring it.")
        if resolve_profile(self.profile) is None:
            known = ", ".join(p.name for p in list_profiles())
            raise ConnectionError_(
                f"unknown broker profile {self.profile!r}. Known: {known}. "
                "Use 'generic_mt5' for an unlisted MetaTrader broker.")

    @property
    def adapter(self) -> str:
        prof = resolve_profile(self.profile)
        return prof.adapter if prof else "mt5"

    def redacted(self) -> Dict[str, Any]:
        """Safe to send to a dashboard: no secret, and no secret-adjacent path."""
        out = asdict(self)
        out["adapter"] = self.adapter
        out["has_credential"] = bool(self.secret_ref)
        # The login is an account NUMBER. It is not a password, but combined
        # with the server name it is half of a credential pair, so only the
        # tail is shown -- enough to tell two accounts apart, not enough to
        # write down from a shoulder-surf or a screen share.
        out["login"] = _mask_login(self.login)
        out["login_full_length"] = len(self.login or "")
        out.pop("secret_ref", None)
        # `asdict` copied `last_probe` VERBATIM, and the probe's account block
        # holds the venue's own account_id -- the same number the masking above
        # exists to hide -- plus the balance. This endpoint is readable by any
        # authenticated session, including a viewer, three lines away from an
        # accounts endpoint that is owner-gated because "enumerating who can
        # log in is reconnaissance". A summary carries everything the UI needs.
        probe = out.pop("last_probe", None)
        out["last_probe"] = _probe_summary(probe) if probe else None
        return out

    def to_storage(self) -> Dict[str, Any]:
        return asdict(self)


def _probe_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    """The parts of a probe result that are safe for any authenticated reader.

    Keeps the verdict, the timing and the operator-facing checks; drops the
    account block (real account number, balance, equity) and masks the id.
    """
    account = report.get("account") or {}
    checks = []
    for check in report.get("checks") or []:
        checks.append({k: check.get(k) for k in
                       ("id", "title", "passed", "detail", "severity")})
    return {
        "connection_id": report.get("connection_id", ""),
        "profile": report.get("profile", ""),
        "adapter": report.get("adapter", ""),
        "ok": bool(report.get("ok")),
        "started_ns": report.get("started_ns", 0),
        "finished_ns": report.get("finished_ns", 0),
        "duration_sec": report.get("duration_sec", 0),
        "symbols_total": report.get("symbols_total", 0),
        "symbol_examples": report.get("symbol_examples") or {},
        "mismatches": report.get("mismatches") or [],
        "degradations": report.get("degradations") or [],
        "blocking_failures": report.get("blocking_failures") or [],
        "error": report.get("error", ""),
        "checks": checks,
        # Kept ONLY in masked form, and only because "which account did this
        # test actually reach" is the question the report exists to answer.
        "account_id": _mask_login(str(account.get("account_id", "") or "")),
        "account_currency": str(account.get("currency", "") or ""),
        "account_type": str(account.get("account_type", "") or ""),
    }


def normalise_account_id(value: str) -> str:
    """Comparable form of an account number.

    Substring matching was used here and it is wrong in both directions.
    Brokers issue consecutive account numbers, so "50123" is "in" "501234" --
    a DIFFERENT live account at the same broker passed as a match, and the
    probe then affirmatively reported "the account number is the one you
    entered". A one-character stub like "7" matched almost everything. In the
    other direction "1234567" and "1234-567" are the same account written two
    ways and were reported as different.
    """
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if digits:
        return digits.lstrip("0") or "0"
    return "".join((value or "").split()).upper()


def same_account(a: str, b: str) -> Optional[bool]:
    """True/False, or None when one side did not say."""
    left, right = normalise_account_id(a), normalise_account_id(b)
    if not left or not right:
        return None
    return left == right


def _mask_login(login: str) -> str:
    text = (login or "").strip()
    if len(text) <= 3:
        return "•" * len(text)
    return "•" * (len(text) - 3) + text[-3:]


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


class ConnectionStore:
    """Durable list of configured venues. JSON, 0600, atomically replaced."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self.path.exists():
            self._write({})

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Do NOT silently start from empty: that un-configures every venue
            # and the next start-up looks like a fresh install. Move the bad
            # file aside so it can be inspected, and say so.
            damaged = self.path.with_suffix(f".damaged.{wall_ns()}")
            try:
                os.replace(self.path, damaged)
            except OSError:
                pass
            raise ConnectionError_(
                f"{self.path} was not valid JSON and has been moved to "
                f"{damaged}. No venue is configured until it is restored or "
                "re-entered.") from None
        return data if isinstance(data, dict) else {}

    def _write(self, data: Dict[str, Dict[str, Any]]) -> None:
        import tempfile
        # A random name in the same directory: a fixed `<path>.tmp` can be
        # squatted by a directory (every write then fails) and two writers in
        # different processes interleave into one file.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            os.write(fd, json.dumps(data, indent=2, sort_keys=True,
                                    ensure_ascii=False).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- api ---------------------------------------------------------------- #

    def list(self) -> List[BrokerConnection]:
        with self._lock:
            raw = self._read()
        out: List[BrokerConnection] = []
        known = set(BrokerConnection.__dataclass_fields__)
        for record in raw.values():
            try:
                out.append(BrokerConnection(
                    **{k: v for k, v in record.items() if k in known}))
            except (ConnectionError_, TypeError):
                # One unreadable record must not hide the rest. It is skipped
                # and the id is kept visible through `damaged_ids`.
                continue
        return sorted(out, key=lambda c: c.created_ns)

    def damaged_ids(self) -> List[str]:
        with self._lock:
            raw = self._read()
        good = {c.id for c in self.list()}
        return sorted(k for k in raw if k not in good)

    def get(self, connection_id: str) -> Optional[BrokerConnection]:
        for c in self.list():
            if c.id == connection_id:
                return c
        return None

    def upsert(self, conn: BrokerConnection) -> BrokerConnection:
        conn.updated_ns = wall_ns()
        with self._lock:
            data = self._read()
            data[conn.id] = conn.to_storage()
            self._write(data)
        return conn

    def delete(self, connection_id: str) -> bool:
        with self._lock:
            data = self._read()
            existed = data.pop(connection_id, None) is not None
            if existed:
                self._write(data)
            return existed

    def set_probe(self, connection_id: str, report: ProbeReport) -> None:
        """Store the SUMMARY, not the raw report.

        The raw report carries the venue's account number, balance and equity.
        Those belong in the answer to the person who ran the test, not in a
        file that is read on every page load and copied into every backup.
        """
        with self._lock:
            data = self._read()
            if connection_id in data:
                data[connection_id]["last_probe"] = _probe_summary(report.to_dict())
                data[connection_id]["updated_ns"] = wall_ns()
                self._write(data)

    def set_exclusive_enabled(self, connection_id: str) -> None:
        """Enable exactly one connection, in ONE read-modify-write.

        Flipping records one at a time let two concurrent activations each turn
        off only what their own stale snapshot had seen, leaving two enabled --
        which bootstrap then refuses to start with, persistently, until someone
        hand-edits the file.
        """
        with self._lock:
            data = self._read()
            for key, record in data.items():
                record["enabled"] = (key == connection_id)
            self._write(data)


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


#: Substrings that identify a broker from the terminal's own company string.
#: Matched longest-first so "alpari international" does not land on a shorter
#: accidental match.
_COMPANY_HINTS: Tuple[Tuple[str, str], ...] = (
    ("amarkets", "amarkets"),
    ("alpari", "alpari"),
)


def profile_for_company(company: str) -> str:
    """Pick a broker profile from the terminal's company name.

    Returns ``generic_mt5`` when nothing matches, which is the correct answer
    and not a failure: the generic profile declares nothing it cannot read from
    the terminal, so an unrecognised broker still works -- it simply gets no
    head start.
    """
    text = (company or "").strip().lower()
    for needle, profile in sorted(_COMPANY_HINTS, key=lambda kv: -len(kv[0])):
        if needle in text:
            return profile
    return "generic_mt5"


def discover(*, environ: Optional[Dict[str, str]] = None,
             mt5_module: Any = None) -> List[Dict[str, Any]]:
    """Propose connections from what is present on this machine.

    Returns dictionaries rather than ``BrokerConnection`` objects because a
    proposal is not yet a configuration: the operator confirms it, gives it a
    name, and supplies a credential. Auto-filling a form is helpful; auto-
    configuring a trading venue is not.
    """
    env = dict(os.environ if environ is None else environ)
    found: List[Dict[str, Any]] = []

    # -- a running MetaTrader terminal ------------------------------------- #
    # Local package first when it exists (Windows); otherwise the bridge the
    # environment names (an Ubuntu engine reaching a Windows terminal).
    mt5 = mt5_module
    if mt5 is None:
        try:
            from .mt5_bridge import bridge_from_env
            mt5 = bridge_from_env(env)
        except Exception:  # noqa: BLE001 - a misconfigured bridge is not a discovery
            mt5 = None
    if mt5 is None:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception:  # noqa: BLE001 - not installed, not Windows, no terminal
            mt5 = None
    if mt5 is not None:
        try:
            if mt5.initialize():
                info = mt5.account_info()
                term = mt5.terminal_info()
                if info is not None:
                    company = str(getattr(info, "company", "") or "")
                    # MT5 trade_mode: 0 demo, 1 contest, 2 real.
                    trade_mode = int(getattr(info, "trade_mode", 0) or 0)
                    found.append({
                        "source": "metatrader5",
                        "profile": profile_for_company(company),
                        "display_name": f"{company or 'MetaTrader'} "
                                        f"{getattr(info, 'login', '')}".strip(),
                        "server": str(getattr(info, "server", "") or ""),
                        "login": str(getattr(info, "login", "") or ""),
                        "account_currency": str(getattr(info, "currency", "") or ""),
                        "terminal_path": str(getattr(term, "path", "") or "")
                                         if term is not None else "",
                        "company": company,
                        "declared_account_type": "live" if trade_mode == 2 else "demo",
                        "leverage": int(getattr(info, "leverage", 0) or 0),
                        "note": ("یک ترمینال متاتریدر روی این سرور باز است و "
                                 "همین حالا وارد این حساب شده. برای وصل شدن به "
                                 "آن رمزی لازم نیست، چون خودِ ترمینال نشست را "
                                 "نگه داشته است."),
                        "needs_credential": False,
                    })
        except Exception:  # noqa: BLE001 - discovery must never raise
            pass

    # -- OANDA, from the environment --------------------------------------- #
    if env.get("OANDA_ACCOUNT_ID") and env.get("OANDA_API_TOKEN"):
        practice = "-" in env.get("OANDA_ACCOUNT_ID", "")
        found.append({
            "source": "environment",
            "profile": "oanda",
            "display_name": "OANDA",
            "server": "practice" if practice else "live",
            "login": env.get("OANDA_ACCOUNT_ID", ""),
            "account_currency": "",
            "declared_account_type": "demo" if practice else "live",
            "note": ("مشخصات OANDA در محیط این سرویس پیدا شد. همان‌جا می‌مانند "
                     "و چیزی در فایل تنظیمات بروکر کپی نمی‌شود."),
            "needs_credential": False,
        })

    # -- the simulator, always ---------------------------------------------- #
    found.append({
        "source": "builtin",
        "profile": "paper",
        "display_name": "شبیه‌ساز داخلی",
        "server": "", "login": "", "account_currency": "USD",
        "declared_account_type": "demo",
        "note": ("شبیه‌ساز داخلی. به هیچ چیزی نیاز ندارد، هزینه‌ای ندارد، و "
                 "تنها جایی است که اشتباه کردن در آن رایگان است."),
        "needs_credential": False,
    })
    return found


# --------------------------------------------------------------------------- #
# the probe
# --------------------------------------------------------------------------- #


def _spread_ceiling(profile: Optional[BrokerProfile], symbol: str) -> Decimal:
    """How wide a spread is too wide, for THIS instrument at THIS venue.

    Four times the profile's own typical spread, with a floor so a venue that
    declares an implausibly tight 0.1 pip does not fail on every ordinary
    quote. Absolute pip thresholds do not survive contact with an index or a
    crypto pair, whose pip is two or three orders of magnitude larger.
    """
    typical = profile.spread_for(symbol) if profile is not None else D("1.2")
    if typical <= 0:
        typical = D("1.2")
    return max(typical * D("4"), D("3"))


def _check(checks: List[ProbeCheck], id: str, title: str,
           passed: Optional[bool], detail: str, severity: str = "info") -> None:
    checks.append(ProbeCheck(id=id, title=title, passed=passed,
                             detail=detail, severity=severity))


def probe(conn: BrokerConnection, *,
          secret: Optional[str] = None,
          instruments: Optional[List[str]] = None,
          builder: Optional[Callable[..., Broker]] = None,
          timeout_sec: float = PROBE_TIMEOUT_SEC) -> ProbeReport:
    """Connect, read, verify, disconnect. Never trade. Never hang.

    The timeout is enforced by running the whole body in a worker and joining
    with a deadline. The previous version computed a deadline and compared it
    AFTER every blocking venue call had already returned, which bounded
    nothing: a venue that accepts the connection and never answers held the
    caller for as long as it liked.

    ``builder`` exists so tests can hand in a fake adapter; production passes
    nothing and gets :func:`sentinel.brokers.build_broker`.
    """
    result: List[ProbeReport] = []
    worker = threading.Thread(
        target=lambda: result.append(
            _probe_body(conn, secret, instruments, builder, timeout_sec)),
        name=f"probe-{conn.id}", daemon=True)
    started_ns = wall_ns()
    worker.start()
    worker.join(timeout_sec + 2.0)
    if result:
        return result[0]

    # The worker is still inside a blocking venue call. It is a daemon and it
    # holds no lock the caller needs, so it is left to finish and be collected;
    # what matters is that the API thread is released.
    finished = wall_ns()
    report = ProbeReport(connection_id=conn.id, profile=conn.profile,
                         adapter=conn.adapter, ok=False,
                         started_ns=started_ns, finished_ns=finished)
    _check(report.checks, "timeout", "بروکر در زمان مقرر جواب نداد", False,
           f"بعد از {int(timeout_sec)} ثانیه هنوز پاسخی نیامده بود و آزمایش "
           "رها شد. معمولاً یعنی ترمینال باز است ولی به سرور بروکر وصل نیست، "
           "یا نام سرور اشتباه است.", "block")
    report.error = "probe timed out"
    return report


def _probe_body(conn: BrokerConnection, secret: Optional[str],
                instruments: Optional[List[str]],
                builder: Optional[Callable[..., Broker]],
                timeout_sec: float) -> ProbeReport:
    started = wall_ns()
    checks: List[ProbeCheck] = []
    report = ProbeReport(connection_id=conn.id, profile=conn.profile,
                         adapter=conn.adapter, ok=False,
                         started_ns=started, finished_ns=started, checks=checks)

    make = builder or _default_builder
    raw: Optional[Broker] = None
    try:
        deadline = time.monotonic() + timeout_sec
        raw = make(conn, secret)
        guarded = ReadOnlyBroker(raw)
        report.opened_adapter = True
        _check(checks, "connect", "اتصال به بروکر برقرار شد", True,
               f"آداپتور «{conn.adapter}» با پروفایل «{conn.profile}» وصل شد.",
               "block")

        _probe_account(guarded, conn, checks, report)
        _probe_symbols(guarded, conn, checks, report, instruments)
        _probe_capabilities(guarded, conn, checks, report)
        if time.monotonic() > deadline:
            _check(checks, "timeout", "زمان آزمایش طولانی شد", False,
                   f"آزمایش بیش از {int(timeout_sec)} ثانیه طول کشید. معمولاً "
                   "یعنی ترمینال باز است ولی به سرور بروکر وصل نیست.", "warn")
    except ProbeRefused as exc:
        report.error = str(exc)
        _check(checks, "readonly_violation", "آزمایش تلاش کرد سفارش بفرستد", False,
               str(exc), "block")
    except Exception as exc:  # noqa: BLE001 - every failure is a finding
        report.error = f"{exc.__class__.__name__}: {exc}"
        _check(checks, "connect", "اتصال به بروکر برقرار نشد", False,
               _explain_connect_failure(conn, exc), "block")
    finally:
        if raw is not None:
            try:
                raw.close()
            except Exception:  # noqa: BLE001
                pass

    report.finished_ns = wall_ns()
    report.ok = not report.blocking_failures and not report.error
    return report


def _default_builder(conn: BrokerConnection, secret: Optional[str]) -> Broker:
    from . import build_broker
    from ..bootstrap import DEFAULT_INSTRUMENTS

    kwargs: Dict[str, Any] = {}
    adapter = conn.adapter
    if adapter == "mt5":
        if conn.account_currency:
            kwargs["account_currency"] = conn.account_currency
        if conn.login:
            kwargs["login"] = int(conn.login) if conn.login.isdigit() else conn.login
        if secret:
            kwargs["password"] = secret
        if conn.server:
            kwargs["server"] = conn.server
        if conn.terminal_path:
            kwargs["terminal_path"] = conn.terminal_path
    elif adapter == "oanda":
        if conn.login:
            kwargs["account_id"] = conn.login
        if secret:
            kwargs["token"] = secret
        kwargs["environment"] = conn.server or "practice"
    elif adapter == "ccxt":
        kwargs["exchange_id"] = conn.exchange_id or conn.server or "binance"
        if secret:
            key, _, sec = secret.partition(":")
            kwargs["api_key"], kwargs["secret"] = key, sec
    elif adapter == "paper":
        kwargs["instruments"] = dict(DEFAULT_INSTRUMENTS)
    return build_broker(conn.profile, **kwargs)


def _explain_connect_failure(conn: BrokerConnection, exc: Exception) -> str:
    """Turn an adapter exception into something an operator can act on."""
    text = str(exc)
    low = text.lower()
    if "metatrader5" in low and "unavailable" in low:
        return ("بستهٔ MetaTrader5 روی این سرور نصب نیست. این بسته فقط روی "
                "ویندوز کار می‌کند. یا سرویس را روی یک ویندوز اجرا کنید، یا از "
                "بروکری استفاده کنید که رابط وب دارد (مثل OANDA).")
    if "initialize failed" in low:
        return (f"ترمینال متاتریدر پاسخ نداد. بررسی کنید: ترمینال باز است؟ "
                f"مسیر آن درست وارد شده؟ نام سرور «{conn.server or '—'}» دقیقاً "
                "همان چیزی است که در خود ترمینال نوشته شده؟ — جزئیات فنی: " + text)
    if "no_symbols" in low or "returned no symbols" in low:
        return ("ترمینال وصل شد ولی هیچ نمادی برنگرداند. معمولاً یعنی حساب هنوز "
                "به سرور بروکر متصل نشده، یا نمادها در Market Watch مخفی‌اند. "
                "در ترمینال روی Market Watch راست‌کلیک کنید و «Show All» را بزنید.")
    if "auth" in low or "401" in low or "invalid" in low and "token" in low:
        return ("نام کاربری، رمز یا توکن پذیرفته نشد. اگر رمز را تازه عوض "
                "کرده‌اید، رمز جدید را دوباره اینجا وارد کنید.")
    return f"جزئیات فنی: {text}"


def _probe_account(broker: Any, conn: BrokerConnection,
                   checks: List[ProbeCheck], report: ProbeReport) -> None:
    try:
        account = broker.account()
    except Exception as exc:  # noqa: BLE001
        _check(checks, "account", "اطلاعات حساب خوانده نشد", False,
               f"وصل شدیم ولی مشخصات حساب برنگشت. جزئیات: {exc}", "block")
        return

    data = account.to_dict() if hasattr(account, "to_dict") else dict(account)
    report.account = {k: (str(v) if isinstance(v, Decimal) else v)
                      for k, v in data.items()}
    # NO MONEY IN THE CHECK TEXT. The checks are persisted with the connection
    # and read back by any authenticated session, including a viewer; the
    # balance belongs in the live response to the person who ran the test, not
    # in a file that is loaded on every page view and copied into every backup.
    _check(checks, "account", "اطلاعات حساب خوانده شد", True,
           f"ارز حساب {data.get('currency', '—')} · "
           f"{'با' if data.get('open_positions') else 'بدون'} معاملهٔ باز.",
           "block")

    # Does the account we reached match the one that was configured?
    reached = str(data.get("account_id", "") or "")
    match = same_account(conn.login, reached) if conn.login else None
    if conn.login and match is None:
        # The venue did not tell us which account we reached. That is NOT a
        # pass: reporting "the account number matches" when nothing was
        # compared is the reassuring answer, and this check exists precisely
        # for the case where the terminal is signed into something else.
        _check(checks, "account_match", "شمارهٔ حساب تأیید نشد", None,
               "بروکر شمارهٔ حسابی که به آن وصل شدیم را گزارش نکرد، پس نشد با "
               f"«{_mask_login(conn.login)}» مقایسه‌اش کرد. خودتان در ترمینال "
               "مطمئن شوید روی همان حساب هستید.", "warn")
    elif conn.login and not match:
        _check(checks, "account_match", "شمارهٔ حساب با آنچه وارد کردید فرق دارد",
               False,
               f"شماره‌ای که وارد کرده بودید «{_mask_login(conn.login)}» است ولی "
               f"اتصال به حساب «{_mask_login(reached)}» برقرار شد. این یعنی "
               "ترمینال به حساب دیگری وصل است. تا وقتی این دو یکی نشوند، معامله "
               "روی حسابی انجام می‌شود که فکرش را نمی‌کنید.", "block")
    elif conn.login:
        _check(checks, "account_match", "شمارهٔ حساب همان است که وارد کردید", True,
               f"حساب «{_mask_login(reached or conn.login)}».")

    # Declared vs observed account type. The dangerous direction is asymmetric:
    # "I said demo, it is live" is money at risk; the reverse is only annoying.
    observed = _observed_account_type(broker, data)
    if observed is None:
        _check(checks, "account_type", "واقعی یا تمرینی بودن حساب مشخص نشد", None,
               "این بروکر نوع حساب را گزارش نمی‌کند. خودتان در ترمینال یا پنل "
               "بروکر مطمئن شوید که روی کدام حساب هستید.", "warn")
    elif observed != conn.declared_account_type:
        blocking = observed == "live" and conn.declared_account_type == "demo"
        _check(checks, "account_type",
               "نوع حساب با آنچه اعلام کرده‌اید نمی‌خواند",
               False,
               ("شما این اتصال را «تمرینی» ثبت کرده‌اید ولی بروکر می‌گوید این "
                "حساب **واقعی** است. یعنی هر معامله با پول واقعی انجام می‌شود. "
                "تا این را درست نکنید اجازهٔ فعال‌سازی داده نمی‌شود."
                if blocking else
                "شما این اتصال را «واقعی» ثبت کرده‌اید ولی بروکر می‌گوید حساب "
                "تمرینی است. خطری ندارد، ولی سود و زیانش واقعی هم نیست."),
               "block" if blocking else "warn")
    else:
        _check(checks, "account_type",
               "نوع حساب همان است که اعلام کرده‌اید", True,
               "حساب واقعی است — با پول واقعی." if observed == "live"
               else "حساب تمرینی است — پولی در خطر نیست.")

    # Leverage against the profile's cap: a venue offering 500:1 to an account
    # sized for 30:1 will accept orders the risk engine assumed were impossible.
    # Compared against what the RISK ENGINE assumed, not against the venue's
    # own advertised ceiling. A profile declaring max_leverage=1000 made the
    # check unfailable for exactly the offshore brokers where it matters: an
    # account at 1:500 produced a cheerful green "within the expected range".
    try:
        lev = int(data.get("leverage") or 0)
    except (TypeError, ValueError):
        lev = 0
    if lev <= 0:
        _check(checks, "leverage", "اهرم حساب گزارش نشد", None,
               "این بروکر اهرم حساب را برنمی‌گرداند، پس نشد بررسی‌اش کرد. "
               "خودتان در پنل بروکر ببینید اهرم چند است — هرچه بالاتر باشد، "
               "یک اشتباه کوچک گران‌تر تمام می‌شود.", "warn")
    else:
        if lev > ASSUMED_MAX_LEVERAGE:
            _check(checks, "leverage", "اهرم این حساب از فرض سامانه بیشتر است",
                   False,
                   f"این حساب اهرم ۱:{lev} دارد؛ محاسبه‌های اندازهٔ معامله بر "
                   f"پایهٔ حداکثر ۱:{ASSUMED_MAX_LEVERAGE} نوشته شده‌اند. "
                   "سقف‌های ایمنی همچنان کار می‌کنند — ولی بروکر سفارش‌هایی را "
                   "می‌پذیرد که سامانه فرض کرده بود اصلاً ممکن نیستند، و حاشیهٔ "
                   "خطا بسیار کمتر می‌شود. اگر می‌توانید، اهرم را در پنل بروکر "
                   "پایین بیاورید.", "warn")
        else:
            _check(checks, "leverage", "اهرم حساب در محدودهٔ فرض‌شده است", True,
                   f"اهرم ۱:{lev}.")


def _observed_account_type(broker: Any, data: Dict[str, Any]) -> Optional[str]:
    """demo / live as the VENUE reports it, or None if it does not say."""
    # `trade_mode` is MT5's integer enum (0 demo, 1 contest, 2 real) and is the
    # ONLY key whose numbers mean that. Applying the same table to `is_live`
    # classified `is_live: 1` -- an adapter saying "yes, live" -- as a demo
    # account, which is the dangerous direction to be wrong in.
    if "trade_mode" in data:
        try:
            return {0: "demo", 1: "demo", 2: "live"}[int(data["trade_mode"])]
        except (TypeError, ValueError, KeyError):
            pass
    if "is_live" in data:
        value = data["is_live"]
        if isinstance(value, (bool, int, float)):
            return "live" if value else "demo"
        text = str(value).strip().lower()
        if text in ("true", "yes", "1", "live", "real"):
            return "live"
        if text in ("false", "no", "0", "demo", "practice", "paper"):
            return "demo"
    for key in ("account_type", "environment"):
        if key not in data:
            continue
        text = str(data[key]).strip().lower()
        if text in ("live", "real"):
            return "live"
        if text in ("demo", "practice", "contest", "paper", "sandbox"):
            return "demo"
    name = str(getattr(getattr(broker, "capabilities", None), "name", "")).lower()
    if "paper" in name or "sim" in name:
        return "demo"
    if "practice" in name:
        return "demo"
    return None


def _probe_symbols(broker: Any, conn: BrokerConnection,
                   checks: List[ProbeCheck], report: ProbeReport,
                   wanted: Optional[List[str]]) -> None:
    try:
        instruments = broker.instruments()
    except Exception as exc:  # noqa: BLE001
        _check(checks, "symbols", "فهرست نمادها خوانده نشد", False,
               f"بدون فهرست نمادها هیچ سفارشی قابل ارسال نیست. جزئیات: {exc}",
               "block")
        return

    report.symbols_total = len(instruments)
    if not instruments:
        _check(checks, "symbols", "بروکر هیچ نمادی برنگرداند", False,
               "در ترمینال، Market Watch را باز کنید و «Show All» را بزنید، "
               "بعد دوباره آزمایش کنید.", "block")
        return

    profile = resolve_profile(conn.profile)
    examples: Dict[str, str] = {}
    for canonical in list(instruments)[:6]:
        venue = (profile.symbols.to_venue(canonical) if profile else canonical)
        examples[canonical] = venue
    report.symbol_examples = examples
    sample = "، ".join(f"{k} → {v}" for k, v in list(examples.items())[:3])
    _check(checks, "symbols", "فهرست نمادها خوانده شد", True,
           f"{len(instruments)} نماد پیدا شد. نمونهٔ نام‌گذاری این بروکر: {sample}")

    mismatches = list(getattr(broker, "profile_mismatches", []) or [])
    report.mismatches = [str(m) for m in mismatches]
    if mismatches:
        _check(checks, "profile_match",
               "چند مشخصه با پروفایل فرق داشت (مقدار خودِ بروکر پذیرفته شد)",
               True,
               f"{len(mismatches)} مورد اختلاف پیدا شد و در همه، عدد خود بروکر "
               "ملاک قرار گرفت — پروفایل فقط یک حدس اولیه است. جزئیات در گزارش "
               "پایین صفحه.", "info")
    else:
        _check(checks, "profile_match", "مشخصات بروکر با پروفایل می‌خواند", True,
               "هیچ اختلافی بین آنچه پروفایل فرض کرده و آنچه بروکر گزارش می‌کند "
               "پیدا نشد.")

    # A live quote, and a sanity check on the spread. A spread of zero is a
    # stale or synthetic feed; an enormous one is a closed market or a symbol
    # the account cannot trade.
    # Symbols the operator CONFIGURED that this venue does not offer. Dropping
    # them silently meant the probe went green on an arbitrary fallback symbol
    # the agent will never trade, while every real order was rejected at run
    # time with a message about the symbol rather than about the venue.
    requested = list(wanted or [])
    absent = [s for s in requested if s not in instruments]
    if absent:
        _check(checks, "instruments_missing",
               "بعضی از نمادهایی که تنظیم کرده‌اید در این بروکر نیستند", False,
               "این نمادها پیدا نشدند: " + "، ".join(absent) + ". یا نام‌شان در "
               "این بروکر فرق دارد، یا برای این نوع حساب فعال نیستند. سفارش روی "
               "این نمادها رد می‌شود.", "block")
    elif requested:
        _check(checks, "instruments_missing",
               "همهٔ نمادهای تنظیم‌شده در این بروکر موجودند", True,
               "، ".join(requested[:8]) + ("…" if len(requested) > 8 else ""))

    targets = [s for s in requested if s in instruments] or list(instruments)[:1]
    # The simulator IS the venue: it has no upstream price of its own and is
    # fed by whatever data source the agent is configured with. Reporting "no
    # live price" as a blocking failure there would be true of the adapter and
    # false about the system, and would make the one venue where mistakes are
    # free the only one that cannot be activated.
    simulator = conn.adapter == "paper"
    for symbol in targets[:3]:
        try:
            quote = broker.quote(symbol)
        except Exception as exc:  # noqa: BLE001
            if simulator:
                _check(checks, f"quote:{symbol}",
                       "شبیه‌ساز قیمت زنده ندارد — و نباید داشته باشد", None,
                       "شبیه‌ساز خودش نقش بروکر را بازی می‌کند و قیمت را از "
                       "همان فید داده‌ای می‌گیرد که ربات استفاده می‌کند. این "
                       "یک ایراد نیست.")
            else:
                _check(checks, f"quote:{symbol}", f"قیمت زندهٔ {symbol} دریافت نشد",
                       False, f"جزئیات: {exc}", "block")
            continue
        inst = instruments[symbol]
        spread = (dec(quote.ask) - dec(quote.bid))
        spread_pips = (spread / inst.pip) if inst.pip > 0 else D("0")
        if spread <= 0:
            _check(checks, f"quote:{symbol}", f"قیمت {symbol} معتبر نیست", False,
                   f"فاصلهٔ خرید و فروش {spread} است. قیمت یا قدیمی است یا بازار "
                   "بسته است.", "block")
        elif spread_pips > _spread_ceiling(profile, symbol):
            # Measured against the PROFILE's typical spread for this symbol,
            # not a flat pip count. With MT5's pip = 10^-(digits-1), a 2-digit
            # index has pip = 0.01, so an ordinary 2-point US30 spread reads as
            # 200 "pips" and a healthy feed was reported as unusable.
            _check(checks, f"quote:{symbol}", f"فاصلهٔ خرید و فروش {symbol} خیلی زیاد است",
                   False,
                   f"{spread_pips:.1f} پیپ، در حالی که برای این نماد حدود "
                   f"{profile.spread_for(symbol) if profile else D('1.2')} پیپ "
                   "انتظار می‌رفت. یا بازار بسته است، یا این نماد برای این حساب "
                   "قابل معامله نیست. با این هزینه هیچ استراتژی‌ای سود نمی‌دهد.",
                   "warn")
        else:
            _check(checks, f"quote:{symbol}", f"قیمت زندهٔ {symbol} دریافت شد", True,
                   f"خرید {quote.ask} / فروش {quote.bid} — فاصله {spread_pips:.1f} پیپ.")

    # Currency conversion for every instrument whose quote currency is not the
    # account currency. A missing rate is the 166x sizing error.
    account_ccy = (conn.account_currency
                   or str(report.account.get("currency", "")) or "USD")
    # Every CONFIGURED instrument, not a slice of the fallback list. The old
    # version checked one symbol and then reported that conversion was
    # available for "all" of them -- the check whose entire purpose is stopping
    # a 166x sizing error on a JPY-quoted pair.
    covered = [s for s in requested if s in instruments] or targets
    missing: List[str] = []
    seen_pairs: set = set()
    for symbol in covered:
        inst = instruments[symbol]
        if inst.quote == account_ccy or inst.quote in seen_pairs:
            continue
        seen_pairs.add(inst.quote)
        try:
            broker.conversion_rate(inst.quote, account_ccy)
        except Exception:  # noqa: BLE001
            missing.append(f"{inst.quote}→{account_ccy}")
    if missing and simulator:
        _check(checks, "conversion", "نرخ تبدیل ارز هنوز بارگذاری نشده است", None,
               "در شبیه‌ساز، نرخ‌های تبدیل هم از فید داده می‌آیند و موقع اجرا "
               "پر می‌شوند. اینجا هنوز چیزی بارگذاری نشده: "
               + "، ".join(sorted(set(missing))))
    elif missing:
        _check(checks, "conversion", "نرخ تبدیل ارز برای بعضی نمادها پیدا نشد",
               False,
               "برای این تبدیل‌ها نرخی پیدا نشد: " + "، ".join(sorted(set(missing)))
               + ". سامانه در این حالت معامله نمی‌کند و این درست است: بدون نرخ "
               "تبدیل، اندازهٔ معامله می‌تواند ده‌ها برابر چیزی باشد که خواسته‌اید.",
               "block")
    else:
        _check(checks, "conversion",
               f"نرخ تبدیل ارز برای هر {len(covered)} نماد بررسی‌شده موجود است",
               True, f"ارز حساب: {account_ccy}.")


def _probe_capabilities(broker: Any, conn: BrokerConnection,
                        checks: List[ProbeCheck], report: ProbeReport) -> None:
    caps = getattr(broker, "capabilities", None)
    if caps is None:
        return
    report.degradations = list(caps.degradation_report())

    if caps.supports_server_side_stop:
        _check(checks, "server_stop", "حد ضرر نزد خود بروکر ثبت می‌شود", True,
               "اگر برق یا اینترنت این سرور قطع شود، حد ضرر همچنان سر جایش است "
               "و از معامله محافظت می‌کند.", "block")
    else:
        _check(checks, "server_stop", "این بروکر حد ضرر سمت سرور ندارد", False,
               "یعنی حد ضرر فقط داخل همین برنامه زندگی می‌کند. اگر ارتباط قطع "
               "شود، معاملهٔ باز بدون محافظ می‌ماند. این بروکر را بدون نظارت "
               "رها نکنید.", "block")

    if caps.supports_client_order_id:
        _check(checks, "order_id", "سفارش‌های تکراری توسط بروکر رد می‌شوند", True,
               "هر سفارش یک شناسهٔ یکتا دارد که خود بروکر بررسی می‌کند، پس یک "
               "پاسخ گم‌شده هرگز به معاملهٔ دوتایی تبدیل نمی‌شود.")
    else:
        _check(checks, "order_id", "بروکر شناسهٔ سفارش سمت مشتری را نمی‌پذیرد",
               None,
               "متاتریدر این امکان را ندارد. سامانه به‌جایش قبل از هر ارسال "
               "دوباره، وضعیت را از بروکر می‌پرسد و یک بازهٔ سکوت نگه می‌دارد. "
               "این ضعیف‌تر است ولی شناخته‌شده و مدیریت‌شده است.", "warn")

    if getattr(broker, "supports_closed_trade_history", False):
        _check(checks, "history", "تاریخچهٔ معامله‌های بسته‌شده در دسترس است", True,
               "بدون این، ربات نمی‌تواند از معامله‌های گذشتهٔ خودش درس بگیرد.")
    else:
        _check(checks, "history", "تاریخچهٔ معامله‌ها از بروکر خوانده نمی‌شود",
               False,
               "بخش «یادگیری از اشتباه» بدون تاریخچه چیزی برای تحلیل ندارد و "
               "خاموش می‌ماند. آمار عملکرد هم خالی می‌ماند.", "warn")


# --------------------------------------------------------------------------- #
# the activation gate
# --------------------------------------------------------------------------- #


def activation_blockers(conn: BrokerConnection, *,
                        open_positions: int,
                        current_broker_name: str = "",
                        licence_allows_live: Optional[bool] = None,
                        licence_reason: str = "",
                        accepted_strategies: int = 0,
                        max_probe_age_sec: float = 86400.0,
                        now_ns: Optional[int] = None) -> List[str]:
    """Reasons this connection must not become the active venue, in plain words.

    An empty list means the switch is safe to make. Every entry is phrased for
    someone who will read it once, under time pressure, possibly at 2am.
    """
    reasons: List[str] = []
    now = now_ns if now_ns is not None else wall_ns()

    # 1. Positions belong to the venue that holds them.
    if open_positions > 0 and conn.id != current_broker_name:
        reasons.append(
            f"همین حالا {open_positions} معاملهٔ باز روی بروکر فعلی وجود دارد. "
            "عوض کردن بروکر یعنی این معامله‌ها پشت سر سامانه رها می‌شوند: "
            "بروکر جدید آن‌ها را نمی‌شناسد و سامانه دیگر آن‌ها را نمی‌بیند. "
            "اول همه را ببندید، بعد بروکر را عوض کنید.")

    # 2. A probe is evidence, and evidence goes stale.
    probe_data = conn.last_probe or {}
    if not probe_data:
        reasons.append(
            "این اتصال هنوز یک بار هم آزمایش نشده است. دکمهٔ «آزمایش اتصال» را "
            "بزنید — هیچ سفارشی فرستاده نمی‌شود، فقط خوانده می‌شود.")
    else:
        if not probe_data.get("ok"):
            failed = probe_data.get("blocking_failures") or []
            reasons.append(
                "آخرین آزمایش این اتصال ناموفق بود"
                + (f" ({len(failed)} ایراد جدی)" if failed else "")
                + ". تا وقتی آزمایش سبز نشود، فعال‌سازی معنی ندارد.")
        finished_ns = int(probe_data.get("finished_ns") or 0)
        age_sec = (now - finished_ns) / 1e9
        if age_sec < -3600:
            # Clamping this to zero made a probe recorded while the clock was
            # ahead stay "fresh" for as long as the skew lasted.
            reasons.append(
                "زمان آخرین آزمایش جلوتر از ساعت فعلی سرور است. یا ساعت سرور "
                "عوض شده یا فایل دست‌کاری شده. یک آزمایش تازه بگیرید.")
        elif age_sec > max_probe_age_sec:
            reasons.append(
                f"آخرین آزمایش {int(age_sec / 3600)} ساعت پیش انجام شده است. "
                "مشخصات بروکر — مثل حداقل فاصلهٔ حد ضرر — بین جلسات معاملاتی "
                "عوض می‌شود. یک آزمایش تازه بگیرید.")
        # The probe must have been run against THIS account, not a sibling.
        probed_login = str(probe_data.get("account_id") or "")
        if conn.login and same_account(conn.login, probed_login) is False:
            reasons.append(
                "آزمایشی که ذخیره شده مربوط به حساب دیگری است. یک آزمایش تازه "
                "روی همین حساب لازم است.")

    # 3. Live money needs more than a working connection.
    if conn.declared_account_type == "live":
        # `is not True`, not `is False`. None means "we could not determine
        # it" -- which the caller produces whenever the licence gate is absent
        # or raised -- and treating that as permission opened a live venue
        # with no licence check at all.
        if licence_allows_live is not True:
            reasons.append(
                "لایسنس فعلی اجازهٔ معاملهٔ واقعی نمی‌دهد"
                if licence_allows_live is False else
                "وضعیت لایسنس خوانده نشد، پس اجازهٔ معاملهٔ واقعی داده نمی‌شود"
                + (f" — {licence_reason}" if licence_reason else "") + ".")
        if accepted_strategies <= 0:
            reasons.append(
                "هیچ استراتژی‌ای از آزمون‌های پذیرش رد نشده است. سامانه با پول "
                "واقعی معامله نمی‌کند مگر دست‌کم یک استراتژی حکم «پذیرفته شد» "
                "گرفته باشد. این عمدی است.")
    return reasons
