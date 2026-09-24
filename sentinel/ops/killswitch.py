"""Out-of-band kill switch and dead-man watchdog.

Both exist because the most important control is the one that still works when
the main program does not.

**Kill switch.** A file on disk. Creating it stops the agent from opening risk
within one decision cycle, and no part of the trading process can remove it.
A file is used rather than an API endpoint because it keeps working when the
web process is wedged, the event loop is blocked, or the agent is in a tight
error loop -- and because ``touch var/KILL`` over SSH needs no credentials the
agent could leak.

**Dead-man switch.** A separate process (``ops/watchdog.py``) reads a heartbeat
file. If the heartbeat stops for longer than the timeout, the watchdog acts on
its own -- close-only, flatten, or alert. The agent cannot suppress it, because
suppression would require the agent to be alive, which is the exact condition
being tested.

A note on ``systemd``: ``Restart=always`` on a trading process is dangerous. A
crash loop becomes a resubmission loop. The provided unit uses
``Restart=on-failure`` with a burst limit, and every start begins with a
mandatory reconciliation.
"""

from __future__ import annotations

import json
import os
import tempfile
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..core.audit import AuditLog, EventType
from ..core.clock import mono_ns, wall_ns


@dataclass
class KillState:
    engaged: bool
    reason: str = ""
    engaged_at_ns: Optional[int] = None
    engaged_by: str = ""

    def to_dict(self) -> dict:
        return {"engaged": self.engaged, "reason": self.reason,
                "engaged_at_ns": self.engaged_at_ns, "engaged_by": self.engaged_by}


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically, with a UNIQUE staging file.

    A fixed ``<name>.tmp`` is shared by every writer, and the engine and the
    watchdog are independent processes engaging the SAME kill file -- so they
    collide precisely during an incident. Measured: 240 concurrent engage()
    calls produced 186 FileNotFoundError from os.replace, because another
    writer had already moved the shared staging file away. The kill itself
    still worked (the read side is fail-safe), but the API returned HTTP 500 to
    the operator and, because the exception fired first, NO audit record was
    written for the engagement.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class KillSwitch:
    """File-backed. Checked on every decision cycle and before every order."""

    def __init__(self, path: str | Path = "var/KILL", audit: Optional[AuditLog] = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.audit = audit
        self._last_state = self.read().engaged

    def read(self) -> KillState:
        if not self.path.exists():
            return KillState(engaged=False)
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            # Unreadable file: assume engaged. Failing closed is the only safe
            # interpretation of "I cannot tell whether I am allowed to trade".
            return KillState(engaged=True, reason="kill file exists but is unreadable")
        if not raw:
            return KillState(engaged=True, reason="manual", engaged_by="file")
        try:
            data = json.loads(raw)
            return KillState(engaged=True, reason=data.get("reason", "manual"),
                             engaged_at_ns=data.get("engaged_at_ns"),
                             engaged_by=data.get("engaged_by", "file"))
        except json.JSONDecodeError:
            return KillState(engaged=True, reason=raw[:200], engaged_by="file")

    def engage(self, reason: str, by: str = "system") -> KillState:
        state = KillState(engaged=True, reason=reason, engaged_at_ns=wall_ns(), engaged_by=by)
        _atomic_write_json(self.path, state.to_dict())
        if self.audit:
            self.audit.append(EventType.KILL_SWITCH, {"engaged": True, "reason": reason},
                              actor=by)
        self._last_state = True
        return state

    def release(self, by: str = "operator") -> bool:
        """Deliberately requires a human. Nothing automatic calls this."""
        if not self.path.exists():
            return False
        self.path.unlink()
        if self.audit:
            self.audit.append(EventType.KILL_SWITCH, {"engaged": False, "released_by": by},
                              actor=by)
        self._last_state = False
        return True

    def poll(self) -> KillState:
        state = self.read()
        if state.engaged != self._last_state and self.audit:
            self.audit.append(EventType.KILL_SWITCH, state.to_dict(), actor="poll")
        self._last_state = state.engaged
        return state


class Heartbeat:
    """Liveness of the DECISION LOOP, written to a file an independent process reads.

    The timestamp the watchdog reads is the moment ``update()`` was last called
    -- which only the decision cycle does -- not the moment the file was last
    written. The background thread republishes that stamp so the file stays
    present, but it cannot make a stalled loop look alive.

    This distinction is the whole mechanism. Beating unconditionally from a
    background thread measures only that the PROCESS exists, so a loop deadlocked
    on a lock or blocked on a broker call -- exactly the condition the dead-man
    switch is for -- would never trip it.
    """

    def __init__(self, path: str | Path = "var/heartbeat.json",
                 interval_sec: int = 5) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.interval_sec = interval_sec
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._payload: dict = {}
        self._lock = threading.Lock()
        self._liveness_ns: int = wall_ns()

    def update(self, **fields) -> None:
        """Called by the decision loop. This is what advances liveness."""
        with self._lock:
            self._payload.update(fields)
            self._liveness_ns = wall_ns()

    def beat(self) -> None:
        with self._lock:
            payload = {
                # ts_ns is the LOOP's stamp, deliberately not "now".
                "ts_ns": self._liveness_ns,
                "written_ns": wall_ns(),
                "mono_ns": mono_ns(), "pid": os.getpid(),
                **self._payload,
            }
        _atomic_write_json(self.path, payload)  # atomic: the watchdog never sees a partial file

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(self.interval_sec):
                try:
                    self.beat()
                except OSError:
                    pass

        self._thread = threading.Thread(target=loop, name="heartbeat", daemon=True)
        self.beat()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def age_sec(self) -> Optional[float]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return (wall_ns() - int(data["ts_ns"])) / 1e9
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return None
