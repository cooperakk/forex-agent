"""Tamper-evident, append-only audit journal.

Every decision, order, config change, veto and operator action is appended
here as one JSON object per line, each carrying the hash of the previous
record. Deleting or editing a line breaks the chain and ``verify()`` says
exactly where.

Why a hash chain and not just a log file: when a trade goes wrong the first
question is "what did the system know, and what did it do?". A log that can be
silently edited -- by a bug, a rotation script, or an intruder -- cannot answer
that question. This one can.

Durability: each append is followed by ``flush()`` and, when
``fsync_every_record`` is on, ``os.fsync``. That costs a few hundred
microseconds and buys the guarantee that an order recorded as sent really was
recorded before the socket write.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .clock import wall_ns
from .ids import RUN_ID

GENESIS = "0" * 64


class EventType(str, Enum):
    # lifecycle
    SYSTEM_START = "system.start"
    SYSTEM_STOP = "system.stop"
    HEARTBEAT = "system.heartbeat"
    CLOCK_ANOMALY = "system.clock_anomaly"
    # config
    CONFIG_CHANGE = "config.change"
    MODE_CHANGE = "config.mode_change"
    # market data
    DATA_STALE = "data.stale"
    DATA_GAP = "data.gap"
    DATA_RECOVERED = "data.recovered"
    DATA_DIVERGENCE = "data.divergence"   # broker price vs an independent reference
    # decisions
    SIGNAL = "decision.signal"
    PROPOSAL = "decision.proposal"
    PROPOSAL_ACCEPTED = "decision.proposal_accepted"
    PROPOSAL_REJECTED = "decision.proposal_rejected"
    RISK_VETO = "decision.risk_veto"
    # orders
    ORDER_INTENT = "order.intent"
    ORDER_SENT = "order.sent"
    ORDER_ACK = "order.ack"
    ORDER_FILLED = "order.filled"
    ORDER_REJECTED = "order.rejected"
    ORDER_CANCELLED = "order.cancelled"
    ORDER_UNKNOWN = "order.unknown"
    ORDER_DUPLICATE_BLOCKED = "order.duplicate_blocked"
    # positions
    POSITION_OPEN = "position.open"
    POSITION_MODIFY = "position.modify"
    POSITION_CLOSE = "position.close"
    # risk / ops
    LIMIT_BREACH = "risk.limit_breach"
    LADDER_STEP = "risk.ladder_step"
    HALT = "risk.halt"
    KILL_SWITCH = "ops.kill_switch"
    DEADMAN_TRIP = "ops.deadman"
    RECONCILE = "ops.reconcile"
    RECONCILE_MISMATCH = "ops.reconcile_mismatch"
    CONNECTIVITY = "ops.connectivity"
    # learning
    POSTMORTEM = "learn.postmortem"
    LESSON = "learn.lesson"
    PARAM_PROPOSAL = "learn.param_proposal"
    # research
    RESEARCH_RUN = "research.run"
    RESEARCH_VERDICT = "research.verdict"
    # security
    AUTH_SUCCESS = "sec.auth_ok"
    AUTH_FAILURE = "sec.auth_fail"
    WRITE_ACTION = "sec.write_action"
    WRITE_DENIED = "sec.write_denied"


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    ts_ns: int
    run_id: str
    event: str
    actor: str
    payload: Dict[str, Any]
    prev_hash: str
    hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "ts_ns": self.ts_ns,
            "run_id": self.run_id,
            "event": self.event,
            "actor": self.actor,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


def _digest(seq: int, ts_ns: int, run_id: str, event: str, actor: str,
            payload: Dict[str, Any], prev_hash: str) -> str:
    body = json.dumps(
        {
            "seq": seq, "ts_ns": ts_ns, "run_id": run_id, "event": event,
            "actor": actor, "payload": payload, "prev_hash": prev_hash,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class AuditChainDamaged(RuntimeError):
    """The journal is damaged beyond a torn final line.

    Raised rather than repaired. The repair for "damaged" would be to start a
    new chain, and that erases the record of every decision the system has ever
    made -- which is precisely what an attacker wants. A human restores from a
    backup.
    """


class AuditLog:
    """Thread-safe append-only journal with a verifiable hash chain."""

    def __init__(self, path: str | Path, *, fsync_every_record: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fsync = fsync_every_record
        # Set by _truncate_torn_tail when a torn final line was dropped.
        self._repaired_from: Optional[tuple] = None
        self._seq, self._last_hash = self._recover_tail()
        # 0o600: the journal contains position sizes and account state.
        self._fh = open(self.path, "a", encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - e.g. exotic filesystems
            pass
        if self._repaired_from is not None:
            seq, sidecar = self._repaired_from
            # The repair is itself part of the record, linked to the head hash
            # it resumed from, so nobody has to take the file's word for it.
            self.append(EventType.SYSTEM_START, {
                "chain_repaired": True,
                "resumed_from_seq": seq,
                "resumed_from_hash": self._last_hash,
                "torn_bytes_saved_to": sidecar,
                "note": "a torn final line was dropped after an unclean shutdown; "
                        "the chain before it is intact and continues here"},
                actor="audit")

    # -- recovery ----------------------------------------------------------- #

    def _recover_tail(self) -> tuple[int, str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, GENESIS
        last_line = ""
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last_line = line
        if not last_line:
            return 0, GENESIS
        try:
            rec = json.loads(last_line)
            return int(rec["seq"]), str(rec["hash"])
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

        # A torn FINAL line (power loss mid-write) is recoverable: drop that one
        # line and continue the existing chain.
        #
        # The previous behaviour moved the whole journal aside and restarted at
        # GENESIS, which turned a two-byte append into total erasure of the
        # history -- and verify() then reported "chain intact (0 records)". An
        # intruder could append "{" , wait for the automatic restart that
        # `Restart=on-failure` guarantees, and have the entire record replaced
        # by a clean-looking new chain. Never rewrite the live journal here.
        return self._truncate_torn_tail()

    def _truncate_torn_tail(self) -> tuple[int, str]:
        """Drop a single unparseable trailing line and resume the chain.

        The damaged bytes are preserved in a sidecar so nothing is destroyed,
        and a CHAIN_REPAIRED record is appended by the caller carrying the
        prior head hash, so the repair itself is part of the record.
        """
        raw = self.path.read_bytes()
        lines = raw.split(b"\n")
        # Strip trailing empties, then the one torn line.
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return 0, GENESIS
        torn = lines.pop()
        good: List[bytes] = lines
        last_seq, last_hash = 0, GENESIS
        if good:
            try:
                rec = json.loads(good[-1].decode("utf-8"))
                last_seq, last_hash = int(rec["seq"]), str(rec["hash"])
            except (json.JSONDecodeError, KeyError, ValueError, UnicodeDecodeError):
                # Damage is not confined to the final line. Refuse rather than
                # guess: a journal whose interior is corrupt must be restored
                # from a backup by a human, not repaired automatically.
                raise AuditChainDamaged(
                    f"{self.path} is damaged beyond the final line. Restore it from a "
                    "backup; this file is the record of every decision the system made "
                    "and must not be rebuilt automatically.")
        sidecar = self.path.with_suffix(self.path.suffix + f".torn.{wall_ns()}")
        try:
            sidecar.write_bytes(torn)
            os.chmod(sidecar, 0o600)
        except OSError:
            pass
        payload = b"\n".join(good) + b"\n" if good else b""
        tmp = self.path.with_suffix(self.path.suffix + ".repair")
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._repaired_from = (last_seq, str(sidecar.name))
        return last_seq, last_hash

    # -- write -------------------------------------------------------------- #

    def append(self, event: EventType | str, payload: Dict[str, Any], *,
               actor: str = "system") -> AuditRecord:
        ev = event.value if isinstance(event, EventType) else str(event)
        with self._lock:
            seq = self._seq + 1
            ts = wall_ns()
            prev = self._last_hash
            h = _digest(seq, ts, RUN_ID, ev, actor, payload, prev)
            rec = AuditRecord(seq, ts, RUN_ID, ev, actor, payload, prev, h)
            self._fh.write(json.dumps(rec.to_dict(), ensure_ascii=False,
                                      separators=(",", ":"), default=str) + "\n")
            self._fh.flush()
            if self._fsync:
                os.fsync(self._fh.fileno())
            self._seq, self._last_hash = seq, h
        self._notify_listeners(rec)
        return rec

    # -- listeners ----------------------------------------------------------- #

    def add_listener(self, fn) -> None:
        """Call ``fn(record)`` after every append (outside the journal lock).

        For side channels such as notifications. A listener must be quick --
        enqueue, never send -- and whatever it raises is swallowed: the record
        is already durable, and a broken side channel must never make an
        append look failed to the code that wrote it.
        """
        listeners = getattr(self, "_listeners", None)
        if listeners is None:
            self._listeners = listeners = []
        listeners.append(fn)

    def _notify_listeners(self, rec: "AuditRecord") -> None:
        for fn in list(getattr(self, "_listeners", None) or ()):
            try:
                fn(rec)
            except Exception:  # noqa: BLE001 - a side channel never fails an append
                pass

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()

    # -- read / verify ------------------------------------------------------ #

    def read(self, *, since_seq: int = 0, event: Optional[str] = None,
             limit: Optional[int] = None) -> list[Dict[str, Any]]:
        out: list[Dict[str, Any]] = []
        for rec in self.iter_records():
            if rec["seq"] <= since_seq:
                continue
            if event and rec["event"] != event:
                continue
            out.append(rec)
            if limit and len(out) >= limit:
                break
        return out

    def iter_records(self) -> Iterator[Dict[str, Any]]:
        """Yield each record, or a sentinel for a line that will not parse.

        An unparseable line is data about the file, not a reason to raise:
        letting json.JSONDecodeError escape turned /api/audit into a blanket
        HTTP 500, so the one endpoint that reports tampering reported nothing
        at all -- strictly worse than saying "line 11 is corrupt".
        """
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    yield {"__unparseable__": True, "__line__": lineno}

    def verify(self) -> tuple[bool, Optional[int], str]:
        """Walk the chain. Returns (ok, first_bad_seq, message)."""
        prev = GENESIS
        expected_seq = 1
        for rec in self.iter_records():
            if rec.get("__unparseable__"):
                return (False, None,
                        f"line {rec['__line__']} is not valid JSON: the journal has been "
                        "edited or truncated")
            if not isinstance(rec, dict) or "seq" not in rec or "hash" not in rec:
                return False, None, "a record is missing its sequence or hash"
            if rec["seq"] != expected_seq:
                return False, rec["seq"], f"sequence gap: expected {expected_seq}, saw {rec['seq']}"
            if rec["prev_hash"] != prev:
                return False, rec["seq"], "previous-hash mismatch (a record was altered or removed)"
            recomputed = _digest(rec["seq"], rec["ts_ns"], rec["run_id"], rec["event"],
                                 rec["actor"], rec["payload"], rec["prev_hash"])
            if recomputed != rec["hash"]:
                return False, rec["seq"], "record hash mismatch (payload was altered)"
            prev = rec["hash"]
            expected_seq += 1
        return True, None, f"chain intact ({expected_seq - 1} records)"

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def head_hash(self) -> str:
        return self._last_hash


class NullAudit(AuditLog):
    """No-op journal for unit tests and backtests. Never used in live paths."""

    def __init__(self) -> None:  # noqa: D107
        self._lock = threading.Lock()
        self._seq = 0
        self._last_hash = GENESIS
        self.path = Path(os.devnull)
        self._fsync = False
        self.records: list[AuditRecord] = []

    def append(self, event, payload, *, actor="system"):  # type: ignore[override]
        ev = event.value if isinstance(event, EventType) else str(event)
        with self._lock:
            self._seq += 1
            rec = AuditRecord(self._seq, wall_ns(), RUN_ID, ev, actor, payload,
                              self._last_hash, "")
            self.records.append(rec)
        self._notify_listeners(rec)
        return rec

    def close(self) -> None:  # type: ignore[override]
        return

    def iter_records(self):  # type: ignore[override]
        for r in self.records:
            yield r.to_dict()

    def verify(self):  # type: ignore[override]
        return True, None, "null audit"
