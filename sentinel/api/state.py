"""Runtime container: the agent, its background loop, and the read models the
dashboard consumes.

The API process never mutates trading state directly. It calls methods on this
object, which owns the lock, writes to the audit chain, and keeps the snapshot
that the dashboard renders. That keeps one writer for the trading state even
though several HTTP workers may be reading it.

For a deployment that separates the two processes (recommended, and what the
systemd units do), the dashboard runs as a different system user with no
credentials for the venue; it reaches the engine over a local socket, and the
engine is the only process holding the API token.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..agent.orchestrator import Agent, CycleReport
from ..core.audit import EventType
from ..core.clock import wall_ns
from ..core.config import AgentMode, ExecutionVenueMode, SentinelConfig, diff_configs
from ..core.money import ZERO, dec
from ..core.types import OrderState
from ..research.backtest import _periods_per_year_from_index
from ..research.metrics import compute_performance
from ..risk.engine import RiskEngine


def _profile_card(profile) -> Dict[str, Any]:
    """A broker profile reduced to what a non-specialist needs to choose one."""
    return {
        "name": profile.name,
        "display_name": profile.display_name,
        "adapter": profile.adapter,
        "regulator": profile.regulator,
        "max_leverage": profile.max_leverage,
        "min_stop_level_points": profile.min_stop_level_points,
        "commission_per_lot_round_turn": str(profile.commission_per_lot_round_turn),
        "default_spread_pips": str(profile.default_spread_pips),
        "supports_server_side_stop": profile.supports_server_side_stop,
        "supports_hedging": profile.supports_hedging,
        "segregated_client_funds": profile.segregated_client_funds,
        "negative_balance_protection": profile.negative_balance_protection,
        "symbol_suffix": profile.symbols.suffix,
        "notes": profile.notes,
        "verify_before_live": list(profile.verify_before_live),
    }


@dataclass
class EquityPoint:
    ts_ns: int
    equity: float
    balance: float
    drawdown_pct: float
    open_positions: int


class Runtime:
    def __init__(self, agent: Agent, config_path: str | Path = "var/config.json",
                 verdicts=None, licence=None) -> None:
        self.agent = agent
        # May be None in tests and in self-hosted builds; every use is guarded.
        self.licence = licence
        self.config_path = Path(config_path)
        if verdicts is None:
            from ..research.verdicts import VerdictStore
            verdicts = VerdictStore(Path(agent.config.ops.state_dir) / "verdicts.db")
        self.verdicts = verdicts
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_cycle: Optional[CycleReport] = None
        self.equity_curve: List[EquityPoint] = []
        self.cycle_history: List[dict] = []
        self.started_ns: int = 0
        self.subscribers: List[Callable[[dict], None]] = []
        self.errors: List[str] = []
        # Accumulated realised history for venues that stream it incrementally.
        self._realised: List = []
        # Venue configuration. Built lazily so that a runtime constructed in a
        # test never touches the filesystem for a feature it does not use.
        self._connections = None
        self._secrets = None
        self._secrets_error = ""
        # Optional assistants, wired by bootstrap. Each runs on the background
        # worker, never on the decision thread, and each can only inform a
        # human or shrink risk.
        self.ai = None           # sentinel.ai.AIService
        self.news_desk = None    # sentinel.news.desk.NewsDesk
        self.coach = None        # sentinel.ai.coach.TradeCoach
        self.reference = None    # sentinel.data.reference.ReferenceDesk
        self.brain = None        # sentinel.brain.Brain
        self.notifier = None     # sentinel.notify.Notifier
        self.macro = None        # sentinel.data.macro.MacroDesk
        # One row per Tehran day on disk (ops/equity_ledger). Built on first
        # use for the same reason as the connection store above.
        self._equity_ledger = None
        # The account type the venue reported at the last cycle, for pages
        # that must not make a venue round trip of their own.
        self._last_account_type = ""
        self._bg_thread: Optional[threading.Thread] = None
        self.background_errors: List[str] = []
        self.background_last_ns: int = 0

    # -- venue configuration -------------------------------------------------- #

    @property
    def state_dir(self) -> Path:
        return Path(self.agent.config.ops.state_dir)

    @property
    def connections(self):
        from ..brokers.connection import ConnectionStore
        if self._connections is None:
            self._connections = ConnectionStore(self.state_dir / "brokers.json")
        return self._connections

    @property
    def secrets(self):
        """The credential store, or None with a reason in ``_secrets_error``.

        A missing credential key is not fatal: every venue that reads its
        credentials from the environment keeps working, and the dashboard shows
        why the "save a password" field is unavailable rather than failing at
        the moment somebody tries to use it.
        """
        from ..brokers.secrets import SecretStore, SecretStoreError
        if self._secrets is None and not self._secrets_error:
            try:
                self._secrets = SecretStore(self.state_dir / "broker-secrets.json")
            except SecretStoreError as exc:
                self._secrets_error = str(exc)
        return self._secrets

    def broker_overview(self, *, include_paths: bool = False) -> Dict[str, Any]:
        """Everything the venue settings page needs, with no secret in it.

        ``include_paths`` adds the credential key's location and the reason a
        credential store could not be opened. Both are filesystem paths on the
        server, which is reconnaissance rather than a secret -- so an owner
        sees them and a viewer is told the protection LEVEL without the map.
        """
        from ..brokers.profiles import list_profiles

        cfg = self.agent.config
        conns = self.connections.list()
        active_profile = cfg.execution.broker
        try:
            caps = self.agent.broker.capabilities
            live_name, degradations = caps.name, list(caps.degradation_report())
        except Exception:  # noqa: BLE001
            live_name, degradations = active_profile, []

        # The count RECORDED BY THE LAST CYCLE, not a fresh venue call and not
        # a full position serialisation. This endpoint is a page load, and
        # reaching the broker from it put a blocking network round trip on the
        # read path of a page any viewer can open 120 times a minute.
        open_positions = (self.equity_curve[-1].open_positions
                          if self.equity_curve else 0)

        secrets = self.secrets
        venue = self.effective_venue()
        return {
            "active_profile": active_profile,
            "active_adapter": live_name,
            "venue_mode": cfg.execution.venue_mode.value
                          if hasattr(cfg.execution.venue_mode, "value")
                          else str(cfg.execution.venue_mode),
            "venue_effective": venue["venue"],
            "venue_source": venue["source"],
            "degradations": degradations,
            "open_positions": open_positions,
            "connections": [c.redacted() for c in conns],
            "damaged": self.connections.damaged_ids(),
            "profiles": [_profile_card(p) for p in list_profiles()],
            "credential_storage": self._credential_note(secrets, include_paths),
            "restart_required": any(
                c.enabled and c.profile != active_profile for c in conns),
        }

    def _credential_note(self, secrets, include_paths: bool) -> Dict[str, Any]:
        if secrets is None:
            note = {"level": "unavailable", "key_source": None, "key_path": None,
                    "note": ("ذخیرهٔ رمز بروکر در دسترس نیست چون کلید رمزنگاری "
                             "پیدا نشد.")}
            if include_paths and self._secrets_error:
                note["note"] += f" جزئیات: {self._secrets_error}"
            return note
        note = dict(secrets.protection_note())
        if not include_paths:
            note.pop("key_path", None)
            note["key_path"] = None
        return note

    def save_connection(self, payload: Dict[str, Any], by: str,
                        secret: Optional[str] = None) -> Dict[str, Any]:
        from ..brokers.connection import BrokerConnection, ConnectionError_

        known = set(BrokerConnection.__dataclass_fields__)
        existing = self.connections.get(str(payload.get("id", "")))
        data = {k: v for k, v in payload.items() if k in known}
        if existing is not None:
            # Preserve what the form does not carry: the probe result belongs to
            # the connection, not to the edit, and blanking it on every save
            # would quietly re-open the activation gate.
            merged = existing.to_storage()
            merged.update(data)
            # A parameter that changes WHICH account is reached invalidates the
            # evidence. Editing the display name does not.
            identity = ("profile", "server", "login", "terminal_path",
                        "exchange_id", "declared_account_type")
            if any(str(merged.get(k)) != str(existing.to_storage().get(k))
                   for k in identity):
                merged["last_probe"] = None
                # Losing the evidence must lose the activation with it.
                # Clearing only the probe let an owner re-point an ALREADY
                # ENABLED connection at a different account -- different login,
                # different server, demo->live -- and the next restart signed
                # into it with no fresh test, no demo/live re-check and no
                # open-position blocker, because activation_blockers() only
                # runs on the activate path.
                merged["enabled"] = False
            data = merged
        try:
            conn = BrokerConnection(**data)
        except ConnectionError_ as exc:
            raise ValueError(str(exc)) from exc

        if secret is not None:
            store = self.secrets
            if store is None:
                raise ValueError(
                    "رمز بروکر ذخیره نشد چون کلید رمزنگاری در دسترس نیست: "
                    + (self._secrets_error or "دلیل نامعلوم"))
            ref = f"broker:{conn.id}"
            if secret == "":
                store.delete(ref)
                conn.secret_ref = ""
            else:
                store.put(ref, secret)
                conn.secret_ref = ref
        self.connections.upsert(conn)
        self.agent.audit.append(
            EventType.CONFIG_CHANGE,
            {"action": "broker_connection_saved", "connection": conn.id,
             "profile": conn.profile, "account_type": conn.declared_account_type,
             "credential_changed": secret is not None}, actor=by)
        return conn.redacted()

    def delete_connection(self, connection_id: str, by: str) -> Dict[str, Any]:
        conn = self.connections.get(connection_id)
        if conn is None:
            raise KeyError(connection_id)
        if conn.profile == self.agent.config.execution.broker and conn.enabled:
            raise ValueError(
                "این اتصال همان بروکری است که همین حالا فعال است. اول یک بروکر "
                "دیگر را فعال کنید، بعد این را پاک کنید.")
        store = self.secrets
        if store is not None and conn.secret_ref:
            store.delete(conn.secret_ref)
        self.connections.delete(connection_id)
        self.agent.audit.append(EventType.CONFIG_CHANGE,
                                {"action": "broker_connection_deleted",
                                 "connection": connection_id}, actor=by)
        return {"deleted": connection_id}

    #: Adapters whose session is a PROCESS-WIDE singleton. The MetaTrader5
    #: Python package is one module object per process: initialize() with
    #: credentials makes the terminal switch accounts, and shutdown() ends the
    #: session for everyone. So constructing a second MT5 adapter to "test" a
    #: connection re-points the live engine at the probed account and then
    #: disconnects it -- the exact order-path reach ReadOnlyBroker exists to
    #: prevent, reached one layer below where that guard sits.
    _PROCESS_GLOBAL_ADAPTERS = {"mt5", "mt4"}

    def test_connection(self, connection_id: str, by: str) -> Dict[str, Any]:
        from ..brokers.connection import probe

        conn = self.connections.get(connection_id)
        if conn is None:
            raise KeyError(connection_id)

        live_adapter = ""
        try:
            live_adapter = str(getattr(self.agent.broker, "_mt5", None) and "mt5"
                               or type(self.agent.broker).__name__.lower())
        except Exception:  # noqa: BLE001
            live_adapter = ""
        if (conn.adapter in self._PROCESS_GLOBAL_ADAPTERS
                and "mt5" in live_adapter):
            raise ValueError(
                "این سرویس همین حالا به یک ترمینال متاتریدر وصل است. آزمایش یک "
                "اتصال متاتریدر دیگر، همان ترمینال را به حساب دیگری منتقل می‌کند "
                "و بعد قطعش می‌کند — یعنی معامله‌های باز روی حسابی می‌مانند که "
                "سامانه دیگر نمی‌بیند. اول ربات را متوقف و سرویس را خاموش کنید، "
                "بعد آزمایش بگیرید."
            )
        store = self.secrets
        secret = None
        if conn.secret_ref and store is not None:
            try:
                secret = store.get(conn.secret_ref)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"رمز ذخیره‌شده خوانده نشد: {exc}") from exc
        report = probe(conn, secret=secret,
                       instruments=list(self.agent.config.execution.instruments)
                       if hasattr(self.agent.config.execution, "instruments") else None)
        self.connections.set_probe(conn.id, report)
        self.agent.audit.append(
            EventType.CONFIG_CHANGE,
            {"action": "broker_connection_tested", "connection": conn.id,
             "ok": report.ok, "blocking": [c.id for c in report.blocking_failures]},
            actor=by)
        return report.to_dict()

    def activate_connection(self, connection_id: str, by: str) -> Dict[str, Any]:
        """Make this connection the venue the agent will use.

        Does NOT hot-swap the adapter. Replacing a live broker under a running
        decision loop means the reconciler, the open positions, the idempotency
        blackout window and the symbol table all change identity between one
        cycle and the next, and there is no ordering of those four that is
        safe. The change is recorded and applied on the next start, which the
        dashboard says in as many words.
        """
        from ..brokers.connection import activation_blockers

        conn = self.connections.get(connection_id)
        if conn is None:
            raise KeyError(connection_id)

        licence_allows, licence_reason = (None, "")
        if self.licence is not None:
            try:
                licence_allows, licence_reason = self.licence.may_trade_live()
            except Exception:  # noqa: BLE001
                licence_allows, licence_reason = None, ""
        accepted = 0
        try:
            accepted = sum(1 for v in self.verdicts.list(None)
                           if str(v.get("verdict", "")).lower() == "accepted")
        except Exception:  # noqa: BLE001
            accepted = 0

        # The CONNECTION that is currently enabled, not the profile name.
        # `execution.broker` holds a profile ("generic_mt5"); the blocker
        # compares it against `conn.id`. Two namespaces: the open-positions
        # exemption never fired for a genuine re-activation, and a second
        # connection named after the profile skipped the guard entirely.
        active_id = next((c.id for c in self.connections.list() if c.enabled), "")
        blockers = activation_blockers(
            conn,
            open_positions=len(self.agent.broker.positions() or []),
            current_broker_name=active_id,
            licence_allows_live=licence_allows,
            licence_reason=licence_reason,
            accepted_strategies=accepted)
        if blockers:
            self.agent.audit.append(
                EventType.WRITE_DENIED,
                {"action": "broker_activate", "connection": conn.id,
                 "blockers": blockers}, actor=by)
            raise ValueError(" | ".join(blockers))

        # Route the actual change through update_config so that it passes every
        # guard that protects the configuration -- verdict authority,
        # validation, the journal entry and the version bump. `execution.broker`
        # is a PRIVILEGED field, which is correct for a blind config write and
        # wrong here: this path has already run every blocker the privileged
        # guard exists to stand in for. Without the exemption the endpoint
        # could never succeed at all -- it returned 409 for every real switch.
        patch, allow = self._activation_patch(conn)
        result = self.update_config(patch, by, allow_privileged=allow)

        # ONE read-modify-write. Flipping records individually let two
        # concurrent activations each turn off only what their own stale
        # snapshot had seen, leaving two enabled -- which bootstrap then
        # refuses to start with, until someone hand-edits the file.
        self.connections.set_exclusive_enabled(conn.id)
        self.agent.audit.append(
            EventType.CONFIG_CHANGE,
            {"action": "broker_connection_activated", "connection": conn.id,
             "profile": conn.profile}, actor=by)
        note = ("تنظیم ذخیره شد. برای اینکه ربات واقعاً به این بروکر "
                "وصل شود باید سرویس یک بار راه‌اندازی دوباره شود — "
                "عوض کردن بروکر وسط کار، معامله‌های باز را از دست "
                "سامانه خارج می‌کند.")
        if conn.declared_account_type == "live" and \
                self.agent.config.execution.venue_mode is not ExecutionVenueMode.LIVE:
            note += (" این حساب «واقعی» اعلام شده ولی تنظیمات ربات روی حالت واقعی نیست؛ "
                     "تا وقتی حالت واقعی آگاهانه و جداگانه روشن نشود، ربات به این حساب "
                     "هیچ سفارشی نمی‌فرستد.")
        return {**result, "connection": conn.id, "restart_required": True, "note": note}

    def _activation_patch(self, conn) -> tuple:
        """The configuration an activation writes, and the privileged fields it
        may touch -- all of it applied at the next start.

        Besides the broker profile, activation now binds the engine to the
        account the probe just verified (number, server, currency), and sets
        the venue mode for a demo or simulator connection. Before 1.8.2 only
        the profile changed. The console kept calling an activated Alpari demo
        «تمرینی — شبیه‌ساز», and the engine traded whichever account the
        terminal was signed into.

        The venue mode NEVER moves to live on this path. A connection declared
        live leaves the mode where it is, and the account binding (a
        non-live mode refuses a real-money account) keeps it from trading until
        live mode is switched on deliberately, where its own gates are.
        """
        execution: Dict[str, Any] = {"broker": conn.profile}
        live_now = self.agent.config.execution.venue_mode is ExecutionVenueMode.LIVE
        if conn.adapter == "paper":
            if not live_now:
                execution["venue_mode"] = ExecutionVenueMode.PAPER.value
            execution["expected_account_id"] = ""
            execution["expected_account_server"] = ""
        else:
            if conn.declared_account_type == "demo":
                # Towards safety only: a demo binding refuses a live account.
                execution["venue_mode"] = ExecutionVenueMode.DEMO.value
            if conn.login:
                from ..brokers.connection import normalise_account_id
                execution["expected_account_id"] = (
                    normalise_account_id(conn.login) if conn.adapter == "mt5"
                    else conn.login.strip())
                execution["expected_account_server"] = (
                    conn.server.strip() if conn.adapter == "mt5" else "")
            # The currency the probe observed, else the one typed in: the
            # binding compares it on every call, and a cent account ("USC")
            # bound as USD would refuse to start.
            observed = str((conn.last_probe or {}).get("account_currency") or "")
            currency = (observed or conn.account_currency or "").strip().upper()
            if 3 <= len(currency) <= 5:
                execution["account_currency"] = currency
        allow = {("execution", key) for key in execution}
        return {"execution": execution}, allow

    def effective_venue(self, account=None) -> Dict[str, str]:
        """What the running engine is actually connected to.

        ``venue_mode`` is what the configuration SAYS, and it changes the
        moment an activation is saved -- before the restart that applies it.
        The console's account-type label follows this instead: the simulator
        when the simulator is running, otherwise the account type the venue
        itself reports, and the configured mode only when the venue does not
        say.
        """
        configured = self.agent.config.execution.venue_mode.value
        inner = getattr(self.agent.broker, "inner", self.agent.broker)
        from ..brokers.paper import PaperBroker
        if isinstance(inner, PaperBroker):
            return {"venue": "paper", "source": "simulator", "configured": configured}
        reported = str(getattr(account, "account_type", "") or "") if account is not None \
            else self._last_account_type
        if reported in ("demo", "live"):
            return {"venue": reported, "source": "account", "configured": configured}
        return {"venue": configured, "source": "config", "configured": configured}

    # -- lifecycle ----------------------------------------------------------- #

    def start(self, run_loop: bool = True) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.started_ns = wall_ns()
            # Defence in depth: whatever constructed this runtime, the trading
            # authority claimed by the configuration is re-checked here against
            # the verdict registry before the first cycle runs.
            from ..research.verdicts import RegistryUnreadable, enforce_config_authority

            try:
                checked, violations, repaired = enforce_config_authority(
                    self.agent.config, self.verdicts)
            except RegistryUnreadable as exc:
                self.agent.audit.append(EventType.CONFIG_CHANGE,
                                        {"startup_refused": str(exc)}, actor="runtime")
                raise
            if violations:
                self.agent.config = checked
                self.agent.risk = RiskEngine(checked.risk)
                for v in violations:
                    self.agent.audit.append(EventType.CONFIG_CHANGE,
                                            {"startup_authority_violation": v},
                                            actor="runtime")
                    self.errors.append(f"authority refused: {v}")
                if repaired:
                    checked.save(self.config_path)
            self.agent.start()
            self._stop.clear()
            if not run_loop:
                return

            def loop() -> None:
                interval = self.agent.config.agent.decision_interval_sec
                while not self._stop.wait(interval):
                    try:
                        self.run_cycle()
                    except Exception as exc:  # noqa: BLE001 - the loop must survive
                        msg = f"{type(exc).__name__}: {exc}"
                        self.errors.append(msg)
                        self.errors = self.errors[-50:]
                        self.agent.audit.append(EventType.SYSTEM_START,
                                                {"cycle_error": msg})

            self._thread = threading.Thread(target=loop, name="agent-loop", daemon=True)
            self._thread.start()

            if (self.news_desk is not None or self.coach is not None
                    or self.reference is not None or self.brain is not None
                    or self.notifier is not None or self.macro is not None):
                def background() -> None:
                    # First pass soon after start, then once a minute; each
                    # assistant decides for itself whether it is due.
                    delay = 5.0
                    while not self._stop.wait(delay):
                        delay = 60.0
                        self.background_tick()

                self._bg_thread = threading.Thread(target=background,
                                                   name="assistants", daemon=True)
                self._bg_thread.start()

    def background_tick(self) -> None:
        """One pass of the news desk and the coach. Never takes the trading lock."""
        self.background_last_ns = wall_ns()
        for name, step in (("news", getattr(self.news_desk, "tick", None)),
                           ("coach", getattr(self.coach, "tick", None)),
                           ("reference", getattr(self.reference, "tick", None)),
                           ("brain", getattr(self.brain, "tick", None)),
                           ("notify", getattr(self.notifier, "tick", None)),
                           ("macro", getattr(self.macro, "tick", None))):
            if step is None:
                continue
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - an assistant must never stop anything
                msg = f"{name}: {type(exc).__name__}: {exc}"[:300]
                self.background_errors = (self.background_errors + [msg])[-20:]

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self.reference is not None:
            self.reference.stop()
        if self.notifier is not None:
            self.notifier.stop()
        self.agent.stop()

    def reference_instruments(self) -> List[str]:
        """What the reference stream follows: every instrument an enabled
        strategy trades, every instrument with an open position, and every
        instrument the owner mapped by hand (so a manual ticket is checked too)."""
        out: List[str] = []
        try:
            out.extend(self.agent._active_instruments())
        except Exception:  # noqa: BLE001
            pass
        # The agent's OWN book, not broker.positions(): this runs on the
        # background worker, and a venue client (the MetaTrader5 package in
        # particular) must only ever be driven from the decision thread.
        try:
            out.extend(list(getattr(self.agent, "_position_meta", {}) or {}))
        except Exception:  # noqa: BLE001 - a dict resized mid-copy; next tick
            pass
        out.extend(self.agent.config.reference.symbol_map)
        seen: List[str] = []
        for inst in out:
            if inst not in seen:
                seen.append(inst)
        return seen[:40]

    def reference_view(self) -> Dict[str, Any]:
        if self.reference is None:
            return {"enabled": False, "available": False,
                    "reason": "the reference service is not wired in this build"}
        view = self.reference.view()
        view["available"] = True
        return view

    def run_cycle(self) -> CycleReport:
        with self._lock:
            before = set(self.agent.processed_trades)
            report = self.agent.cycle()
            self.last_cycle = report
            # The agent drains the venue's history with a cursor; mirror whatever
            # it just consumed so the blotter and the statistics are not empty.
            new_ids = set(self.agent.processed_trades) - before
            if new_ids and not isinstance(
                    getattr(self.agent.broker, "closed_trades", None), list):
                rows = self.agent.memory.autopsies(limit=len(new_ids) * 2)
                self._realised.extend(
                    r for r in rows if r.get("trade_id") in new_ids)
            try:
                acct = self.agent.broker.account()
                peak = self.agent.equity_peak or acct.equity
                dd = float((peak - acct.equity) / peak * 100) if peak > 0 else 0.0
                self.equity_curve.append(EquityPoint(
                    ts_ns=report.ts_ns, equity=float(acct.equity),
                    balance=float(acct.balance), drawdown_pct=dd,
                    open_positions=acct.open_positions))
                if len(self.equity_curve) > 20000:
                    self.equity_curve = self.equity_curve[-20000:]
                self._record_equity_day(report.ts_ns, acct)
                self._last_account_type = str(getattr(acct, "account_type", "") or "")
            except Exception:  # noqa: BLE001
                pass
            self.cycle_history.append(report.to_dict())
            if len(self.cycle_history) > 500:
                self.cycle_history = self.cycle_history[-500:]
        self._broadcast({"type": "cycle", "data": report.to_dict()})
        return report

    # -- pub/sub ------------------------------------------------------------- #

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            self.subscribers.append(fn)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self.subscribers)

    def unsubscribe(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            if fn in self.subscribers:
                self.subscribers.remove(fn)

    def _broadcast(self, message: dict) -> None:
        for fn in list(self.subscribers):
            try:
                fn(message)
            except Exception:  # noqa: BLE001 - a bad subscriber must not stop the engine
                self.unsubscribe(fn)

    # -- read models --------------------------------------------------------- #

    def status(self) -> dict:
        agent = self.agent
        cfg = agent.config
        acct = None
        try:
            acct = agent.broker.account()
            account = {
                "id": acct.account_id, "currency": acct.currency,
                "balance": str(acct.balance), "equity": str(acct.equity),
                "margin_used": str(acct.margin_used),
                "margin_available": str(acct.margin_available),
                "unrealised_pnl": str(acct.unrealised_pnl),
                "margin_level_pct": (str(acct.margin_level_pct)
                                     if acct.margin_level_pct is not None else None),
                "open_positions": acct.open_positions,
            }
        except Exception as exc:  # noqa: BLE001
            account = {"error": str(exc)}
        kill = agent.kill.read()
        caps = agent.broker.capabilities
        venue = self.effective_venue(acct)
        return {
            "ts_ns": wall_ns(),
            "uptime_sec": round((wall_ns() - self.started_ns) / 1e9, 1) if self.started_ns else 0,
            "mode": cfg.agent.mode.value,
            "venue_mode": cfg.execution.venue_mode.value,
            # What is actually running; the label follows this (effective_venue).
            "venue_effective": venue["venue"],
            "venue_source": venue["source"],
            "halted": agent.halted,
            "halt_reason": agent.halt_reason,
            "kill_switch": kill.to_dict(),
            "cycles": agent.cycles,
            "account": account,
            "regime": agent.regime.to_dict() if agent.regime else None,
            "health": (self.last_cycle.health if self.last_cycle else {}),
            "broker": {
                "name": caps.name,
                "supports_client_order_id": caps.supports_client_order_id,
                "supports_server_side_stop": caps.supports_server_side_stop,
                "supports_transaction_stream": caps.supports_transaction_stream,
                "degradations": caps.degradation_report(),
            },
            # Strategies the performance guard has suspended, with the reason.
            # They open nothing until the owner releases them, so an operator
            # who cannot see this list sees a strategy that has simply gone
            # quiet.
            "guard_suspended": agent.guard_suspended,
            "entries_permitted": self._entries_permitted(),
            "unresolved_orders": len(agent.oms.unresolved),
            "quarantined": sorted(agent.oms.quarantined),
            "advisory_pending": len(agent.pending_advice()),
            "proposals_pending": len(agent.proposals.pending()),
            "config_version": cfg.version,
            "errors": self.errors[-5:],
            "cooldowns": self._brain_cooldowns(),
            "terminal": self._terminal_status(),
            "day_pnl": (str(acct.equity - agent.day_start_equity)
                        if "error" not in account and agent.day_start_equity > 0 else None),
        }

    def _terminal_status(self) -> Optional[Dict[str, Any]]:
        wd = getattr(self.agent, "terminal_watchdog", None)
        if wd is None:
            return None
        try:
            return wd.status()
        except Exception:  # noqa: BLE001
            return None

    def macro_view(self) -> Dict[str, Any]:
        if self.macro is None:
            return {"available": False}
        view = self.macro.view()
        view["available"] = True
        return view

    def _brain_cooldowns(self) -> Dict[str, str]:
        if self.brain is None:
            return {}
        try:
            return self.brain.cooldowns()
        except Exception:  # noqa: BLE001
            return {}

    def brain_view(self) -> Dict[str, Any]:
        if self.brain is None:
            return {"enabled": False, "available": False}
        regime = self.agent.regime.regime.value if self.agent.regime else ""
        names = [a.name for a in self.agent.config.strategies if a.enabled]
        view = self.brain.view(names, regime)
        view["available"] = True
        view["regime"] = regime
        return view

    def _entries_permitted(self) -> Dict[str, Any]:
        try:
            allowed, why = self.agent.entry_permission()
        except Exception as exc:  # noqa: BLE001
            allowed, why = False, str(exc)
        return {"allowed": bool(allowed), "reason": why}

    def positions(self) -> List[dict]:
        out: List[dict] = []
        try:
            for pos in self.agent.broker.positions():
                inst = self.agent.broker.instruments().get(pos.instrument)
                row: Dict[str, Any] = {
                    "instrument": pos.instrument, "side": pos.side.value,
                    "lots": str(pos.lots), "entry_price": str(pos.entry_price),
                    "stop_loss": str(pos.stop_loss) if pos.stop_loss else None,
                    "take_profit": str(pos.take_profit) if pos.take_profit else None,
                    "broker_stop_confirmed": pos.broker_stop_confirmed,
                    "strategy": pos.strategy, "opened_ns": pos.opened_ns,
                    "initial_risk": str(pos.initial_risk),
                    "financing_paid": str(pos.financing_paid),
                }
                try:
                    q = self.agent.broker.quote(pos.instrument)
                    conv = self.agent.broker.conversion_rate(
                        inst.quote, self.agent.config.execution.account_currency)
                    row["current_price"] = str(q.price_for(pos.side.opposite))
                    row["unrealised"] = str(pos.unrealised(q, inst, conv))
                    r = pos.r_multiple(q, inst, conv)
                    row["r_multiple"] = str(r) if r is not None else None
                    row["spread_pips"] = str(q.spread_pips(inst))
                except Exception:  # noqa: BLE001
                    row["current_price"] = None
                out.append(row)
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)}]
        return out

    def trades(self, limit: int = 200) -> List[dict]:
        trades = self.realised_trades()
        return [{
            "trade_id": t.trade_id, "strategy": t.strategy, "instrument": t.instrument,
            "side": t.side.value, "lots": str(t.lots), "entry_price": str(t.entry_price),
            "exit_price": str(t.exit_price), "opened_ns": t.opened_ns,
            "closed_ns": t.closed_ns, "pnl": str(t.pnl), "pnl_pips": str(t.pnl_pips),
            "r_multiple": str(t.r_multiple), "exit_reason": t.exit_reason,
            "commission": str(t.commission), "financing": str(t.financing),
            "duration_sec": t.duration_sec, "regime": t.regime,
            "max_favourable_r": str(t.max_favourable_r),
            "max_adverse_r": str(t.max_adverse_r),
        } for t in trades[-limit:]]

    def realised_trades(self) -> List:
        """Every closed trade this runtime has seen, from any venue.

        The agent consumes its history incrementally with a cursor, so the
        dashboard cannot re-read the venue without stealing those rows. The
        runtime keeps its own accumulated copy instead."""
        held = getattr(self.agent.broker, "closed_trades", None)
        if isinstance(held, list):
            return list(held)
        return list(self._realised)

    @property
    def equity_ledger(self):
        if self._equity_ledger is None:
            from ..ops.equity_ledger import EquityLedger
            self._equity_ledger = EquityLedger(
                Path(self.agent.config.ops.state_dir) / "equity-days.db")
        return self._equity_ledger

    def _record_equity_day(self, ts_ns: int, acct) -> None:
        """Keep the day's equity on disk. A failure here costs a monthly
        figure, never a cycle."""
        try:
            account = f"{self.agent.config.execution.broker}:{acct.account_id}"
            self.equity_ledger.record(ts_ns, float(acct.equity), float(acct.balance), account)
        except Exception as exc:  # noqa: BLE001
            msg = f"equity ledger: {type(exc).__name__}: {exc}"[:300]
            if msg not in self.errors[-5:]:
                self.errors.append(msg)
                self.errors = self.errors[-50:]

    def monthly_returns(self) -> dict:
        return self.equity_ledger.monthly()

    def performance(self) -> dict:
        import pandas as pd

        trades = self.realised_trades()
        # Snapshot BOTH derived series under the lock and in one pass. Iterating
        # self.equity_curve twice -- once for the index, once for the values --
        # raced the cycle thread's append and its periodic rebind, and produced
        # "Length of values does not match length of index" as an HTTP 500.
        with self._lock:
            points = list(self.equity_curve)
        if not points:
            return {"n_trades": len(trades), "note": "no equity history yet"}
        idx = pd.DatetimeIndex(pd.to_datetime([p.ts_ns for p in points],
                                              unit="ns", utc=True))
        equity = pd.Series([p.equity for p in points], index=idx)
        equity = equity[~equity.index.duplicated(keep="last")]
        start = points[0].equity or 1.0
        # The live curve is sampled at the agent's decision cadence, not daily.
        # A hard-coded 252 annualised it as though each point were a trading
        # day, which is wrong by sqrt(cadence ratio) -- about 24x at a 60-second
        # decision interval.
        ppy = _periods_per_year_from_index(equity.index) or 252
        perf = compute_performance(trades, equity, periods_per_year=int(round(ppy)),
                                   starting_equity=start)
        out = perf.to_dict()
        out["periods_per_year"] = int(round(ppy))
        return out

    def equity_series(self, limit: int = 2000) -> List[dict]:
        pts = self.equity_curve[-limit:]
        return [{"ts_ns": p.ts_ns, "equity": p.equity, "balance": p.balance,
                 "drawdown_pct": p.drawdown_pct, "open_positions": p.open_positions}
                for p in pts]

    def decisions(self, limit: int = 200, action: Optional[str] = None) -> List[dict]:
        items = self.agent.decisions
        if action:
            items = [d for d in items if d.action == action]
        return [d.to_dict() for d in items[-limit:]][::-1]

    def risk_view(self) -> dict:
        agent = self.agent
        cfg = agent.config.risk
        try:
            acct = agent.broker.account()
            positions = agent.broker.positions()
            snap = agent.feed.snapshot(agent._active_instruments(), now_ns=agent.now())
            ctx = agent._build_context(agent.now(), acct, positions, snap,
                                       agent.health.connected, agent.health.snapshot())
            from ..risk.exposure import currency_exposure, gross_notional
            mids = {k: v.mid for k, v in ctx.quotes.items()}
            risk_map = {p.instrument: (p.initial_risk or ZERO) for p in positions}
            exposures = currency_exposure(positions, ctx.instruments,
                                          risk_by_instrument=risk_map,
                                          conversions=ctx.conversions, mid_prices=mids)
            gross = gross_notional(positions, ctx.instruments, mids, ctx.conversions)
            from ..risk.sizing import ladder_multiplier
            return {
                "drawdown_pct": float(ctx.drawdown_pct),
                "equity_peak": str(ctx.equity_peak),
                "risk_multiplier": str(ladder_multiplier(ctx.drawdown_pct, cfg.ladder,
                                                         cfg.ladder_enabled)),
                "day_pnl": str(ctx.day_pnl), "day_pnl_pct": float(ctx.day_pnl_pct),
                "trades_today": ctx.trades_today,
                "trades_this_week": ctx.trades_this_week,
                "trades_this_year": ctx.trades_this_year,
                "gross_leverage": float(gross / acct.equity) if acct.equity > 0 else 0.0,
                "pending_risk": str(ctx.pending_risk),
                "currency_exposure": [
                    {"currency": c, "net_risk": str(e.net_risk),
                     "gross_risk": str(e.gross_risk),
                     "net_risk_pct": float(abs(e.net_risk) / acct.equity * 100)
                     if acct.equity > 0 else 0.0,
                     "contributors": e.contributors}
                    for c, e in sorted(exposures.items(),
                                       key=lambda kv: abs(kv[1].net_risk), reverse=True)],
                "alarms": [v.to_dict() for v in agent.risk.portfolio_alarms(ctx)],
                "limits": {
                    "risk_per_trade_pct": str(cfg.risk_per_trade_pct),
                    "daily_loss_limit_pct": str(cfg.daily_loss_limit_pct),
                    "weekly_loss_limit_pct": str(cfg.weekly_loss_limit_pct),
                    "monthly_loss_limit_pct": str(cfg.monthly_loss_limit_pct),
                    "max_drawdown_halt_pct": str(cfg.max_drawdown_halt_pct),
                    "max_open_positions": cfg.max_open_positions,
                    "max_trades_per_day": cfg.max_trades_per_day,
                    "max_currency_exposure_pct": str(cfg.max_currency_exposure_pct),
                    "max_correlated_risk_pct": str(cfg.max_correlated_risk_pct),
                    "max_gross_leverage": str(cfg.max_gross_leverage),
                },
                "ladder": cfg.ladder,
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def execution_quality(self) -> dict:
        return self.agent.oms.execution_report()

    # -- write actions (all audited; authorisation happens in the route) ------ #

    # Every mutation below takes the SAME lock the decision cycle holds. The
    # agent's counters (trades_today, last_entry_ns), its position metadata and
    # its state file are all read-modify-write, and a dashboard action landing
    # mid-cycle was interleaving with the agent thread.

    def set_mode(self, mode: str, by: str) -> dict:
        with self._lock:
            self.agent.set_mode(AgentMode(mode), by)
            self.agent.config.save(self.config_path)
        return {"mode": mode}

    def engage_kill(self, reason: str, by: str) -> dict:
        with self._lock:
            return self.agent.kill.engage(reason, by).to_dict()

    def release_kill(self, by: str) -> dict:
        with self._lock:
            return {"released": self.agent.kill.release(by)}

    def halt(self, reason: str, by: str) -> dict:
        with self._lock:
            self.agent.halt(f"{reason} (by {by})")
            return {"halted": True, "reason": self.agent.halt_reason}

    def resume(self, by: str) -> dict:
        with self._lock:
            return {"resumed": self.agent.resume(by)}

    def flatten_all(self, by: str) -> dict:
        """Close everything, and report per-position success honestly.

        A flatten in which every close failed used to be indistinguishable from
        one that worked -- same HTTP 200, same shape. In an emergency that is the
        worst possible ambiguity, so failures are separated and the caller is
        told plainly whether the book is actually flat."""
        closed: List[str] = []
        failed: List[Dict[str, str]] = []
        with self._lock:
            return self._flatten_locked(by, closed, failed)

    def _flatten_locked(self, by: str, closed: List[str],
                        failed: List[Dict[str, str]]) -> dict:
        for pos in self.agent.broker.positions():
            try:
                res = self.agent.broker.close_position(pos.instrument,
                                                       reason="manual_flatten")
            except Exception as exc:  # noqa: BLE001
                failed.append({"instrument": pos.instrument, "error": str(exc)})
                continue
            if res.state in (OrderState.FILLED, OrderState.PARTIAL):
                closed.append(pos.instrument)
            else:
                failed.append({"instrument": pos.instrument,
                               "error": res.reject_reason or res.state.value})
        remaining = [p.instrument for p in self.agent.broker.positions()]
        self._forget_closed(remaining)
        self.agent.audit.append(EventType.POSITION_CLOSE,
                                {"action": "flatten_all", "closed": closed,
                                 "failed": failed, "still_open": remaining}, actor=by)
        if failed:
            self.agent.halt(f"flatten did not complete: {len(failed)} position(s) "
                            "could not be closed")
        return {"ok": not failed, "closed": closed, "failed": failed,
                "still_open": remaining}

    def close_position(self, instrument: str, by: str,
                       lots: Optional[str] = None) -> dict:
        # `lots="0"` is falsy as a string only by accident; treating it as "not
        # specified" closed the ENTIRE position when the caller asked for none.
        size: Optional[Decimal] = None
        if lots is not None and str(lots).strip() != "":
            size = dec(lots)
            if size <= 0:
                return {"state": "rejected",
                        "reason": "a close size must be greater than zero; omit the "
                                  "field entirely to close the whole position"}
        with self._lock:
            res = self.agent.broker.close_position(instrument, size, reason="manual_close")
            self.agent.audit.append(EventType.POSITION_CLOSE,
                                    {"instrument": instrument, "lots": lots,
                                     "state": res.state.value}, actor=by)
            if res.state in (OrderState.FILLED, OrderState.PARTIAL):
                try:
                    remaining = [p.instrument for p in self.agent.broker.positions()]
                except Exception:  # noqa: BLE001 - the next reconcile will settle it
                    remaining = None
                if remaining is not None:
                    self._forget_closed(remaining)
        return {"state": res.state.value, "reason": res.reject_reason}

    def manual_order(self, *, instrument: str, side: str, stop_loss, take_profit,
                     risk_pct, by: str, preview: bool) -> dict:
        from ..core.money import dec as _dec
        with self._lock:
            decision = self.agent.manual_order(
                instrument=instrument, side=side, stop_loss=_dec(stop_loss),
                take_profit=_dec(take_profit) if take_profit not in (None, "") else None,
                risk_pct=_dec(risk_pct) if risk_pct not in (None, "") else None,
                by=by, preview=preview)
        return decision.to_dict()

    def _forget_closed(self, still_open: List[str]) -> None:
        """Drop the agent's metadata for positions a human just closed.

        Left in place, the next reconciliation compared the agent's book --
        still holding the closed position -- against the venue and journalled
        a `phantom` mismatch for every manual close, burying real mismatches
        in noise an operator learns to ignore.
        """
        live = set(still_open)
        meta = getattr(self.agent, "_position_meta", None)
        if not isinstance(meta, dict):
            return
        for sym in [k for k in meta if k not in live]:
            meta.pop(sym, None)
        try:
            self.agent._save_state()
        except Exception:  # noqa: BLE001
            pass

    # Fields that a config write may never change, because each has its own
    # endpoint with its own guard. Routing them through the generic config patch
    # was an escalation path: one authenticated write could mark a strategy
    # accepted, switch the venue to live and set the mode to autonomous, all of
    # which bypassed the checks on their dedicated endpoints.
    _PRIVILEGED = {
        ("agent", "mode"): "use POST /api/control/mode, which validates the "
                           "autonomous+live precondition",
        ("execution", "venue_mode"): "switching to live requires every enabled "
                                     "strategy to hold a passing acceptance verdict; "
                                     "promote them first",
        ("execution", "broker"): "changing the venue adapter requires a restart with "
                                 "the corresponding credentials in the environment",
        ("execution", "expected_account_id"): "the account binding is fixed at startup; "
                                              "a different account is a different engine",
        ("execution", "expected_account_server"): "the account binding is fixed at startup",
        ("execution", "account_currency"): "the account currency is fixed at startup; "
                                           "every open risk figure is denominated in it",
        # Forensic and security settings. These are restart-only, file-edited
        # settings -- not dashboard settings -- and each one is an escalation:
        # relocating the kill file silently discards a persisted engagement,
        # relocating the journal restarts the hash chain in a fresh file with
        # no link to the old head (the only way to break the audit trail's
        # continuity), and the security block can open the bind, widen CORS or
        # switch off the second factor entirely.
        ("ops", "audit_log"): "the audit journal's location is fixed at startup; "
                              "moving it would restart the hash chain in a new file "
                              "with no link to the existing record",
        ("ops", "state_dir"): "the state directory is fixed at startup",
        ("ops", "killswitch_file"): "moving the kill file would discard an engagement "
                                    "that is currently in force",
        ("security", "bind_host"): "the bind address is fixed at startup",
        ("security", "bind_port"): "the bind port is fixed at startup",
        ("security", "require_totp_for_writes"): "the second factor cannot be switched "
                                                 "off from behind the second factor",
        ("security", "allowed_origins"): "CORS is fixed at startup",
        ("security", "session_ttl_minutes"): "session lifetime is fixed at startup",
        ("security", "dashboard_read_only_default"): "fixed at startup",
        # Filesystem locations that the process LOADS or WRITES. Each is a
        # restart-only, server-side setting, because through the dashboard
        # each one is an escalation from "owner of the console" to "code
        # execution on the server": the meta model is a joblib (pickle) file
        # that is unpickled at startup, the plugin directory is imported as
        # Python, and the data paths decide where SQLite and backups write.
        ("agent", "meta_model_path"): "the meta-label model is unpickled at startup; "
                                      "set it in the config file on the server",
        ("ops", "strategy_plugin_dir"): "plugin files are imported as Python code; set "
                                        "the directory on the server",
        ("ops", "backup_dir"): "the backup location is fixed at startup",
        ("ops", "group_ledger_dir"): "the cross-account ledger location is fixed at "
                                     "startup",
        ("data", "store_path"): "the market-data store location is fixed at startup",
    }

    def _guard_privileged_fields(self, current: SentinelConfig,
                                 merged: Dict[str, Any],
                                 allow: Optional[set] = None) -> None:
        """Refuse a blind write to a field that has its own guarded path.

        ``allow`` exempts a field for ONE call, and exists for exactly one
        caller: `activate_connection`, which has already run the full
        activation gate -- probe freshness, account identity, open positions,
        licence, acceptance verdicts. Without it `execution.broker` could never
        be changed by anything, so the venue-switch endpoint returned 409 for
        every real switch and appeared to work only when the profile was
        already active. An exemption a caller must ask for by name is a narrow
        door; removing the field from _PRIVILEGED would have been an open one.
        """
        cur = current.model_dump(mode="json")
        exempt = set(allow or ())
        for (section, key), reason in self._PRIVILEGED.items():
            if (section, key) in exempt:
                continue
            before = (cur.get(section) or {}).get(key)
            after = (merged.get(section) or {}).get(key)
            if after != before:
                raise ValueError(
                    f"{section}.{key} cannot be changed through a configuration "
                    f"write ({before!r} -> {after!r}): {reason}")

        # A lifecycle promotion has to be backed by a stored, passing verdict for
        # that exact strategy. The schema alone only checks that the run id is a
        # non-empty string, which is not evidence of anything.
        from ..research.verdicts import config_fingerprint

        before = {a["name"]: a for a in cur.get("strategies", [])}
        for alloc in merged.get("strategies", []):
            name = alloc.get("name")
            if alloc.get("lifecycle") != "accepted":
                continue
            # The fingerprint now covers the runtime policy, so a write that
            # changes a risk limit beside an accepted strategy re-checks that
            # strategy against the registry -- there is no "unchanged"
            # short-circuit, because the thing that changed may be the policy.
            fingerprint = config_fingerprint(alloc.get("instruments"),
                                             alloc.get("params"),
                                             alloc.get("timeframe"),
                                             runtime_config=merged)
            ok, why = self.verdicts.authorises(name, alloc.get("acceptance_run_id"),
                                               config_hash=fingerprint)
            if not ok:
                prior = before.get(name, {})
                if prior.get("lifecycle") == "accepted" and                         prior.get("acceptance_run_id") == alloc.get("acceptance_run_id"):
                    # The allocation is untouched, so what changed is the
                    # runtime policy the verdict was earned under. Say that,
                    # and say what to do: demote first, or re-run acceptance.
                    raise ValueError(
                        f"this change alters the runtime policy (risk limits, execution, "
                        f"news or research settings) that {name!r} was accepted under; "
                        f"the verdict no longer describes what would run. Set the strategy "
                        f"to 'suspended' first, apply the change, and re-run the acceptance "
                        f"protocol on the new configuration. ({why})")
                raise ValueError(f"refusing to promote {name!r} to accepted: {why}")

    def update_config(self, patch: Dict[str, Any], by: str,
                      *, allow_privileged: Optional[set] = None,
                      replace: Optional[set] = None) -> dict:
        """Merge ``patch`` into the configuration, validate it, save it.

        Nested dictionaries MERGE, so a patch cannot delete a key from one --
        except for the ``(section, key)`` pairs in ``replace``, whose value in
        the patch replaces the stored one whole (a mapping the owner edits as a
        table, where a removed row must actually disappear).
        """
        with self._lock:
            current = self.agent.config
            data = current.model_dump(mode="json")

            def merge(dst: dict, src: dict) -> dict:
                for k, v in src.items():
                    if isinstance(v, dict) and isinstance(dst.get(k), dict):
                        merge(dst[k], v)
                    else:
                        dst[k] = v
                return dst

            merged = merge(data, patch)
            for section, key in (replace or ()):
                sub = patch.get(section)
                if isinstance(sub, dict) and key in sub:
                    merged[section][key] = sub[key]
            self._guard_privileged_fields(current, merged, allow=allow_privileged)
            merged["version"] = current.version + 1
            merged["updated_at_ns"] = wall_ns()
            merged["updated_by"] = by
            # Validation happens here: an invalid config is rejected before it
            # can reach the risk engine.
            new = SentinelConfig.model_validate(merged)
            changes = diff_configs(current, new)
            self.agent.config = new
            self.agent.risk = RiskEngine(new.risk)
            new.save(self.config_path)
        self.agent.audit.append(EventType.CONFIG_CHANGE, {
            "version": new.version,
            "changes": [{"path": c.path, "from": c.old, "to": c.new} for c in changes],
        }, actor=by)
        return {"version": new.version,
                "changes": [{"path": c.path, "from": c.old, "to": c.new} for c in changes]}
