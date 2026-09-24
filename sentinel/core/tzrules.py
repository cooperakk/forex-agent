"""Release-time zones, DST-aware, with no tz database required.

Why this exists at all: an economic release is scheduled in a *local* wall
clock, never in UTC. Non-farm payrolls is 08:30 in New York, the ECB decision
is 14:15 in Frankfurt, and both of those map to a UTC instant that moves by an
hour twice a year. A calendar that stores a fixed UTC hour is correct for
roughly seven months and silently an hour wrong for the rest -- which puts the
blackout window either an hour early (so the agent is flat through nothing and
trading through the release) or an hour late (so it is trading straight into
it). Neither failure shows up in a backtest whose calendar was built the same
wrong way.

The US and European DST windows are also *different*. The US switches on the
second Sunday in March and the first Sunday in November; Europe switches on the
last Sunday in March and the last Sunday in October. For about three weeks a
year the New York/Frankfurt offset is five hours instead of six, so a calendar
that derives Frankfurt from a New York offset is wrong in exactly the weeks
containing an ECB meeting and a US CPI print.

These rules are implemented arithmetically rather than through ``zoneinfo``
because the deployment image is a slim container and ``tzdata`` is not
guaranteed to be installed; ``ZoneInfo("America/New_York")`` raises there, and
a calendar that raises on import is a calendar that gets disabled.

**Known limits, stated plainly:**

* The rules are the *current* ones. US DST has followed the 2007 Energy Policy
  Act dates since 2007; European Summer Time has followed Directive 2000/84/EC
  since 2002. Dates before those years are wrong, and any future change to
  either regime (the EU has repeatedly discussed abolishing the switch) makes
  them wrong going forward. ``zoneinfo``, when present, is the authority --
  ``verify_against_zoneinfo`` exists so a deployment that *does* have tzdata can
  assert the agreement in its own tests rather than trusting this file.
* Within the changeover hour itself the local wall clock is ambiguous (repeated)
  or non-existent (skipped). No scheduled economic release sits in that hour --
  02:00-03:00 local on a Sunday -- so the ambiguity is resolved toward standard
  time and noted rather than handled.
"""

from __future__ import annotations

import datetime as dt

# The four zones a scheduled FX release actually lives in. Anything else is an
# error at call time rather than a silent UTC assumption.
ZONES = ("America/New_York", "Europe/Frankfurt", "Europe/London", "Asia/Tokyo",
         "UTC")

