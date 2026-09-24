"""Market data with a passport.

Every value the agent sees carries where it came from, when it arrived, and how
old it is. The research brief calls this a data passport, and it exists because
a missing value silently replaced by zero -- or by the last known value -- can
flip a decision without leaving a trace.

Three rules, enforced here:

* **A gap is a fact.** Missing bars are marked ``DataQuality.GAP`` and are never
  forward-filled behind the caller's back.
* **Staleness is visible.** ``age_sec`` is attached to every quote and bar, and
  the risk engine refuses entries on stale data.
* **A bar in progress is not a bar.** Only ``complete`` bars are handed to
  strategies. Acting on the current, unfinished bar is the most common
  look-ahead bug in live systems, and the one that never shows up in a
  backtest.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..core.clock import wall_ns
from ..core.errors import StaleDataError
from ..core.money import dec
from ..core.types import Bar, DataQuality, Quote

TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    instrument TEXT NOT NULL,
    timeframe  TEXT NOT NULL,
    start_ns   INTEGER NOT NULL,
    end_ns     INTEGER NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'unknown',
    quality TEXT NOT NULL DEFAULT 'ok',
    received_ns INTEGER NOT NULL,
    PRIMARY KEY (instrument, timeframe, start_ns)
);
CREATE INDEX IF NOT EXISTS idx_bars_lookup ON bars(instrument, timeframe, start_ns DESC);
CREATE TABLE IF NOT EXISTS bar_features (
    instrument TEXT NOT NULL,
    timeframe  TEXT NOT NULL,
    start_ns   INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (instrument, timeframe, start_ns)
);
"""


@dataclass
class DataPassport:
    instrument: str
    timeframe: str
    source: str
    bars: int
    first_ns: Optional[int]
    last_ns: Optional[int]
    age_sec: float
    gaps: int
    quality: DataQuality
    expected_interval_sec: int

    def to_dict(self) -> dict:
        return {"instrument": self.instrument, "timeframe": self.timeframe,
                "source": self.source, "bars": self.bars,
                "first_ns": self.first_ns, "last_ns": self.last_ns,
                "age_sec": (round(self.age_sec, 1) if np.isfinite(self.age_sec) else None),
                "gaps": self.gaps,
                "quality": self.quality.value,
                "expected_interval_sec": self.expected_interval_sec}


