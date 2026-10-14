"""Execution Signals v10.5, adapted to FX -- and the bugs found on the way in.

The strategy's rules are checked on hand-built bars where the right answer can
be worked out by eye: an H1 mother bar, an inside bar, then M15 closes that do
or do not break it. Around it:

* the New York clock it runs on, against the real tz database;
* the synthetic market, which could not produce bars finer than an hour, so
  one M15 allocation on the paper venue blanked the H4 strategies beside it;
* the strategy tooling, which told the operator every allocation got H4 bars
  and allocated M15 strategies on H4 by default.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal as D
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sentinel.core.config import RiskConfig, SentinelConfig, StrategyAllocation
from sentinel.core.money import Instrument
from sentinel.core.types import Side
from sentinel.data.synthetic import DEFAULT_UNIVERSE, generate_series, generate_universe
from sentinel.research.backtest import BacktestConfig, run_backtest
from sentinel.strategy.base import new_york_clock
from sentinel.strategy.registry import build

ROOT = Path(__file__).resolve().parents[1]
PIP = 0.0001
M15 = pd.Timedelta(minutes=15)


# --------------------------------------------------------------------------- #
# hand-built bars
# --------------------------------------------------------------------------- #

def _quiet_history(start: pd.Timestamp, n: int, price: float = 1.1000) -> list:
    """Flat-ish M15 bars whose H1 buckets are never inside one another.

    Hours of equal width, alternately shifted three pips, so no H1 bar is
    inside another; and every bar closes mid-range, so nothing ever breaks:
    the only trade in a test is the one the test builds.
    """
    rows = []
    for i in range(n):
        mid = price + (3 * PIP if (i // 4) % 2 else 0.0)
        rows.append((start + i * M15, mid, mid + 4 * PIP, mid - 4 * PIP, mid))
    return rows


def _frame(rows) -> pd.DataFrame:
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame({"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                         "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                         "volume": 1.0}, index=idx)


def _scenario(break_close: float, *, setup_hour_utc: int = 13, first_bar: bool = True,
              day: str = "2026-06-10", extra_after: list | None = None):
    """History, then an H1 mother bar, an H1 inside bar, then the M15 bars of the
    next hour. Returns (frame, index of the bar that should break).

    2026-06-10 is a Wednesday; 13:00 UTC is 09:00 New York (summer).
    """
    start = pd.Timestamp(f"{day} {setup_hour_utc:02d}:00", tz="UTC") - pd.Timedelta(days=4)
    n_hist = int((pd.Timestamp(f"{day} {setup_hour_utc:02d}:00", tz="UTC") - start) / M15)
    rows = _quiet_history(start, n_hist)
    t = pd.Timestamp(f"{day} {setup_hour_utc:02d}:00", tz="UTC")
    # Mother hour: 1.1000 +/- 20 pips.
    for k in range(4):
        rows.append((t + k * M15, 1.1000, 1.1020, 1.0980, 1.1000))
    t += pd.Timedelta(hours=1)
    # Inside hour: 1.1000 +/- 5 pips, closing mid-range.
    for k in range(4):
        rows.append((t + k * M15, 1.1000, 1.1005, 1.0995, 1.1000))
    t += pd.Timedelta(hours=1)
    # The next hour: the break bar first (or second), then quiet bars.
    hour = []
    if not first_bar:
        hour.append((t, 1.1000, 1.1003, 1.0997, 1.1001))
    hour.append((t + len(hour) * M15, 1.1001, max(break_close, 1.1002) + 1 * PIP,
                 min(break_close, 1.0999) - 1 * PIP, break_close))
    while len(hour) < 4:
        hour.append((t + len(hour) * M15, break_close, break_close + 2 * PIP,
                     break_close - 2 * PIP, break_close))
    rows += hour
    for extra in extra_after or []:
        rows.append(extra)
    df = _frame(rows)
    break_at = df.index.get_loc(t + (0 if first_bar else 1) * M15)
    return df, break_at


def _signals(df, symbol="EUR_USD", **params):
    s = build("execution_signals", **params)
    data = {symbol: df}
    s.prepare(data)
    return {i: sig for i in range(len(df)) if (sig := s.generate(data, symbol, i))}


# --------------------------------------------------------------------------- #
# the rules
# --------------------------------------------------------------------------- #

class TestBreakOfSignalBar:
    def test_a_close_above_the_inside_bar_is_a_long(self):
        df, at = _scenario(1.1008)
        sigs = _signals(df)
        assert list(sigs) == [at]
        sig = sigs[at]
        assert sig.side is Side.BUY and sig.timeframe == "M15"
        # Prior-bar stop (the inside hour's last bar, low 1.0995) is 13 pips
        # away: wider than the 10-pip floor, so it stands.
        assert float(sig.stop_price) == pytest.approx(1.0995)
        risk = 1.1008 - 1.0995
        assert float(sig.target_price) == pytest.approx(1.1008 + 3 * risk)
        assert sig.features["sig_code"] == 1.0
        assert sig.calibrated is False

    def test_a_close_below_is_a_short(self):
        df, at = _scenario(1.0991)
        [(i, sig)] = _signals(df).items()
        assert i == at and sig.side is Side.SELL
        # Prior-bar high 1.1005 is 14 pips above the close: it stands.
        assert float(sig.stop_price) == pytest.approx(1.1005)

    def test_a_stop_closer_than_ten_pips_is_pushed_out_to_ten(self):
        df, at = _scenario(1.1006)          # prior low 1.0995 is 11 pips: stands
        assert float(_signals(df, min_stop_pct=0.0)[at].stop_price) == pytest.approx(1.0995)
        df, at = _scenario(1.1006)
        df.iloc[at - 1, df.columns.get_loc("low")] = 1.1000   # now 6 pips
        sig = _signals(df, min_stop_pct=0.0)[at]
        assert float(sig.stop_price) == pytest.approx(1.1006 - 10 * PIP)
        assert sig.features["stop_widened"] == 1.0

    def test_jpy_pips_are_a_hundredth(self):
        df, at = _scenario(1.1008)
        df.iloc[at - 1, df.columns.get_loc("low")] = 1.1000   # 8 pips under the close
        jpy = df * 100.0
        jpy["volume"] = 1.0
        sig = _signals(jpy, symbol="USD_JPY", min_stop_pct=0.0)[at]
        assert float(sig.stop_price) == pytest.approx(110.08 - 10 * 0.01)

    def test_the_stop_never_implies_a_position_above_the_leverage_cap(self):
        """0.5% risk / 5x gross leverage: a stop under 0.10% of price is one
        position over the cap, which the risk engine refuses. The default
        floor keeps the strategy from proposing it."""
        df, at = _scenario(1.1008)
        df.iloc[at - 1, df.columns.get_loc("low")] = 1.1000
        jpy = df * 136.0                     # USD/JPY ~150: 0.10% is ~15 pips
        jpy["volume"] = 1.0
        sig = _signals(jpy, symbol="USD_JPY")[at]
        close = 1.1008 * 136.0
        assert close - float(sig.stop_price) == pytest.approx(close * 0.001)
        assert (close - float(sig.stop_price)) / 0.01 > 14
        cfg = RiskConfig()
        per_trade_leverage = float(cfg.risk_per_trade_pct) / 100 / (
            (close - float(sig.stop_price)) / close)
        assert per_trade_leverage <= float(cfg.max_gross_leverage) + 1e-9

    def test_a_close_still_inside_is_nothing(self):
        df, _ = _scenario(1.1002)
        assert _signals(df) == {}

    def test_the_literal_reading_only_checks_the_first_bar(self):
        df, at = _scenario(1.1008, first_bar=False)
        assert _signals(df) == {}, "first_bar: a break on the second bar is too late"
        assert list(_signals(df, trigger_window="period")) == [at]

    def test_one_shot_per_signal_bar(self):
        df, at = _scenario(1.1008)
        # Every later bar of the hour also closes above the inside bar.
        sigs = _signals(df, trigger_window="period")
        assert list(sigs) == [at]

    def test_direction_switches(self):
        df, at = _scenario(1.1008)
        assert _signals(df, allow_long=False) == {}
        df, at = _scenario(1.0991)
        assert _signals(df, allow_short=False) == {}

    def test_the_setup_expires_with_its_period(self):
        # The hour after the inside bar does not break it (and pokes above it
        # once, so it is not itself a new inside bar); the break comes an hour
        # too late.
        df, at = _scenario(1.1001)
        df.iloc[at, df.columns.get_loc("high")] = 1.1007
        late = df.index[-1] + M15
        rows = [(late + k * M15, 1.1001, 1.1012, 1.0999, 1.1010) for k in range(4)]
        df2 = pd.concat([df, _frame(rows)])
        assert _signals(df2, trigger_window="period") == {}

    def test_failure_bars_arm_only_when_enabled(self):
        df, at = _scenario(1.1008)
        inside_hour = df.index[at] - pd.Timedelta(hours=1)
        mask = (df.index >= inside_hour) & (df.index < inside_hour + pd.Timedelta(hours=1))
        # Turn the inside hour into a failed high: pokes above 1.1020, closes back.
        first = np.flatnonzero(mask)[0]
        df.iloc[first, df.columns.get_loc("high")] = 1.1026
        assert _signals(df) == {}, "failures are off by default (paper: u_f = false)"
        sigs = _signals(df, trade_failures=True)
        # The failure bar's range is 1.0995..1.1026; 1.1008 is inside it.
        assert sigs == {}
        df.iloc[at, df.columns.get_loc("close")] = 1.0990
        df.iloc[at, df.columns.get_loc("low")] = 1.0988
        sigs = _signals(df, trade_failures=True)
        assert [s.side for s in sigs.values()] == [Side.SELL]
        assert next(iter(sigs.values())).features["sig_code"] == 2.0


class TestTheClock:
    def test_no_new_entries_between_the_flat_and_the_reopen(self):
        # Setup at 18:00-20:00 UTC = 14:00-16:00 NY; the break bar is 16:00 NY.
        df, at = _scenario(1.1008, setup_hour_utc=18)
        assert _signals(df) == {}
        assert _signals(df, session="rth") == {}
        # "Full session" trades it, as the paper's does -- held to tomorrow's 15:59.
        [(i, sig)] = _signals(df, session="full").items()
        assert i == at and sig.horizon_bars == (pd.Timestamp("2026-06-11 15:59")
                                                - pd.Timestamp("2026-06-10 16:15")) // M15

    def test_the_overnight_session_trades_and_rth_only_does_not(self):
        # Break bar 23:00 UTC = 19:00 NY: overnight.
        df, at = _scenario(1.1008, setup_hour_utc=21)
        assert list(_signals(df)) == [at]
        assert _signals(df, session="rth") == {}

    def test_horizon_ends_at_15_59_new_york(self):
        df, at = _scenario(1.1008)              # break bar 15:00 UTC = 11:00 NY
        sig = _signals(df)[at]
        close_ny = pd.Timestamp("2026-06-10 11:15")
        assert sig.horizon_bars == (pd.Timestamp("2026-06-10 15:59") - close_ny) // M15

    def test_an_overnight_entry_is_flat_by_next_afternoon(self):
        df, at = _scenario(1.1008, setup_hour_utc=21)   # break 19:00 NY, close 19:15
        sig = _signals(df)[at]
        assert sig.horizon_bars == (pd.Timestamp("2026-06-11 15:59")
                                    - pd.Timestamp("2026-06-10 19:15")) // M15

    def test_too_little_day_left_is_no_trade(self):
        # Break bar 19:00 UTC = 15:00 NY, closing 15:15: 44 minutes = 2 bars left.
        df, at = _scenario(1.1008, setup_hour_utc=17)
        assert _signals(df)[at].horizon_bars == 2
        assert _signals(df, min_bars_before_flat=3) == {}
        # On the second bar (closing 15:30) only one bar is left.
        df, at = _scenario(1.1008, setup_hour_utc=17, first_bar=False)
        assert _signals(df, trigger_window="period") == {}
        [sig] = _signals(df, trigger_window="period", min_bars_before_flat=1).values()
        assert sig.horizon_bars == 1

    def test_nothing_is_carried_into_the_weekend(self):
        # Friday 2026-06-12, setup 21:00-23:00 UTC -> break at 19:00 NY Friday is
        # after the roll; the market would be shut. Thursday overnight is fine.
        df, at = _scenario(1.1008, setup_hour_utc=21, day="2026-06-12")
        assert _signals(df) == {}
        df, at = _scenario(1.1008, setup_hour_utc=21, day="2026-06-11")
        assert list(_signals(df)) == [at]

    def test_new_york_clock_matches_the_tz_database(self):
        zoneinfo = pytest.importorskip("zoneinfo")
        try:
            ny = zoneinfo.ZoneInfo("America/New_York")
        except zoneinfo.ZoneInfoNotFoundError:
            pytest.skip("no tzdata on this machine")
        idx = pd.date_range("2008-01-01", "2027-12-31", freq="15min", tz="UTC")
        got = new_york_clock(idx)
        local = idx.tz_convert(ny)
        assert (got["ny_minute"].to_numpy() == np.asarray(local.hour * 60 + local.minute)).all()
        assert (got["ny_weekday"].to_numpy() == np.asarray(local.dayofweek)).all()

    def test_the_fx_day_rolls_at_17_new_york(self):
        idx = pd.DatetimeIndex(["2026-06-10 20:45", "2026-06-10 21:00",   # summer
                                "2026-12-09 21:45", "2026-12-09 22:00"], tz="UTC")
        fx = new_york_clock(idx)["fx_day"].to_numpy()
        assert fx[1] == fx[0] + 1 and fx[3] == fx[2] + 1


class TestHigherTimeframeBars:
    @pytest.mark.parametrize("day,hours", [
        ("2026-06-08", [1, 5, 9, 13, 17, 21]),     # summer: 17:00 NY = 21:00 UTC
        ("2026-12-07", [2, 6, 10, 14, 18, 22]),    # winter: 17:00 NY = 22:00 UTC
    ])
    def test_h4_bars_are_anchored_on_the_roll(self, day, hours):
        s = build("execution_signals", signal_timeframe="H4")
        idx = pd.date_range(f"{day} 00:00", periods=3 * 96, freq="15min", tz="UTC")
        period = s.signal_periods(idx)
        starts = idx[1:][np.diff(period) != 0]
        assert sorted({t.hour for t in starts}) == hours
        assert (np.diff(period) >= 0).all() and set(np.diff(period)) <= {0, 1}
        utc = build("execution_signals", signal_timeframe="H4", htf_anchor="utc")
        assert sorted({t.hour for t in idx[1:][np.diff(utc.signal_periods(idx)) != 0]}) \
            == [0, 4, 8, 12, 16, 20]
        assert s.warmup() > build("execution_signals").warmup()

    def test_a_weekend_is_a_jump_in_period_numbers(self):
        s = build("execution_signals")
        idx = pd.DatetimeIndex(["2026-06-12 20:45", "2026-06-14 21:00"], tz="UTC")
        a, b = s.signal_periods(idx)
        assert b - a > 1

    def test_a_gap_between_two_hours_is_not_compared(self):
        df, at = _scenario(1.1008)
        inside_hour = df.index[at] - pd.Timedelta(hours=1)
        mother = inside_hour - pd.Timedelta(hours=1)
        keep = (df.index < mother) | (df.index >= mother + pd.Timedelta(hours=1))
        # Without the mother hour there is nothing to be inside of.
        assert _signals(df[keep]) == {}

    def test_an_hour_missing_most_of_its_bars_is_not_a_bar(self):
        df, at = _scenario(1.1008)
        inside_hour = df.index[at] - pd.Timedelta(hours=1)
        drop = df.index[(df.index >= inside_hour) & (df.index < inside_hour + 2 * M15)]
        assert _signals(df.drop(drop)) == {}
        assert list(_signals(df.drop(drop), min_bucket_coverage=0.5)) == [at - 2]

    def test_bars_of_another_timeframe_trade_nothing(self):
        h4 = generate_universe(n_bars=1500, bars_per_day=6, seed=3)
        s = build("execution_signals")
        s.prepare(h4)
        assert not any(s.generate(h4, sym, i) for sym, df in h4.items()
                       for i in range(len(df)))


class TestFilters:
    def _with_prior_day(self, df, high, low):
        """Make the previous FX day's range exactly [low, high]."""
        clock = new_york_clock(df.index)
        last = clock["fx_day"].iloc[-1]
        prev = (clock["fx_day"] == last - 1).to_numpy()
        out = df.copy()
        mid = (high + low) / 2
        out.loc[prev, ["open", "close"]] = mid
        out.loc[prev, "high"] = high
        out.loc[prev, "low"] = low
        return out

    def test_above_yesterdays_high_only_longs(self):
        df, at = _scenario(1.0991)
        df = self._with_prior_day(df, high=1.0990, low=1.0950)   # close 1.0991 > PDH
        assert _signals(df, pdhl_filter=True) == {}
        assert list(_signals(df)) == [at]

    def test_inside_yesterdays_range_can_be_blocked(self):
        df, at = _scenario(1.1008)
        df = self._with_prior_day(df, high=1.1100, low=1.0900)
        assert list(_signals(df, pdhl_filter=True)) == [at]
        assert _signals(df, pdhl_filter=True, pdhl_inside="block") == {}


