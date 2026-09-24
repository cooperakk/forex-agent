"""Strategy interface and indicator toolkit.

A strategy produces ``Signal`` objects. It has no access to the account, the
broker, or position size -- deliberately. Mixing "is there an opportunity" with
"how much should I bet" is how a good signal becomes a blown account, and it
also makes the signal impossible to evaluate on its own.

Every strategy declares its ``lifecycle``, and the risk engine refuses real
money to anything that is not ``accepted``. Declaring is not achieving: the
state changes only when ``research/acceptance.py`` says so.

All indicators here are causal. Each value at bar *t* uses only bars <= *t*.
That property is asserted in ``tests/test_indicators.py``, because a single
centred window is enough to manufacture a beautiful, untradable backtest.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..core.types import Signal, Side


@dataclass(frozen=True)
# FROZEN. The registry refuses to register a class that declares
# lifecycle="accepted", because a plugin file dropped into a watched directory
# would otherwise be a complete bypass of the acceptance protocol. With a
# mutable dataclass that refusal was one line of code away from useless: a
# plugin could register cleanly and then set `meta.lifecycle = "accepted"` on
# itself -- or on a built-in strategy. It never authorised real money (the
# allocation's own lifecycle is what the agent reads, and that still demands a
# stored verdict) but the dashboard reported "accepted", which is its own kind
# of lie.

class StrategyMeta:
    name: str
    version: str = "1.0.0"
    # Which family the strategy belongs to (trend, mean_reversion, breakout,
    # momentum, carry, volatility, session, pattern, ...). This is not
    # decoration: the trial ledger charges a candidate with the search effort
    # spent on its WHOLE family, because picking the best of six trend systems
    # is one search over six trials, not six independent discoveries.
    family: str = "unclassified"
    description: str = ""
    horizon_bars: int = 24
    timeframe: str = "H1"
    lifecycle: str = "hypothesis"
    hypothesis: str = ""
    failure_conditions: List[str] = field(default_factory=list)
    required_history: int = 200
    params_schema: Dict[str, Any] = field(default_factory=dict)


class Strategy(abc.ABC):
    """Base strategy.

    Indicators are computed once per run by ``prepare`` and read row-by-row by
    ``generate``. Recomputing a rolling window inside the bar loop turns a
    backtest into an O(n^2) job and makes a five-year study take hours; it also
    diverges from live behaviour, where indicators are updated incrementally.

    Precomputing is safe *only* because every indicator in this module is
    causal: the value at row ``i`` depends on rows <= ``i``, so reading it at
    ``i`` reveals nothing about ``i+1``. ``tests/test_indicators.py`` asserts
    that property; if an indicator ever stops being causal, precomputation
    would silently become look-ahead, which is why the assertion exists.
    """

    meta: StrategyMeta

    def __init__(self, **params: Any) -> None:
        self.params = {**self.default_params(), **params}
        self._pre: Dict[str, pd.DataFrame] = {}
        self._validate()

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {}

    def _validate(self) -> None:
        return

    # -- indicator precomputation ---------------------------------------- #

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Causal indicator frame aligned to ``df``. Override per strategy."""
        return pd.DataFrame(index=df.index)

    def prepare(self, data: Dict[str, pd.DataFrame]) -> None:
        """Compute the indicator frames, once per DISTINCT input frame.

        The live loop calls this every cycle, and most cycles bring no new
        bar: a 60-second loop on H4 data sees the same frame 240 times in a
        row. Every indicator here is causal, so an unchanged frame has an
        unchanged indicator frame, and recomputing it is pure cost -- in the
        agent replay it was 40% of the run. The key is the frame's shape and
        its first/last stamps and last close, which is what changes when a bar
        arrives or is revised.
        """
        pre: Dict[str, pd.DataFrame] = {}
        keys = getattr(self, "_pre_keys", {})
        for sym, df in data.items():
            key = _frame_key(df)
            if key is not None and keys.get(sym) == key and sym in self._pre:
                pre[sym] = self._pre[sym]
                continue
            pre[sym] = self.indicators(df)
            keys[sym] = key
        self._pre = pre
        self._pre_keys = keys

    def features_at(self, instrument: str, df: pd.DataFrame, index: int) -> pd.Series:
        """Indicator row at ``index``, computing on a window if not prepared."""
        pre = self._pre.get(instrument)
        if pre is not None and len(pre) > index:
            return pre.iloc[index]
        lookback = max(self.meta.required_history, 2)
        start = max(0, index - lookback * 3)
        window = df.iloc[start:index + 1]
        return self.indicators(window).iloc[-1]

    @abc.abstractmethod
    def generate(self, data: Dict[str, pd.DataFrame], instrument: str,
                 index: int) -> Optional[Signal]:
        """Signal for ``instrument`` at bar ``index``.

        Implementations must read only rows <= ``index``. Touching a later row
        is look-ahead and invalidates every downstream statistic.
        """

    def warmup(self) -> int:
        return self.meta.required_history

    def describe(self) -> Dict[str, Any]:
        return {"name": self.meta.name, "version": self.meta.version,
                "lifecycle": self.meta.lifecycle, "params": dict(self.params),
                "horizon_bars": self.meta.horizon_bars,
                "hypothesis": self.meta.hypothesis,
                "failure_conditions": self.meta.failure_conditions}


