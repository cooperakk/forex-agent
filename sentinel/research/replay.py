"""Replay the PRODUCTION agent -- cycle, OMS, risk engine, protection, news --
over historical bars, with an explicit execution clock.

The legacy backtester (``backtest.run_backtest``) reproduces the agent's rules;
this module runs the agent. The difference is the whole point of gate L11: a
harness that re-implements the trading rules validates a program that will
never be deployed, and every divergence between the two -- the order the
protections run in, the refresh of the risk context after a fill, the halt on
a partial close, the session gate, the advisory queue -- is a place where the
accepted system and the running system quietly disagree.

Two clocks. The DECISION bars are the strategy's own timeframe and become
visible to it only after they close. The EXECUTION bars are finer (M1 for a
60-second cycle) and drive the paper venue: each one is pushed as an
open -> adverse extreme -> other extreme -> close path, and the agent cycles
once per execution bar, exactly as it cycles once per decision interval in
production. When no execution data is supplied the decision bars drive both,
the cadence equals the timeframe, and the replay says so -- L11 then fails,
honestly, because a system that decides every minute was replayed deciding
every four hours.

Bid/ask. When the execution frame carries ``bid_*``/``ask_*`` columns the
venue is fed the real spread; otherwise the simulator's session spread model
is used and ``bid_ask_quotes`` is False in the diagnostics. L10 reads that.

News. A ``HistoricalCalendarSource`` (a CSV of dated releases) can be
supplied; the agent's news policy then sees the calendar as it stood at the
simulated clock, so a strategy validated with the news filter on is the
strategy that runs with it on. Without one, ``news.enabled`` must be False
for the replay to count as a shared runtime.

What this is not: tick history. The intrabar path is an adverse-first
approximation, stated in the diagnostics, and stays so until the execution
bars are built from the venue's own ticks (``data/ticks.py``).
"""

from __future__ import annotations

import tempfile
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..agent.memory import MemoryStore
from ..agent.orchestrator import Agent
from ..agent.proposals import ProposalQueue
from ..brokers.paper import PaperBroker, SimProfile
from ..core.audit import AuditLog
from ..core.config import (DEADMAN_MARGIN_SEC, AgentMode, ExecutionVenueMode, RiskConfig,
                           SentinelConfig, StrategyAllocation, deadman_timeout_ok)
from ..core.money import D, Instrument, dec
from ..core.types import DataQuality, Quote, Side
from ..data.feed import TIMEFRAME_SECONDS, DataPassport, MarketFeed
from ..data.validation import has_bid_ask, validate_universe
from ..strategy.base import Strategy
from .metrics import compute_performance
from .trials import record_trial, window_key

ENGINE_NAME = "agent-replay-v1"


@dataclass
class ReplayConfig:
    starting_equity: Decimal = D("10000")
    account_currency: str = "USD"
    cost_multiplier: float = 1.0
    latency_multiplier: float = 1.0
    seed: int = 20260914
    warmup_bars: Optional[int] = None
    label: str = "replay"
    data_label: str = "unknown"
    trial_kind: str = "search"
    record_trial: bool = True
    periods_per_year: int = 252
    #: The configuration the agent runs under: risk, agent gating, news,
    #: execution costs. Defaults are used when None.
    runtime_config: Optional[SentinelConfig] = None
    #: Finer bars that drive execution. None -> the decision bars drive it.
    execution_data: Optional[Dict[str, pd.DataFrame]] = None
    #: A CalendarSource of DATED historical releases, or None.
    news_source: Any = None
    #: Bars ahead over which a signal's forward return is measured (L2).
    forward_return_bars: int = 8
    #: A fitted research.metalabel.MetaGate for the agent to consult.
    meta_gate: Any = None


