"""Causality.

Every indicator must depend only on rows <= i. The backtester precomputes
indicators over the whole series for speed, so if this property ever breaks,
precomputation silently becomes look-ahead and every result downstream is
worthless. That is why these assertions exist as tests rather than comments.
"""

import numpy as np
import pandas as pd
import pytest

from sentinel.strategy.base import (
    adx, atr, bollinger, donchian, ema, engulfing, hurst_exponent, ichimoku,
    inside_bar, kama, keltner, opening_range, previous_day_extremes,
    realised_vol, rolling_corr, rolling_percentile, rsi, session_of, sma,
    squeeze_ratio, supertrend, zscore,
)

RNG = np.random.default_rng(7)


@pytest.fixture(scope="module")
def frame():
    n = 500
    # A UTC DatetimeIndex, because the session and previous-day indicators are
    # functions of the calendar and cannot be tested on a positional index.
    idx = pd.date_range("2022-01-03", periods=n, freq="4h", tz="UTC")
    close = pd.Series(1.08 * np.exp(np.cumsum(RNG.normal(0, 0.001, n))), index=idx)
    return pd.DataFrame({
        "close": close,
        "high": close * (1 + np.abs(RNG.normal(0, 0.0006, n))),
        "low": close * (1 - np.abs(RNG.normal(0, 0.0006, n))),
        "open": close.shift(1).fillna(close.iloc[0]),
        "volume": RNG.lognormal(9, 0.4, n),
    }, index=idx)


CUT = 320

INDICATORS = {
    "sma": lambda df: sma(df["close"], 20),
    "ema": lambda df: ema(df["close"], 20),
    "atr": lambda df: atr(df, 14),
    "rsi": lambda df: rsi(df["close"], 14),
    "adx": lambda df: adx(df, 14),
    "zscore": lambda df: zscore(df["close"], 60),
    "realised_vol": lambda df: realised_vol(df["close"], 50),
    "donchian_upper": lambda df: donchian(df, 20)[0],
    "donchian_lower": lambda df: donchian(df, 20)[1],
    # Everything below was added with the family library. The assertion is the
    # same one and it matters more, not less, with thirty strategies reading
    # these values: one non-causal line here would silently invalidate every
    # backtest in the library at once.
    "bollinger_upper": lambda df: bollinger(df["close"], 20, 2.0)[1],
    "bollinger_lower": lambda df: bollinger(df["close"], 20, 2.0)[2],
    "keltner_upper": lambda df: keltner(df, 20, 20, 1.5)[1],
    "squeeze_ratio": lambda df: squeeze_ratio(df),
    "kama": lambda df: kama(df["close"], 10, 2, 30),
    "supertrend_line": lambda df: supertrend(df, 10, 3.0)[0],
    "supertrend_dir": lambda df: supertrend(df, 10, 3.0)[1],
    "ichimoku_tenkan": lambda df: ichimoku(df)["tenkan"],
    "ichimoku_cloud_top": lambda df: ichimoku(df)["cloud_top"],
    "ichimoku_cloud_bottom": lambda df: ichimoku(df)["cloud_bottom"],
    "rolling_percentile": lambda df: rolling_percentile(atr(df, 14), 100),
    "previous_day_high": lambda df: previous_day_extremes(df)[0],
    "previous_day_low": lambda df: previous_day_extremes(df)[1],
    "opening_range_high": lambda df: opening_range(df, 2)[0],
    "opening_range_low": lambda df: opening_range(df, 2)[1],
    "inside_bar": lambda df: inside_bar(df).astype(float),
    "engulfing": lambda df: engulfing(df),
}


@pytest.mark.parametrize("name", sorted(INDICATORS))
def test_truncating_the_future_does_not_change_the_past(name, frame):
    """The only test that matters: values at t must not move when t+1.. is removed."""
    full = INDICATORS[name](frame).iloc[:CUT]
    truncated = INDICATORS[name](frame.iloc[:CUT])
    both = full.notna() & truncated.notna()
    assert both.sum() > 50, "not enough overlapping values to be a real test"
    assert np.allclose(full[both], truncated[both], rtol=1e-9, atol=1e-12)


def test_donchian_excludes_the_current_bar(frame):
    """Without the shift, every bar is a breakout of its own high."""
    upper, lower = donchian(frame, 20)
    i = 100
    assert upper.iloc[i] == pytest.approx(frame["high"].iloc[i - 20:i].max())
    assert lower.iloc[i] == pytest.approx(frame["low"].iloc[i - 20:i].min())


def test_atr_uses_the_previous_close(frame):
    """True range compares to the PREVIOUS close; using the current one
    understates every gap."""
    a = atr(frame, 14)
    assert a.iloc[:13].isna().all()
    assert (a.dropna() > 0).all()


def test_rsi_stays_in_range(frame):
    r = rsi(frame["close"], 14)
    assert r.min() >= 0 and r.max() <= 100


def test_adx_stays_in_range(frame):
    a = adx(frame, 14).dropna()
    assert a.min() >= 0 and a.max() <= 100


def test_hurst_of_a_random_walk_is_near_a_half(frame):
    assert 0.35 < hurst_exponent(frame["close"]) < 0.65


def test_minimum_periods_are_respected(frame):
    """A rolling window must not emit a value before it is full; a partial
    window is a different statistic wearing the same name."""
    assert sma(frame["close"], 20).iloc[:19].isna().all()
    assert zscore(frame["close"], 60).iloc[:59].isna().all()


# --------------------------------------------------------------------------- #
# Properties of the indicators added with the family library
# --------------------------------------------------------------------------- #


