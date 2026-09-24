#!/usr/bin/env python3
"""Independent dead-man watchdog.

Runs as its own process and its own systemd unit. It reads the heartbeat file
and, if the trading engine goes quiet for longer than the timeout, acts without
asking the engine's permission -- because by then the engine is, by definition,
not answering.

Actions, in increasing severity:

``alert``       engage the kill switch and notify. Positions stay open with
                their venue-side stops.
``close_only``  engage the kill switch (no new entries) and notify. The default:
                it stops the bleeding without forcing an exit at a possibly
                terrible price.
``flatten``     close every position at market. Correct when positions might be
                unprotected; expensive otherwise, which is why it is not the
                default.

The watchdog itself is simple on purpose. A complicated watchdog is a second
thing that can fail.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sentinel.core.audit import AuditLog, EventType  # noqa: E402
from sentinel.core.clock import wall_ns  # noqa: E402
from sentinel.ops.killswitch import KillSwitch  # noqa: E402


def heartbeat_age(path: Path) -> float | None:
    """Seconds since the decision loop last completed a cycle.

    A NEGATIVE age means the clock moved: either the engine's host stepped
    forward or this host stepped back. Returning it unchanged made the dead-man
    switch report "healthy" for the whole duration of a backwards NTP step,
    however dead the loop actually was. It is reported as an anomaly instead --
    a clock that disagrees with the engine's is itself a reason to stop, since
    every timestamp the system records is now suspect.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age = (wall_ns() - int(data["ts_ns"])) / 1e9
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None
    if age < -CLOCK_TOLERANCE_SEC:
        return float("inf")     # treat as stale: the clocks disagree
    return max(0.0, age)


# How far the two hosts' clocks may disagree before the heartbeat is distrusted.
CLOCK_TOLERANCE_SEC = 5.0


def _kill_engaged(kill) -> bool:
    """Whether the kill file is currently engaged, read fresh each poll."""
    try:
        return bool(kill.read().engaged)
    except Exception:  # noqa: BLE001 - an unreadable kill file is treated as engaged
        return True


def flatten_positions(audit: AuditLog) -> None:
    """Close everything. Imported lazily so the watchdog starts even if the
    broker package is misconfigured -- a watchdog that will not start is worse
    than one that can only engage the kill switch."""
    try:
        from sentinel.core.config import SentinelConfig
        from sentinel.brokers import build_broker

        cfg = SentinelConfig.load(os.environ.get("SENTINEL_CONFIG", "var/config.json"))
        broker = build_broker(cfg.execution.broker)
        positions = broker.positions()
        closed, failed = [], []
        for pos in positions:
            # CHECK THE RESULT. Journalling a flatten without reading the
            # return value recorded "the book was closed" whenever the close
            # was refused -- and the one moment this code runs is the moment
            # nobody is watching. The adapter half of this was fixed (a close
            # that matches nothing now returns REJECTED); this is the other
            # half.
            result = broker.close_position(pos.instrument, reason="deadman_flatten")
            ok = getattr(result, "state", None) is not None and \
                getattr(result.state, "value", "") in ("filled", "partial")
            (closed if ok else failed).append(pos.instrument)
            audit.append(EventType.DEADMAN_TRIP,
                         {"action": "flatten", "instrument": pos.instrument,
                          "closed": ok,
                          "reason": (None if ok else
                                     getattr(result, "reject_reason", "unknown"))},
                         actor="watchdog")

        # Verify against the venue rather than trusting the loop. If anything
        # is still open, say so loudly: an operator who reads "flatten: done"
        # and still has a live book is worse off than one who reads nothing.
        try:
            remaining = [p.instrument for p in broker.positions()]
        except Exception as exc:  # noqa: BLE001
            remaining = [f"<could not re-read the book: {exc}>"]
        audit.append(EventType.DEADMAN_TRIP,
                     {"action": "flatten_summary",
                      "requested": len(positions), "closed": closed,
                      "failed": failed, "still_open": remaining,
                      "complete": not remaining},
                     actor="watchdog")
        if remaining:
            print(f"[watchdog] FLATTEN INCOMPLETE: still open {remaining}. "
                  "The kill switch is engaged, so no NEW risk is taken, but "
                  "these positions are live and rely on their venue-side stops.",
                  flush=True)
    except Exception as exc:  # noqa: BLE001 - last line of defence, log everything
        audit.append(EventType.DEADMAN_TRIP,
                     {"action": "flatten", "error": str(exc)}, actor="watchdog")


