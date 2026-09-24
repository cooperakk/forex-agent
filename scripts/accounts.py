#!/usr/bin/env python3
"""Isolated account workspaces: one engine, one venue, one state directory each.

    python scripts/accounts.py add alpari-demo --broker alpari --account 12345678 \\
        --server "Alpari-MT5-Demo" --port 8091 --bridge 127.0.0.1:5555 --kind demo
    python scripts/accounts.py list
    python scripts/accounts.py show alpari-demo
    python scripts/accounts.py freeze alpari-demo
    python scripts/accounts.py fingerprint alpari-demo

Why a directory per account and not a switch in one process: an engine's
memory is its account. The equity peak, the drawdown rung, the period
baselines, the position metadata, the order journal, the audit chain, the
lessons -- every one of them belongs to one venue account, and "switching"
an engine between two accounts would carry one account's drawdown budget
into the other's book. So a second account is a second engine: its own
config.json, its own var/, its own dashboard port, its own JWT secret, its
own bridge (a second Windows terminal, a second ``mt5_bridge.py`` on another
port, a second tunnel), and its own systemd unit.

"Switching" in the dashboard sense is opening the other account's dashboard.
Both engines keep running. Nothing is transferred.

What this does NOT do: pool risk across accounts. Two engines each sized to
2% total open risk are 4% of the combined capital. ``risk/portfolio.py`` and
``ops.group_ledger_dir`` are the shared ledger that bounds the group; every
account in a group should point at the same directory.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.core.config import SentinelConfig, StrategyAllocation  # noqa: E402

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
ENV_ALLOWED = {"SENTINEL_JWT_SECRET", "SENTINEL_ADMIN_USER", "SENTINEL_ADMIN_PASSWORD",
               "SENTINEL_MT5_BRIDGE", "SENTINEL_MT5_BRIDGE_TOKEN", "SENTINEL_MT5_TERMINAL",
               "SENTINEL_CONFIG"}


def account_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SentinelFX" / "accounts"
    return Path(os.environ.get("SENTINEL_ACCOUNTS_ROOT", "/var/lib/sentinel/accounts"))


def safe_name(name: str) -> str:
    if not NAME_RE.fullmatch(name or ""):
        raise SystemExit("account name: lowercase letters, digits and hyphens, starting with "
                         "a letter, at most 32 characters")
    return name


def private_json(path: Path, body: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)


def load_account_env(folder: Path) -> dict:
    env = json.loads((folder / "environment.json").read_text(encoding="utf-8"))
    if set(env) - ENV_ALLOWED or any(not isinstance(v, str) for v in env.values()):
        raise SystemExit(f"{folder / 'environment.json'}: unexpected keys or non-string values")
    return env


def portal(root: Path) -> Path:
    cards = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        file = directory / "config.json"
        if not file.is_file():
            continue
        c = SentinelConfig.load(file)
        cards.append(
            f'<article><b>{html.escape(directory.name)}</b>'
            f'<p>{html.escape(c.execution.broker)} · حساب {html.escape(c.execution.expected_account_id)}'
            f' · {html.escape(c.execution.venue_mode.value)} · حالت {html.escape(c.agent.mode.value)}</p>'
            f'<a href="http://127.0.0.1:{c.security.bind_port}" target="_blank" rel="noopener">'
            f'داشبورد این حساب</a></article>')
    out = root / "accounts.html"
    out.write_text(
        '<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">'
        '<title>حساب‌های Sentinel-FX</title><style>body{font:18px Tahoma,sans-serif;'
        'background:#10202d;color:#eee;padding:40px;max-width:800px;margin:auto}'
        'article{background:#203646;padding:20px 24px;margin:14px 0;border-radius:12px}'
        'a{color:#6ee7c0}p{line-height:1.8;margin:6px 0}</style>'
        '<h1>حساب‌های معاملاتی</h1>'
        '<p>هر حساب یک موتور جداگانه با حافظه، ریسک و دفتر رویداد خودش است. انتخاب یک حساب '
        'فقط داشبورد آن را باز می‌کند؛ موتور بقیه ادامه می‌دهد. سقف ریسک مشترک بین حساب‌ها '
        'را در <code>ops.group_ledger_dir</code> تنظیم کنید.</p>'
        + "".join(cards) + "</html>", encoding="utf-8")
    return out


def cmd_add(args, root: Path) -> None:
    name = safe_name(args.name)
    if not (1024 <= args.port <= 65535):
        raise SystemExit("--port must be an unprivileged port (1024-65535)")
    if not args.account.strip():
        raise SystemExit("--account is required: the venue's account number")
    if bool(args.bridge) == bool(args.terminal):
        raise SystemExit("choose exactly one of --bridge (engine on Linux/another host) or "
                         "--terminal (MetaTrader package on this Windows machine)")
    for existing in root.glob("*/config.json"):
        old = SentinelConfig.load(existing)
        if old.security.bind_port == args.port:
            raise SystemExit(f"port {args.port} is already used by {existing.parent.name}")
        if (old.execution.broker == args.broker
                and old.execution.expected_account_id == args.account):
            raise SystemExit(f"{args.broker} account {args.account} already belongs to "
                             f"{existing.parent.name}: one engine per account")
        env = load_account_env(existing.parent)
        if args.bridge and env.get("SENTINEL_MT5_BRIDGE") == args.bridge:
            raise SystemExit(f"bridge {args.bridge} is already used by {existing.parent.name}; "
                             "each account needs its own terminal and bridge port")
        if args.terminal and env.get("SENTINEL_MT5_TERMINAL", "").lower() == \
                str(Path(args.terminal).resolve()).lower():
            raise SystemExit("each account needs a separate MetaTrader terminal installation")
    directory = root / name
    if directory.exists():
        raise SystemExit(f"{directory} already exists")

    cfg = SentinelConfig()
    cfg.execution.broker = args.broker
    cfg.execution.account_currency = args.currency.upper()
    cfg.execution.expected_account_id = args.account.strip()
    cfg.execution.expected_account_server = args.server.strip()
    cfg.execution.venue_mode = args.kind
    cfg.security.bind_port = args.port
    cfg.ops.state_dir = str(directory / "var")
    cfg.ops.audit_log = str(directory / "var" / "audit.jsonl")
    cfg.ops.killswitch_file = str(directory / "var" / "KILL")
    cfg.ops.backup_dir = str(directory / "backups")
    cfg.data.store_path = str(directory / "var" / "market.db")
    if args.group_ledger:
        cfg.ops.group_ledger_dir = str(Path(args.group_ledger).resolve())
    # A demo starts advisory (it may execute what a human accepts); a live
    # account starts OBSERVE and opens nothing until a human changes the mode
    # through the dashboard with a second factor.
    cfg.agent.mode = "advisory" if args.kind == "demo" else "observe"
    instruments = [s.strip() for s in args.instruments.split(",") if s.strip()]
    cfg.strategies = [StrategyAllocation(
        name=s.strip(), enabled=(args.kind == "demo"), instruments=instruments,
        timeframe="H4", lifecycle="hypothesis") for s in args.strategies.split(",") if s.strip()]
    directory.mkdir(mode=0o700, exist_ok=False)
    (directory / "var").mkdir(mode=0o700, exist_ok=True)
    cfg.save(directory / "config.json")

    env = {"SENTINEL_JWT_SECRET": secrets.token_urlsafe(48),
           "SENTINEL_ADMIN_USER": "owner",
           "SENTINEL_ADMIN_PASSWORD": secrets.token_urlsafe(20)}
    if args.bridge:
        from getpass import getpass
        token = getpass("bridge token (as printed by mt5_bridge.py on the terminal machine): ")
        if len(token.strip()) < 16:
            raise SystemExit("that does not look like a bridge token")
        env["SENTINEL_MT5_BRIDGE"] = args.bridge
        env["SENTINEL_MT5_BRIDGE_TOKEN"] = token.strip()
    else:
        env["SENTINEL_MT5_TERMINAL"] = str(Path(args.terminal).resolve())
    private_json(directory / "environment.json", env)
    print(f"created {name}")
    print(f"  config     {directory / 'config.json'}")
    print(f"  secrets    {directory / 'environment.json'}  (0600; contains the first owner password)")
    print(f"  dashboard  http://127.0.0.1:{args.port}")
    print(f"  mode       {cfg.agent.mode.value} ({args.kind})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=account_root())
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="create an isolated account workspace")
    add.add_argument("name")
    add.add_argument("--broker", required=True, help="profile: alpari, amarkets, generic_mt5, oanda")
    add.add_argument("--account", required=True, help="the venue's account number")
    add.add_argument("--server", default="", help="exact MT5 server name from the terminal")
    add.add_argument("--currency", default="USD")
    add.add_argument("--kind", choices=["demo", "live"], default="demo")
    add.add_argument("--port", type=int, required=True, help="dashboard port for this account")
    add.add_argument("--bridge", default=None, help="host:port of this account's mt5 bridge")
    add.add_argument("--terminal", default=None, help="terminal64.exe path (Windows, local package)")
    add.add_argument("--strategies", default="donchian_trend,ma_cross_atr")
    add.add_argument("--instruments", default="EUR_USD,GBP_USD,USD_JPY")
    add.add_argument("--group-ledger", default=None,
                     help="shared directory for the cross-account risk ledger")
    for c in ("show", "freeze", "unfreeze", "fingerprint"):
        p = sub.add_parser(c)
        p.add_argument("name")
    sub.add_parser("list")
    args = ap.parse_args()

    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.cmd == "add":
        cmd_add(args, root)
    elif args.cmd == "list":
        for file in sorted(root.glob("*/config.json")):
            cfg = SentinelConfig.load(file)
            print(f"{file.parent.name:<20} {cfg.execution.broker:<12} "
                  f"{cfg.execution.expected_account_id:<12} {cfg.execution.venue_mode.value:<5} "
                  f"{cfg.agent.mode.value:<11} http://127.0.0.1:{cfg.security.bind_port}")
    else:
        directory = root / safe_name(args.name)
        if not (directory / "config.json").is_file():
            raise SystemExit(f"no account named {args.name!r} under {root}")
        cfg = SentinelConfig.load(directory / "config.json")
        if args.cmd == "freeze":
            Path(cfg.ops.killswitch_file).parent.mkdir(parents=True, exist_ok=True)
            Path(cfg.ops.killswitch_file).write_text("operator freeze via accounts.py\n")
            print("frozen: no new entries. Open positions stay managed while the engine runs.")
        elif args.cmd == "unfreeze":
            try:
                Path(cfg.ops.killswitch_file).unlink()
                print("kill file removed. The engine resumes entries on its next cycle.")
            except FileNotFoundError:
                print("not frozen")
        elif args.cmd == "fingerprint":
            from sentinel.research.verdicts import config_fingerprint
            from sentinel.strategy.registry import build
            for a in cfg.strategies:
                params = build(a.name, **a.params).params
                print(a.name, config_fingerprint(a.instruments, params, a.timeframe,
                                                 runtime_config=cfg))
        else:
            print(json.dumps({
                "name": args.name, "broker": cfg.execution.broker,
                "account": cfg.execution.expected_account_id,
                "server": cfg.execution.expected_account_server,
                "kind": cfg.execution.venue_mode.value, "mode": cfg.agent.mode.value,
                "dashboard": f"http://127.0.0.1:{cfg.security.bind_port}",
                "state_dir": cfg.ops.state_dir,
                "strategies": [a.name for a in cfg.strategies if a.enabled],
            }, indent=2, ensure_ascii=False))
    print(f"account selector: {portal(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
