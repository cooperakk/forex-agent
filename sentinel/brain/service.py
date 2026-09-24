"""The Brain: one object the agent consults, the runtime schedules, the API reads.

Hooks the AGENT calls, on its own thread, cheaply:

* ``observe_context``    -- cache instruments and conversions (for the lab);
* ``record``             -- put a considered signal in the shadow book;
* ``resolve``            -- score open shadow signals against new bars;
* ``on_trades``          -- update loss streaks when trades close;
* ``cooldowns``          -- active rests, for the risk engine's veto;
* ``strategy_layers``    -- drift, equity-curve and allocation multipliers;
* ``similarity``         -- the similar-situation multiplier;
* ``stress_settings``    -- the gap-stress table and budget;
* ``meta_gate``          -- the owner-approved meta-label filter, if any.

Work the RUNTIME schedules on its background worker: the nightly lab (on its
own thread), the weekly self-report, pruning. Everything is fail-safe in the
same direction: a brain that errors contributes a multiplier of 1.0 and no
veto -- the pre-existing risk engine still stands between every signal and
the account.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from ..core.clock import wall_ns
from . import stats
from .shadow import TF_SECONDS, TAKEN, feature_matrix, meta_live_check, resolve_path, \
    scorecard, shadow_key
from .store import BrainStore

_SEC = 1_000_000_000
EVENT_COOLDOWN = "brain.cooldown"
EVENT_DRIFT = "brain.drift"
EVENT_LAB = "brain.lab"
EVENT_REPORT = "brain.report"
EVENT_MODEL = "brain.model"


class Brain:
    def __init__(self, config: Callable[[], Any], store: BrainStore, memory, audit, *,
                 notify: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 clock: Callable[[], int] = wall_ns) -> None:
        self._config = config
        self.store = store
        self.memory = memory
        self.audit = audit
        self.notify = notify
        self._clock = clock
        self._lock = threading.RLock()
        self._instruments: Dict[str, Any] = {}
        self._conversions: Dict[str, Any] = {}
        self._version = 0
        self._layer_cache: Dict[Tuple[str, str, int], Tuple[float, List[str], Dict]] = {}
        self._sim_cache: Dict[str, Tuple[int, float, np.ndarray, np.ndarray, List[str]]] = {}
        self._drift_alarmed: Dict[str, bool] = dict(store.get_state("drift_alarmed", {}) or {})
        self._meta_gate = None
        self._meta_gate_id: Optional[str] = None
        self.lab = None                    # ResearchLab, wired by bootstrap
        self._lab_thread: Optional[threading.Thread] = None
        self.last_error = ""

    @property
    def cfg(self):
        return self._config()

    # ------------------------------------------------------------------ #
    # context and the shadow book
    # ------------------------------------------------------------------ #

    def observe_context(self, instruments: Dict[str, Any], conversions: Dict[str, Any]) -> None:
        with self._lock:
            if instruments:
                self._instruments = dict(instruments)
            if conversions:
                self._conversions = dict(conversions)

    def record(self, signal, decision, frame=None) -> None:
        cfg = self.cfg
        if not (cfg.enabled and cfg.shadow_book) or signal is None or decision is None:
            return
        if signal.side is None or signal.stop_price is None:
            return
        try:
            entry = float(decision.entry) if decision.entry else (
                float(frame["close"].iloc[-1]) if frame is not None and len(frame) else None)
            if entry is None:
                return
            diag = decision.diagnostics or {}
            features = dict(diag.get("meta_features") or {})
            if not features:
                from ..research.metalabel import bar_context_features, signal_features
                ctx = bar_context_features(frame, len(frame) - 1) \
                    if frame is not None and len(frame) else {}
                features = signal_features(signal, ctx)
            stop_pips = _num(diag.get("stop_pips"))
            cost_pips = _num(diag.get("round_trip_cost_pips"))
            cost_r = (cost_pips / stop_pips) if stop_pips and cost_pips else 0.05
            rule = None
            if decision.action not in TAKEN and decision.vetoes:
                rule = str(decision.vetoes[0].get("rule") or "unknown")
            self.store.record_signal({
                "key": shadow_key(signal.strategy, signal.instrument, signal.side.value,
                                  signal.decision_ns),
                "ts_ns": int(signal.decision_ns), "strategy": signal.strategy,
                "instrument": signal.instrument, "side": signal.side.value,
                "timeframe": signal.timeframe, "entry": entry,
                "stop": float(signal.stop_price),
                "target": float(signal.target_price) if signal.target_price else None,
                "horizon": int(getattr(signal, "horizon_bars", 0) or
                               cfg.shadow_default_horizon_bars),
                "action": decision.action, "rule": rule,
                "size_mult": _num(diag.get("caution_multiplier")),
                "layers": diag.get("brain_layers") or {},
                "meta_p": _num(diag.get("meta_probability")),
                "features": {k: float(v) for k, v in features.items() if _finite(v)},
                "regime": decision.regime, "cost_r": min(max(cost_r, 0.0), 2.0),
            })
        except Exception as exc:  # noqa: BLE001 - recording must never cost a decision
            self.last_error = f"record: {type(exc).__name__}: {exc}"[:300]

    def resolve(self, snap, now_ns: Optional[int] = None, limit: int = 300) -> int:
        cfg = self.cfg
        if not (cfg.enabled and cfg.shadow_book) or snap is None:
            return 0
        now = int(now_ns or self._clock())
        done = 0
        try:
            for row in self.store.open_signals(limit=limit):
                tf = row.get("timeframe") or ""
                frame = (snap.frames_for(tf) if tf else snap.frames).get(row["instrument"])
                horizon = int(row["horizon"])
                span = TF_SECONDS.get(tf, 3600) * horizon * _SEC
                if frame is None or len(frame) == 0:
                    if now - int(row["ts_ns"]) > 3 * span:
                        self.store.resolve_signal(row["key"], outcome_r=None,
                                                  exit_kind="expired", bars_seen=0,
                                                  resolved=True)
                    continue
                after = frame[_index_ns(frame.index) > int(row["ts_ns"])]
                if len(after) == 0:
                    continue
                ok, r, kind, used = resolve_path(
                    side=row["side"], entry=float(row["entry"]), stop=float(row["stop"]),
                    target=row.get("target"), horizon=horizon, bars=after,
                    cost_r=float(row.get("cost_r") or 0.0))
                if not ok and now - int(row["ts_ns"]) > 3 * span:
                    ok, kind = True, "expired"
                    last = float(after["close"].iloc[-1])
                    risk = abs(float(row["entry"]) - float(row["stop"]))
                    if risk > 0:
                        sign = 1.0 if row["side"] == "BUY" else -1.0
                        r = sign * (last - float(row["entry"])) / risk \
                            - float(row.get("cost_r") or 0.0)
                if ok or used != int(row.get("bars_seen") or 0):
                    self.store.resolve_signal(row["key"], outcome_r=r, exit_kind=kind,
                                              bars_seen=used, resolved=ok)
                    done += int(ok)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"resolve: {type(exc).__name__}: {exc}"[:300]
        if done:
            with self._lock:
                self._sim_cache.clear()
        return done

    # ------------------------------------------------------------------ #
    # loss streaks and cooldowns
    # ------------------------------------------------------------------ #

    def on_trades(self, autopsies: List[Dict[str, Any]], now_ns: Optional[int] = None) -> None:
        """Advance loss streaks with newly closed trades (oldest first)."""
        if not autopsies:
            return
        cfg = self.cfg
        now = int(now_ns or self._clock())
        streaks = self.store.get_state("streaks", {"account": 0, "strategies": {}}) or {}
        streaks.setdefault("account", 0)
        streaks.setdefault("strategies", {})
        cooldowns = self.store.get_state("cooldowns", {}) or {}
        events: List[Dict[str, Any]] = []
        for a in sorted(autopsies, key=lambda x: int(x.get("closed_ns") or 0)):
            strat = str(a.get("strategy") or "?")
            outcome = str(a.get("outcome") or "")
            r = _num(a.get("r_multiple")) or 0.0
            lost = outcome == "loss" or (outcome not in ("win", "scratch") and r < -0.05)
            won = outcome == "win" or (outcome not in ("loss", "scratch") and r > 0.05)
            if lost:
                streaks["account"] += 1
                streaks["strategies"][strat] = int(streaks["strategies"].get(strat, 0)) + 1
            elif won:
                streaks["account"] = 0
                streaks["strategies"][strat] = 0
            if not cfg.enabled:
                continue
            if cfg.loss_streak_limit and streaks["account"] >= cfg.loss_streak_limit \
                    and cfg.loss_streak_cooldown_hours > 0:
                until = now + int(cfg.loss_streak_cooldown_hours * 3600 * _SEC)
                cooldowns["*"] = {"until_ns": until, "losses": streaks["account"],
                                  "reason": f"{streaks['account']} losing trades in a row; "
                                            f"the account rests for "
                                            f"{cfg.loss_streak_cooldown_hours:g}h"}
                events.append({"scope": "*", **cooldowns["*"]})
                streaks["account"] = 0
            n = int(streaks["strategies"].get(strat, 0))
            if cfg.strategy_loss_streak_limit and n >= cfg.strategy_loss_streak_limit \
                    and cfg.strategy_cooldown_hours > 0:
                until = now + int(cfg.strategy_cooldown_hours * 3600 * _SEC)
                rest_h = cfg.strategy_cooldown_hours
                cooldowns[strat] = {"until_ns": until, "losses": n,
                                    "reason": f"{strat}: {n} losing trades in a row; the "
                                              f"strategy rests for {rest_h:g}h"}
                events.append({"scope": strat, **cooldowns[strat]})
                streaks["strategies"][strat] = 0
        self.store.set_state("streaks", streaks)
        self.store.set_state("cooldowns", cooldowns)
        with self._lock:
            self._version += 1
            self._layer_cache.clear()
        for ev in events:
            self._emit(EVENT_COOLDOWN, ev)

    def cooldowns(self, now_ns: Optional[int] = None) -> Dict[str, str]:
        if not self.cfg.enabled:
            return {}
        now = int(now_ns or self._clock())
        out: Dict[str, str] = {}
        for scope, c in (self.store.get_state("cooldowns", {}) or {}).items():
            try:
                until = int(c.get("until_ns", 0))
            except (TypeError, ValueError):
                continue
            if until > now:
                when = dt.datetime.fromtimestamp(until / 1e9, tz=dt.timezone.utc)
                out[scope] = f"{c.get('reason', 'loss streak')} (until {when:%Y-%m-%d %H:%M} UTC)"
        return out

    def clear_cooldown(self, scope: str, by: str) -> Dict[str, Any]:
        cooldowns = self.store.get_state("cooldowns", {}) or {}
        if scope not in cooldowns:
            raise ValueError(f"no cooldown for {scope!r}")
        cooldowns.pop(scope)
        self.store.set_state("cooldowns", cooldowns)
        self._audit(EVENT_COOLDOWN, {"cleared": scope}, actor=by)
        return {"cleared": scope}

    # ------------------------------------------------------------------ #
    # per-strategy layers: drift, equity curve, allocation
    # ------------------------------------------------------------------ #

    def baseline(self, strategy: str) -> Dict[str, float]:
        base = (self.store.get_state("baselines", {}) or {}).get(strategy)
        if base and base.get("n", 0) >= 10:
            return {"mean_r": float(base["mean_r"]), "sd_r": max(float(base["sd_r"]), 0.5),
                    "source": "lab"}
        return {"mean_r": float(self.cfg.drift_expected_r), "sd_r": 1.0, "source": "default"}

    def _strategy_r(self, strategy: str, limit: int = 300) -> List[Dict[str, Any]]:
        rows = self.memory.autopsies(strategy, limit=limit) if self.memory is not None else []
        return sorted(rows, key=lambda a: int(a.get("closed_ns") or 0))

    def strategy_layers(self, strategy: str, regime: str = ""
                        ) -> Tuple[float, List[str], Dict[str, float]]:
        """(multiplier <= 1, reasons, per-layer multipliers). Fail-safe: 1.0."""
        cfg = self.cfg
        if not cfg.enabled or not strategy:
            return 1.0, [], {}
        key = (strategy, regime or "", self._version)
        with self._lock:
            hit = self._layer_cache.get(key)
        if hit is not None:
            return hit
        reasons: List[str] = []
        layers: Dict[str, float] = {}
        try:
            rows = self._strategy_r(strategy)
            r = [float(a.get("r_multiple") or 0.0) for a in rows]
            base = self.baseline(strategy)
            if cfg.drift_enabled and len(r) >= cfg.drift_min_trades:
                c = stats.cusum_down(r, mu0=base["mean_r"], sigma=base["sd_r"],
                                     k=cfg.drift_k, h=cfg.drift_h)
                # Hysteresis: raise above h, clear only once back under h/2, so
                # a statistic hovering near the line cannot flap size every trade.
                prev = self._drift_alarmed.get(strategy, False)
                alarmed = bool(c["alarm"] or (prev and not c["recovering"]))
                if alarmed:
                    layers["drift"] = float(cfg.drift_multiplier)
                    reasons.append(f"drift: live results fell below the {base['source']} "
                                   f"baseline ({base['mean_r']:+.2f}R); CUSUM {c['stat']:.1f}")
                if alarmed != self._drift_alarmed.get(strategy, False):
                    self._drift_alarmed[strategy] = alarmed
                    self.store.set_state("drift_alarmed", self._drift_alarmed)
                    self._emit(EVENT_DRIFT, {"strategy": strategy, "alarm": alarmed,
                                             "cusum": c["stat"], "baseline": base})
            if cfg.equity_filter_enabled:
                below = stats.below_equity_average(r, cfg.equity_filter_window)
                if below:
                    layers["equity_curve"] = float(cfg.equity_filter_multiplier)
                    reasons.append(f"equity curve: below its {cfg.equity_filter_window}-trade "
                                   "average")
            if cfg.allocation_enabled and regime:
                cell = [float(a.get("r_multiple") or 0.0) for a in rows
                        if (a.get("regime") or "") == regime]
                if len(cell) >= 5:
                    post = stats.posterior_positive(cell, prior_mean=cfg.allocation_prior_mean_r,
                                                    prior_sd=cfg.allocation_prior_sd)
                    m = stats.allocation_multiplier(post["p_positive"], cfg.allocation_floor)
                    if m < 1.0:
                        layers["allocation"] = float(m)
                        reasons.append(f"allocation: in '{regime}' the chance this strategy "
                                       f"is profitable is {post['p_positive']:.0%} "
                                       f"({post['n']} trades)")
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"layers: {type(exc).__name__}: {exc}"[:300]
            return 1.0, [], {}
        mult = min([1.0] + list(layers.values()))
        out = (float(mult), reasons, layers)
        with self._lock:
            self._layer_cache[key] = out
        return out

    # ------------------------------------------------------------------ #
    # similar situations
    # ------------------------------------------------------------------ #

    def similarity(self, strategy: str, features: Dict[str, float]
                   ) -> Tuple[float, str, Dict[str, Any]]:
        cfg = self.cfg
        if not (cfg.enabled and cfg.similarity_enabled) or not features:
            return 1.0, "", {}
        try:
            ref = self._similarity_reference(strategy)
            if ref is None:
                return 1.0, "", {"n": 0}
            _, _, X, y, names = ref
            q = np.array([float(features.get(n, np.nan)) for n in names], dtype=float)
            res = stats.nearest_outcomes(X, y, q, cfg.similarity_k)
            if res.get("n", 0) and res.get("ci_high") is not None and res["ci_high"] < 0:
                return (float(cfg.similarity_multiplier),
                        f"similar situations: the {res['n']} most similar past signals "
                        f"averaged {res['mean_r']:+.2f}R (90% CI up to {res['ci_high']:+.2f})",
                        res)
            return 1.0, "", res
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"similarity: {type(exc).__name__}: {exc}"[:300]
            return 1.0, "", {}

    def _similarity_reference(self, strategy: str):
        cfg = self.cfg
        now = time.monotonic()
        with self._lock:
            hit = self._sim_cache.get(strategy)
        if hit is not None and now - hit[1] < 600:
            return hit
        rows = self.store.resolved_signals(strategy=strategy, limit=5000)
        rows = [r for r in rows if r.get("features")]
        if len(rows) < cfg.similarity_min_samples:
            return None
        X, names = feature_matrix(rows)
        y = np.array([float(r["outcome_r"]) for r in rows], dtype=float)
        ref = (len(rows), now, X, y, names)
        with self._lock:
            self._sim_cache[strategy] = ref
        return ref

    # ------------------------------------------------------------------ #
    # stress and the meta filter
    # ------------------------------------------------------------------ #

    def stress_settings(self) -> Tuple[Dict[str, float], float]:
        cfg = self.cfg
        if not (cfg.enabled and cfg.stress_enabled):
            return {}, 0.0
        return dict(cfg.stress_scenarios), float(cfg.stress_loss_limit_pct)

    def meta_gate(self):
        """The owner-approved filter, loaded once and re-verified by hash."""
        active = self.store.active_model()
        if active is None:
            self._meta_gate, self._meta_gate_id = None, None
            return None
        if self._meta_gate_id == active["id"]:
            return self._meta_gate
        try:
            import hashlib
            path = Path(active["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != active["sha256"]:
                raise ValueError("the model file does not match its recorded hash")
            from ..research.metalabel import MetaGate
            gate = MetaGate.load(path)
        except Exception as exc:  # noqa: BLE001 - an unusable filter passes everything
            self.last_error = f"meta: {type(exc).__name__}: {exc}"[:300]
            self._meta_gate, self._meta_gate_id = None, active["id"]
            return None
        self._meta_gate, self._meta_gate_id = gate, active["id"]
        return gate

    def approve_model(self, model_id: str, by: str) -> Dict[str, Any]:
        m = self.store.model(model_id)
        if m is None:
            raise ValueError("no such model")
        if not (m["report"] or {}).get("eligible"):
            raise ValueError("this model did not pass its out-of-sample check")
        self.store.set_model_status(model_id, "active", by)
        self._meta_gate_id = None
        self._audit(EVENT_MODEL, {"approved": model_id,
                                  "holdout": (m["report"] or {}).get("holdout")}, actor=by)
        return {"active": model_id}

    def retire_model(self, by: str) -> Dict[str, Any]:
        active = self.store.active_model()
        if active is None:
            raise ValueError("no active model")
        self.store.set_model_status(active["id"], "retired", by)
        self._meta_gate, self._meta_gate_id = None, None
        self._audit(EVENT_MODEL, {"retired": active["id"]}, actor=by)
        return {"retired": active["id"]}

    # ------------------------------------------------------------------ #
    # background: the lab and the weekly report
    # ------------------------------------------------------------------ #

    def tick(self, now_ns: Optional[int] = None) -> None:
        cfg = self.cfg
        if not cfg.enabled:
            return
        now = int(now_ns or self._clock())
        t = dt.datetime.fromtimestamp(now / 1e9, tz=dt.timezone.utc)
        if cfg.lab_enabled and t.hour == cfg.lab_hour_utc:
            last = self.store.get_state("lab_last_date", "")
            if last != t.date().isoformat():
                self.start_lab(by="schedule", date=t.date().isoformat())
        if t.weekday() == cfg.weekly_report_dow and t.hour >= cfg.weekly_report_hour_utc:
            week = f"{t.isocalendar()[0]}-W{t.isocalendar()[1]:02d}"
            if self.store.get_state("weekly_last", "") != week:
                self.store.set_state("weekly_last", week)
                self.weekly_report(now)
        if t.minute < 2 and t.hour == 3:
            self.store.prune()

    @property
    def lab_running(self) -> bool:
        return self._lab_thread is not None and self._lab_thread.is_alive()

    def start_lab(self, by: str, date: Optional[str] = None) -> bool:
        if self.lab is None or self.lab_running:
            return False
        cfg = self.cfg
        if date:
            self.store.set_state("lab_last_date", date)

        def work() -> None:
            from .lab import run_safely
            report = run_safely(self.lab, max_seconds=cfg.lab_max_minutes * 60,
                                max_bars=cfg.lab_max_bars, meta_min_auc=cfg.meta_min_auc,
                                train_meta=cfg.meta_auto_train)
            report["by"] = by
            self._absorb_lab(report)

        self._lab_thread = threading.Thread(target=work, name="brain-lab", daemon=True)
        self._lab_thread.start()
        return True

    def _absorb_lab(self, report: Dict[str, Any]) -> None:
        baselines = report.get("baselines") or {}
        if baselines:
            merged = dict(self.store.get_state("baselines", {}) or {})
            merged.update(baselines)
            self.store.set_state("baselines", merged)
            with self._lock:
                self._version += 1
                self._layer_cache.clear()
        meta = report.get("meta") or {}
        if meta.get("model_id") and meta.get("eligible"):
            self.store.add_model(meta["model_id"], meta["path"], meta["sha256"], meta)
        self.store.add_lab_run(report)
        self._emit(EVENT_LAB, {
            "strategies": [{"strategy": s.get("strategy"), "status": s.get("status"),
                            "mean_r": s.get("mean_r"), "n": s.get("n")}
                           for s in report.get("strategies", [])],
            "proposals": [{"path": p.get("path"), "status": p.get("status")}
                          for p in report.get("proposals", [])],
            "meta_candidate": meta.get("model_id") if meta.get("eligible") else None,
            "errors": (report.get("errors") or [])[:3],
            "seconds": report.get("seconds")})

    def weekly_report(self, now_ns: Optional[int] = None) -> Dict[str, Any]:
        now = int(now_ns or self._clock())
        week_ago = now - 7 * 86_400 * _SEC
        rows = self.store.resolved_signals(since_ns=week_ago)
        card = scorecard(rows)
        trades = [a for a in (self.memory.autopsies(None, limit=2000) if self.memory else [])
                  if int(a.get("closed_ns") or 0) >= week_ago]
        by_strategy: Dict[str, List[float]] = {}
        for a in trades:
            by_strategy.setdefault(a.get("strategy") or "?", []).append(
                float(a.get("r_multiple") or 0.0))
        lessons = []
        try:
            lessons = [lz.to_dict() for lz in self.memory.all_lessons()][:20] \
                if self.memory is not None else []
        except Exception:  # noqa: BLE001
            lessons = []
        payload = {
            "week_ending": dt.datetime.fromtimestamp(now / 1e9, tz=dt.timezone.utc)
            .strftime("%Y-%m-%d"),
            "trades": stats.summarise_r([float(a.get("r_multiple") or 0.0) for a in trades]),
            "by_strategy": {k: stats.summarise_r(v) for k, v in by_strategy.items()},
            "shadow": card,
            "cooldowns_this_week": [c for c in (self.store.get_state("cooldowns", {}) or {})
                                    .values() if int(c.get("until_ns", 0)) >= week_ago],
            "drift_alarms": {k: v for k, v in self._drift_alarmed.items() if v},
            "meta_live": meta_live_check(rows),
            "lessons_active": len(lessons),
        }
        self.store.add_report("weekly", payload)
        self._emit(EVENT_REPORT, payload)
        return payload

    # ------------------------------------------------------------------ #
    # read model
    # ------------------------------------------------------------------ #

    def view(self, strategies: Optional[List[str]] = None, regime: str = "") -> Dict[str, Any]:
        cfg = self.cfg
        now = self._clock()
        rows = self.store.resolved_signals(since_ns=now - 180 * 86_400 * _SEC)
        strat_rows = []
        for name in strategies or []:
            m, reasons, layers = self.strategy_layers(name, regime)
            r = [float(a.get("r_multiple") or 0.0) for a in self._strategy_r(name)]
            base = self.baseline(name)
            c = stats.cusum_down(r, mu0=base["mean_r"], sigma=base["sd_r"], k=cfg.drift_k,
                                 h=cfg.drift_h) if r else None
            strat_rows.append({"strategy": name, "multiplier": m, "reasons": reasons,
                               "layers": layers, "live": stats.summarise_r(r),
                               "baseline": base, "cusum": c,
                               "drift_threshold": cfg.drift_h})
        streaks = self.store.get_state("streaks", {"account": 0, "strategies": {}}) or {}
        return {
            "enabled": bool(cfg.enabled),
            "config": cfg.model_dump(mode="json") if hasattr(cfg, "model_dump") else {},
            "cooldowns": self.cooldowns(now),
            "streaks": streaks,
            "shadow_counts": self.store.shadow_counts(),
            "scorecard": scorecard(rows),
            "strategies": strat_rows,
            "models": self.store.models(),
            "meta_live": meta_live_check(rows),
            "lab_running": self.lab_running,
            "lab_runs": self.store.lab_runs(5),
            "reports": self.store.reports("weekly", 6),
            "baselines": self.store.get_state("baselines", {}) or {},
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ #

    def _audit(self, event: str, payload: Dict[str, Any], actor: str = "brain") -> None:
        try:
            self.audit.append(event, payload, actor=actor)
        except Exception:  # noqa: BLE001
            pass

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        self._audit(event, payload)
        if self.notify is not None:
            try:
                self.notify(event, payload)
            except Exception:  # noqa: BLE001
                pass


def _num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _finite(v) -> bool:
    return _num(v) is not None


def _index_ns(index) -> np.ndarray:
    """Bar start times in NANOseconds, whatever unit pandas chose (pandas 3
    defaults to microseconds; a bare .asi8 would be 1000x off)."""
    try:
        return index.as_unit("ns").asi8
    except (AttributeError, TypeError, ValueError):
        return np.asarray(index.values, dtype="datetime64[ns]").astype("int64")