class BarStore:
    """SQLite-backed bar history. Append-only in practice."""

    def __init__(self, path: str | Path = "var/market.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def upsert(self, bars: Iterable[Bar]) -> int:
        rows = [(b.instrument, b.timeframe, b.start_ns, b.end_ns, float(b.open),
                 float(b.high), float(b.low), float(b.close), float(b.volume),
                 1 if b.complete else 0, b.source, b.quality.value, wall_ns())
                for b in bars]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO bars (instrument, timeframe, start_ns, end_ns, open, high, low, "
                "close, volume, complete, source, quality, received_ns) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(instrument, timeframe, start_ns) DO UPDATE SET "
                "end_ns=excluded.end_ns, open=excluded.open, high=excluded.high, "
                "low=excluded.low, close=excluded.close, volume=excluded.volume, "
                "complete=excluded.complete, quality=excluded.quality, "
                "received_ns=excluded.received_ns "
                # A completed bar is never overwritten by an incomplete update.
                "WHERE excluded.complete >= bars.complete", rows)
            self._conn.commit()
        return len(rows)

    def upsert_frame(self, frame: pd.DataFrame, instrument: str, timeframe: str,
                     source: str = "import") -> int:
        """Store a validated frame, keeping the columns beyond OHLCV.

        Bid/ask OHLC, ``carry_bp``, swap rates -- anything a strategy or the
        research protocol reads beside the mid price -- goes into the
        ``bar_features`` side table and comes back joined by ``frame()``. The
        carry family's whole input used to be dropped at this boundary.
        """
        from .validation import validate_frame
        validate_frame(frame, name=f"{instrument}@{timeframe}")
        n = self.upsert(bars_from_frame(frame, instrument, timeframe, source))
        extras = [c for c in frame.columns
                  if c not in ("open", "high", "low", "close", "volume", "source", "quality")]
        if extras:
            starts = frame.index.tz_convert("UTC").as_unit("ns").asi8
            rows = []
            for i, start in enumerate(starts):
                payload = {}
                for c in extras:
                    v = frame[c].iloc[i]
                    if hasattr(v, "item"):
                        v = v.item()
                    if isinstance(v, float) and not np.isfinite(v):
                        continue
                    payload[c] = v
                rows.append((instrument, timeframe, int(start),
                             json.dumps(payload, allow_nan=False, default=str)))
            with self._lock:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO bar_features VALUES (?,?,?,?)", rows)
                self._conn.commit()
        return n

    def frame(self, instrument: str, timeframe: str, limit: int = 5000,
              complete_only: bool = True) -> pd.DataFrame:
        q = ("SELECT start_ns, end_ns, open, high, low, close, volume, source, quality, complete "
             "FROM bars WHERE instrument=? AND timeframe=?")
        args: List = [instrument, timeframe]
        if complete_only:
            q += " AND complete=1"
        q += " ORDER BY start_ns DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume",
                                         "source", "quality"])
        df = pd.DataFrame([dict(r) for r in rows]).sort_values("start_ns")
        idx = pd.to_datetime(df["start_ns"], unit="ns", utc=True)
        out = df[["open", "high", "low", "close", "volume", "source", "quality"]].copy()
        out.index = pd.DatetimeIndex(idx)
        with self._lock:
            extra = self._conn.execute(
                "SELECT start_ns, payload FROM bar_features WHERE instrument=? AND "
                "timeframe=? AND start_ns>=? AND start_ns<=?",
                (instrument, timeframe, int(df["start_ns"].iloc[0]),
                 int(df["start_ns"].iloc[-1]))).fetchall()
        if extra:
            feats = pd.DataFrame([json.loads(r["payload"]) for r in extra],
                                 index=pd.to_datetime([int(r["start_ns"]) for r in extra],
                                                      unit="ns", utc=True))
            out = out.join(feats, how="left")
        return out

    def passport(self, instrument: str, timeframe: str,
                 now_ns: Optional[int] = None) -> DataPassport:
        """Freshness is measured against ``now_ns``.

        Injecting the clock rather than reading it here is what makes an
        accelerated replay testable: in production the caller passes the wall
        clock, in a simulation it passes the simulated one, and the staleness
        logic under test is identical in both."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) n, MIN(start_ns) first_ns, MAX(start_ns) last_ns, "
                "MAX(source) src FROM bars WHERE instrument=? AND timeframe=? AND complete=1",
                (instrument, timeframe)).fetchone()
        n = int(row["n"] or 0)
        interval = TIMEFRAME_SECONDS.get(timeframe, 3600)
        if n == 0:
            return DataPassport(instrument, timeframe, "none", 0, None, None,
                                float("inf"), 0, DataQuality.GAP, interval)
        age = ((now_ns if now_ns is not None else wall_ns()) - int(row["last_ns"])) / 1e9
        gaps = self.count_gaps(instrument, timeframe)
        quality = DataQuality.OK
        if age > interval * 3:
            quality = DataQuality.STALE
        elif gaps > 0:
            quality = DataQuality.GAP
        return DataPassport(instrument, timeframe, row["src"] or "unknown", n,
                            int(row["first_ns"]), int(row["last_ns"]), age, gaps,
                            quality, interval)

    def latest_start_ns(self, instrument: str, timeframe: str,
                        complete_only: bool = True) -> Optional[int]:
        """Start of the newest stored bar, or None when there is none.

        The incremental fetch is keyed on this: everything after it is what the
        venue may have completed since the last cycle.
        """
        q = "SELECT MAX(start_ns) AS last_ns FROM bars WHERE instrument=? AND timeframe=?"
        if complete_only:
            q += " AND complete=1"
        with self._lock:
            row = self._conn.execute(q, (instrument, timeframe)).fetchone()
        if row is None or row["last_ns"] is None:
            return None
        return int(row["last_ns"])

    def count_gaps(self, instrument: str, timeframe: str, limit: int = 2000) -> int:
        """Missing bars beyond the normal weekend break."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT start_ns FROM bars WHERE instrument=? AND timeframe=? AND complete=1 "
                "ORDER BY start_ns DESC LIMIT ?", (instrument, timeframe, limit)).fetchall()
        if len(rows) < 3:
            return 0
        starts = np.array(sorted(int(r["start_ns"]) for r in rows))
        interval_ns = TIMEFRAME_SECONDS.get(timeframe, 3600) * 1_000_000_000
        # A gap is a missing bar during MARKET HOURS. The FX week is shut from
        # Friday 22:00 UTC to Sunday 22:00 UTC (within an hour across DST), so
        # a hole that lies entirely inside that window is the weekend, not a
        # gap -- and a hole that spans Thursday is a gap however long it is.
        # The old "anything shorter than 2.6 days" heuristic counted every
        # weekend on M15 and missed a day-long outage on D1.
        gaps = 0
        for a, b in zip(starts[:-1], starts[1:]):
            if b - a <= interval_ns * 1.5:
                continue
            missing = pd.date_range(pd.Timestamp(int(a + interval_ns), unit="ns", tz="UTC"),
                                    pd.Timestamp(int(b - 1), unit="ns", tz="UTC"),
                                    freq=pd.Timedelta(interval_ns, unit="ns"))
            if any(not _fx_closed(t) for t in missing):
                gaps += 1
        return gaps

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _fx_closed(t: pd.Timestamp) -> bool:
    """Inside the weekend close (Fri 22:00 UTC .. Sun 22:00 UTC)."""
    wd, h = t.weekday(), t.hour
    return wd == 5 or (wd == 4 and h >= 22) or (wd == 6 and h < 22)