class TestLtfMode:
    def test_bias_then_an_m15_inside_bar_then_its_break(self):
        df, at = _scenario(1.1008)
        t = df.index[at]
        # Bars 2 and 3 of the hour: an M15 inside bar, then a close above it.
        df.iloc[at + 1] = [1.1008, 1.1010, 1.1004, 1.1007, 1.0]
        df.iloc[at + 2] = [1.1007, 1.1009, 1.1005, 1.1008, 1.0]   # inside the previous
        df.iloc[at + 3] = [1.1008, 1.1016, 1.1007, 1.1014, 1.0]   # breaks 1.1009
        sigs = _signals(df, execution_mode="ltf")
        assert list(sigs) == [at + 3]
        sig = sigs[at + 3]
        assert sig.side is Side.BUY
        assert df.index[at + 3] == t + 3 * M15

    def test_the_bias_expires(self):
        df, at = _scenario(1.1008)
        df.iloc[at + 1] = [1.1008, 1.1010, 1.1004, 1.1007, 1.0]
        df.iloc[at + 2] = [1.1007, 1.1009, 1.1005, 1.1008, 1.0]
        df.iloc[at + 3] = [1.1008, 1.1016, 1.1007, 1.1014, 1.0]
        assert _signals(df, execution_mode="ltf", bias_expiry_bars=2) == {}


