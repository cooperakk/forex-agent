"""Performance metrics.

Chosen so that a strategy cannot look good by hiding in the gaps between the
usual numbers:

* Return and Sharpe alone say nothing about survivability, so drawdown depth,
  drawdown *duration* and time-to-recovery are first-class.
* Skew and the tail ratio are reported because a high Sharpe with strong
  negative skew is the signature payoff of an unhedged short-volatility bet.
* Cost drag is separated out: gross P&L minus net P&L is the number that
  decides whether trading frequency is affordable.
* ``effective_n`` travels with the trade count, so nobody can present 300
  overlapping trades as 300 independent observations.
* Every R-multiple statistic is computed only from trades that actually had a
  defined initial risk; trades without one are counted and excluded rather
  than assigned a fabricated denominator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from math import sqrt
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
from scipy import stats as sps

from ..core.types import ClosedTrade


@dataclass
class PerformanceReport:
    # returns
    net_profit: float = 0.0            # from the equity curve, marked to market
    realised_pnl: float = 0.0          # sum of closed trades, net of cost
    net_return_pct: float = 0.0
    gross_profit: float = 0.0          # realised_pnl + total_cost
    total_cost: float = 0.0
    cost_drag_pct: float = 0.0
    cagr_pct: float = 0.0
    # risk-adjusted
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    # drawdown
    max_drawdown_pct: float = 0.0
    max_drawdown_amount: float = 0.0
    max_drawdown_duration_bars: int = 0
    time_to_recovery_bars: Optional[int] = None
    ulcer_index: float = 0.0
    # trades
    n_trades: int = 0
    effective_n: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    payoff_ratio: float = 0.0
    max_consecutive_losses: int = 0
    trades_without_defined_risk: int = 0
    # distribution
    return_skew: float = 0.0
    return_kurtosis: float = 3.0
    tail_ratio: float = 0.0
    var_95_r: float = 0.0
    cvar_95_r: float = 0.0
    # behaviour
    avg_hold_hours: float = 0.0
    exposure_pct: float = 0.0
    trades_per_year: float = 0.0
    avg_mae_r: float = 0.0
    avg_mfe_r: float = 0.0
    edge_efficiency: float = 0.0
    exit_breakdown: Dict[str, int] = field(default_factory=dict)
    by_instrument: Dict[str, Dict[str, float]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: (round(v, 6) if isinstance(v, float) else v)
             for k, v in self.__dict__.items()}
        return d


def _max_drawdown(equity: pd.Series) -> tuple[float, float, int, Optional[int]]:
    if equity.empty:
        return 0.0, 0.0, 0, None
    values = equity.to_numpy(dtype=float)
    peaks = np.maximum.accumulate(values)
    dd = np.where(peaks > 0, (peaks - values) / peaks, 0.0)
    idx = int(np.argmax(dd))
    max_dd_pct = float(dd[idx] * 100)
    max_dd_amt = float(peaks[idx] - values[idx])

    # longest stretch below a running peak
    longest = current = 0
    for i in range(len(values)):
        if values[i] < peaks[i]:
            current += 1
            longest = max(longest, current)
        else:
            current = 0

    recovery: Optional[int] = None
    peak_before = int(np.argmax(values[: idx + 1])) if idx > 0 else 0
    target = values[peak_before]
    after = np.where(values[idx:] >= target)[0]
    if after.size:
        # Measured from the PEAK, which is when the investor's money was last
        # whole -- not from the trough. Counting from the trough understates
        # every recovery by the length of the decline.
        recovery = int(after[0]) + (idx - peak_before)
    return max_dd_pct, max_dd_amt, longest, recovery


def _ulcer(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    v = equity.to_numpy(dtype=float)
    peaks = np.maximum.accumulate(v)
    dd = np.where(peaks > 0, (peaks - v) / peaks * 100, 0.0)
    return float(sqrt(np.mean(dd ** 2)))


def compute_performance(
    trades: Sequence[ClosedTrade],
    equity: pd.Series,
    periods_per_year: int = 252,
    starting_equity: float = 10000.0,
    effective_n: Optional[float] = None,
) -> PerformanceReport:
    rep = PerformanceReport()
    notes: List[str] = []

    if equity is not None and len(equity) > 1:
        rets = equity.pct_change().dropna()
        final = float(equity.iloc[-1])
        rep.net_profit = final - starting_equity
        rep.net_return_pct = (final / starting_equity - 1) * 100 if starting_equity else 0.0
        # Same degeneracy guard as research.stats: a flat curve has a standard
        # deviation of ~1e-19, not 0, and dividing by it manufactures a Sharpe
        # in the quadrillions.
        from .stats import _degenerate, sharpe_ratio as _sharpe
        arr = rets.to_numpy(dtype=float)
        sd = float(arr.std(ddof=1)) if arr.size > 2 else 0.0
        if arr.size > 2 and not _degenerate(arr, sd):
            rep.sharpe = _sharpe(arr, periods_per_year)
            # Downside deviation, not the standard deviation of the negative
            # returns: sqrt( mean over ALL N of min(r - MAR, 0)^2 ), MAR = 0.
            # Demeaning the losses about their own mean and dividing by
            # n_neg - 1 both overstates the ratio in the normal case and, when
            # the losses are tightly clustered, collapses to ~1e-18 so the
            # degenerate guard reported Sortino = 0 for a strategy whose true
            # Sortino was 17.6 -- exactly the short-volatility payoff shape
            # this report exists to expose.
            dsd = float(np.sqrt(np.mean(np.minimum(arr, 0.0) ** 2)))
            rep.sortino = (float(arr.mean() / dsd * sqrt(periods_per_year))
                           if dsd > 1e-15 else 0.0)
            rep.return_skew = float(sps.skew(arr, bias=False))
            rep.return_kurtosis = float(sps.kurtosis(arr, fisher=False, bias=False))
            p95, p5 = np.percentile(arr, 95), np.percentile(arr, 5)
            rep.tail_ratio = float(abs(p95 / p5)) if p5 != 0 else 0.0
        years = max(1e-9, len(equity) / periods_per_year)
        if starting_equity > 0 and final > 0:
            rep.cagr_pct = float(((final / starting_equity) ** (1 / years) - 1) * 100)
        dd_pct, dd_amt, dd_dur, recovery = _max_drawdown(equity)
        rep.max_drawdown_pct = dd_pct
        rep.max_drawdown_amount = dd_amt
        rep.max_drawdown_duration_bars = dd_dur
        rep.time_to_recovery_bars = recovery
        rep.ulcer_index = _ulcer(equity)
        rep.calmar = float(rep.cagr_pct / dd_pct) if dd_pct > 0 else 0.0
        if recovery is None and dd_pct > 0:
            notes.append("the maximum drawdown had not recovered by the end of the sample")

    rep.n_trades = len(trades)
    rep.effective_n = float(effective_n if effective_n is not None else len(trades))
    if not trades:
        rep.notes = notes + ["no trades were taken"]
        return rep

    pnls = np.array([float(t.pnl) for t in trades])
    with_risk = [t for t in trades if t.initial_risk and float(t.initial_risk) > 0]
    rep.trades_without_defined_risk = len(trades) - len(with_risk)
    rs = np.array([float(t.r_multiple) for t in with_risk]) if with_risk else np.array([])

    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    rep.win_rate = float(len(wins) / len(pnls))
    gross_win, gross_loss = float(wins.sum()), float(abs(losses.sum()))
    rep.profit_factor = float(gross_win / gross_loss) if gross_loss > 0 else float("inf")
    # ClosedTrade.pnl is already net of cost, so gross is net plus cost back.
    # The identity gross - cost == realised_pnl holds exactly, and
    # realised_pnl tracks net_profit up to open positions and the final mark.
    rep.realised_pnl = float(pnls.sum())
    # financing is SIGNED: negative is a charge, positive is a carry credit,
    # and net_pnl = price_pnl - commission + financing. So the cost term is
    # (commission - financing). Taking abs() turned a credit into a cost and
    # put gross_profit out by twice the financing -- invisible while financing
    # was always a charge, wrong the moment a positive-carry trade appeared.
    rep.total_cost = float(sum(float(t.commission) - float(t.financing) for t in trades))
    rep.gross_profit = rep.realised_pnl + rep.total_cost
    rep.cost_drag_pct = (rep.total_cost / starting_equity * 100) if starting_equity else 0.0

    if rs.size:
        rep.expectancy_r = float(rs.mean())
        win_r, loss_r = rs[rs > 0], rs[rs < 0]
        rep.avg_win_r = float(win_r.mean()) if win_r.size else 0.0
        rep.avg_loss_r = float(loss_r.mean()) if loss_r.size else 0.0
        rep.payoff_ratio = float(abs(rep.avg_win_r / rep.avg_loss_r)) if rep.avg_loss_r else 0.0
        rep.var_95_r = float(np.percentile(rs, 5))
        tail = rs[rs <= rep.var_95_r]
        rep.cvar_95_r = float(tail.mean()) if tail.size else rep.var_95_r
    if rep.trades_without_defined_risk:
        notes.append(
            f"{rep.trades_without_defined_risk} trade(s) had no defined initial risk and are "
            "excluded from every R statistic")

    streak = worst = 0
    for p in pnls:
        streak = streak + 1 if p < 0 else 0
        worst = max(worst, streak)
    rep.max_consecutive_losses = worst

    durations = np.array([t.duration_sec for t in trades], dtype=float)
    rep.avg_hold_hours = float(durations.mean() / 3600)
    if equity is not None and len(equity) > 1:
        span_years = max(1e-9, len(equity) / periods_per_year)
        rep.trades_per_year = float(len(trades) / span_years)
        total_span = float((equity.index[-1] - equity.index[0]).total_seconds())
        rep.exposure_pct = float(min(100.0, durations.sum() / total_span * 100)) if total_span > 0 else 0.0

    maes = np.array([float(t.max_adverse_r) for t in with_risk]) if with_risk else np.array([])
    mfes = np.array([float(t.max_favourable_r) for t in with_risk]) if with_risk else np.array([])
    if maes.size:
        rep.avg_mae_r = float(maes.mean())
    if mfes.size:
        rep.avg_mfe_r = float(mfes.mean())
        # How much of the move the exit actually captured. A low number with a
        # positive expectancy means the exit, not the entry, is the weak part.
        denom = float(mfes[mfes > 0].mean()) if (mfes > 0).any() else 0.0
        rep.edge_efficiency = float(rep.expectancy_r / denom) if denom > 0 else 0.0

    breakdown: Dict[str, int] = {}
    for t in trades:
        breakdown[t.exit_reason or "unknown"] = breakdown.get(t.exit_reason or "unknown", 0) + 1
    rep.exit_breakdown = dict(sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True))

    per_inst: Dict[str, Dict[str, float]] = {}
    for t in trades:
        d = per_inst.setdefault(t.instrument, {"n": 0.0, "pnl": 0.0, "wins": 0.0})
        d["n"] += 1
        d["pnl"] += float(t.pnl)
        d["wins"] += 1 if t.pnl > 0 else 0
    for sym, d in per_inst.items():
        d["win_rate"] = d["wins"] / d["n"] if d["n"] else 0.0
        d["avg_pnl"] = d["pnl"] / d["n"] if d["n"] else 0.0
    rep.by_instrument = per_inst

    if rep.n_trades < 30:
        notes.append(f"only {rep.n_trades} trades: no statistic here is stable")
    if rep.return_skew < -0.7:
        notes.append(f"return skew {rep.return_skew:.2f}: many small wins and rare large "
                     "losses. Treat the Sharpe with suspicion.")
    if rep.effective_n and rep.effective_n < rep.n_trades * 0.6:
        notes.append(f"effective n is {rep.effective_n:.0f} against {rep.n_trades} raw trades: "
                     "the observations overlap heavily")
    rep.notes = notes
    return rep


def equity_drawdown_series(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return equity
    peaks = equity.cummax()
    return (peaks - equity) / peaks * 100


def rolling_sharpe(returns: pd.Series, window: int = 60, periods_per_year: int = 252) -> pd.Series:
    mean = returns.rolling(window, min_periods=window).mean()
    sd = returns.rolling(window, min_periods=window).std(ddof=1)
    return (mean / sd.replace(0, np.nan)) * sqrt(periods_per_year)


def monthly_returns_table(equity: pd.Series) -> pd.DataFrame:
    if equity.empty:
        return pd.DataFrame()
    monthly = equity.resample("ME").last().pct_change().dropna() * 100
    if monthly.empty:
        return pd.DataFrame()
    df = monthly.to_frame("ret")
    df["year"] = df.index.year
    df["month"] = df.index.month
    return df.pivot_table(index="year", columns="month", values="ret")
