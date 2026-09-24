"""The agent.

One decision cycle, in order:

    kill switch -> health -> reconcile -> market snapshot -> regime ->
    manage open positions -> generate signals -> meta filter -> risk ->
    act according to mode -> record -> learn

Every step can veto the ones after it, and the order is not arbitrary: the
cheapest and most consequential checks come first, so that a system in a bad
state spends no time reasoning about opportunities it is not allowed to take.

The four modes are a single dial on how much authority the agent has, and the
risk engine is in the path of all four:

* ``OBSERVE``     thinks, records, places nothing.
* ``ADVISORY``    emits proposals for a human.
* ``SEMI_AUTO``   acts inside a pre-approved envelope; anything outside it
                  becomes an advisory proposal instead of being skipped.
* ``AUTONOMOUS``  acts without per-trade approval, still inside every limit.

What the agent cannot do in any mode: widen a risk limit, promote its own
strategy, release the kill switch, or trade a strategy that has not passed
acceptance with real money.

Every decision produces an explanation with the inputs, the vetoes, the
applicable lessons and the numbers behind them, so the dashboard can always
answer "why did you (not) do that?".
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

from ..brokers.base import Broker
from ..core.audit import AuditLog, EventType
from ..core.clock import wall_ns
from ..core.config import AgentMode, ExecutionVenueMode, SentinelConfig
from ..core.errors import UnknownOutcomeError
from ..core.ids import client_order_id
from ..core.money import CostModel, D, ZERO, dec
from ..core.types import (
    ClosedTrade, DataQuality, OrderIntent, OrderState, Position, Quote, Side, Signal,
)
from ..data.feed import MarketFeed
from ..execution.oms import OrderManager
from ..execution.reconcile import Reconciler
from ..ops.health import HealthMonitor
from ..ops.killswitch import Heartbeat, KillSwitch
from ..risk.engine import RiskContext, RiskDecision, RiskEngine, Severity
from ..risk.protect import (ProtectAction, evaluate_protection, time_stop,
                            weekend_flat)
from ..strategy.base import Strategy
from ..strategy.registry import build as build_strategy, can_trade
from ..news.policy import NewsAssessment, NewsPolicy
from .memory import Lesson, MemoryStore
from .postmortem import aggregate, autopsy
from .proposals import ProposalQueue, derive_proposals, regime_lessons
from .regime import RegimeState, classify


# How many spread observations to keep per instrument per session bucket, and
# how many are needed before the median is trusted instead of the live value.
_SPREAD_WINDOW = 500
_SPREAD_MIN_SAMPLES = 30
# Equity marks kept for the rolling-24h loss budget: at most one per bucket,
# so 24h needs 288 of them whatever the decision interval.
_EQUITY_MARK_WINDOW = 400
_EQUITY_MARK_BUCKET_NS = 300 * 1_000_000_000
# Seconds per bar, used to turn a signal's horizon in bars into a time stop.
_TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800,
}


@dataclass
class Decision:
    """One considered action, whether or not it happened."""

    ts_ns: int
    strategy: str
    instrument: str
    action: str                       # proposed | executed | vetoed | skipped | queued
    side: Optional[str] = None
    lots: Optional[str] = None
    entry: Optional[str] = None
    stop: Optional[str] = None
    target: Optional[str] = None
    risk_amount: Optional[str] = None
    risk_pct: Optional[str] = None
    signal_strength: float = 0.0
    regime: str = ""
    vetoes: List[dict] = field(default_factory=list)
    warnings: List[dict] = field(default_factory=list)
    lessons: List[str] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    explanation: str = ""
    client_order_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class CycleReport:
    ts_ns: int
    mode: str
    halted: bool
    kill_switch: bool
    regime: Optional[dict]
    equity: str
    decisions: List[Decision] = field(default_factory=list)
    protections: List[dict] = field(default_factory=list)
    alarms: List[dict] = field(default_factory=list)
    health: Dict[str, Any] = field(default_factory=dict)
    reconcile: Optional[dict] = None
    lessons_learned: int = 0
    proposals_created: int = 0
    errors: List[str] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ts_ns": self.ts_ns, "mode": self.mode, "halted": self.halted,
            "kill_switch": self.kill_switch, "regime": self.regime, "equity": self.equity,
            "decisions": [d.to_dict() for d in self.decisions],
            "protections": self.protections, "alarms": self.alarms, "health": self.health,
            "reconcile": self.reconcile, "lessons_learned": self.lessons_learned,
            "proposals_created": self.proposals_created, "errors": self.errors,
            "duration_ms": round(self.duration_ms, 1),
        }


class Agent:
    def __init__(
        self,
        config: SentinelConfig,
        broker: Broker,
        feed: MarketFeed,
        audit: AuditLog,
        memory: MemoryStore,
        *,
        proposals: Optional[ProposalQueue] = None,
        strategies: Optional[Dict[str, Strategy]] = None,
        on_cycle: Optional[Callable[[CycleReport], None]] = None,
        clock_fn: Optional[Callable[[], int]] = None,
        news: Optional[NewsPolicy] = None,
        meta_gate: Any = None,
        entry_gate: Optional[Callable[[], tuple]] = None,
    ) -> None:
        self.config = config
        #: ``() -> (allowed, reason)``, asked before any NEW live risk is opened
        #: (see entry_permission). Normally LicenseGate.may_trade_live.
        self.entry_gate = entry_gate
        self.broker = broker
        self.feed = feed
        self.audit = audit
        self.memory = memory
        self.proposals = proposals or ProposalQueue(f"{config.ops.state_dir}/proposals.json")
        self.on_cycle = on_cycle
        # Injectable clock: the wall clock in production, the simulated clock in
        # an accelerated replay. Everything time-dependent goes through self.now().
        self._clock_fn = clock_fn or wall_ns
        self.news = news
        self._news_assessment: Dict[str, NewsAssessment] = {}
        # The meta-label filter: a fitted act/skip model consulted BEFORE the
        # risk engine. Injected, or loaded from agent.meta_model_path. A model
        # that cannot be loaded passes everything through and says so once.
        self.meta_gate = meta_gate
        if self.meta_gate is None and config.agent.meta_model_path:
            try:
                from ..research.metalabel import MetaGate
                self.meta_gate = MetaGate.load(config.agent.meta_model_path)
                audit.append(EventType.SYSTEM_START, {
                    "meta_model": config.agent.meta_model_path,
                    "sha256": self.meta_gate.sha256,
                    "threshold": self.meta_gate.labeler.report.threshold})
            except Exception as exc:  # noqa: BLE001 - a broken filter is not a stopped agent
                audit.append(EventType.SYSTEM_START, {
                    "meta_model": config.agent.meta_model_path,
                    "load_failed": str(exc)[:200],
                    "effect": "every primary signal passes through unfiltered"})

        self.risk = RiskEngine(config.risk)
        self.oms = OrderManager(broker, audit,
                                submit_timeout_ms=config.execution.submit_timeout_ms,
                                max_retries=config.execution.max_submit_retries,
                                retry_backoff_ms=config.execution.retry_backoff_ms,
                                max_slippage_pips=config.execution.max_slippage_pips)
        self.reconciler = Reconciler(broker, audit)
        self.health = HealthMonitor(audit, max_clock_skew_ms=config.risk.max_clock_skew_ms)
        self.kill = KillSwitch(config.ops.killswitch_file, audit)
        self.heartbeat = Heartbeat(f"{config.ops.state_dir}/heartbeat.json",
                                   config.ops.heartbeat_interval_sec)

        self.strategies: Dict[str, Strategy] = strategies or {}
        if not self.strategies:
            for alloc in config.strategies:
                if alloc.enabled:
                    try:
                        self.strategies[alloc.name] = build_strategy(alloc.name, **alloc.params)
                    except Exception as exc:  # noqa: BLE001
                        audit.append(EventType.SYSTEM_START,
                                     {"strategy_load_error": alloc.name, "error": str(exc)})

        self.state_path = Path(config.ops.state_dir) / "agent_state.json"
        self.halted = False
        self.halt_reason = ""
        self.regime: Optional[RegimeState] = None
        self.decisions: List[Decision] = []
        self.cycles = 0
        self.last_reconcile_ns = 0
        self.equity_peak: Decimal = ZERO
        # Marked-to-market peak, shown in the dashboard but NOT used by the
        # ladder or the halt -- see _build_context.
        self.equity_peak_marked: Decimal = ZERO
        # Latches for the rest of the venue day once the profit lock trips.
        self.day_profit_locked: bool = False
        # Current drawdown-ladder rung, carried across cycles so the hysteresis
        # band has something to be hysteretic about.
        self._ladder_rung: int = 0
        self.day_start_equity: Decimal = ZERO
        self.week_start_equity: Decimal = ZERO
        self.month_start_equity: Decimal = ZERO
        self._day_key: Optional[int] = None
        self._week_key: Optional[tuple] = None
        self._month_key: Optional[tuple] = None
        self._year_key: Optional[int] = None
        self.trades_today = self.trades_week = self.trades_year = 0
        # instrument -> risk metadata the venue does not return (initial risk,
        # scale-out flags). Live adapters rebuild Position objects on every call,
        # so without this the protection layer has no R to measure against and
        # silently does nothing.
        self._position_meta: Dict[str, Dict[str, Any]] = {}
        # (instrument, session bucket) -> recent observed spreads, so the spread
        # veto compares the live value against a real baseline.
        self._spread_history: Dict[tuple, deque] = {}
        # Rolling 24h equity marks, so a loss straddling the calendar-day
        # boundary cannot be granted two full daily budgets.
        self._equity_marks: deque = deque(maxlen=_EQUITY_MARK_WINDOW)
        self.last_entry_ns: Optional[int] = None
        self.processed_trades: set[str] = set()
        # instrument -> the regime observed when the position was opened. Kept
        # after the position closes so the post-mortem can attribute correctly.
        self._entry_regime: Dict[str, str] = {}
        self._trade_cursor: str = ""
        self._warned_no_history = False
        self._pending_signals: List[Signal] = []
        self._advisory_queue: List[Decision] = []
        self._seq = 0
        #: strategy -> reason, set by the performance guard. A suspended
        #: strategy opens nothing until a human clears it; positions it already
        #: holds are still managed.
        self._guard_suspended: Dict[str, str] = {}
        self._licence_block_logged = False
        #: Insertion order of processed_trades, so the bounded copy written to
        #: disk keeps the MOST RECENT ids. Sorting the set kept the
        #: lexicographically largest, which for numeric venue tickets is not
        #: the newest ("999" sorts after "1000").
        self._processed_order: deque = deque(maxlen=5000)
        #: The owner's cross-account ledger, when this engine is in a group.
        self._group = None
        if config.ops.group_ledger_dir:
            from ..risk.portfolio import GroupLedger
            self._group = GroupLedger(
                config.ops.group_ledger_dir,
                config.execution.expected_account_id or broker.capabilities.name,
                stale_after_sec=config.risk.group_ledger_stale_sec)
        self._group_view = None

    def now(self) -> int:
        return int(self._clock_fn())

    # ------------------------------------------------------------------ #
    # durable risk state
    # ------------------------------------------------------------------ #

    def _save_state(self) -> None:
        """Persist the counters every loss budget is measured against.

        Holding the equity peak and the period baselines only in memory means a
        restart -- including a crash loop -- resets the drawdown ladder, the
        max-drawdown halt and the daily loss budget to zero. An agent that has
        just lost 8% would come back believing it was flat.
        """
        payload = {
            "equity_peak": str(self.equity_peak),
            "equity_peak_marked": str(self.equity_peak_marked),
            "day_profit_locked": self.day_profit_locked,
            "ladder_rung": self._ladder_rung,
            "day_start_equity": str(self.day_start_equity),
            "week_start_equity": str(self.week_start_equity),
            "month_start_equity": str(self.month_start_equity),
            "day_key": self._day_key, "week_key": list(self._week_key or ()),
            "month_key": list(self._month_key or ()), "year_key": self._year_key,
            "trades_today": self.trades_today, "trades_week": self.trades_week,
            "trades_year": self.trades_year, "last_entry_ns": self.last_entry_ns,
            "halted": self.halted, "halt_reason": self.halt_reason,
            "position_meta": {k: {kk: str(vv) for kk, vv in v.items()}
                              for k, v in self._position_meta.items()},
            "entry_regime": dict(self._entry_regime),
            # Bounded: only the most recent ids are needed to avoid re-autopsying
            # on restart, and an unbounded set would grow without limit.
            "processed_trades": self._bounded_processed(),
            "equity_marks": [[ns, str(eq)] for ns, eq in self._equity_marks],
            "trade_cursor": self._trade_cursor,
            "guard_suspended": dict(self._guard_suspended),
            "saved_ns": self.now(),
        }
        try:
            import os as _os
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            # fsync before the rename: the peak equity and the ladder rung are
            # what a restart measures every loss budget against, and a rename
            # of an unflushed file can survive a power loss as an empty file.
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.flush()
                _os.fsync(handle.fileno())
            _os.replace(tmp, self.state_path)
        except OSError as exc:  # pragma: no cover - disk failure
            # A state that cannot be persisted is a drawdown budget that a
            # restart will forget. Refuse new risk until a human looks.
            self.audit.append(EventType.SYSTEM_START, {"state_save_failed": str(exc)})
            self.halted = True
            self.halt_reason = f"state persistence failed ({exc}); new entries refused"

    def _load_state(self) -> bool:
        if not self.state_path.exists():
            return False
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Unreadable is NOT "fresh". A fresh state resets the equity peak,
            # the ladder rung and every period baseline to zero -- an agent
            # 8% down would resume at full size. Halt, and say what to restore.
            self.audit.append(EventType.SYSTEM_START, {"state_load_failed": str(exc)})
            self.halted = True
            self.halt_reason = (f"agent state at {self.state_path} is unreadable ({exc}); "
                                "restore it from a backup before resuming")
            return False
        self.equity_peak = dec(data.get("equity_peak", 0))
        self.equity_peak_marked = dec(data.get("equity_peak_marked", 0))
        self.day_profit_locked = bool(data.get("day_profit_locked", False))
        # The ladder rung is persisted so a restart does not re-derive it from a
        # drawdown that happens to sit on a boundary -- with hysteresis, the
        # rung is state, not a pure function of the current drawdown.
        rung = data.get("ladder_rung")
        self._ladder_rung = int(rung) if rung is not None else 0
        self.day_start_equity = dec(data.get("day_start_equity", 0))
        self.week_start_equity = dec(data.get("week_start_equity", 0))
        self.month_start_equity = dec(data.get("month_start_equity", 0))
        self._day_key = data.get("day_key")
        self._week_key = tuple(data.get("week_key") or ()) or None
        self._month_key = tuple(data.get("month_key") or ()) or None
        self._year_key = data.get("year_key")
        self.trades_today = int(data.get("trades_today", 0))
        self.trades_week = int(data.get("trades_week", 0))
        self.trades_year = int(data.get("trades_year", 0))
        self.last_entry_ns = data.get("last_entry_ns")
        # A halt survives a restart. Restarting is not how a halt is cleared;
        # a human is.
        if data.get("halted"):
            self.halted = True
            self.halt_reason = data.get("halt_reason", "carried over from a previous run")
        self._entry_regime = dict(data.get("entry_regime") or {})
        ids = [str(x) for x in (data.get("processed_trades") or [])]
        self.processed_trades = set(ids)
        self._processed_order.clear()
        self._processed_order.extend(ids)
        # The rolling-24h loss window survives a restart. Held only in memory,
        # a restart -- including a crash loop -- granted a fresh 24h budget
        # immediately after a loss that had used most of it.
        self._equity_marks.clear()
        for row in data.get("equity_marks") or []:
            try:
                self._equity_marks.append((int(row[0]), dec(row[1])))
            except (TypeError, ValueError, IndexError, InvalidOperation):
                continue
        self._trade_cursor = str(data.get("trade_cursor") or "")
        for sym, meta in (data.get("position_meta") or {}).items():
            self._position_meta[sym] = {
                "initial_risk": dec(meta.get("initial_risk", 0)),
                "strategy": meta.get("strategy", ""),
                "opened_ns": int(meta.get("opened_ns", 0) or 0),
                "side": meta.get("side", ""),
                "client_order_id": meta.get("client_order_id", ""),
                "regime": meta.get("regime", ""),
                "provisional": meta.get("provisional") == "True",
                "partial_taken": meta.get("partial_taken") == "True",
                "breakeven_moved": meta.get("breakeven_moved") == "True",
                # The rest of what the venue cannot tell us. Dropping these on
                # restart made _local_book() report every position at zero lots
                # and zero entry (a size_drift for the reconciler), reset the
                # give-back ratchet's peak (re-arming a stop already tightened)
                # and lost the horizon stop.
                "lots": dec(meta.get("lots", 0) or 0),
                "entry_price": dec(meta.get("entry_price", 0) or 0),
                "stop_loss": (dec(meta["stop_loss"]) if meta.get("stop_loss")
                              not in (None, "", "None") else None),
                "max_favourable": dec(meta.get("max_favourable", 0) or 0),
                "max_adverse": dec(meta.get("max_adverse", 0) or 0),
                "max_hold_sec": int(float(meta.get("max_hold_sec", 0) or 0)),
                "intended_risk": dec(meta.get("intended_risk", 0) or 0),
            }
        self._guard_suspended = {str(k): str(v) for k, v in
                                 (data.get("guard_suspended") or {}).items()}
        self.audit.append(EventType.SYSTEM_START, {
            "state_restored": True, "equity_peak": str(self.equity_peak),
            "drawdown_budget_carried": True, "halted": self.halted,
            "guard_suspended": sorted(self._guard_suspended)})
        return True

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self.audit.append(EventType.SYSTEM_START, {
            "mode": self.config.agent.mode.value,
            "venue_mode": self.config.execution.venue_mode.value,
            "broker": self.broker.capabilities.name,
            "strategies": sorted(self.strategies),
            "capability_degradations": self.broker.capabilities.degradation_report(),
        })
        self.heartbeat.start()
        restored = self._load_state()

        # Replay our own journal for orders that were sent but never reached a
        # terminal state, and resolve each by QUERY. Without this a crash between
        # "sent" and "filled" leaves a position nobody is tracking.
        for coid in self._unterminated_orders_from_journal():
            try:
                res = self.broker.query_order(coid)
            except Exception as exc:  # noqa: BLE001
                res = None
                self.audit.append(EventType.ORDER_UNKNOWN,
                                  {"client_order_id": coid, "replay_query_failed": str(exc)})
            self.audit.append(EventType.RECONCILE, {
                "client_order_id": coid, "journal_replay": True,
                "resolved_to": res.state.value if res else "unresolved"})
            if res is None:
                self.halt(f"order {coid} was sent before the last restart and the venue "
                          "has no record of it; a human must confirm the account state")

        # A restart ALWAYS reconciles before anything else. The account state we
        # remember is a cache, and it is stale by assumption.
        report = self.reconciler.reconcile([], unresolved_order_ids=[])
        if report.halt_required:
            self.halt("startup reconciliation found a state that needs a human: "
                      + "; ".join(m.kind for m in report.mismatches))
        acct = self.broker.account()
        if not restored:
            self.equity_peak = acct.balance
            self.day_start_equity = acct.equity
            self.week_start_equity = acct.equity
            self.month_start_equity = acct.equity
        else:
            # Never let a restart RAISE the peak; the drawdown budget only
            # resets when a new high is genuinely made.
            self.equity_peak = max(self.equity_peak, ZERO)
            if self.equity_peak <= 0:
                self.equity_peak = acct.equity
            for attr in ("day_start_equity", "week_start_equity", "month_start_equity"):
                if getattr(self, attr) <= 0:
                    setattr(self, attr, acct.equity)
        self._save_state()

    def _unterminated_orders_from_journal(self) -> List[str]:
        """Client order ids that were journalled as sent and never resolved."""
        sent: set[str] = set()
        done: set[str] = set()
        terminal = {EventType.ORDER_FILLED.value, EventType.ORDER_REJECTED.value,
                    EventType.ORDER_CANCELLED.value}
        try:
            for rec in self.audit.iter_records():
                coid = (rec.get("payload") or {}).get("client_order_id")
                if not coid:
                    continue
                if rec["event"] == EventType.ORDER_SENT.value:
                    sent.add(coid)
                elif rec["event"] in terminal:
                    done.add(coid)
        except (OSError, ValueError) as exc:  # unreadable is not empty
            self.halt(f"the order journal cannot be read ({exc}); orders sent before the "
                      "last restart cannot be resolved, so no new risk is taken")
            return []
        return sorted(sent - done)

    def stop(self) -> None:
        self._save_state()
        self.heartbeat.stop()
        self.audit.append(EventType.SYSTEM_STOP, {"cycles": self.cycles})

    def halt(self, reason: str) -> None:
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        self.audit.append(EventType.HALT, {"reason": reason})
        self._save_state()

    def resume(self, by: str) -> bool:
        """Only a human resumes. Nothing in the agent calls this."""
        if not self.halted:
            return False
        self.halted = False
        self.audit.append(EventType.MODE_CHANGE, {"resumed": True, "reason": self.halt_reason},
                          actor=by)
        self.halt_reason = ""
        self._save_state()
        return True

    def set_mode(self, mode: AgentMode, by: str) -> None:
        old = self.config.agent.mode
        if mode is AgentMode.AUTONOMOUS and \
           self.config.execution.venue_mode is ExecutionVenueMode.LIVE:
            unaccepted = [a.name for a in self.config.strategies
                          if a.enabled and a.lifecycle != "accepted"]
            if unaccepted:
                raise ValueError(
                    "autonomous live trading refused: these strategies have not passed "
                    f"acceptance: {', '.join(unaccepted)}")
        self.config.agent.mode = mode
        self.audit.append(EventType.MODE_CHANGE,
                          {"from": old.value, "to": mode.value}, actor=by)

    # ------------------------------------------------------------------ #
    # the cycle
    # ------------------------------------------------------------------ #

    def cycle(self) -> CycleReport:
        from ..core.clock import Stopwatch

        sw = Stopwatch()
        now = self.now()
        self.cycles += 1
        cfg = self.config
        report = CycleReport(ts_ns=now, mode=cfg.agent.mode.value, halted=self.halted,
                             kill_switch=False, regime=None, equity="0")

        # --- 1. kill switch ------------------------------------------------ #
        kill_state = self.kill.poll()
        report.kill_switch = kill_state.engaged

        # --- 2. health ------------------------------------------------------ #
        connected = self.health.probe(self.broker, now_ns=now)
        symbols = self._active_instruments()
        # Every timeframe an enabled allocation declares, so a daily system is
        # handed daily bars and not the feed's primary four-hour frame.
        snap = self.feed.snapshot(symbols, now_ns=now, timeframes=self._active_timeframes())
        health = self.health.snapshot({s: snap.ages.get(s, float("inf")) for s in symbols})
        report.health = health.to_dict()

        try:
            account = self.broker.account()
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"account read failed: {exc}")
            self.audit.append(EventType.CONNECTIVITY, {"account_read_failed": str(exc)})
            report.duration_ms = sw.elapsed_ms
            # The heartbeat must still advance: a degraded cycle is a LIVE cycle,
            # and starving the heartbeat here would trip the dead-man switch on a
            # transient venue outage.
            self.heartbeat.update(cycle=self.cycles, degraded=True,
                                  error="account read failed")
            self.heartbeat.beat()
            return report
        report.equity = str(account.equity)
        self._roll_periods(now, account.equity)
        # The peak that drives the drawdown ladder and the halt is tracked on
        # BALANCE (realised), not equity. A trade that ran +6% open and gave it
        # all back would otherwise leave the peak 6% higher forever: the next
        # tick reads a 6% drawdown, the ladder cuts size to a quarter and the
        # halt sits 4% away -- all from money that was never bankable.
        # The marked-to-market peak is still kept, for display only.
        self.equity_peak = max(self.equity_peak, account.balance)
        self.equity_peak_marked = max(getattr(self, "equity_peak_marked", ZERO),
                                      account.equity)

        # Advance the drawdown-ladder rung once per cycle, with hysteresis. The
        # rung is state rather than a pure function of the current drawdown, so
        # two entries minutes apart either side of a threshold get the same
        # budget instead of oscillating.
        if self.config.risk.ladder_enabled and self.equity_peak > 0:
            from ..risk.sizing import ladder_rung as _rung
            dd_now = ((self.equity_peak - account.equity) / self.equity_peak
                      * D("100"))
            previous = self._ladder_rung
            self._ladder_rung = _rung(dd_now, self.config.risk.ladder,
                                      current_rung=previous,
                                      hysteresis_pct=self.config.risk.ladder_hysteresis_pct)
            if self._ladder_rung != previous:
                self.audit.append(EventType.LADDER_STEP, {
                    "from_rung": previous, "to_rung": self._ladder_rung,
                    "drawdown_pct": f"{dd_now:.2f}",
                    "direction": "tighten" if self._ladder_rung > previous else "release"})
                self._save_state()

        # Reading the position book can fail exactly like reading the account --
        # and now does so deliberately, since an unreadable stop list raises
        # rather than pretending the positions are unprotected. Degrade the same
        # way: record it, keep the heartbeat alive, and try again next cycle.
        # Snapshot the agent's OWN book BEFORE _hydrate runs. _hydrate prunes
        # metadata for every symbol the venue no longer reports, which is
        # exactly the evidence a `phantom` mismatch is made of -- capturing it
        # afterwards would delete the finding a few lines before the diff that
        # exists to find it.
        local_book = self._local_book()
        try:
            positions = self._hydrate(self.broker.positions())
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"position read failed: {exc}")
            self.audit.append(EventType.CONNECTIVITY, {"position_read_failed": str(exc)})
            self.heartbeat.update(cycle=self.cycles, degraded=True,
                                  error="position read failed")
            self.heartbeat.beat()
            report.duration_ms = sw.elapsed_ms
            return report

        # --- 3. reconcile --------------------------------------------------- #
        due = (now - self.last_reconcile_ns) / 1e9 >= cfg.execution.reconcile_interval_sec
        if due or self.oms.unresolved:
            self.oms.resolve_all_unknown()
            # Pass the agent's OWN book, reconstructed from _position_meta --
            # not the venue's positions handed back to the venue. Comparing the
            # venue against itself made `phantom`, `orphan`, `direction` and
            # `size_drift` structurally unreachable: the venue could silently
            # double a position and reconcile would report `ok=True`.
            rec = self.reconciler.reconcile(
                local_book,
                unresolved_order_ids=[o.intent.client_order_id
                                      for o in self.oms.unresolved],
                conversions=self._conversion_map())
            self.last_reconcile_ns = now
            report.reconcile = rec.to_dict()
            if rec.halt_required:
                self.halt("reconciliation mismatch: " +
                          (", ".join(m.kind for m in rec.mismatches) or "unresolved orders"))
            positions = self._hydrate(self.broker.positions())

        # --- 4. regime ------------------------------------------------------ #
        frames = {s: snap.frames[s] for s in symbols
                  if s in snap.frames and len(snap.frames[s]) > 120}
        if cfg.agent.regime_detection_enabled and frames:
            idx = min(len(f) for f in frames.values()) - 1
            try:
                # Without ADX values the classifier scores trend strength as a
                # constant 0, so TRENDING was unreachable and every trend was
                # labelled "quiet range". Regime is the dimension the whole
                # lesson store is keyed by, so a constant label meant the agent
                # could never learn "this strategy only works in a trend" --
                # the exact question the module exists to answer.
                from ..strategy.base import _frame_key, adx as _adx_fn
                adx_values = {}
                cache = getattr(self, "_adx_cache", {})
                for sym, f in frames.items():
                    try:
                        # Memoised on the frame's identity: the same H4 frame
                        # arrives on ~240 consecutive 60-second cycles.
                        key = _frame_key(f)
                        hit = cache.get(sym)
                        if hit is not None and hit[0] == key:
                            adx_values[sym] = hit[1]
                            continue
                        series = _adx_fn(f, 14)
                        if len(series) and pd.notna(series.iloc[-1]):
                            adx_values[sym] = float(series.iloc[-1])
                            cache[sym] = (key, adx_values[sym])
                    except Exception:  # noqa: BLE001 - one bad frame is not fatal
                        continue
                self._adx_cache = cache
                self.regime = classify(frames, idx, adx_values=adx_values or None)
                report.regime = self.regime.to_dict()
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"regime detection failed: {exc}")

        # --- 4b. news: a filter, never a trigger ---------------------------- #
        self._news_assessment = {}
        if self.news is not None and cfg.news.enabled and symbols:
            try:
                self._news_assessment = self.news.assess(now, symbols)
            except Exception as exc:  # noqa: BLE001 - news must never break the loop
                report.errors.append(f"news assessment failed: {exc}")

        # --- 5. build the risk context -------------------------------------- #
        correlations = self._correlations(frames) if frames else {}
        self._group_view = self._publish_group(now, account, positions)
        ctx = self._build_context(now, account, positions, snap, connected, health,
                                  correlations=correlations)
        alarms = self.risk.portfolio_alarms(ctx)
        report.alarms = [v.to_dict() for v in alarms]
        blocking = [a.rule for a in alarms if a.severity is Severity.BLOCK]
        for alarm in alarms:
            if alarm.severity is Severity.HALT:
                self.halt(f"{alarm.rule}: {alarm.message}")
        # A BLOCK alarm -- an unprotected position, a thin margin level -- stops
        # new risk. Previously only HALT was acted on, so the agent would happily
        # open a fresh position beside a naked one.
        if blocking:
            ctx = self._build_context(now, account, positions, snap, connected, health,
                                      correlations=correlations, blocking_alarms=blocking)
        report.halted = self.halted

        # --- 6. manage open positions (always, even when halted) ------------ #
        report.protections = self._manage_positions(positions, snap, ctx)

        # --- 7/8/9. signals -> filter -> risk -> act ------------------------ #
        blocked = self.halted or kill_state.engaged
        permitted, licence_reason = self.entry_permission()
        if not permitted:
            blocked = True
            report.alarms.append({
                "rule": "licence", "severity": "block", "observed": None, "limit": None,
                "message": ("no new live positions: " + licence_reason
                            + " -- open positions are still managed and protected")})
            if not self._licence_block_logged:
                self._licence_block_logged = True
                self.audit.append(EventType.HALT, {
                    "licence_blocks_entries": licence_reason,
                    "effect": "no new live entries; exits and protection unaffected"})
        elif self._licence_block_logged:
            self._licence_block_logged = False
            self.audit.append(EventType.MODE_CHANGE, {"licence_entries_restored": True})
        if not blocked and self._in_session(now):
            report.decisions = self._consider_entries(now, snap, ctx, frames,
                                                      correlations=correlations,
                                                      blocking_alarms=blocking,
                                                      health=health,
                                                      connected=connected)
        elif not blocked:
            report.decisions = []

        # --- 10. learn ------------------------------------------------------ #
        learned, proposed = self._learn()
        report.lessons_learned = learned
        report.proposals_created = proposed
        if learned or proposed:
            # _learn advances processed_trades and the trade cursor. Without a
            # save here, a crash on a cycle that learned but did not trade lost
            # the cursor and replayed every closed trade on restart -- harmless
            # for the autopsy table (INSERT OR REPLACE) but it re-ran the whole
            # aggregate -> lesson -> proposal pass and re-inserted lesson rows.
            self._save_state()

        self.decisions.extend(report.decisions)
        if len(self.decisions) > 5000:
            self.decisions = self.decisions[-5000:]

        self.heartbeat.update(
            cycle=self.cycles, mode=cfg.agent.mode.value, halted=self.halted,
            equity=str(account.equity), open_positions=len(positions),
            kill_switch=kill_state.engaged)
        self.heartbeat.beat()
        report.duration_ms = sw.elapsed_ms
        if self.on_cycle:
            self.on_cycle(report)
        return report

    # ------------------------------------------------------------------ #

    def _active_instruments(self) -> List[str]:
        out: List[str] = []
        for alloc in self.config.strategies:
            if alloc.enabled:
                out.extend(alloc.instruments)
        if not out:
            out = list(self.config.agent.semi_auto_envelope.get("instruments", []))
        return sorted(set(out))

    def _active_timeframes(self) -> List[str]:
        """Timeframes the enabled allocations declare, in a stable order."""
        seen: List[str] = []
        for alloc in self.config.strategies:
            if alloc.enabled and alloc.timeframe and alloc.timeframe not in seen:
                seen.append(alloc.timeframe)
        return seen

    def _roll_periods(self, now_ns: int, equity: Decimal) -> None:
        """Roll the budget windows.

        The weekly and monthly baselines were previously never captured, which
        left `weekly_loss_limit_pct` and `monthly_loss_limit_pct` structurally
        unable to fire: the engine computed -0/equity every time.
        """
        dt = datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc)
        rolled = False
        # The budget day must match the day the BROKER reconciles against,
        # which rolls at 17:00 New York (21:00/22:00 UTC), not at UTC midnight.
        # With a midnight boundary a continuous loss straddling it was granted
        # two full daily budgets an hour apart -- demonstrated at ~4% of
        # drawdown inside 40 minutes with both "daily" limits honoured.
        venue_day = self._venue_day(dt)
        if self._day_key != venue_day:
            self._day_key = venue_day
            self.day_start_equity = equity
            self.trades_today = 0
            self.day_profit_locked = False
            rolled = True
        iso = dt.isocalendar()
        week_key = (iso[0], iso[1])
        if self._week_key != week_key:
            self._week_key = week_key
            self.week_start_equity = equity
            self.trades_week = 0
            rolled = True
        month_key = (dt.year, dt.month)
        if self._month_key != month_key:
            self._month_key = month_key
            self.month_start_equity = equity
            rolled = True
        if self._year_key != dt.year:
            self._year_key = dt.year
            self.trades_year = 0
            rolled = True
        if rolled:
            self._save_state()

    @staticmethod
    def _venue_day(dt) -> int:
        """Ordinal of the venue's trading day for a UTC timestamp.

        The FX day rolls at 17:00 New York. Subtracting 21 hours puts the whole
        of a venue day into one calendar ordinal, and is within an hour of
        correct across the two DST changeovers -- immaterial for a day boundary
        that exists to stop two budgets being spent as one.
        """
        from datetime import timedelta
        return (dt - timedelta(hours=21)).toordinal()

    def _rolling_24h_pnl(self, now_ns: int, equity: Decimal) -> Decimal:
        """Equity change over the trailing 24 hours.

        Runs beside the calendar-day budget rather than replacing it: the
        calendar day is what the operator and the broker statement agree on,
        and the rolling window is what actually bounds the loss.
        """
        cutoff = now_ns - 86_400 * 1_000_000_000
        # At most one mark per bucket. Appending on every call -- and this is
        # called on every context rebuild, several times a cycle -- filled the
        # bounded deque long before 24 hours had passed at a short decision
        # interval: at 5 s cycles the "24h" window silently covered about
        # seven hours, and a loss older than that stopped counting.
        bucket = _EQUITY_MARK_BUCKET_NS
        if (not self._equity_marks
                or now_ns // bucket != self._equity_marks[-1][0] // bucket):
            self._equity_marks.append((now_ns, equity))
        while self._equity_marks and self._equity_marks[0][0] < cutoff:
            self._equity_marks.popleft()
        if not self._equity_marks:
            return ZERO
        return equity - self._equity_marks[0][1]

    def _bounded_processed(self) -> List[str]:
        """The most recently processed trade ids, oldest first."""
        order = list(self._processed_order)
        seen = set(order)
        # Ids added to the set by anything other than _learn still persist.
        extra = sorted(t for t in self.processed_trades if t not in seen)
        return (extra + order)[-5000:]

    def _in_session(self, now_ns: int) -> bool:
        dt = datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc)
        if dt.weekday() not in self.config.agent.trade_days:
            return False
        return any(start <= dt.hour < end
                   for start, end in self.config.agent.session_windows_utc)

    def _correlations(self, frames: Dict[str, pd.DataFrame]
                      ) -> Dict[tuple[str, str], float]:
        """Realised pairwise correlation over the configured lookback.

        This was previously never populated, so every pair scored 0.0 and the
        correlated-risk limit could not fire -- the exact "four different trades
        are one bet" failure the limit exists to catch.
        """
        import numpy as np
        from ..risk.exposure import rolling_correlation

        window = self.config.risk.correlation_lookback_bars
        rets: Dict[str, list[float]] = {}
        for sym, df in frames.items():
            if df is None or len(df) < 30:
                continue
            closes = df["close"].astype(float).tail(window + 1).to_numpy()
            if closes.size < 30 or (closes <= 0).any():
                continue
            rets[sym] = np.diff(np.log(closes)).tolist()
        out: Dict[tuple[str, str], float] = {}
        names = sorted(rets)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                rho = rolling_correlation(rets[a], rets[b])
                if rho is not None:
                    out[(a, b)] = rho
        return out

    def _build_context(self, now_ns: int, account, positions, snap, connected,
                       health, *, correlations=None, blocking_alarms=None) -> RiskContext:
        cfg = self.config
        instruments = self.broker.instruments()
        conversions: Dict[str, Decimal] = {}
        missing: List[str] = []
        for sym in snap.quotes:
            inst = instruments.get(sym)
            if inst is None:
                continue
            if inst.quote == cfg.execution.account_currency:
                conversions[inst.quote] = D("1")
                continue
            try:
                rate = self.broker.conversion_rate(
                    inst.quote, cfg.execution.account_currency)
            except Exception:  # noqa: BLE001 - a missing rate must block, not crash
                conversions.pop(inst.quote, None)
                missing.append(inst.quote)
                continue
            if rate and rate > 0:
                conversions[inst.quote] = rate
            else:
                conversions.pop(inst.quote, None)
                missing.append(inst.quote)

        lifecycles = {a.name: a.lifecycle for a in cfg.strategies}
        normal_spreads: Dict[str, Decimal] = {}
        cost_models: Dict[str, CostModel] = {}
        for sym, q in snap.quotes.items():
            inst = instruments.get(sym)
            if inst is None:
                continue
            spread = q.spread_pips(inst)
            # NORMAL must be a historical baseline, not the live value. Setting
            # normal := live turned the veto into `s > 2.5 * s`, false for every
            # positive spread, so the agent would happily enter during a
            # rollover or a news blow-out at six times its usual cost -- and the
            # break-even calculation was then run against the blown-out spread
            # as though it were normal, wrong in both directions at once.
            self._observe_spread(sym, spread)
            normal_spreads[sym] = self._normal_spread(sym, spread)
            cost_models[sym] = CostModel(
                spread_pips=spread,
                commission_per_lot_round_turn=cfg.execution.commission_per_lot_round_turn,
                slippage_pips_median=cfg.execution.expected_slippage_pips)

        # A period baseline of zero means "not captured yet" (the agent has
        # not rolled its periods). Measuring against zero read the whole
        # account as today's profit: +100%, the profit lock latched, and every
        # entry was refused for a reason that was not true.
        def _base(value: Decimal) -> Decimal:
            return value if value > 0 else account.equity
        day_base = _base(self.day_start_equity)
        week_base = _base(self.week_start_equity)
        month_base = _base(self.month_start_equity)
        return RiskContext(
            now_ns=now_ns, account=account, positions=positions, instruments=instruments,
            quotes=snap.quotes, conversions=conversions, equity_peak=self.equity_peak,
            day_pnl=account.equity - day_base,
            day_start_equity=day_base,
            week_pnl=account.equity - week_base,
            month_pnl=account.equity - month_base,
            week_start_equity=week_base,
            month_start_equity=month_base,
            rolling_24h_pnl=self._rolling_24h_pnl(now_ns, account.equity),
            day_profit_locked=self.day_profit_locked,
            ladder_rung=self._ladder_rung,
            venue_min_stop_pips=self._venue_stop_floors(instruments),
            trades_today=self.trades_today, trades_this_week=self.trades_week,
            trades_this_year=self.trades_year, last_entry_ns=self.last_entry_ns,
            pending_risk=self.oms.pending_risk(),
            unresolved_orders=len(self.oms.unresolved),
            clock_skew_ms=health.clock_skew_ms, connectivity_ok=connected,
            offline_seconds=health.offline_seconds,
            data_age_sec=dict(snap.ages), data_quality=dict(snap.quality),
            normal_spread_pips=normal_spreads, cost_models=cost_models,
            news_blackout={sym: "; ".join(a.reasons)[:180]
                           for sym, a in self._news_assessment.items() if a.blocked},
            missing_conversions=sorted(set(missing)),
            blocking_alarms=list(blocking_alarms or []),
            correlations=dict(correlations or {}),
            regime_risk_multiplier=(dec(self.regime.risk_multiplier)
                                    if self.regime else D("1")),
            halted=self.halted, halt_reason=self.halt_reason,
            kill_switch=self.kill.read().engaged,
            strategy_lifecycles=lifecycles,
            live_money=cfg.execution.venue_mode is ExecutionVenueMode.LIVE,
            group_others_open_risk=(self._group_view.others_open_risk
                                    if self._group_view is not None else None),
            group_others_equity=(self._group_view.others_equity
                                 if self._group_view is not None else ZERO),
            group_others_currency_risk=(dict(self._group_view.others_currency_risk)
                                        if self._group_view is not None else {}),
            group_unknown_members=(sorted(set(self._group_view.stale)
                                          | set(self._group_view.unreadable))
                                   if self._group_view is not None else []),
        )

    def _publish_group(self, now_ns: int, account, positions):
        """Write this engine's row to the owner's ledger and read the others."""
        if self._group is None:
            return None
        from ..risk.exposure import currency_exposure
        from ..risk.portfolio import GroupRow
        try:
            instruments = self.broker.instruments()
            conv = self._conversion_map()
            risk_map = {p.instrument: (p.initial_risk or ZERO) for p in positions}
            exposures = currency_exposure(list(positions), instruments,
                                          risk_by_instrument=risk_map, conversions=conv,
                                          account_currency=account.currency)
            open_risk = sum((p.initial_risk or ZERO for p in positions), ZERO)
            row = GroupRow(
                account=self._group.account, currency=account.currency,
                equity=account.equity, open_risk=open_risk,
                pending_risk=self.oms.pending_risk(),
                drawdown_pct=((self.equity_peak - account.equity) / self.equity_peak * D("100")
                              if self.equity_peak > 0 else ZERO),
                positions=len(positions),
                currency_risk={c: e.net_risk for c, e in exposures.items()},
                written_ns=now_ns, halted=self.halted)
            self._group.publish(row)
            return self._group.view(now_ns, conversions=conv, own_currency=account.currency)
        except Exception as exc:  # noqa: BLE001 - the ledger must never break a cycle
            self.audit.append(EventType.CONNECTIVITY, {"group_ledger_error": str(exc)[:200]})
            from ..risk.portfolio import GroupView
            v = GroupView()
            v.unreadable.append("ledger")
            return v

    # ------------------------------------------------------------------ #

    def _local_book(self) -> List[Position]:
        """The agent's own view of what it believes is open.

        Reconstructed from `_position_meta`, which is the only record the agent
        keeps independently of the venue. This is what makes reconciliation a
        real diff rather than a tautology.
        """
        book: List[Position] = []
        for sym, meta in self._position_meta.items():
            if meta.get("provisional"):
                # An UNKNOWN order's placeholder is not yet a belief about the
                # book; the unresolved-orders path already blocks on it.
                continue
            try:
                side = Side(str(meta.get("side", "")))
            except ValueError:
                continue
            try:
                lots = dec(meta.get("lots", 0))
                risk = dec(meta.get("initial_risk", 0))
                stop = meta.get("stop_loss")
                entry = meta.get("entry_price")
                book.append(Position(
                    instrument=sym, side=side, lots=lots,
                    entry_price=dec(entry) if entry else ZERO,
                    opened_ns=int(meta.get("opened_ns") or 0),
                    strategy=str(meta.get("strategy", "")),
                    stop_loss=dec(stop) if stop else None,
                    initial_risk=risk,
                    client_order_id=str(meta.get("client_order_id", "")),
                ))
            except Exception:  # noqa: BLE001 - a malformed row is not fatal
                continue
        return book

    def _conversion_map(self) -> Dict[str, Decimal]:
        """Quote-currency -> account-currency rates, for the reconciler.

        Only rates the venue actually returned. A missing rate stays missing:
        the reconciler falls back to its flat distance rather than guessing.
        """
        out: Dict[str, Decimal] = {}
        acct_ccy = self.config.execution.account_currency
        for sym, inst in self.broker.instruments().items():
            if inst.quote == acct_ccy:
                out[inst.quote] = D("1")
                continue
            try:
                rate = self.broker.conversion_rate(inst.quote, acct_ccy)
            except Exception:  # noqa: BLE001
                continue
            if rate and rate > 0:
                out[inst.quote] = rate
        return out

    def _hydrate(self, positions: Sequence[Position]) -> List[Position]:
        """Attach locally-held risk metadata to venue-reported positions.

        Live adapters rebuild Position objects on every call and cannot know the
        initial risk, so ``r_multiple`` is undefined and the whole profit
        protection layer returns immediately. The agent keeps that metadata and
        re-attaches it here, which also makes the scale-out flags survive the
        rebuild -- otherwise a tick storm could scale out repeatedly.

        The metadata is matched on SIDE as well as instrument, and stale entries
        are discarded rather than re-attached. Keying on the instrument alone
        meant that after an order whose outcome was UNKNOWN -- the case the whole
        OMS is built around, where the venue may have filled without telling us
        -- a brand-new position inherited the previous trade's risk, strategy and
        scale-out flags. Its R read roughly double, and both profit-protection
        rules were silently switched off.
        """
        out: List[Position] = []
        for pos in positions:
            meta = self._position_meta.get(pos.instrument)
            if meta and str(meta.get("side", "")) != pos.side.value:
                self.audit.append(EventType.RECONCILE_MISMATCH, {
                    "instrument": pos.instrument,
                    "stale_position_metadata": True,
                    "known_side": meta.get("side"), "venue_side": pos.side.value,
                    "action": "metadata discarded; the venue holds a different position "
                              "from the one we recorded"})
                self._position_meta.pop(pos.instrument, None)
                meta = None
                # Risk metadata we cannot trust is worse than none: a wrong
                # initial_risk mis-scales every protection rule. Ask for a
                # reconciliation instead of guessing.
                self.halt(f"the {pos.side.value} position on {pos.instrument} does not "
                          "match the position this agent recorded; its risk cannot be "
                          "measured until a human confirms the account state")
            if meta:
                if pos.initial_risk <= 0:
                    pos.initial_risk = dec(meta.get("initial_risk", 0))
                if not pos.strategy:
                    pos.strategy = str(meta.get("strategy", ""))
                if not pos.opened_ns:
                    pos.opened_ns = int(meta.get("opened_ns", 0) or 0)
                pos.partial_taken = bool(meta.get("partial_taken", False))
                pos.breakeven_moved = bool(meta.get("breakeven_moved", False))
                if meta.get("regime") and not getattr(pos, "regime", ""):
                    pos.tags = list(pos.tags) + [f"regime:{meta['regime']}"]
            out.append(pos)
        # Drop metadata for positions the venue no longer reports.
        live = {p.instrument for p in positions}
        for sym in list(self._position_meta):
            if sym not in live:
                self._position_meta.pop(sym, None)
        for pos in out:
            # Re-attach the peak excursion so a restart does not reset the
            # give-back ratchet and re-arm a stop it had already tightened.
            meta = self._position_meta.get(pos.instrument)
            if meta:
                try:
                    pos.max_favourable = dec(meta.get("max_favourable", 0) or 0)
                    pos.max_adverse = dec(meta.get("max_adverse", 0) or 0)
                except (TypeError, ValueError):
                    pass
        return out

    @staticmethod
    def _session_bucket(now_ns: int) -> str:
        """Asia / London / rollover. Spreads differ by a factor of 5 between
        them, so one baseline across all hours would either never fire during
        London or fire constantly during Asia."""
        from datetime import datetime, timezone
        hour = datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc).hour
        if 20 <= hour < 22:
            return "rollover"
        if 7 <= hour < 17:
            return "london"
        return "asia"

    def _observe_spread(self, instrument: str, spread: Decimal) -> None:
        if spread is None or spread <= 0:
            return
        key = (instrument, self._session_bucket(self.now()))
        window = self._spread_history.setdefault(key, deque(maxlen=_SPREAD_WINDOW))
        window.append(float(spread))

    def _normal_spread(self, instrument: str, live: Decimal) -> Decimal:
        """Median observed spread for this instrument in this session bucket.

        Falls back to the live value until enough history exists, which makes
        the veto inert rather than trigger-happy during the first minutes of a
        run -- the safe direction for a guard whose false positive is a missed
        trade and whose false negative is an expensive one.
        """
        key = (instrument, self._session_bucket(self.now()))
        window = self._spread_history.get(key)
        if not window or len(window) < _SPREAD_MIN_SAMPLES:
            return live
        ordered = sorted(window)
        mid = len(ordered) // 2
        median = (ordered[mid] if len(ordered) % 2
                  else (ordered[mid - 1] + ordered[mid]) / 2.0)
        return dec(median) if median > 0 else live

    def _track_excursion(self, pos: Position, quote: Quote, inst, conv: Decimal) -> None:
        """Maintain the best and worst R this position has reached.

        Persisted with the rest of the position metadata, so a restart does not
        reset the peak and re-arm a ratchet that had already tightened.
        """
        if pos.initial_risk <= 0:
            return
        try:
            r = pos.unrealised(quote, inst, conv) / pos.initial_risk
        except (ZeroDivisionError, InvalidOperation):
            return
        meta = self._position_meta.setdefault(pos.instrument, {})
        best = dec(meta.get("max_favourable", 0) or 0)
        worst = dec(meta.get("max_adverse", 0) or 0)
        best = max(best, r, pos.max_favourable or ZERO)
        worst = min(worst, r, pos.max_adverse or ZERO)
        if best != dec(meta.get("max_favourable", 0) or 0) or \
           worst != dec(meta.get("max_adverse", 0) or 0):
            meta["max_favourable"] = str(best)
            meta["max_adverse"] = str(worst)
        pos.max_favourable = best
        pos.max_adverse = worst

    def _venue_stop_floors(self, instruments: Dict[str, Any]) -> Dict[str, Decimal]:
        """Each instrument's venue-enforced minimum stop distance, in pips.

        Read from the adapter, which reads it from the terminal. Brokers differ --
        one allows a stop half a pip away and the next enforces twenty -- and
        the difference is the difference between a working strategy and one
        whose every order is rejected.
        """
        getter = getattr(self.broker, "min_stop_distance", None)
        if getter is None:
            return {}
        out: Dict[str, Decimal] = {}
        for symbol, inst in instruments.items():
            try:
                distance = getter(symbol)
            except Exception:  # noqa: BLE001 - never let this break a cycle
                continue
            if distance and inst.pip > 0:
                out[symbol] = dec(distance) / inst.pip
        return out

    def _cost_model(self, instrument: str, snap) -> Optional[CostModel]:
        """The live cost model for an instrument, or None if it cannot be built.

        Used by the break-even rule so the "break-even" stop actually breaks
        even rather than realising the commission as a small loss.
        """
        inst = self.broker.instruments().get(instrument)
        quote = snap.quotes.get(instrument) if snap is not None else None
        if inst is None or quote is None:
            return None
        try:
            # The operator's verified cost schedule, as everywhere else. The
            # bare constructor charged the CostModel DEFAULTS (7.00 per lot,
            # 0.1p slippage) whatever the venue actually costs, so on a
            # commission-free account the "break-even" stop locked in a small
            # profit, and above 7.00 it realised a small loss.
            return CostModel(
                spread_pips=quote.spread_pips(inst),
                commission_per_lot_round_turn=(
                    self.config.execution.commission_per_lot_round_turn),
                slippage_pips_median=self.config.execution.expected_slippage_pips)
        except Exception:  # noqa: BLE001
            return None

    def _max_hold_sec(self, pos: Position) -> int:
        """Configured horizon, or one derived from the signal that opened it.

        An explicit `max_hold_sec` always wins. Otherwise the horizon recorded
        at entry is used, so a strategy that says "this idea plays out over 20
        bars" is not still holding the trade 200 bars later.
        """
        explicit = int(self.config.risk.max_hold_sec)
        if explicit > 0:
            return explicit
        meta = self._position_meta.get(pos.instrument) or {}
        horizon = meta.get("max_hold_sec")
        try:
            return int(horizon) if horizon else 0
        except (TypeError, ValueError):
            return 0

    def _apply_profit_lock(self, positions: Sequence[Position], snap,
                           ctx: RiskContext) -> List[dict]:
        """Actually lock the day's profit, rather than merely pausing entries.

        The old behaviour appended a veto and stopped. That paused NEW risk
        while the risk already on the book -- in one measured run, 2.43% of
        equity -- ran untouched, so the whole +3% day could still be given
        back through open positions. A "lock" that protects nothing already
        open is the opposite of what the name claims.

        When the lock trips, every open position's stop is tightened to at
        least the level that preserves ``profit_lock_keep_fraction`` of the
        day's gain, sharing the requirement across the open book. Positions
        already better protected are left alone; stops are never widened.
        """
        out: List[dict] = []
        cfg = self.config.risk
        if cfg.daily_profit_lock_pct <= 0:
            return out
        if ctx.day_pnl_pct < cfg.daily_profit_lock_pct and not self.day_profit_locked:
            return out

        if not self.day_profit_locked:
            self.day_profit_locked = True
            self._save_state()
            self.audit.append(EventType.LADDER_STEP, {
                "profit_lock_engaged": True,
                "day_pnl_pct": f"{ctx.day_pnl_pct:.2f}",
                "threshold_pct": str(cfg.daily_profit_lock_pct),
                "keep_fraction": str(cfg.profit_lock_keep_fraction),
                "effect": "no new entries for the rest of the venue day, and every "
                          "open stop tightened to preserve the day's gain"})

        open_positions = [p for p in positions if p.lots > 0]
        if not open_positions:
            return out
        # How much of the day's gain must survive, spread across the open book.
        keep_total = ctx.day_pnl * cfg.profit_lock_keep_fraction
        give_back_budget = ctx.day_pnl - keep_total
        per_position = give_back_budget / D(len(open_positions))

        instruments = self.broker.instruments()
        for pos in open_positions:
            inst = instruments.get(pos.instrument)
            quote = snap.quotes.get(pos.instrument)
            if inst is None or quote is None:
                continue
            conv = ctx.conversion(inst.quote)
            if conv is None or conv <= 0:
                continue        # never guess a rate; the entry veto still holds
            unreal = pos.unrealised(quote, inst, conv)
            # The floor this position may fall to, in account currency.
            floor_value = unreal - per_position
            pip_val = inst.pip_value_quote(pos.lots) * conv
            if pip_val <= 0:
                continue
            ref = quote.price_for(pos.side.opposite)
            distance = (unreal - floor_value) / pip_val * inst.pip
            sign = D(pos.side.sign)
            candidate = inst.round_price(ref - distance * sign)
            from ..risk.protect import _tighter
            if not _tighter(pos.side, pos.stop_loss, candidate):
                continue
            beyond = (candidate >= ref) if pos.side is Side.BUY else (candidate <= ref)
            if beyond:
                continue        # would be a market close; leave that to the ratchet
            try:
                if self.broker.modify_position(pos.instrument, stop_loss=candidate):
                    meta = self._position_meta.setdefault(pos.instrument, {})
                    meta["stop_loss"] = str(candidate)
                    self.audit.append(EventType.POSITION_MODIFY, {
                        "rule": "profit_lock", "instrument": pos.instrument,
                        "new_stop": str(candidate),
                        "reason": f"daily profit lock: preserve "
                                  f"{cfg.profit_lock_keep_fraction} of +{ctx.day_pnl_pct:.2f}%"})
                    out.append({"action": "move_stop", "rule": "profit_lock",
                                "instrument": pos.instrument, "new_stop": str(candidate)})
            except Exception as exc:  # noqa: BLE001
                self.audit.append(EventType.POSITION_MODIFY,
                                  {"rule": "profit_lock", "failed": True,
                                   "instrument": pos.instrument, "error": str(exc)})
        if out:
            self._save_state()
        return out

    def _manage_positions(self, positions: Sequence[Position], snap,
                          ctx: RiskContext) -> List[dict]:
        out: List[dict] = []
        cfg = self.config
        instruments = self.broker.instruments()
        # Lock the day's gain BEFORE the per-position rules, so the rules below
        # can only tighten further from an already-protected level.
        out.extend(self._apply_profit_lock(positions, snap, ctx))
        for pos in positions:
            quote = snap.quotes.get(pos.instrument)
            inst = instruments.get(pos.instrument)
            if quote is None or inst is None:
                continue

            # ORDER MATTERS. The time-based exits are evaluated FIRST, because
            # they read only the clock: they need no exchange rate and no R, so
            # a missing rate must not suspend them. A missing rate is itself a
            # symptom of degraded connectivity -- exactly the condition most
            # likely to coincide with a Friday close -- and this is the one exit
            # whose failure turns a bounded loss into an unbounded one.
            if cfg.risk.weekend_flat:
                wf = weekend_flat(pos, ctx.now_ns, cfg.risk.friday_close_utc_hour)
                if wf is not None:
                    if self._close(pos, wf.reason, code=wf.rule or "weekend_flat"):
                        out.append(wf.to_dict())
                    else:
                        out.append({"failed": True, **wf.to_dict()})
                    continue

            # The horizon stop. A trade held far past the horizon it was tested
            # over is no longer that trade: its statistics belong to a different
            # distribution, and it pays financing the whole time. This function
            # existed but was never called, which also made the "slow_bleed"
            # autopsy mode structurally unreachable -- so a whole diagnostic
            # category was invisible to the learning loop.
            hold_limit = self._max_hold_sec(pos)
            if hold_limit > 0:
                ts = time_stop(pos, ctx.now_ns, hold_limit)
                if ts is not None:
                    if self._close(pos, ts.reason, code=ts.rule or "time_stop"):
                        out.append(ts.to_dict())
                    else:
                        out.append({"failed": True, **ts.to_dict()})
                    continue

            # Everything below is denominated in R, and R needs a rate to the
            # account currency. An unknown rate is not 1.0: a JPY position valued
            # at 1.0 reads ~150x its true excursion, and a half-pip move would
            # trigger a scale-out.
            conv = ctx.conversion(inst.quote)
            if conv is None or conv <= 0:
                self.audit.append(EventType.DATA_STALE, {
                    "instrument": pos.instrument,
                    "missing_conversion": inst.quote,
                    "effect": "R-denominated protection (break-even, scale-out, trail) "
                              "suspended for this position; the time-based exits still "
                              "apply because they need no rate"})
                out.append({"action": "protection_suspended", "failed": False,
                            "instrument": pos.instrument,
                            "reason": f"no {inst.quote}->{ctx.account.currency} rate"})
                continue

            # Track the peak and trough excursion HERE, so it exists on every
            # venue. Only PaperBroker maintained `max_favourable`; the live
            # adapters rebuild Position objects on each poll and cannot know
            # it, so the give-back ratchet -- whose whole purpose is to work
            # when the ATR trail cannot -- could never arm on a real account.
            # The backtest showed it firing and hid that completely.
            self._track_excursion(pos, quote, inst, conv)

            atr_val = None
            # The ATR of the timeframe the position's STRATEGY runs on. A daily
            # system trailed on a four-hour ATR is trailed a quarter as wide as
            # it was validated with.
            alloc = next((a for a in cfg.strategies if a.name == pos.strategy), None)
            frame = None
            if alloc is not None and alloc.timeframe:
                frame = snap.frames_for(alloc.timeframe).get(pos.instrument)
            if frame is None:
                frame = snap.frames.get(pos.instrument)
            if frame is not None and len(frame) > 20:
                from ..strategy.base import atr as _atr
                series = _atr(frame, 14)
                if len(series) and pd.notna(series.iloc[-1]):
                    atr_val = dec(float(series.iloc[-1]))

            for act in evaluate_protection(pos, quote, inst, cfg.risk, atr=atr_val,
                                           quote_to_account=conv,
                                           cost_model=self._cost_model(pos.instrument, snap)):
                try:
                    meta = self._position_meta.setdefault(pos.instrument, {})
                    if act.action is ProtectAction.MOVE_STOP and act.new_stop is not None:
                        if self.broker.modify_position(pos.instrument, stop_loss=act.new_stop):
                            # Only the break-even RULE sets the break-even flag.
                            # Setting it for any stop move meant the first ATR
                            # trail permanently disabled the break-even rule, so
                            # a position could be stopped out below entry after
                            # having been a full R in profit.
                            if act.rule == "breakeven":
                                pos.breakeven_moved = True
                                meta["breakeven_moved"] = True
                            meta["stop_loss"] = str(act.new_stop)
                            self._save_state()
                            self.audit.append(EventType.POSITION_MODIFY, act.to_dict())
                            out.append(act.to_dict())
                        else:
                            # A refused stop move is not a no-op: the position is
                            # now less protected than the agent believes.
                            self.audit.append(EventType.POSITION_MODIFY,
                                              {"failed": True, **act.to_dict()})
                    elif act.action is ProtectAction.PARTIAL_CLOSE and act.close_lots:
                        res = self.broker.close_position(pos.instrument, act.close_lots,
                                                         reason="partial_take")
                        if res.state in (OrderState.FILLED, OrderState.PARTIAL):
                            pos.partial_taken = True
                            meta["partial_taken"] = True
                            # Straight to disk. Held only in memory, a crash
                            # between here and the next _save_state() re-armed
                            # the scale-out, and a second one leaves 25% of the
                            # intended runner.
                            remaining = pos.lots - (act.close_lots or ZERO)
                            if remaining > 0 and pos.initial_risk > 0:
                                meta["initial_risk"] = str(
                                    pos.initial_risk * remaining / pos.lots)
                            self._save_state()
                            self.audit.append(EventType.POSITION_CLOSE, act.to_dict())
                            out.append(act.to_dict())
                        else:
                            self.audit.append(EventType.POSITION_CLOSE,
                                              {"failed": True, "reason": res.reject_reason,
                                               **act.to_dict()})
                    elif act.action is ProtectAction.CLOSE:
                        if self._close(pos, act.reason, code=act.rule or "protect_close"):
                            out.append(act.to_dict())
                except Exception as exc:  # noqa: BLE001
                    self.audit.append(EventType.POSITION_MODIFY,
                                      {"error": str(exc), **act.to_dict()})
        return out

    def _close(self, pos: Position, reason: str, code: str = "") -> bool:
        """Close a position, and report honestly whether it closed.

        The previous version discarded the result and swallowed every exception,
        so a refused close (no price, venue down) was written to the audit chain
        and shown on the dashboard as a completed flatten while the position was
        still open -- over a weekend gap, in the worst case.

        ``code`` is the short machine-readable exit reason the VENUE records;
        ``reason`` is the sentence a human reads in the audit log. They were the
        same string, and the broker truncates to 32 characters, so the venue's
        record of a horizon exit read "time stop: held 5.0h beyond the " -- which
        matches nothing downstream. The post-mortem's horizon modes were
        unreachable on every venue as a direct result.
        """
        try:
            res = self.broker.close_position(pos.instrument,
                                             reason=(code or reason)[:32])
        except Exception as exc:  # noqa: BLE001
            self.audit.append(EventType.POSITION_CLOSE,
                              {"instrument": pos.instrument, "reason": reason,
                               "closed": False, "error": str(exc)})
            self.halt(f"could not close {pos.instrument} ({exc}); a position the agent "
                      "cannot exit is not a position it may leave unattended")
            return False
        if res.state in (OrderState.FILLED, OrderState.PARTIAL):
            # Confirm against the book. A PARTIAL close leaves a residual the
            # agent would otherwise forget it owns.
            try:
                remaining = [p for p in self.broker.positions()
                             if p.instrument == pos.instrument]
            except Exception as exc:  # noqa: BLE001
                remaining = []
                self.audit.append(EventType.POSITION_CLOSE,
                                  {"instrument": pos.instrument, "reason": reason,
                                   "post_close_read_failed": str(exc)})
            if remaining:
                self.audit.append(EventType.POSITION_CLOSE,
                                  {"instrument": pos.instrument, "reason": reason,
                                   "closed": False, "residual_lots": str(remaining[0].lots)})
                self.halt(f"{pos.instrument} closed only partially; the residual position "
                          "needs a human before any new risk")
                return False
            self.audit.append(EventType.POSITION_CLOSE,
                              {"instrument": pos.instrument, "reason": reason,
                               "closed": True})
            self._position_meta.pop(pos.instrument, None)
            return True
        self.audit.append(EventType.POSITION_CLOSE,
                          {"instrument": pos.instrument, "reason": reason,
                           "closed": False, "venue_reason": res.reject_reason})
        self.halt(f"{pos.instrument} could not be closed ({res.reject_reason}); the "
                  "position is still open and the reason needs a human")
        return False

    # ------------------------------------------------------------------ #

    def _consider_entries(self, now_ns: int, snap, ctx: RiskContext,
                          frames: Dict[str, pd.DataFrame], *,
                          correlations=None, blocking_alarms=None,
                          health=None, connected: bool = True) -> List[Decision]:
        """Evaluate every candidate entry, refreshing the context after each fill.

        The context used to be built once per cycle, so entries 2..N were judged
        against a snapshot that did not contain entries 1..N-1: the position cap,
        the entry-spacing rule and the daily trade cap could all be breached
        inside a single millisecond. After anything executes, the context is
        rebuilt from the broker before the next candidate is considered.
        """
        decisions: List[Decision] = []
        cfg = self.config
        regime_name = self.regime.regime.value if self.regime else ""

        def refresh() -> RiskContext:
            account = self.broker.account()
            positions = self._hydrate(self.broker.positions())
            return self._build_context(self.now(), account, positions, snap,
                                       connected, health or self.health.snapshot(),
                                       correlations=correlations,
                                       blocking_alarms=blocking_alarms)

        for alloc in cfg.strategies:
            if not alloc.enabled or alloc.name not in self.strategies:
                continue
            if alloc.name in self._guard_suspended:
                decisions.append(Decision(
                    ts_ns=now_ns, strategy=alloc.name, instrument="*", action="skipped",
                    regime=regime_name,
                    rationale="suspended by the performance guard: "
                              + self._guard_suspended[alloc.name]))
                continue
            if not can_trade(alloc.lifecycle,
                             cfg.execution.venue_mode is ExecutionVenueMode.LIVE):
                decisions.append(Decision(
                    ts_ns=now_ns, strategy=alloc.name, instrument="*", action="skipped",
                    regime=regime_name,
                    rationale=f"lifecycle '{alloc.lifecycle}' may not trade in "
                              f"{cfg.execution.venue_mode.value} mode"))
                continue
            strategy = self.strategies[alloc.name]
            # The frames of THIS allocation's timeframe. The primary frame is
            # H4; a daily or hourly system handed it would be a different
            # system from the one that was validated, so an allocation whose
            # timeframe was not loaded is skipped and says why.
            tf_frames = snap.frames_for(alloc.timeframe) if alloc.timeframe else frames
            if alloc.timeframe and not tf_frames:
                decisions.append(Decision(
                    ts_ns=now_ns, strategy=alloc.name, instrument="*", action="skipped",
                    regime=regime_name,
                    rationale=f"no {alloc.timeframe} bars are loaded for this "
                              "allocation; it is not handed another timeframe"))
                continue
            tf_frames = {s: f for s, f in tf_frames.items()
                         if s in alloc.instruments and f is not None and len(f) > 0}
            # PREPARE, exactly as the backtest does.
            #
            # Without this, features_at() falls through to recomputing each
            # indicator over a trailing window, while the backtest computes it
            # over the whole frame -- so the gate that was validated and the
            # gate that runs live are different numbers. Measured on a
            # volatility regime shift, an expanding volatility ceiling read
            # 3.4x higher in the backtest than live, and the filter passed on
            # 100% of bars there against 92% here. A strategy accepted on one
            # set of numbers was then traded on another.
            try:
                strategy.prepare(tf_frames)
            except Exception as exc:  # noqa: BLE001 - never let one strategy
                                      # stop the cycle
                self.audit.append(EventType.SIGNAL,
                                  {"strategy": alloc.name, "prepare_failed": str(exc)})
            for sym in alloc.instruments:
                frame = tf_frames.get(sym)
                if frame is None or len(frame) <= strategy.warmup():
                    continue
                try:
                    signal = strategy.generate(tf_frames, sym, len(frame) - 1)
                except Exception as exc:  # noqa: BLE001
                    self.audit.append(EventType.SIGNAL,
                                      {"strategy": alloc.name, "instrument": sym,
                                       "error": str(exc)})
                    continue
                if signal is None or signal.side is None or signal.stop_price is None:
                    continue
                # The signal's timestamp is the BAR it was raised on, not the
                # wall clock: the client order id is derived from it, and only a
                # bar timestamp is stable across a restart that replays the bar.
                try:
                    signal.decision_ns = int(frame.index[-1].value)
                except (AttributeError, TypeError, ValueError):
                    pass
                decision = self._act_on_signal(signal, ctx, snap, regime_name)
                decisions.append(decision)
                # Refresh whenever an order reached the venue in ANY live
                # state, not just a fill -- and UNKNOWN is the MOST important
                # one, because that is precisely the state where the venue may
                # be holding a position we cannot see. Leaving it out meant the
                # second candidate in a cycle was judged against
                # unresolved_orders=0 and pending_risk=0, so the engine's
                # hardest pre-trade lock could not fire on the exact venue
                # behaviour the whole OMS exists to survive.
                if decision.diagnostics.get("venue_order_state") in (
                        "filled", "partial", "acked", "unknown"):
                    ctx = refresh()
        return decisions

    def _act_on_signal(self, signal: Signal, ctx: RiskContext, snap,
                       regime_name: str) -> Decision:
        cfg = self.config
        inst = ctx.instruments.get(signal.instrument)
        quote = ctx.quotes.get(signal.instrument)
        decision = Decision(
            ts_ns=signal.decision_ns, strategy=signal.strategy, instrument=signal.instrument,
            action="skipped", side=signal.side.value if signal.side else None,
            signal_strength=signal.strength, regime=regime_name,
            rationale=signal.rationale)

        if inst is None or quote is None:
            decision.vetoes = [{"rule": "no_market", "message": "no instrument or price"}]
            return decision

        # Lessons inform, they never override. News can only shrink.
        caution, reasons = self._caution_for(signal.strategy, signal.instrument,
                                             regime_name)
        decision.lessons = reasons
        # Carried on the decision so a proposal accepted later by a human is
        # sized with the same shrinkage the agent applied when it made it.
        meta_diag: Dict[str, Any] = {}

        # The meta-label gate: whether to act on THIS primary signal at all,
        # from the state it was raised in. It runs before the risk engine so a
        # skipped signal costs nothing, and it can only shrink -- its size
        # multiplier joins the caution product.
        if self.meta_gate is not None:
            try:
                from ..research.metalabel import bar_context_features
                frame = snap.frames_for(signal.timeframe).get(signal.instrument) \
                    if snap is not None else None
                context = bar_context_features(frame, len(frame) - 1) \
                    if frame is not None and len(frame) else {}
                act, p, scale = self.meta_gate.decide(signal, context)
                meta_diag["meta_features"] = {
                    **{f"sig_{k}": float(v) for k, v in (signal.features or {}).items()},
                    "strength": float(signal.strength), **context}
                if p is not None:
                    meta_diag["meta_probability"] = round(float(p), 4)
                decision.diagnostics.update(meta_diag)
                if not act:
                    decision.action = "skipped"
                    decision.vetoes = [{"rule": "meta_label",
                                        "message": f"act probability {p:.2f} below the "
                                                   f"filter's threshold "
                                                   f"{self.meta_gate.labeler.report.threshold:.2f}",
                                        "severity": "block"}]
                    decision.explanation = (f"{signal.strategy} sees {signal.side.value} "
                                            f"{signal.instrument}, but the meta-label filter "
                                            f"rates this setup at {p:.2f}: skipped.")
                    self.audit.append(EventType.SIGNAL, decision.to_dict())
                    return decision
                if scale < 1.0:
                    caution = float(min(caution, max(0.0, scale)))
                    decision.lessons.append(f"meta-label filter scales size by {scale:.2f}")
            except Exception as exc:  # noqa: BLE001 - never let the filter stop the loop
                self.audit.append(EventType.SIGNAL, {"meta_gate_error": str(exc)[:200]})

        # No process-local counter: (account, strategy, instrument, side, bar)
        # identifies exactly one intent, and a restarted process replaying the
        # same bar must produce the same key or the venue cannot deduplicate.
        coid = client_order_id(strategy=signal.strategy, instrument=signal.instrument,
                               side=signal.side.value, decision_ns=signal.decision_ns,
                               account=ctx.account.account_id)
        decision.client_order_id = coid
        try:
            intent = OrderIntent(
                client_order_id=coid, strategy=signal.strategy, instrument=signal.instrument,
                side=signal.side, lots=inst.min_lot,
                stop_loss=inst.round_price(signal.stop_price),
                take_profit=(inst.round_price(signal.target_price)
                             if signal.target_price else None),
                decision_ns=signal.decision_ns, reason=signal.rationale[:100],
                # Carry the strategy's own horizon through to the intent, so
                # the time stop has something to work from. Without this the
                # horizon was dropped here, _horizon_sec always returned 0, and
                # the time stop -- which had just been wired in -- was called
                # on every cycle with a limit of zero. A fix that runs and does
                # nothing is worse than one that is obviously missing.
                # ...and the timeframe those bars are counted in, so the
                # horizon is not converted with the feed's primary timeframe
                # for a strategy that runs on another.
                metadata=({"horizon_bars": signal.horizon_bars,
                           "timeframe": signal.timeframe}
                          if getattr(signal, "horizon_bars", 0) else {}))
        except ValueError as exc:
            decision.vetoes = [{"rule": "malformed_intent", "message": str(exc)}]
            return decision

        verdict: RiskDecision = self.risk.evaluate_entry(intent, ctx)
        decision.vetoes = [v.to_dict() for v in verdict.vetoes]
        decision.warnings = [w.to_dict() for w in verdict.warnings]
        # MERGE, do not replace. Assigning the engine's diagnostics wholesale
        # erased the meta-label probability and features recorded a few lines
        # above, so every approved or vetoed decision reached the dashboard
        # with no trace of the filter that had just scored it.
        decision.diagnostics = {**meta_diag, **dict(verdict.diagnostics)}
        decision.diagnostics["caution_multiplier"] = round(float(caution), 4)
        if getattr(signal, "horizon_bars", 0):
            decision.diagnostics["horizon_bars"] = int(signal.horizon_bars)
            decision.diagnostics["timeframe"] = signal.timeframe
        decision.stop = str(intent.stop_loss)
        decision.target = str(intent.take_profit) if intent.take_profit else None
        decision.entry = str(quote.price_for(signal.side))

        if verdict.halting:
            self.halt("risk engine halt: " +
                      "; ".join(v.rule for v in verdict.vetoes if v.severity is Severity.HALT))

        if not verdict.approved:
            decision.action = "vetoed"
            decision.explanation = self._explain(signal, verdict, caution, reasons, regime_name)
            self.audit.append(EventType.RISK_VETO, decision.to_dict())
            return decision

        # Caution can only shrink the position.
        lots = inst.round_lots_down(verdict.approved_lots * dec(caution))
        if lots < inst.min_lot:
            decision.action = "skipped"
            decision.vetoes.append({
                "rule": "caution_multiplier",
                "message": (f"accumulated lessons reduce the size to {lots} lots, below the "
                            f"{inst.min_lot} minimum"),
                "severity": "block"})
            decision.explanation = self._explain(signal, verdict, caution, reasons, regime_name)
            return decision

        intent.lots = lots
        intent.risk_amount = verdict.risk_amount * dec(caution)
        intent.risk_pct = verdict.risk_pct * dec(caution)
        intent.expected_cost_pips = verdict.expected_cost_pips
        decision.lots = str(lots)
        decision.risk_amount = str(intent.risk_amount)
        decision.risk_pct = str(intent.risk_pct)
        decision.explanation = self._explain(signal, verdict, caution, reasons, regime_name)

        mode = cfg.agent.mode
        if mode is AgentMode.OBSERVE:
            decision.action = "proposed"
            self.audit.append(EventType.SIGNAL, decision.to_dict())
            return decision
        if mode is AgentMode.ADVISORY or (
                mode is AgentMode.SEMI_AUTO and not self._within_envelope(intent)):
            decision.action = "queued"
            # One proposal per intent, and a bounded queue: a strategy that
            # re-raises the same signal every cycle for a week must not fill
            # memory with ten thousand copies of one idea.
            self._advisory_queue = [d for d in self._advisory_queue
                                    if d.client_order_id != decision.client_order_id]
            self._advisory_queue.append(decision)
            if len(self._advisory_queue) > 500:
                self._advisory_queue = self._advisory_queue[-500:]
            self.audit.append(EventType.PROPOSAL, decision.to_dict())
            return decision

        return self._execute(intent, decision, quote)

    def _caution_for(self, strategy: str, instrument: str,
                     regime_name: str) -> tuple[float, List[str]]:
        """The size multiplier from lessons and news, in [0, 1], with reasons.

        One function for the agent's own entries AND for a human accepting a
        proposal later, so the two cannot drift apart. Both inputs can only
        shrink a position: lessons are capped at 1.0 by the memory store, and
        the news multiplier is clamped here.
        """
        caution, reasons = self.memory.caution_multiplier(
            strategy=strategy, instrument=instrument, regime=regime_name)
        reasons = list(reasons)
        assessment = self._news_assessment.get(instrument)
        if assessment is not None and assessment.size_multiplier < D("1"):
            caution = float(min(dec(caution), assessment.size_multiplier))
            reasons.extend(assessment.reasons[:2])
        return max(0.0, min(1.0, float(caution))), reasons

    def entry_permission(self) -> tuple[bool, str]:
        """May NEW risk be opened right now, as far as the licence is concerned?

        The licence gate used to be consulted once, at boot. A licence that
        expired while the process stayed up kept authorising live entries until
        the next restart -- months, on a stable server -- although the gate
        itself re-evaluates every 15 minutes and the dashboard said "expired".
        The question is now asked on every cycle, and on every human path that
        opens a position. Only NEW risk is refused; exits and protection are
        never gated by a licence.
        """
        if self.config.execution.venue_mode is not ExecutionVenueMode.LIVE:
            return True, ""
        if self.entry_gate is None:
            return True, ""
        try:
            allowed, why = self.entry_gate()
        except Exception as exc:  # noqa: BLE001 - an unanswerable gate is a closed gate
            return False, f"the licence gate could not be evaluated ({exc})"
        return bool(allowed), str(why or "")

    def _realised_risk(self, intent: OrderIntent, order) -> Decimal:
        """Risk of the position the venue actually opened.

        Computed from the average fill price, the stop the intent carried, and
        the lots that filled -- plus the commission already paid, which is part
        of what a stop-out costs. Falls back to the intent's own number when a
        fill price is unavailable.
        """
        fill_px = order.avg_fill_price
        lots = order.filled_lots or intent.lots
        if fill_px is None or intent.stop_loss is None or lots <= 0:
            return intent.risk_amount
        inst = self.broker.instruments().get(intent.instrument)
        if inst is None:
            return intent.risk_amount
        try:
            conv = self._conversion_map().get(inst.quote)
            if conv is None or conv <= 0:
                return intent.risk_amount
            distance = abs(fill_px - intent.stop_loss)
            pips = distance / inst.pip
            pip_val = inst.pip_value_quote(lots) * conv
            return pips * pip_val
        except Exception:  # noqa: BLE001
            return intent.risk_amount

    def _horizon_sec(self, intent: OrderIntent) -> int:
        """Intended holding horizon in seconds, from the signal that opened it."""
        horizon_bars = intent.metadata.get("horizon_bars") if intent.metadata else None
        try:
            bars = int(horizon_bars) if horizon_bars else 0
        except (TypeError, ValueError):
            return 0
        if bars <= 0:
            return 0
        timeframe = (intent.metadata.get("timeframe") if intent.metadata else None) \
            or self.feed.timeframe
        seconds = _TIMEFRAME_SECONDS.get(str(timeframe), 0)
        return bars * seconds

    def _within_envelope(self, intent: OrderIntent) -> bool:
        env = self.config.agent.semi_auto_envelope
        if intent.instrument not in env.get("instruments", []):
            return False
        if float(intent.lots) > float(env.get("max_lots", 0)):
            return False
        if float(intent.risk_pct) > float(env.get("max_risk_pct", 0)):
            return False
        return True

    def _execute(self, intent: OrderIntent, decision: Decision, quote: Quote) -> Decision:
        try:
            order = self.oms.submit(intent, decision_price=quote.price_for(intent.side))
        except UnknownOutcomeError as exc:
            decision.action = "vetoed"
            decision.vetoes.append({"rule": "quarantine", "message": str(exc),
                                    "severity": "block"})
            return decision
        # An ACKED order is live at the venue even though it has not filled, so
        # it consumes the frequency budget and the spacing rule exactly like a
        # fill. Counting only fills let an ack-then-fill venue -- the normal
        # OANDA path -- bypass every frequency cap.
        # Record what the venue actually did, separately from the label shown in
        # the UI. The caller refreshes its risk context on this, not on the
        # display string: an ACKED order is live at the venue even though it
        # reads as "queued", and on an ack-then-fill venue -- OANDA's normal
        # path -- keying the refresh on "executed" alone left the whole
        # within-cycle bypass intact.
        decision.diagnostics["venue_order_state"] = order.state.value
        if order.state is OrderState.UNKNOWN:
            # UNKNOWN exists because the venue MAY have filled. Recording nothing
            # leaves whatever the previous trade left behind, so the entry is
            # written and marked provisional: reconciliation either confirms it
            # or the side check in _hydrate discards it.
            self._position_meta[intent.instrument] = {
                "initial_risk": intent.risk_amount,
                "strategy": intent.strategy,
                "side": intent.side.value,
                "client_order_id": intent.client_order_id,
                "opened_ns": self.now(),
                "regime": self.regime.regime.value if self.regime else "",
                "provisional": True,
                "partial_taken": False,
                "breakeven_moved": False,
            }
            # An order that MAY have filled consumes the budget. Releasing it
            # back is what reconciliation is for; spending it twice is not
            # recoverable. Without this, a venue that times out -- the one the
            # OMS exists for -- let the agent take unlimited entries while
            # every frequency cap read as untouched.
            self.trades_today += 1
            self.trades_week += 1
            self.trades_year += 1
            self.last_entry_ns = self.now()
            self._save_state()
        if order.state in (OrderState.FILLED, OrderState.PARTIAL, OrderState.ACKED):
            decision.action = "executed" if order.state is not OrderState.ACKED else "queued"
            self.trades_today += 1
            self.trades_week += 1
            self.trades_year += 1
            self.last_entry_ns = self.now()
            if order.filled_lots > 0 or order.state is OrderState.ACKED:
                # Risk is re-derived from what the venue ACTUALLY did, not from
                # the reference price the sizing used. Slippage inside the
                # configured tolerance was enough to push a book sized at
                # exactly 2.00% to a real 2.30% while every limit read green,
                # because the engine kept quoting the pre-fill number.
                realised_risk = self._realised_risk(intent, order)
                self._position_meta[intent.instrument] = {
                    "initial_risk": realised_risk,
                    "intended_risk": intent.risk_amount,
                    "strategy": intent.strategy,
                    "side": intent.side.value,
                    "lots": order.filled_lots or intent.lots,
                    "entry_price": order.avg_fill_price or intent.limit_price or ZERO,
                    "stop_loss": intent.stop_loss,
                    "client_order_id": intent.client_order_id,
                    "opened_ns": self.now(),
                    # The regime is captured HERE, at entry. Stamping it when the
                    # trade is later processed records the regime the agent
                    # happened to be in days afterwards, which is exactly the
                    # guess the attribution exists to replace.
                    "regime": self.regime.regime.value if self.regime else "",
                    "max_hold_sec": self._horizon_sec(intent),
                    "partial_taken": False,
                    "breakeven_moved": False,
                }
                self._entry_regime[intent.instrument] = (
                    self.regime.regime.value if self.regime else "")
            self._save_state()
        elif order.state is OrderState.REJECTED:
            decision.action = "vetoed"
            decision.vetoes.append({"rule": "broker_reject",
                                    "message": order.reject_reason or "rejected",
                                    "severity": "block"})
        else:
            decision.action = "queued"
        return decision

    def _explain(self, signal: Signal, verdict: RiskDecision, caution: float,
                 lessons: List[str], regime_name: str) -> str:
        parts = [f"{signal.strategy} sees {signal.side.value} {signal.instrument}: "
                 f"{signal.rationale}."]
        if regime_name:
            parts.append(f"Regime: {regime_name}.")
        if verdict.break_even_win_rate is not None:
            parts.append(f"Needs {float(verdict.break_even_win_rate) * 100:.1f}% accuracy to "
                         f"break even after {float(verdict.expected_cost_pips):.2f}p of cost.")
        if verdict.risk_multiplier != D("1"):
            parts.append(
                f"Risk budget scaled by {verdict.risk_multiplier} "
                f"(drawdown ladder {verdict.diagnostics.get('ladder_multiplier', '1')}, "
                f"regime {verdict.diagnostics.get('regime_multiplier', '1')}).")
        if caution < 1.0:
            parts.append(f"Past lessons scale size by {caution:.2f}: " + "; ".join(lessons[:2]))
        if verdict.vetoes:
            parts.append("Refused because " +
                         "; ".join(f"{v.rule} ({v.message})" for v in verdict.vetoes[:3]) + ".")
        elif verdict.warnings:
            parts.append("Warnings: " + "; ".join(w.message for w in verdict.warnings[:2]) + ".")
        return " ".join(parts)

    # ------------------------------------------------------------------ #
    # advisory queue
    # ------------------------------------------------------------------ #

    def pending_advice(self) -> List[Decision]:
        return list(self._advisory_queue)

    def accept_advice(self, client_order_id: str, by: str) -> Optional[Decision]:
        """A human accepts a proposal. Re-checked against the risk engine first --
        the market has moved since the proposal was made."""
        target = next((d for d in self._advisory_queue if d.client_order_id == client_order_id),
                      None)
        if target is None:
            return None
        permitted, why = self.entry_permission()
        if not permitted:
            self.audit.append(EventType.PROPOSAL_REJECTED,
                              {"client_order_id": client_order_id,
                               "reason": f"licence: {why}"}, actor=by)
            target.action = "vetoed"
            target.vetoes.append({"rule": "licence", "message": why, "severity": "block"})
            return target
        if target.strategy in self._guard_suspended:
            # The performance guard suspended this strategy after the proposal
            # was queued. A proposal is the strategy's idea; a strategy whose
            # realised R is demonstrably negative does not get to place it
            # through the back door of a human click.
            self._advisory_queue.remove(target)
            self.audit.append(EventType.PROPOSAL_REJECTED,
                              {"client_order_id": client_order_id,
                               "reason": "strategy suspended by the performance guard"},
                              actor=by)
            target.action = "vetoed"
            target.vetoes.append({
                "rule": "performance_guard",
                "message": self._guard_suspended[target.strategy], "severity": "block"})
            return target
        if not self._in_session(self.now()):
            # The agent itself would not enter now; a human clicking "accept"
            # at 03:00 on a Sunday does not change what the session rule is for.
            self.audit.append(EventType.PROPOSAL_REJECTED,
                              {"client_order_id": client_order_id,
                               "reason": "accepted outside the permitted session"}, actor=by)
            target.action = "vetoed"
            target.vetoes.append({"rule": "out_of_session",
                                  "message": "entries are not permitted at this hour/day",
                                  "severity": "block"})
            return target
        self._advisory_queue.remove(target)
        snap = self.feed.snapshot([target.instrument], now_ns=self.now())
        quote = snap.quotes.get(target.instrument)
        inst = self.broker.instruments().get(target.instrument)
        if quote is None or inst is None:
            target.action = "vetoed"
            target.vetoes.append({"rule": "stale_proposal",
                                  "message": "no current price for re-validation",
                                  "severity": "block"})
            return target
        account = self.broker.account()
        ctx = self._build_context(self.now(), account,
                                  self._hydrate(self.broker.positions()), snap,
                                  self.health.connected, self.health.snapshot())
        horizon = target.diagnostics.get("horizon_bars") if target.diagnostics else None
        intent = OrderIntent(
            client_order_id=target.client_order_id, strategy=target.strategy,
            instrument=target.instrument, side=Side(target.side),
            lots=dec(target.lots or inst.min_lot),
            stop_loss=dec(target.stop) if target.stop else None,
            take_profit=dec(target.target) if target.target else None,
            decision_ns=target.ts_ns,
            reason="human-accepted advisory proposal",
            # The horizon travels with the proposal, so a trade a human
            # accepted still gets the time stop its strategy was tested with.
            metadata=({"horizon_bars": int(horizon),
                       "timeframe": target.diagnostics.get("timeframe")}
                      if horizon else {}))
        verdict = self.risk.evaluate_entry(intent, ctx)
        self.audit.append(EventType.PROPOSAL_ACCEPTED,
                          {"client_order_id": client_order_id,
                           "revalidated": verdict.approved,
                           "vetoes": [v.to_dict() for v in verdict.vetoes]}, actor=by)
        if not verdict.approved:
            target.action = "vetoed"
            target.vetoes = [v.to_dict() for v in verdict.vetoes]
            return target
        # The SAME shrinkage the agent would apply, recomputed now and never
        # looser than when the proposal was made. The engine sizes from the
        # full per-trade budget, so assigning its lots directly let a human
        # click place a position up to 1/caution times larger than the agent
        # itself would have: every lesson, news advisory and meta-label scale
        # that had shrunk the proposal was silently discarded on acceptance.
        regime_name = self.regime.regime.value if self.regime else ""
        fresh, reasons = self._caution_for(target.strategy, target.instrument, regime_name)
        try:
            proposed = float(target.diagnostics.get("caution_multiplier", 1.0))
        except (TypeError, ValueError):
            proposed = 1.0
        caution = max(0.0, min(1.0, fresh, proposed))
        lots = inst.round_lots_down(verdict.approved_lots * dec(caution))
        if lots < inst.min_lot:
            target.action = "vetoed"
            target.vetoes.append({
                "rule": "caution_multiplier",
                "message": (f"lessons and news reduce the size to {lots} lots, below the "
                            f"{inst.min_lot} minimum"),
                "severity": "block"})
            target.lessons = reasons
            return target
        intent.lots = lots
        intent.risk_amount = verdict.risk_amount * dec(caution)
        intent.risk_pct = verdict.risk_pct * dec(caution)
        intent.expected_cost_pips = verdict.expected_cost_pips
        target.lots = str(lots)
        target.risk_amount = str(intent.risk_amount)
        target.risk_pct = str(intent.risk_pct)
        target.diagnostics["caution_multiplier"] = round(caution, 4)
        return self._execute(intent, target, quote)

    # ------------------------------------------------------------------ #
    # manual trading
    # ------------------------------------------------------------------ #

    MANUAL_STRATEGY = "manual"

    def manual_order(self, *, instrument: str, side: str, stop_loss: Decimal,
                     take_profit: Optional[Decimal], by: str,
                     risk_pct: Optional[Decimal] = None,
                     preview: bool = False) -> Decision:
        """A human's own trade, through the same gates as the agent's.

        The ticket names the instrument, the side, the stop (required) and
        optionally a target and a LOWER risk percentage. Everything else is the
        system's: the size comes from the risk budget, and every veto the
        engine has -- loss budgets, drawdown ladder, news blackout, spread,
        exposure, margin, the unprotected-book rule -- applies unchanged. There
        is no override. The only thing a human ticket is excused from is the
        strategy LIFECYCLE rule, and with real money only when the owner has
        turned ``agent.manual_trading_live`` on.

        ``preview=True`` evaluates everything and sends nothing.
        """
        now = self.now()
        decision = Decision(ts_ns=now, strategy=self.MANUAL_STRATEGY,
                            instrument=instrument, action="skipped", side=side,
                            rationale=f"manual ticket by {by}")
        try:
            side_enum = Side(str(side).upper())
        except ValueError:
            decision.vetoes = [{"rule": "malformed_ticket",
                                "message": "side must be BUY or SELL", "severity": "block"}]
            return decision
        live = self.config.execution.venue_mode is ExecutionVenueMode.LIVE
        if live and not self.config.agent.manual_trading_live:
            decision.action = "vetoed"
            decision.vetoes = [{
                "rule": "manual_live_disabled",
                "message": ("manual trades with real money are switched off; the owner "
                            "must enable agent.manual_trading_live first"),
                "severity": "block"}]
            return decision
        permitted, why = self.entry_permission()
        if not permitted:
            decision.action = "vetoed"
            decision.vetoes = [{"rule": "licence", "message": why, "severity": "block"}]
            return decision
        if not self._in_session(now):
            decision.action = "vetoed"
            decision.vetoes = [{
                "rule": "out_of_session",
                "message": ("entries are not permitted at this hour/day; the permitted "
                            "hours are agent.session_windows_utc and agent.trade_days"),
                "severity": "block"}]
            return decision

        snap = self.feed.snapshot([instrument], now_ns=now)
        quote = snap.quotes.get(instrument)
        inst = self.broker.instruments().get(instrument)
        if quote is None or inst is None:
            decision.action = "vetoed"
            decision.vetoes = [{"rule": "no_market",
                                "message": f"no instrument or live price for {instrument}",
                                "severity": "block"}]
            return decision
        ctx = self._build_context(now, self.broker.account(),
                                  self._hydrate(self.broker.positions()), snap,
                                  self.health.connected, self.health.snapshot())
        # The ticket is judged as an accepted strategy ONLY for the lifecycle
        # rule, and only when the checks above allowed a manual ticket at all.
        ctx.strategy_lifecycles = {**ctx.strategy_lifecycles,
                                   self.MANUAL_STRATEGY: "accepted"}
        coid = client_order_id(strategy=self.MANUAL_STRATEGY, instrument=instrument,
                               side=side_enum.value, decision_ns=now,
                               account=ctx.account.account_id)
        decision.client_order_id = coid
        try:
            intent = OrderIntent(
                client_order_id=coid, strategy=self.MANUAL_STRATEGY, instrument=instrument,
                side=side_enum, lots=inst.min_lot,
                stop_loss=inst.round_price(dec(stop_loss)),
                take_profit=inst.round_price(dec(take_profit)) if take_profit else None,
                decision_ns=now, reason=f"manual ticket by {by}"[:100])
        except (ValueError, InvalidOperation) as exc:
            decision.action = "vetoed"
            decision.vetoes = [{"rule": "malformed_ticket", "message": str(exc),
                                "severity": "block"}]
            return decision

        verdict = self.risk.evaluate_entry(intent, ctx)
        decision.vetoes = [v.to_dict() for v in verdict.vetoes]
        decision.warnings = [w.to_dict() for w in verdict.warnings]
        decision.diagnostics = dict(verdict.diagnostics)
        decision.entry = str(quote.price_for(side_enum))
        decision.stop = str(intent.stop_loss)
        decision.target = str(intent.take_profit) if intent.take_profit else None
        if not verdict.approved:
            decision.action = "vetoed"
            decision.explanation = "; ".join(
                f"{v.rule}: {v.message}" for v in verdict.vetoes[:4])
            if not preview:
                self.audit.append(EventType.RISK_VETO, {**decision.to_dict(),
                                                        "manual": True}, actor=by)
            return decision

        # A human may ask for LESS risk than the budget, never more. News and
        # lessons shrink a manual ticket exactly as they shrink the agent's.
        regime_name = self.regime.regime.value if self.regime else ""
        caution, reasons = self._caution_for(self.MANUAL_STRATEGY, instrument, regime_name)
        budget = self.config.risk.risk_per_trade_pct
        if risk_pct is not None:
            wanted = dec(risk_pct)
            if wanted <= 0:
                decision.action = "vetoed"
                decision.vetoes = [{"rule": "malformed_ticket",
                                    "message": "risk must be greater than zero",
                                    "severity": "block"}]
                return decision
            if wanted < budget:
                caution = min(caution, float(wanted / budget))
                reasons.append(f"risk lowered by the ticket to {wanted}%")
        lots = inst.round_lots_down(verdict.approved_lots * dec(caution))
        decision.lessons = reasons
        decision.diagnostics["caution_multiplier"] = round(caution, 4)
        if lots < inst.min_lot:
            decision.action = "vetoed"
            decision.vetoes.append({
                "rule": "caution_multiplier",
                "message": f"the requested risk sizes to {lots} lots, below the "
                           f"{inst.min_lot} minimum",
                "severity": "block"})
            return decision
        intent.lots = lots
        intent.risk_amount = verdict.risk_amount * dec(caution)
        intent.risk_pct = verdict.risk_pct * dec(caution)
        intent.expected_cost_pips = verdict.expected_cost_pips
        decision.lots = str(lots)
        decision.risk_amount = str(intent.risk_amount)
        decision.risk_pct = str(intent.risk_pct)
        be = verdict.break_even_win_rate
        decision.explanation = (
            f"manual {side_enum.value} {instrument}: {lots} lots, risk "
            f"{float(intent.risk_pct):.2f}% of equity"
            + (f"; needs {float(be) * 100:.1f}% wins to break even after costs"
               if be is not None else ""))
        if preview:
            decision.action = "preview"
            return decision
        self.audit.append(EventType.PROPOSAL_ACCEPTED, {
            "manual_ticket": True, "client_order_id": coid, "instrument": instrument,
            "side": side_enum.value, "lots": str(lots), "stop": str(intent.stop_loss),
            "target": str(intent.take_profit) if intent.take_profit else None,
            "risk_pct": str(intent.risk_pct)}, actor=by)
        result = self._execute(intent, decision, quote)
        self.decisions.append(result)
        return result

    def reject_advice(self, client_order_id: str, by: str, reason: str = "") -> bool:
        target = next((d for d in self._advisory_queue if d.client_order_id == client_order_id),
                      None)
        if target is None:
            return False
        self._advisory_queue.remove(target)
        self.audit.append(EventType.PROPOSAL_REJECTED,
                          {"client_order_id": client_order_id, "reason": reason}, actor=by)
        return True

    # ------------------------------------------------------------------ #
    # learning
    # ------------------------------------------------------------------ #

    def _closed_trades(self) -> List[ClosedTrade]:
        """Realised history since the last cycle, from whatever the venue offers.

        Previously this read an in-memory list that only the simulator has, so
        the entire learning subsystem -- autopsies, lessons, caution multipliers,
        proposals -- did nothing at all on a live or demo venue while the
        dashboard looked perfectly healthy.
        """
        if not self.broker.supports_closed_trade_history:
            if not self._warned_no_history:
                self._warned_no_history = True
                self.audit.append(EventType.LESSON, {
                    "learning_disabled": True,
                    "reason": f"{self.broker.capabilities.name} exposes no realised "
                              "trade history, so post-mortems and lessons cannot be "
                              "produced. This is a capability gap, not an absence "
                              "of trades."})
            return []
        try:
            trades, cursor = self.broker.fetch_closed_trades(self._trade_cursor)
        except Exception as exc:  # noqa: BLE001 - history is not worth a crash
            self.audit.append(EventType.LESSON, {"trade_history_read_failed": str(exc)})
            return []
        self._trade_cursor = cursor
        return trades

    def _trade_path(self, trade: ClosedTrade) -> Optional[list]:
        """The bars the trade actually lived through, in R-space.

        Without this the counterfactual engine can only score the one
        alternative that needs no ordering -- a scale-out -- and everything that
        depends on WHEN a level was touched is reported as not computable rather
        than guessed at. Supplying the bars is what turns the break-even and
        trailing counterfactuals from assertions into measurements.

        Returns None whenever any ingredient is missing. A partial path would
        be worse than none: a counterfactual scored on half a trade is wrong
        with a number attached.
        """
        from .postmortem import path_in_r

        try:
            risk_distance = self._trade_risk_distance(trade)
            if risk_distance is None or risk_distance <= 0:
                return None
            frame = self.feed.store.frame(trade.instrument, self.feed.timeframe, 5000)
            if frame is None or len(frame) == 0:
                return None
            if getattr(frame.index, "tz", None) is None:
                return None
            # `.as_unit("ns")` before the cast: a microsecond-resolution index
            # viewed as int64 yields microseconds, and every timestamp compare
            # below would then be off by a factor of a thousand.
            ts = frame.index.tz_convert("UTC").as_unit("ns").astype("int64").to_numpy()
            lo, hi = int(trade.opened_ns), int(trade.closed_ns)
            bars = [(int(ts[i]), float(frame["open"].iloc[i]), float(frame["high"].iloc[i]),
                     float(frame["low"].iloc[i]), float(frame["close"].iloc[i]))
                    for i in range(len(frame)) if lo <= int(ts[i]) <= hi]
            if len(bars) < 2:
                return None
            return path_in_r(bars, entry_price=float(trade.entry_price),
                             risk_price_distance=risk_distance, side=trade.side)
        except Exception:  # noqa: BLE001 - a missing path is a degraded autopsy, not a crash
            return None

    def _trade_risk_distance(self, trade: ClosedTrade) -> Optional[float]:
        """Entry-to-stop distance in price terms, for one R.

        ClosedTrade does not carry the stop, so this is recovered from the two
        things it does carry: the move in pips and the realised R. Refused when
        the trade closed near flat -- dividing a small pip move by a small R is
        numerically meaningless, and a wrong R scale silently distorts every
        counterfactual for that trade in a direction nobody would notice.
        """
        r = abs(float(trade.r_multiple))
        pips = abs(float(trade.pnl_pips))
        if r < 0.25 or pips <= 0:
            return None
        inst = self.broker.instruments().get(trade.instrument)
        if inst is None:
            return None
        return (pips / r) * float(inst.pip)

    def _performance_guard(self, strategy_name: str) -> None:
        """Suspend a strategy whose realised R is demonstrably negative.

        Over the most recent ``performance_guard_min_trades`` closed trades,
        the mean R is estimated from five block means (blocks, because losses
        cluster in time and consecutive trades are not independent draws), and
        a one-sided 95% upper confidence bound is formed with Student's t on
        four degrees of freedom. If even that generous bound is below zero,
        the strategy has spent its evidence: it opens nothing further until a
        human reviews it. Nothing in the agent lifts the suspension.

        This is not a profitability test -- passing it says only "not yet
        proven to lose". It exists because a strategy that IS losing must not
        get to keep proving it with the account's money.
        """
        if not self.config.agent.performance_guard_enabled:
            return
        if strategy_name in self._guard_suspended:
            return
        count = int(self.config.agent.performance_guard_min_trades)
        records = self.memory.autopsies(strategy_name, limit=count)
        if len(records) < count:
            return
        try:
            import numpy as np
            from scipy.stats import t as student_t
            values = np.array([float(r["r_multiple"]) for r in records[:count]], dtype=float)
            values = values[np.isfinite(values)]
            if values.size < count:
                return
            blocks = np.array([b.mean() for b in np.array_split(values, 5)])
            spread = float(blocks.std(ddof=1))
            if not np.isfinite(spread):
                return
            upper = float(blocks.mean() + student_t.ppf(0.95, 4) * spread / np.sqrt(5))
        except Exception as exc:  # noqa: BLE001 - a guard that crashes protects nothing
            self.audit.append(EventType.LESSON,
                              {"performance_guard_error": str(exc), "strategy": strategy_name})
            return
        if upper < 0:
            reason = (f"{count} closed trades, mean R {float(values.mean()):+.3f}, "
                      f"95% upper bound {upper:+.3f} < 0")
            self._guard_suspended[strategy_name] = reason
            self.audit.append(EventType.HALT, {
                "performance_guard": True, "strategy": strategy_name, "reason": reason,
                "effect": "no new entries for this strategy; open positions still managed; "
                          "a human clears it with Agent.release_guard"})
            self._save_state()

    def release_guard(self, strategy_name: str, by: str) -> bool:
        """Only a human releases a performance suspension."""
        if strategy_name not in self._guard_suspended:
            return False
        reason = self._guard_suspended.pop(strategy_name)
        self.audit.append(EventType.MODE_CHANGE,
                          {"performance_guard_released": strategy_name, "was": reason},
                          actor=by)
        self._save_state()
        return True

    @property
    def guard_suspended(self) -> Dict[str, str]:
        return dict(self._guard_suspended)

    def _learn(self) -> tuple[int, int]:
        if not self.config.agent.learning_enabled:
            return 0, 0
        new_autopsies = 0
        for trade in self._closed_trades():
            if trade.trade_id in self.processed_trades:
                continue
            self.processed_trades.add(trade.trade_id)
            self._processed_order.append(trade.trade_id)
            if not trade.regime:
                # Entry-time regime, captured when the position was opened.
                trade.regime = self._entry_regime.get(trade.instrument, "")
            if trade.initial_risk <= 0:
                # No R, no lesson. A trade whose risk the agent cannot attribute
                # (opened by hand, or before the journal existed) is history,
                # not evidence; feeding it into the R-denominated statistics
                # would teach a lesson about a number that was never defined.
                self.audit.append(EventType.LESSON, {
                    "trade_id": trade.trade_id, "learning_skipped": "no risk attribution",
                    "pnl_kept_in_history": str(trade.pnl)})
                continue
            a = autopsy(trade, path=self._trade_path(trade))
            self.memory.record_autopsy(a.to_dict())
            self.audit.append(EventType.POSTMORTEM, a.to_dict())
            new_autopsies += 1

        if new_autopsies == 0:
            return 0, 0

        proposals_created = 0
        lessons_added = 0
        # The global (None) pass re-aggregates the SAME rows as the per-strategy
        # pass whenever there is only one strategy, producing two lessons from one
        # body of evidence whose caution multipliers then compound. Only widen to
        # the global scope when there is genuinely more than one source.
        scopes: set = set(self.strategies)
        if len(self.strategies) > 1:
            scopes.add(None)
        for strategy_name in scopes:
            total = self.memory.autopsy_count(strategy_name)
            if strategy_name and total >= self.config.agent.performance_guard_min_trades:
                self._performance_guard(strategy_name)
            if total < self.config.agent.proposal_min_sample:
                continue
            records = self.memory.autopsies(strategy_name, limit=1000)
            from .postmortem import TradeAutopsy, Counterfactual

            objs = [TradeAutopsy(
                trade_id=r["trade_id"], strategy=r["strategy"], instrument=r["instrument"],
                outcome=r["outcome"], mode=r["mode"], r_multiple=r["r_multiple"],
                mae_r=r["mae_r"], mfe_r=r["mfe_r"], capture_ratio=r["capture_ratio"],
                tags=r.get("tags", []),
                counterfactuals=[Counterfactual(**c) for c in r.get("counterfactuals", [])],
            ) for r in records]
            findings = aggregate(objs, min_sample=max(20, total // 4),
                                 alpha=self.config.research.alpha)

            # Re-test what the agent already believes against the same fresh
            # evidence, BEFORE learning anything new. A lesson that no longer
            # holds must stop taxing decisions; a lesson store that only ever
            # grows is a permanent bias with an audit trail.
            self.memory.review_against(findings, alpha=self.config.research.alpha)
            self.memory.expire_unconfirmed()

            for f in findings:
                # The corrected p-value, not the raw one. Twenty patterns tested
                # at the declared alpha produce a winner by chance on nearly
                # every pass, and a lesson minted from one is a permanent
                # caution derived from noise.
                p_eff = f.p_value_adjusted if f.n_tests_in_family else f.p_value
                if not (p_eff < self.config.research.alpha and f.n >= 25
                        and f.pattern.startswith("tag:") and f.mean_delta_r < -0.15
                        and (f.significant or not f.n_tests_in_family)):
                    continue
                if f.diagnosis in ("luck", "insufficient", "descriptive"):
                    continue
                # A regime-confined effect becomes a regime-SCOPED lesson, so it
                # is recalled only in the market state that produced it. The
                # same statement held globally is a tax on every other state.
                scoped_regime = (f.dominant_regime
                                 if f.diagnosis == "regime"
                                 and f.dominant_regime not in ("", "unknown") else None)
                lesson = Lesson(
                    scope=("regime" if scoped_regime else
                           ("strategy" if strategy_name else "global")),
                    strategy=strategy_name, regime=scoped_regime,
                    statement=f.recommendation,
                    evidence=f.to_dict(), sample_size=f.n, effect_r=f.mean_delta_r,
                    p_value=p_eff,
                    caution=max(0.5, 1.0 + f.mean_delta_r / 2.0))
                self.memory.add_lesson(lesson)
                self.audit.append(EventType.LESSON, lesson.to_dict())
                lessons_added += 1

            # Counterfactual findings that are real but confined to one regime
            # become lessons too, rather than the global parameter change the
            # proposal engine would otherwise have derived from them.
            for spec in regime_lessons(findings, strategy=strategy_name,
                                       min_sample=self.config.agent.proposal_min_sample,
                                       alpha=self.config.research.alpha):
                lesson = Lesson(**spec)
                self.memory.add_lesson(lesson)
                self.audit.append(EventType.LESSON, lesson.to_dict())
                lessons_added += 1

            for proposal in derive_proposals(
                    findings, self.config.model_dump(mode="json"),
                    strategy=strategy_name,
                    min_sample=self.config.agent.proposal_min_sample,
                    alpha=self.config.research.alpha):
                self.proposals.add(proposal)
                self.audit.append(EventType.PARAM_PROPOSAL, proposal.to_dict())
                proposals_created += 1
        return lessons_added, proposals_created
