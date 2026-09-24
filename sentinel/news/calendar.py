"""Economic calendar: scheduling, deduplication, tiering and surprise.

Four things live here, and each exists because of a specific way the naive
version is wrong.

**1. A versioned consensus archive.** Consensus forecasts are *revised*, and
most providers serve only the latest revision. Backtesting a surprise as
``actual - consensus`` against a consensus revised after the release is
look-ahead of the purest kind, and it looks completely normal in the code. So
the archive is append-only and versioned from the moment collection starts, and
a surprise computed against a consensus recorded *after* the cutoff is refused,
not silently returned.

**2. Release times are local, not UTC.** Payrolls is 08:30 in New York and the
ECB decision is 14:15 in Frankfurt; both map to a UTC instant that moves by an
hour twice a year, on dates that are three weeks apart. A stored UTC hour is
right for most of the year and quietly an hour out for the rest -- either
blacking out an empty hour and trading through the release, or the reverse.
Every event therefore carries its ``zone`` and ``local_time``, and
``reschedule_for_dst`` recomputes the UTC instant from them.

**3. One event is one event.** A release whose time moves must update the
existing row, not create a second one. Two rows means two overlapping blackout
windows for one release, so the agent sits out twice as long and the operator
sees a calendar that disagrees with the provider's. Events are keyed on
``(series_id, period)`` -- the *reference period*, not the release date -- and a
time change is recorded as a revision against the same ``event_id``.

**4. Four distinct timestamps**, because they are four different facts and
conflating them is the other classic error::

    event_ns     -- when the release is scheduled
    published_ns -- when the source published it
    received_ns  -- when we received it
    usable_ns    -- when a decision could first have used it
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..core.clock import wall_ns
from ..core.tzrules import local_to_utc_ns
from .schedule import (
    BLACKOUT_CERTAINTIES,
    CERTAINTY_LEVELS,
    CalendarSource,
    IngestReport,
    RecurringScheduleSource,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    event_ns        INTEGER NOT NULL,
    country         TEXT NOT NULL,
    currency        TEXT NOT NULL,
    name            TEXT NOT NULL,
    impact          TEXT NOT NULL,
    period          TEXT,
    unit            TEXT,
    source          TEXT NOT NULL,
    series_id       TEXT,
    zone            TEXT NOT NULL DEFAULT 'UTC',
    local_time      TEXT NOT NULL DEFAULT '',
    certainty       TEXT NOT NULL DEFAULT 'confirmed',
    curated_impact  TEXT NOT NULL DEFAULT 'medium',
    measured_impact TEXT,
    forecast        REAL,
    previous        REAL,
    revision_count  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'scheduled',
    first_seen_ns   INTEGER NOT NULL DEFAULT 0,
    updated_ns      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_ns);
-- The natural key. A provider that republishes a moved event under a new id
-- still lands on the same row, so one release is one blackout window.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_series
    ON events(series_id, period) WHERE series_id IS NOT NULL AND period IS NOT NULL;
CREATE TABLE IF NOT EXISTS event_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    recorded_ns INTEGER NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    source      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_event ON event_revisions(event_id, recorded_ns);
CREATE TABLE IF NOT EXISTS consensus_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    recorded_ns INTEGER NOT NULL,
    consensus   REAL,
    previous    REAL,
    source      TEXT NOT NULL,
    UNIQUE(event_id, recorded_ns)
);
CREATE INDEX IF NOT EXISTS idx_consensus_event ON consensus_versions(event_id, recorded_ns);
CREATE TABLE IF NOT EXISTS actuals (
    event_id       TEXT PRIMARY KEY,
    actual         REAL,
    revised_prev   REAL,
    previous_as_first_published REAL,
    published_ns   INTEGER NOT NULL,
    received_ns    INTEGER NOT NULL,
    source         TEXT NOT NULL,
    is_revision    INTEGER NOT NULL DEFAULT 0
);
-- Realised volatility observed around a release, used to TIER event types by
-- what they actually did rather than by whether their name is on a list.
CREATE TABLE IF NOT EXISTS event_volatility (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id    TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    instrument   TEXT NOT NULL,
    window_move_pips   REAL NOT NULL,
    baseline_move_pips REAL NOT NULL,
    observed_ns  INTEGER NOT NULL,
    UNIQUE(series_id, event_id, instrument)
);
CREATE INDEX IF NOT EXISTS idx_vol_series ON event_volatility(series_id, observed_ns);
"""