class _ReplayStore:
    """A BarStore-shaped view that reveals a bar only after it has closed."""

    def __init__(self, data: Dict[str, pd.DataFrame], timeframe: str,
                 clock: Callable[[], int], label: str) -> None:
        self._data = data
        self._tf = timeframe
        self._seconds = TIMEFRAME_SECONDS[timeframe]
        self._clock = clock
        self._label = label
        self._ends = {s: df.index.as_unit("ns").asi8 + self._seconds * 10**9
                      for s, df in data.items()}

    def frame(self, symbol: str, timeframe: str, limit: int = 5000,
              complete_only: bool = True) -> pd.DataFrame:
        if timeframe != self._tf or symbol not in self._data:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        visible = self._ends[symbol] <= self._clock()
        return self._data[symbol][visible].tail(limit)

    def latest_start_ns(self, symbol: str, timeframe: str,
                        complete_only: bool = True) -> Optional[int]:
        f = self.frame(symbol, timeframe, 1)
        return int(f.index[-1].value) if len(f) else None

    def passport(self, symbol: str, timeframe: str, now_ns: Optional[int] = None) -> DataPassport:
        f = self.frame(symbol, timeframe)
        now = now_ns if now_ns is not None else self._clock()
        age = (now - int(f.index[-1].value)) / 1e9 if len(f) else float("inf")
        return DataPassport(
            symbol, timeframe, self._label, len(f),
            int(f.index[0].value) if len(f) else None,
            int(f.index[-1].value) if len(f) else None,
            age, 0, DataQuality.OK if age <= self._seconds * 3 else DataQuality.STALE,
            self._seconds)


class _NullHeartbeat:
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def update(self, **fields) -> None: ...
    def beat(self) -> None: ...


def _adverse_first_path(row: pd.Series, side: Optional[Side]) -> List[str]:
    first, second = ("high", "low") if side is Side.SELL else ("low", "high")
    return ["open", first, second, "close"]


