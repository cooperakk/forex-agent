"""Operational health: connectivity, clock, data freshness.

The research brief treats connectivity as a first-class risk for a system
reaching an offshore venue from Iran, not as an edge case. So the link is
*measured*, and the measurement is a gate: the distribution of outage lengths
decides which strategy horizons are viable, independently of any financial
analysis. A system that is blind for eleven minutes at a time cannot run a
fifteen-minute trade.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from ..core.audit import AuditLog, EventType
from ..core.clock import ClockMonitor, MonotonicGuard, Stopwatch, wall_ns


@dataclass
class Outage:
    started_ns: int
    ended_ns: Optional[int] = None
    reason: str = ""

    @property
    def duration_sec(self) -> float:
        return ((self.ended_ns or wall_ns()) - self.started_ns) / 1e9


@dataclass
class HealthSnapshot:
    connected: bool
    uptime_pct: float
    offline_seconds: float
    median_latency_ms: Optional[float]
    p95_latency_ms: Optional[float]
    clock_skew_ms: Optional[float]
    clock_regressions: int
    outages_24h: int
    longest_outage_sec: float
    data_age_sec: Dict[str, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @staticmethod
    def _jsonable(value: Optional[float]) -> Optional[float]:
        """Round a float, turning a non-finite value into None.

        THE P0. `inf` means "this instrument has no quote at all", which the
        agent produces on the very first cycle of a fresh install -- the
        market store is empty and every configured instrument reports an
        infinite age. `json.dumps(allow_nan=False)`, which is what FastAPI
        uses, then raises ValueError on it, the middleware converts that to a
        blanket HTTP 500, and /api/status -- the dashboard's primary poll --
        is dead from the first cycle until every instrument happens to have a
        live quote.

        None is the honest encoding: the age is not a number, it is unknown.
        Substituting a large finite value would let a caller compare it and
        conclude something false.
        """
        if value is None:
            return None
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return round(v, 1)

    def to_dict(self) -> dict:
        return {
            "connected": self.connected, "uptime_pct": round(self.uptime_pct, 4),
            "offline_seconds": round(self.offline_seconds, 1),
            "median_latency_ms": self._jsonable(self.median_latency_ms),
            "p95_latency_ms": self._jsonable(self.p95_latency_ms),
            "clock_skew_ms": self._jsonable(self.clock_skew_ms),
            "clock_regressions": self.clock_regressions,
            "outages_24h": self.outages_24h,
            "longest_outage_sec": self._jsonable(self.longest_outage_sec),
            # None where an instrument has no quote at all -- see _jsonable.
            "data_age_sec": {k: self._jsonable(v)
                             for k, v in self.data_age_sec.items()},
            "warnings": self.warnings,
        }


class HealthMonitor:
    def __init__(self, audit: Optional[AuditLog] = None, *, window: int = 2000,
                 max_clock_skew_ms: float = 750.0) -> None:
        self.audit = audit
        self.max_clock_skew_ms = max_clock_skew_ms
        self.clock = ClockMonitor(window=512)
        self.monotonic = MonotonicGuard()
        self.latencies: Deque[float] = deque(maxlen=window)
        self.probes: Deque[tuple[int, bool]] = deque(maxlen=window)
        self.outages: List[Outage] = []
        self._current: Optional[Outage] = None
        self._started_ns = wall_ns()

    # -- probes -------------------------------------------------------------- #

    def record_probe(self, ok: bool, latency_ms: Optional[float] = None,
                     venue_ts_ns: Optional[int] = None, reason: str = "",
                     now_ns: Optional[int] = None) -> None:
        now = now_ns if now_ns is not None else wall_ns()
        self.probes.append((now, ok))
        if ok and latency_ms is not None:
            self.latencies.append(latency_ms)
        if ok and venue_ts_ns is not None:
            sample = self.clock.observe(venue_ts_ns,
                                        round_trip_ns=int((latency_ms or 0) * 1e6))
            if now_ns is not None:
                # Replace the wall-clock stamp with the injected clock so an
                # accelerated replay does not read as a multi-day clock skew.
                self.clock.samples[-1] = sample.__class__(
                    local_ns=now_ns, remote_ns=sample.remote_ns,
                    round_trip_ns=sample.round_trip_ns)
        regression = self.monotonic.check()
        if regression and self.audit:
            self.audit.append(EventType.CLOCK_ANOMALY, {
                "wall_clock_regression_ms": round(regression, 2),
                "note": "durations use the monotonic clock and are unaffected; event "
                        "ordering against the venue may be"})
        if ok:
            if self._current is not None:
                self._current.ended_ns = now
                self.outages.append(self._current)
                if self.audit:
                    self.audit.append(EventType.CONNECTIVITY, {
                        "state": "recovered",
                        "outage_sec": round(self._current.duration_sec, 1)})
                self._current = None
        else:
            if self._current is None:
                self._current = Outage(started_ns=now, reason=reason)
                if self.audit:
                    self.audit.append(EventType.CONNECTIVITY,
                                      {"state": "lost", "reason": reason})

    def probe(self, broker, now_ns: Optional[int] = None) -> bool:
        """Latency-measured liveness check against the venue."""
        sw = Stopwatch()
        try:
            account = broker.account()
        except Exception as exc:  # noqa: BLE001 - any failure is a failed probe
            self.record_probe(False, reason=f"{exc.__class__.__name__}: {exc}", now_ns=now_ns)
            return False
        self.record_probe(True, latency_ms=sw.elapsed_ms, venue_ts_ns=account.ts_ns,
                          now_ns=now_ns)
        return True

    # -- state ---------------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        return self._current is None

    @property
    def offline_seconds(self) -> float:
        return self._current.duration_sec if self._current else 0.0

    def uptime_pct(self, window_sec: float = 86400) -> float:
        cutoff = wall_ns() - int(window_sec * 1e9)
        recent = [(ts, ok) for ts, ok in self.probes if ts >= cutoff]
        if not recent:
            return 100.0
        return 100.0 * sum(1 for _, ok in recent if ok) / len(recent)

    def _pct(self, p: float) -> Optional[float]:
        if not self.latencies:
            return None
        xs = sorted(self.latencies)
        return xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))]

    def snapshot(self, data_age_sec: Optional[Dict[str, float]] = None) -> HealthSnapshot:
        warnings: List[str] = []
        uptime = self.uptime_pct()
        recent_outages = [o for o in self.outages
                          if o.started_ns >= wall_ns() - 86_400_000_000_000]
        longest = max([o.duration_sec for o in recent_outages] + [self.offline_seconds] + [0.0])
        skew = self.clock.median_skew_ms

        if uptime < 99.0:
            warnings.append(
                f"link uptime {uptime:.2f}% over 24h. Below 99% the short-horizon "
                "strategies are ruled out by infrastructure alone.")
        if longest > 300:
            warnings.append(
                f"longest recent outage {longest / 60:.1f} minutes: any strategy whose "
                "trade lives inside that window is untenable")
        if skew is not None and abs(skew) > self.max_clock_skew_ms:
            warnings.append(
                f"clock skew against the venue is {skew:.0f}ms: event ordering is unreliable")
        if self.monotonic.regressions:
            warnings.append(
                f"{self.monotonic.regressions} wall-clock regression(s) observed "
                f"(worst {self.monotonic.max_regression_ms:.0f}ms)")
        p95 = self._pct(0.95)
        if p95 and p95 > 1500:
            warnings.append(f"p95 venue latency {p95:.0f}ms: fills will differ materially "
                            "from decision prices")

        return HealthSnapshot(
            connected=self.connected, uptime_pct=uptime,
            offline_seconds=self.offline_seconds,
            median_latency_ms=self._pct(0.5), p95_latency_ms=p95,
            clock_skew_ms=skew, clock_regressions=self.monotonic.regressions,
            outages_24h=len(recent_outages), longest_outage_sec=longest,
            data_age_sec=data_age_sec or {}, warnings=warnings,
        )

    def outage_distribution(self) -> Dict[str, float]:
        """The number that decides which horizons are viable."""
        if not self.outages:
            return {"n": 0}
        ds = sorted(o.duration_sec for o in self.outages)
        def q(p: float) -> float:
            return ds[min(len(ds) - 1, int(p * (len(ds) - 1) + 0.5))]
        return {"n": len(ds), "median_sec": round(q(0.5), 1), "p90_sec": round(q(0.9), 1),
                "p99_sec": round(q(0.99), 1), "max_sec": round(ds[-1], 1),
                "total_sec": round(sum(ds), 1)}