IMPACT_LEVELS = ("low", "medium", "high")


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns an older database is missing.

    An existing var/calendar.db predates the series key, the zone and the
    tiering columns. Dropping and recreating would throw away the consensus
    archive, which is the one table that cannot be rebuilt from anywhere --
    it is a record of what was believed at a point in time, and that is gone
    the moment it is deleted.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    if not have:
        return
    additions = {
        "series_id": "TEXT", "zone": "TEXT NOT NULL DEFAULT 'UTC'",
        "local_time": "TEXT NOT NULL DEFAULT ''",
        "certainty": "TEXT NOT NULL DEFAULT 'confirmed'",
        "curated_impact": "TEXT NOT NULL DEFAULT 'medium'",
        "measured_impact": "TEXT", "forecast": "REAL", "previous": "REAL",
        "revision_count": "INTEGER NOT NULL DEFAULT 0",
        "status": "TEXT NOT NULL DEFAULT 'scheduled'",
        "first_seen_ns": "INTEGER NOT NULL DEFAULT 0",
        "updated_ns": "INTEGER NOT NULL DEFAULT 0",
    }
    for col, decl in additions.items():
        if col not in have:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(actuals)").fetchall()}
    if acols:
        for col, decl in (("previous_as_first_published", "REAL"),
                          ("is_revision", "INTEGER NOT NULL DEFAULT 0")):
            if col not in acols:
                conn.execute(f"ALTER TABLE actuals ADD COLUMN {col} {decl}")


@dataclass
class CalendarEvent:
    """One scheduled release.

    ``impact`` is the EFFECTIVE tier used for gating. ``curated_impact`` is the
    judgement it falls back to and ``measured_impact`` is what the realised
    volatility says, when there is enough of it. Keeping all three means the
    dashboard can show which one is speaking.
    """

    event_id: str
    event_ns: int
    country: str
    currency: str
    name: str
    impact: str = "medium"
    period: str | None = None
    unit: str | None = None
    source: str = "manual"
    series_id: str | None = None
    zone: str = "UTC"
    local_time: str = ""
    certainty: str = "confirmed"
    curated_impact: str = "medium"
    measured_impact: str | None = None
    forecast: float | None = None
    previous: float | None = None
    revision_count: int = 0
    status: str = "scheduled"
    first_seen_ns: int = 0
    updated_ns: int = 0

    def __post_init__(self) -> None:
        if self.impact not in IMPACT_LEVELS:
            raise ValueError(f"impact must be one of {IMPACT_LEVELS}")
        if self.curated_impact not in IMPACT_LEVELS:
            raise ValueError(f"curated_impact must be one of {IMPACT_LEVELS}")
        if self.certainty not in CERTAINTY_LEVELS:
            raise ValueError(f"certainty must be one of {CERTAINTY_LEVELS}")

    @property
    def blocks_trading(self) -> bool:
        """Whether this event is certain enough to justify a hard blackout.

        An ``approximate`` date is a guess. Blocking on a guess costs
        opportunity for nothing AND -- the part that actually hurts -- clears
        the real release day, so the agent trades into the print believing it
        is protected.
        """
        return self.certainty in BLACKOUT_CERTAINTIES

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["blocks_trading"] = self.blocks_trading
        return d


@dataclass
class Surprise:
    event_id: str
    actual: float
    consensus: float
    surprise: float
    standardised: float | None
    consensus_recorded_ns: int
    published_ns: int
    usable_ns: int
    valid: bool
    reason: str = ""
    dispersion: float | None = None
    dispersion_n: int = 0
    dispersion_method: str = ""
    series_id: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class UpsertResult:
    event_id: str
    action: str                  # inserted | rescheduled | updated | unchanged
    previous_event_ns: int | None = None


