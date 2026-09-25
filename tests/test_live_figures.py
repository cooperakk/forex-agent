"""A live install must never present the demo's figures as the owner's own.

Until 1.8.2 the dashboard's live provider filled the research gates, the CPCV
paths and the monthly table from the bundled demo dataset. These tests cover
what replaced that: the Persian calendar, the durable daily equity ledger the
monthly table now reads, its endpoint, and a guard on the provider itself.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sentinel.core.jalali import jalali_month_of_day, tehran_day, to_jalali
from sentinel.ops.equity_ledger import WRITE_INTERVAL_NS, EquityLedger
from tests.test_api import auth, system  # noqa: F401  (the API fixture)

ROOT = Path(__file__).resolve().parents[1]


def _ns(y, m, d, h=12, mi=0) -> int:
    """A wall-clock instant given in Tehran time (UTC+03:30)."""
    utc = datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp() - 3.5 * 3600
    return int(utc * 1e9)


# --------------------------------------------------------------------------- #
# the calendar
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("greg,jal", [
    ((2023, 3, 21), (1402, 1, 1)),
    ((2024, 3, 20), (1403, 1, 1)),    # 1403 began on 20 March
    ((2025, 3, 20), (1403, 12, 30)),  # 1403 was a leap year
    ((2025, 3, 21), (1404, 1, 1)),
    ((2026, 3, 20), (1404, 12, 29)),
    ((2026, 3, 21), (1405, 1, 1)),
    ((2026, 9, 23), (1405, 7, 1)),    # Mehr 1
    ((2026, 9, 22), (1405, 6, 31)),
])
def test_known_persian_dates(greg, jal):
    assert to_jalali(*greg) == jal


def test_tehran_day_boundary_is_at_tehran_midnight():
    # 20:29 UTC is 23:59 in Tehran; 20:31 UTC is already the next day there.
    before = int(datetime(2026, 9, 22, 20, 29, tzinfo=timezone.utc).timestamp() * 1e9)
    after = int(datetime(2026, 9, 22, 20, 31, tzinfo=timezone.utc).timestamp() * 1e9)
    assert tehran_day(before) == "2026-09-22"
    assert tehran_day(after) == "2026-09-23"
    assert jalali_month_of_day(tehran_day(after)) == (1405, 7)


# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #

def test_ledger_survives_a_restart(tmp_path):
    path = tmp_path / "equity-days.db"
    first = EquityLedger(path)
    first.record(_ns(2026, 9, 1), 10_000.0, 10_000.0, "paper:P1")
    first.close()
    again = EquityLedger(path)
    assert [d["day"] for d in again.days()] == ["2026-09-01"]


def test_writes_are_throttled_within_a_day_but_not_across_days(tmp_path):
    ledger = EquityLedger(tmp_path / "e.db")
    t0 = _ns(2026, 9, 1, 10)
    assert ledger.record(t0, 100.0, 100.0, "a") is True
    assert ledger.record(t0 + 60_000_000_000, 101.0, 101.0, "a") is False
    assert ledger.record(t0 + WRITE_INTERVAL_NS, 102.0, 102.0, "a") is True
    assert ledger.record(_ns(2026, 9, 2, 0, 1), 103.0, 103.0, "a") is True
    days = ledger.days()
    assert [(d["day"], d["first_equity"], d["last_equity"]) for d in days] == [
        ("2026-09-01", 100.0, 102.0), ("2026-09-02", 103.0, 103.0)]


def test_monthly_returns_run_month_end_to_month_end(tmp_path):
    ledger = EquityLedger(tmp_path / "e.db")
    # Shahrivar 1405 = 23 Aug .. 22 Sep 2026; Mehr 1405 starts 23 Sep.
    ledger.record(_ns(2026, 9, 10), 10_000.0, 10_000.0, "alpari:1")
    ledger.record(_ns(2026, 9, 22, 23), 10_200.0, 10_200.0, "alpari:1")
    ledger.record(_ns(2026, 9, 23, 9), 10_150.0, 10_150.0, "alpari:1")
    ledger.record(_ns(2026, 10, 22, 23), 9_996.0, 9_996.0, "alpari:1")
    out = ledger.monthly()
    [row] = out["rows"]
    assert row["year"] == 1405
    assert row["months"][5] == 2.0          # Shahrivar: 10,000 -> 10,200, from its first reading
    assert row["months"][6] == -2.0         # Mehr: 10,200 (Shahrivar's end) -> 9,996
    assert all(v is None for i, v in enumerate(row["months"]) if i not in (5, 6))
    # The first month is measured from a reading inside it, and says so.
    assert out["partial"] == [{"year": 1405, "month": 6}]
    assert out["days"] == 4
    assert out["since_ns"] == _ns(2026, 9, 10)


def test_a_change_of_account_is_never_bridged(tmp_path):
    ledger = EquityLedger(tmp_path / "e.db")
    ledger.record(_ns(2026, 9, 25), 10_000.0, 10_000.0, "paper:PAPER-001")
    ledger.record(_ns(2026, 10, 5), 10_400.0, 10_400.0, "paper:PAPER-001")
    # The owner connects an Alpari demo with a 500-dollar balance mid-month.
    ledger.record(_ns(2026, 10, 10), 500.0, 500.0, "alpari:123")
    ledger.record(_ns(2026, 10, 20), 510.0, 510.0, "alpari:123")
    out = ledger.monthly()
    mehr = out["rows"][0]["months"][6]
    # 500 -> 510 on the demo, not 10,000 -> 510 across the switch.
    assert mehr == 2.0
    assert {"year": 1405, "month": 7} in out["partial"]


def test_an_empty_ledger_says_so(tmp_path):
    assert EquityLedger(tmp_path / "e.db").monthly() == {
        "rows": [], "partial": [], "since_ns": None, "days": 0}


# --------------------------------------------------------------------------- #
# the runtime and the endpoint
# --------------------------------------------------------------------------- #

def test_a_cycle_records_the_day_and_the_endpoint_serves_it(system):
    runtime = system["runtime"]
    runtime.run_cycle()
    days = runtime.equity_ledger.days()
    assert len(days) == 1 and days[0]["account"].startswith("paper:")
    headers = auth(system["client"], "owner1", "a-sufficiently-long-password")
    body = system["client"].get("/api/performance/monthly", headers=headers).json()
    assert body["days"] == 1
    assert len(body["rows"]) == 1 and body["partial"]


def test_a_ledger_failure_never_breaks_a_cycle(system):
    runtime = system["runtime"]

    class Broken:
        def record(self, *a, **k):
            raise OSError("disk full")

    runtime._equity_ledger = Broken()
    report = runtime.run_cycle()
    assert report is not None
    assert runtime.equity_curve, "the in-memory curve must still advance"
    assert any("equity ledger" in e for e in runtime.errors)


# --------------------------------------------------------------------------- #
# the dashboard's live provider
# --------------------------------------------------------------------------- #

def test_live_provider_never_reads_the_demo_dataset():
    src = (ROOT / "dashboard" / "src" / "api.ts").read_text(encoding="utf-8")
    live = src[src.index("export function makeLiveProvider"):src.index("export function makeDemoProvider")]
    assert "demoSnapshot" not in live and "demoEndpoints" not in live, (
        "the live provider borrows demo data; a real install would show it as the owner's")
    for field in ("gates", "cpcvSharpes", "research", "monthly", "capitalTable", "costTable"):
        assert re.search(rf"\b{field}\b\s*:", live), f"live snapshot no longer sets {field}"


def test_research_page_has_no_hard_coded_verdict_numbers():
    src = (ROOT / "dashboard" / "src" / "pages" / "Research.tsx").read_text(encoding="utf-8")
    tiles = src[src.index("function ResearchTiles"):src.index("function quantile")]
    for literal in ('value="۰٫۱۸"', 'value="۰٫۳۱"', "۴۸ نسخه امتحان شده"):
        assert literal not in tiles, f"hard-coded verdict figure {literal!r} in the tiles"
