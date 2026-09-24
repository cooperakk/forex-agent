"""A synthetic market that runs in wall-clock time, for the paper venue.

``PaperBroker`` is a fill engine, not a price source: it evaluates stops,
margin and financing against whatever quote it is handed, and hands nothing to
itself. The backtester and ``scripts/run_paper_sim.py`` drive it from a frame
of synthetic bars. ``scripts/serve.py`` did not -- so the shipped default
(``broker=paper``) produced a dashboard, a heartbeat and an audit chain, and a
``no_price`` veto on every cycle, forever.

This driver closes that gap for the paper venue only. On each tick it

1. generates, once per instrument, a deterministic synthetic path at the
   FINEST timeframe anyone has asked for, whose last completed bar ends at the
   most recent boundary of that timeframe;
2. derives every coarser timeframe by resampling that one path, so an H4
   strategy and a D1 strategy on the same instrument see the same market and
   the quote the simulator fills at is the same price both were looking at;
3. writes every bar that has closed since the previous tick into the store,
   labelled ``synthetic`` so the acceptance protocol (gate L10) and the trial
   ledger can never mistake it for market data;
4. marks the paper broker with a price for the bar still forming, so the
   simulator's stop/target/margin logic runs against a moving quote.

It is deliberately not a random walk seeded from the clock: the same seed
produces the same market on every restart, which is what makes a paper run
reproducible enough to compare two configurations on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import pandas as pd

from ..core.clock import wall_ns
from ..core.money import dec
from .feed import TIMEFRAME_SECONDS, BarStore, bars_from_frame
from .synthetic import DEFAULT_UNIVERSE, MarketSpec, generate_series

#: Plausible starting levels for instruments the default universe does not
#: name, so any paper instrument set gets a series in the right neighbourhood.
_START_PRICES = {
    "EUR_USD": 1.0850, "GBP_USD": 1.2700, "AUD_USD": 0.6600, "NZD_USD": 0.6100,
    "USD_JPY": 150.00, "USD_CHF": 0.8800, "USD_CAD": 1.3600, "EUR_JPY": 162.00,
    "EUR_GBP": 0.8550, "GBP_JPY": 190.00,
}

_OHLCV = ["open", "high", "low", "close", "volume"]


@dataclass
class _Series:
    base_tf: str
    base: pd.DataFrame                              # finest-timeframe path
    derived: Dict[str, pd.DataFrame] = field(default_factory=dict)
    stored_upto: Dict[str, int] = field(default_factory=dict)   # tf -> last row stored


class SyntheticMarketDriver:
    def __init__(self, broker, store: BarStore, *, timeframe: str = "H4",
                 history: int = 1500, seed: int = 20260914,
                 future_bars: int = 4000) -> None:
        self.broker = broker
        self.store = store
        self.timeframe = timeframe
        self.history = max(50, int(history))
        self.seed = int(seed)
        self.future_bars = max(10, int(future_bars))
        self._series: Dict[str, _Series] = {}

    # -- construction ---------------------------------------------------- #

    def _spec_for(self, symbol: str) -> MarketSpec:
        for spec in DEFAULT_UNIVERSE:
            if spec.symbol == symbol:
                return spec
        inst = self.broker.instruments().get(symbol)
        pip = float(inst.pip) if inst is not None else 0.0001
        return MarketSpec(symbol, _START_PRICES.get(symbol, 1.0), pip=pip)

    def _build(self, symbol: str, now_ns: int, base_tf: str) -> _Series:
        interval = TIMEFRAME_SECONDS[base_tf] * 1_000_000_000
        # The newest COMPLETED bar ends on the boundary at or before now; the
        # series then continues into the future so the driver has bars to
        # release for as long as the process runs. `history` is in PRIMARY
        # bars, so the base series is long enough for the coarsest timeframe.
        coarsest = max(TIMEFRAME_SECONDS[t] for t in (self.timeframe, base_tf))
        ratio = max(1, coarsest // TIMEFRAME_SECONDS[base_tf])
        n_hist = self.history * ratio
        boundary = (now_ns // interval) * interval
        n = n_hist + self.future_bars * ratio
        first_start = boundary - n_hist * interval
        bars_per_day = max(1, 86400 // TIMEFRAME_SECONDS[base_tf])
        spec = self._spec_for(symbol)
        # A per-symbol seed, so two instruments are not the same path.
        offset = sum(ord(c) for c in symbol) * 7919
        df = generate_series(spec, n_bars=n, bars_per_day=bars_per_day,
                             seed=self.seed + offset)
        df.index = pd.date_range(pd.Timestamp(first_start, unit="ns", tz="UTC"),
                                 periods=n, freq=pd.Timedelta(interval, unit="ns"))
        return _Series(base_tf=base_tf, base=df[_OHLCV].copy())

    @staticmethod
    def _resample(base: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """Coarser bars from the base path, aligned to the epoch grid."""
        rule = pd.Timedelta(TIMEFRAME_SECONDS[timeframe], unit="s")
        agg = base.resample(rule, origin="epoch", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last",
             "volume": "sum"})
        return agg.dropna(subset=["open"])

    def _frame(self, series: _Series, timeframe: str) -> pd.DataFrame:
        if timeframe == series.base_tf:
            return series.base
        cached = series.derived.get(timeframe)
        if cached is None:
            cached = series.derived[timeframe] = self._resample(series.base, timeframe)
        return cached

    # -- ticking ---------------------------------------------------------- #

    def tick(self, now_ns: Optional[int], instruments: Sequence[str],
             timeframes: Optional[Sequence[str]] = None) -> Dict[str, int]:
        """Release closed bars for every timeframe and mark the forming price.

        Returns new bars stored per "instrument@timeframe".
        """
        now = now_ns if now_ns is not None else wall_ns()
        tfs: List[str] = [self.timeframe]
        for tf in timeframes or ():
            if tf in TIMEFRAME_SECONDS and tf not in tfs:
                tfs.append(tf)
        base_tf = min(tfs, key=lambda t: TIMEFRAME_SECONDS[t])
        out: Dict[str, int] = {}
        for sym in instruments:
            if sym not in self.broker.instruments():
                continue
            series = self._series.get(sym)
            if series is None or TIMEFRAME_SECONDS[series.base_tf] > TIMEFRAME_SECONDS[base_tf]:
                # First sight, or a finer timeframe than the path was built on:
                # rebuild finer. Derived frames follow from the new base.
                series = self._series[sym] = self._build(sym, now, base_tf)
            for tf in tfs:
                frame = self._frame(series, tf)
                interval = TIMEFRAME_SECONDS[tf] * 1_000_000_000
                starts = frame.index.as_unit("ns").asi8
                closed = int(((starts + interval) <= now).sum())
                upto = series.stored_upto.get(tf, -1)
                if closed - 1 > upto:
                    new = frame.iloc[upto + 1:closed]
                    if len(new):
                        out[f"{sym}@{tf}"] = self.store.upsert(
                            bars_from_frame(new, sym, tf, source="synthetic"))
                    series.stored_upto[tf] = closed - 1
            # The forming BASE bar, marked at an interpolated price. Every
            # coarser bar's forming portion is made of these same base bars,
            # so one quote is consistent with all of them.
            base = series.base
            interval = TIMEFRAME_SECONDS[series.base_tf] * 1_000_000_000
            starts = base.index.as_unit("ns").asi8
            idx = int(((starts + interval) <= now).sum())
            if idx < len(base):
                row = base.iloc[idx]
                start = int(starts[idx])
                frac = min(1.0, max(0.0, (now - start) / interval))
                mid = _path_price(float(row["open"]), float(row["high"]),
                                  float(row["low"]), float(row["close"]), frac, start)
                self.broker.mark_mid(sym, dec(round(mid, 6)), now)
        return out


def _path_price(o: float, h: float, l: float, c: float, frac: float, salt: int) -> float:
    """A deterministic price inside the forming bar at fraction ``frac`` of it.

    Open to close on a straight line, with a sinusoidal excursion toward the
    bar's high or low in the middle, so a stop inside the range can actually
    be touched during the bar rather than only at its close.
    """
    base = o + (c - o) * frac
    up = max(0.0, h - max(o, c))
    down = max(0.0, min(o, c) - l)
    phase = (salt // 1_000_000_007) % 7 / 7.0
    swing = math.sin(math.pi * frac + phase)
    wiggle = up * max(0.0, swing) * math.sin(math.pi * frac) \
        - down * max(0.0, -swing) * math.sin(math.pi * frac)
    return min(h, max(l, base + wiggle))
