"""Durable registry of acceptance verdicts.

A strategy may only touch real money from the ``accepted`` lifecycle, and this
is the store that makes that claim checkable. Before the registry existed, the
lifecycle field validated only that ``acceptance_run_id`` was a non-empty
string -- so one authenticated config write could mark any strategy accepted,
flip the venue to live and the mode to autonomous, with no acceptance run
having been executed at all.

Now the promotion path is: run the protocol -> store the verdict here -> the
config write is checked against this store. A run id that is not in the store,
or whose verdict failed, or whose verdict belongs to a different strategy, is
refused.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.clock import wall_ns


def config_fingerprint(instruments, params, timeframe: str) -> str:
    """Identity of the exact configuration a verdict was earned on.

    A verdict authorises a *configuration*, not a name. Without this, a strategy
    promoted on EUR_USD at H4 with a 55-bar channel could be re-pointed at four
    other pairs with a 5-bar channel while keeping the accepted badge and the
    same run id -- wearing evidence earned by a different strategy.
    """
    body = json.dumps({
        "instruments": sorted(instruments or []),
        "params": params or {},
        "timeframe": timeframe or "",
    }, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]

SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    run_id      TEXT PRIMARY KEY,
    strategy    TEXT NOT NULL,
    accepted    INTEGER NOT NULL,
    created_ns  INTEGER NOT NULL,
    stored_ns   INTEGER NOT NULL,
    data_label  TEXT NOT NULL,
    summary     TEXT NOT NULL,
    config_hash TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdicts_strategy ON verdicts(strategy, created_ns);
"""


class RegistryUnreadable(RuntimeError):
    """The verdict registry exists but cannot be read.

    Distinct from "this run id is not in the registry". Conflating the two is
    how a disk failure, a restore that missed a file, or a container without a
    persistent volume silently demotes every accepted strategy and writes that
    demotion back to the configuration -- destroying acceptance state that can
    only be rebuilt by re-running the whole protocol.
    """


class VerdictStore:
    def __init__(self, path: str | Path = "var/verdicts.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        existed = self.path.exists() and self.path.stat().st_size > 0
        try:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            with self._lock:
                self._conn.executescript(SCHEMA)
                self._conn.commit()
        except sqlite3.DatabaseError as exc:
            raise RegistryUnreadable(
                f"the acceptance registry at {self.path} exists but is not a readable "
                f"database ({exc}). This is NOT evidence that no strategy was ever "
                "accepted; restore the file or move it aside deliberately."
            ) from exc
        # A registry that vanished is different from one that is legitimately
        # empty. `existed` records what was on disk when we opened it; a write
        # in this process makes it present from then on.
        self._existed_on_open = existed

    def record(self, verdict, *, config_hash: str = "") -> str:
        payload = verdict.to_dict()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO verdicts (run_id, strategy, accepted, created_ns,"
                " stored_ns, data_label, summary, config_hash, payload)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (verdict.run_id, verdict.strategy, 1 if verdict.accepted else 0,
                 verdict.created_at_ns, wall_ns(), verdict.data_label, verdict.summary,
                 config_hash or str(payload.get("config_hash", "")),
                 json.dumps(payload, ensure_ascii=False)))
            self._conn.commit()
        self._existed_on_open = True
        return verdict.run_id

    @property
    def was_present(self) -> bool:
        """Whether this registry holds, or has ever held, a verdict.

        Used to tell "no such run id" (a legitimate refusal) from "the registry
        is gone" (a state that must not be repaired automatically, because the
        repair erases acceptance history irreversibly).
        """
        if self._existed_on_open:
            return True
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM verdicts").fetchone()
        return bool(row and row["c"])

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM verdicts WHERE run_id=?",
                                     (run_id,)).fetchone()
        return dict(row) if row else None

    def list(self, strategy: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        q = "SELECT run_id, strategy, accepted, created_ns, data_label, summary FROM verdicts"
        args: List[Any] = []
        if strategy:
            q += " WHERE strategy = ?"
            args.append(strategy)
        q += " ORDER BY created_ns DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def authorises(self, strategy: str, run_id: Optional[str],
                   config_hash: str) -> tuple[bool, str]:
        """May ``strategy``, in THIS configuration, be marked accepted on ``run_id``?

        ``config_hash`` is REQUIRED, not optional. A default would make the
        configuration binding opt-in, and a future caller that omitted it would
        lose the protection silently instead of failing.
        """
        if not run_id:
            return False, "no acceptance run id was supplied"
        row = self.get(run_id)
        if row is None:
            return False, (f"acceptance run {run_id!r} is not in the verdict store; "
                           "a strategy cannot be promoted on the strength of an id "
                           "that no run produced")
        if row["strategy"] != strategy:
            return False, (f"acceptance run {run_id!r} belongs to {row['strategy']!r}, "
                           f"not {strategy!r}")
        if not row["accepted"]:
            return False, (f"acceptance run {run_id!r} did NOT pass: {row['summary']}")
        if row["data_label"] != "live-quality":
            return False, (f"acceptance run {run_id!r} was evaluated on "
                           f"{row['data_label']!r} data; promotion requires the venue's "
                           "own historical bid/ask and real commission schedule")
        if not config_hash:
            return False, "no configuration fingerprint was supplied to check against"
        stored = (row["config_hash"] or "").strip()
        if stored and stored != config_hash:
            return False, (f"acceptance run {run_id!r} was earned on a different "
                           "configuration (instruments, parameters or timeframe have "
                           "changed since); re-run the protocol on the configuration "
                           "you intend to trade")
        if not stored:
            return False, (f"acceptance run {run_id!r} predates configuration binding "
                           "and cannot be matched to the configuration being promoted; "
                           "re-run the protocol")
        return True, "ok"

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------- #
# Startup enforcement
# --------------------------------------------------------------------------- #


