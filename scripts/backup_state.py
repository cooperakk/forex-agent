#!/usr/bin/env python3
"""Back up the state that cannot be re-fetched from the broker. Cross-platform.

The same contract as deploy/backup.sh, for hosts without bash and sqlite3 --
Windows Server in particular:

* SQLite databases are copied through the database's own backup API, never
  with a plain file copy (a live database copied mid-write restores corrupt);
* the audit chains are re-verified INSIDE the backup, with a standalone walker
  that never opens the copy for writing;
* sealed credential stores are included, their KEY is not -- a backup that
  carries both the lock and the key protects nothing;
* the archive is created private (0600 on POSIX) and old ones are pruned.

    python scripts/backup_state.py --state C:\\ProgramData\\SentinelFX --dest D:\\backups
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import List, Optional

DATABASES = ("verdicts.db", "memory.db", "market.db", "calendar.db", "users.db", "ai.db",
             "brain.db")
FILES = ("audit.jsonl", "watchdog.jsonl", "admin.jsonl", "agent_state.json",
         "proposals.json", "config.json", "brokers.json", "broker-secrets.json",
         "ai-secrets.json", "ai.json", "mt5-intents.json", "licence.key",
         "licence-timing.json", "licence-lease.json", "notify.json", "notify-secrets.json")
JOURNALS = ("audit.jsonl", "watchdog.jsonl", "admin.jsonl")
GENESIS = "0" * 64


def _find(state: Path, name: str) -> Optional[Path]:
    for candidate in (state / "var" / name, state / name):
        if candidate.is_file():
            return candidate
    return None


def verify_chain(path: Path) -> tuple:
    """(ok, message). A standalone walker: it never writes to the file."""
    prev, expected, count = GENESIS, 1, 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return False, f"line {lineno} is not valid JSON"
            if rec.get("seq") != expected:
                return False, f"expected seq {expected}, saw {rec.get('seq')!r}"
            if rec.get("prev_hash") != prev:
                return False, f"previous-hash mismatch at seq {expected}"
            body = json.dumps({k: rec[k] for k in ("seq", "ts_ns", "run_id", "event",
                                                    "actor", "payload", "prev_hash")},
                              sort_keys=True, separators=(",", ":"))
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != rec.get("hash"):
                return False, f"record hash mismatch at seq {expected}"
            prev, expected, count = rec["hash"], expected + 1, count + 1
    return True, f"OK ({count} records)"


def backup(state: Path, dest: Path, keep_days: int = 30) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = dest / f"sentinel-state-{stamp}.zip"
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        copied: List[str] = []
        for name in DATABASES:
            src = _find(state, name)
            if src is None:
                continue
            # as_uri() gives file:///C:/... on Windows and file:///... on POSIX;
            # a bare "file:C:\\..." is not a valid SQLite URI on Windows.
            with sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True) as source, \
                    sqlite3.connect(tmp / name) as target:
                source.backup(target)
            copied.append(name)
        for name in FILES:
            src = _find(state, name)
            if src is not None:
                shutil.copy2(src, tmp / name)
                copied.append(name)
        for name in JOURNALS:
            if (tmp / name).is_file():
                ok, message = verify_chain(tmp / name)
                print(f"[backup] {name}: {message}" if ok
                      else f"[backup] WARNING: {name} did not verify: {message}")
        fd = os.open(out, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as raw, zipfile.ZipFile(raw, "w",
                                                          zipfile.ZIP_DEFLATED) as zf:
            for name in copied:
                zf.write(tmp / name, arcname=name)
    print(f"[backup] {out} ({out.stat().st_size // 1024} KB, {len(copied)} files)")
    print("[backup] NOTE: the credential key (broker-secrets.key / SENTINEL_SECRET_KEY) "
          "is deliberately not in this archive; keep a copy of it somewhere else.")
    cutoff = time.time() - keep_days * 86400
    for old in dest.glob("sentinel-state-*.zip"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True, help="state directory (holds var/ or the files)")
    ap.add_argument("--dest", required=True, help="where archives are written")
    ap.add_argument("--keep-days", type=int, default=30)
    args = ap.parse_args(argv)
    state = Path(args.state)
    if not state.is_dir():
        print(f"[backup] no state directory at {state}", file=sys.stderr)
        return 1
    backup(state, Path(args.dest), args.keep_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
