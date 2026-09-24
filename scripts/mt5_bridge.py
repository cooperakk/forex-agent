#!/usr/bin/env python3
"""Serve a MetaTrader 5 terminal to an engine running elsewhere.

Run this on the machine where the terminal is signed in (Windows, or a Wine
prefix). The engine -- typically an Ubuntu server -- reaches it through an
SSH tunnel and sets:

    SENTINEL_MT5_BRIDGE=127.0.0.1:5555
    SENTINEL_MT5_BRIDGE_TOKEN=<the same token>

    python scripts\\mt5_bridge.py --token-file bridge.token

The token file is created with a random 48-character token on first run and
printed once. Copy it to the engine's environment file. The server binds
loopback; nothing on the network can reach it without the tunnel, and the
tunnel is what encrypts the account password on its way through.

    --allow-remote --bind 0.0.0.0    only if a VPN provides the isolation

Needs the real package:  pip install MetaTrader5   (Windows only).
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.brokers.mt5_bridge import DEFAULT_PORT, BridgeServer  # noqa: E402


def _token(path: Path) -> str:
    if path.exists():
        tok = path.read_text(encoding="utf-8").strip()
        if len(tok) >= 16:
            return tok
    tok = secrets.token_urlsafe(36)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (tok + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    print(f"[bridge] new token written to {path}. Put it in the engine's environment as")
    print(f"[bridge]     SENTINEL_MT5_BRIDGE_TOKEN={tok}")
    print("[bridge] It is printed this once. Keep the file private.")
    return tok


def main() -> int:
    ap = argparse.ArgumentParser(description="Sentinel-FX MetaTrader 5 bridge")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token-file", default="bridge.token")
    ap.add_argument("--allow-remote", action="store_true",
                    help="permit a non-loopback bind (only behind a VPN)")
    ap.add_argument("--terminal", default=None,
                    help="path to terminal64.exe; omit to attach to the running terminal")
    ap.add_argument("--max-lots", type=float, default=0.5,
                    help="largest new position the bridge will pass to the terminal")
    ap.add_argument("--allow-live", action="store_true",
                    help="permit new positions on a LIVE account (default: demo only)")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated VENUE symbols the bridge may open; omit for any")
    ap.add_argument("--journal", default="bridge-writes.json",
                    help="durable write journal beside the token file")
    args = ap.parse_args()

    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError:
        print("[bridge] the MetaTrader5 package is not installed. On the Windows machine "
              "run:  pip install MetaTrader5", file=sys.stderr)
        return 2

    kwargs = {"path": args.terminal} if args.terminal else {}
    if not mt5.initialize(**kwargs):
        print(f"[bridge] MetaTrader initialize failed: {mt5.last_error()}. Is the terminal "
              "installed, running and signed in?", file=sys.stderr)
        return 2
    info = mt5.account_info()
    if info is None:
        print("[bridge] the terminal is running but not signed in to any account.",
              file=sys.stderr)
        return 2
    kind = {0: "DEMO", 1: "CONTEST (demo)", 2: "LIVE"}.get(int(getattr(info, "trade_mode", 0)), "?")
    print(f"[bridge] attached to {getattr(info, 'company', '')} account {info.login} "
          f"({kind}) on {getattr(info, 'server', '')}")
    # Release the session; the engine's own initialize() re-attaches through
    # the bridge with whatever credentials it holds.
    mt5.shutdown()

    token = _token(Path(args.token_file))
    from sentinel.brokers.mt5_bridge import BridgeEnvelope
    envelope = BridgeEnvelope(
        account_id=str(info.login), server=str(getattr(info, "server", "") or ""),
        max_lots=args.max_lots, allow_live=args.allow_live,
        symbols={s.strip() for s in args.symbols.split(",")} if args.symbols else None,
        journal_path=str(Path(args.token_file).with_name(args.journal)))
    print(f"[bridge] bound to account {info.login}; new positions capped at "
          f"{args.max_lots} lots; live {'ALLOWED' if args.allow_live else 'refused'}")
    server = BridgeServer(mt5, token=token, host=args.bind, port=args.port,
                          allow_remote=args.allow_remote, log=lambda s: print(f"[bridge] {s}"),
                          envelope=envelope)
    server.start()
    print("[bridge] serving. Leave this window open; Ctrl+C stops it.")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