def frame_from_bars(bars: Sequence[Bar]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    idx = pd.DatetimeIndex(pd.to_datetime([b.start_ns for b in bars], unit="ns", utc=True))
    return pd.DataFrame({
        "open": [float(b.open) for b in bars], "high": [float(b.high) for b in bars],
        "low": [float(b.low) for b in bars], "close": [float(b.close) for b in bars],
        "volume": [float(b.volume) for b in bars],
        "source": [b.source for b in bars],
    }, index=idx)


def bars_from_frame(df: pd.DataFrame, instrument: str, timeframe: str,
                    source: str = "import") -> List[Bar]:
    interval_ns = TIMEFRAME_SECONDS.get(timeframe, 3600) * 1_000_000_000
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None:
        raise TypeError("frame index must be a timezone-aware DatetimeIndex (UTC)")
    starts = idx.tz_convert("UTC").as_unit("ns").astype("int64").to_numpy()
    out: List[Bar] = []
    for i, start in enumerate(starts):
        row = df.iloc[i]
        out.append(Bar(
            instrument=instrument, timeframe=timeframe,
            open=dec(row["open"]), high=dec(row["high"]), low=dec(row["low"]),
            close=dec(row["close"]), volume=dec(row.get("volume", 0)),
            start_ns=int(start), end_ns=int(start) + interval_ns,
            complete=True, source=source,
        ))
    return out


@dataclass
class FeedSnapshot:
    quotes: Dict[str, Quote] = field(default_factory=dict)
    #: Primary-timeframe frames, one per instrument.
    frames: Dict[str, pd.DataFrame] = field(default_factory=dict)
    #: timeframe -> instrument -> frame, for every timeframe that was requested
    #: (the primary included). A strategy is handed the frame of the timeframe
    #: it declares, never a substitute.
    frames_by_tf: Dict[str, Dict[str, pd.DataFrame]] = field(default_factory=dict)
    passports: Dict[str, DataPassport] = field(default_factory=dict)
    passports_by_tf: Dict[str, Dict[str, DataPassport]] = field(default_factory=dict)
    ages: Dict[str, float] = field(default_factory=dict)
    quality: Dict[str, DataQuality] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)

    def frames_for(self, timeframe: Optional[str]) -> Dict[str, pd.DataFrame]:
        """Frames of ``timeframe``; the primary set when it is None or unknown.

        Returning the primary for an UNKNOWN timeframe is deliberate only for
        ``None``: a named timeframe that was never loaded returns an empty map,
        so the caller skips rather than trading a daily rule on four-hour bars.
        """
        if not timeframe:
            return self.frames
        return self.frames_by_tf.get(timeframe, {})

    def to_dict(self) -> dict:
        return {
            "quotes": {k: {"bid": str(v.bid), "ask": str(v.ask), "source": v.source,
                           "age_sec": round(v.age_ns() / 1e9, 2)}
                       for k, v in self.quotes.items()},
            "passports": {k: v.to_dict() for k, v in self.passports.items()},
            "passports_by_tf": {tf: {k: v.to_dict() for k, v in m.items()}
                                for tf, m in self.passports_by_tf.items()},
            "errors": self.errors,
        }


