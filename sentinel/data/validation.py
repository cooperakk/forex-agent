"""Strict market-data boundaries. Never repair a price silently.

Every frame that enters the store, the backtester or the replay passes through
here. The checks are the ones whose failure would otherwise become a number:
a NaN close that a rolling mean turns into a NaN indicator that `isfinite`
skips (a silent gap); a high below the open that the adverse-first path
"visits" (a stop touched by a price that never existed); a duplicate timestamp
that doubles a bar's weight; a naive index that lands four hours off.

A frame that fails is refused with the reason. The caller decides what to do
about it -- usually nothing, loudly -- because the alternative, fixing the
row, is how a data problem becomes a trading problem nobody can trace.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd

OHLC = ("open", "high", "low", "close")


def validate_frame(frame: pd.DataFrame, *, name: str = "bars",
                   bid_ask: bool = False, allow_empty: bool = False) -> pd.DataFrame:
    """Refuse a frame that cannot be a bar series. Returns the frame unchanged."""
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"{name}: not a DataFrame")
    if frame.empty:
        if allow_empty:
            return frame
        raise ValueError(f"{name}: empty")
    idx = frame.index
    if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None:
        raise ValueError(f"{name}: a timezone-aware DatetimeIndex (UTC) is required")
    if idx.has_duplicates:
        raise ValueError(f"{name}: duplicate timestamps")
    if not idx.is_monotonic_increasing:
        raise ValueError(f"{name}: timestamps are not increasing")
    required = list(OHLC)
    if bid_ask:
        required += [f"{side}_{f}" for side in ("bid", "ask") for f in OHLC]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(f"{name}: missing columns {missing}")
    prefixes = ["", "bid_", "ask_"] if bid_ask else [""]
    for prefix in prefixes:
        values = frame[[prefix + c for c in OHLC]].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"{name}: non-finite {prefix or 'mid '}prices")
        if (values <= 0).any():
            raise ValueError(f"{name}: non-positive {prefix or 'mid '}prices")
        o, h, l, c = values.T
        bad = (h < np.maximum(o, c)) | (l > np.minimum(o, c)) | (h < l)
        if bad.any():
            where = idx[np.argmax(bad)]
            raise ValueError(f"{name}: impossible OHLC geometry at {where}")
    if bid_ask:
        for f in OHLC:
            if (frame[f"ask_{f}"].to_numpy(dtype=float)
                    < frame[f"bid_{f}"].to_numpy(dtype=float)).any():
                raise ValueError(f"{name}: crossed bid/ask on {f}")
    if "volume" in frame.columns:
        vol = frame["volume"].to_numpy(dtype=float)
        if not np.isfinite(vol).all() or (vol < 0).any():
            raise ValueError(f"{name}: invalid volume")
    return frame


def validate_universe(data: Dict[str, pd.DataFrame], *, bid_ask: bool = False,
                      require_aligned: bool = False) -> Dict[str, pd.DataFrame]:
    """Validate every frame; optionally require identical timestamps.

    Alignment is a separate decision from validity: the backtester aligns on
    the union and treats a missing bar as a gap, while the replay engine needs
    identical indexes because it drives one clock. Both are legitimate, so the
    caller says which.
    """
    if not data:
        raise ValueError("empty market universe")
    first: Optional[pd.DatetimeIndex] = None
    for name, frame in data.items():
        validate_frame(frame, name=name, bid_ask=bid_ask)
        if require_aligned:
            if first is None:
                first = frame.index
            elif not first.equals(frame.index):
                raise ValueError(
                    f"{name}: timestamps differ from the rest of the universe; "
                    "align explicitly before a single-clock replay")
    return data


def has_bid_ask(frame: pd.DataFrame) -> bool:
    return all(f"{side}_{f}" in frame.columns for side in ("bid", "ask") for f in OHLC)