class EconomicCalendar:
    def __init__(self, path: str | Path = "var/calendar.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            _migrate(self._conn)
            self._conn.commit()

    # -- ingestion ---------------------------------------------------------- #

    def add_event(self, event: CalendarEvent) -> UpsertResult:
        """Insert or reconcile one event. Never creates a duplicate window."""
        now = wall_ns()
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE event_id=?",
                                     (event.event_id,)).fetchone()
            if row is None and event.series_id and event.period:
                row = self._conn.execute(
                    "SELECT * FROM events WHERE series_id=? AND period=?",
                    (event.series_id, event.period)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO events (event_id, event_ns, country, currency, name, "
                    "impact, period, unit, source, series_id, zone, local_time, certainty, "
                    "curated_impact, measured_impact, forecast, previous, revision_count, "
                    "status, first_seen_ns, updated_ns) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event.event_id, event.event_ns, event.country, event.currency,
                     event.name, event.impact, event.period, event.unit, event.source,
                     event.series_id, event.zone, event.local_time, event.certainty,
                     event.curated_impact, event.measured_impact, event.forecast,
                     event.previous, 0, event.status, now, now))
                self._conn.commit()
                return UpsertResult(event.event_id, "inserted")

            # The row that already exists wins the identity argument: keeping
            # its event_id is what stops a rescheduled release from opening a
            # second blackout window beside the first.
            existing_id = row["event_id"]
            action = "unchanged"
            old_ns = int(row["event_ns"])
            if old_ns != event.event_ns:
                self._record_revision(existing_id, "event_ns", old_ns, event.event_ns,
                                      event.source, now)
                action = "rescheduled"
            for fieldname, new in (("certainty", event.certainty),
                                   ("forecast", event.forecast),
                                   ("previous", event.previous),
                                   ("curated_impact", event.curated_impact),
                                   ("name", event.name)):
                old = row[fieldname] if fieldname in row.keys() else None
                if new is not None and old != new:
                    self._record_revision(existing_id, fieldname, old, new,
                                          event.source, now)
                    if action == "unchanged":
                        action = "updated"
            # A lower-certainty restatement never downgrades a confirmed event:
            # the bundled pattern source runs on every cycle, and letting it
            # overwrite a feed's confirmed time with a guessed one would undo
            # the ingestion silently.
            certainty = max((row["certainty"], event.certainty),
                            key=lambda c: CERTAINTY_LEVELS.index(c))
            keep_time = (certainty == row["certainty"]
                         and CERTAINTY_LEVELS.index(event.certainty)
                         < CERTAINTY_LEVELS.index(row["certainty"]))
            new_ns = old_ns if keep_time else event.event_ns
            if keep_time and action == "rescheduled":
                action = "unchanged"
            self._conn.execute(
                "UPDATE events SET event_ns=?, name=?, currency=?, country=?, "
                "curated_impact=?, impact=?, certainty=?, forecast=COALESCE(?,forecast), "
                "previous=COALESCE(?,previous), zone=?, local_time=?, source=?, "
                "revision_count=revision_count + ?, updated_ns=? WHERE event_id=?",
                (new_ns, event.name, event.currency, event.country, event.curated_impact,
                 row["impact"] if row["measured_impact"] else event.impact, certainty,
                 event.forecast, event.previous, event.zone or row["zone"],
                 event.local_time or row["local_time"], event.source,
                 1 if action == "rescheduled" else 0, now, existing_id))
            self._conn.commit()
            return UpsertResult(existing_id, action,
                                previous_event_ns=old_ns if action == "rescheduled" else None)

    def _record_revision(self, event_id: str, fieldname: str, old: Any, new: Any,
                         source: str, now: int) -> None:
        self._conn.execute(
            "INSERT INTO event_revisions (event_id, recorded_ns, field, old_value, "
            "new_value, source) VALUES (?,?,?,?,?,?)",
            (event_id, now, fieldname,
             None if old is None else str(old), None if new is None else str(new), source))

    def ingest(self, source: CalendarSource, start_ns: int, end_ns: int) -> IngestReport:
        """Pull a window from a source and reconcile it into the store."""
        report = IngestReport(source=getattr(source, "name", source.__class__.__name__))
        try:
            rows = source.fetch(start_ns, end_ns)
        except Exception as exc:  # noqa: BLE001 - a dead feed is data, not a crash
            report.errors.append(f"fetch failed: {exc}")
            return report
        for raw in rows:
            try:
                event = self._event_from_row(raw)
            except Exception as exc:  # noqa: BLE001
                report.rejected += 1
                report.errors.append(f"{raw.get('name', '?')}: {exc}")
                continue
            result = self.add_event(event)
            setattr(report, result.action, getattr(report, result.action) + 1)
            if not event.blocks_trading and event.impact == "high":
                report.advisories.append(
                    f"{event.name} ({event.currency}) is expected around "
                    f"{dt.datetime.fromtimestamp(event.event_ns / 1e9, tz=dt.UTC):%Y-%m-%d}"
                    " but the date is a pattern estimate, not a confirmed release. It "
                    "will NOT create a blackout window until a feed confirms it.")
        return report

    @staticmethod
    def _event_from_row(raw: dict[str, Any]) -> CalendarEvent:
        curated = raw.get("curated_impact") or raw.get("impact") or "medium"
        return CalendarEvent(
            event_id=raw.get("event_id") or f"{raw.get('series_id', 'X')}:{raw['event_ns']}",
            event_ns=int(raw["event_ns"]), country=raw.get("country", ""),
            currency=raw["currency"], name=raw["name"],
            impact=raw.get("impact") or curated, curated_impact=curated,
            period=raw.get("period"), unit=raw.get("unit"),
            source=raw.get("source", "feed"), series_id=raw.get("series_id"),
            zone=raw.get("zone", "UTC"), local_time=raw.get("local_time", ""),
            certainty=raw.get("certainty", "confirmed"),
            forecast=raw.get("forecast"), previous=raw.get("previous"))

    def reschedule_for_dst(self, from_ns: int, to_ns: int) -> int:
        """Recompute UTC instants from the stored local time and zone.

        Runs after a DST boundary. An event ingested in February with a stored
        08:30 New York time is 13:30 UTC; the same release in April is 12:30,
        and nothing about the row changes except the derived instant. Without
        this, a calendar loaded once per quarter drifts an hour for six weeks
        every spring and autumn.
        """
        moved = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, event_ns, zone, local_time FROM events "
                "WHERE event_ns BETWEEN ? AND ? AND local_time != '' AND status='scheduled'",
                (from_ns, to_ns)).fetchall()
        for r in rows:
            try:
                utc = dt.datetime.fromtimestamp(r["event_ns"] / 1e9, tz=dt.UTC)
                hh, _, mm = r["local_time"].partition(":")
                from ..core.tzrules import utc_to_local
                local_day = utc_to_local(utc, r["zone"]).date()
                want = local_to_utc_ns(
                    dt.datetime(local_day.year, local_day.month, local_day.day,
                                int(hh), int(mm or 0)), r["zone"])
            except Exception:  # noqa: BLE001 - a malformed row is not worth a crash
                continue
            if want != int(r["event_ns"]):
                now = wall_ns()
                with self._lock:
                    self._record_revision(r["event_id"], "event_ns", r["event_ns"], want,
                                          "dst_recompute", now)
                    self._conn.execute(
                        "UPDATE events SET event_ns=?, updated_ns=? WHERE event_id=?",
                        (want, now, r["event_id"]))
                    self._conn.commit()
                moved += 1
        return moved

    def record_consensus(self, event_id: str, consensus: float | None,
                         previous: float | None, source: str,
                         recorded_ns: int | None = None) -> None:
        """Append a consensus observation. Never updates an earlier one."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO consensus_versions "
                "(event_id, recorded_ns, consensus, previous, source) VALUES (?,?,?,?,?)",
                (event_id, recorded_ns or wall_ns(), consensus, previous, source))
            self._conn.commit()

    def record_actual(self, event_id: str, actual: float | None,
                      revised_prev: float | None, published_ns: int,
                      source: str, received_ns: int | None = None,
                      previous_as_first_published: float | None = None) -> None:
        """Record the released value.

        ``revised_prev`` matters more than it looks. A payroll print of +150k
        against a +180k consensus is a miss; the same print alongside a -60k
        revision to the prior month is a much bigger one, and a surprise
        computed from the headline alone misses that entirely. Both numbers are
        stored so the revision can be measured rather than assumed away.
        """
        with self._lock:
            prior = self._conn.execute("SELECT actual FROM actuals WHERE event_id=?",
                                       (event_id,)).fetchone()
            is_revision = 1 if (prior is not None and prior["actual"] is not None
                                and actual is not None
                                and float(prior["actual"]) != float(actual)) else 0
            if is_revision:
                self._record_revision(event_id, "actual", prior["actual"], actual,
                                      source, wall_ns())
            self._conn.execute(
                "INSERT OR REPLACE INTO actuals "
                "(event_id, actual, revised_prev, previous_as_first_published, "
                "published_ns, received_ns, source, is_revision) VALUES (?,?,?,?,?,?,?,?)",
                (event_id, actual, revised_prev, previous_as_first_published,
                 published_ns, received_ns or wall_ns(), source, is_revision))
            self._conn.execute("UPDATE events SET status='released' WHERE event_id=?",
                               (event_id,))
            self._conn.commit()

    def record_event_volatility(self, series_id: str, event_id: str, instrument: str,
                                window_move_pips: float, baseline_move_pips: float,
                                observed_ns: int | None = None) -> None:
        """One observation of how far the market actually moved around a release."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO event_volatility (series_id, event_id, instrument, "
                "window_move_pips, baseline_move_pips, observed_ns) VALUES (?,?,?,?,?,?)",
                (series_id, event_id, instrument, float(window_move_pips),
                 float(baseline_move_pips), observed_ns or wall_ns()))
            self._conn.commit()

    def volatility_observations(self, series_id: str,
                                before_ns: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM event_volatility WHERE series_id=?"
        args: list[Any] = [series_id]
        if before_ns is not None:
            q += " AND observed_ns < ?"
            args.append(before_ns)
        q += " ORDER BY observed_ns"
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def set_measured_impact(self, series_id: str, tier: str | None) -> int:
        """Write a measured tier back onto every event of a series."""
        if tier is not None and tier not in IMPACT_LEVELS:
            raise ValueError(f"tier must be one of {IMPACT_LEVELS}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE events SET measured_impact=?, impact=COALESCE(?, curated_impact), "
                "updated_ns=? WHERE series_id=?",
                (tier, tier, wall_ns(), series_id))
            self._conn.commit()
            return cur.rowcount

    def series_ids(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute(
                "SELECT DISTINCT series_id FROM events WHERE series_id IS NOT NULL").fetchall()]

    # -- queries ------------------------------------------------------------- #

    def _row_to_event(self, r: sqlite3.Row) -> CalendarEvent:
        keys = set(r.keys())
        allowed = set(CalendarEvent.__dataclass_fields__)
        return CalendarEvent(**{k: r[k] for k in keys & allowed})

    def get(self, event_id: str) -> CalendarEvent | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE event_id=?",
                                     (event_id,)).fetchone()
        return self._row_to_event(row) if row else None

    def upcoming(self, now_ns: int, horizon_sec: int = 86400,
                 min_impact: str = "medium") -> list[CalendarEvent]:
        floor = IMPACT_LEVELS.index(min_impact)
        allowed = IMPACT_LEVELS[floor:]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE event_ns BETWEEN ? AND ? "
                f"AND impact IN ({','.join('?' * len(allowed))}) ORDER BY event_ns",
                (now_ns, now_ns + horizon_sec * 1_000_000_000, *allowed)).fetchall()
        return [self._row_to_event(r) for r in rows]

    def blackout(self, now_ns: int, currencies: Sequence[str], *,
                 before_min: int, after_min: int,
                 min_impact: str = "high",
                 require_certain: bool = True) -> dict[str, str]:
        """Currencies currently inside a scheduled-event window.

        ``require_certain`` is the offline-safety switch. With it on -- the
        default -- only confirmed and structurally-scheduled events block. A
        pattern-estimated date is an advisory, surfaced by ``advisories``, not a
        gate: blocking on a guess sits out the wrong day and clears the right
        one, which is worse than having no calendar at all.
        """
        floor = IMPACT_LEVELS.index(min_impact)
        allowed = IMPACT_LEVELS[floor:]
        lo = now_ns - after_min * 60 * 1_000_000_000
        hi = now_ns + before_min * 60 * 1_000_000_000
        q = (f"SELECT * FROM events WHERE event_ns BETWEEN ? AND ? "
             f"AND impact IN ({','.join('?' * len(allowed))})")
        args: list[Any] = [lo, hi, *allowed]
        if require_certain:
            q += f" AND certainty IN ({','.join('?' * len(BLACKOUT_CERTAINTIES))})"
            args.extend(BLACKOUT_CERTAINTIES)
        q += " ORDER BY event_ns"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        out: dict[str, str] = {}
        nearest: dict[str, int] = {}
        wanted = set(currencies)
        for r in rows:
            ccy = r["currency"]
            if ccy not in wanted:
                continue
            # The NEAREST event wins the label, not the earliest. Rows come back
            # ordered by scheduled time and the window straddles now, so taking
            # the first row labels an imminent payroll print with whatever
            # happened forty minutes ago -- the block is still correct, but the
            # operator is told the wrong reason for it.
            distance = abs(int(r["event_ns"]) - now_ns)
            if ccy in nearest and nearest[ccy] <= distance:
                continue
            nearest[ccy] = distance
            minutes = (r["event_ns"] - now_ns) / 6e10
            when = f"in {minutes:.0f}m" if minutes >= 0 else f"{-minutes:.0f}m ago"
            out[ccy] = f"{r['name']} ({when})"
        return out

    def advisories(self, now_ns: int, currencies: Sequence[str], *,
                   horizon_sec: int = 7 * 86400,
                   min_impact: str = "high") -> list[dict[str, Any]]:
        """High-impact releases expected soon whose DATE is not confirmed.

        These are the ones an operator has to go and confirm. They are reported
        rather than enforced, because the system cannot honestly claim to know
        when they are.
        """
        floor = IMPACT_LEVELS.index(min_impact)
        allowed = IMPACT_LEVELS[floor:]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE event_ns BETWEEN ? AND ? "
                f"AND impact IN ({','.join('?' * len(allowed))}) "
                f"AND certainty='approximate' ORDER BY event_ns",
                (now_ns, now_ns + horizon_sec * 1_000_000_000, *allowed)).fetchall()
        wanted = set(currencies)
        return [{"event_id": r["event_id"], "name": r["name"], "currency": r["currency"],
                 "event_ns": int(r["event_ns"]), "certainty": r["certainty"],
                 "note": "date is a pattern estimate; confirm it before relying on the "
                         "blackout window"}
                for r in rows if r["currency"] in wanted]

    def instrument_blackout(self, now_ns: int, instruments: Sequence[str], *,
                            before_min: int, after_min: int,
                            min_impact: str = "high",
                            require_certain: bool = True) -> dict[str, str]:
        currencies = set()
        for sym in instruments:
            parts = sym.replace("/", "_").split("_")
            currencies.update(p for p in parts if len(p) == 3)
        by_ccy = self.blackout(now_ns, sorted(currencies), before_min=before_min,
                               after_min=after_min, min_impact=min_impact,
                               require_certain=require_certain)
        out: dict[str, str] = {}
        for sym in instruments:
            for ccy, label in by_ccy.items():
                if ccy in sym:
                    out[sym] = f"{ccy}: {label}"
                    break
        return out

    def consensus_as_of(self, event_id: str, as_of_ns: int) -> sqlite3.Row | None:
        """The consensus as it stood at ``as_of_ns``. The only honest version."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM consensus_versions WHERE event_id=? AND recorded_ns<=? "
                "ORDER BY recorded_ns DESC LIMIT 1", (event_id, as_of_ns)).fetchone()

    # -- surprise ------------------------------------------------------------ #

    def historical_surprises(self, series_id: str, before_ns: int,
                             exclude_event_id: str | None = None
                             ) -> list[tuple[int, float]]:
        """Past surprises of one series, strictly causally.

        Three filters, all load-bearing:

        * the release must have been PUBLISHED before ``before_ns`` -- a value
          that had not printed yet cannot be in a scale used to judge today;
        * the consensus must have been RECORDED before that release's own
          scheduled time -- otherwise the historical surprise is itself
          contaminated, and a scale built from contaminated surprises is a
          contaminated scale;
        * the event being judged is excluded from its own denominator.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.event_id AS eid, e.event_ns AS ens, a.actual AS actual, "
                "a.published_ns AS pns FROM events e JOIN actuals a ON a.event_id=e.event_id "
                "WHERE e.series_id=? AND a.published_ns < ? AND a.actual IS NOT NULL "
                "ORDER BY a.published_ns", (series_id, before_ns)).fetchall()
        out: list[tuple[int, float]] = []
        for r in rows:
            if exclude_event_id and r["eid"] == exclude_event_id:
                continue
            cons = self.consensus_as_of(r["eid"], int(r["ens"]))
            if cons is None or cons["consensus"] is None:
                continue
            out.append((int(r["pns"]), float(r["actual"]) - float(cons["consensus"])))
        return out

    def surprise_dispersion(self, series_id: str, before_ns: int, *,
                            exclude_event_id: str | None = None,
                            min_obs: int = 8) -> dict[str, Any]:
        """The scale of this series' own surprises, from history only.

        Normalising by a robust scale rather than a standard deviation is not a
        preference. Surprise distributions are fat-tailed and a single crisis
        print inflates the SD enough to make every subsequent surprise look
        small -- which is precisely backwards, because the fat tail is the part
        that moves the market. The median absolute deviation, scaled by 1.4826
        to match the SD of a Gaussian, does not have that failure.

        Returns ``n`` alongside the scale. A z-score from six observations is
        not a z-score, and the caller is told so rather than handed a number
        that looks the same as one from six hundred.
        """
        hist = self.historical_surprises(series_id, before_ns,
                                         exclude_event_id=exclude_event_id)
        values = np.array([v for _, v in hist], dtype=float)
        if values.size < min_obs:
            return {"scale": None, "n": int(values.size), "method": "insufficient",
                    "reason": (f"{values.size} prior surprises for {series_id}; at least "
                               f"{min_obs} are needed before a standardised surprise means "
                               "anything")}
        med = float(np.median(values))
        mad = float(np.median(np.abs(values - med)))
        robust = 1.4826 * mad
        if robust <= 0:
            sd = float(values.std(ddof=1))
            if sd <= 0:
                return {"scale": None, "n": int(values.size), "method": "degenerate",
                        "reason": "every prior surprise was identical; there is no scale"}
            return {"scale": sd, "n": int(values.size), "method": "sd",
                    "centre": med,
                    "reason": "MAD was zero (many identical prints); fell back to the SD"}
        return {"scale": robust, "n": int(values.size), "method": "mad", "centre": med,
                "reason": ""}

    def surprise(self, event_id: str, *, decision_ns: int | None = None,
                 history_sd: float | None = None,
                 min_history: int = 8) -> Surprise | None:
        """actual - consensus, standardised by this series' own history.

        ``decision_ns`` is the cutoff. Everything used here -- the consensus
        version, the dispersion scale, the set of prior surprises -- is
        restricted to what existed before it. Passing a cutoff *after* the
        release is legitimate (that is the post-release feature), but the
        consensus still comes from before the scheduled time, because a
        consensus revised in the minutes after a print is a description of the
        print.
        """
        with self._lock:
            actual = self._conn.execute("SELECT * FROM actuals WHERE event_id=?",
                                        (event_id,)).fetchone()
            event = self._conn.execute("SELECT * FROM events WHERE event_id=?",
                                       (event_id,)).fetchone()
        if actual is None or event is None:
            return None
        series_id = event["series_id"]
        # The consensus cutoff is ALWAYS the scheduled time, never the caller's
        # decision time: a consensus recorded after the release describes it.
        cons = self.consensus_as_of(event_id, int(event["event_ns"]))
        cutoff = decision_ns if decision_ns is not None else int(event["event_ns"])
        if cons is None or cons["consensus"] is None:
            return Surprise(event_id, float(actual["actual"] or 0.0), 0.0, 0.0, None,
                            0, int(actual["published_ns"]), int(actual["received_ns"]),
                            valid=False, series_id=series_id,
                            reason="no consensus was recorded before the release; a surprise "
                                   "computed from a later revision would be look-ahead")
        surprise = float(actual["actual"] or 0.0) - float(cons["consensus"])

        standardised: float | None = None
        disp: dict[str, Any] = {"scale": None, "n": 0, "method": "none", "reason": ""}
        if history_sd is not None and history_sd > 0:
            standardised = surprise / history_sd
            disp = {"scale": float(history_sd), "n": 0, "method": "caller_supplied",
                    "reason": "the caller supplied a scale; its causality is the "
                              "caller's responsibility"}
        elif series_id:
            # The scale is built from releases published before the cutoff and
            # never includes this one. Including the event being judged shrinks
            # its own z-score toward zero -- every surprise looks ordinary, and
            # the feature the strategy was built on is quietly dead.
            disp = self.surprise_dispersion(series_id, cutoff,
                                            exclude_event_id=event_id,
                                            min_obs=min_history)
            if disp["scale"]:
                standardised = surprise / float(disp["scale"])

        return Surprise(
            event_id=event_id, actual=float(actual["actual"] or 0.0),
            consensus=float(cons["consensus"]), surprise=surprise,
            standardised=standardised, consensus_recorded_ns=int(cons["recorded_ns"]),
            published_ns=int(actual["published_ns"]), usable_ns=int(actual["received_ns"]),
            valid=True, series_id=series_id, dispersion=disp.get("scale"),
            dispersion_n=int(disp.get("n", 0)), dispersion_method=str(disp.get("method")),
            reason=str(disp.get("reason", "")))

    def consensus_revisions(self, event_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT recorded_ns, consensus, previous, source FROM consensus_versions "
                "WHERE event_id=? ORDER BY recorded_ns", (event_id,)).fetchall()
        return [dict(r) for r in rows]

    def event_revisions(self, event_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT recorded_ns, field, old_value, new_value, source FROM "
                "event_revisions WHERE event_id=? ORDER BY recorded_ns", (event_id,)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def seed_reference_calendar(cal: EconomicCalendar, start_ns: int, weeks: int = 8) -> int:
    """Populate the calendar from the bundled recurring-release patterns.

    This is the offline path: no network, no subscription, and every event
    honestly labelled with how much the system actually knows about its date.
    Use ``cal.ingest(LiveCalendarSource(...))`` once a provider is wired.
    """
    end_ns = start_ns + weeks * 7 * 86400 * 1_000_000_000
    report = cal.ingest(RecurringScheduleSource(), start_ns, end_ns)
    return report.inserted + report.rescheduled + report.updated + report.unchanged