def _config_deadman_defaults() -> tuple:
    """(timeout_sec, action) from SENTINEL_CONFIG, or the built-in defaults.

    Read defensively: a watchdog that will not start because the config is
    unreadable is worse than one using its defaults.
    """
    try:
        from sentinel.core.config import SentinelConfig
        cfg = SentinelConfig.load(os.environ.get("SENTINEL_CONFIG", "var/config.json"))
        return float(cfg.ops.deadman_timeout_sec), str(cfg.ops.deadman_action)
    except Exception:  # noqa: BLE001
        return 180.0, "close_only"


def main() -> int:
    ap = argparse.ArgumentParser(description="Sentinel-FX dead-man watchdog")
    ap.add_argument("--heartbeat", default="var/heartbeat.json")
    ap.add_argument("--kill-file", default="var/KILL")
    # A SEPARATE journal from the engine's. Each AuditLog keeps its own
    # (seq, last_hash) in memory, so two processes appending to one file
    # produce duplicate sequence numbers and break the chain permanently --
    # and the first watchdog trip, the most forensically important moment in
    # the system's life, is exactly when that would happen.
    ap.add_argument("--audit", default="var/watchdog.jsonl")
    # Defaults come from the CONFIG when it is readable, so
    # ops.deadman_timeout_sec and ops.deadman_action are settings an operator
    # can actually change instead of dead fields that nothing reads.
    _cfg_timeout, _cfg_action = _config_deadman_defaults()
    ap.add_argument("--timeout", type=float, default=_cfg_timeout)
    ap.add_argument("--poll", type=float, default=5.0)
    ap.add_argument("--action", choices=["alert", "close_only", "flatten"],
                    default=_cfg_action)
    ap.add_argument("--once", action="store_true", help="single check, for tests")
    args = ap.parse_args()

    audit = AuditLog(args.audit)
    kill = KillSwitch(args.kill_file, audit)
    hb = Path(args.heartbeat)
    tripped = False

    print(f"[watchdog] pid={os.getpid()} watching {hb} "
          f"timeout={args.timeout}s action={args.action}", flush=True)

    # One grace period, measured from the watchdog's OWN start on the monotonic
    # clock, so an engine that is still initialising (journal replay, symbol
    # table, first reconcile) is not killed before its first cycle. It is not
    # refreshed per poll: a stale heartbeat after the grace period is a trip.
    started = time.monotonic()
    while True:
        age = heartbeat_age(hb)
        stale = age is None or age > args.timeout
        if stale and not getattr(args, "once", False) and time.monotonic() - started < args.timeout:
            time.sleep(args.poll)
            continue
        # STATE-based, not edge-based. Latching on the transition meant that if
        # an owner released the kill switch while the engine was still wedged
        # -- believing it had recovered -- the watchdog never re-engaged it for
        # the rest of the outage, and a loop that later unblocked resumed
        # trading with no dead-man protection at all.
        if stale and (not tripped or not _kill_engaged(kill)):
            first_trip = not tripped
            tripped = True
            reason = (f"no heartbeat for {age:.0f}s (limit {args.timeout:.0f}s)"
                      if age is not None and age != float("inf")
                      else "heartbeat missing or the clocks disagree")
            audit.append(EventType.DEADMAN_TRIP,
                         {"action": args.action, "reason": reason,
                          "heartbeat_age_sec": None if age in (None, float("inf")) else age,
                          "re_engaged": not first_trip},
                         actor="watchdog")
            kill.engage(f"dead-man switch: {reason}", by="watchdog")
            print(f"[watchdog] {'TRIPPED' if first_trip else 'RE-ENGAGED'}: "
                  f"{reason} -> {args.action}", flush=True)
            if args.action == "flatten" and first_trip:
                flatten_positions(audit)
        elif not stale and tripped:
            # The engine came back. The kill switch is NOT released
            # automatically: a human decides whether it is safe to resume.
            tripped = False
            audit.append(EventType.HEARTBEAT,
                         {"recovered": True, "heartbeat_age_sec": age,
                          "note": "kill switch stays engaged until a human releases it"},
                         actor="watchdog")
            print("[watchdog] heartbeat recovered; kill switch remains engaged", flush=True)
        if args.once:
            return 1 if stale else 0
        time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
