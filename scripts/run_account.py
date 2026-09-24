#!/usr/bin/env python3
"""Run one account's engine (or its watchdog) with its own environment and a lock.

    python scripts/run_account.py alpari-demo              # engine + dashboard
    python scripts/run_account.py alpari-demo --watchdog   # independent dead-man
    python scripts/run_account.py alpari-demo --console    # engine, logs to the terminal

The account's ``environment.json`` is loaded into the process environment --
and every connection variable another account could have left behind is
removed first, so an engine can never inherit a sibling's bridge or secret.
An OS-level lock on ``<account>/engine.lock`` guarantees one engine per
account: a second copy exits immediately with a message, rather than two
processes trading one book.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.accounts import ENV_ALLOWED, account_root, load_account_env, safe_name  # noqa: E402
from sentinel.core.config import SentinelConfig, deadman_timeout_ok  # noqa: E402


def lock_account(folder: Path, component: str):
    """Hold an exclusive OS lock for the life of the process."""
    handle = open(folder / f"{component}.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit(f"another {component} already owns {folder.name}; refusing to start "
                         "a second one on the same account")
    return handle


def load_account(folder: Path) -> SentinelConfig:
    if not (folder / "config.json").is_file():
        raise SystemExit(f"{folder} has no config.json; create the account with accounts.py")
    env = load_account_env(folder)
    for key in ENV_ALLOWED | {"OANDA_ACCOUNT_ID", "OANDA_API_TOKEN"}:
        os.environ.pop(key, None)
    os.environ.update(env)
    os.environ["SENTINEL_CONFIG"] = str(folder / "config.json")
    cfg = SentinelConfig.load(folder / "config.json")
    if not deadman_timeout_ok(cfg.ops.deadman_timeout_sec, cfg.agent.decision_interval_sec):
        raise SystemExit("ops.deadman_timeout_sec must exceed agent.decision_interval_sec by "
                         "the configured margin (see core.config.DEADMAN_MARGIN_SEC)")
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name")
    ap.add_argument("--root", type=Path, default=account_root())
    ap.add_argument("--watchdog", action="store_true")
    ap.add_argument("--console", action="store_true", help="log to the terminal, not a file")
    args = ap.parse_args()

    folder = args.root.resolve() / safe_name(args.name)
    cfg = load_account(folder)
    lock = lock_account(folder, "watchdog" if args.watchdog else "engine")
    handler = None
    try:
        if not args.console:
            (folder / "logs").mkdir(exist_ok=True)
            logger = logging.getLogger("sentinel.account")
            logger.setLevel(logging.INFO)
            handler = RotatingFileHandler(
                folder / "logs" / ("watchdog.log" if args.watchdog else "engine.log"),
                maxBytes=5_000_000, backupCount=5, encoding="utf-8")
            logger.addHandler(handler)

            class _Stream:
                def write(self, text: str) -> None:
                    if text.strip():
                        logger.info(text.rstrip())

                def flush(self) -> None:
                    handler.flush()

                def isatty(self) -> bool:
                    return False
            sys.stdout = sys.stderr = _Stream()
        project = Path(__file__).resolve().parents[1]
        os.chdir(project)
        if args.watchdog:
            from sentinel.ops.watchdog import main as run_watchdog
            sys.argv = ["watchdog", "--heartbeat", str(Path(cfg.ops.state_dir) / "heartbeat.json"),
                        "--kill-file", cfg.ops.killswitch_file,
                        "--audit", str(Path(cfg.ops.state_dir) / "watchdog.jsonl"),
                        "--timeout", str(cfg.ops.deadman_timeout_sec),
                        "--action", cfg.ops.deadman_action]
            return run_watchdog()
        from scripts.serve import main as serve
        sys.argv = ["serve.py", "--config", str(folder / "config.json"),
                    "--dashboard", str(project / "dashboard" / "dist")]
        return serve()
    finally:
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        if handler is not None:
            handler.close()
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
