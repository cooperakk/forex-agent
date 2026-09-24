"""The US dollar index (DXY), rebuilt from the broker's own prices.

ICE's published definition is a geometric average of six exchange rates:

    DXY = 50.14348112 x EURUSD^-0.576 x USDJPY^0.136 x GBPUSD^-0.119
                      x USDCAD^0.091  x USDSEK^0.042 x USDCHF^0.036

so its logarithm is a weighted sum of log prices. Computing it here, rather
than reading a third-party ticker, keeps it on the same clock, the same bars
and the same data-quality checks as everything else the agent trades on.

Many retail servers do not list USD/SEK. The index is then rebuilt from the
components that exist, with their weights rescaled to the same total, and
flagged ``complete=False``: its LEVEL is no longer comparable with the
published DXY, but its returns -- the only thing the features use -- track it
closely, because the missing leg carries 4.2% of the weight.

Every feature is CAUSAL: a value "as of" a time uses only bars that had
CLOSED by then.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

DXY_CONSTANT = 50.14348112
#: canonical instrument -> exponent (EUR and GBP are quoted against the dollar,
#: so a RISE in them is a FALL in the index).
DXY_WEIGHTS: Dict[str, float] = {
    "EUR_USD": -0.576, "USD_JPY": 0.136, "GBP_USD": -0.119,
    "USD_CAD": 0.091, "USD_SEK": 0.042, "USD_CHF": 0.036,
}
TOTAL_WEIGHT = sum(abs(w) for w in DXY_WEIGHTS.values())      # 1.0

_TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400,
               "D1": 86400}


@dataclass
class DxySeries:
    #: log index value per bar START (UTC), oldest first
    log_level: pd.Series
    complete: bool
    used: List[str]
    missing: List[str]
    timeframe: str = "H1"
    #: bar length in nanoseconds (a bar is usable once start + length <= as_of)
    bar_ns: int = 3600 * 10**9
    notes: List[str] = field(default_factory=list)

    @property
    def level(self) -> pd.Series:
        return np.exp(self.log_level)

    def __len__(self) -> int:
        return len(self.log_level)


def build_dxy(closes: Mapping[str, pd.Series], timeframe: str = "H1",
              min_weight: float = 0.75) -> Optional[DxySeries]:
    """The index from per-instrument close series (UTC DatetimeIndex).

    Returns None when the components present carry less than ``min_weight``
    of the index -- without EUR/USD, for instance, there is no dollar index.
    """
    used = [s for s in DXY_WEIGHTS if s in closes and closes[s] is not None
            and len(closes[s]) > 0]
    weight = sum(abs(DXY_WEIGHTS[s]) for s in used)
    if weight < min_weight:
        return None
    frame = pd.concat({s: closes[s].astype(float) for s in used}, axis=1, join="inner")
    frame = frame[(frame > 0).all(axis=1)].sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    if frame.empty:
        return None
    scale = TOTAL_WEIGHT / weight
    log_level = sum(np.log(frame[s]) * DXY_WEIGHTS[s] * scale for s in used)
    complete = len(used) == len(DXY_WEIGHTS)
    if complete:
        log_level = log_level + math.log(DXY_CONSTANT)
    missing = [s for s in DXY_WEIGHTS if s not in used]
    notes = [] if complete else [
        f"rebuilt without {', '.join(missing)}: returns track the published index, "
        "the level does not"]
    return DxySeries(log_level=log_level.rename("log_dxy"), complete=complete, used=used,
                     missing=missing, timeframe=timeframe,
                     bar_ns=_TF_SECONDS.get(timeframe, 3600) * 10**9, notes=notes)


def usd_sign(base: str, quote: str, side_sign: int) -> int:
    """+1 if the trade is LONG the dollar, -1 if short, 0 if the dollar is not a leg.

    BUY USD/JPY is long USD; BUY EUR/USD is short USD; BUY XAU/USD is short USD.
    """
    if base == "USD" and quote != "USD":
        return int(side_sign)
    if quote == "USD" and base != "USD":
        return -int(side_sign)
    return 0


def dxy_state(series: Optional[DxySeries], as_of_ns: int, *, momentum_bars: int = 20,
              window: int = 50) -> Dict[str, float]:
    """Causal state of the dollar as of ``as_of_ns``: empty when unknown.

    * ``dxy_mom``   -- the last ``momentum_bars`` log return in units of its own
      noise (one-bar volatility x sqrt(n)): +2 is a statistically strong rise;
    * ``dxy_z``     -- distance of the index from its ``window``-bar mean in
      standard deviations;
    * ``dxy_vol``   -- annualisation-free one-bar volatility, in basis points.
    """
    if series is None or len(series) == 0:
        return {}
    ends = series.log_level.index.as_unit("ns").asi8 + series.bar_ns
    n_ok = int(np.searchsorted(ends, int(as_of_ns), side="right"))
    need = max(momentum_bars, window) + 2
    if n_ok < need:
        return {}
    x = series.log_level.to_numpy(dtype=float)[:n_ok]
    rets = np.diff(x[-(window + 1):])
    vol = float(np.std(rets, ddof=1)) if rets.size > 2 else 0.0
    if not math.isfinite(vol) or vol <= 0:
        return {}
    mom = (x[-1] - x[-1 - momentum_bars]) / (vol * math.sqrt(momentum_bars))
    tail = x[-window:]
    sd = float(np.std(tail, ddof=1))
    z = (x[-1] - float(np.mean(tail))) / sd if sd > 0 else 0.0
    clip = lambda v: float(max(-4.0, min(4.0, v)))  # noqa: E731
    return {"dxy_mom": round(clip(mom), 4), "dxy_z": round(clip(z), 4),
            "dxy_vol_bp": round(vol * 1e4, 3)}
