"""CFTC Commitments of Traders: how the big players are positioned in currency futures.

Every Friday the CFTC publishes, for each futures market, the long and short
positions held on the previous TUESDAY by large "non-commercial" traders
(speculators: funds, CTAs) and "commercial" traders (hedgers). For FX, the
CME currency futures are the standard window onto speculative positioning.

What it is good for, and what it is not
---------------------------------------
Speculators' net position moves WITH the exchange rate far more reliably than
it predicts it (Klitgaard & Weir 2004, FRBNY Economic Policy Review): chasing
it is chasing the past. What the literature does support is that an EXTREME,
crowded position is fragile -- Brunnermeier, Nagel & Pedersen (2008) find that
speculators' net futures positions predict currency crash risk (negative
skewness). Sentinel therefore uses it for caution, not for direction:

* the POSITIONING INDEX ranks the current net position against the last
  ``lookback`` weekly reports: 0 = the most short in three years, 100 = the
  most long;
* a trade on the side of a crowded position (index >= ``extreme`` for a long,
  <= 100 - ``extreme`` for a short) is sized down, and the brain's scorecard
  measures whether that helped.

No look-ahead
-------------
A report describes Tuesday but is public only on Friday afternoon (15:30 New
York). Each report carries ``available_ns`` = its Tuesday + 3 days at 21:00
UTC -- after the release in both winter and summer time -- and every query
"as of" a time uses only reports available by then. A backtest that joined
COT on its Tuesday date would be trading on data three days early.

Sources
-------
* the CFTC Public Reporting Environment (Socrata), dataset ``6dca-aqww``
  (legacy report, futures only), at a FIXED host; no redirects, capped size;
* or the CFTC's own yearly files (``deacotYYYY.zip`` / ``annual.txt``), for a
  server that cannot reach the API -- download them in a browser and import.

Both spell the same columns differently; the parser accepts either.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import sqlite3
import threading
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

#: currency -> CFTC contract market code (CME unless noted).
COT_CODES: Dict[str, str] = {
    "EUR": "099741",   # Euro FX
    "JPY": "097741",   # Japanese yen
    "GBP": "096742",   # British pound
    "CHF": "092741",   # Swiss franc
    "CAD": "090741",   # Canadian dollar
    "AUD": "232741",   # Australian dollar
    "NZD": "112741",   # New Zealand dollar
    "MXN": "095741",   # Mexican peso
    "USD": "098662",   # US Dollar Index (ICE Futures US)
    "XAU": "088691",   # Gold (COMEX)
    "XAG": "084691",   # Silver (COMEX)
}
CODE_TO_CCY = {v: k for k, v in COT_CODES.items()}

API_HOST = "https://publicreporting.cftc.gov"
API_PATH = "/resource/6dca-aqww.json"
MAX_RESPONSE = 8_000_000
RELEASE_LAG_NS = (3 * 86_400 + 21 * 3_600) * 10**9

Transport = Callable[..., Tuple[int, str]]


class CotError(RuntimeError):
    pass


@dataclass
class CotReport:
    currency: str
    code: str
    report_date: str            # the Tuesday, YYYY-MM-DD
    available_ns: int           # when it became public (conservative)
    spec_long: float
    spec_short: float
    comm_long: float
    comm_short: float
    open_interest: float

    @property
    def spec_net(self) -> float:
        return self.spec_long - self.spec_short

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# parsing: the API and the yearly files spell every column differently
# --------------------------------------------------------------------------- #

def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


_ALIASES = {
    "date": ("reportdateasyyyymmdd", "asofdateinformyyyymmdd", "reportdate"),
    "code": ("cftccontractmarketcode",),
    "spec_long": ("noncommpositionslongall", "noncommercialpositionslongall"),
    "spec_short": ("noncommpositionsshortall", "noncommercialpositionsshortall"),
    "comm_long": ("commpositionslongall", "commercialpositionslongall"),
    "comm_short": ("commpositionsshortall", "commercialpositionsshortall"),
    "oi": ("openinterestall",),
}


def available_from(report_date: str) -> int:
    d = dt.datetime.strptime(report_date[:10], "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp()) * 10**9 + RELEASE_LAG_NS


def parse_rows(rows: Iterable[Dict[str, Any]],
               codes: Optional[Sequence[str]] = None) -> List[CotReport]:
    """Reports from API or file rows; unknown markets and broken rows are skipped."""
    wanted = set(codes or CODE_TO_CCY)
    out: List[CotReport] = []
    for row in rows:
        norm = {_norm(k): v for k, v in (row or {}).items()}

        def pick(field: str, norm: Dict[str, Any] = norm):
            for alias in _ALIASES[field]:
                if alias in norm and norm[alias] not in (None, ""):
                    return norm[alias]
            return None

        code = str(pick("code") or "").strip()
        if code not in wanted:
            continue
        raw_date = str(pick("date") or "").strip()[:10]
        try:
            dt.datetime.strptime(raw_date, "%Y-%m-%d")
            vals = [float(str(pick(f)).replace(",", "")) for f in
                    ("spec_long", "spec_short", "comm_long", "comm_short", "oi")]
        except (TypeError, ValueError):
            continue
        if any(v < 0 for v in vals) or vals[4] <= 0:
            continue
        out.append(CotReport(currency=CODE_TO_CCY.get(code, code), code=code,
                             report_date=raw_date, available_ns=available_from(raw_date),
                             spec_long=vals[0], spec_short=vals[1], comm_long=vals[2],
                             comm_short=vals[3], open_interest=vals[4]))
    return out


def parse_file(path: str | Path) -> List[CotReport]:
    """A CFTC yearly file (``deacotYYYY.zip`` or the ``annual.txt`` inside it)."""
    p = Path(path)
    if p.stat().st_size > 200_000_000:
        raise CotError("file larger than 200 MB")
    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            names = [n for n in z.namelist() if n.lower().endswith((".txt", ".csv"))]
            if not names:
                raise CotError("no .txt or .csv inside the zip")
            text = z.read(names[0]).decode("utf-8", errors="replace")
    else:
        text = p.read_text("utf-8", errors="replace")
    return parse_rows(csv.DictReader(io.StringIO(text)))


# --------------------------------------------------------------------------- #
# the API client
# --------------------------------------------------------------------------- #

def _default_get(url: str, *, params: Dict[str, str], timeout: float) -> Tuple[int, str]:
    import httpx
    with httpx.stream("GET", url, params=params, timeout=timeout,
                      follow_redirects=False) as resp:
        chunks, total = [], 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE:
                raise CotError("the CFTC API answered with more than 8 MB")
            chunks.append(chunk)
        return resp.status_code, b"".join(chunks).decode("utf-8", errors="replace")


class CotClient:
    def __init__(self, *, get: Optional[Transport] = None, timeout: float = 30.0) -> None:
        self._get = get or _default_get
        self.timeout = timeout

    def fetch(self, codes: Sequence[str], since: str) -> List[CotReport]:
        codes = [c for c in codes if re.fullmatch(r"\d{6}", c)]
        if not codes:
            return []
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
            raise ValueError("since must be YYYY-MM-DD")
        where = ("cftc_contract_market_code in(" + ",".join(f"'{c}'" for c in codes)
                 + f") AND report_date_as_yyyy_mm_dd >= '{since}T00:00:00.000'")
        params = {"$where": where, "$order": "report_date_as_yyyy_mm_dd ASC",
                  "$limit": "20000"}
        try:
            status, body = self._get(API_HOST + API_PATH, params=params, timeout=self.timeout)
        except CotError:
            raise
        except Exception as exc:  # noqa: BLE001 - network failures are data
            raise CotError(f"{type(exc).__name__}: {exc}"[:300]) from None
        if status in (301, 302, 303, 307, 308):
            raise CotError("the CFTC API answered with a redirect, which is not followed")
        if status != 200:
            raise CotError(f"HTTP {status}: {body[:200]}")
        try:
            rows = json.loads(body)
        except ValueError:
            raise CotError("the CFTC API did not answer with JSON") from None
        if not isinstance(rows, list):
            raise CotError(f"unexpected answer: {str(rows)[:200]}")
        return parse_rows(rows, codes)


# --------------------------------------------------------------------------- #
# storage and the positioning index
# --------------------------------------------------------------------------- #

class CotStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS cot (
        code TEXT NOT NULL, report_date TEXT NOT NULL, currency TEXT NOT NULL,
        available_ns INTEGER NOT NULL, spec_long REAL NOT NULL, spec_short REAL NOT NULL,
        comm_long REAL NOT NULL, comm_short REAL NOT NULL, open_interest REAL NOT NULL,
        PRIMARY KEY (code, report_date));
    CREATE TABLE IF NOT EXISTS macro_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
        except (FileExistsError, OSError):
            pass
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(self.SCHEMA)
            self._conn.commit()
        self._cache: Dict[str, List[CotReport]] = {}

    def upsert(self, reports: Iterable[CotReport]) -> int:
        rows = [(r.code, r.report_date, r.currency, r.available_ns, r.spec_long, r.spec_short,
                 r.comm_long, r.comm_short, r.open_interest) for r in reports]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR REPLACE INTO cot VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self._conn.commit()
            self._cache.clear()
            return self._conn.total_changes - before

    def history(self, currency: str) -> List[CotReport]:
        code = COT_CODES.get(currency)
        if code is None:
            return []
        with self._lock:
            hit = self._cache.get(code)
            if hit is not None:
                return hit
            rows = self._conn.execute(
                "SELECT * FROM cot WHERE code=? ORDER BY report_date", (code,)).fetchall()
            out = [CotReport(**{k: r[k] for k in r.keys()}) for r in rows]
            self._cache[code] = out
            return out

    def latest_date(self) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT MAX(report_date) d FROM cot").fetchone()
        return row["d"] if row and row["d"] else None

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM cot").fetchone()[0])

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT v FROM macro_state WHERE k=?", (key,)).fetchone()
        try:
            return json.loads(row["v"]) if row else default
        except ValueError:
            return default

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO macro_state VALUES (?,?)",
                               (key, json.dumps(value, default=str)))
            self._conn.commit()


def positioning(history: Sequence[CotReport], as_of_ns: int, *, lookback: int = 156,
                min_weeks: int = 52) -> Optional[Dict[str, Any]]:
    """The speculators' positioning index as of a time, or None if unknown.

    ``index`` = (net - min) / (max - min) x 100 over the last ``lookback``
    reports available by ``as_of_ns``; ``change`` = index change since the
    previous report; ``net_pct_oi`` = net position as % of open interest.
    """
    avail = [r for r in history if r.available_ns <= as_of_ns]
    if len(avail) < min_weeks:
        return None
    window = avail[-lookback:]
    nets = [r.spec_net for r in window]
    lo, hi = min(nets), max(nets)
    last = window[-1]

    def idx(v: float) -> float:
        return 50.0 if hi <= lo else (v - lo) / (hi - lo) * 100.0

    index = idx(last.spec_net)
    prev = idx(window[-2].spec_net) if len(window) >= 2 else index
    return {"currency": last.currency, "report_date": last.report_date,
            "available_ns": last.available_ns, "net": last.spec_net,
            "net_pct_oi": round(last.spec_net / last.open_interest * 100.0, 2),
            "index": round(index, 1), "change": round(index - prev, 1),
            "weeks": len(window)}
