#!/usr/bin/env python3
"""Manage which strategies the served agent evaluates.

The dashboard shows the allocations but its switches are deliberately inert: a
strategy's lifecycle is earned through the acceptance protocol, not clicked,
and which instruments a hypothesis is pointed at is a research decision that
belongs beside the config file, not behind a browser session.

    python scripts/manage_strategies.py list
    python scripts/manage_strategies.py available
    python scripts/manage_strategies.py add     --name ma_cross_atr --instruments EUR_USD,GBP_USD
    python scripts/manage_strategies.py enable  --name donchian_trend
    python scripts/manage_strategies.py disable --name donchian_trend
    python scripts/manage_strategies.py remove  --name donchian_trend

Edits the configuration ON DISK, through the same validated model the engine
loads, so an incoherent result is refused here rather than at 03:00. Restart
the service afterwards: the running process reads its configuration at start
and through the audited API, not by watching the file.

What this cannot do, on purpose: mark anything ``accepted``. That lifecycle is
written only by the verdict registry after a passed run, and any value set
here is demoted at the next start (``research.verdicts.enforce_config_authority``).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.core.config import SentinelConfig, StrategyAllocation  # noqa: E402
from sentinel.strategy.registry import available, describe_all, get  # noqa: E402

#: The served feed is H4 (bootstrap.build_runtime). A strategy declared on
#: another timeframe still receives H4 bars, so say so loudly.
SERVED_TIMEFRAME = "H4"


def _load(path: Path) -> SentinelConfig:
    if not path.exists():
        print(f"error: no configuration at {path}; start the server once to create it",
              file=sys.stderr)
        raise SystemExit(2)
    return SentinelConfig.load(path)


def _save(cfg: SentinelConfig, path: Path, by: str) -> None:
    cfg.bump(by).save(path)
    print(f"saved {path} (version {cfg.version + 1}). Restart the service to apply.")


def cmd_list(cfg: SentinelConfig) -> int:
    if not cfg.strategies:
        print("no strategies configured. Try: manage_strategies.py add --name donchian_trend")
        return 0
    for a in cfg.strategies:
        flag = "ON " if a.enabled else "off"
        note = ""
        if a.timeframe != SERVED_TIMEFRAME:
            note = f"  (declared {a.timeframe}; the served feed is {SERVED_TIMEFRAME})"
        print(f"  [{flag}] {a.name:<28} {a.lifecycle:<12} {a.timeframe:<3} "
              f"{','.join(a.instruments)}{note}")
    return 0


def cmd_available() -> int:
    for d in describe_all():
        mark = "" if d["timeframe"] == SERVED_TIMEFRAME else f"  (native {d['timeframe']})"
        print(f"  {d['name']:<28} {d['family']:<15} {d['description'][:60]}{mark}")
    return 0


def cmd_add(cfg: SentinelConfig, name: str, instruments: list[str],
            timeframe: str, params: dict) -> int:
    if name not in available():
        print(f"error: unknown strategy {name!r}. See: manage_strategies.py available",
              file=sys.stderr)
        return 2
    if any(a.name == name for a in cfg.strategies):
        print(f"error: {name!r} is already configured; use enable/disable/remove",
              file=sys.stderr)
        return 2
    # Validate the parameters by building the strategy once.
    try:
        get(name)(**params)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {name} rejects these parameters: {exc}", file=sys.stderr)
        return 2
    if timeframe != SERVED_TIMEFRAME:
        print(f"warning: the served feed is {SERVED_TIMEFRAME}; this allocation will be "
              f"handed {SERVED_TIMEFRAME} bars whatever it declares.")
    cfg.strategies.append(StrategyAllocation(
        name=name, enabled=True, instruments=instruments, timeframe=timeframe,
        params=params, lifecycle="hypothesis"))
    return 0


def cmd_set_enabled(cfg: SentinelConfig, name: str, enabled: bool) -> int:
    for a in cfg.strategies:
        if a.name == name:
            if a.lifecycle == "suspended" and enabled:
                print(f"error: {name!r} is suspended; a suspended strategy is re-enabled "
                      "only by a new acceptance run", file=sys.stderr)
                return 2
            a.enabled = enabled
            return 0
    print(f"error: {name!r} is not configured", file=sys.stderr)
    return 2


def cmd_remove(cfg: SentinelConfig, name: str) -> int:
    before = len(cfg.strategies)
    cfg.strategies = [a for a in cfg.strategies if a.name != name]
    if len(cfg.strategies) == before:
        print(f"error: {name!r} is not configured", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Sentinel-FX strategy allocations")
    ap.add_argument("--config", default=os.environ.get("SENTINEL_CONFIG", "var/config.json"))
    ap.add_argument("--by", default="manage_strategies", help="who to record as the editor")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show configured allocations")
    sub.add_parser("available", help="show every strategy in the library")

    a = sub.add_parser("add", help="configure a strategy (enabled, hypothesis lifecycle)")
    a.add_argument("--name", required=True)
    a.add_argument("--instruments", default="EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CHF",
                   help="comma-separated canonical symbols")
    a.add_argument("--timeframe", default=SERVED_TIMEFRAME)
    a.add_argument("--param", action="append", default=[],
                   help="key=value, repeatable; values are parsed as JSON when they can be")

    for name, help_text in (("enable", "switch an allocation on"),
                            ("disable", "switch an allocation off"),
                            ("remove", "drop an allocation from the configuration")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--name", required=True)

    args = ap.parse_args()
    path = Path(args.config)

    if args.cmd == "available":
        return cmd_available()
    cfg = _load(path)
    if args.cmd == "list":
        return cmd_list(cfg)

    if args.cmd == "add":
        import json
        params: dict = {}
        for item in args.param:
            key, _, raw = item.partition("=")
            try:
                params[key] = json.loads(raw)
            except ValueError:
                params[key] = raw
        rc = cmd_add(cfg, args.name, [s.strip() for s in args.instruments.split(",") if s.strip()],
                     args.timeframe, params)
    elif args.cmd == "enable":
        rc = cmd_set_enabled(cfg, args.name, True)
    elif args.cmd == "disable":
        rc = cmd_set_enabled(cfg, args.name, False)
    else:
        rc = cmd_remove(cfg, args.name)
    if rc:
        return rc
    _save(cfg, path, args.by)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