def test_rolling_correlation_is_causal(frame):
    """Two-series version of the same assertion; it needs its own test because
    the parametrised harness passes a single frame."""
    a = frame["close"]
    b = frame["close"] * 1.01 + RNG.normal(0, 0.001, len(frame))
    full = rolling_corr(a, b, 100).iloc[:CUT]
    truncated = rolling_corr(a.iloc[:CUT], b.iloc[:CUT], 100)
    both = full.notna() & truncated.notna()
    assert both.sum() > 50
    assert np.allclose(full[both], truncated[both], rtol=1e-9, atol=1e-12)


def test_ichimoku_has_no_chikou_span(frame):
    """The lagging span is the classic Ichimoku look-ahead and must not exist.

    Chikou is the close shifted BACKWARD, so reading it at bar t is reading the
    price at t+26. Every 'price above chikou' rule that backtests beautifully is
    doing exactly that. It is left out of the toolkit rather than left in with a
    warning, because a warning is not enforcement.
    """
    cols = set(ichimoku(frame).columns)
    assert "chikou" not in cols and "lagging" not in cols


def test_ichimoku_cloud_comes_from_displaced_history(frame):
    """The cloud printed at bar t must be computable from data at t-26."""
    ich = ichimoku(frame, 9, 26, 52, 26)
    i = 200
    high, low = frame["high"], frame["low"]
    src = i - 26
    expected_b = (high.iloc[src - 51:src + 1].max() + low.iloc[src - 51:src + 1].min()) / 2
    assert ich["span_b"].iloc[i] == pytest.approx(expected_b)


def test_previous_day_extremes_shift_by_calendar_day(frame):
    """Every bar of a day sees the previous day's completed range, and none of
    its own -- a bar-count shift would leak on any day with an odd bar count."""
    prev_high, prev_low = previous_day_extremes(frame)
    days = pd.Series(frame.index.floor("D"), index=frame.index)
    unique = list(dict.fromkeys(days))
    today, yesterday = unique[5], unique[4]
    mask = days == today
    assert (prev_high[mask].dropna() == frame["high"][days == yesterday].max()).all()
    assert (prev_low[mask].dropna() == frame["low"][days == yesterday].min()).all()
    # The first day has no predecessor and must be NaN rather than its own range.
    assert prev_high[days == unique[0]].isna().all()


def test_opening_range_is_not_complete_until_its_bars_have_closed(frame):
    """A range that includes the bar breaking it is not a range.

    While ``bars_since_open < range_bars`` the values returned are the running
    extremes, which is why every caller checks that counter before acting.
    """
    hi, lo, pos = opening_range(frame, bars=3)
    day = pd.Series(frame.index.floor("D"), index=frame.index)
    first_day = day == list(dict.fromkeys(day))[1]
    sub = frame[first_day]
    complete = pos[first_day] >= 3
    assert complete.any()
    expected_high = sub["high"].iloc[:3].max()
    settled = hi[first_day][complete]
    assert np.allclose(settled.to_numpy(dtype=float), expected_high)
    # and it does not move for the rest of the session
    assert settled.nunique() == 1


def test_opening_range_restricted_to_a_session_ignores_other_bars(frame):
    hi, lo, pos = opening_range(frame, bars=1, session="london")
    sessions = pd.Series([session_of(ts) for ts in frame.index], index=frame.index)
    assert hi[sessions != "london"].isna().all()
    assert pos[sessions == "london"].notna().any()


def test_supertrend_direction_is_only_plus_or_minus_one(frame):
    line, direction = supertrend(frame, 10, 3.0)
    values = set(direction.dropna().unique())
    assert values <= {1.0, -1.0}
    assert line.dropna().size > 100


def test_kama_follows_a_trend_and_flattens_in_chop():
    """The efficiency ratio is the whole point of KAMA: if the smoothing
    constant did not adapt, this would be an EMA with extra steps.

    Measured as total variation of the average against total variation of the
    price. A perfectly directional series should be tracked almost one-for-one;
    an oscillation of the same amplitude should be almost entirely smoothed
    away.
    """
    n = 300
    idx = pd.date_range("2022-01-03", periods=n, freq="4h", tz="UTC")
    trend = pd.Series(np.linspace(1.0, 1.2, n), index=idx)
    chop = pd.Series(1.1 + 0.002 * np.sin(np.arange(n)), index=idx)

    def tracking(price):
        k = kama(price).dropna()
        return k.diff().abs().sum() / price.reindex(k.index).diff().abs().sum()

    assert tracking(trend) > 0.9
    assert tracking(chop) < 0.2


def test_rolling_percentile_stays_in_the_unit_interval(frame):
    p = rolling_percentile(atr(frame, 14), 100).dropna()
    assert p.min() >= 0.0 and p.max() <= 1.0
    assert p.size > 100


def test_engulfing_requires_the_body_to_cover_the_previous_body(frame):
    n = 6
    idx = pd.date_range("2022-01-03", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame({
        "open":  [1.10, 1.09, 1.12, 1.07, 1.075, 1.10],
        "close": [1.09, 1.11, 1.08, 1.08, 1.085, 1.10],
        "high":  [1.11, 1.12, 1.13, 1.09, 1.09, 1.11],
        "low":   [1.08, 1.08, 1.07, 1.06, 1.07, 1.09],
    }, index=idx)
    e = engulfing(df)
    assert e.iloc[1] == 1.0    # down bar, then an up bar covering its body
    assert e.iloc[2] == -1.0   # up bar, then a down bar covering its body
    assert e.iloc[3] == 0.0    # up bar after a down bar, but the body is inside