class TestValidation:
    @pytest.mark.parametrize("bad", [
        {"signal_timeframe": "M15"}, {"signal_timeframe": "M5"},
        {"r_multiple": 1.0}, {"min_stop_pips": 0.0},
        {"trade_inside_bars": False}, {"allow_long": False, "allow_short": False},
        {"flat_ny": "17:30"}, {"reopen_ny": "16:30"}, {"rth_open_ny": "9h30"},
        {"execution_mode": "tick"}, {"max_stop_pips": 5.0},
    ])
    def test_refuses(self, bad):
        with pytest.raises(ValueError):
            build("execution_signals", **bad)

    def test_session_times_are_not_numbers_the_robustness_search_would_perturb(self):
        numeric = [k for k, v in build("execution_signals").params.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)]
        assert not any(k.endswith("_ny") for k in numeric)

    def test_causal_over_the_whole_indicator_frame(self):
        u = generate_universe(n_bars=1600, bars_per_day=96, seed=11)
        df = u["EUR_USD"]
        for mode in ("break", "ltf"):
            s = build("execution_signals", execution_mode=mode, trade_failures=True,
                      trigger_window="period")
            full = s.indicators(df)
            for cut in range(400, 1600, 97):
                part = s.indicators(df.iloc[:cut])
                pd.testing.assert_frame_equal(part, full.iloc[:cut], check_exact=False,
                                              rtol=1e-12, obj=f"{mode}@{cut}")


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #

