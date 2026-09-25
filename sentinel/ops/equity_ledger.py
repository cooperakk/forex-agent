"""A durable record of the account's equity, one row per Tehran calendar day.

The runtime's equity curve is a list in memory: one point per cycle, capped at
20,000 points (about two weeks at the default cadence) and gone at every
restart. Monthly results cannot be derived from that honestly. A month would
silently mean "since the engine last started". So the dashboard showed the
demo dataset's monthly table instead, as if it were the owner's.

This ledger keeps the first and the last reading of every day on disk, with
the account they belong to. Monthly returns are month-end equity over the
previous month-end, in the Persian calendar the dashboard reports in. It is
not a statement from the broker:

* deposits and withdrawals are not separated from trading results;
* a month whose history starts inside it (the first month recorded, or the
  first month on a newly connected account) is measured from its first
  reading and flagged ``partial``;
* a change of account is never bridged: equity on a demo account is not a
  return on the simulator's equity.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.jalali import jalali_month_of_day, tehran_day

SCHEMA = """
CREATE TABLE IF NOT EXISTS equity_days (
    day          TEXT PRIMARY KEY,   -- YYYY-MM-DD, calendar day in Tehran
    account      TEXT NOT NULL,      -- broker profile + account id
    first_ts_ns  INTEGER NOT NULL,
    first_equity REAL NOT NULL,
    last_ts_ns   INTEGER NOT NULL,
    last_equity  REAL NOT NULL,
    last_balance REAL NOT NULL
);
"""

#: Write at most this often within one day. The last reading of a day is
#: therefore at most this stale -- five minutes of a month-end, not of a month.
WRITE_INTERVAL_NS = 300 * 1_000_000_000


class EquityLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._last_write_ns = 0
        self._last_key: Tuple[str, str] = ("", "")

    def record(self, ts_ns: int, equity: float, balance: float, account: str) -> bool:
        """Note a reading. Returns whether it reached the disk.

        A new day or a new account is written at once; otherwise at most
        every five minutes. A day on which the account changed keeps the
        FIRST account's opening reading and ends on the new account: the
        month computation then sees the change and does not bridge it.
        """
        day = tehran_day(ts_ns)
        key = (day, account)
        if key == self._last_key and ts_ns - self._last_write_ns < WRITE_INTERVAL_NS:
            return False
        with self._lock:
            row = self._conn.execute("SELECT account FROM equity_days WHERE day=?",
                                     (day,)).fetchone()
            if row is None or row["account"] != account:
                # A new day, or the account changed today: the day restarts on
                # the account now in use, so its first and last reading always
                # describe one account.
                self._conn.execute(
                    "INSERT OR REPLACE INTO equity_days (day, account, first_ts_ns,"
                    " first_equity, last_ts_ns, last_equity, last_balance)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (day, account, ts_ns, equity, ts_ns, equity, balance))
            else:
                self._conn.execute(
                    "UPDATE equity_days SET last_ts_ns=?, last_equity=?, last_balance=?"
                    " WHERE day=?", (ts_ns, equity, balance, day))
            self._conn.commit()
        self._last_write_ns, self._last_key = ts_ns, key
        return True

    def days(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM equity_days ORDER BY day").fetchall()]

    def monthly(self) -> Dict[str, Any]:
        """Monthly returns in percent, by Jalali year, for the dashboard.

        ``rows`` is ``[{"year": 1405, "months": [12 values or None]}]``;
        ``partial`` lists the months measured from a reading inside the month.
        """
        days = self.days()
        if not days:
            return {"rows": [], "partial": [], "since_ns": None, "days": 0}
        months: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        order: List[Tuple[int, int]] = []
        for d in days:
            key = jalali_month_of_day(d["day"])
            if key not in months:
                months[key] = []
                order.append(key)
            months[key].append(d)

        values: Dict[Tuple[int, int], Optional[float]] = {}
        partial: List[Dict[str, int]] = []
        previous_end: Optional[Dict[str, Any]] = None
        for key in order:
            rows = months[key]
            end = rows[-1]
            mixed = any(r["account"] != end["account"] for r in rows)
            base: Optional[float]
            if previous_end is not None and previous_end["account"] == end["account"] \
                    and not mixed:
                base = previous_end["last_equity"]
            else:
                # History starts inside this month, or the account changed in
                # it: measure from the first reading of the account's latest
                # uninterrupted stretch, and say so.
                start = len(rows) - 1
                while start > 0 and rows[start - 1]["account"] == end["account"]:
                    start -= 1
                partial.append({"year": key[0], "month": key[1]})
                base = rows[start]["first_equity"]
            values[key] = (round((end["last_equity"] / base - 1.0) * 100.0, 2)
                           if base and base > 0 else None)
            previous_end = end

        years = sorted({k[0] for k in order})
        out = [{"year": y, "months": [values.get((y, m)) for m in range(1, 13)]}
               for y in years]
        return {"rows": out, "partial": partial, "since_ns": days[0]["first_ts_ns"],
                "days": len(days)}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
