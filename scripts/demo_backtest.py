#!/usr/bin/env python3
"""Run strategies against the synthetic universe. Illustration, not evidence.

Two things this is meant to show, beyond the numbers:

* the library is a family library -- ``--family trend`` runs every trend
  system, and they are not independent bets;
* every run is a TRIAL, and the ledger counts it. The summary printed at the
  end is the bar these strategies would have to clear in an acceptance run, and
  it rises every time you run this script with a new configuration. That is the
  price of a large library, made visible.
"""
import argparse, sys, time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.core.config import RiskConfig
from sentinel.core.money import Instrument
from sentinel.data.synthetic import generate_universe
from sentinel.research.backtest import BacktestConfig, run_backtest
from sentinel.research.trials import TrialLedger, set_default_ledger
from sentinel.strategy.registry import available, build, families, family_of

INSTRUMENTS = {
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
    "AUD_USD": Instrument("AUD_USD", "AUD", "USD"),
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001")),
    "USD_CHF": Instrument("USD_CHF", "USD", "CHF"),
}
CONVERSIONS = {"USD": D("1"), "JPY": D("1") / D("150"), "CHF": D("1") / D("0.88")}


def universe(n_bars=3000, bars_per_day=6, seed=20260914):
    return generate_universe(n_bars=n_bars, bars_per_day=bars_per_day,
                             seed=seed, dollar_factor_strength=0.6)


def show(res):
    p = res.performance
    print(f"\n=== {res.label} ===")
    print(f"signals {res.signals_generated:5d} | submitted {res.orders_submitted:4d} | "
          f"rejected {res.orders_rejected:3d} | trades {p.n_trades:4d}")
    print(f"net {p.net_return_pct:+7.2f}%  CAGR {p.cagr_pct:+7.2f}%  "
          f"Sharpe {p.sharpe:6.2f}  Sortino {p.sortino:6.2f}  Calmar {p.calmar:6.2f}")
    print(f"maxDD {p.max_drawdown_pct:6.2f}%  ulcer {p.ulcer_index:5.2f}  "
          f"win {p.win_rate*100:5.1f}%  PF {p.profit_factor:5.2f}  E[R] {p.expectancy_r:+.3f}")
    print(f"cost {p.total_cost:8.0f} ({p.cost_drag_pct:5.2f}% of equity)  "
          f"trades/yr {p.trades_per_year:5.0f}  hold {p.avg_hold_hours:5.1f}h")
    print(f"MAE {p.avg_mae_r:+.2f}R  MFE {p.avg_mfe_r:+.2f}R  efficiency {p.edge_efficiency:+.2f}")
    print("exits:", p.exit_breakdown)
    print("vetoes:", dict(list(res.to_dict()["vetoes"].items())[:6]))
    for n in p.notes:
        print("  note:", n)


def trial_report(ledger, names):
    """What the runs above cost in multiple-testing terms.

    Printed even on a demo, because the whole point of the ledger is that the
    cost of searching is visible at the moment of searching rather than
    discovered later in an acceptance report nobody expected to fail.
    """
    print("\n=== TRIAL LEDGER ===")
    for family in sorted({family_of(n) for n in names}):
        if family == "baseline":
            continue
        summary = ledger.summary(next(n for n in names if family_of(n) == family), family)
        print(f"  {summary.sentence()}")
    print(f"  ledger: {ledger.path}")
    print("  Every one of those is a draw. The deflated-Sharpe gate in "
          "scripts/run_acceptance.py\n  charges the family count, so adding a "
          "strategy raises the bar for its siblings.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--family", default=None, choices=sorted(families()),
                    help="run every strategy in one family instead of the default trio")
    ap.add_argument("--ensemble", action="store_true",
                    help="also run an equal-risk ensemble over the selected strategies")
    ap.add_argument("--n-bars", type=int, default=3000)
    ap.add_argument("--state-dir", default="var")
    args = ap.parse_args()

    # Install the ledger before anything runs: a backtest that happens is a
    # trial that happened, whether or not anyone meant it as one.
    ledger = TrialLedger(Path(args.state_dir) / "trials.db")
    set_default_ledger(ledger)

    names = (available(family=args.family) if args.family
             else ["donchian_trend", "vol_reversion", "baseline_coin_flip"])
    u = universe(n_bars=args.n_bars)
    rc = RiskConfig()
    for label in names:
        t0 = time.time()
        res = run_backtest(build(label), u, INSTRUMENTS, rc,
                           BacktestConfig(label=label, periods_per_year=1512,
                                          data_label="synthetic"),
                           conversions=CONVERSIONS)
        show(res)
        print(f"  ({time.time()-t0:.1f}s)")

    if args.ensemble:
        from sentinel.strategy.ensemble import Ensemble

        members = [build(n) for n in names if not n.startswith("baseline_")]
        if len(members) >= 2:
            ens = Ensemble(members)
            t0 = time.time()
            res = run_backtest(ens, u, INSTRUMENTS, rc,
                               BacktestConfig(label=ens.meta.name,
                                              periods_per_year=1512,
                                              data_label="synthetic"),
                               conversions=CONVERSIONS)
            show(res)
            print(f"  ({time.time()-t0:.1f}s)")

    trial_report(ledger, names)
