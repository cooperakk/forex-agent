"""The nightly research lab: re-examine every strategy on the broker's own bars.

What it does, in a bounded time budget, on a worker thread:

1. **Health of every enabled strategy.** A backtest on the most recent
   ``lab_max_bars`` bars the feed has stored FROM THE BROKER (not a vendor
   file), at normal cost and at twice the cost. The mean trade R gets a block-
   bootstrap interval, because losing trades cluster. Each strategy is then
   ``alive`` (the whole interval above zero and still positive at 2x cost),
   ``dead`` (the whole interval below zero), ``weak`` (in between) or
   ``insufficient`` (fewer than 30 trades). The mean and spread become the
   drift detector's baseline, so live results are compared with what the
   strategy did on recent history rather than with a hopeful constant.
2. **Every pending proposal, A/B.** The same bars with the proposed value;
   the report shows the change in mean R with its interval. Adoption still
   needs the owner and the acceptance protocol -- the lab only saves them the
   question "would this even have helped lately?".
3. **The meta-label filter.** Trained on the signal logs of the FIRST 60% of
   the window and judged on the LAST 40% it never saw (research.metalabel's
   own split discipline), it becomes a candidate only if its out-of-sample
   AUC clears ``meta_min_auc`` and its expected value at the chosen threshold
   is positive. A candidate changes nothing until the owner approves it.

Nothing here is a promotion verdict: those still come only from
``scripts/run_acceptance.py`` with its full battery (CPCV, PBO, DSR, baselines).
The lab is the night shift that tells the owner where to look.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from ..core.clock import wall_ns
from .shadow import TF_SECONDS
from .stats import block_bootstrap_ci, summarise_r


@dataclass
class LabDeps:
    config: Callable[[], Any]                 # -> SentinelConfig
    bar_store: Any                            # sentinel.data.feed.BarStore
    instruments: Callable[[], Dict[str, Any]]
    conversions: Callable[[], Dict[str, Decimal]]
    proposals: Optional[Callable[[], List[Any]]] = None
    model_dir: Optional[Path] = None


def _frames(bar_store, instruments: List[str], timeframe: str, limit: int
            ) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    for sym in instruments:
        try:
            f = bar_store.frame(sym, timeframe, limit=limit)
        except Exception:  # noqa: BLE001 - one unreadable series is skipped
            continue
        if f is None or len(f) < 300:
            continue
        cols = [c for c in ("open", "high", "low", "close", "volume") if c in f.columns]
        f = f[cols].astype(float)
        f = f[~f.index.duplicated(keep="last")].sort_index()
        out[sym] = f
    return out


def _trade_r(result) -> List[float]:
    return [float(t.r_multiple) for t in result.trades
            if t.initial_risk and float(t.initial_risk) > 0]


def _status(n: int, lo: Optional[float], hi: Optional[float], stressed_mean: Optional[float]
            ) -> str:
    if n < 30 or lo is None or hi is None:
        return "insufficient"
    if hi < 0:
        return "dead"
    if lo > 0 and (stressed_mean or 0) > 0:
        return "alive"
    return "weak"


class ResearchLab:
    def __init__(self, deps: LabDeps) -> None:
        self.deps = deps

    def run(self, *, max_seconds: float, max_bars: int, meta_min_auc: float = 0.55,
            train_meta: bool = True) -> Dict[str, Any]:
        from ..research.backtest import BacktestConfig, run_backtest
        from ..strategy.registry import build as build_strategy

        started = time.monotonic()
        cfg = self.deps.config()
        instruments = self.deps.instruments()
        conversions = self.deps.conversions()
        report: Dict[str, Any] = {"started_ns": wall_ns(), "strategies": [], "proposals": [],
                                  "meta": None, "errors": [], "bars_limit": max_bars}
        signal_logs: List[Dict[str, Any]] = []
        baselines: Dict[str, Dict[str, float]] = {}

        def remaining() -> float:
            return max_seconds - (time.monotonic() - started)

        def backtest(strategy, data, risk_cfg, *, cost=1.0, label="lab"):
            bcfg = BacktestConfig(
                starting_equity=Decimal("10000"),
                account_currency=cfg.execution.account_currency,
                cost_multiplier=cost, label=label, data_label="broker-bars",
                trial_kind="validation", record_trial=False,
                session_windows_utc=cfg.agent.session_windows_utc,
                trade_days=cfg.agent.trade_days)
            return run_backtest(strategy, data, instruments, risk_cfg, bcfg,
                                conversions=conversions)

        for alloc in cfg.strategies:
            if not alloc.enabled:
                continue
            if remaining() <= 0:
                report["errors"].append("time budget exhausted before every strategy ran")
                break
            row: Dict[str, Any] = {"strategy": alloc.name, "timeframe": alloc.timeframe,
                                   "instruments": list(alloc.instruments)}
            try:
                data = _frames(self.deps.bar_store, alloc.instruments, alloc.timeframe,
                               max_bars)
                data = {k: v for k, v in data.items() if k in instruments}
                if not data:
                    row["status"] = "no_data"
                    row["note"] = "no stored broker bars for this allocation yet"
                    report["strategies"].append(row)
                    continue
                strategy = build_strategy(alloc.name, **alloc.params)
                base = backtest(strategy, data, cfg.risk, label=f"lab:{alloc.name}")
                r = _trade_r(base)
                summary = summarise_r(r)
                lo, hi = block_bootstrap_ci(r)
                stressed = backtest(build_strategy(alloc.name, **alloc.params), data,
                                    cfg.risk, cost=2.0, label=f"lab:{alloc.name}:2x")
                sr = _trade_r(stressed)
                stressed_mean = float(np.mean(sr)) if sr else None
                perf = base.performance.to_dict() if hasattr(base.performance, "to_dict") else {}
                row.update({
                    **summary, "boot_low": lo, "boot_high": hi,
                    "stressed_mean_r": None if stressed_mean is None else round(stressed_mean, 4),
                    "status": _status(len(r), lo, hi, stressed_mean),
                    "bars": {k: len(v) for k, v in data.items()},
                    "first_bar": str(min(v.index[0] for v in data.values())),
                    "last_bar": str(max(v.index[-1] for v in data.values())),
                    "max_drawdown_pct": perf.get("max_drawdown_pct"),
                    "sharpe": perf.get("sharpe"),
                })
                if len(r) >= 10:
                    baselines[alloc.name] = {"mean_r": float(np.mean(r)),
                                             "sd_r": float(np.std(r, ddof=1)),
                                             "n": len(r), "ts_ns": wall_ns()}
                tf_s = TF_SECONDS.get(alloc.timeframe, 3600)
                for s in base.signal_log:
                    s = dict(s)
                    s["strategy"] = alloc.name
                    s["label_end_ns"] = int(s.get("ts_ns") or 0) + \
                        int(s.get("horizon_bars") or 1) * tf_s * 1_000_000_000
                    signal_logs.append(s)
            except Exception as exc:  # noqa: BLE001 - one broken strategy is reported
                row["status"] = "error"
                row["error"] = f"{type(exc).__name__}: {exc}"[:300]
                report["errors"].append(f"{alloc.name}: {row['error']}")
            report["strategies"].append(row)

        # -- pending proposals, A/B on the same bars --------------------------- #
        for prop in (self.deps.proposals() if self.deps.proposals else [])[:10]:
            if remaining() <= 0:
                break
            report["proposals"].append(self._ab(prop, cfg, instruments, backtest, max_bars))

        # -- the meta-label candidate ------------------------------------------ #
        if train_meta and signal_logs and remaining() > 0:
            try:
                report["meta"] = self._train_meta(signal_logs, meta_min_auc)
            except Exception as exc:  # noqa: BLE001
                report["meta"] = {"trained": False, "reason": f"{type(exc).__name__}: {exc}"}
        report["baselines"] = baselines
        report["seconds"] = round(time.monotonic() - started, 1)
        return report

    # ------------------------------------------------------------------ #

    def _ab(self, prop, cfg, instruments, backtest, max_bars) -> Dict[str, Any]:
        row = {"id": getattr(prop, "id", ""), "path": getattr(prop, "path", ""),
               "from": getattr(prop, "current_value", None),
               "to": getattr(prop, "proposed_value", None)}
        path = str(row["path"])
        try:
            if not path.startswith("risk."):
                row["status"] = "unsupported"
                row["note"] = "only risk.* proposals are A/B tested by the lab"
                return row
            key = path.split(".", 1)[1]
            if not hasattr(cfg.risk, key):
                row["status"] = "unsupported"
                return row
            variant = cfg.risk.model_copy(update={key: prop.proposed_value})
            deltas: List[float] = []
            from ..strategy.registry import build as build_strategy
            for alloc in cfg.strategies:
                if not alloc.enabled:
                    continue
                data = _frames(self.deps.bar_store, alloc.instruments, alloc.timeframe, max_bars)
                data = {k: v for k, v in data.items() if k in instruments}
                if not data:
                    continue
                a = _trade_r(backtest(build_strategy(alloc.name, **alloc.params), data, cfg.risk,
                                      label=f"lab:ab:{alloc.name}:a"))
                b = _trade_r(backtest(build_strategy(alloc.name, **alloc.params), data, variant,
                                      label=f"lab:ab:{alloc.name}:b"))
                if a and b:
                    deltas.append(float(np.mean(b)) - float(np.mean(a)))
            row["mean_r_delta_by_strategy"] = [round(d, 4) for d in deltas]
            row["status"] = ("better" if deltas and min(deltas) > 0 else
                             "worse" if deltas and max(deltas) < 0 else
                             "mixed" if deltas else "no_data")
        except Exception as exc:  # noqa: BLE001
            row["status"] = "error"
            row["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return row

    def _train_meta(self, signal_logs: List[Dict[str, Any]], min_auc: float) -> Dict[str, Any]:
        from ..research.metalabel import fit_meta_gate

        rows = sorted([r for r in signal_logs if r.get("fwd_ret_h") is not None],
                      key=lambda r: r.get("ts_ns", 0))
        if len(rows) < 250:
            return {"trained": False, "reason": f"only {len(rows)} labelled signals (need 250)"}
        cut = int(len(rows) * 0.6)
        train, hold = rows[:cut], rows[cut:]
        # Purge (Lopez de Prado 2018, ch. 7): a training label whose forward
        # window reaches into the holdout has seen holdout prices. Drop it.
        hold_start = int(hold[0].get("ts_ns") or 0)
        purged = [r for r in train if int(r.get("label_end_ns") or 0) < hold_start]
        n_purged = len(train) - len(purged)
        train = purged
        gate, fit_report = fit_meta_gate(train)
        out: Dict[str, Any] = {"trained": gate is not None, "fit": fit_report,
                               "n_train": len(train), "n_holdout": len(hold),
                               "n_purged": n_purged}
        if gate is None:
            out["reason"] = "the fit refused (see fit notes)"
            return out
        # Out-of-sample: the last 40%, never seen by the fit or its calibration,
        # labelled exactly as the fit labels (side x forward return > cost).
        hold_rows = [r for r in hold if r.get("features") and r.get("fwd_ret_h") is not None]
        if len(hold_rows) < 50:
            out["reason"] = "holdout too small"
            out["trained"] = False
            return out
        from ..ai import calibration as cal
        names = gate.labeler.feature_names
        X = np.array([[float(r["features"].get(n, 0.0)) for n in names] for r in hold_rows],
                     dtype=float)
        y = [float(r["side_sign"]) * float(r["fwd_ret_h"]) > 0.0003 for r in hold_rows]
        p = gate.labeler.model.predict_proba(np.nan_to_num(X))[:, 1]
        pairs = list(zip(p.tolist(), y, strict=True))
        holdout = cal.summarise(pairs, gate.labeler.report.threshold)
        out["holdout"] = {k: holdout[k] for k in ("n", "positives", "brier", "skill", "auc")}
        ok = (holdout["auc"] is not None and holdout["auc"] >= min_auc
              and fit_report.get("expected_value_at_threshold", 0) > 0)
        out["eligible"] = bool(ok)
        if not ok:
            out["reason"] = (f"holdout AUC {holdout['auc']} below {min_auc} or no positive "
                             "expected value: not offered as a candidate")
            return out
        if self.deps.model_dir is not None:
            self.deps.model_dir.mkdir(parents=True, exist_ok=True)
            model_id = time.strftime("meta-%Y%m%d-%H%M%S", time.gmtime())
            path = self.deps.model_dir / f"{model_id}.joblib"
            sha = gate.save(path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
            out.update(model_id=model_id, path=str(path), sha256=sha)
        return out


def run_safely(lab: ResearchLab, **kw) -> Dict[str, Any]:
    try:
        return lab.run(**kw)
    except Exception as exc:  # noqa: BLE001 - the lab must never take anything down
        return {"errors": [f"{type(exc).__name__}: {exc}", traceback.format_exc()[-800:]],
                "strategies": [], "proposals": [], "meta": None}
