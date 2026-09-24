"""Assemble a running system from configuration and environment.

Nothing here reads a secret from a file in the repository. Credentials come
from the environment only, and the process refuses to start in live mode
without them.
"""

from __future__ import annotations

import os
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Optional, Tuple

from .agent.memory import MemoryStore
from .agent.orchestrator import Agent
from .agent.proposals import ProposalQueue
from .api.security import SecurityManager, UserStore
from .api.state import Runtime
from .brokers import build_broker
from .brokers.paper import PaperBroker, SimProfile
from .core.audit import AuditLog, EventType
from .core.config import AgentMode, ExecutionVenueMode, SentinelConfig
from .core.money import D, Instrument
from .licensing import LicenseGate
from .data.feed import BarStore, MarketFeed
from .news.calendar import EconomicCalendar
from .news.policy import NewsPolicy
from .research.verdicts import RegistryUnreadable, VerdictStore, enforce_config_authority

DEFAULT_INSTRUMENTS = {
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
    "AUD_USD": Instrument("AUD_USD", "AUD", "USD"),
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001")),
    "USD_CHF": Instrument("USD_CHF", "USD", "CHF"),
    "USD_CAD": Instrument("USD_CAD", "USD", "CAD"),
    "NZD_USD": Instrument("NZD_USD", "NZD", "USD"),
    "EUR_JPY": Instrument("EUR_JPY", "EUR", "JPY", pip=D("0.01"), tick=D("0.001")),
}


def _connection_kwargs(config, state_dir, audit) -> dict:
    """Connection parameters for the venue the operator configured.

    Reads the saved connection whose profile matches ``execution.broker`` and
    unseals its credential. If none is saved, returns nothing and the adapter
    falls back to the environment -- which is the correct behaviour for a
    container deployment and was the ONLY behaviour before the dashboard could
    configure a venue.

    Failures here are deliberately non-fatal and loud. A credential store that
    cannot be opened must not stop the process: it must start, refuse to trade
    live for want of credentials, and say why in a place the operator will look.
    """
    try:
        from .brokers.connection import ConnectionStore
    except Exception:  # noqa: BLE001
        return {}
    path = state_dir / "brokers.json"
    if not path.exists():
        return {}
    try:
        store = ConnectionStore(path)
        chosen = [c for c in store.list()
                  if c.enabled and c.profile == config.execution.broker]
    except Exception as exc:  # noqa: BLE001
        audit.append(EventType.CONFIG_CHANGE,
                     {"broker_connection": "unreadable", "error": str(exc)[:200]},
                     actor="bootstrap")
        return {}
    if not chosen:
        return {}
    if len(chosen) > 1:
        # Two enabled connections for one profile is ambiguous, and picking one
        # by list order would route orders to whichever was saved first.
        raise RuntimeError(
            f"{len(chosen)} saved connections are enabled for broker "
            f"'{config.execution.broker}': "
            + ", ".join(c.id for c in chosen)
            + ". Enable exactly one.")

    conn = chosen[0]

    # The activation gate runs in the dashboard, and a file on disk can reach
    # this point without ever having passed through it: hand-edited, restored
    # from a backup taken before a failed test, or enabled by a version of the
    # software that did not check. Re-assert the two conditions that cost
    # money, here, at the last moment before the adapter is built.
    #
    # (This used to re-import ExecutionVenueMode with `from ..core.config`,
    # which from sentinel/bootstrap.py is a relative import beyond the package
    # and raises ImportError -- so every saved, enabled connection stopped the
    # process at this line, before the adapter was built. The module-level
    # import above is the one to use, and tests/test_bar_feed.py now walks
    # this path with a saved connection.)
    live_mode = config.execution.venue_mode is ExecutionVenueMode.LIVE
    probe = conn.last_probe or {}
    problems: list = []
    if live_mode and conn.declared_account_type != "live":
        problems.append(
            f"connection {conn.id!r} is recorded as a {conn.declared_account_type} "
            "account but the configuration says live trading")
    if live_mode and not probe:
        problems.append(
            f"connection {conn.id!r} has never passed a connection test")
    elif live_mode and not probe.get("ok"):
        problems.append(
            f"connection {conn.id!r} last failed its connection test "
            f"({', '.join(probe.get('blocking_failures') or []) or 'no detail'})")
    if problems:
        audit.append(EventType.CONFIG_CHANGE,
                     {"broker_connection": conn.id, "refused": problems},
                     actor="bootstrap")
        raise RuntimeError(
            "refusing to start in live mode: " + "; ".join(problems)
            + ". Open the dashboard, test the connection, and activate it there.")

    kwargs: dict = {}
    if conn.account_currency:
        kwargs["account_currency"] = conn.account_currency
    adapter = conn.adapter
    if adapter == "mt5":
        if conn.login:
            kwargs["login"] = conn.login
        if conn.server:
            kwargs["server"] = conn.server
        if conn.terminal_path:
            kwargs["terminal_path"] = conn.terminal_path
    elif adapter == "oanda":
        if conn.login:
            kwargs["account_id"] = conn.login
        kwargs["environment"] = conn.server or "practice"
    elif adapter == "ccxt":
        kwargs["exchange_id"] = conn.exchange_id or conn.server or "binance"

    if conn.secret_ref:
        try:
            from .brokers.secrets import SecretStore
            secret = SecretStore(state_dir / "broker-secrets.json").get(conn.secret_ref)
        except Exception as exc:  # noqa: BLE001
            audit.append(EventType.CONFIG_CHANGE,
                         {"broker_connection": conn.id,
                          "credential": "unreadable", "error": str(exc)[:200]},
                         actor="bootstrap")
            secret = None
        if secret:
            if adapter == "mt5":
                kwargs["password"] = secret
            elif adapter == "oanda":
                kwargs["token"] = secret
            elif adapter == "ccxt":
                key, _, sec = secret.partition(":")
                kwargs["api_key"], kwargs["secret"] = key, sec
    audit.append(EventType.CONFIG_CHANGE,
                 {"broker_connection": conn.id, "profile": conn.profile,
                  "account_type": conn.declared_account_type,
                  "credential_supplied": bool(conn.secret_ref)},
                 actor="bootstrap")
    return kwargs