INSTRUMENTS = {
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
    "AUD_USD": Instrument("AUD_USD", "AUD", "USD"),
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001")),
    "USD_CHF": Instrument("USD_CHF", "USD", "CHF"),
}
CONVERSIONS = {"USD": D("1"), "JPY": D("1") / D("150"), "CHF": D("1") / D("0.88")}


def test_backtest_runs_and_no_trade_is_held_through_the_roll():
    u = generate_universe(n_bars=5000, bars_per_day=96, seed=7, dollar_factor_strength=0.6)
    res = run_backtest(build("execution_signals", trigger_window="period"), u, INSTRUMENTS,
                       RiskConfig(), BacktestConfig(record_trial=False, data_label="synthetic"),
                       conversions=CONVERSIONS)
    assert res.diagnostics["generation_error_count"] == 0
    assert res.signals_generated > 20 and res.performance.n_trades > 5
    trades = [t for t in res.trades if t.exit_reason != "end_of_backtest"]
    opened = new_york_clock(pd.DatetimeIndex(pd.to_datetime([t.opened_ns for t in trades],
                                                            utc=True)))
    closed = new_york_clock(pd.DatetimeIndex(pd.to_datetime([t.closed_ns for t in trades],
                                                            utc=True)))
    # Opened and closed on the same FX day: nothing crossed a 17:00 roll.
    assert (opened["fx_day"].to_numpy() == closed["fx_day"].to_numpy()).all()
    assert "time_stop" in res.performance.exit_breakdown


