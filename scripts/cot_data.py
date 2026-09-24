#!/usr/bin/env python3
"""CFTC Commitments of Traders: test the connection, import files, show positioning.

    python scripts/cot_data.py --selftest
        can this server reach the CFTC API? (prints the last two EUR reports)

    python scripts/cot_data.py --import deacot2024.zip deacot2025.zip --state var
        import the CFTC's yearly "Futures Only" legacy files, for a server that
        cannot reach the API. Download them in a browser from
        https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalCompressed/index.htm
        ("Futures Only Reports", one zip per year).

    python scripts/cot_data.py --show --state var
        the positioning index per currency, as the agent sees it now.

Read-only against the CFTC; the only thing written is var/macro.db.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.core.clock import wall_ns  # noqa: E402
from sentinel.data.cot import (  # noqa: E402
    COT_CODES, CotClient, CotError, CotStore, parse_file, positioning,
)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--import", dest="files", nargs="*", default=[])
    p.add_argument("--show", action="store_true")
    p.add_argument("--state", default="var", help="state directory holding macro.db")
    args = p.parse_args(argv)

    if args.selftest:
        since = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        try:
            reports = CotClient().fetch([COT_CODES["EUR"]], since)
        except CotError as exc:
            print(f"FAIL  the CFTC API is not reachable from here: {exc}")
            print("      Import the yearly files instead (--import), or allow "
                  "publicreporting.cftc.gov in the firewall.")
            return 1
        if not reports:
            print("FAIL  the API answered but returned no rows for Euro FX")
            return 1
        for r in reports[-2:]:
            print(f"OK    {r.currency} {r.report_date}: speculators long {r.spec_long:,.0f} "
                  f"short {r.spec_short:,.0f} (open interest {r.open_interest:,.0f})")
        return 0

    store = CotStore(Path(args.state) / "macro.db")
    if args.files:
        total = 0
        for f in args.files:
            try:
                reports = parse_file(f)
            except (OSError, CotError) as exc:
                print(f"FAIL  {f}: {exc}")
                return 1
            store.upsert(reports)
            total += len(reports)
            print(f"OK    {f}: {len(reports)} currency reports")
        print(f"stored; newest report {store.latest_date()}, {store.count()} rows in total")

    if args.show or not (args.files or args.selftest):
        now = wall_ns()
        print(f"{'ccy':5}{'report':12}{'index':>7}{'change':>8}{'net % OI':>10}  weeks")
        for ccy in COT_CODES:
            pos = positioning(store.history(ccy), now)
            if pos is None:
                print(f"{ccy:5}{'-':12}{'-':>7}{'-':>8}{'-':>10}  {len(store.history(ccy))}")
                continue
            print(f"{ccy:5}{pos['report_date']:12}{pos['index']:>7.1f}{pos['change']:>+8.1f}"
                  f"{pos['net_pct_oi']:>10.1f}  {pos['weeks']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
