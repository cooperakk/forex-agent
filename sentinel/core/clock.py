"""Clock discipline.

Three separate notions of time are kept apart on purpose, because conflating
them is one of the classic sources of silent trading bugs:

1. ``wall_ns()``    -- UTC wall-clock, nanoseconds. Can jump backwards (NTP
                       step, VM resume, leap smear). Used ONLY for stamping
                       events so they can be compared with broker timestamps.
2. ``mono_ns()``    -- monotonic counter, nanoseconds. Never jumps backwards.
                       Used for EVERY duration / timeout / rate-limit budget.
3. broker timestamp -- the venue's own stamp, stored verbatim next to ours.
                       ``ClockMonitor`` turns the difference into a measured
                       quantity (clock skew) instead of an invisible bug.

Everything is stored in UTC. Conversion to Asia/Tehran (or any local zone)
happens only in the presentation layer.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Optional

NS_PER_SECOND = 1_000_000_000
NS_PER_MS = 1_000_000

# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #


def wall_ns() -> int:
    """UTC wall-clock in nanoseconds. May jump. Never use for durations."""
    return time.time_ns()


def mono_ns() -> int:
    """Monotonic nanoseconds. Use for every duration measurement."""
    return time.monotonic_ns()


def utc_now() -> datetime:
    """Timezone-aware UTC datetime. Always aware -- naive datetimes are banned."""
    return datetime.now(timezone.utc)


def ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / NS_PER_SECOND, tz=timezone.utc)


def dt_to_ns(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime rejected: attach tzinfo (UTC) explicitly")
    return int(dt.timestamp() * NS_PER_SECOND)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or utc_now()).isoformat(timespec="microseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Stopwatch -- monotonic, safe across wall-clock steps
# --------------------------------------------------------------------------- #


class Stopwatch:
    """Monotonic elapsed-time measurement. ``with Stopwatch() as sw: ...``"""

    __slots__ = ("_start", "_stop")

    def __init__(self) -> None:
        self._start = mono_ns()
        self._stop: Optional[int] = None

    def __enter__(self) -> "Stopwatch":
        self._start = mono_ns()
        return self

    def __exit__(self, *exc) -> None:
        self._stop = mono_ns()

    @property
    def elapsed_ns(self) -> int:
        return (self._stop if self._stop is not None else mono_ns()) - self._start

    @property
    def elapsed_ms(self) -> float:
        return self.elapsed_ns / NS_PER_MS


class Deadline:
    """Monotonic deadline. ``Deadline(250).expired`` is immune to clock steps."""

    __slots__ = ("_deadline_ns",)

    def __init__(self, budget_ms: float) -> None:
        if budget_ms < 0:
            raise ValueError("budget_ms must be >= 0")
        self._deadline_ns = mono_ns() + int(budget_ms * NS_PER_MS)

    @property
    def remaining_ms(self) -> float:
        return max(0.0, (self._deadline_ns - mono_ns()) / NS_PER_MS)

    @property
    def expired(self) -> bool:
        return mono_ns() >= self._deadline_ns


# --------------------------------------------------------------------------- #
# Skew / drift monitoring
# --------------------------------------------------------------------------- #


@dataclass
class SkewSample:
    local_ns: int
    remote_ns: int
    round_trip_ns: int

    @property
    def skew_ns(self) -> int:
        """Positive => our clock is AHEAD of the venue.

        The round-trip is halved out on the assumption of a symmetric path.
        That assumption is imperfect; the residual shows up as noise in the
        distribution, which is exactly why we keep the whole distribution
        rather than a single number.
        """
        return self.local_ns - (self.remote_ns + self.round_trip_ns // 2)


@dataclass
class ClockMonitor:
    """Rolling estimate of skew against a venue clock.

    Trading rule that depends on this: if ``abs(median_skew_ms)`` exceeds the
    configured ceiling, the risk engine refuses new entries. A system that
    cannot say *when* something happened cannot honestly say what it knew.
    """

    window: int = 512
    samples: Deque[SkewSample] = field(default_factory=lambda: deque(maxlen=512))

    def __post_init__(self) -> None:
        self.samples = deque(maxlen=self.window)

    def observe(self, remote_ns: int, round_trip_ns: int = 0) -> SkewSample:
        s = SkewSample(local_ns=wall_ns(), remote_ns=remote_ns, round_trip_ns=round_trip_ns)
        self.samples.append(s)
        return s

    def _sorted_skews(self) -> list[int]:
        return sorted(s.skew_ns for s in self.samples)

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def median_skew_ms(self) -> Optional[float]:
        xs = self._sorted_skews()
        if not xs:
            return None
        n = len(xs)
        mid = n // 2
        med = xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2
        return med / NS_PER_MS

    @property
    def p95_abs_skew_ms(self) -> Optional[float]:
        xs = sorted(abs(s.skew_ns) for s in self.samples)
        if not xs:
            return None
        idx = min(len(xs) - 1, int(0.95 * (len(xs) - 1) + 0.5))
        return xs[idx] / NS_PER_MS

    def healthy(self, ceiling_ms: float) -> bool:
        m = self.median_skew_ms
        return m is not None and abs(m) <= ceiling_ms


class MonotonicGuard:
    """Detects wall-clock regressions.

    ``systemd``-restarted processes on a VM that was suspended will see time
    jump. Any such jump invalidates in-flight latency budgets and is recorded
    as an operational event rather than silently absorbed.
    """

    def __init__(self) -> None:
        self._last_wall = wall_ns()
        self._last_mono = mono_ns()
        self.regressions: int = 0
        self.max_regression_ms: float = 0.0

    def check(self) -> Optional[float]:
        w, m = wall_ns(), mono_ns()
        d_wall = w - self._last_wall
        d_mono = m - self._last_mono
        self._last_wall, self._last_mono = w, m
        # Tolerate 50 ms of jitter between the two sources before crying wolf.
        divergence_ns = d_wall - d_mono
        if divergence_ns < -50 * NS_PER_MS:
            self.regressions += 1
            reg_ms = abs(divergence_ns) / NS_PER_MS
            self.max_regression_ms = max(self.max_regression_ms, reg_ms)
            return reg_ms
        return None