def _frame_key(df: pd.DataFrame):
    """Cheap identity of a bar frame: (rows, first stamp, last stamp, last close)."""
    try:
        n = len(df)
        if n == 0:
            return (0,)
        return (n, int(df.index[0].value), int(df.index[-1].value), float(df["close"].iloc[-1]))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Causal indicators
# --------------------------------------------------------------------------- #


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average true range. Uses the *previous* close, never the current one."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's RSI.

    Two details that a naive implementation gets wrong, both of which matter:

    * **No losses means RSI = 100, not 50.** Dividing by a zero average loss
      gives NaN, and filling NaN with a neutral 50 reported a one-sided rally
      as perfectly balanced. A strategy gating its shorts on ``rsi >= 68``
      could then never fire in exactly the runaway rally it exists to fade.
    * **The warmup stays NaN.** Filling it with 50 makes the first ``window``
      bars look like real, neutral readings, which defeats the ``isfinite``
      guard every strategy uses to skip its own warmup.
    """
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    warm = gain.notna() & loss.notna()
    rs = gain / loss.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    flat = warm & (gain == 0.0) & (loss == 0.0)
    # Zero average loss during a valid (warmed-up) window is RSI 100 --
    # but only if there were gains. A series that has not moved AT ALL is
    # neither overbought nor oversold, and calling a dead or forward-filled
    # feed "maximum overbought" is exactly the wrong answer for any rule
    # gated on `rsi >= threshold`.
    out = out.where(~(warm & (loss == 0.0) & (gain > 0.0)), 100.0)
    # Zero average gain during a valid window is RSI 0.
    out = out.where(~(warm & (gain == 0.0) & (loss > 0.0)), 0.0)
    out = out.where(~flat, 50.0)
    return out.where(warm)


def donchian(df: pd.DataFrame, window: int) -> tuple[pd.Series, pd.Series]:
    """Channel from the ``window`` bars BEFORE the current one.

    The shift is the whole point: comparing today's high to a channel that
    includes today's high produces a breakout on every bar.
    """
    upper = df["high"].rolling(window, min_periods=window).max().shift(1)
    lower = df["low"].rolling(window, min_periods=window).min().shift(1)
    return upper, lower


def zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window, min_periods=window).mean()
    sd = series.rolling(window, min_periods=window).std(ddof=1)
    return (series - mean) / sd.replace(0.0, np.nan)


def adx(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Trend strength. Used to gate a trend system out of a range."""
    high, low = df["high"], df["low"]
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = atr(df, window)
    alpha = 1.0 / window
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=alpha, adjust=False, min_periods=window).mean() / tr.replace(0.0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=alpha, adjust=False, min_periods=window).mean() / tr.replace(0.0, np.nan)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)) * 100
    return dx.ewm(alpha=alpha, adjust=False, min_periods=window).mean()


def realised_vol(series: pd.Series, window: int = 50) -> pd.Series:
    return np.log(series).diff().rolling(window, min_periods=window).std(ddof=1)


def hurst_exponent(series: pd.Series, max_lag: int = 40) -> float:
    """Rough regime discriminator. > 0.5 trending, < 0.5 mean-reverting.

    Deliberately used only as a *descriptive* label in the regime panel, never
    as a trading trigger: the estimator is noisy on short windows and its
    sampling distribution on real FX data is wide enough that thresholding it
    would be closer to a coin flip than a filter.
    """
    x = np.asarray(series.dropna(), dtype=float)
    if x.size < max_lag * 2:
        return 0.5
    lags = range(2, max_lag)
    tau = []
    for lag in lags:
        diff = x[lag:] - x[:-lag]
        sd = np.std(diff)
        tau.append(sd if sd > 0 else 1e-12)
    try:
        poly = np.polyfit(np.log(list(lags)), np.log(tau), 1)
        return float(poly[0])
    except (ValueError, np.linalg.LinAlgError):  # pragma: no cover
        return 0.5