# --------------------------------------------------------------------------- #
# the bugs found on the way in
# --------------------------------------------------------------------------- #

class TestIntradaySyntheticMarket:
    @pytest.mark.parametrize("bars_per_day,minutes", [(96, 15), (288, 5), (48, 30)])
    def test_bars_finer_than_an_hour(self, bars_per_day, minutes):
        df = generate_series(DEFAULT_UNIVERSE[0], 300, bars_per_day=bars_per_day)
        assert (df.index[1] - df.index[0]) == pd.Timedelta(minutes=minutes)

    def test_a_day_that_does_not_split_into_minutes_is_refused(self):
        with pytest.raises(ValueError):
            generate_series(DEFAULT_UNIVERSE[0], 10, bars_per_day=7)

    def test_an_m15_allocation_no_longer_blanks_the_paper_market(self, tmp_path):
        from sentinel.bootstrap import build_runtime

        cfg = SentinelConfig()
        cfg.ops.state_dir = str(tmp_path)
        cfg.ops.audit_log = str(tmp_path / "a.jsonl")
        cfg.ops.killswitch_file = str(tmp_path / "KILL")
        cfg.data.store_path = str(tmp_path / "m.db")
        cfg.data.history_bars = 300
        cfg.strategies = [
            StrategyAllocation(name="donchian_trend", enabled=True,
                               instruments=["EUR_USD"], timeframe="H4"),
            StrategyAllocation(name="execution_signals", enabled=True,
                               instruments=["EUR_USD"], timeframe="M15"),
        ]
        cfg.save(tmp_path / "config.json")
        runtime, _ = build_runtime(tmp_path / "config.json")
        snap = runtime.agent.feed.snapshot(["EUR_USD"],
                                           timeframes=runtime.agent._active_timeframes())
        assert "*" not in runtime.agent.feed.last_errors
        assert len(snap.frames_for("H4")["EUR_USD"]) == 300
        m15 = snap.frames_for("M15")["EUR_USD"]
        assert len(m15) >= 300
        assert (m15.index[-1] - m15.index[-2]) == M15


class TestStrategyTooling:
    def _run(self, *args, config):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "manage_strategies.py"),
                               "--config", str(config), *args],
                              capture_output=True, text=True, timeout=120)

    def test_add_defaults_to_the_strategys_own_timeframe(self, tmp_path):
        path = tmp_path / "config.json"
        SentinelConfig().save(path)
        out = self._run("add", "--name", "execution_signals", config=path)
        assert out.returncode == 0, out.stderr
        assert "warning" not in out.stdout
        [alloc] = SentinelConfig.load(path).strategies
        assert alloc.timeframe == "M15" and alloc.lifecycle == "hypothesis"

    def test_a_different_timeframe_is_allowed_but_named(self, tmp_path):
        path = tmp_path / "config.json"
        SentinelConfig().save(path)
        out = self._run("add", "--name", "inside_bar_break", "--timeframe", "H1",
                        config=path)
        assert out.returncode == 0
        assert "written for H4" in out.stdout
        listing = self._run("list", config=path)
        assert "allocated on H1" in listing.stdout