def _validated_totp_secret(secret: str) -> str:
    """Reject a TOTP secret that is not usable base32 with real entropy.

    An unvalidated value was accepted verbatim, so a typo produced an owner who
    could never satisfy a write, and "AAAAAAAAAAAAAAAA" produced one whose
    second factor was guessable.
    """
    import base64
    cleaned = secret.strip().replace(" ", "").upper()
    try:
        raw = base64.b32decode(cleaned, casefold=True)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "SENTINEL_ADMIN_TOTP_SECRET is not valid base32. Leave it unset and "
            "let the system generate one.") from exc
    if len(raw) < 16:
        raise RuntimeError(
            "SENTINEL_ADMIN_TOTP_SECRET is shorter than 128 bits. Leave it unset "
            "and let the system generate one.")
    if len(set(raw)) < 4:
        raise RuntimeError(
            "SENTINEL_ADMIN_TOTP_SECRET has almost no entropy. Leave it unset and "
            "let the system generate one.")
    return cleaned


def build_runtime(config_path: str | Path = "var/config.json",
                  *, starting_balance: Decimal = D("10000")) -> Tuple[Runtime, SecurityManager]:
    config = SentinelConfig.load(config_path)
    state_dir = Path(config.ops.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    audit = AuditLog(config.ops.audit_log)

    # A configuration file can claim anything. Re-check every "accepted" badge
    # against the verdict registry before the file is allowed to authorise a
    # single order, and repair downward if it cannot be backed.
    try:
        verdicts = VerdictStore(state_dir / "verdicts.db")
        config, violations, repaired = enforce_config_authority(config, verdicts)
    except RegistryUnreadable as exc:
        audit.append(EventType.CONFIG_CHANGE,
                     {"startup_refused": str(exc)}, actor="startup")
        raise RuntimeError(f"refusing to start: {exc}") from exc
    for v in violations:
        audit.append(EventType.CONFIG_CHANGE,
                     {"startup_authority_violation": v}, actor="startup")
        print(f"[bootstrap] REFUSED: {v}")
    if repaired:
        config.save(config_path)

    # --- user strategy plugins ------------------------------------------ #
    # Loaded after the configuration has been re-checked and before anything is
    # built from it, so a plugin is available to an allocation that names it.
    # Failures are recorded and skipped rather than fatal: a typo in an
    # experimental strategy must not stop a process that may be holding
    # positions. `register()` still refuses anything declaring itself accepted,
    # so a file dropped here cannot become a route to real money.
    if config.ops.strategy_plugin_dir:
        from .strategy.registry import load_report, load_user_strategies

        loaded = load_user_strategies(config.ops.strategy_plugin_dir)
        errors = load_report()["errors"]
        audit.append(EventType.SYSTEM_START,
                     {"strategy_plugins": {"dir": config.ops.strategy_plugin_dir,
                                           "loaded": loaded, "errors": errors}},
                     actor="startup")
        for err in errors:
            print(f"[strategy] plugin NOT loaded: {err}")
        if loaded:
            print(f"[strategy] loaded {len(loaded)} user strategies: {', '.join(loaded)}")

    # --- licence ------------------------------------------------------- #
    # Evaluated BEFORE the broker is built, so a licence problem is reported
    # once and clearly rather than as a puzzling refusal later. Note what it
    # does NOT do: nothing here stops the agent managing positions that are
    # already open. A licence dispute must never cost the customer money.
    gate = LicenseGate(licence_path=os.environ.get("SENTINEL_LICENSE",
                                                   str(state_dir / "licence.key")),
                       root=Path(__file__).resolve().parents[1])
    lic_status = gate.check()
    audit.append(EventType.SYSTEM_START, {"licence": lic_status.to_dict()},
                 actor="licence")
    for warning in lic_status.warnings:
        print(f"[licence] {warning}")
    if config.execution.venue_mode is ExecutionVenueMode.LIVE:
        allowed, why = gate.may_trade_live()
        if not allowed:
            # DEGRADE, do not refuse to start.
            #
            # Refusing to boot was the obvious behaviour and it was wrong: with
            # Restart=on-failure it becomes a crash loop, and a process that
            # cannot start cannot manage the positions that are ALREADY OPEN --
            # their trailing stops, their give-back ratchet, the weekend
            # flatten. That turns a billing problem into a market loss, which
            # is precisely what enforcement.py says must never happen.
            #
            # So the agent starts, drops to advisory, and keeps protecting the
            # book. No new risk is taken; existing risk is still managed.
            audit.append(EventType.CONFIG_CHANGE,
                         {"licence_refused_live": why,
                          "action": "started in advisory mode; open positions are "
                                    "still being managed"}, actor="licence")
            config.agent.mode = AgentMode.ADVISORY
            print(f"[licence] {why}")
            print("[licence] Started in ADVISORY mode instead of refusing to run. "
                  "No new positions will be opened; positions already open are "
                  "still protected and managed.")
    elif not lic_status.valid and not lic_status.unlicensed_mode:
        print(f"[licence] {lic_status.reason}")
        print("[licence] Paper trading, research and the dashboard are unaffected.")

    for breach in gate.check_limits(
            instruments=len({i for a in config.strategies for i in a.instruments})):
        print(f"[licence] {breach}")
        audit.append(EventType.CONFIG_CHANGE, {"licence_limit": breach}, actor="licence")

    if config.execution.broker == "paper":
        broker = PaperBroker(instruments=DEFAULT_INSTRUMENTS,
                             starting_balance=starting_balance,
                             account_currency=config.execution.account_currency,
                             profile=SimProfile())
        broker.set_conversion("JPY", D("1") / D("150"))
        broker.set_conversion("CHF", D("1") / D("0.88"))
        broker.set_conversion("CAD", D("1") / D("1.36"))
    else:
        connection_kwargs = _connection_kwargs(config, state_dir, audit)
        if config.execution.venue_mode is ExecutionVenueMode.LIVE:
            required = {"oanda": ("OANDA_ACCOUNT_ID", "OANDA_API_TOKEN")}.get(
                config.execution.broker, ())
            # Satisfied by the SEALED CREDENTIAL STORE as well as by the
            # environment. Requiring the environment unconditionally meant a
            # customer who configured OANDA entirely through the dashboard --
            # the supported path -- could not start in live mode at all, while
            # MetaTrader, which needs a password just as much, was never
            # checked for one.
            supplied = {"OANDA_ACCOUNT_ID": bool(connection_kwargs.get("account_id")),
                        "OANDA_API_TOKEN": bool(connection_kwargs.get("token"))}
            missing = [k for k in required
                       if not os.environ.get(k) and not supplied.get(k)]
            if missing:
                raise RuntimeError(
                    f"live mode needs {', '.join(missing)} — either in the "
                    "environment, or saved against an enabled broker connection "
                    "in the dashboard. Credentials are never read from a config "
                    "file or the repository.")
        # The MT5 intent journal lives in the state directory, beside the
        # audit chain: it is what resolves an UNKNOWN order after a restart.
        from .brokers.profiles import resolve_profile as _resolve
        _prof = _resolve(config.execution.broker)
        if (_prof.adapter if _prof else config.execution.broker) == "mt5":
            connection_kwargs.setdefault("state_path", str(state_dir / "mt5-intents.json"))
        broker = build_broker(config.execution.broker, **connection_kwargs)
        # Bind the adapter to the declared account. From here on every call
        # re-checks the venue's own statement of identity, so a terminal that
        # someone signs into another account stops receiving orders instead of
        # receiving them for the wrong book.
        if config.execution.expected_account_id:
            from .brokers.bound import AccountBoundBroker
            broker = AccountBoundBroker(
                broker, config.execution.expected_account_id,
                config.execution.venue_mode.value, config.execution.account_currency,
                config.execution.expected_account_server)
            audit.append(EventType.SYSTEM_START,
                         {"account_bound": broker.bound_to}, actor="bootstrap")
        elif config.execution.venue_mode is ExecutionVenueMode.LIVE:
            raise RuntimeError(
                "live trading needs execution.expected_account_id so the engine can "
                "refuse to route orders when the terminal's account changes")
        else:
            print("[bootstrap] WARNING: no expected_account_id -- the engine will trade "
                  "whichever account the terminal is signed into. Set it before demo "
                  "testing with real broker credentials.")

    store = BarStore(config.data.store_path)
    # The paper venue has no price source of its own: it is a fill engine. The
    # synthetic driver gives it a reproducible market in wall-clock time, and
    # labels every bar `synthetic` so nothing downstream mistakes it for data.
    # A real venue delivers its own candles through Broker.fetch_bars, and the
    # feed polls those incrementally. Before either existed the store was only
    # ever written by scripts/run_paper_sim.py, so a served process -- paper,
    # demo or live -- saw empty frames and produced no signals at all.
    driver = None
    if config.execution.broker == "paper":
        from .data.synthetic_live import SyntheticMarketDriver
        driver = SyntheticMarketDriver(broker, store, timeframe="H4",
                                       history=config.data.history_bars)
    feed = MarketFeed(broker, store, timeframe="H4", history=config.data.history_bars,
                      driver=driver)
    if driver is None and not broker.supports_bar_history:
        msg = (f"{broker.capabilities.name} delivers no bar history; strategies will "
               "see empty frames until bars are imported into the store")
        print(f"[bootstrap] WARNING: {msg}")
        audit.append(EventType.DATA_GAP, {"bar_history": "unsupported", "note": msg},
                     actor="bootstrap")
    memory = MemoryStore(state_dir / "memory.db")
    proposals = ProposalQueue(str(state_dir / "proposals.json"))
    calendar = EconomicCalendar(state_dir / "calendar.db")
    # Seed from the bundled recurrence patterns and keep the horizon rolling.
    # The store had no ingestion at all, so the blackout query ran against an
    # empty table on every cycle and the news filter was decorative: it reported
    # "no events" forever and nobody could tell that apart from a quiet week.
    #
    # reschedule_for_dst afterwards, because a calendar seeded in February holds
    # New York releases at their winter UTC instant; after the March switch each
    # of them is an hour early, which is worse than having no window at all.
    if config.news.enabled and config.news.calendar_source in ("local", "bundled"):
        try:
            from .core.clock import wall_ns
            from .news.schedule import RecurringScheduleSource

            now_ns = wall_ns()
            horizon = now_ns + 120 * 86400 * 1_000_000_000
            report = calendar.ingest(RecurringScheduleSource(), now_ns, horizon)
            calendar.reschedule_for_dst(now_ns, horizon)
            if report.errors:
                print(f"[bootstrap] calendar ingestion reported {len(report.errors)} "
                      f"problems; first: {report.errors[0]}")
        except Exception as exc:  # noqa: BLE001 - a calendar is not worth a failed boot
            print(f"[bootstrap] offline calendar seeding failed ({exc}); the news filter "
                  "will see an empty calendar. This is a degraded state, not a quiet one.")
    news = NewsPolicy(calendar, role=config.news.role,
                      before_min=config.risk.block_minutes_before_high_impact,
                      after_min=config.risk.block_minutes_after_high_impact)

    # The licence is asked again before every NEW live entry, not only here at
    # boot: a licence that expires while the service stays up must stop new
    # live risk the moment it lapses (open positions stay managed).
    agent = Agent(config, broker, feed, audit, memory, proposals=proposals, news=news,
                  entry_gate=gate.may_trade_live)
    runtime = Runtime(agent, config_path, verdicts=verdicts, licence=gate)

    jwt_secret = os.environ.get("SENTINEL_JWT_SECRET")
    if not jwt_secret:
        jwt_secret = secrets.token_urlsafe(48)
        print("[bootstrap] SENTINEL_JWT_SECRET was not set; a random one was generated. "
              "Every session is invalidated on restart. Set it in the environment for "
              "a persistent deployment.")
    security = SecurityManager(
        audit, secret=jwt_secret,
        session_ttl_minutes=config.security.session_ttl_minutes,
        max_login_attempts=config.security.max_login_attempts,
        lockout_minutes=config.security.lockout_minutes,
        write_rate_per_minute=config.security.write_rate_limit_per_minute,
        read_rate_per_minute=config.security.api_rate_limit_per_minute,
        store=UserStore(state_dir / "users.db"))

    admin_user = os.environ.get("SENTINEL_ADMIN_USER")
    admin_pass = os.environ.get("SENTINEL_ADMIN_PASSWORD")
    existing_owners = [u for u in security.list_users() if u.role == "owner"]
    if admin_user and admin_pass:
        if existing_owners:
            # An owner already exists. Do NOT create another from the
            # environment. Refusing only when the USERNAME matched still let a
            # changed SENTINEL_ADMIN_USER silently mint a SECOND owner with an
            # attacker-chosen TOTP secret -- and compose keeps env_file live for
            # the life of the service, so the variables are always readable.
            named = ", ".join(sorted(u.username for u in existing_owners))
            print(f"[bootstrap] an owner already exists ({named}); environment "
                  "credentials ignored. Remove SENTINEL_ADMIN_PASSWORD from the "
                  "environment -- it is no longer needed. Use "
                  "scripts/manage_users.py to add accounts.")
        else:
            totp = os.environ.get("SENTINEL_ADMIN_TOTP_SECRET")
            if totp:
                totp = _validated_totp_secret(totp)
            try:
                _, uri = security.add_user(admin_user, admin_pass, "owner",
                                           totp_secret=totp)
            except ValueError as exc:
                # A rejected username or password used to surface as a Python
                # traceback at first boot, from inside a function whose name
                # said nothing about the environment variable at fault. The
                # service still must not start -- without an owner nobody can
                # log in at all -- but the operator has to be told what to
                # change, in the first line, not the thirtieth.
                raise RuntimeError(
                    "the first owner account could not be created.\n"
                    f"  reason: {exc}\n"
                    "  fix:    edit SENTINEL_ADMIN_USER / SENTINEL_ADMIN_PASSWORD "
                    "in the environment file and start the service again.\n"
                    "  note:   the password must be at least 12 characters, must "
                    "not contain the username, and must not be one of the "
                    "commonly-breached passwords."
                ) from exc
            if not totp:
                # NEVER print the URI. stdout is journald under systemd and the
                # json-file driver under compose (retained 5 x 20MB), so the
                # owner's second factor would be readable by anyone who can run
                # `docker logs`, read the journal, or receive a support bundle.
                enrol = state_dir / f"enrolment-{admin_user}.txt"
                # Created 0600 by open(), not chmod'd afterwards. write_text()
                # creates at 0666 & ~umask -- typically 0644 -- and a descriptor
                # opened during that window keeps read access for ever, because
                # POSIX checks permissions at open() and not at read(). This
                # file holds the SECOND FACTOR for the only owner, and it is
                # the same race UserStore.__init__ documents having been won
                # against this very secret.
                fd = os.open(enrol, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, (uri + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
                print(f"[bootstrap] owner {admin_user!r} created. Enrol your "
                      f"authenticator from:\n    {enrol}\n"
                      "Then DELETE that file. It is not printed to the log on "
                      "purpose -- logs are readable by more people than you think.")
    elif not security.list_users():
        print("[bootstrap] WARNING: no accounts exist, so nobody can log in. Create "
              "the first owner with:\n"
              "    python scripts/manage_users.py add --username owner --role owner")
    return runtime, security