def session_of(ts: pd.Timestamp) -> str:
    h = ts.hour
    if 7 <= h < 12:
        return "london"
    if 12 <= h < 16:
        return "overlap"
    if 16 <= h < 21:
        return "newyork"
    return "asia"


# --------------------------------------------------------------------------- #
# Causal indicators, part two: the ones the extended family library needs
#
# Every function below obeys the same contract as the block above -- the value
# at row ``t`` is a function of rows <= ``t`` only -- and every one of them is
# in the truncation test in ``tests/test_indicators.py``. The contract is not a
# style preference. The backtester precomputes these over the whole series, so
# a single non-causal line here would not raise, would not look wrong, and
# would quietly make every statistic downstream a fiction.
# --------------------------------------------------------------------------- #


def bollinger(series: pd.Series, window: int = 20,
              k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(mid, upper, lower) from a trailing window that INCLUDES the current bar.

    Including bar ``t`` is correct here and wrong for ``donchian``: a Bollinger
    band is a statement about where the current price sits inside its own
    recent distribution, and the decision is taken on the closed bar. A channel
    breakout is a statement about exceeding a level that existed *before* the
    bar, which is why that one is shifted and this one is not.
    """
    mid = series.rolling(window, min_periods=window).mean()
    sd = series.rolling(window, min_periods=window).std(ddof=1)
    return mid, mid + k * sd, mid - k * sd


def keltner(df: pd.DataFrame, window: int = 20, atr_window: int = 20,
            k: float = 1.5) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(mid, upper, lower) around an EMA, widened by ATR rather than by sigma."""
    mid = ema(df["close"], window)
    a = atr(df, atr_window)
    return mid, mid + k * a, mid - k * a


def squeeze_ratio(df: pd.DataFrame, window: int = 20, atr_window: int = 20,
                  bb_k: float = 2.0, kc_k: float = 1.5) -> pd.Series:
    """Bollinger width divided by Keltner width.

    Below 1.0 the bands sit inside the channel -- the "squeeze" that volatility
    contraction systems trade. It is a ratio of two volatility estimates, which
    is deliberate: an absolute band width is not comparable across instruments
    or across the volatility regimes of one instrument.
    """
    _, bb_u, bb_l = bollinger(df["close"], window, bb_k)
    _, kc_u, kc_l = keltner(df, window, atr_window, kc_k)
    kc_width = (kc_u - kc_l).replace(0.0, np.nan)
    return (bb_u - bb_l) / kc_width


def kama(series: pd.Series, er_window: int = 10, fast: int = 2,
         slow: int = 30) -> pd.Series:
    """Kaufman adaptive moving average.

    The smoothing constant moves with the efficiency ratio -- net change over
    the sum of absolute changes -- so the average tracks price closely in a
    directional move and flattens in chop. The recursion is strictly forward:
    ``kama[t]`` depends on ``kama[t-1]`` and on prices up to ``t``.

    Implemented as an explicit loop over a numpy array. A vectorised form does
    not exist for a state-dependent smoothing constant, and the loop runs once
    per series in ``prepare()``, not once per bar in the trading loop.
    """
    x = series.astype(float).to_numpy()
    n = x.size
    out = np.full(n, np.nan)
    if n <= er_window:
        return pd.Series(out, index=series.index)
    fast_sc = 2.0 / (fast + 1.0)
    slow_sc = 2.0 / (slow + 1.0)
    change = np.abs(x[er_window:] - x[:-er_window])
    volatility = pd.Series(np.abs(np.diff(x, prepend=x[0]))).rolling(
        er_window, min_periods=er_window).sum().to_numpy()
    out[er_window] = x[er_window]
    for t in range(er_window + 1, n):
        vol = volatility[t]
        er = 0.0 if not np.isfinite(vol) or vol <= 0 else change[t - er_window] / vol
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        prev = out[t - 1]
        out[t] = prev + sc * (x[t] - prev)
    return pd.Series(out, index=series.index)


def supertrend(df: pd.DataFrame, atr_window: int = 10,
               multiplier: float = 3.0) -> tuple[pd.Series, pd.Series]:
    """(line, direction) -- ATR bands that ratchet and flip.

    ``direction`` is +1 while price holds above the lower band and -1 while it
    holds below the upper one. The ratchet is the substance: the band only ever
    moves in the favourable direction while the trend is intact, so the flip
    happens on a genuine give-back rather than on noise.

    Like ``kama`` this is a forward recursion, so truncating the future cannot
    change the past, and the loop lives in ``prepare()``.
    """
    a = atr(df, atr_window).to_numpy()
    close = df["close"].astype(float).to_numpy()
    hl2 = ((df["high"].astype(float) + df["low"].astype(float)) / 2.0).to_numpy()
    n = close.size
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    line = np.full(n, np.nan)
    direction = np.full(n, np.nan)
    started = False
    for t in range(n):
        if not np.isfinite(a[t]):
            continue
        basic_up = hl2[t] + multiplier * a[t]
        basic_low = hl2[t] - multiplier * a[t]
        if not started:
            upper[t], lower[t] = basic_up, basic_low
            direction[t] = 1.0 if close[t] >= basic_low else -1.0
            line[t] = lower[t] if direction[t] > 0 else upper[t]
            started = True
            continue
        prev_up, prev_low = upper[t - 1], lower[t - 1]
        upper[t] = basic_up if (basic_up < prev_up or close[t - 1] > prev_up) else prev_up
        lower[t] = basic_low if (basic_low > prev_low or close[t - 1] < prev_low) else prev_low
        prev_dir = direction[t - 1]
        if prev_dir > 0:
            direction[t] = -1.0 if close[t] < lower[t] else 1.0
        else:
            direction[t] = 1.0 if close[t] > upper[t] else -1.0
        line[t] = lower[t] if direction[t] > 0 else upper[t]
    return (pd.Series(line, index=df.index), pd.Series(direction, index=df.index))


def ichimoku(df: pd.DataFrame, tenkan: int = 9, kijun: int = 26,
             senkou_b: int = 52, displacement: int = 26) -> pd.DataFrame:
    """Tenkan, Kijun and the two cloud edges, *as observable at each bar*.

    The two spans are shifted FORWARD by ``displacement``: the cloud printed at
    bar ``t`` was computed from data at ``t - displacement``. That is the
    direction that keeps it causal, and it is the direction the indicator is
    actually defined in.

    The chikou span -- close shifted BACKWARD -- is deliberately absent. Every
    published "price above chikou" rule reads a price from ``t + 26`` while
    pretending to stand at ``t``. It is the single most common look-ahead bug
    in retail Ichimoku systems, it backtests beautifully, and it cannot be
    traded. Leaving it out of the toolkit is cheaper than trusting each caller
    to shift it correctly.
    """
    high, low = df["high"], df["low"]

    def mid_channel(window: int) -> pd.Series:
        return (high.rolling(window, min_periods=window).max()
                + low.rolling(window, min_periods=window).min()) / 2.0

    conv = mid_channel(tenkan)
    base = mid_channel(kijun)
    span_a = ((conv + base) / 2.0).shift(displacement)
    span_b = mid_channel(senkou_b).shift(displacement)
    return pd.DataFrame({
        "tenkan": conv, "kijun": base,
        "span_a": span_a, "span_b": span_b,
        "cloud_top": pd.concat([span_a, span_b], axis=1).max(axis=1),
        "cloud_bottom": pd.concat([span_a, span_b], axis=1).min(axis=1),
    }, index=df.index)


def rolling_percentile(series: pd.Series, window: int = 100) -> pd.Series:
    """Percentile rank of the current value inside its own trailing window.

    In [0, 1], with the current bar included. Used wherever a threshold has to
    be comparable across instruments and across regimes: "ATR above 2.5" means
    nothing on USD/JPY and EUR/USD at once, "ATR in its top decile" means the
    same thing everywhere.

    An expanding or full-sample quantile is the classic look-ahead in this
    spot; the window here is trailing and closes at the current bar.
    """
    return series.rolling(window, min_periods=window).rank(pct=True)


def rolling_corr(a: pd.Series, b: pd.Series, window: int = 100) -> pd.Series:
    """Trailing correlation of two aligned series. Estimate, not a fact.

    Correlation is the input that breaks exactly when it matters: pairs that
    have run together for a year decouple on the day one of the two central
    banks moves. Anything built on this must have a stop that does not depend
    on the relationship holding.
    """
    joined = pd.concat([a.astype(float), b.astype(float)], axis=1).dropna()
    if joined.empty:
        return pd.Series(np.nan, index=a.index)
    ra = joined.iloc[:, 0].pct_change()
    rb = joined.iloc[:, 1].pct_change()
    return ra.rolling(window, min_periods=window).corr(rb).reindex(a.index)


def previous_day_extremes(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(prev_day_high, prev_day_low) broadcast to every bar of the next day.

    The shift is by CALENDAR DAY, not by bar count: every bar of Tuesday sees
    Monday's completed range and nothing of Tuesday's. Shifting by a fixed
    number of bars instead would leak part of the current day into its own
    "previous day" level on any session with an irregular bar count -- which,
    with weekends and holidays, is most of them.
    """
    day = pd.Series(df.index.floor("D"), index=df.index)
    prev_high = df["high"].groupby(day).max().shift(1)
    prev_low = df["low"].groupby(day).min().shift(1)
    return day.map(prev_high), day.map(prev_low)


def session_key(index: pd.DatetimeIndex) -> pd.Series:
    """A distinct label per (calendar day, session), for grouping intraday bars.

    The day component is what makes this usable as a grouping key: "london" on
    its own would merge every Monday's London session with every Tuesday's, so
    an opening range built on it would span the whole sample.
    """
    idx = pd.DatetimeIndex(index)
    sessions = pd.Series([session_of(ts) for ts in idx], index=idx)
    return pd.Series(idx.floor("D").astype(str), index=idx) + "|" + sessions


def opening_range(df: pd.DataFrame, bars: int = 2,
                  session: Optional[str] = None) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(range_high, range_low, bars_since_session_open) for each bar.

    The range is the high/low of the first ``bars`` bars of the session, and it
    is only *complete* once ``bars_since_open >= bars`` -- which is the
    condition every caller must check. While the range is still forming the
    returned values are the running extremes, and a breakout of a range that
    includes the breaking bar is not a breakout at all.

    ``session`` restricts the grouping to one named session (london, newyork,
    asia, overlap); ``None`` uses the calendar day, which is the right grouping
    for a daily opening-range on a 24-hour market.
    """
    idx = pd.DatetimeIndex(df.index)
    names = pd.Series([session_of(ts) for ts in idx], index=df.index)
    if session is None:
        key = pd.Series(idx.floor("D").astype(str), index=df.index)
        mask = None
    else:
        key = session_key(idx).set_axis(df.index)
        # Bars outside the requested session get their own per-bar key so they
        # never join a group and never contribute to a range.
        mask = names == session
        key = key.where(mask, pd.Series(np.arange(len(df)), index=df.index).astype(str))
    pos = df.groupby(key).cumcount()
    in_range = pos < bars
    hi = df["high"].where(in_range).groupby(key).cummax().groupby(key).ffill()
    lo = df["low"].where(in_range).groupby(key).cummin().groupby(key).ffill()
    if mask is not None:
        hi, lo, pos = hi.where(mask), lo.where(mask), pos.where(mask)
    return hi, lo, pos


def inside_bar(df: pd.DataFrame) -> pd.Series:
    """True when this bar's range sits entirely inside the previous bar's."""
    return (df["high"] < df["high"].shift(1)) & (df["low"] > df["low"].shift(1))


def engulfing(df: pd.DataFrame) -> pd.Series:
    """+1 bullish engulfing, -1 bearish, 0 otherwise.

    Body-based, not range-based: the wicks of an FX bar are mostly a function
    of which venue's feed you are looking at, while the open-to-close body is
    the part every feed agrees on.
    """
    o, c = df["open"].astype(float), df["close"].astype(float)
    po, pc = o.shift(1), c.shift(1)
    body_top, body_bottom = np.maximum(o, c), np.minimum(o, c)
    prev_top, prev_bottom = np.maximum(po, pc), np.minimum(po, pc)
    bull = (c > o) & (pc < po) & (body_bottom <= prev_bottom) & (body_top >= prev_top)
    bear = (c < o) & (pc > po) & (body_bottom <= prev_bottom) & (body_top >= prev_top)
    out = pd.Series(0.0, index=df.index)
    out = out.mask(bull, 1.0).mask(bear, -1.0)
    return out.where(o.notna() & po.notna())
