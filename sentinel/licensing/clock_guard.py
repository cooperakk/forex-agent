"""Anti-rollback state for time-limited licences.

The problem
-----------
Every expiry check in every licensing system on earth reduces to comparing the
licence's end date against the machine's clock. The machine's clock belongs to
the customer. So the cheapest bypass of a three-month licence is not reverse
engineering, not patching, not key extraction -- it is ``date -s``, and it
takes one line and no knowledge of the product.

What this module does about it
------------------------------
It keeps a small authenticated state file recording:

* the **highest wall-clock time ever observed** on this installation, and
* the set of **licence ids already seen past their expiry**.

Two rules follow, and between them they close the trivial bypass:

1. If the clock is now materially *behind* the high-water mark, time has been
   moved backwards. The licence is treated as unverifiable until the clock is
   corrected -- not as valid, and not as forged.
2. A licence that this installation has *already watched expire* stays expired
   for ever, whatever the clock says afterwards. Expiry is recorded as a fact
   about history, not recomputed from a number the customer controls.

The state is authenticated with HMAC-SHA256 under a key derived from this
machine's fingerprint and the vendor public key, so the file cannot be
meaningfully hand-edited and cannot be lifted onto a different machine.

What it does NOT do -- read this
--------------------------------
The derivation inputs are all present on the customer's machine, so someone
with root and this source file can recompute the HMAC and write whatever state
they like. They can also simply delete the file. **This raises the bypass from
"change the clock" to "read the source, derive the key, forge the state" --
which is a real increase and is not a guarantee.** Deleting the file is
detected and reported, because a healthy installation that has been running for
months and has no anti-rollback state has had it removed; but "reported" means
a warning in the dashboard and a line in the audit chain, not a refusal, since
a genuine restore-from-backup looks identical.

The only structural answer remains the one in ``licensing/__init__.py``: put
the valuable part on a server the vendor runs. Everything here is a good lock
on a door the customer owns.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

#: How far the clock may move backwards before it is treated as tampering.
#: NTP corrections, a VM resuming from a snapshot and a DST-confused RTC are
#: all real and all small. Six hours is far larger than any of them and far
#: smaller than the days a bypass needs.
CLOCK_SLACK_SEC = 6 * 3600

_STATE_VERSION = 1


class ClockRollback(RuntimeError):
    """The system clock moved backwards past the tolerance."""


@dataclass
class GuardState:
    high_water_ns: int = 0
    first_seen_ns: int = 0
    checks: int = 0
    expired_licences: List[str] = field(default_factory=list)
    last_licence_id: str = ""
    version: int = _STATE_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "high_water_ns": self.high_water_ns,
            "first_seen_ns": self.first_seen_ns,
            "checks": self.checks,
            # Insertion order, de-duplicated. Sorting here destroyed the order
            # the [-200:] truncation depends on, so the survivors were the
            # alphabetically-highest ids rather than the most recent.
            "expired_licences": list(dict.fromkeys(self.expired_licences)),
            "last_licence_id": self.last_licence_id,
        }


@dataclass
class GuardReport:
    """What the guard concluded, for the dashboard and the audit journal."""

    ok: bool
    rolled_back: bool = False
    rollback_seconds: float = 0.0
    previously_expired: bool = False
    state_missing: bool = False
    state_unreadable: bool = False
    state_unwritable: bool = False
    message: str = ""
    checks: int = 0
    first_seen_ns: int = 0

    @property
    def degraded(self) -> bool:
        """The guard has no reliable memory, for any reason."""
        return self.state_missing or self.state_unreadable or self.state_unwritable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "rolled_back": self.rolled_back,
            "rollback_seconds": round(self.rollback_seconds, 1),
            "previously_expired": self.previously_expired,
            "state_missing": self.state_missing,
            "state_unreadable": self.state_unreadable,
            "state_unwritable": self.state_unwritable,
            "degraded": self.degraded,
            "message": self.message, "checks": self.checks,
            "first_seen_ns": self.first_seen_ns,
        }


def _derive_key(fingerprint: Dict[str, str], public_key_b64: str) -> bytes:
    """A per-installation, per-vendor key. Deterministic, never stored.

    ``fingerprint`` must be the map the LICENCE is bound to, not the one this
    machine reports today. Those differ, and the difference was a complete
    bypass: a licence tolerates 3-of-5 fingerprint components matching, so
    changing the hostname kept the licence valid while silently changing this
    key -- the state file then failed to authenticate, the guard started from
    an empty history, and a licence recorded as permanently expired came back
    valid with the clock wound back. One `hostnamectl set-hostname` plus one
    `date -s` defeated both rules at once.

    Keying on the licence's own bound map fixes that: it is a constant for the
    life of the licence, so no change to the running machine can rotate it.
    """
    material = json.dumps(
        {"fp": {k: fingerprint.get(k, "") for k in sorted(fingerprint)},
         "pk": public_key_b64},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b"sentinel-clock-guard-v2|" + material).digest()


class ClockGuard:
    """Load, evaluate, persist. One instance per process."""

    def __init__(self, path: str | Path, *, public_key_b64: str = "",
                 fingerprint: Optional[Dict[str, str]] = None,
                 slack_sec: int = CLOCK_SLACK_SEC) -> None:
        self.path = Path(path)
        self.slack_sec = int(slack_sec)
        self._public_key = public_key_b64 or ""
        # The key is derived per LICENCE, in rekey(), because it must be pinned
        # to something the running machine cannot change. Until a licence is
        # seen, fall back to the vendor key alone -- constant, and the same on
        # every install, which is correct for the "no licence file" path where
        # there is nothing installation-specific to bind to yet.
        self._bound: Dict[str, str] = dict(fingerprint or {})
        self._key = _derive_key(self._bound, self._public_key)

    def rekey(self, licence_machine: Optional[Dict[str, str]]) -> None:
        """Pin the state key to the machine map inside the licence."""
        self._bound = dict(licence_machine or {})
        self._key = _derive_key(self._bound, self._public_key)

    # -- storage ------------------------------------------------------------ #

    def _read(self) -> tuple:
        """(state, missing, unreadable)."""
        if not self.path.exists():
            return GuardState(), True, False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            body = raw["state"]
            tag = raw["tag"]
        except Exception:  # noqa: BLE001
            return GuardState(), False, True
        expected = hmac.new(self._key, _canon(body), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(tag)):
            return GuardState(), False, True
        state = GuardState(
            high_water_ns=int(body.get("high_water_ns", 0)),
            first_seen_ns=int(body.get("first_seen_ns", 0)),
            checks=int(body.get("checks", 0)),
            expired_licences=list(body.get("expired_licences", []) or []),
            last_licence_id=str(body.get("last_licence_id", "")),
            version=int(body.get("version", _STATE_VERSION)),
        )
        return state, False, False

    def _write(self, state: GuardState) -> bool:
        """Persist, and SAY whether it worked.

        Swallowing the failure was a complete bypass and a cheaper one than the
        documented floor: `mkdir licence-timing.json.tmp` in the state
        directory made every write raise IsADirectoryError, which was
        discarded. The high-water mark then froze for ever, an expiry watched
        happening was never recorded, and the report still said `ok=True,
        state_missing=False` -- so nothing warned, nothing was journalled, and
        the clock could be wound back indefinitely. No root, no key derivation,
        one command.
        """
        body = state.to_dict()
        document = {
            "state": body,
            "tag": hmac.new(self._key, _canon(body), hashlib.sha256).hexdigest(),
        }
        try:
            # mkdir is INSIDE the handler: a state directory that cannot be
            # created raises here, and a guard which cannot write must not stop
            # the agent from trading -- it must complain instead.
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # A RANDOM temp name in the same directory, so a squatted path
            # cannot pin the failure and two writers cannot collide.
            import tempfile
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
            tmp = Path(tmp_name)
            try:
                os.write(fd, json.dumps(document, sort_keys=True).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    # -- the check ---------------------------------------------------------- #

    def evaluate(self, *, now_ns: int, licence_id: str = "",
                 licence_expired: bool = False) -> GuardReport:
        """Record this moment and report anything inconsistent with the past."""
        state, missing, unreadable = self._read()
        report = GuardReport(ok=True, state_missing=missing,
                             state_unreadable=unreadable,
                             checks=state.checks + 1,
                             first_seen_ns=state.first_seen_ns or now_ns)

        if unreadable:
            # Either corruption or an edit. Both mean the record of the past is
            # gone, and rebuilding it from the present is precisely what an
            # attacker wants -- so say so loudly and start again from here.
            report.message = (
                "the licence timing record did not authenticate. It was edited, "
                "corrupted, or copied from another machine. A new record starts "
                "from now, and this event is in the audit journal.")

        rollback = (state.high_water_ns - now_ns) / 1e9 if state.high_water_ns else 0.0
        if rollback > self.slack_sec:
            report.ok = False
            report.rolled_back = True
            report.rollback_seconds = rollback
            report.message = (
                f"this machine's clock is {rollback / 86400:.1f} days behind the "
                "latest time this installation has already seen. Until the clock "
                "is correct, the licence cannot be checked and live trading is "
                "refused. Open positions keep being managed.")

        if licence_id and licence_id in set(state.expired_licences):
            report.previously_expired = True
            report.ok = False
            if not report.message:
                report.message = (
                    "this licence has already been seen past its expiry date on "
                    "this installation. Expiry is recorded as something that "
                    "happened, so moving the clock does not undo it. Renew the "
                    "licence.")

        # Persist forward. The high-water mark only ever ADVANCES, so a clock
        # set forward and then corrected leaves a mark that looks like the
        # future -- which is why the slack exists and why a correction below it
        # is silently tolerated.
        state.high_water_ns = max(state.high_water_ns, int(now_ns))
        state.first_seen_ns = state.first_seen_ns or int(now_ns)
        state.checks += 1
        state.last_licence_id = licence_id or state.last_licence_id
        if licence_expired and licence_id:
            if licence_id not in state.expired_licences:
                state.expired_licences.append(licence_id)
            # Bound the list. A vendor issuing quarterly licences for twenty
            # years produces eighty entries; anything beyond that is either a
            # test harness or an attempt to grow the file, and the oldest
            # entries are the ones least likely to be replayed.
            state.expired_licences = state.expired_licences[-200:]
        if not self._write(state):
            report.state_unwritable = True
            if not report.message:
                report.message = (
                    "the licence timing record could not be written, so this "
                    "installation is not remembering when it last ran. Check "
                    "that the state directory is writable and that nothing has "
                    "taken the name of the temporary file.")
        report.checks = state.checks
        report.first_seen_ns = state.first_seen_ns
        return report


def _canon(body: Dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def monotonic_now_ns() -> int:
    """Wall clock, but never used alone for a security decision here."""
    return time.time_ns()