#: A venue is not asked for bars more often than this, whatever the decision
#: interval. Over a weekend nothing completes for two days and the ask is
#: cheap, but a tight loop against a rate-limited REST venue is still a bill.
_MIN_FETCH_GAP_NS = 30 * 1_000_000_000


class MarketFeed:
    """Combines live quotes from the broker with stored bar history.

    ``refresh`` is the half of this class that was missing for a long time: the
    store was only ever written by the synthetic paper simulation, so on a real
    venue every strategy saw an empty frame, never reached its warm-up bar, and
    never produced a signal -- while the heartbeat, the reconciler and the
    dashboard all reported a healthy system. It looked like a quiet market. It
    was an empty table.

    Two sources, one interface:

    * a venue that reports its own candles (``broker.supports_bar_history``)
      is polled incrementally -- a full backfill on the first cycle, then only
      the bars that could have completed since;
    * a ``driver`` -- the synthetic market for the paper venue -- owns both the
      bars and the quotes and is ticked instead.
    """

    def __init__(self, broker, store: BarStore, *, timeframe: str = "H1",
                 history: int = 1500, driver=None) -> None:
        self.broker = broker
        self.store = store
        #: The PRIMARY timeframe: regime detection, the ATR the protection
        #: layer trails on, and the correlation estimate all read this one.
        #: Each strategy allocation is handed the frame of ITS OWN declared
        #: timeframe -- a daily system given four-hour bars is a different
        #: system from the one that was validated.
        self.timeframe = timeframe
        self.history = history
        self.driver = driver
        self._lock = threading.Lock()
        #: (instrument, timeframe) -> wall time of the last venue fetch.
        self._last_fetch_ns: Dict[tuple, int] = {}
        #: "instrument@timeframe" -> the last problem, so the snapshot can say
        #: why a frame is short instead of leaving the caller to guess.
        self.last_errors: Dict[str, str] = {}
        self.bars_ingested: int = 0

    # -- ingestion ------------------------------------------------------------ #

    def refresh(self, instruments: Sequence[str], now_ns: Optional[int] = None,
                timeframes: Optional[Sequence[str]] = None) -> Dict[str, int]:
        """Bring the store up to date. Returns NEW bars per "instrument@timeframe".

        Never raises: a venue that cannot deliver bars is recorded in
        ``last_errors`` and the snapshot goes on with whatever history exists,
        which the data-quality passport then reports honestly as stale.
        """
        now = now_ns if now_ns is not None else wall_ns()
        tfs = self._timeframes(timeframes)
        out: Dict[str, int] = {}
        if self.driver is not None:
            try:
                out = dict(self.driver.tick(now, list(instruments), timeframes=tfs) or {})
                self.last_errors.pop("*", None)
            except Exception as exc:  # noqa: BLE001
                self.last_errors["*"] = f"driver: {exc}"
            self.bars_ingested += sum(out.values())
            return out
        if not getattr(self.broker, "supports_bar_history", False):
            return out
        for tf in tfs:
            for sym in instruments:
                key = f"{sym}@{tf}"
                try:
                    n = self._ingest(sym, tf, now)
                except Exception as exc:  # noqa: BLE001 - one symbol must not stop the rest
                    self.last_errors[key] = f"{exc.__class__.__name__}: {exc}"
                    continue
                self.last_errors.pop(key, None)
                if n:
                    out[key] = n
                    self.bars_ingested += n
        return out

    def _timeframes(self, requested: Optional[Sequence[str]]) -> List[str]:
        tfs = [self.timeframe]
        for tf in requested or ():
            if tf and tf not in tfs:
                if tf not in TIMEFRAME_SECONDS:
                    self.last_errors[f"*@{tf}"] = f"unknown timeframe {tf!r}"
                    continue
                tfs.append(tf)
        return tfs

    def _ingest(self, sym: str, timeframe: str, now_ns: int) -> int:
        interval_ns = TIMEFRAME_SECONDS.get(timeframe, 3600) * 1_000_000_000
        with self._lock:
            last = self.store.latest_start_ns(sym, timeframe)
            last_fetch = self._last_fetch_ns.get((sym, timeframe), 0)
            if last is not None:
                # Nothing can have completed until the bar after the newest one
                # closes; until then a fetch is pure noise against the venue.
                if now_ns < last + 2 * interval_ns and last_fetch:
                    return 0
                if now_ns - last_fetch < _MIN_FETCH_GAP_NS:
                    return 0
                elapsed = max(0, now_ns - last) // interval_ns
                # +2: the bar that just closed and the one still forming;
                # a small overlap re-confirms the newest stored bar.
                count = int(min(self.history, max(3, elapsed + 2)))
            else:
                count = int(self.history)
            self._last_fetch_ns[(sym, timeframe)] = now_ns
        bars = self.broker.fetch_bars(sym, timeframe, count, end_ns=now_ns)
        if not bars:
            return 0
        # Only what has CLOSED. A bar in progress is not a bar (module docstring),
        # and the venue's own `complete` flag is believed over the clock when
        # both are available -- OANDA marks it, MetaTrader is derived.
        fresh = [b for b in bars if b.complete and b.end_ns <= now_ns]
        if not fresh:
            return 0
        # Bar.__post_init__ already refuses impossible geometry per bar; the
        # frame-level check adds ordering and duplicates, which a venue that
        # re-sends a corrected bar can produce.
        from .validation import validate_frame
        validate_frame(frame_from_bars(fresh), name=f"{sym}@{timeframe}")
        self.store.upsert(fresh)
        # Report NEW bars, not rows touched: the overlap re-confirms the head
        # and must not read as ingestion on every cycle.
        return sum(1 for b in fresh if last is None or b.start_ns > last)

    # -- read ----------------------------------------------------------------- #

    def snapshot(self, instruments: Sequence[str], now_ns: Optional[int] = None,
                 timeframes: Optional[Sequence[str]] = None) -> FeedSnapshot:
        """Quotes, the primary-timeframe frames, and a frame per extra timeframe.

        ``snap.frames`` is the primary timeframe, as before. ``snap.frames_by_tf``
        holds every requested timeframe including the primary, so a caller
        asks for ``frames_for(alloc.timeframe)`` and gets what that allocation
        was validated on.
        """
        snap = FeedSnapshot()
        now = now_ns if now_ns is not None else wall_ns()
        tfs = self._timeframes(timeframes)
        self.refresh(instruments, now, timeframes=tfs)
        for key, err in self.last_errors.items():
            sym = key.split("@", 1)[0]
            if sym in instruments or sym == "*":
                snap.errors[f"bars:{key}"] = err
        for sym in instruments:
            try:
                q = self.broker.quote(sym)
                snap.quotes[sym] = q
                # The older of two ages: when we received it, and when the
                # VENUE stamped it. A price the venue stamped ten minutes ago
                # and we received just now is ten minutes old, and measuring
                # only receipt let a frozen feed read as fresh forever.
                venue_age = (now - q.ts_ns) / 1e9 if q.ts_ns else 0.0
                if q.ts_ns and q.ts_ns > now + 2_000_000_000:
                    # A quote from the future is a clock problem somewhere;
                    # trading on it is trading on a number nobody can place.
                    snap.quality[sym] = DataQuality.SUSPECT
                snap.ages[sym] = max((now - q.received_ns) / 1e9, venue_age)
            except Exception as exc:  # noqa: BLE001 - a failed quote is data, not a crash
                snap.errors[sym] = f"quote: {exc}"
                snap.quality[sym] = DataQuality.GAP
            for tf in tfs:
                frame = self.store.frame(sym, tf, self.history)
                snap.frames_by_tf.setdefault(tf, {})[sym] = frame
                if tf == self.timeframe:
                    snap.frames[sym] = frame
                    passport = self.store.passport(sym, tf, now_ns=now)
                    snap.passports[sym] = passport
                    snap.quality.setdefault(sym, passport.quality)
                    snap.ages.setdefault(sym, passport.age_sec)
                else:
                    snap.passports_by_tf.setdefault(tf, {})[sym] = \
                        self.store.passport(sym, tf, now_ns=now)
        return snap
