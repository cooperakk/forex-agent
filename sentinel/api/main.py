"""FastAPI application.

Shape of the surface:

* ``GET``  endpoints are read-only and need a session.
* ``POST`` endpoints mutate something and need a session **plus** a fresh TOTP
  code in the ``X-TOTP`` header. There is no exception, including for the
  emergency controls -- the kill switch also has an out-of-band file path that
  needs no HTTP at all, which is the real emergency route.
* Risk-limit changes additionally require the ``owner`` role.

Binding to loopback is the default. The app refuses to start bound to a public
interface unless ``SENTINEL_ALLOW_PUBLIC_BIND=1`` is set, and then it stamps a
warning into every status response so the dashboard can show a banner that
cannot be dismissed.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket,
    WebSocketDisconnect, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from ..core.audit import EventType
from ..core.clock import wall_ns
from ..core.config import AgentMode
from .security import SECURITY_HEADERS, SecurityManager, Session
from .state import Runtime

API_VERSION = "1.8.1"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ModeRequest(BaseModel):
    mode: str


class ReasonRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class ClosePositionRequest(BaseModel):
    instrument: str = Field(min_length=3, max_length=24)
    lots: Optional[str] = None


class ConfigPatch(BaseModel):
    patch: Dict[str, Any]


class AdviceAction(BaseModel):
    client_order_id: str
    reason: str = ""


class GuardRelease(BaseModel):
    strategy: str = Field(min_length=1, max_length=64)


_DECIMAL_TEXT = r"^[0-9]+(\.[0-9]+)?$"


class ManualTicket(BaseModel):
    instrument: str = Field(min_length=3, max_length=24, pattern=r"^[A-Z0-9_.]+$")
    side: str = Field(pattern=r"^(BUY|SELL)$")
    stop_loss: str = Field(min_length=1, max_length=24, pattern=_DECIMAL_TEXT)
    take_profit: Optional[str] = Field(default=None, max_length=24, pattern=_DECIMAL_TEXT)
    risk_pct: Optional[str] = Field(default=None, max_length=8, pattern=_DECIMAL_TEXT)


class AIProviderSave(BaseModel):
    provider: str = Field(min_length=2, max_length=20)
    enabled: bool = False
    model: str = Field(default="", max_length=120)
    base_url: str = Field(default="", max_length=300)
    #: None keeps the stored key; "" deletes it.
    api_key: Optional[str] = Field(default=None, max_length=400)


class AISettingsSave(BaseModel):
    primary: str = Field(default="", max_length=20)
    fallbacks: List[str] = Field(default_factory=list, max_length=6)
    purposes: Dict[str, bool] = Field(default_factory=dict)
    max_calls_per_hour: int = Field(default=60, ge=0, le=10000)
    max_calls_per_day: int = Field(default=400, ge=0, le=100000)


class AIProviderRef(BaseModel):
    provider: str = Field(min_length=2, max_length=20)


class JevModeSave(BaseModel):
    mode: str = Field(pattern=r"^(shadow|shrink_only|active)$")


class JevLabel(BaseModel):
    article_id: str = Field(min_length=1, max_length=200)
    is_correction: Optional[bool] = None
    contradicts_prior: Optional[bool] = None
    direction: Optional[str] = Field(default=None,
                                     pattern=r"^(hawkish|dovish|neutral|unclear)$")


class JevLabels(BaseModel):
    labels: List[JevLabel] = Field(min_length=1, max_length=100)


class BrainSave(BaseModel):
    #: Fields of the `brain` configuration section; `stress_scenarios`, when
    #: sent, replaces the stored table.
    patch: Dict[str, Any] = Field(max_length=60)


class BrainModelRef(BaseModel):
    model_id: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class BrainCooldownRef(BaseModel):
    scope: str = Field(min_length=1, max_length=64)


class NotifyChannelSave(BaseModel):
    channel: str = Field(pattern=r"^(telegram|bale)$")
    enabled: bool = False
    #: None keeps the stored chat id; "" clears it.
    chat_id: Optional[str] = Field(default=None, max_length=40)
    categories: Optional[List[str]] = Field(default=None, max_length=10)
    commands: bool = False
    #: None keeps the stored token; "" deletes it.
    token: Optional[str] = Field(default=None, max_length=120)
    daily_hour_utc: Optional[int] = Field(default=None, ge=0, le=23)


class NotifyChannelRef(BaseModel):
    channel: str = Field(pattern=r"^(telegram|bale)$")


class SectionPatch(BaseModel):
    """Fields of one configuration section; validated by the section's model."""
    patch: Dict[str, Any] = Field(max_length=40)


class ReferenceSave(BaseModel):
    """The independent reference price (TradingView). Every field is optional;
    ``symbol_map``, when sent, REPLACES the stored mapping."""

    enabled: Optional[bool] = None
    exchange: Optional[str] = Field(default=None, max_length=24, pattern=r"^[A-Z0-9_]+$")
    symbol_map: Optional[Dict[str, str]] = None
    shrink_bp: Optional[float] = None
    block_bp: Optional[float] = None
    shrink_multiplier: Optional[float] = None
    max_age_sec: Optional[int] = None
    ta_ratings: Optional[bool] = None


class ProposalReview(BaseModel):
    proposal_id: str
    approve: bool
    reason: str = ""


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: str = Field(default="viewer")


