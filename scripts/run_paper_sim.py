#!/usr/bin/env python3
"""Accelerated paper-trading simulation.

Exercises the exact live code path -- agent loop, risk engine, OMS, reconciler,
kill switch, memory, proposals -- against the internal simulator with synthetic
bars that end at the current time. It is an integration test with a readable
transcript, not evidence about any market.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from sentinel.agent.memory import MemoryStore
from sentinel.agent.orchestrator import Agent
from sentinel.agent.proposals import ProposalQueue
from sentinel.brokers.paper import PaperBroker, SimProfile
from sentinel.core.audit import AuditLog
from sentinel.core.config import (
    AgentConfig, AgentMode, ExecutionConfig, OpsConfig, SentinelConfig,
    StrategyAllocation,
)
from sentinel.core.money import Instrument
from sentinel.core.types import Quote
from sentinel.data.feed import BarStore, MarketFeed, bars_from_frame
from sentinel.data.synthetic import DEFAULT_UNIVERSE, generate_universe

INSTRUMENTS = {
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
    "AUD_USD": Instrument("AUD_USD", "AUD", "USD"),
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001")),
    "USD_CHF": Instrument("USD_CHF", "USD", "CHF"),
}
SYMBOLS = list(INSTRUMENTS)


def build(tmp: Path, mode: AgentMode, bars: int, live_bars: int, seed: int):
    n = bars + live_bars
    bars_per_day = 6
    # Anchor the series so the last bar lands on "now": otherwise every quote is
    # stale by years and the risk engine -- correctly -- refuses to trade.
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=(24 // bars_per_day) * (n - 1))
    universe = generate_universe(DEFAULT_UNIVERSE, n_bars=n, bars_per_day=bars_per_day,
                                 seed=seed, dollar_factor_strength=0.6)
    for sym, df in universe.items():
        df.index = pd.date_range(start, periods=n, freq="4h", tz="UTC")

    store = BarStore(tmp / "market.db")
    for sym, df in universe.items():
        store.upsert(bars_from_frame(df.iloc[:bars], sym, "H4", source="synthetic"))

    broker = PaperBroker(instruments=INSTRUMENTS, starting_balance=D("10000"),
                         profile=SimProfile(), seed=seed,
                         start_ns=int(df.index[0].value))
    broker.set_conversion("JPY", D("1") / D("150"))
    broker.set_conversion("CHF", D("1") / D("0.88"))

    config = SentinelConfig(
        agent=AgentConfig(mode=mode, decision_interval_sec=60,
                          session_windows_utc=[[0, 24]], trade_days=[0, 1, 2, 3, 4, 5, 6],
                          proposal_min_sample=25,
                          semi_auto_envelope={"instruments": SYMBOLS, "max_lots": 0.5,
                                              "max_risk_pct": 1.0}),
        execution=ExecutionConfig(broker="paper"),
        ops=OpsConfig(state_dir=str(tmp), killswitch_file=str(tmp / "KILL"),
                      audit_log=str(tmp / "audit.jsonl")),
        strategies=[
            StrategyAllocation(name="donchian_trend", enabled=True, instruments=SYMBOLS,
                               timeframe="H4", lifecycle="experimental"),
            StrategyAllocation(name="vol_reversion", enabled=True,
                               instruments=["EUR_USD", "GBP_USD"], timeframe="H4",
                               lifecycle="experimental"),
        ],
    )
    audit = AuditLog(tmp / "audit.jsonl", fsync_every_record=False)
    memory = MemoryStore(tmp / "memory.db")
    feed = MarketFeed(broker, store, timeframe="H4", history=1200)
    # The agent reads time through this closure, so the simulated clock drives
    # staleness, skew and session logic exactly as the wall clock would live.
    agent = Agent(config, broker, feed, audit, memory,
                  proposals=ProposalQueue(str(tmp / "proposals.json")),
                  clock_fn=lambda: broker.now_ns)
    return agent, broker, store, universe, audit, memory, bars


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="autonomous",
                    choices=[m.value for m in AgentMode])
    ap.add_argument("--warmup-bars", type=int, default=600)
    ap.add_argument("--live-bars", type=int, default=400)
    ap.add_argument("--seed", type=int, default=20260914)
    ap.add_argument("--dir", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    tmp = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="sentinel-sim-"))
    tmp.mkdir(parents=True, exist_ok=True)
    agent, broker, store, universe, audit, memory, warm = build(
        tmp, AgentMode(args.mode), args.warmup_bars, args.live_bars, args.seed)

    print(f"workspace: {tmp}")
    agent.start()
    executed = vetoed = queued = 0
    veto_reasons: dict[str, int] = {}

    for step in range(args.live_bars):
        i = warm + step
        ts = universe[SYMBOLS[0]].index[i]
        ns = int(ts.value)
        broker.set_time(ns)
        for sym, df in universe.items():
            row = df.iloc[i]
            inst = INSTRUMENTS[sym]
            broker.on_bar_prices(sym, D(repr(float(row["open"]))), D(repr(float(row["high"]))),
                                 D(repr(float(row["low"]))), D(repr(float(row["close"]))),
                                 ns, ns + 14_400_000_000_000)
            store.upsert(bars_from_frame(df.iloc[i:i + 1], sym, "H4", source="synthetic"))
        report = agent.cycle()
        for d in report.decisions:
            if d.action == "executed":
                executed += 1
                if not args.quiet:
                    print(f"  [{ts:%Y-%m-%d %H:%M}] EXEC {d.strategy:16s} {d.instrument} "
                          f"{d.side} {d.lots} lots  risk {float(d.risk_pct or 0):.2f}%")
            elif d.action == "vetoed":
                vetoed += 1
                for v in d.vetoes:
                    veto_reasons[v["rule"]] = veto_reasons.get(v["rule"], 0) + 1
            elif d.action == "queued":
                queued += 1
        if report.errors and not args.quiet:
            print("  errors:", report.errors[:2])

    agent.stop()
    acct = broker.account()
    print("\n--- simulation summary ---")
    print(f"cycles              {agent.cycles}")
    print(f"executed / vetoed / queued   {executed} / {vetoed} / {queued}")
    print(f"closed trades       {len(broker.closed_trades)}")
    print(f"final equity        {acct.equity:.2f} (start 10000)")
    print(f"open positions      {len(broker.positions())}")
    print(f"advisory queue      {len(agent.pending_advice())}")
    print(f"autopsies recorded  {memory.autopsy_count()}")
    print(f"lessons             {len(memory.all_lessons())}")
    print(f"proposals pending   {len(agent.proposals.pending())}")
    print(f"execution quality   {agent.oms.execution_report()}")
    print(f"top vetoes          {dict(sorted(veto_reasons.items(), key=lambda kv: -kv[1])[:8])}")
    ok, bad, msg = audit.verify()
    print(f"audit chain         {ok} ({msg})")
    print(f"halted              {agent.halted} {agent.halt_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
