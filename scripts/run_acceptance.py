#!/usr/bin/env python3
"""Run the acceptance protocol for one strategy and record the verdict.

This is the ONLY path that can make a strategy eligible for real money. It runs
the candidate against the baselines, the stress case and the CPCV paths,
evaluates every declared gate, and writes the verdict -- pass or fail -- into
the store the promotion guard reads.

The default data source is the synthetic universe, which can FALSIFY a strategy
but can never accept one: gate L10 refuses any verdict not built on the venue's
own historical bid/ask and real commission schedule. Point ``--bars`` at a
directory of per-instrument CSVs exported from your broker to produce a verdict
that can actually promote.

    python3 scripts/run_acceptance.py --strategy donchian_trend
    python3 scripts/run_acceptance.py --strategy donchian_trend \
        --bars data/oanda_h4 --data-label live-quality
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from sentinel.core.config import ResearchConfig, RiskConfig, SentinelConfig
from sentinel.core.money import Instrument
from sentinel.data.synthetic import generate_universe
from sentinel.research.acceptance import environment_gates, evaluate
from sentinel.research.backtest import BacktestConfig, run_backtest
from sentinel.research.factors import carry_factor, dollar_factor, momentum_factor
from sentinel.research.trials import TrialLedger, effective_trial_count, set_default_ledger
from sentinel.research.verdicts import VerdictStore, config_fingerprint
from sentinel.strategy.baselines import BuyAndHold, CoinFlip, NoTrade
from sentinel.strategy.registry import build as build_strategy, family_of

INSTRUMENTS = {
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
    "AUD_USD": Instrument("AUD_USD", "AUD", "USD"),
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001")),
    "USD_CHF": Instrument("USD_CHF", "USD", "CHF"),
}
CONVERSIONS = {"USD": D("1"), "JPY": D("1") / D("150"), "CHF": D("1") / D("0.88")}


#: Columns beyond OHLCV that a strategy or a gate may need, kept when present.
#: `bid`/`ask` are what makes a file "live-quality" at all; `carry_bp` is the
#: carry family's whole input and was being dropped by the importer.
_EXTRA_COLUMNS = ("bid", "ask", "bid_close", "ask_close", "spread", "carry_bp",
                  "swap_long", "swap_short")


def load_bars(directory: Path) -> dict[str, pd.DataFrame]:
    """One CSV per instrument: timestamp,open,high,low,close[,volume,bid,ask,carry_bp...] (UTC).

    Every column the strategies and gates can use is preserved. Only the
    timestamp is normalised.
    """
    out: dict[str, pd.DataFrame] = {}
    for path in sorted(directory.glob("*.csv")):
        symbol = path.stem.upper()
        if symbol not in INSTRUMENTS:
            print(f"[acceptance] skipping {path.name}: no contract specification")
            continue
        df = pd.read_csv(path)
        df.columns = [str(c).strip().lower() for c in df.columns]
        ts_col = next((c for c in df.columns
                       if c in ("timestamp", "time", "date", "datetime")), None)
        if ts_col is None:
            raise SystemExit(f"{path}: no timestamp column")
        idx = pd.DatetimeIndex(pd.to_datetime(df[ts_col], utc=True))
        keep = ["open", "high", "low", "close"] + \
            [c for c in _EXTRA_COLUMNS if c in df.columns]
        missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
        if missing:
            raise SystemExit(f"{path}: missing columns {missing}")
        frame = df[keep].assign(volume=df["volume"] if "volume" in df.columns else 0.0)
        frame.index = idx
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        out[symbol] = frame
    if not out:
        raise SystemExit(f"no usable CSVs in {directory}")
    return out


def data_quality_label(universe: dict[str, pd.DataFrame], requested: str | None) -> str:
    """The provenance label a file set is ENTITLED to, not the one that was typed.

    ``live-quality`` -- the only label that can promote -- is refused unless
    every file carries the venue's own bid and ask. A mid-price CSV, whatever
    its source, is at best ``third-party``: the spread the strategy was charged
    is then the simulator's assumption, not the venue's history, and the
    verdict must say so. (The default for any CSV used to be `live-quality`,
    which made the strongest claim in the protocol the one that needed the
    least evidence.)
    """
    has_ba = all(("bid" in df.columns or "bid_close" in df.columns)
                 and ("ask" in df.columns or "ask_close" in df.columns)
                 for df in universe.values())
    if requested == "live-quality":
        if not has_ba:
            raise SystemExit(
                "--data-label live-quality refused: not every file carries bid and "
                "ask columns. Export the venue's own bid/ask history (and its "
                "commission schedule) or label the data third-party. Acceptance on "
                "mid prices would charge the simulator's spread, not the venue's.")
        return "live-quality"
    if requested:
        return requested
    return "third-party"


def parameter_variants(strategy_name: str, base: dict, n: int = 8) -> list[dict]:
    """Genuine neighbours of the candidate's configuration.

    Every numeric parameter is perturbed, each by +/-15% and +/-30%, and the
    resulting configurations are validated by constructing the strategy, so a
    neighbour the strategy itself rejects (a target below its stop, a window
    under its floor) is not counted. Duplicates collapse. The list is the
    family of configurations the candidate was, in effect, chosen from -- and
    the PBO, SPA and CPCV gates are only as honest as this family is real.

    (The previous generator varied a parameter called `channel` and nothing
    else, so for the 27 strategies without one it ran the same configuration
    five times and called it five variants: a K-column matrix with one unique
    column, which makes PBO and SPA measure nothing.)
    """
    from sentinel.strategy.registry import get

    cls = get(strategy_name)
    numeric = [(k, v) for k, v in base.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)]
    seen: set = set()
    out: list[dict] = []

    def consider(params: dict) -> None:
        key = tuple(sorted((k, repr(v)) for k, v in params.items()))
        if key in seen:
            return
        try:
            cls(**params)
        except Exception:  # noqa: BLE001 - an invalid neighbour is not a variant
            return
        seen.add(key)
        out.append(dict(params))

    consider(dict(base))
    for factor in (0.85, 1.15, 0.7, 1.3):
        for k, v in numeric:
            if len(out) >= n:
                break
            nv = v * factor
            if isinstance(v, int):
                nv = int(round(nv))
                if nv == v:
                    nv = v + (1 if factor > 1 else -1)
                if nv < 1:
                    continue
            consider({**base, k: nv})
    return out[:n]


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the acceptance protocol")
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--bars", default=None,
                    help="directory of per-instrument CSVs; omit for synthetic data")
    ap.add_argument("--data-label", default=None,
                    choices=["synthetic", "third-party", "live-quality"])
    ap.add_argument("--n-bars", type=int, default=3000)
    ap.add_argument("--periods-per-year", type=int, default=1512)
    ap.add_argument("--declared-trials", type=int, default=1,
                    help="EVERY parameter combination you have ever tried on this "
                         "data, across every session. Understating it is the "
                         "cheapest way to make a strategy look acceptable. The "
                         "run raises this to the number of variants it evaluates "
                         "itself, to the number the persistent trial ledger holds "
                         "for this strategy's FAMILY, and never goes below 2 -- "
                         "but the ledger cannot see what you did elsewhere, so a "
                         "larger honest figure here still wins.")
    ap.add_argument("--state-dir", default="var")
    ap.add_argument("--config", default=None,
                    help="var/config.json to take the risk limits, session windows "
                         "and broker from; defaults are used when omitted")
    ap.add_argument("--broker", default=None,
                    help="broker profile the verdict is for (amarkets, alpari, "
                         "oanda, generic_mt5, paper); defaults to the config's")
    ap.add_argument("--variants", type=int, default=8,
                    help="parameter neighbours to evaluate for PBO / SPA / CPCV")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if args.bars:
        universe = load_bars(Path(args.bars))
        label = data_quality_label(universe, args.data_label)
    else:
        universe = generate_universe(n_bars=args.n_bars, bars_per_day=6,
                                     dollar_factor_strength=0.6)
        if args.data_label and args.data_label != "synthetic":
            raise SystemExit("synthetic data cannot be labelled anything but synthetic")
        label = "synthetic"

    cfg = SentinelConfig.load(Path(args.config)) if args.config else SentinelConfig()
    risk: RiskConfig = cfg.risk
    research: ResearchConfig = cfg.research
    strategy = build_strategy(args.strategy)
    instruments = {k: v for k, v in INSTRUMENTS.items() if k in universe}

    # The venue the verdict is FOR. Its declared capabilities feed the
    # environment gates (L0.3, L0.4); they were hard-coded True, which is not
    # a finding about any broker. The profile is a declaration and the probe
    # in the dashboard is the measurement; both are named in the verdict.
    from sentinel.brokers.profiles import resolve_profile
    from sentinel.brokers.profiles import venues as _venues  # noqa: F401
    profile = resolve_profile(args.broker or cfg.execution.broker)
    if profile is None:
        raise SystemExit(f"unknown broker profile {args.broker or cfg.execution.broker!r}")

    # Install the trial ledger BEFORE any backtest runs. Every call to
    # run_backtest below then records itself, so this run pays for its own
    # search whether or not anyone remembers to declare it. This is the one
    # workflow that can lead to real money, so it is the one place where the
    # accounting must not be optional.
    state = Path(args.state_dir)
    ledger = TrialLedger(state / "trials.db")
    set_default_ledger(ledger)
    if not ledger.was_present:
        # A ledger that has never been written is indistinguishable from one
        # that was DELETED, and deleting it silently resets the
        # multiple-testing bar to nothing -- which is the single cheapest way
        # to make a strategy look acceptable, and the one an optimistic
        # operator will reach for without noticing what it means.
        #
        # This is a warning rather than a refusal because a genuinely first
        # run has to be possible. But it is loud, it names the consequence,
        # and it is recorded in the verdict.
        print()
        print("  " + "!" * 68)
        print("  !  THE TRIAL LEDGER IS EMPTY.")
        print("  !")
        print("  !  Either this is the first acceptance run on this machine, or")
        print("  !  var/trials.db was deleted. The multiple-testing bar (gate")
        print("  !  L5.1) is computed from the number of configurations that")
        print("  !  have been searched, so an empty ledger sets that bar to its")
        print("  !  floor -- which flatters the candidate.")
        print("  !")
        print("  !  If you have backtested this idea before, pass the honest")
        print("  !  count with --declared-trials. It is the one number nobody")
        print("  !  else can supply.")
        print("  " + "!" * 68)
        print()
    family = family_of(args.strategy)

    # The agent's own entry gating, so what is validated is what will run:
    # the same session hours, the same trading days, the weekend flatten.
    gating = dict(session_windows_utc=cfg.agent.session_windows_utc,
                  trade_days=cfg.agent.trade_days,
                  weekend_flat=risk.weekend_flat,
                  friday_close_utc_hour=risk.friday_close_utc_hour)

    def bt(strat, label_, *, kind="search", **kw):
        return run_backtest(strat, universe, instruments, risk,
                            BacktestConfig(label=label_, data_label=label,
                                           trial_kind=kind,
                                           periods_per_year=args.periods_per_year,
                                           **gating, **kw),
                            conversions=CONVERSIONS)

    print(f"[acceptance] candidate: {args.strategy} ({family} family) on "
          f"{sorted(universe)} ({label})")
    candidate = bt(strategy, "candidate")
    # Baselines, the stress pass and the CPCV folds are VALIDATION of a
    # configuration already counted, not additional draws from the search
    # distribution. Counting them would penalise thorough validation, which is
    # the behaviour this system wants more of, not less.
    baselines = {
        "no_trade": bt(NoTrade(), "no_trade", kind="validation"),
        "coin_flip": bt(CoinFlip(), "coin_flip", kind="validation"),
        "buy_and_hold": bt(BuyAndHold(), "buy_and_hold", kind="validation"),
    }
    stressed = bt(build_strategy(args.strategy), "stressed", kind="validation",
                  cost_multiplier=research.cost_stress_multiple,
                  latency_multiplier=research.latency_stress_multiple)

    # The family of configurations the candidate was chosen from. Each one IS
    # a search trial, and the matrix of their returns is the input to three
    # gates: PBO (L4), SPA (L5.2) and CPCV (L6).
    variant_params = parameter_variants(args.strategy, dict(strategy.params),
                                        n=max(2, args.variants))
    variant_results = []
    for j, params in enumerate(variant_params):
        try:
            v = bt(build_strategy(args.strategy, **params), f"var{j}", kind="search")
            variant_results.append((params, v.per_bar_returns))
        except (TypeError, ValueError) as exc:
            print(f"[acceptance] variant {j} ({params}) skipped: {exc}")
            continue
    pbo_matrix = None
    if len(variant_results) >= 2:
        width = min(len(r) for _, r in variant_results)
        pbo_matrix = np.column_stack([r.to_numpy(dtype=float)[:width]
                                      for _, r in variant_results])
    unique_variants = len({tuple(sorted(p.items())) for p, _ in variant_results})
    if unique_variants < 2:
        print("[acceptance] WARNING: this strategy exposes fewer than two distinct "
              "configurations; the search-sensitive gates (L4, L5.2, L6) have no "
              "family to test against and will fail for want of evidence.")

    # CPCV, actually combinatorial and actually purged: for every combination
    # of test blocks the best variant is chosen on the purged train rows and
    # scored on the test rows, and the pieces are reassembled into complete
    # out-of-sample paths. What is measured is the SELECTION -- "pick the
    # parameters that worked" -- out of sample, which is the thing a slice
    # backtest of one fixed configuration cannot measure.
    path_returns = []
    cpcv_report = None
    if pbo_matrix is not None and unique_variants >= 2:
        from sentinel.research.cv import cpcv_paths_from_matrix
        try:
            cpcv_report = cpcv_paths_from_matrix(
                pbo_matrix, n_groups=research.cpcv_groups,
                test_groups=research.cpcv_test_groups,
                embargo_pct=research.embargo_pct,
                horizon_bars=int(strategy.meta.horizon_bars),
                periods_per_year=args.periods_per_year,
                index=variant_results[0][1].index[:pbo_matrix.shape[0]])
            path_returns = cpcv_report.paths
        except ValueError as exc:
            print(f"[acceptance] CPCV not run: {exc}")

    # Factor controls built from the same universe.
    rets = {sym: np.log(df["close"].astype(float)).diff().fillna(0).to_numpy()
            for sym, df in universe.items()}
    factor_data = {"dollar": dollar_factor(rets), "momentum": momentum_factor(rets)}
    if any("carry_bp" in df.columns for df in universe.values()):
        rates = {sym: df["carry_bp"].astype(float).to_numpy()
                 for sym, df in universe.items() if "carry_bp" in df.columns}
        if len(rates) >= 3:
            factor_data["carry"] = carry_factor(
                {k: rets[k] for k in rates}, rates)

    # Environment gates against the venue's OWN declaration, and its cost.
    round_trip = profile.default_spread_pips + (
        profile.commission_per_lot_round_turn / D("10") if profile.commission_per_lot_round_turn else D("0"))
    env = environment_gates(
        instrument=instruments.get("EUR_USD", next(iter(instruments.values()))),
        equity=D("10000"), risk_pct=risk.risk_per_trade_pct,
        stop_pips=D("30"), target_pips=D("75"),
        round_trip_cost_pips=round_trip + D("0.2"), pip_value_per_lot=D("10"),
        broker_supports_client_order_id=bool(profile.supports_client_order_id),
        broker_supports_server_stop=bool(profile.supports_server_side_stop))

    # Read the ledger AFTER this run's own backtests have been recorded: a
    # trial you just ran is a trial that counts, and reading first would let a
    # run exclude itself from its own correction.
    trial_summary = ledger.summary(args.strategy, family)

    alloc = next((a for a in cfg.strategies if a.name == args.strategy), None)
    store = VerdictStore(state / "verdicts.db")
    run_id = args.run_id or f"RUN-{args.strategy}-{candidate.performance.n_trades}-{label}"

    verdict = evaluate(
        run_id=run_id, strategy_name=args.strategy, candidate=candidate,
        baselines=baselines, stressed=stressed, cpcv_path_returns=path_returns,
        variant_returns=(pbo_matrix if pbo_matrix is not None else None),
        pbo_matrix=pbo_matrix, factor_data=factor_data,
        research_config=research, declared_trials=args.declared_trials,
        trial_summary=trial_summary,
        max_drawdown_ceiling_pct=float(risk.max_drawdown_halt_pct),
        periods_per_year=args.periods_per_year, data_label=label, environment=env,
        instruments=sorted(universe), params=dict(strategy.params),
        timeframe=strategy.meta.timeframe,
        cpcv_report=cpcv_report, venue_profile=profile.name,
        store=store,   # the verdict is recorded here, pass or fail
    )

    if args.json:
        print(json.dumps(verdict.to_dict(), ensure_ascii=False, indent=2))
        return 0 if verdict.accepted else 1

    n_variants = int(pbo_matrix.shape[1]) if pbo_matrix is not None else 0
    print(f"\n=== TRIAL ACCOUNTING ===")
    print(f"  {trial_summary.sentence()}")
    print(f"  declared {args.declared_trials} | ledger {trial_summary.charge} | "
          f"this run {n_variants} | floor 2  ->  effective "
          f"{effective_trial_count(args.declared_trials, trial_summary.charge, n_variants)}")
    for note in trial_summary.notes:
        print(f"  note: {note}")
    print(f"  ledger: {ledger.path}")

    print(f"\n=== VERDICT {verdict.run_id} ===")
    print(f"{'ACCEPTED' if verdict.accepted else 'NOT ACCEPTED'} — {verdict.summary}\n")
    for g in verdict.gates:
        mark = "PASS" if g.passed else "FAIL"
        print(f"  [{mark}] {g.id:5s} {g.name:34s} {g.observed:46s} need {g.threshold}")
    print(f"\nconfiguration fingerprint: {verdict.config_hash}")
    print(f"recorded in {store.path}")
    if verdict.accepted:
        print("\nTo promote, apply this allocation through the dashboard or API:")
        print(json.dumps({"strategies": [{
            "name": args.strategy, "enabled": True, "lifecycle": "accepted",
            "acceptance_run_id": verdict.run_id,
            "instruments": sorted(universe),
            "timeframe": strategy.meta.timeframe,
            "params": dict(strategy.params)}]}, ensure_ascii=False))
    else:
        print("\nThis is a result. Record it, and change the hypothesis rather than "
              "the threshold.")
    return 0 if verdict.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