class UserRole(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    role: str


class UserFlag(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    disabled: bool


class UserPassword(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserName(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class ConnectionSave(BaseModel):
    id: str = Field(min_length=2, max_length=40)
    display_name: str = Field(min_length=1, max_length=80)
    profile: str = Field(min_length=1, max_length=40)
    declared_account_type: str = Field(default="demo")
    server: str = Field(default="", max_length=120)
    login: str = Field(default="", max_length=64)
    # Validated on the way in: this string is handed to MetaTrader5's
    # initialize(path=...), which LAUNCHES it. It must look like an absolute
    # path to a terminal executable and nothing else.
    terminal_path: str = Field(default="", max_length=512)

    @field_validator("terminal_path")
    @classmethod
    def _plausible_terminal(cls, v: str) -> str:
        text = (v or "").strip()
        if not text:
            return ""
        if any(ch in text for ch in ";|&$`\n\r\t\0><*?\""):
            raise ValueError(
                "مسیر برنامه نباید نویسه‌های خط فرمان داشته باشد.")
        looks_absolute = text.startswith("/") or (
            len(text) > 2 and text[1] == ":" and text[2] in "\\/")
        if not looks_absolute:
            raise ValueError(
                "مسیر برنامهٔ متاتریدر باید کامل باشد، مثل "
                r"C:\Program Files\MetaTrader 5\terminal64.exe")
        tail = text.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if not tail.endswith((".exe", "terminal", "terminal64")):
            raise ValueError(
                "مسیر باید به خود برنامهٔ ترمینال ختم شود (مثلاً terminal64.exe)، "
                "نه به یک پوشه.")
        return text
    account_currency: str = Field(default="", max_length=8)
    exchange_id: str = Field(default="", max_length=40)
    origin: str = Field(default="manual", max_length=16)
    notes: str = Field(default="", max_length=500)
    #: None means "leave the stored credential alone"; "" means "delete it".
    secret: Optional[str] = Field(default=None, max_length=512)


class ConnectionRef(BaseModel):
    id: str = Field(min_length=2, max_length=40)


class LicenceInstall(BaseModel):
    document: str = Field(min_length=64, max_length=16384)


# Concurrent live streams allowed across all sessions. Each pins a queue and
# is walked on every broadcast.
_MAX_WS_SUBSCRIBERS = 24


def _is_loopback(host: str) -> bool:
    """True only for addresses that cannot be reached from another machine."""
    import ipaddress
    value = (host or "").strip().strip("[]")
    if value in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        # A hostname we cannot resolve here. Treat it as public: the safe
        # direction is to warn about an exposure that may not exist, never to
        # stay silent about one that does.
        return False


def create_app(runtime: Runtime, security: SecurityManager, *,
               dashboard_dir: Optional[str] = None,
               bind_host: Optional[str] = None) -> FastAPI:
    cfg = runtime.agent.config
    # The host ACTUALLY being bound, which is not always the one in the config:
    # the caller may pass --host, and the shipped container does. Deriving this
    # from the config alone let --host 0.0.0.0 skip both the refusal below and
    # the permanent exposure banner in /api/status.
    effective_host = bind_host or cfg.security.bind_host
    # Anything that is not loopback is a public bind. Matching only 0.0.0.0 and
    # :: meant binding to a specific routable address -- 203.0.113.5, say --
    # skipped both the refusal and the dashboard's permanent exposure banner,
    # which is the one case where an operator most believes they were careful.
    public_bind = not _is_loopback(effective_host)
    allow_public = os.environ.get("SENTINEL_ALLOW_PUBLIC_BIND") == "1"
    if public_bind and not allow_public:
        raise RuntimeError(
            "refusing to start: bind_host exposes the dashboard beyond loopback. "
            "A dashboard with trading authority on a public interface is equivalent "
            "to publishing the account. Put it behind a VPN or an SSH tunnel; set "
            "SENTINEL_ALLOW_PUBLIC_BIND=1 only if you have done that and understand "
            "the exposure.")

    app = FastAPI(title="Sentinel-FX", version=API_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.state.security = security
    app.state.public_bind = public_bind

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.security.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-TOTP"],
        max_age=600,
    )

    @app.middleware("http")
    async def harden(request: Request, call_next):
        # Every exit path stamps the headers. The early 413 and the 500
        # catch-all used to return before the loop, so an error response
        # carried no CSP, no nosniff and no frame protection -- the responses
        # an attacker is most able to provoke.
        def _stamp(response):
            for k, v in SECURITY_HEADERS.items():
                response.headers[k] = v
            return response

        length = request.headers.get("content-length")
        if length:
            try:
                too_big = int(length) > 256_000 or int(length) < 0
            except ValueError:
                # int() on a malformed header raised OUTSIDE the try below, so
                # the request escaped every handler as an unstamped 500.
                return _stamp(JSONResponse({"detail": "malformed content-length"},
                                           status_code=400))
            if too_big:
                return _stamp(JSONResponse({"detail": "request body too large"},
                                           status_code=413))
        try:
            response = await call_next(request)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            # LOG IT. Swallowing the exception with only an audit record meant
            # an operator saw a dead dashboard and a clean server log, with the
            # real cause -- a non-JSON-serialisable float -- visible nowhere.
            # A 500 nobody can diagnose is worse than a crash.
            import logging
            import traceback
            logging.getLogger(__name__).error(
                "unhandled error serving %s: %s\n%s",
                request.url.path, exc, traceback.format_exc())
            # A read that fails is not a denied WRITE. Recording it as one put
            # a `sec.write_denied` record in the tamper-evident journal for
            # every failed dashboard poll, which buries real denials.
            runtime.agent.audit.append(
                EventType.DATA_STALE if request.method == "GET"
                else EventType.WRITE_DENIED,
                {"path": request.url.path, "method": request.method,
                 "error": str(exc)[:400]})
            # Never leak a stack trace to a client that may be hostile.
            return _stamp(JSONResponse({"detail": "internal error"}, status_code=500))
        return _stamp(response)

    # -- audit verification cache ------------------------------------------ #
    #
    # Verifying the chain re-hashes every record ever written. It ran on EVERY
    # /api/audit call, which the dashboard polls, so a months-old journal made
    # each poll a full-file SHA-256 walk that any viewer could trigger 120
    # times a minute. The result is reused while the file is byte-for-byte the
    # same size and modification time and the head sequence has not moved; any
    # edit, truncation or append changes one of the three and forces a walk.
    _verify_cache: Dict[str, Any] = {"key": None, "result": None}
    _verify_lock = __import__("threading").Lock()

    def _verified_chain(audit):
        try:
            st = os.stat(audit.path)
            key = (int(st.st_size), int(st.st_mtime_ns), int(audit.seq))
        except (OSError, AttributeError, TypeError):
            return audit.verify()
        with _verify_lock:
            if _verify_cache["key"] == key and _verify_cache["result"] is not None:
                return _verify_cache["result"]
        result = audit.verify()
        with _verify_lock:
            _verify_cache["key"], _verify_cache["result"] = key, result
        return result

    # -- dependencies ------------------------------------------------------- #

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    async def current_session(request: Request,
                              authorization: str = Header(default="")) -> Session:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
        token = authorization[7:]
        session = security.verify_token(
            token, user_agent=request.headers.get("user-agent", ""),
            client_ip=client_ip(request))
        if session is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired session")
        # Read the config at REQUEST time, not from a local captured when the
        # app was built. update_config REPLACES agent.config with a new object,
        # so the closure kept the boot-time instance forever: an operator
        # lowering the rate limit during an attack was told it worked and got
        # no effect.
        if not security.check_rate(f"read:{session.username}",
                                   runtime.agent.config.security.api_rate_limit_per_minute):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded")
        return session

    def require_write(action: str, requires_owner: bool = False):
        async def dep(session: Session = Depends(current_session),
                      x_totp: str = Header(default="")) -> Session:
            ok, msg = security.authorise_write(session, x_totp, action,
                                               requires_owner=requires_owner)
            if not ok:
                raise HTTPException(status.HTTP_403_FORBIDDEN, msg)
            return session
        return dep

    # -- liveness (unauthenticated) ------------------------------------------ #

    @app.get("/health", include_in_schema=False)
    async def process_health():
        """Is the process serving? Nothing about the account, so no session.

        The container healthcheck and the installer's readiness wait need a
        200 without credentials; /api/health stays authenticated because it
        reports the account.
        """
        return {"status": "ok", "version": API_VERSION}

    # -- auth ---------------------------------------------------------------- #

    @app.post("/api/auth/login")
    def login(body: LoginRequest, request: Request):
        ip = client_ip(request)
        if not security.check_rate(f"login:{ip}", 10):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts")
        token, msg = security.login(body.username, body.password,
                                    user_agent=request.headers.get("user-agent", ""),
                                    client_ip=ip)
        if token is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, msg)
        user = security.get_user(body.username)
        return {"token": token, "role": user.role if user else "viewer",
                "expires_in_sec": security.session_ttl,
                "requires_totp_for_writes": True}

    @app.post("/api/auth/logout")
    def logout(session: Session = Depends(current_session)):
        return {"ok": security.logout(session.token_id)}

    @app.get("/api/auth/me")
    def me(session: Session = Depends(current_session)):
        user = security.get_user(session.username)
        return {"username": session.username, "role": session.role,
                "can_write": bool(user and user.can_write),
                "can_change_risk": bool(user and user.can_change_risk),
                "session_expires_ns": session.expires_ns}

    # -- read ---------------------------------------------------------------- #

    @app.get("/api/status")
    def get_status(session: Session = Depends(current_session)):
        data = runtime.status()
        data["api_version"] = API_VERSION
        if app.state.public_bind:
            data["security_warning"] = (
                "This dashboard is bound to a public interface. Anyone who reaches "
                "this port and obtains a session can move the account. Put it behind "
                "a VPN or an SSH tunnel.")
        return data

    @app.get("/api/positions")
    def get_positions(session: Session = Depends(current_session)):
        return {"positions": runtime.positions()}

    @app.get("/api/trades")
    def get_trades(limit: int = Query(200, ge=1, le=2000),
                         session: Session = Depends(current_session)):
        return {"trades": runtime.trades(limit)}

    @app.get("/api/performance")
    def get_performance(session: Session = Depends(current_session)):
        return runtime.performance()

    @app.get("/api/equity")
    def get_equity(limit: int = Query(2000, ge=10, le=20000),
                         session: Session = Depends(current_session)):
        return {"points": runtime.equity_series(limit)}

    @app.get("/api/decisions")
    def get_decisions(limit: int = Query(200, ge=1, le=2000),
                            action: Optional[str] = None,
                            session: Session = Depends(current_session)):
        return {"decisions": runtime.decisions(limit, action)}

    @app.get("/api/risk")
    def get_risk(session: Session = Depends(current_session)):
        return runtime.risk_view()

    @app.get("/api/execution")
    def get_execution(session: Session = Depends(current_session)):
        return runtime.execution_quality()

    @app.get("/api/config")
    def get_config(session: Session = Depends(current_session)):
        return json.loads(runtime.agent.config.to_json())

    @app.get("/api/strategies")
    def get_strategies(session: Session = Depends(current_session)):
        from ..strategy.registry import describe_all, families, load_report
        allocs = {a.name: a.model_dump(mode="json") for a in runtime.agent.config.strategies}
        # `loading` carries the plugin report. A user strategy that failed to
        # import is skipped rather than fatal, which means the only way anyone
        # finds out is if the reason is surfaced somewhere -- here.
        return {"available": describe_all(), "allocations": allocs,
                "families": families(), "loading": load_report()}

    @app.get("/api/advice")
    def get_advice(session: Session = Depends(current_session)):
        return {"pending": [d.to_dict() for d in runtime.agent.pending_advice()]}

    @app.get("/api/proposals")
    def get_proposals(session: Session = Depends(current_session)):
        return {"proposals": [p.to_dict() for p in runtime.agent.proposals.all()]}

    @app.get("/api/lessons")
    def get_lessons(include_superseded: bool = False,
                          session: Session = Depends(current_session)):
        return {"lessons": [l.to_dict()
                            for l in runtime.agent.memory.all_lessons(include_superseded)]}

    @app.get("/api/autopsies")
    def get_autopsies(strategy: Optional[str] = None,
                            limit: int = Query(200, ge=1, le=2000),
                            session: Session = Depends(current_session)):
        return {"autopsies": runtime.agent.memory.autopsies(strategy, limit)}

    @app.get("/api/audit")
    def get_audit(since_seq: int = Query(0, ge=0),
                  limit: int = Query(200, ge=1, le=2000),
                  event: Optional[str] = None,
                  tail: bool = Query(True),
                  session: Session = Depends(current_session)):
        audit = runtime.agent.audit
        if since_seq == 0 and tail:
            # The NEWEST records. Reading forward from seq 0 returned the first
            # `limit` records ever written, so after the first busy hour the
            # dashboard's journal showed nothing but the boot sequence -- the
            # most recent halt, veto or failed login was never on screen.
            from collections import deque as _deque
            window: _deque = _deque(maxlen=limit)
            for rec in audit.iter_records():
                if event and rec.get("event") != event:
                    continue
                window.append(rec)
            records = list(window)
        else:
            records = audit.read(since_seq=since_seq, event=event, limit=limit)
        ok, bad_seq, msg = _verified_chain(audit)
        return {"records": records, "chain_valid": ok, "first_bad_seq": bad_seq,
                "message": msg, "head_seq": audit.seq}

    @app.get("/api/licence")
    def get_licence(session: Session = Depends(current_session)):
        """What the licence permits, and what is wrong with it if anything.

        Readable by any authenticated role: an operator who cannot see why the
        system refuses to trade cannot do their job, and nothing here is
        secret -- the fingerprint values are hashed.
        """
        gate = getattr(runtime, "licence", None)
        if gate is None:
            return {"enforced": False,
                    "note": "this build has no licence enforcement"}
        status = gate.check()
        allowed, why = gate.may_trade_live()
        out = status.to_dict()
        out["enforced"] = not status.unlicensed_mode
        out["live_trading_allowed"] = allowed
        out["live_trading_reason"] = why
        return out

    @app.get("/api/research/latest")
    def latest_research(session: Session = Depends(current_session)):
        """The newest verdict, and whether it still describes what is running."""
        from ..research.verdicts import config_fingerprint
        rows = runtime.verdicts.list(limit=1)
        if not rows:
            return {"verdict": None}
        record = runtime.verdicts.get(rows[0]["run_id"])
        if record is None:
            return {"verdict": None}
        verdict = json.loads(record["payload"])
        allocation = next((a for a in runtime.agent.config.strategies
                           if a.name == record["strategy"]), None)
        current = bool(allocation and record.get("config_hash") == config_fingerprint(
            allocation.instruments, allocation.params, allocation.timeframe,
            runtime_config=runtime.agent.config))
        verdict["current_config"] = current
        return {"verdict": verdict}

    @app.get("/api/verdicts")
    def get_verdicts(strategy: Optional[str] = None,
                           session: Session = Depends(current_session)):
        return {"verdicts": runtime.verdicts.list(strategy)}

    @app.get("/api/health")
    def get_health(session: Session = Depends(current_session)):
        snap = runtime.agent.health.snapshot()
        return {**snap.to_dict(),
                "outage_distribution": runtime.agent.health.outage_distribution()}

    @app.get("/api/cycles")
    def get_cycles(limit: int = Query(50, ge=1, le=500),
                         session: Session = Depends(current_session)):
        return {"cycles": runtime.cycle_history[-limit:][::-1]}

    # -- write --------------------------------------------------------------- #

    # The mode dial grants the agent unattended trading authority and the kill
    # release cancels a stop the watchdog may have engaged. Both are wider levers
    # than a risk-limit edit, so both take the same owner-only gate.
    @app.post("/api/control/mode")
    def set_mode(body: ModeRequest,
                       session: Session = Depends(
                           require_write("set_mode", requires_owner=True))):
        try:
            AgentMode(body.mode)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown mode {body.mode!r}")
        try:
            return runtime.set_mode(body.mode, session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    @app.post("/api/control/kill")
    def engage_kill(body: ReasonRequest,
                          session: Session = Depends(require_write("engage_kill"))):
        return runtime.engage_kill(body.reason or "engaged from the dashboard",
                                   session.username)

    @app.post("/api/control/kill/release")
    def release_kill(session: Session = Depends(
            require_write("release_kill", requires_owner=True))):
        return runtime.release_kill(session.username)

    @app.post("/api/control/halt")
    def halt(body: ReasonRequest,
                   session: Session = Depends(require_write("halt"))):
        return runtime.halt(body.reason or "manual", session.username)

    @app.post("/api/control/resume")
    def resume(session: Session = Depends(
            require_write("resume", requires_owner=True))):
        return runtime.resume(session.username)

    @app.post("/api/control/release-guard")
    def release_guard(body: GuardRelease,
                      session: Session = Depends(
                          require_write("release_guard", requires_owner=True))):
        """Lift a performance-guard suspension. OWNER only.

        The guard suspends a strategy whose realised R is demonstrably
        negative. Before this endpoint existed the only way to lift it was a
        Python shell on the server, so an owner who had reviewed the strategy
        and wanted it back could not do so from the console that showed it.
        """
        with runtime._lock:
            ok = runtime.agent.release_guard(body.strategy, session.username)
        if not ok:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "this strategy is not suspended by the performance guard")
        return {"released": body.strategy}

    # -- manual trading --------------------------------------------------- #
    #
    # A human's ticket goes through the SAME risk engine as the agent's: the
    # stop is mandatory, the size comes from the risk budget, and every veto
    # applies. The preview is a GET because it changes nothing.

    @app.get("/api/trade/preview")
    def preview_ticket(instrument: str = Query(..., min_length=3, max_length=24,
                                               pattern=r"^[A-Z0-9_.]+$"),
                       side: str = Query(..., pattern=r"^(BUY|SELL)$"),
                       stop_loss: str = Query(..., max_length=24, pattern=_DECIMAL_TEXT),
                       take_profit: Optional[str] = Query(None, max_length=24,
                                                          pattern=_DECIMAL_TEXT),
                       risk_pct: Optional[str] = Query(None, max_length=8,
                                                       pattern=_DECIMAL_TEXT),
                       session: Session = Depends(current_session)):
        user = security.get_user(session.username)
        if user is None or not user.can_write:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "this role cannot trade")
        return runtime.manual_order(instrument=instrument, side=side, stop_loss=stop_loss,
                                    take_profit=take_profit, risk_pct=risk_pct,
                                    by=session.username, preview=True)

    @app.post("/api/trade/manual")
    def manual_ticket(body: ManualTicket,
                      session: Session = Depends(require_write("manual_trade"))):
        from ..core.config import ExecutionVenueMode
        if runtime.agent.config.execution.venue_mode is ExecutionVenueMode.LIVE:
            # Real money: owner only, on top of the TOTP the dependency checked.
            user = security.get_user(session.username)
            if user is None or not user.can_change_risk:
                raise HTTPException(status.HTTP_403_FORBIDDEN,
                                    "only the owner may place a manual trade with real money")
        return runtime.manual_order(instrument=body.instrument, side=body.side,
                                    stop_loss=body.stop_loss, take_profit=body.take_profit,
                                    risk_pct=body.risk_pct, by=session.username,
                                    preview=False)

    @app.post("/api/control/flatten")
    def flatten(session: Session = Depends(require_write("flatten_all"))):
        return runtime.flatten_all(session.username)

    @app.post("/api/control/close")
    def close_position(body: ClosePositionRequest,
                             session: Session = Depends(require_write("close_position"))):
        return runtime.close_position(body.instrument, session.username, body.lots)

    @app.post("/api/config")
    def patch_config(body: ConfigPatch,
                           session: Session = Depends(
                               require_write("update_config", requires_owner=True))):
        try:
            return runtime.update_config(body.patch, session.username)
        except Exception as exc:  # noqa: BLE001 - validation errors are user errors
            # JOURNAL THE REFUSAL. The TOTP gate logs the authorisation, and
            # the guard then raises -- so an attempt to switch the system to
            # LIVE TRADING, disable the second factor, or relocate the audit
            # journal itself looked identical in the record to an ordinary
            # risk-limit tweak. Those are the most security-relevant events
            # this system can witness, and they were the ones it did not
            # write down.
            runtime.agent.audit.append(
                EventType.WRITE_DENIED,
                {"action": "update_config", "username": session.username,
                 "refused": str(exc)[:400],
                 "attempted_paths": sorted(
                     f"{section}.{key}"
                     for section, values in (body.patch or {}).items()
                     if isinstance(values, dict) for key in values)},
                actor=session.username)
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)[:600])

    @app.post("/api/advice/accept")
    def accept_advice(body: AdviceAction,
                            session: Session = Depends(require_write("accept_advice"))):
        # Under the runtime lock: accept_advice increments the same counters the
        # decision cycle does.
        with runtime._lock:
            d = runtime.agent.accept_advice(body.client_order_id, session.username)
        if d is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such pending proposal")
        return d.to_dict()

    @app.post("/api/advice/reject")
    def reject_advice(body: AdviceAction,
                            session: Session = Depends(require_write("reject_advice"))):
        with runtime._lock:
            ok = runtime.agent.reject_advice(body.client_order_id, session.username,
                                             body.reason)
        if not ok:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such pending proposal")
        return {"rejected": True}

    @app.post("/api/proposals/review")
    def review_proposal(body: ProposalReview,
                              session: Session = Depends(
                                  require_write("review_proposal", requires_owner=True))):
        try:
            # Under the runtime lock, like its neighbours: review() is a
            # check-then-act on the proposal's status followed by a full-file
            # rewrite, racing the cycle thread's own add()/expire_stale().
            with runtime._lock:
                p = runtime.agent.proposals.review(body.proposal_id, body.approve,
                                                   session.username)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such proposal")
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
        runtime.agent.audit.append(EventType.PARAM_PROPOSAL,
                                   {"reviewed": p.id, "approved": body.approve,
                                    "reason": body.reason}, actor=session.username)
        return p.to_dict()

    @app.post("/api/cycle")
    def force_cycle(session: Session = Depends(require_write("force_cycle"))):
        return runtime.run_cycle().to_dict()

    # -- accounts ------------------------------------------------------------ #
    #
    # Every endpoint here is OWNER-ONLY, including the listing. Enumerating who
    # can log in is reconnaissance: it tells an attacker which names to spray
    # and which of them can move money. A viewer has no business seeing it.

    @app.get("/api/users")
    def list_users(session: Session = Depends(current_session)):
        user = security.get_user(session.username)
        if user is None or not user.can_change_risk:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "only an owner may see the account list")
        from .security import MIN_PASSWORD_LENGTH, ROLE_DESCRIPTIONS, ROLES
        return {"users": security.user_summaries(),
                "roles": [{"id": key, "label": ROLES[key],
                           "description": ROLE_DESCRIPTIONS[key]}
                          for key in ("owner", "operator", "viewer")],
                "min_password_length": MIN_PASSWORD_LENGTH}

    @app.post("/api/users/create")
    def create_user(body: UserCreate,
                          session: Session = Depends(
                              require_write("create_user", requires_owner=True))):
        try:
            user, uri = security.add_user(body.username, body.password, body.role,
                                          actor=session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))
        # The enrolment URI is returned ONCE and never stored anywhere the API
        # can read back. If it is lost, the answer is to rotate the factor, not
        # to look it up -- a second factor that can be re-fetched over HTTP is
        # not a second factor.
        return {"username": user.username, "role": user.role,
                "totp_uri": uri,
                "note": ("این آدرس فقط همین یک بار نشان داده می‌شود. آن را در "
                         "برنامهٔ احراز هویت کاربر ثبت کنید. اگر گم شد، باید "
                         "کد دومرحله‌ای را از نو بسازید.")}

    @app.post("/api/users/role")
    def change_role(body: UserRole,
                          session: Session = Depends(
                              require_write("change_role", requires_owner=True))):
        try:
            ok = security.set_role(body.username, body.role, actor=session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
        if not ok:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        return {"username": body.username, "role": body.role}

    @app.post("/api/users/disable")
    def disable_user(body: UserFlag,
                           session: Session = Depends(
                               require_write("disable_user", requires_owner=True))):
        try:
            ok = security.set_disabled(body.username, body.disabled,
                                       actor=session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
        if not ok:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        return {"username": body.username, "disabled": body.disabled}

    @app.post("/api/users/password")
    def reset_password(body: UserPassword,
                             session: Session = Depends(
                                 require_write("reset_password", requires_owner=True))):
        try:
            ok = security.set_password(body.username, body.password,
                                       actor=session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        if not ok:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        return {"username": body.username, "changed": True}

    @app.post("/api/users/totp")
    def rotate_totp(body: UserName,
                          session: Session = Depends(
                              require_write("rotate_totp", requires_owner=True))):
        uri = security.rotate_totp(body.username, actor=session.username)
        if uri is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        return {"username": body.username, "totp_uri": uri,
                "note": ("کد دومرحله‌ای قبلی دیگر کار نمی‌کند و همهٔ نشست‌های "
                         "این کاربر بسته شد.")}

    @app.post("/api/users/delete")
    def delete_user(body: UserName,
                          session: Session = Depends(
                              require_write("delete_user", requires_owner=True))):
        if body.username == session.username:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "حساب خودتان را پاک نکنید. اگر می‌خواهید دسترسی‌تان را کم کنید، "
                "سطح آن را عوض کنید یا از یک حساب مدیر دیگر این کار را بکنید.")
        try:
            ok = security.delete_user(body.username, actor=session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
        if not ok:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such user")
        return {"username": body.username, "deleted": True}

    # -- venues --------------------------------------------------------------- #

    # NOTE on `def` versus `async def` in this section.
    #
    # Every handler below performs a BLOCKING call: a venue round trip, a
    # terminal handshake, a SQLite write. FastAPI runs a plain `def` handler in
    # a worker thread and an `async def` handler directly on the event loop --
    # so declaring these `async` put a network call with no timeout on the one
    # thread that serves every other request in the process. A venue that
    # accepts the connection and never answers froze the whole API behind it:
    # no login, no /api/control/kill, no flatten, no websocket. These are
    # deliberately synchronous.

    @app.get("/api/brokers")
    def brokers_overview(session: Session = Depends(current_session)):
        user = security.get_user(session.username)
        return runtime.broker_overview(
            include_paths=bool(user and user.can_change_risk))

    @app.post("/api/brokers/discover")
    def discover_brokers(session: Session = Depends(
            require_write("discover_brokers", requires_owner=True))):
        # OWNER-ONLY, and it is not a cosmetic tightening: discovery attaches
        # to whatever MetaTrader terminal is running and reads the signed-in
        # account number straight out of it.
        from ..brokers.connection import discover
        return {"found": discover()}

    @app.post("/api/brokers/save")
    def save_connection(body: ConnectionSave,
                              session: Session = Depends(
                                  require_write("save_broker", requires_owner=True))):
        payload = body.model_dump()
        secret = payload.pop("secret", None)
        try:
            return runtime.save_connection(payload, session.username, secret=secret)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    @app.post("/api/brokers/test")
    def test_connection(body: ConnectionRef,
                        session: Session = Depends(
                            # OWNER-ONLY. Constructing a MetaTrader adapter
                            # signs a terminal in -- it can re-point a running
                            # session, and it launches the executable named by
                            # `terminal_path`. Neither belongs to a role
                            # documented as unable to change settings.
                            require_write("test_broker", requires_owner=True))):
        try:
            return runtime.test_connection(body.id, session.username)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such connection")
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    @app.post("/api/brokers/activate")
    def activate_connection(body: ConnectionRef,
                                  session: Session = Depends(
                                      require_write("activate_broker",
                                                    requires_owner=True))):
        try:
            return runtime.activate_connection(body.id, session.username)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such connection")
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    @app.post("/api/brokers/delete")
    def delete_connection(body: ConnectionRef,
                                session: Session = Depends(
                                    require_write("delete_broker",
                                                  requires_owner=True))):
        try:
            return runtime.delete_connection(body.id, session.username)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such connection")
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    # -- licence -------------------------------------------------------------- #

    @app.get("/api/licence/fingerprint")
    def licence_fingerprint(session: Session = Depends(current_session)):
        """What to send the vendor so a licence can be issued for this machine.

        Owner-only: the fingerprint identifies the installation, and while the
        values are hashed, handing them to every viewer is handing out the one
        input a licence is bound to.
        """
        user = security.get_user(session.username)
        if user is None or not user.can_change_risk:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "only an owner may read the machine fingerprint")
        from ..licensing.fingerprint import fingerprint_components, machine_fingerprint
        return {"fingerprint": machine_fingerprint(),
                "components": fingerprint_components(),
                "note": ("این مقادیر هش‌شده‌اند و چیزی از محتوای سرور شما لو "
                         "نمی‌دهند. آن‌ها را برای فروشنده بفرستید تا لایسنس "
                         "مخصوص همین دستگاه صادر شود.")}

    @app.post("/api/licence/install")
    def install_licence(body: LicenceInstall,
                              session: Session = Depends(
                                  require_write("install_licence",
                                                requires_owner=True))):
        """Validate a pasted licence, then write it to disk.

        Validation happens BEFORE the file is replaced. Writing first and
        checking afterwards would let a mistyped paste destroy a working
        licence, and the customer would discover it at the next restart.
        """
        gate = getattr(runtime, "licence", None)
        if gate is None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "this build has no licence enforcement")
        from ..licensing.license import LicenseError, verify
        document = body.document.strip()
        if not gate.public_key:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "این نسخه کلید عمومی فروشنده را ندارد، پس لایسنس قابل بررسی نیست.")
        try:
            licence = verify(document, gate.public_key,
                             grace_days=gate.grace_days,
                             current_fingerprint=getattr(gate, "_fingerprint", None))
        except LicenseError as exc:
            runtime.agent.audit.append(
                EventType.WRITE_DENIED,
                {"action": "install_licence", "refused": str(exc)[:300]},
                actor=session.username)
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

        path = Path(gate.licence_path)
        backup = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.exists():
                backup = path.with_suffix(path.suffix + ".previous")
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                os.chmod(backup, 0o600)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(document + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                                f"could not write the licence file: {exc}")
        runtime.agent.audit.append(
            EventType.CONFIG_CHANGE,
            {"action": "licence_installed", "licence_id": licence.licence_id,
             "tier": licence.tier, "expires_at": licence.expires_at,
             "term_months": licence.term_months, "term_index": licence.term_index},
            actor=session.username)
        status_after = gate.check(force=True)
        return {"installed": True, "licence": status_after.to_dict(),
                "previous_kept_at": str(backup) if backup else None}

    # -- AI assistants and news ---------------------------------------------- #

    def _ai_or_409():
        ai = getattr(runtime, "ai", None)
        if ai is None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "the AI assistants are not enabled in this build")
        return ai

    @app.get("/api/ai")
    def ai_overview(session: Session = Depends(current_session)):
        ai = getattr(runtime, "ai", None)
        if ai is None:
            return {"enabled": False}
        user = security.get_user(session.username)
        return {"enabled": True,
                **ai.describe(include_private=bool(user and user.can_change_risk))}

    @app.post("/api/ai/provider")
    def ai_save_provider(body: AIProviderSave,
                         session: Session = Depends(
                             require_write("ai_provider", requires_owner=True))):
        ai = _ai_or_409()
        try:
            return ai.save_provider(body.provider, enabled=body.enabled, model=body.model,
                                    base_url=body.base_url, api_key=body.api_key,
                                    by=session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)[:300])

    @app.post("/api/ai/settings")
    def ai_save_settings(body: AISettingsSave,
                         session: Session = Depends(
                             require_write("ai_settings", requires_owner=True))):
        ai = _ai_or_409()
        try:
            return ai.save_settings(primary=body.primary, fallbacks=body.fallbacks,
                                    purposes=body.purposes,
                                    max_calls_per_hour=body.max_calls_per_hour,
                                    max_calls_per_day=body.max_calls_per_day,
                                    by=session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)[:300])

    @app.post("/api/ai/test")
    def ai_test(body: AIProviderRef,
                session: Session = Depends(require_write("ai_test", requires_owner=True))):
        ai = _ai_or_409()
        from ..ai import CATALOG
        if body.provider not in CATALOG:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such provider")
        return ai.test_provider(body.provider, by=session.username)

    @app.get("/api/ai/insights")
    def ai_insights(session: Session = Depends(current_session)):
        ai = getattr(runtime, "ai", None)
        coach = getattr(runtime, "coach", None)
        desk = getattr(runtime, "news_desk", None)
        return {
            "reviews": ai.store.reviews(40) if ai is not None else [],
            "themes": coach.themes() if coach is not None else None,
            "brief": ai.store.latest_brief() if ai is not None else None,
            "headlines": desk.headlines(40) if desk is not None else [],
            "desk": desk.status.to_dict() if desk is not None else None,
            "background_errors": list(getattr(runtime, "background_errors", []))[-5:],
        }

    @app.post("/api/ai/brief")
    def ai_brief(session: Session = Depends(require_write("ai_brief"))):
        ai = _ai_or_409()
        from ..ai.coach import generate_brief
        try:
            return generate_brief(ai, runtime)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)[:300])

    # -- the System One model: authority, versions, calibration ----------------- #

    @app.get("/api/ai/jev")
    def ai_jev(session: Session = Depends(current_session)):
        return _ai_or_409().jev_report()

    @app.post("/api/ai/jev/mode")
    def ai_jev_mode(body: JevModeSave,
                    session: Session = Depends(require_write("ai_jev_mode",
                                                             requires_owner=True))):
        try:
            return _ai_or_409().set_jev_mode(body.mode, session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)[:400])

    @app.post("/api/ai/jev/accept-version")
    def ai_jev_accept(session: Session = Depends(require_write("ai_jev_version",
                                                               requires_owner=True))):
        try:
            return _ai_or_409().accept_jev_version(session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)[:400])

    @app.post("/api/ai/jev/labels")
    def ai_jev_labels(body: JevLabels,
                      session: Session = Depends(require_write("ai_jev_labels",
                                                               requires_owner=True))):
        # One second-factor code for a whole batch: codes are single-use, and
        # labelling twenty headlines one code at a time is how nobody labels.
        try:
            return _ai_or_409().save_jev_labels(
                [lab.model_dump() for lab in body.labels], session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)[:300])

    @app.post("/api/news/refresh")
    def news_refresh(session: Session = Depends(require_write("news_refresh"))):
        desk = getattr(runtime, "news_desk", None)
        if desk is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "live news feeds are off")
        now = wall_ns()
        report = desk.refresh_calendar(now)
        desk.refresh_feeds(now)
        return {"calendar": report, "desk": desk.status.to_dict()}

    # -- the brain ------------------------------------------------------------ #

    def _brain_or_409():
        brain = getattr(runtime, "brain", None)
        if brain is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "the brain is not wired in this build")
        return brain

    @app.get("/api/brain")
    def brain_overview(session: Session = Depends(current_session)):
        return runtime.brain_view()

    @app.post("/api/brain/settings")
    def brain_settings(body: BrainSave,
                       session: Session = Depends(require_write("brain_settings",
                                                                requires_owner=True))):
        _brain_or_409()
        try:
            result = runtime.update_config({"brain": body.patch}, session.username,
                                           replace={("brain", "stress_scenarios")})
        except Exception as exc:  # noqa: BLE001 - validation errors are user errors
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)[:400])
        return {**result, "brain": runtime.brain_view()}

    @app.post("/api/brain/lab/run")
    def brain_lab_run(session: Session = Depends(require_write("brain_lab",
                                                               requires_owner=True))):
        brain = _brain_or_409()
        if not brain.start_lab(by=session.username):
            raise HTTPException(status.HTTP_409_CONFLICT, "the lab is already running")
        return {"started": True}

    @app.post("/api/brain/model/approve")
    def brain_model_approve(body: BrainModelRef,
                            session: Session = Depends(require_write("brain_model",
                                                                     requires_owner=True))):
        try:
            return _brain_or_409().approve_model(body.model_id, session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)[:300])

    @app.post("/api/brain/model/retire")
    def brain_model_retire(session: Session = Depends(require_write("brain_model",
                                                                    requires_owner=True))):
        try:
            return _brain_or_409().retire_model(session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)[:300])

    @app.post("/api/brain/cooldown/clear")
    def brain_cooldown_clear(body: BrainCooldownRef,
                             session: Session = Depends(require_write("brain_cooldown",
                                                                      requires_owner=True))):
        try:
            return _brain_or_409().clear_cooldown(body.scope, session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)[:300])

    # -- notifications: Telegram and Bale ------------------------------------- #

    def _notifier_or_409():
        n = getattr(runtime, "notifier", None)
        if n is None:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "notifications are not wired in this build")
        return n

    @app.get("/api/notify")
    def notify_overview(session: Session = Depends(current_session)):
        n = getattr(runtime, "notifier", None)
        if n is None:
            return {"available": False}
        user = security.get_user(session.username)
        return {"available": True,
                **n.describe(include_private=bool(user and user.can_change_risk))}

    @app.post("/api/notify/channel")
    def notify_channel(body: NotifyChannelSave,
                       session: Session = Depends(require_write("notify_channel",
                                                                requires_owner=True))):
        try:
            return _notifier_or_409().save_channel(
                body.channel, enabled=body.enabled, chat_id=body.chat_id,
                categories=body.categories, commands=body.commands, token=body.token,
                daily_hour_utc=body.daily_hour_utc, by=session.username)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)[:300])

    @app.post("/api/notify/test")
    def notify_test(body: NotifyChannelRef,
                    session: Session = Depends(require_write("notify_test",
                                                             requires_owner=True))):
        return _notifier_or_409().test(body.channel, by=session.username)

    @app.post("/api/notify/discover")
    def notify_discover(body: NotifyChannelRef,
                        session: Session = Depends(require_write("notify_discover",
                                                                 requires_owner=True))):
        return _notifier_or_409().discover(body.channel)

    # -- macro context: dollar index and COT ----------------------------------- #

    @app.get("/api/macro")
    def macro_overview(session: Session = Depends(current_session)):
        return runtime.macro_view()

    @app.post("/api/macro/settings")
    def macro_settings(body: SectionPatch,
                       session: Session = Depends(require_write("macro_settings",
                                                                requires_owner=True))):
        if getattr(runtime, "macro", None) is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "the macro desk is not wired")
        try:
            result = runtime.update_config({"macro": body.patch}, session.username)
        except Exception as exc:  # noqa: BLE001 - validation errors are user errors
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)[:400])
        return {**result, "macro": runtime.macro_view()}

    @app.post("/api/macro/cot/refresh")
    def macro_cot_refresh(session: Session = Depends(require_write("macro_cot",
                                                                   requires_owner=True))):
        macro = getattr(runtime, "macro", None)
        if macro is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "the macro desk is not wired")
        # HTTP to the CFTC only -- never the MetaTrader terminal -- so it is
        # safe on this thread.
        return macro.refresh_cot(by=session.username)

    # -- the MetaTrader terminal watchdog ------------------------------------ #

    @app.get("/api/terminal")
    def terminal_overview(session: Session = Depends(current_session)):
        wd = getattr(runtime.agent, "terminal_watchdog", None)
        return {"available": wd is not None,
                "config": runtime.agent.config.ops.terminal_watchdog.model_dump(mode="json"),
                **(wd.status() if wd is not None else {})}

    @app.post("/api/terminal/settings")
    def terminal_settings(body: SectionPatch,
                          session: Session = Depends(require_write("terminal_watchdog",
                                                                   requires_owner=True))):
        try:
            result = runtime.update_config({"ops": {"terminal_watchdog": body.patch}},
                                           session.username)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)[:400])
        return result

    # -- independent reference price (TradingView) ------------------------------ #

    @app.get("/api/reference")
    def reference_overview(session: Session = Depends(current_session)):
        return runtime.reference_view()

    @app.post("/api/reference/settings")
    def reference_settings(body: ReferenceSave,
                           session: Session = Depends(
                               require_write("reference_settings", requires_owner=True))):
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        if not patch:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing to change")
        try:
            result = runtime.update_config({"reference": patch}, session.username,
                                           replace={("reference", "symbol_map")})
        except Exception as exc:  # noqa: BLE001 - validation errors are user errors
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)[:400])
        desk = getattr(runtime, "reference", None)
        if desk is not None:
            try:
                # Start/stop and resubscribe now; the ratings (an outbound
                # request of up to 15 s) wait for the background worker.
                desk.tick(with_ta=False)
            except Exception:  # noqa: BLE001 - applied at the next background tick
                pass
        return {**result, "reference": runtime.reference_view()}

    @app.post("/api/reference/refresh")
    def reference_refresh(session: Session = Depends(require_write("reference_refresh"))):
        desk = getattr(runtime, "reference", None)
        if desk is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "the reference service is not wired")
        if not runtime.agent.config.reference.enabled:
            raise HTTPException(status.HTTP_409_CONFLICT, "the reference price is switched off")
        desk.tick(force_ta=True)
        return runtime.reference_view()

    _search_times: List[float] = []

    @app.get("/api/reference/search")
    def reference_search(q: str = Query(min_length=1, max_length=40),
                         kind: str = Query(default="forex", max_length=12),
                         session: Session = Depends(current_session)):
        user = security.get_user(session.username)
        if user is None or not user.can_change_risk:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "owner only")
        # Each search is an outbound request on the owner's behalf; ten a
        # minute is plenty for picking a symbol and useless for anything else.
        now = time.monotonic()
        _search_times[:] = [t for t in _search_times if now - t < 60]
        if len(_search_times) >= 10:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many searches")
        _search_times.append(now)
        from ..data.tradingview import TradingViewError, search_symbols
        try:
            return {"results": search_symbols(q, kind)}
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)[:200])
        except TradingViewError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)[:200])

    # -- live stream ---------------------------------------------------------- #

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        # The bearer token travels in the Sec-WebSocket-Protocol header, never
        # in the URL: a query string lands in access logs, proxies and browser
        # history, and a session token in any of those is a session for whoever
        # reads them. Browsers cannot set arbitrary headers on a WebSocket, but
        # they can offer subprotocols, so "auth.<token>" is the carrier.
        offered = [v.strip() for v in
                   websocket.headers.get("sec-websocket-protocol", "").split(",") if v.strip()]
        token = next((v[5:] for v in offered if v.startswith("auth.")), "")
        if "sentinel-v1" not in offered or not token:
            await websocket.close(code=4401)
            return
        session = security.verify_token(
            token, user_agent=websocket.headers.get("user-agent", ""),
            client_ip=websocket.client.host if websocket.client else "unknown")
        if session is None:
            await websocket.close(code=4401)
            return
        # Cap concurrent streams. Each pins a queue and is walked on every
        # cycle broadcast, so one token opening 80 of them is a cheap way to
        # slow the engine down.
        if runtime.subscriber_count() >= _MAX_WS_SUBSCRIBERS:
            await websocket.close(code=4429)
            return
        await websocket.accept(subprotocol="sentinel-v1")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)

        def on_message(msg: dict) -> None:
            # Called from the engine thread; hop to the event loop safely, and
            # drop rather than block if the client is slow.
            def put() -> None:
                if not queue.full():
                    queue.put_nowait(msg)
            loop.call_soon_threadsafe(put)

        ua = websocket.headers.get("user-agent", "")
        ip = websocket.client.host if websocket.client else "unknown"

        def still_authorised() -> bool:
            """Re-check the session on every send.

            Authenticating only at the handshake meant logout, account
            disablement and session expiry all left an established stream
            pushing equity, positions and decisions indefinitely.
            """
            return security.verify_token(token, user_agent=ua, client_ip=ip) is not None

        runtime.subscribe(on_message)
        try:
            # runtime.status() reads the account from the venue. On the event
            # loop, a venue that accepts the connection and never answers
            # froze every other request in the process -- including the kill
            # switch -- so it runs in a worker thread like the HTTP handlers.
            status_now = await asyncio.to_thread(runtime.status)
            await websocket.send_json({"type": "status", "data": status_now})
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                    if not still_authorised():
                        await websocket.close(code=4401)
                        return
                    await websocket.send_json(msg)
                except asyncio.TimeoutError:
                    if not still_authorised():
                        await websocket.close(code=4401)
                        return
                    await websocket.send_json({"type": "heartbeat",
                                               "data": {"ts_ns": wall_ns()}})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            runtime.unsubscribe(on_message)

    # -- dashboard ------------------------------------------------------------ #

    if dashboard_dir and Path(dashboard_dir).is_dir():
        app.mount("/", StaticFiles(directory=dashboard_dir, html=True), name="dashboard")

    return app
