#!/usr/bin/env python3
"""TradingView history for research, and a live self-test of the connection.

Download closed bars into the CSV layout scripts/run_acceptance.py reads
(one file per instrument, ``timestamp,open,high,low,close,volume``, UTC):

    python scripts/tv_history.py --instruments EUR_USD,GBP_USD,USD_JPY \\
        --timeframe H4 --count 5000 --out data/tradingview/H4

    python scripts/tv_history.py --instruments XAU_USD --map XAU_USD=OANDA:XAUUSD \\
        --timeframe D1 --count 3000 --out data/tradingview/D1

These are SINGLE-PRICE bars from TradingView's data provider, not the broker's
bid/ask. The acceptance protocol labels such a dataset ``third-party`` and it
can never promote a strategy to live money on its own: re-run on the broker's
own history (scripts/export_history.py) before trusting a result.

Check, from the server itself, that the reference price will work:

    python scripts/tv_history.py --selftest

It opens the websocket for a few seconds, reads live quotes, fetches a few
bars and the technical ratings, and says which part failed if one did. Exit
code 0 means everything the dashboard's reference page needs is reachable.

TradingView's interface used here is unofficial; see docs/TRADINGVIEW.md.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.data import tradingview as tv  # noqa: E402


def _mapping(instruments, exchange, pairs):
    explicit = {}
    for item in pairs or []:
        inst, _, sym = item.partition("=")
        explicit[inst.strip().upper()] = tv.validate_symbol(sym)
    out = {}
    for inst in instruments:
        sym = explicit.get(inst) or tv.default_symbol(inst, exchange)
        if not sym:
            raise SystemExit(f"{inst}: no TradingView symbol; pass --map {inst}=EXCHANGE:SYMBOL")
        out[inst] = sym
    return out


def download(args) -> int:
    instruments = [s.strip().upper() for s in args.instruments.split(",") if s.strip()]
    mapping = _mapping(instruments, args.exchange, args.map)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failures = 0
    for inst, sym in mapping.items():
        try:
            series = tv.fetch_bars(sym, args.timeframe, args.count, timeout=args.timeout)
            frame = series.to_frame()
        except (tv.TradingViewError, ValueError, OSError) as exc:
            print(f"[tv] {inst} ({sym}): FAILED -- {exc}")
            failures += 1
            continue
        path = out / f"{inst}.csv"
        frame.index.name = "timestamp"
        frame.to_csv(path, float_format="%.10g")
        print(f"[tv] {inst} ({sym}) {args.timeframe}: {len(frame)} bars "
              f"{frame.index[0]:%Y-%m-%d} .. {frame.index[-1]:%Y-%m-%d %H:%M} -> {path}"
              + (f" (dropped {series.dropped_incomplete} unfinished)"
                 if series.dropped_incomplete else ""))
        time.sleep(1.0)       # one request at a time, politely
    print("[tv] single-price third-party data: the acceptance protocol will label it so.")
    return 1 if failures else 0


def selftest(args) -> int:
    symbols = ["OANDA:EURUSD", "OANDA:USDJPY"]
    ok = True
    print(f"[selftest] websocket {tv.WS_URL}")
    stream = tv.TradingViewStream(backoff_min=2.0, backoff_max=4.0)
    stream.set_symbols(symbols)
    stream.start()
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        if all((q := stream.quote(s)) is not None and q.mid for s in symbols):
            break
        time.sleep(0.25)
    snap = stream.snapshot()
    stream.stop()
    status = snap["status"]
    for s in symbols:
        q = snap["quotes"].get(s)
        if q and q.get("mid"):
            print(f"[selftest]   {s}: mid {q['mid']:.6g}  mode={q.get('update_mode') or '?'}"
                  f"  session={q.get('session') or '?'}")
        else:
            ok = False
            print(f"[selftest]   {s}: NO QUOTE")
    if status.get("last_error"):
        print(f"[selftest]   last error: {status['last_error']}")
        if "403" in status["last_error"] or "proxy" in status["last_error"].lower():
            print("[selftest]   -> an outbound proxy or firewall refuses data.tradingview.com")
    try:
        series = tv.fetch_bars("OANDA:EURUSD", "H4", 10, timeout=20)
        print(f"[selftest] history: {len(series.bars)} closed H4 bars "
              f"({series.info.get('description', '')})")
        ok = ok and len(series.bars) > 0
    except Exception as exc:  # noqa: BLE001 - report, do not crash
        ok = False
        print(f"[selftest] history: FAILED -- {exc}")
    try:
        ratings = tv.fetch_ta(symbols)
        eu = ratings.get("OANDA:EURUSD", {}).get("240", {})
        print(f"[selftest] ratings: EURUSD 4h = {eu.get('label', 'none')} ({eu.get('all')})")
        ok = ok and bool(ratings)
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[selftest] ratings: FAILED -- {exc}")
    print("[selftest] OK" if ok else "[selftest] FAILED (trading is unaffected; the reference "
                                     "price simply stays unavailable)")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--selftest", action="store_true", help="test the live connection and exit")
    p.add_argument("--seconds", type=float, default=15.0, help="self-test quote wait")
    p.add_argument("--instruments", default="EUR_USD,GBP_USD,USD_JPY")
    p.add_argument("--timeframe", default="H4", choices=sorted(tv.TIMEFRAMES))
    p.add_argument("--count", type=int, default=5000)
    p.add_argument("--exchange", default="OANDA")
    p.add_argument("--map", action="append", metavar="INSTRUMENT=EXCHANGE:SYMBOL")
    p.add_argument("--out", default="data/tradingview")
    p.add_argument("--timeout", type=float, default=60.0)
    args = p.parse_args(argv)
    return selftest(args) if args.selftest else download(args)


if __name__ == "__main__":
    raise SystemExit(main())