def run_agent_replay(strategy: Strategy, data: Dict[str, pd.DataFrame],
                     instruments: Dict[str, Instrument], risk_config: RiskConfig,
                     cfg: ReplayConfig, *, sim_profile: Optional[SimProfile] = None,
                     conversions: Optional[Dict[str, Decimal]] = None):
    from .backtest import BacktestResult, _periods_per_year_from_index

    tf = strategy.meta.timeframe
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"unknown timeframe {tf!r}")
    tf_seconds = TIMEFRAME_SECONDS[tf]
    symbols = sorted(s for s in data if s in instruments)
    if not symbols:
        raise ValueError("no instrument in `data` has a contract specification")
    data = {s: data[s] for s in symbols}
    validate_universe(data, require_aligned=True)
    execution = cfg.execution_data or data
    execution = {s: execution[s] for s in symbols if s in execution}
    if set(execution) != set(data):
        raise ValueError("execution and decision universes must cover the same instruments")
    validate_universe(execution, require_aligned=True)
    exec_index = execution[symbols[0]].index
    exec_ns = exec_index.as_unit("ns").asi8
    if len(exec_ns) < 3:
        raise ValueError("execution data too short")
    cadence = int(np.median(np.diff(exec_ns)) // 10**9)
    if cadence <= 0 or cadence > tf_seconds:
        raise ValueError(f"execution cadence {cadence}s must be positive and <= {tf} ({tf_seconds}s)")
    bid_ask = all(has_bid_ask(execution[s]) for s in symbols)

    # --- the runtime the agent runs under ------------------------------------ #
    runtime = (cfg.runtime_config or SentinelConfig()).model_copy(deep=True)
    runtime.risk = risk_config.model_copy(deep=True)
    runtime.execution.venue_mode = ExecutionVenueMode.PAPER
    runtime.execution.broker = "paper"
    runtime.execution.account_currency = cfg.account_currency
    runtime.agent.mode = AgentMode.AUTONOMOUS
    # The decision interval is the execution cadence, clamped to the config's
    # range; the dead-man must clear it by the configured margin, and is
    # clamped to ITS range (a coarse replay on D1 bars runs the loop once a
    # day, which no live dead-man would tolerate, and the replay does not
    # run a watchdog anyway).
    runtime.agent.decision_interval_sec = max(5, min(3600, cadence))
    runtime.ops.deadman_timeout_sec = min(
        3600, max(runtime.ops.deadman_timeout_sec,
                  runtime.agent.decision_interval_sec + DEADMAN_MARGIN_SEC + 1))
    if not deadman_timeout_ok(runtime.ops.deadman_timeout_sec,
                              runtime.agent.decision_interval_sec):
        # Only reachable with an hourly-or-coarser cadence: shorten the
        # interval instead, the loop still runs once per execution bar.
        runtime.agent.decision_interval_sec = 3600 - DEADMAN_MARGIN_SEC - 1
    runtime.strategies = [StrategyAllocation(
        name=strategy.meta.name, enabled=True, instruments=symbols, timeframe=tf,
        params=dict(strategy.params), lifecycle="experimental")]
    news_replayed = cfg.news_source is not None
    if runtime.news.enabled and not news_replayed:
        runtime = runtime.model_copy(deep=True)
        runtime.news.enabled = False
        news_note = "news disabled for the replay: no historical calendar was supplied"
    else:
        news_note = ("historical calendar replayed at the simulated clock"
                     if news_replayed else "news disabled in the runtime configuration")

    profile = (sim_profile or SimProfile())
    if cfg.cost_multiplier != 1.0 or cfg.latency_multiplier != 1.0:
        profile = profile.stressed(cfg.cost_multiplier, cfg.latency_multiplier)
    # The runtime's declared commission is what the venue charges, so the
    # research cost and the live cost are one number.
    profile.commission_per_lot_round_turn = dec(runtime.execution.commission_per_lot_round_turn) \
        * dec(cfg.cost_multiplier)
    profile.slippage_pips_mean = dec(runtime.execution.expected_slippage_pips)

    start_ns = int(exec_ns[0])
    broker = PaperBroker(instruments={s: instruments[s] for s in symbols},
                         starting_balance=cfg.starting_equity,
                         account_currency=cfg.account_currency, profile=profile,
                         seed=cfg.seed, start_ns=start_ns - 1)
    for ccy, rate in (conversions or {}).items():
        broker.set_conversion(ccy, dec(rate))

    clock = {"now": start_ns}
    store = _ReplayStore(data, tf, lambda: clock["now"], cfg.data_label)

    equity: List[float] = []
    stamps: List[int] = []
    errors: List[str] = []
    vetoes: Counter = Counter()
    signals = submitted = rejected = 0
    signal_log: List[Dict[str, Any]] = []
    decisions_seen: set = set()
    closes = {s: data[s]["close"].to_numpy(dtype=float) for s in symbols}
    dec_ns = {s: data[s].index.as_unit("ns").asi8 for s in symbols}

    with tempfile.TemporaryDirectory(prefix="sentinel-replay-") as tmp:
        root = Path(tmp)
        runtime.ops.state_dir = tmp
        runtime.ops.audit_log = str(root / "audit.jsonl")
        runtime.ops.killswitch_file = str(root / "KILL")
        runtime.data.store_path = str(root / "market.db")
        audit = AuditLog(root / "audit.jsonl", fsync_every_record=False)
        memory = MemoryStore(root / "memory.db")
        proposals = ProposalQueue(str(root / "proposals.json"))
        feed = MarketFeed(broker, store, timeframe=tf, history=runtime.data.history_bars)

        news = None
        if news_replayed:
            from ..news.calendar import EconomicCalendar
            from ..news.policy import NewsPolicy
            calendar = EconomicCalendar(root / "calendar.db")
            calendar.ingest(cfg.news_source, start_ns - 30 * 86400 * 10**9,
                            int(exec_ns[-1]) + 30 * 86400 * 10**9)
            news = NewsPolicy(calendar, role=runtime.news.role,
                              before_min=runtime.risk.block_minutes_before_high_impact,
                              after_min=runtime.risk.block_minutes_after_high_impact)

        agent = Agent(runtime, broker, feed, audit, memory, proposals=proposals,
                      strategies={strategy.meta.name: strategy},
                      clock_fn=lambda: clock["now"], news=news, meta_gate=cfg.meta_gate)
        warmup = cfg.warmup_bars if cfg.warmup_bars is not None else strategy.warmup()
        # No heartbeat file in a replay: there is no watchdog to read it, and
        # an atomic JSON write per simulated minute is a tenth of the runtime.
        agent.heartbeat = _NullHeartbeat()
        agent.start()
        agent.equity_peak = agent.day_start_equity = cfg.starting_equity
        agent.week_start_equity = agent.month_start_equity = cfg.starting_equity

        def push(i: int, phase: int, ns: int) -> None:
            clock["now"] = ns
            broker.set_time(ns)
            for sym in symbols:
                row = execution[sym].iloc[i]
                pos = next((p for p in broker.positions() if p.instrument == sym), None)
                field_name = _adverse_first_path(row, pos.side if pos else None)[phase]
                inst = instruments[sym]
                if bid_ask:
                    bid, ask = dec(row[f"bid_{field_name}"]), dec(row[f"ask_{field_name}"])
                    if cfg.cost_multiplier != 1.0:
                        mid = (bid + ask) / 2
                        half = (ask - bid) / 2 * dec(cfg.cost_multiplier)
                        bid, ask = mid - half, mid + half
                    mid = (bid + ask) / 2
                else:
                    mid = dec(row[field_name])
                    half = broker._spread_price(inst, ns) / 2
                    bid, ask = mid - half, mid + half
                # Historical conversion rates follow the prices being replayed;
                # a fixed present-day rate would size 2019 trades at 2026 FX.
                if inst.base == cfg.account_currency and mid > 0:
                    broker.set_conversion(inst.quote, D("1") / mid)
                elif inst.quote == cfg.account_currency:
                    broker.set_conversion(inst.base, mid)
                broker.on_quote(Quote(sym, inst.round_price(bid), inst.round_price(ask),
                                      ts_ns=ns, received_ns=ns, source="replay"))

        try:
            for i in range(len(exec_ns)):
                ns = int(exec_ns[i])
                push(i, 0, ns)
                visible = store.frame(symbols[0], tf)
                if len(visible) >= warmup:
                    report = agent.cycle()
                    errors.extend(report.errors)
                    for d in report.decisions:
                        if d.side is not None and d.client_order_id and \
                                d.client_order_id not in decisions_seen:
                            decisions_seen.add(d.client_order_id)
                            signals += 1
                            # The forecast the strategy made, and what followed
                            # on the DECISION bars, for the directional gate.
                            j = int(np.searchsorted(dec_ns[d.instrument], d.ts_ns, side="right")) - 1
                            if 0 <= j < len(closes[d.instrument]):
                                h = max(1, min(int(cfg.forward_return_bars),
                                               int(strategy.meta.horizon_bars or 10**9)))
                                c0 = closes[d.instrument][j]
                                fh = closes[d.instrument][j + h] if j + h < len(closes[d.instrument]) else np.nan
                                f1 = closes[d.instrument][j + 1] if j + 1 < len(closes[d.instrument]) else np.nan
                                sign = 1 if d.side == "BUY" else -1
                                signal_log.append({
                                    "instrument": d.instrument, "index": j, "ts_ns": d.ts_ns,
                                    "side": d.side, "side_sign": sign,
                                    "strength": float(d.signal_strength),
                                    "fwd_ret_1": float(f1 / c0 - 1) if np.isfinite(f1) and c0 > 0 else None,
                                    "fwd_ret_h": float(fh / c0 - 1) if np.isfinite(fh) and c0 > 0 else None,
                                    "horizon_bars": h,
                                    "features": dict(d.diagnostics.get("meta_features") or {})})
                        submitted += d.action in ("executed", "queued")
                        rejected += any(v.get("rule") == "broker_reject" for v in d.vetoes)
                        vetoes.update(v["rule"] for v in d.vetoes)
                step = cadence * 10**9 // 3
                for phase in (1, 2, 3):
                    push(i, phase, ns + phase * step - (1 if phase == 3 else 0))
                equity.append(float(broker.account().equity))
                stamps.append(broker.now_ns)
            for pos in list(broker.positions()):
                broker.close_position(pos.instrument, reason="end_of_backtest")
            if equity:
                equity[-1] = float(broker.account().equity)
            for rec in audit.iter_records():
                p = rec.get("payload") or {}
                if rec.get("event") == "decision.signal" and (p.get("error") or p.get("prepare_failed")):
                    errors.append(str(p)[:200])
            halted, halt_reason = agent.halted, agent.halt_reason
        finally:
            agent.stop()
            audit.close()
            memory.close()

    curve = pd.Series(equity, index=pd.DatetimeIndex(pd.to_datetime(stamps, unit="ns", utc=True)),
                      name="equity")
    ppy = int(round(_periods_per_year_from_index(curve.index) or cfg.periods_per_year))
    perf = compute_performance(broker.closed_trades, curve, ppy, float(cfg.starting_equity))
    if cfg.record_trial:
        record_trial(strategy=strategy.meta.name,
                     family=getattr(strategy.meta, "family", "unclassified"),
                     params=dict(strategy.params), instruments=symbols, timeframe=tf,
                     data_window=window_key(data[symbols[0]].index[0], data[symbols[0]].index[-1],
                                            len(data[symbols[0]]), cfg.data_label),
                     data_label=cfg.data_label, kind=cfg.trial_kind, note=cfg.label)
    return BacktestResult(
        label=cfg.label, trades=broker.closed_trades, equity_curve=curve, performance=perf,
        vetoes=dict(vetoes), signals_generated=signals, orders_submitted=submitted,
        orders_rejected=rejected,
        config_snapshot={"strategy": strategy.describe(),
                         "cost_multiplier": cfg.cost_multiplier,
                         "latency_multiplier": cfg.latency_multiplier,
                         "starting_equity": str(cfg.starting_equity), "seed": cfg.seed,
                         "session_windows_utc": runtime.agent.session_windows_utc,
                         "trade_days": runtime.agent.trade_days,
                         "weekend_flat": runtime.risk.weekend_flat,
                         "risk": {"risk_per_trade_pct": str(runtime.risk.risk_per_trade_pct),
                                  "max_open_positions": runtime.risk.max_open_positions,
                                  "max_trades_per_day": runtime.risk.max_trades_per_day}},
        per_bar_returns=curve.pct_change().dropna(),
        diagnostics={
            "engine": ENGINE_NAME,
            "instruments": symbols,
            "first_bar": str(data[symbols[0]].index[0]), "last_bar": str(data[symbols[0]].index[-1]),
            "execution_cadence_sec": cadence,
            "decision_interval_sec": runtime.agent.decision_interval_sec,
            "decision_timeframe": tf,
            "bid_ask_quotes": bid_ask,
            "ohlc_path": "adverse-first approximation, not tick order",
            "news_replayed": news_replayed, "news_note": news_note,
            "meta_gate": bool(cfg.meta_gate is not None and getattr(cfg.meta_gate, "active", False)),
            "runtime_context_complete": news_replayed or not (cfg.runtime_config or SentinelConfig()).news.enabled,
            "halted": halted, "halt_reason": halt_reason,
            "generation_errors": errors[:30], "generation_error_count": len(errors),
            "periods_per_year": ppy,
        },
        signal_log=signal_log,
    )
