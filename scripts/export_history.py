#!/usr/bin/env python3
"""Export the venue's own bid/ask history from a MetaTrader terminal.

    python scripts/export_history.py --config var/config.json \\
        --symbols EUR_USD,GBP_USD,USD_JPY --days 400 \\
        --timeframes H4,M5 --out data/amarkets --costs-out data/amarkets/costs.json

Reads ticks through the configured adapter (local package or the bridge),
aggregates them into bid/ask/mid OHLC bars per timeframe, writes one CSV per
instrument under ``<out>/<timeframe>/``, and writes a cost-schedule skeleton
with the swap tables read from the terminal. Commission and slippage in that
file are the configuration's declared values -- verify them against the
broker's tariff before running the acceptance protocol.

This is the only supported route to a ``live-quality`` dataset from a
MetaTrader broker: a bid candle plus a stored spread is not an ask history.

The probe is read-only. Nothing here can send an order.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.core.config import SentinelConfig  # noqa: E402
from sentinel.data.ticks import export_history  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="var/config.json")
    ap.add_argument("--symbols", required=True, help="comma-separated canonical symbols")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--timeframes", default="H4,M5")
    ap.add_argument("--out", required=True)
    ap.add_argument("--costs-out", default=None)
    ap.add_argument("--chunk-days", type=int, default=3)
    args = ap.parse_args()

    cfg = SentinelConfig.load(args.config)
    if cfg.execution.broker == "paper":
        raise SystemExit("the paper venue has no tick history; configure a MetaTrader broker")
    from sentinel.bootstrap import _connection_kwargs
    from sentinel.brokers import build_broker
    from sentinel.brokers.connection import ReadOnlyBroker
    from sentinel.core.audit import NullAudit
    kwargs = _connection_kwargs(cfg, Path(cfg.ops.state_dir), NullAudit())
    # Read-only by construction: every write on this wrapper raises, and the
    # export only ever reads ticks and symbol swaps.
    inner = ReadOnlyBroker(build_broker(cfg.execution.broker, **kwargs))
    try:
        inner.fetch_ticks
    except Exception:  # noqa: BLE001
        raise SystemExit(f"{cfg.execution.broker} adapter has no tick history")

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    end_ns = time.time_ns()
    start_ns = end_ns - args.days * 86400 * 10**9
    written = export_history(inner, symbols, timeframes, start_ns=start_ns, end_ns=end_ns,
                             out_dir=args.out, chunk_days=args.chunk_days)
    for tf, per in written.items():
        for sym, n in per.items():
            print(f"[export] {tf} {sym}: {n} bars")

    if args.costs_out:
        swap_long, swap_short = {}, {}
        for sym in symbols:
            try:
                lo, sh = inner.swap_pips_per_day(sym)
            except Exception as exc:  # noqa: BLE001
                lo = sh = None
                print(f"[export] swap for {sym} unavailable: {exc}")
            swap_long[sym] = float(lo) if lo is not None else None
            swap_short[sym] = float(sh) if sh is not None else None
        costs = {
            "commission_per_lot_round_turn": float(cfg.execution.commission_per_lot_round_turn),
            "slippage_pips_mean": float(cfg.execution.expected_slippage_pips),
            "slippage_pips_sigma": 0.10,
            "swap_long_pips_per_day": swap_long,
            "swap_short_pips_per_day": swap_short,
            "_note": ("swap tables read from the terminal; commission and slippage are the "
                      "configuration's declared values -- verify against the broker's tariff. "
                      "A null swap means the terminal quotes it in a unit this tool does not "
                      "convert; fill it in by hand in pips per lot per day."),
        }
        Path(args.costs_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.costs_out).write_text(json.dumps(costs, indent=2), encoding="utf-8")
        print(f"[export] cost schedule skeleton: {args.costs_out}")
    try:
        inner.close()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
