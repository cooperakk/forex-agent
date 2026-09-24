"""Event-driven backtester.

It runs the *same* risk engine, the *same* sizing code and the *same* order
path as live trading. That is the point: a research harness that reimplements
the trading rules tests a system that will never be deployed.

Deliberate pessimism, all of it sourced from the research brief:

* Signals are generated on the close of bar ``i`` and fill at the OPEN of
  bar ``i+1`` -- the first price the agent could act on -- before that bar's
  path is replayed. A signal that acts on the close it just observed is
  look-ahead; one that fills at the *next* close, after the whole bar is
  known, is look-ahead with a delay.
* The agent's entry gating -- session windows, trading days, the weekend
  flatten, the horizon time stop -- is applied when the caller supplies it
  (``scripts/run_acceptance.py`` always does), so the trades validated are
  the trades that would be allowed.
* Instruments are aligned on TIMESTAMP. Histories that do not coexist are
  refused; a symbol missing a bar at an instant has a gap there, not a shift.
* Intrabar path is adverse-first (see ``PaperBroker.on_bar_prices``): when a
  bar touched both the stop and the target, the stop is scored.
* Cost, slippage, latency, last look, financing and margin close-out are all
  live in the simulator, and can be stressed by a multiple.
* The equity curve is marked at bid/ask, never the mid.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..brokers.paper import PaperBroker, SimProfile
from ..core.config import RiskConfig
from ..core.ids import client_order_id
from ..core.money import CostModel, D, Instrument, ZERO, dec
from ..core.types import ClosedTrade, OrderIntent, OrderState, Quote, Side, Signal
from ..risk.engine import RiskContext, RiskEngine
from ..risk.protect import ProtectAction, evaluate_protection, time_stop, weekend_flat
from ..strategy.base import Strategy, atr
from .metrics import PerformanceReport, compute_performance
from .metalabel import bar_context_features, signal_features
from .trials import record_trial, window_key

_TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800,
}


def _align_on_timestamps(data: Dict[str, pd.DataFrame], symbols: List[str],
                         warmup: int) -> tuple[Dict[str, pd.DataFrame], pd.DatetimeIndex, Dict[str, int]]:
    """Put every symbol on one clock: the sorted union of their timestamps.

    A symbol with no bar at an instant gets a NaN row there -- a gap, which its
    indicators see as a gap and the loop skips. Histories that do not overlap
    for at least the warm-up are refused outright: a portfolio backtest across
    two calendars is not a backtest.
    """
    frames: Dict[str, pd.DataFrame] = {}
    for s in symbols:
        df = data[s]
        if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
            raise TypeError(f"{s}: bar index must be a timezone-aware DatetimeIndex (UTC)")
        idx = df.index.tz_convert("UTC")
        if not idx.is_monotonic_increasing or idx.has_duplicates:
            raise ValueError(f"{s}: bar index must be strictly increasing without duplicates")
        frames[s] = df.set_axis(idx)
    union = frames[symbols[0]].index
    for s in symbols[1:]:
        union = union.union(frames[s].index)
    union = pd.DatetimeIndex(union).sort_values()
    # Pairwise overlap: every symbol's span must share at least `warmup` bars of
    # the common clock with every other, or the pair never coexisted.
    spans = {s: (frames[s].index[0], frames[s].index[-1]) for s in symbols}
    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            lo = max(spans[a][0], spans[b][0])
            hi = min(spans[a][1], spans[b][1])
            overlap = int(((union >= lo) & (union <= hi)).sum()) if hi >= lo else 0
            if overlap <= warmup:
                raise ValueError(
                    f"{a} and {b} share only {overlap} bars of history "
                    f"({spans[a][0]}..{spans[a][1]} vs {spans[b][0]}..{spans[b][1]}); "
                    "a multi-instrument backtest needs histories that coexist")
    aligned: Dict[str, pd.DataFrame] = {}
    missing: Dict[str, int] = {}
    for s in symbols:
        re = frames[s].reindex(union)
        aligned[s] = re
        missing[s] = int(re["open"].isna().sum())
    return aligned, union, missing


def _periods_per_year_from_index(index) -> Optional[float]:
    """Bars actually observed per calendar year on a datetime index.

    Counting BARS OVER ELAPSED TIME rather than inverting the median spacing is
    deliberate: an H4 series has a 4-hour median spacing but only ~1512 bars a
    year, because the market is shut at weekends. Inverting the spacing would
    claim 2191 and annualise every statistic 20% too high.

    Note ``as_unit("ns")``: pandas 3.0 builds datetime indexes in MICROseconds
    by default, so reading ``asi8`` without normalising gives a number 1000x
    wrong -- the same trap that once made every holding time read as minutes
    instead of days.
    """
    try:
        values = pd.DatetimeIndex(index).as_unit("ns").asi8
    except (TypeError, ValueError, AttributeError):
        return None
    if values.size < 3:
        return None
    span_ns = float(values[-1] - values[0])
    if span_ns <= 0:
        return None
    span_years = span_ns / 1e9 / (365.25 * 24 * 3600)
    if span_years <= 0:
        return None
    return (values.size - 1) / span_years


@dataclass
class BacktestConfig:
    starting_equity: Decimal = D("10000")
    account_currency: str = "USD"
    periods_per_year: int = 252
    cost_multiplier: float = 1.0
    latency_multiplier: float = 1.0
    seed: int = 20260914
    warmup_bars: Optional[int] = None
    apply_protection: bool = True
    news_windows: Dict[str, Sequence[int]] = field(default_factory=dict)
    max_bars: Optional[int] = None
    label: str = "run"
    # Provenance of the bars, copied into the trial ledger. 'synthetic' can
    # falsify a strategy but can never accept one (gate L10), and the ledger
    # records which kind of data each trial was searched on so that a count
    # built entirely on synthetic runs is visible as such.
    data_label: str = "unknown"
    # 'search' -- this run is one of the configurations being chosen between.
    # 'validation' -- it re-runs a configuration already counted (a CPCV fold,
    # a stress pass, a baseline). See research/trials.py for why the
    # distinction has to exist.
    trial_kind: str = "search"
    record_trial: bool = True
    # The agent's own entry gating, applied here so the thing validated is the
    # thing that runs. None means "not applied" and exists for unit tests that
    # exercise a rule in isolation; scripts/run_acceptance.py always passes the
    # configuration's values. (Before this existed the live loop refused
    # entries outside 07-16 UTC and on weekends while the backtest took every
    # signal, so a strategy was accepted on trades it would never be allowed
    # to make.)
    session_windows_utc: Optional[List[List[int]]] = None
    trade_days: Optional[List[int]] = None
    #: Flatten before the weekend gap, exactly as the agent does.
    weekend_flat: bool = True
    friday_close_utc_hour: Optional[int] = None
    #: Close a position held past the signal's declared horizon, as the agent does.
    apply_time_stop: bool = True
    #: Bars ahead over which a signal's forward return is measured, for the
    #: directional-accuracy gate. Capped by the strategy's own horizon.
    forward_return_bars: int = 8
    #: "rules"  -- this module's harness: the agent's rules, re-implemented.
    #:            Fast, exploratory, and NOT a shared runtime (gate L11 fails).
    #: "agent"  -- research.replay: the production Agent/OMS/risk loop itself,
    #:            driven by execution bars at the production cadence.
    engine: str = "rules"
    #: For engine="agent": the runtime configuration, finer execution bars and
    #: an optional historical calendar source. See research.replay.
    runtime_config: object = None
    execution_data: object = None
    news_source: object = None
    #: A fitted research.metalabel.MetaGate consulted before every entry, in
    #: both engines. None -> every primary signal passes.
    meta_gate: object = None


@dataclass
class BacktestResult:
    label: str
    trades: List[ClosedTrade]
    equity_curve: pd.Series
    performance: PerformanceReport
    vetoes: Dict[str, int]
    signals_generated: int
    orders_submitted: int
    orders_rejected: int
    config_snapshot: Dict[str, object]
    per_bar_returns: pd.Series
    diagnostics: Dict[str, object] = field(default_factory=dict)
    #: One row per signal the strategy RAISED (filled or not): the direction it
    #: called and the return that followed. This is what the directional gate
    #: tests -- the strategy's actual forecasts, not its equity curve.
    signal_log: List[Dict[str, object]] = field(default_factory=list)

    def signed_forward_returns(self, horizon: str = "h") -> np.ndarray:
        """side x forward return, one per signal; positive means the call was right."""
        key = "fwd_ret_h" if horizon == "h" else "fwd_ret_1"
        vals = [float(r["side_sign"]) * float(r[key]) for r in self.signal_log
                if r.get(key) is not None and np.isfinite(float(r[key]))]
        return np.asarray(vals, dtype=float)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "performance": self.performance.to_dict(),
            "signals_generated": self.signals_generated,
            "orders_submitted": self.orders_submitted,
            "orders_rejected": self.orders_rejected,
            "vetoes": dict(sorted(self.vetoes.items(), key=lambda kv: kv[1], reverse=True)),
            "config": self.config_snapshot,
            "diagnostics": self.diagnostics,
        }


def _bars_to_ns(index: pd.DatetimeIndex) -> np.ndarray:
    """Bar timestamps as integer nanoseconds UTC.

    The explicit ``as_unit("ns")`` is not decoration. pandas 3.0 gives
    ``date_range`` a ``datetime64[us]`` dtype by default, so a bare
    ``.astype("int64")`` yields MICROseconds. Every duration downstream would
    then be 1000x too small -- holding times, financing, time stops, the
    annual-cost projection -- while every number still looked plausible. This
    is the archetypal silent unit bug, so the conversion is centralised here
    and asserted in ``tests/test_backtest.py``.
    """
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError("bar index must be a pandas DatetimeIndex")
    if index.tz is None:
        raise TypeError("bar index must be timezone-aware; use UTC")
    idx = index.tz_convert("UTC")
    try:
        return idx.as_unit("ns").astype("int64").to_numpy().astype(np.int64)
    except (AttributeError, ValueError):  # pragma: no cover - older pandas
        return np.asarray(idx.values, dtype="datetime64[ns]").astype("int64")


def run_backtest(
    strategy: Strategy,
    data: Dict[str, pd.DataFrame],
    instruments: Dict[str, Instrument],
    risk_config: RiskConfig,
    config: Optional[BacktestConfig] = None,
    *,
    sim_profile: Optional[SimProfile] = None,
    conversions: Optional[Dict[str, Decimal]] = None,
    signal_filter: Optional[Callable[[Signal], bool]] = None,
    size_scaler: Optional[Callable[[Signal], float]] = None,
) -> BacktestResult:
    cfg = config or BacktestConfig()
    if cfg.engine == "agent":
        from .replay import ReplayConfig, run_agent_replay
        rc = ReplayConfig(
            starting_equity=cfg.starting_equity, account_currency=cfg.account_currency,
            cost_multiplier=cfg.cost_multiplier, latency_multiplier=cfg.latency_multiplier,
            seed=cfg.seed, warmup_bars=cfg.warmup_bars, label=cfg.label,
            data_label=cfg.data_label, trial_kind=cfg.trial_kind,
            record_trial=cfg.record_trial, periods_per_year=cfg.periods_per_year,
            runtime_config=cfg.runtime_config, execution_data=cfg.execution_data,
            news_source=cfg.news_source, forward_return_bars=cfg.forward_return_bars,
            meta_gate=cfg.meta_gate)
        return run_agent_replay(strategy, data, instruments, risk_config, rc,
                                sim_profile=sim_profile, conversions=conversions)
    if cfg.engine != "rules":
        raise ValueError(f"unknown backtest engine {cfg.engine!r}; use 'rules' or 'agent'")
    profile = sim_profile or SimProfile()
    if cfg.cost_multiplier != 1.0 or cfg.latency_multiplier != 1.0:
        profile = profile.stressed(cfg.cost_multiplier, cfg.latency_multiplier)

    symbols = [s for s in data if s in instruments]
    if not symbols:
        raise ValueError("no instrument in `data` has a matching contract specification")
    warmup = cfg.warmup_bars if cfg.warmup_bars is not None else strategy.warmup()

    # ALIGN ON TIMESTAMPS, not on row number. Placing frames side by side by
    # position and reading the clock off the first one let a GBP_USD history
    # shifted by a year run to completion without complaint: every cross-pair
    # signal, every correlation and every portfolio limit was then computed
    # between prices that never coexisted. The union of the timestamps is the
    # clock; a symbol with no bar at a given instant simply has no bar there --
    # it is not replayed, not signalled, and its indicators see the gap.
    from ..data.validation import validate_universe
    validate_universe({s: data[s] for s in symbols})
    data, index, missing_bars = _align_on_timestamps(data, symbols, warmup)
    length = len(index)
    if cfg.max_bars:
        length = min(length, cfg.max_bars)
        index = index[:length]
    if length <= warmup + 10:
        raise ValueError(f"need more than {warmup + 10} bars, got {length}")

    ts_ns = _bars_to_ns(index)
    if length > 2:
        spacing = np.diff(ts_ns)
        median_spacing = float(np.median(spacing))
        if median_spacing < 1_000_000_000:
            raise ValueError(
                f"bar spacing resolves to {median_spacing:.0f} ns (< 1 second); the "
                "index unit is almost certainly not nanoseconds")
        if (spacing <= 0).any():
            raise ValueError("bar index is not strictly increasing")
    bar_ns = int(median_spacing) if length > 2 else 3600 * 1_000_000_000
    present = {s: data[s]["open"].notna().to_numpy()[:length] for s in symbols}

    broker = PaperBroker(instruments={s: instruments[s] for s in symbols},
                         starting_balance=cfg.starting_equity,
                         account_currency=cfg.account_currency,
                         profile=profile, seed=cfg.seed, start_ns=int(ts_ns[0]) - 1)
    for ccy, rate in (conversions or {}).items():
        broker.set_conversion(ccy, dec(rate))
    engine = RiskEngine(risk_config)

    # One causal pass over the data for indicators and the trailing-stop ATR.
    view = {s: data[s].iloc[:length] for s in symbols}
    strategy.prepare(view)
    atr_cache = {s: atr(view[s], 14) for s in symbols}
    normal_spread = {s: profile.base_spread_pips.get(s, profile.default_spread_pips)
                     for s in symbols}
    cost_models = {s: CostModel(spread_pips=normal_spread[s],
                                commission_per_lot_round_turn=profile.commission_per_lot_round_turn)
                   for s in symbols}

    equity_points: List[float] = []
    veto_counts: Dict[str, int] = {}
    signals_generated = orders_submitted = orders_rejected = 0
    generation_errors: List[str] = []
    signal_log: List[Dict[str, object]] = []
    seq = 0
    peak_equity = cfg.starting_equity
    day_key: Optional[int] = None
    day_start_equity = cfg.starting_equity
    trades_today = trades_week = trades_year = 0
    week_key = year_key = None
    last_entry_ns: Optional[int] = None
    pending: List[Signal] = []
    #: instrument -> the horizon (seconds) of the signal that opened it.
    hold_limit: Dict[str, int] = {}
    tf_seconds = _TIMEFRAME_SECONDS.get(strategy.meta.timeframe, bar_ns // 1_000_000_000)
    closes = {s: data[s]["close"].to_numpy(dtype=float)[:length] for s in symbols}

    def in_session(ts: pd.Timestamp) -> bool:
        if cfg.trade_days is not None and ts.weekday() not in cfg.trade_days:
            return False
        if cfg.session_windows_utc is not None:
            return any(lo <= ts.hour < hi for lo, hi in cfg.session_windows_utc)
        return True

    for i in range(length):
        now_ns = int(ts_ns[i])
        broker.set_time(now_ns)
        end_ns = now_ns + max(1, int(ts_ns[min(i + 1, length - 1)] - now_ns)) \
            if i + 1 < length else now_ns + bar_ns
        ts = index[i]

        # --- 1. mark the OPEN, and fill last bar's signals at it -------------
        #
        # A signal raised on bar i-1's close is the agent's decision at the
        # first price it can act on: the open of bar i. Executing after the
        # whole of bar i had been replayed -- as this loop used to -- filled at
        # bar i's CLOSE, four hours late on H4, with the bar's whole move
        # already known. The order of the two steps is the whole difference
        # between a backtest and a look-ahead.
        for sym in symbols:
            if not present[sym][i]:
                continue
            row = data[sym].iloc[i]
            broker.mark_mid(sym, dec(row["open"]), now_ns)
        acct = broker.account()
        if pending and i >= warmup and in_session(ts):
            for sig in pending:
                if sig.side is None or sig.stop_price is None:
                    continue
                if not present[sig.instrument][i]:
                    veto_counts["no_bar"] = veto_counts.get("no_bar", 0) + 1
                    continue
                inst = instruments[sig.instrument]
                try:
                    q = broker.quote(sig.instrument)
                except Exception:
                    continue
                ctx = RiskContext(
                    now_ns=now_ns, account=acct, positions=broker.positions(),
                    instruments={s: instruments[s] for s in symbols},
                    quotes={s: broker.quote(s) for s in symbols if s in broker._quotes},
                    conversions={instruments[s].quote: broker.conversion_rate(
                        instruments[s].quote, cfg.account_currency) for s in symbols},
                    equity_peak=peak_equity,
                    day_pnl=acct.equity - day_start_equity, day_start_equity=day_start_equity,
                    trades_today=trades_today, trades_this_week=trades_week,
                    trades_this_year=trades_year, last_entry_ns=last_entry_ns,
                    normal_spread_pips={s: normal_spread[s] for s in symbols},
                    cost_models=cost_models,
                    strategy_lifecycles={strategy.meta.name: "accepted"},
                    live_money=False,
                    news_blackout={},
                )
                seq += 1
                coid = client_order_id(strategy=strategy.meta.name, instrument=sig.instrument,
                                       side=sig.side.value, decision_ns=sig.decision_ns, seq=seq)
                try:
                    intent = OrderIntent(
                        client_order_id=coid, strategy=strategy.meta.name,
                        instrument=sig.instrument, side=sig.side, lots=D("0.01"),
                        stop_loss=inst.round_price(sig.stop_price),
                        take_profit=(inst.round_price(sig.target_price)
                                     if sig.target_price else None),
                        decision_ns=sig.decision_ns, reason=sig.rationale[:100])
                except ValueError:
                    continue
                decision = engine.evaluate_entry(intent, ctx)
                if not decision.approved:
                    for v in decision.vetoes:
                        veto_counts[v.rule] = veto_counts.get(v.rule, 0) + 1
                    continue
                lots = decision.approved_lots
                if size_scaler is not None:
                    lots = inst.round_lots_down(lots * dec(size_scaler(sig)))
                if lots < inst.min_lot:
                    veto_counts["size_after_scaling"] = \
                        veto_counts.get("size_after_scaling", 0) + 1
                    continue
                intent.lots = lots
                intent.risk_amount = decision.risk_amount
                intent.risk_pct = decision.risk_pct
                intent.expected_cost_pips = decision.expected_cost_pips
                res = broker.submit(intent, timeout_ms=5000)
                orders_submitted += 1
                if res.state in (OrderState.FILLED, OrderState.PARTIAL):
                    trades_today += 1
                    trades_week += 1
                    trades_year += 1
                    last_entry_ns = now_ns
                    if sig.horizon_bars and tf_seconds:
                        hold_limit[sig.instrument] = int(sig.horizon_bars) * int(tf_seconds)
                    acct = broker.account()
                elif res.state is OrderState.REJECTED:
                    orders_rejected += 1
                    veto_counts[f"broker:{res.reject_reason}"] = \
                        veto_counts.get(f"broker:{res.reject_reason}", 0) + 1
        elif pending and i >= warmup:
            veto_counts["out_of_session"] = veto_counts.get("out_of_session", 0) + len(pending)
        pending = []

        # --- 2. replay the bar through the simulator (fills, stops, margin) --
        for sym in symbols:
            if not present[sym][i]:
                continue
            row = data[sym].iloc[i]
            broker.on_bar_prices(
                sym, dec(row["open"]), dec(row["high"]), dec(row["low"]), dec(row["close"]),
                now_ns, end_ns)

        acct = broker.account()
        equity_points.append(float(acct.equity))
        # BALANCE, as the agent tracks it: an open gain that is given back must
        # not leave a phantom peak that reads as a drawdown for ever after.
        peak_equity = max(peak_equity, acct.balance)

        if day_key != ts.dayofyear:
            day_key, day_start_equity, trades_today = ts.dayofyear, acct.equity, 0
        iso_week = ts.isocalendar()[1]
        if week_key != iso_week:
            week_key, trades_week = iso_week, 0
        if year_key != ts.year:
            year_key, trades_year = ts.year, 0

        if i < warmup:
            continue

        # --- 3. exits the agent applies before any R-denominated rule --------
        for pos in list(broker.positions()):
            if cfg.weekend_flat:
                wf = weekend_flat(pos, end_ns, cfg.friday_close_utc_hour)
                if wf is not None:
                    broker.close_position(pos.instrument, reason="weekend_flat")
                    hold_limit.pop(pos.instrument, None)
                    continue
            if cfg.apply_time_stop:
                limit = int(risk_config.max_hold_sec) or hold_limit.get(pos.instrument, 0)
                if limit > 0 and time_stop(pos, end_ns, limit) is not None:
                    broker.close_position(pos.instrument, reason="time_stop")
                    hold_limit.pop(pos.instrument, None)

        # --- 4. profit protection on open positions ------------------------
        if cfg.apply_protection:
            for pos in list(broker.positions()):
                try:
                    q = broker.quote(pos.instrument)
                except Exception:
                    continue
                inst = instruments[pos.instrument]
                a_val = atr_cache[pos.instrument].iloc[i]
                a = dec(float(a_val)) if np.isfinite(a_val) else None
                for act in evaluate_protection(pos, q, inst, risk_config, atr=a,
                                               quote_to_account=broker.conversion_rate(
                                                   inst.quote, cfg.account_currency)):
                    if act.action is ProtectAction.MOVE_STOP and act.new_stop is not None:
                        try:
                            broker.modify_position(pos.instrument, stop_loss=act.new_stop)
                            # Only the break-even RULE sets the flag. Setting it
                            # for any stop move let the first ATR trail disable
                            # break-even permanently, so a position could be
                            # stopped out below entry after having been +1R.
                            if act.rule == "breakeven":
                                pos.breakeven_moved = True
                        except Exception:
                            pass
                    elif act.action is ProtectAction.PARTIAL_CLOSE and act.close_lots:
                        broker.close_position(pos.instrument, act.close_lots,
                                              reason="partial_take")
                        pos.partial_taken = True
                    elif act.action is ProtectAction.CLOSE:
                        broker.close_position(pos.instrument, reason=act.reason[:24])


        # --- 5. generate signals for the NEXT bar --------------------------
        if i < length - 1:
            for sym in symbols:
                if not present[sym][i]:
                    continue
                try:
                    sig = strategy.generate(view, sym, i)
                except Exception as exc:  # a broken strategy must not kill the run
                    generation_errors.append(f"{sym}@{i}: {exc.__class__.__name__}: {exc}")
                    sig = None
                if sig is None or sig.side is None:
                    continue
                signals_generated += 1
                # The forecast the strategy just made, and what followed it.
                # Recorded for EVERY raised signal, filled or not, because the
                # question "does this rule know which way price goes" is about
                # the rule, not about which of its calls the risk engine let
                # through. Forward returns are read from the close series, and
                # bars beyond the sample are None, never zero.
                h = max(1, min(int(cfg.forward_return_bars),
                               int(sig.horizon_bars) if sig.horizon_bars else 10**9))
                c0 = closes[sym][i]
                f1 = closes[sym][i + 1] if i + 1 < length else np.nan
                fh = closes[sym][i + h] if i + h < length else np.nan
                signal_log.append({
                    "instrument": sym, "index": i, "ts_ns": now_ns,
                    "side": sig.side.value, "side_sign": sig.side.sign,
                    "strength": float(sig.strength),
                    "fwd_ret_1": (float(f1 / c0 - 1.0)
                                  if np.isfinite(f1) and c0 > 0 else None),
                    "fwd_ret_h": (float(fh / c0 - 1.0)
                                  if np.isfinite(fh) and c0 > 0 else None),
                    "horizon_bars": h,
                    "features": signal_features(sig, bar_context_features(view[sym], i)),
                })
                if signal_filter is not None and not signal_filter(sig):
                    veto_counts["meta_filter"] = veto_counts.get("meta_filter", 0) + 1
                    continue
                if cfg.meta_gate is not None:
                    act, p, _ = cfg.meta_gate.decide(
                        sig, bar_context_features(view[sym], i))
                    if not act:
                        veto_counts["meta_label"] = veto_counts.get("meta_label", 0) + 1
                        continue
                pending.append(sig)

    # Flatten anything still open, at the last available price, and OVERWRITE the
    # final equity sample rather than appending one. Appending produced a series
    # one point longer than the index, and the trailing point -- the only one that
    # reflected the closing trades -- was then silently truncated away, so the
    # equity curve disagreed with the trade ledger by the size of the last flatten.
    for pos in list(broker.positions()):
        broker.close_position(pos.instrument, reason="end_of_backtest")
    if equity_points:
        equity_points[-1] = float(broker.account().equity)

    equity = pd.Series(equity_points[:length], index=index[:len(equity_points[:length])],
                       name="equity")
    # Annualisation must match the SAMPLING RATE of this curve, which is one
    # point per bar. A default of 252 (daily) applied to an H4 series understates
    # Sharpe by sqrt(1512/252) = 2.45x, and the same constant drives CAGR,
    # Calmar, trades_per_year and the de-annualisation inside PSR/DSR/MinTRL.
    derived_ppy = _periods_per_year_from_index(equity.index)
    if derived_ppy and cfg.periods_per_year:
        ratio = derived_ppy / float(cfg.periods_per_year)
        if ratio < 0.8 or ratio > 1.25:
            cfg = replace(cfg, periods_per_year=int(round(derived_ppy)))
    # dropna(), not fillna(0): compute_performance drops the leading NaN, so
    # filling it here put a spurious zero at the front of any series handed to
    # PBO or SPA and shifted the sample relative to every other statistic.
    rets = equity.pct_change().dropna()
    perf = compute_performance(broker.closed_trades, equity, cfg.periods_per_year,
                               float(cfg.starting_equity))

    # Record what was just evaluated. This is the automatic half of trial
    # accounting: a backtest that runs is a trial that happened, and relying on
    # anyone to remember to declare it afterwards is how the multiple-testing
    # gate ends up switched off. It is a no-op unless a ledger is installed
    # (research/trials.py explains why that default is what it is), and it can
    # never fail the run.
    if cfg.record_trial:
        record_trial(
            strategy=strategy.meta.name,
            family=getattr(strategy.meta, "family", "unclassified"),
            params=dict(strategy.params),
            instruments=symbols,
            timeframe=strategy.meta.timeframe,
            data_window=window_key(index[0], index[length - 1], length, cfg.data_label),
            data_label=cfg.data_label,
            kind=cfg.trial_kind,
            note=cfg.label,
        )

    return BacktestResult(
        label=cfg.label, trades=broker.closed_trades, equity_curve=equity,
        performance=perf, vetoes=veto_counts, signals_generated=signals_generated,
        orders_submitted=orders_submitted, orders_rejected=orders_rejected,
        per_bar_returns=rets,
        config_snapshot={
            "strategy": strategy.describe(),
            "cost_multiplier": cfg.cost_multiplier,
            "latency_multiplier": cfg.latency_multiplier,
            "starting_equity": str(cfg.starting_equity),
            "seed": cfg.seed, "bars": length, "warmup": warmup,
            "session_windows_utc": cfg.session_windows_utc,
            "trade_days": cfg.trade_days,
            "weekend_flat": cfg.weekend_flat,
            "risk": {"risk_per_trade_pct": str(risk_config.risk_per_trade_pct),
                     "max_open_positions": risk_config.max_open_positions,
                     "max_trades_per_day": risk_config.max_trades_per_day},
        },
        diagnostics={"engine": "rules-harness", "instruments": symbols,
                     "first_bar": str(index[0]), "last_bar": str(index[length - 1]),
                     "missing_bars": missing_bars,
                     "generation_errors": generation_errors[:20],
                     "generation_error_count": len(generation_errors)},
        signal_log=signal_log,
    )