# Standard-time offsets, and whether the zone observes summer time at all.
_BASE: dict[str, tuple[int, str]] = {
    "America/New_York": (-5, "us"),
    "Europe/Frankfurt": (1, "eu"),
    "Europe/London": (0, "eu"),
    "Asia/Tokyo": (9, "none"),      # Japan has had no DST since 1951.
    "UTC": (0, "none"),
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """The ``n``-th ``weekday`` of a month (Monday=0), 1-indexed."""
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return dt.date(year, month, 1 + offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    """The last ``weekday`` of a month."""
    if month == 12:
        nxt = dt.date(year + 1, 1, 1)
    else:
        nxt = dt.date(year, month + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _us_dst(moment: dt.datetime) -> bool:
    """US summer time: 2nd Sunday in March 02:00 local to 1st Sunday in Nov 02:00."""
    year = moment.year
    start = _nth_weekday(year, 3, 6, 2)
    end = _nth_weekday(year, 11, 6, 1)
    today = moment.date()
    if today < start or today > end:
        return False
    if today > start and today < end:
        return True
    # Changeover day: the switch is at 02:00 local standard / 02:00 local
    # daylight respectively. Resolve at the hour rather than treating the whole
    # day as one side, so a Sunday-evening Asian open is dated correctly.
    if today == start:
        return moment.hour >= 2
    return moment.hour < 2


def _eu_dst(moment: dt.datetime) -> bool:
    """European summer time: last Sunday in March to last Sunday in October,
    both at 01:00 UTC -- the whole Union switches at the same *instant*, not at
    the same local hour, which is why this test is against UTC and the US one is
    against local time."""
    year = moment.year
    start = _last_weekday(year, 3, 6)
    end = _last_weekday(year, 10, 6)
    today = moment.date()
    if today < start or today > end:
        return False
    if start < today < end:
        return True
    # Approximate within the changeover day using the hour of the supplied
    # moment. Callers pass local wall time; 01:00 UTC is 01:00 London / 02:00
    # Frankfurt, and no release is scheduled then.
    if today == start:
        return moment.hour >= 2
    return moment.hour < 2


def utc_offset_hours(zone: str, moment: dt.datetime) -> int:
    """UTC offset of ``zone`` at ``moment`` (interpreted as local wall time)."""
    try:
        base, regime = _BASE[zone]
    except KeyError:
        raise ValueError(
            f"unknown release zone {zone!r}; known zones are {ZONES}. A release "
            "time with no zone cannot be converted honestly, and defaulting to "
            "UTC would put the blackout window an hour out for half the year."
        ) from None
    if regime == "us":
        return base + (1 if _us_dst(moment) else 0)
    if regime == "eu":
        return base + (1 if _eu_dst(moment) else 0)
    return base


def local_to_utc(local: dt.datetime, zone: str) -> dt.datetime:
    """A naive local wall-clock time in ``zone`` as a tz-aware UTC datetime."""
    if local.tzinfo is not None:
        local = local.replace(tzinfo=None)
    offset = utc_offset_hours(zone, local)
    return (local - dt.timedelta(hours=offset)).replace(tzinfo=dt.UTC)


def local_to_utc_ns(local: dt.datetime, zone: str) -> int:
    return int(local_to_utc(local, zone).timestamp() * 1_000_000_000)


def utc_to_local(moment: dt.datetime, zone: str) -> dt.datetime:
    """A UTC instant as naive local wall time in ``zone``.

    The offset is resolved from an approximate local time first and then
    re-resolved, because the offset depends on the local date and the local
    date depends on the offset. Two passes settle every case outside the
    changeover hour itself.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    utc = moment.astimezone(dt.UTC).replace(tzinfo=None)
    guess = utc + dt.timedelta(hours=utc_offset_hours(zone, utc))
    return utc + dt.timedelta(hours=utc_offset_hours(zone, guess))


#: Display names used in this codebase that are not IANA keys.
_IANA_ALIASES = {
    "Europe/Frankfurt": "Europe/Berlin",
}


def verify_against_zoneinfo(zone: str, moments: list[dt.datetime]) -> list:
    """Disagreements between these rules and the real tz database.

    Returns an empty list when they agree. Used by the test suite on machines
    that have ``tzdata``; it is deliberately NOT called at runtime, because the
    whole point of the arithmetic rules is to work where tzdata is absent.
    """
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover - 3.11+ always has the module
        return []

    # "Europe/Frankfurt" is NOT an IANA key -- the canonical name is
    # "Europe/Berlin". ZoneInfo raised, a bare `except` turned that into
    # "nothing to verify", and the caller read an empty list as "they agree".
    # The ECB's zone was therefore never checked by the test that exists to
    # check it, and would not have been checked even with tzdata installed.
    iana = _IANA_ALIASES.get(zone, zone)
    try:
        tz = ZoneInfo(iana) if zone != "UTC" else dt.UTC
    except ZoneInfoNotFoundError as exc:
        # Distinguish "this machine has no tzdata" -- a legitimate skip, and
        # the whole reason these arithmetic rules exist -- from "that zone
        # name is wrong", which is a bug in the caller and must not be
        # silently reported as agreement.
        try:
            ZoneInfo("UTC")
        except ZoneInfoNotFoundError:
            return []            # no tzdata at all: genuinely nothing to verify
        raise ValueError(
            f"{zone!r} (resolved to {iana!r}) is not a zone this system knows. "
            "Add it to _IANA_ALIASES rather than letting the check pass "
            "vacuously.") from exc
    bad = []
    for m in moments:
        naive = m.replace(tzinfo=None)
        theirs = int(naive.replace(tzinfo=tz).utcoffset().total_seconds() // 3600)
        ours = utc_offset_hours(zone, naive)
        if theirs != ours:
            bad.append((naive.isoformat(), ours, theirs))
    return bad
