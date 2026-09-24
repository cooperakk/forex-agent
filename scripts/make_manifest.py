#!/usr/bin/env python3
"""Write the manifest that makes a bar directory verifiable by content.

    python scripts/make_manifest.py --bars data/amarkets/H4 --broker amarkets \\
        --costs costs.json --output data/amarkets/H4-manifest.json

``costs.json`` must hold the venue's ACTUAL schedule for the account and the
period -- commission per lot round turn in the account currency, slippage
mean and sigma in pips, and BOTH swap tables with an entry per instrument
(an explicit 0 where the venue charges none)::

    {"commission_per_lot_round_turn": 7.0,
     "slippage_pips_mean": 0.15, "slippage_pips_sigma": 0.10,
     "swap_long_pips_per_day":  {"EUR_USD": -0.72, "USD_JPY": 0.35},
     "swap_short_pips_per_day": {"EUR_USD":  0.21, "USD_JPY": -0.90}}

``scripts/export_history.py`` can read the swap tables straight from the
terminal into this file. The numbers above are placeholders, not a tariff.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.research.evidence import write_dataset_manifest  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bars", required=True, help="directory of per-instrument CSVs")
    ap.add_argument("--broker", required=True, help="broker profile (amarkets, alpari, ...)")
    ap.add_argument("--costs", required=True, help="cost schedule JSON")
    ap.add_argument("--output", required=True)
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    costs = json.loads(Path(args.costs).read_text(encoding="utf-8"))
    body = write_dataset_manifest(args.bars, broker=args.broker, cost_schedule=costs,
                                  output=args.output, note=args.note)
    print(f"manifest written: {args.output} ({len(body['files'])} files)")
    print("Keep the original broker export beside it. The hash proves the file the "
          "protocol reads is this one; it does not prove where it came from.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
