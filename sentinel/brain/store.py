"""The brain's memory: shadow signals, state, lab runs, reports, models (SQLite)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.clock import wall_ns

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow (
    key TEXT PRIMARY KEY, ts_ns INTEGER NOT NULL, strategy TEXT NOT NULL,
    instrument TEXT NOT NULL, side TEXT NOT NULL, timeframe TEXT, entry REAL NOT NULL,
    stop REAL NOT NULL, target REAL, horizon INTEGER NOT NULL, action TEXT NOT NULL,
    rule TEXT, size_mult REAL, layers TEXT, meta_p REAL, features TEXT, regime TEXT,
    cost_r REAL NOT NULL DEFAULT 0, resolved INTEGER NOT NULL DEFAULT 0, outcome_r REAL,
    exit TEXT, resolved_ns INTEGER, bars_seen INTEGER NOT NULL DEFAULT 0,
    first_seen_ns INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_shadow_open ON shadow(resolved, ts_ns);
CREATE INDEX IF NOT EXISTS idx_shadow_strategy ON shadow(strategy, resolved, ts_ns);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lab_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ns INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ns INTEGER NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY, ts_ns INTEGER NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
    report TEXT NOT NULL, status TEXT NOT NULL, decided_by TEXT, decided_ns INTEGER);
"""

#: Actions that were NOT taken, whose outcome is the counterfactual.
NOT_TAKEN = ("vetoed", "skipped")


class BrainStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except (FileExistsError, OSError):
            pass
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- shadow signals ------------------------------------------------------ #

    def record_signal(self, row: Dict[str, Any]) -> bool:
        """Insert once per (strategy, instrument, side, bar); later sightings of
        the same signal only upgrade the action (a signal vetoed at first and
        executed a minute later counts as executed). True if new."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO shadow (key, ts_ns, strategy, instrument, side, "
                "timeframe, entry, stop, target, horizon, action, rule, size_mult, layers, "
                "meta_p, features, regime, cost_r, first_seen_ns) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["key"], row["ts_ns"], row["strategy"], row["instrument"], row["side"],
                 row.get("timeframe"), row["entry"], row["stop"], row.get("target"),
                 row["horizon"], row["action"], row.get("rule"), row.get("size_mult"),
                 json.dumps(row.get("layers") or {}), row.get("meta_p"),
                 json.dumps(row.get("features") or {}), row.get("regime"),
                 float(row.get("cost_r") or 0.0), wall_ns()))
            new = cur.rowcount == 1
            if not new and row["action"] == "executed":
                self._conn.execute(
                    "UPDATE shadow SET action='executed', rule=NULL, size_mult=?, layers=? "
                    "WHERE key=? AND action != 'executed'",
                    (row.get("size_mult"), json.dumps(row.get("layers") or {}), row["key"]))
            self._conn.commit()
        return new

    def open_signals(self, limit: int = 400) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shadow WHERE resolved = 0 ORDER BY ts_ns LIMIT ?",
                (int(limit),)).fetchall()
        return [self._row(r) for r in rows]

    def resolve_signal(self, key: str, *, outcome_r: Optional[float], exit_kind: str,
                       bars_seen: int, resolved: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE shadow SET resolved=?, outcome_r=?, exit=?, bars_seen=?, "
                "resolved_ns=? WHERE key=?",
                (1 if resolved else 0, outcome_r, exit_kind, int(bars_seen),
                 wall_ns() if resolved else None, key))
            self._conn.commit()

    def resolved_signals(self, *, strategy: Optional[str] = None, since_ns: int = 0,
                         limit: int = 20_000) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM shadow WHERE resolved = 1 AND outcome_r IS NOT NULL AND ts_ns >= ?"
        args: List[Any] = [int(since_ns)]
        if strategy:
            sql += " AND strategy = ?"
            args.append(strategy)
        sql += " ORDER BY ts_ns DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def shadow_counts(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT action, resolved, COUNT(*) n FROM shadow GROUP BY action, resolved"
            ).fetchall()
        out: Dict[str, int] = {}
        for r in rows:
            out[f"{r['action']}:{'resolved' if r['resolved'] else 'open'}"] = int(r["n"])
        return out

    def prune(self, keep_days: int = 400) -> None:
        cutoff = wall_ns() - keep_days * 86_400 * 10**9
        with self._lock:
            self._conn.execute("DELETE FROM shadow WHERE ts_ns < ?", (cutoff,))
            self._conn.execute("DELETE FROM reports WHERE id NOT IN "
                               "(SELECT id FROM reports ORDER BY ts_ns DESC LIMIT 200)")
            self._conn.execute("DELETE FROM lab_runs WHERE id NOT IN "
                               "(SELECT id FROM lab_runs ORDER BY ts_ns DESC LIMIT 60)")
            self._conn.commit()

    @staticmethod
    def _row(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        for k in ("layers", "features"):
            try:
                d[k] = json.loads(d[k]) if d.get(k) else {}
            except ValueError:
                d[k] = {}
        return d

    # -- key/value state ----------------------------------------------------- #

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["v"])
        except ValueError:
            return default

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO state VALUES (?,?,?)",
                               (key, json.dumps(value, ensure_ascii=False, default=str),
                                wall_ns()))
            self._conn.commit()

    # -- lab runs and reports ------------------------------------------------ #

    def add_lab_run(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO lab_runs (ts_ns, payload) VALUES (?,?)",
                               (wall_ns(), json.dumps(payload, ensure_ascii=False, default=str)))
            self._conn.commit()

    def lab_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM lab_runs ORDER BY ts_ns DESC LIMIT ?",
                                      (int(limit),)).fetchall()
        return [{"id": r["id"], "ts_ns": r["ts_ns"], **json.loads(r["payload"])} for r in rows]

    def add_report(self, kind: str, payload: Dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO reports (ts_ns, kind, payload) VALUES (?,?,?)",
                                     (wall_ns(), kind,
                                      json.dumps(payload, ensure_ascii=False, default=str)))
            self._conn.commit()
            return int(cur.lastrowid)

    def reports(self, kind: Optional[str] = None, limit: int = 12) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM reports", []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        sql += " ORDER BY ts_ns DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [{"id": r["id"], "ts_ns": r["ts_ns"], "kind": r["kind"],
                 **json.loads(r["payload"])} for r in rows]

    # -- models ------------------------------------------------------------- #

    def add_model(self, model_id: str, path: str, sha256: str, report: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO models (id, ts_ns, path, sha256, report, status) "
                "VALUES (?,?,?,?,?, 'candidate')",
                (model_id, wall_ns(), path, sha256, json.dumps(report, default=str)))
            self._conn.commit()

    def models(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM models ORDER BY ts_ns DESC LIMIT ?",
                                      (int(limit),)).fetchall()
        return [dict(r) | {"report": json.loads(r["report"])} for r in rows]

    def model(self, model_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        return None if r is None else dict(r) | {"report": json.loads(r["report"])}

    def set_model_status(self, model_id: str, status: str, by: str) -> None:
        with self._lock:
            if status == "active":
                self._conn.execute("UPDATE models SET status='retired' WHERE status='active'")
            self._conn.execute("UPDATE models SET status=?, decided_by=?, decided_ns=? "
                               "WHERE id=?", (status, by[:64], wall_ns(), model_id))
            self._conn.commit()

    def active_model(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM models WHERE status='active' "
                                   "ORDER BY decided_ns DESC LIMIT 1").fetchone()
        return None if r is None else dict(r) | {"report": json.loads(r["report"])}
