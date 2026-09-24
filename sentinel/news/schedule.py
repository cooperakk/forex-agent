"""Where calendar events come from.

``calendar.py`` is a store. This module is the ingestion side of it: recurrence
rules, one working offline source built from a bundled pattern file, and a
documented hook for a live feed.

The design point that matters is the honesty of ``certainty``:

``confirmed``
    An exact date and time from a provider. Only :class:`LiveCalendarSource`
    can produce this. Drives blackout windows.

``scheduled_pattern``
    The release DAY follows a published rule -- payrolls on the first Friday,
    ISM on the first business day -- and the time is the agency's standing
    release time. Drives blackout windows, because the day is right.

``approximate``
    The day is a typical-case guess inside a window. **Does not drive a
    blackout.** A guessed date that blocks trading on the wrong day costs
    opportunity for nothing, and -- far worse -- clears the real day, so the
    agent trades straight into the release believing it is protected. These
    surface as advisories: "a US CPI is expected this week, confirm the date".

That distinction is the difference between a calendar that is useful offline
and one that is actively dangerous offline.

**No live source ships enabled.** The deployment may have no outbound network
at all, and a calendar module that fails on import because a subscription is
missing would simply be switched off. :class:`LiveCalendarSource` takes an
injected fetcher, so wiring a provider is a config change and a function, not a
fork of this file.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..core.tzrules import local_to_utc_ns

BUNDLED_PATH = Path(__file__).with_name("data") / "recurring_releases.json"

CERTAINTY_LEVELS = ("approximate", "scheduled_pattern", "confirmed")
# Only these two put a currency into a hard blackout. See the module docstring.
BLACKOUT_CERTAINTIES = ("scheduled_pattern", "confirmed")


@dataclass
class ReleaseSeries:
    """One recurring release, as a rule rather than a list of dates."""

    series_id: str
    name: str
    country: str
    currency: str
    zone: str
    local_time: str                     # "HH:MM" in ``zone``
    recurrence: dict[str, Any]
    curated_tier: str = "medium"
    certainty: str = "approximate"
    unit: str | None = None
    period_lag_months: int = 0
    note: str = ""

    def __post_init__(self) -> None:
        if self.certainty not in CERTAINTY_LEVELS:
            raise ValueError(f"certainty must be one of {CERTAINTY_LEVELS}")
        hh, _, mm = self.local_time.partition(":")
        self._hour, self._minute = int(hh), int(mm or 0)

    def period_for(self, occurrence: dt.date) -> str:
        """The reference period a release describes, not the date it lands on.

        A payroll print on 2026-03-06 is the *February* report. Keying the
        consensus archive on the release date instead would make every
        revision look like a different event.
        """
        y, m = occurrence.year, occurrence.month
        lag = self.period_lag_months
        m -= lag
        while m <= 0:
            m += 12
            y -= 1
        return f"{y:04d}-{m:02d}"


# --------------------------------------------------------------------------- #
# Recurrence expansion
# --------------------------------------------------------------------------- #


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date | None:
    first = dt.date(year, month, 1)
    if n > 0:
        offset = (weekday - first.weekday()) % 7
        day = 1 + offset + (n - 1) * 7
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None
    nxt = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    last = nxt - dt.timedelta(days=1)
    last -= dt.timedelta(days=(last.weekday() - weekday) % 7)
    return last + dt.timedelta(weeks=n + 1)


def _nth_business_day(year: int, month: int, n: int) -> dt.date | None:
    """The n-th weekday of a month. Public holidays are NOT modelled.

    A US release scheduled for the first business day slips when that day is a
    federal holiday, and this function does not know that. The resulting event
    is therefore no better than ``scheduled_pattern``: right about the week,
    occasionally a day early. It is never promoted to ``confirmed``.
    """
    day = dt.date(year, month, 1)
    seen = 0
    while day.month == month:
        if day.weekday() < 5:
            seen += 1
            if seen == n:
                return day
        day += dt.timedelta(days=1)
    return None


def _clamp_to_weekday(day: dt.date) -> dt.date:
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def _month_iter(start: dt.date, end: dt.date) -> Iterable[tuple]:
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1


def occurrences(series: ReleaseSeries, start: dt.date, end: dt.date) -> list[dt.date]:
    """Local release dates of ``series`` in ``[start, end]``."""
    rec = series.recurrence
    kind = rec.get("kind")
    out: list[dt.date] = []

    if kind == "nth_weekday":
        every = int(rec.get("every_months", 1))
        anchor_month = int(rec.get("anchor_month", 1))
        for y, m in _month_iter(start, end):
            if (m - anchor_month) % every != 0:
                continue
            day = _nth_weekday(y, m, int(rec["weekday"]), int(rec["nth"]))
            if day:
                out.append(day)
    elif kind == "nth_business_day":
        every = int(rec.get("every_months", 1))
        anchor_month = int(rec.get("anchor_month", 1))
        for y, m in _month_iter(start, end):
            if (m - anchor_month) % every != 0:
                continue
            day = _nth_business_day(y, m, int(rec["nth"]))
            if day:
                out.append(day)
    elif kind == "day_window":
        every = int(rec.get("every_months", 1))
        anchor_month = int(rec.get("anchor_month", 1))
        typical = int(rec.get("typical_day", rec.get("earliest_day", 15)))
        for y, m in _month_iter(start, end):
            if (m - anchor_month) % every != 0:
                continue
            last_day = ((dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1))
                        - dt.timedelta(days=1)).day
            day = dt.date(y, m, min(typical, last_day))
            if rec.get("weekdays_only", True):
                day = _clamp_to_weekday(day)
            out.append(day)
    elif kind == "explicit_dates":
        for iso in rec.get("dates", []):
            out.append(dt.date.fromisoformat(iso))
    elif kind == "every_n_weeks":
        anchor = dt.date.fromisoformat(rec["anchor"])
        weeks = int(rec.get("weeks", 6))
        # Walk backwards to the first occurrence at or before ``start`` so the
        # phase of the cadence does not depend on where the caller began.
        cursor = anchor
        while cursor > start:
            cursor -= dt.timedelta(weeks=weeks)
        while cursor <= end:
            if cursor >= start:
                out.append(cursor)
            cursor += dt.timedelta(weeks=weeks)
    else:
        raise ValueError(f"unknown recurrence kind {kind!r} for {series.series_id}")

    return [d for d in out if start <= d <= end]


def expand(series: ReleaseSeries, start_ns: int, end_ns: int) -> list[dict[str, Any]]:
    """Concrete event dictionaries for ``series`` in a UTC nanosecond window.

    The local release time is converted through ``core.tzrules``, so a release
    tied to New York moves by an hour in UTC across the US DST switch and one
    tied to Frankfurt moves on the European one -- three weeks apart, twice a
    year. Storing a fixed UTC hour instead is the classic version of this bug
    and it is invisible until the blackout misses a payroll print.
    """
    start = dt.datetime.fromtimestamp(start_ns / 1e9, tz=dt.UTC).date()
    end = dt.datetime.fromtimestamp(end_ns / 1e9, tz=dt.UTC).date()
    # Widen by a day on each side: a local release time can land on the other
    # side of midnight UTC.
    out: list[dict[str, Any]] = []
    for day in occurrences(series, start - dt.timedelta(days=1), end + dt.timedelta(days=1)):
        local = dt.datetime(day.year, day.month, day.day, series._hour, series._minute)
        event_ns = local_to_utc_ns(local, series.zone)
        if not (start_ns <= event_ns <= end_ns):
            continue
        period = series.period_for(day)
        out.append({
            "event_id": f"{series.series_id}:{period}:{day:%Y%m%d}",
            "series_id": series.series_id,
            "event_ns": event_ns,
            "country": series.country,
            "currency": series.currency,
            "name": series.name,
            "curated_impact": series.curated_tier,
            "period": period,
            "unit": series.unit,
            "zone": series.zone,
            "local_time": series.local_time,
            "certainty": series.certainty,
            "source": "bundled_pattern",
        })
    return out


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


class CalendarSource(Protocol):
    """Anything that can produce calendar events for a window.

    Implement this to wire a provider. The contract is deliberately tiny:

        def fetch(self, start_ns: int, end_ns: int) -> List[dict]

    Each dict must carry at least ``event_ns`` (UTC nanoseconds), ``currency``,
    ``name`` and ``certainty``. Supplying ``series_id`` and ``period`` is what
    lets the store recognise a rescheduled event as the SAME event rather than
    opening a second blackout window beside the first.
    """

    name: str

    def fetch(self, start_ns: int, end_ns: int) -> list[dict[str, Any]]:
        ...


def load_bundled_series(path: Path | None = None) -> list[ReleaseSeries]:
    raw = json.loads(Path(path or BUNDLED_PATH).read_text(encoding="utf-8"))
    return [ReleaseSeries(**{k: v for k, v in s.items()}) for s in raw["series"]]


class RecurringScheduleSource:
    """The offline source. Works with no network and no subscription.

    What it is good for: exercising the whole path -- blackout, tiering,
    surprise arithmetic, the policy's size clamp -- and being roughly right
    about which WEEK a release lands in.

    What it is not: a substitute for a feed. It knows no public holidays, no
    schedule changes, no unscheduled statements, and no actual or forecast
    values. Every event it emits is ``scheduled_pattern`` or ``approximate``,
    never ``confirmed``.
    """

    name = "bundled_pattern"

    def __init__(self, series: Sequence[ReleaseSeries] | None = None) -> None:
        self.series = list(series) if series is not None else load_bundled_series()

    def fetch(self, start_ns: int, end_ns: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for s in self.series:
            out.extend(expand(s, start_ns, end_ns))
        out.sort(key=lambda e: e["event_ns"])
        return out


class LiveCalendarSource:
    """The documented hook for a real provider.

    ``fetcher(start_ns, end_ns) -> Iterable[dict]`` is injected, so this class
    never imports a network library and never runs unless an operator has
    supplied one. A deployment with no outbound access simply does not
    construct it, and the bundled source keeps working.

    A provider's rows are mapped by ``normalise``; override or pass a mapper.
    The one field worth insisting on is ``series_id``: without a stable series
    key, a provider that reschedules an event by publishing a new row with a
    new id produces a second blackout window overlapping the first, and the
    agent sits out twice as long for one release.
    """

    def __init__(self, fetcher: Callable[[int, int], Iterable[dict[str, Any]]], *,
                 name: str = "live_feed",
                 mapper: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 trust_certainty: bool = True) -> None:
        self.name = name
        self._fetcher = fetcher
        self._mapper = mapper or (lambda row: row)
        self.trust_certainty = trust_certainty

    def fetch(self, start_ns: int, end_ns: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self._fetcher(start_ns, end_ns):
            mapped = dict(self._mapper(row))
            mapped.setdefault("source", self.name)
            if not self.trust_certainty:
                mapped["certainty"] = "scheduled_pattern"
            else:
                mapped.setdefault("certainty", "confirmed")
            out.append(mapped)
        return out


@dataclass
class IngestReport:
    source: str
    inserted: int = 0
    rescheduled: int = 0
    unchanged: int = 0
    updated: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"source": self.source, "inserted": self.inserted,
                "rescheduled": self.rescheduled, "unchanged": self.unchanged,
                "updated": self.updated, "rejected": self.rejected,
                "errors": self.errors[:10], "advisories": self.advisories[:20]}
