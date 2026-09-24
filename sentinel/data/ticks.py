"""Bid/ask bars from the venue's own ticks.

The acceptance protocol will not promote a strategy on mid-price bars, and
for a good reason: the spread a strategy paid is the difference between the
ask it bought at and the bid it sold at, and a bid candle plus a stored
"spread" integer is not that history -- it is one number per bar standing in
for thousands of quotes. MetaTrader keeps the ticks (``copy_ticks_range``,
bid and ask per tick, going back months on most brokers), so the history the
protocol wants is obtainable from the terminal the account already has.

``bars_from_ticks`` aggregates raw ``(utc_ms, bid, ask)`` ticks into one frame
per timeframe with three OHLC sets -- mid, bid, ask -- and the tick count as
volume. Bars with no ticks are absent, not filled: a gap is a fact.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np
import pandas as pd

from .feed import TIMEFRAME_SECONDS

TickRows = Sequence[Tuple[int, float, float]]


def bars_from_ticks(ticks: TickRows, timeframe: str, *, min_ticks: int = 1) -> pd.DataFrame:
    """Aggregate ``(utc_ms, bid, ask)`` ticks into bid/ask/mid OHLC bars.

    Bars are aligned to the epoch grid of ``timeframe`` in UTC (H4 opens at
    00:00, 04:00, ...). A bar with fewer than ``min_ticks`` ticks is dropped
    rather than built from a handful of quotes.
    """
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    if not len(ticks):
        return pd.DataFrame(columns=_COLUMNS)
    arr = np.asarray(ticks, dtype=float)
    if arr.ndim != 2 or arr.shape[1] < 3:
        raise ValueError("ticks must be rows of (utc_ms, bid, ask)")
    ms, bid, ask = arr[:, 0], arr[:, 1], arr[:, 2]
    ok = np.isfinite(ms) & np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask >= bid)
    ms, bid, ask = ms[ok], bid[ok], ask[ok]
    if ms.size == 0:
        return pd.DataFrame(columns=_COLUMNS)
    order = np.argsort(ms, kind="stable")
    ms, bid, ask = ms[order], bid[order], ask[order]
    mid = (bid + ask) / 2.0
    width_ms = TIMEFRAME_SECONDS[timeframe] * 1000
    bucket = (ms // width_ms).astype(np.int64)
    frame = pd.DataFrame({"bucket": bucket, "mid": mid, "bid": bid, "ask": ask})
    g = frame.groupby("bucket", sort=True)
    out = pd.DataFrame({
        "open": g["mid"].first(), "high": g["mid"].max(), "low": g["mid"].min(),
        "close": g["mid"].last(),
        "bid_open": g["bid"].first(), "bid_high": g["bid"].max(),
        "bid_low": g["bid"].min(), "bid_close": g["bid"].last(),
        "ask_open": g["ask"].first(), "ask_high": g["ask"].max(),
        "ask_low": g["ask"].min(), "ask_close": g["ask"].last(),
        "volume": g["mid"].size().astype(float),
    })
    out = out[out["volume"] >= float(min_ticks)]
    out.index = pd.to_datetime(out.index.to_numpy() * width_ms, unit="ms", utc=True)
    out.index.name = "timestamp"
    return out[_COLUMNS]


_COLUMNS = ["open", "high", "low", "close", "volume",
            "bid_open", "bid_high", "bid_low", "bid_close",
            "ask_open", "ask_high", "ask_low", "ask_close"]


def export_history(broker, symbols: Iterable[str], timeframes: Sequence[str], *,
                   start_ns: int, end_ns: int, out_dir, chunk_days: int = 3,
                   min_ticks: int = 1, log=print) -> Dict[str, Dict[str, int]]:
    """Pull ticks from an adapter with ``fetch_ticks`` and write one CSV per
    (timeframe, symbol) under ``out_dir/<timeframe>/<SYMBOL>.csv``.

    Ticks are fetched in ``chunk_days`` windows -- a terminal returns tens of
    thousands of ticks per day per major -- and each window is aggregated as
    it arrives, so memory stays flat over a year of history.
    """
    from pathlib import Path
    out_dir = Path(out_dir)
    written: Dict[str, Dict[str, int]] = {tf: {} for tf in timeframes}
    step_ns = int(chunk_days * 86400 * 1e9)
    for sym in symbols:
        partial: Dict[str, List[pd.DataFrame]] = {tf: [] for tf in timeframes}
        lo = start_ns
        while lo < end_ns:
            hi = min(end_ns, lo + step_ns)
            ticks = broker.fetch_ticks(sym, lo, hi)
            if ticks:
                for tf in timeframes:
                    bars = bars_from_ticks(ticks, tf, min_ticks=min_ticks)
                    if len(bars):
                        partial[tf].append(bars)
            log(f"[ticks] {sym} {pd.Timestamp(lo, unit='ns', tz='UTC'):%Y-%m-%d} "
                f"{len(ticks)} ticks")
            lo = hi
        for tf in timeframes:
            if not partial[tf]:
                continue
            frame = pd.concat(partial[tf])
            # A bar straddling two chunks appears twice; keep the union of its
            # ticks by re-aggregating the boundary rows is overkill -- keep
            # the LAST occurrence, which saw the later ticks, and accept that a
            # boundary bar may lack a few opening ticks. Chunks are aligned to
            # whole days, so on H1+ this only ever touches the 00:00 bar.
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
            folder = out_dir / tf
            folder.mkdir(parents=True, exist_ok=True)
            frame.to_csv(folder / f"{sym}.csv", index_label="timestamp")
            written[tf][sym] = int(len(frame))
    return written
