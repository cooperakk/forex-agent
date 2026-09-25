"""Gregorian -> Jalali (Iranian solar hijri) dates, and the Tehran calendar day.

The dashboard reports monthly results "by the months of the Persian year", so
the server has to know which Persian month a moment falls in. This is the
arithmetic of the jalaali-js library (Borkowski's leap-year breaks), which
agrees with the official Iranian calendar for 1178-1633 AP. No dependency: a
Windows install has no tz database and no jdatetime, and a calendar is not
worth a new package.

Integer division in the reference algorithm truncates toward zero (JavaScript
``~~(a / b)``), which differs from Python's ``//`` for negative operands. The
``_div`` / ``_mod`` helpers keep those semantics, because one branch of the
leap computation depends on ``mod(-1, 4) == -1``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Tuple

#: Iran has observed no daylight saving time since 2022 (1401); UTC+03:30 all
#: year. A fixed offset, not a zone lookup: Windows Python has no zone data.
TEHRAN = timezone(timedelta(hours=3, minutes=30), "Asia/Tehran")

_BREAKS = (-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060,
           2097, 2192, 2262, 2324, 2394, 2456, 3178)

MONTHS_FA = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
             "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")


def _div(a: int, b: int) -> int:
    return int(a / b)


def _mod(a: int, b: int) -> int:
    return a - _div(a, b) * b


def _jal_cal(jy: int) -> Tuple[int, int, int]:
    """(leap, gregorian year, day of March on which Farvardin 1 falls)."""
    if jy < _BREAKS[0] or jy >= _BREAKS[-1]:
        raise ValueError(f"Jalali year {jy} is outside the supported range")
    gy = jy + 621
    leap_j = -14
    jp = _BREAKS[0]
    jump = 0
    for jm in _BREAKS[1:]:
        jump = jm - jp
        if jy < jm:
            break
        leap_j += _div(jump, 33) * 8 + _div(_mod(jump, 33), 4)
        jp = jm
    n = jy - jp
    leap_j += _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4)
    if _mod(jump, 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150
    march = 20 + leap_j - leap_g
    if jump - n < 6:
        n = n - jump + _div(jump + 4, 33) * 33
    leap = _mod(_mod(n + 1, 33) - 1, 4)
    if leap == -1:
        leap = 4
    return leap, gy, march


def _g2d(gy: int, gm: int, gd: int) -> int:
    d = (_div((gy + _div(gm - 8, 6) + 100100) * 1461, 4)
         + _div(153 * _mod(gm + 9, 12) + 2, 5) + gd - 34840408)
    return d - _div(_div(gy + 100100 + _div(gm - 8, 6), 100) * 3, 4) + 752


def _d2g(jdn: int) -> Tuple[int, int, int]:
    j = 4 * jdn + 139361631
    j = j + _div(_div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908
    i = _div(_mod(j, 1461), 4) * 5 + 308
    gd = _div(_mod(i, 153), 5) + 1
    gm = _mod(_div(i, 153), 12) + 1
    gy = _div(j, 1461) - 100100 + _div(8 - gm, 6)
    return gy, gm, gd


def _d2j(jdn: int) -> Tuple[int, int, int]:
    gy = _d2g(jdn)[0]
    jy = gy - 621
    leap, _, march = _jal_cal(jy)
    k = jdn - _g2d(gy, 3, march)
    if k >= 0:
        if k <= 185:
            return jy, 1 + _div(k, 31), _mod(k, 31) + 1
        k -= 186
    else:
        jy -= 1
        k += 179
        if leap == 1:
            k += 1
    return jy, 7 + _div(k, 30), _mod(k, 30) + 1


def to_jalali(gy: int, gm: int, gd: int) -> Tuple[int, int, int]:
    """Gregorian (year, month, day) -> Jalali (year, month, day)."""
    return _d2j(_g2d(gy, gm, gd))


def tehran_day(ts_ns: int) -> str:
    """The calendar day in Tehran of a wall-clock timestamp, as YYYY-MM-DD."""
    return datetime.fromtimestamp(ts_ns / 1e9, TEHRAN).strftime("%Y-%m-%d")


def jalali_month_of_day(day: str) -> Tuple[int, int]:
    """'YYYY-MM-DD' (Gregorian, Tehran) -> (Jalali year, Jalali month 1..12)."""
    y, m, d = (int(x) for x in day.split("-"))
    jy, jm, _ = to_jalali(y, m, d)
    return jy, jm