def enforce_config_authority(config, store: "VerdictStore",
                             *, persist: bool = True) -> tuple[object, List[str], bool]:
    """Re-check a loaded configuration against the verdict registry.

    The API's promotion guard is necessary but not sufficient: a
    ``var/config.json`` that was hand-edited, restored from a backup, or written
    by an older build never passes through it. Without this, the config file is a
    complete escalation surface -- live mode, autonomous mode and an "accepted"
    badge, all with no second factor and no acceptance run.

    Violations are REPAIRED downward, never ignored: an unbacked strategy is
    demoted to ``suspended`` (its open positions are still managed out, it simply
    opens nothing new), and if that leaves a live configuration with no
    authorised strategy the venue mode is forced back to ``paper``. The returned
    list is the audit record of what was refused.
    """
    from ..core.config import ExecutionVenueMode, SentinelConfig

    violations: List[str] = []
    data = config.model_dump(mode="json")
    live = data.get("execution", {}).get("venue_mode") == ExecutionVenueMode.LIVE.value
    claims_acceptance = any(a.get("lifecycle") == "accepted"
                            for a in data.get("strategies", []))

    # A configuration that claims acceptance against a registry that was never
    # written is a state nobody should repair automatically. Repairing it would
    # erase the run ids, and the only recovery is re-running the protocol.
    if claims_acceptance and not store.was_present:
        raise RegistryUnreadable(
            f"the configuration marks a strategy as accepted but the acceptance "
            f"registry at {store.path} is missing or empty. Restore it, or clear the "
            "accepted lifecycle deliberately -- this is not repaired automatically "
            "because the repair is irreversible.")

    for alloc in data.get("strategies", []):
        if alloc.get("lifecycle") != "accepted":
            continue
        fingerprint = config_fingerprint(alloc.get("instruments"), alloc.get("params"),
                                         alloc.get("timeframe"))
        ok, why = store.authorises(alloc.get("name", ""), alloc.get("acceptance_run_id"),
                                   fingerprint)
        if not ok:
            violations.append(
                f"strategy {alloc.get('name')!r} claims the accepted lifecycle but "
                f"{why}; demoted to suspended")
            alloc["lifecycle"] = "suspended"
            alloc["enabled"] = False
            alloc["accepted_at_ns"] = None
            alloc["acceptance_run_id"] = None

    if live:
        tradable = [a for a in data.get("strategies", [])
                    if a.get("enabled") and a.get("lifecycle") == "accepted"]
        if not tradable:
            violations.append(
                "the configuration asks for live trading but no enabled strategy holds "
                "a verified acceptance verdict; the venue mode is forced back to paper")
            data["execution"]["venue_mode"] = ExecutionVenueMode.PAPER.value

    if not violations:
        return config, [], False

    # FRESH #5: a repair is a mutation, so it takes a new version. Leaving the
    # version untouched meant one version number denoted two different
    # configurations, and diff_configs -- which skips the metadata fields --
    # would not surface the demotion either.
    data["version"] = int(data.get("version", 0)) + 1
    data["updated_at_ns"] = wall_ns()
    data["updated_by"] = "startup-authority"
    return SentinelConfig.model_validate(data), violations, True
