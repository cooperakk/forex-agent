"""Meta-labelling, wired: features from a signal, labels from what followed,
a filter the agent can consult, and a file the runtime can load.

The primary strategy says WHICH WAY. The meta-model says WHETHER, from the
state the signal was raised in -- the strategy's own features (ADX, ATR,
channel excess...), the bar's volatility percentile, the session hour, the
day of week. Its label is the one thing a filter should learn from: did
acting on this signal, over its declared horizon, earn more than the cost of
acting? ``1`` if ``side x forward_return > cost``, else ``0``.

Two rules keep this from being a second overfitting machine:

1. **The meta-model is trained on a window the candidate is not scored on.**
   ``split_for_meta`` cuts the history; the fit sees the first part and every
   acceptance gate runs on the rest. A filter evaluated on the signals it was
   fitted to is a lookup table.
2. **A fitted filter is a trial.** It goes into the trial ledger like any
   parameter set, because "primary + filter" is a configuration that was
   chosen among others.

At runtime the agent loads ``agent.meta_model_path`` and consults
``MetaGate.decide`` before the risk engine. A missing or unusable model is
"pass everything through" -- a broken filter must never silently stop all
trading -- and the model file's hash is part of the runtime fingerprint, so
a swapped model is a different system.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..core.types import Signal
from ..strategy.meta import MetaLabeler

FEATURE_NAMES_BASE = ("strength", "hour_utc", "dow", "vol_pctile", "adx", "atr_pct")


def bar_context_features(frame: pd.DataFrame, index: int) -> Dict[str, float]:
    """Causal features of the bar a signal was raised on."""
    out: Dict[str, float] = {}
    if frame is None or index < 0 or index >= len(frame):
        return out
    ts = frame.index[index]
    out["hour_utc"] = float(getattr(ts, "hour", 0))
    out["dow"] = float(getattr(ts, "weekday", lambda: 0)())
    closes = frame["close"].to_numpy(dtype=float)[: index + 1]
    if closes.size >= 60:
        rets = np.diff(np.log(closes[-260:]))
        window = 20
        if rets.size > window + 20:
            vols = pd.Series(rets).rolling(window).std().dropna().to_numpy()
            if vols.size and vols[-1] == vols[-1]:
                out["vol_pctile"] = float((vols <= vols[-1]).mean())
    try:
        from ..strategy.base import adx as _adx, atr as _atr
        sub = frame.iloc[max(0, index - 200): index + 1]
        a = _atr(sub, 14)
        d = _adx(sub, 14)
        if len(a) and a.iloc[-1] == a.iloc[-1] and closes[-1] > 0:
            out["atr_pct"] = float(a.iloc[-1] / closes[-1] * 100)
        if len(d) and d.iloc[-1] == d.iloc[-1]:
            out["adx"] = float(d.iloc[-1])
    except Exception:  # noqa: BLE001 - features are optional
        pass
    return out


def signal_features(sig: Signal, context: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    feats: Dict[str, float] = {"strength": float(sig.strength)}
    for k, v in (sig.features or {}).items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv == fv:
            feats[f"sig_{k}"] = fv
    for k, v in (context or {}).items():
        feats[k] = float(v)
    return feats


@dataclass
class MetaTrainingSet:
    X: np.ndarray
    y: np.ndarray
    names: List[str]
    n_signals: int
    positive_rate: float


def training_set_from_log(signal_log: Sequence[Dict[str, Any]], *,
                          cost_return: float = 0.0003) -> Optional[MetaTrainingSet]:
    """Features and labels from a backtest's signal log.

    ``cost_return`` is the round trip as a fraction of price (0.0003 ~ 3 pips
    on EUR/USD): the label asks whether the signal beat it, not whether the
    price moved at all.
    """
    rows = [r for r in signal_log if r.get("fwd_ret_h") is not None and r.get("features")]
    if len(rows) < 50:
        return None
    names = sorted({k for r in rows for k in r["features"]})
    X = np.array([[float(r["features"].get(k, 0.0)) for k in names] for r in rows], dtype=float)
    y = np.array([1 if float(r["side_sign"]) * float(r["fwd_ret_h"]) > cost_return else 0
                  for r in rows], dtype=int)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    return MetaTrainingSet(X, y, names, len(rows), float(y.mean()))


def split_for_meta(data: Dict[str, pd.DataFrame], train_fraction: float = 0.6
                   ) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
    """(train window, holdout window), by time, identical cut for every instrument."""
    if not (0.3 <= train_fraction <= 0.8):
        raise ValueError("train_fraction must be within [0.3, 0.8]")
    first = next(iter(data.values()))
    cut = first.index[int(len(first) * train_fraction)]
    train = {s: df[df.index < cut] for s, df in data.items()}
    hold = {s: df[df.index >= cut] for s, df in data.items()}
    return train, hold


class MetaGate:
    """The runtime face of a fitted filter."""

    def __init__(self, labeler: MetaLabeler, *, source: str = "", sha256: str = "") -> None:
        self.labeler = labeler
        self.source = source
        self.sha256 = sha256

    @property
    def active(self) -> bool:
        return self.labeler.model is not None and self.labeler.report.trained

    def decide(self, sig: Signal, context: Optional[Dict[str, float]] = None
               ) -> Tuple[bool, Optional[float], float]:
        """(act?, probability, size multiplier). Unusable model -> (True, None, 1.0)."""
        if not self.active:
            return True, None, 1.0
        feats = signal_features(sig, context)
        act, p = self.labeler.should_act(feats)
        scale = self.labeler.size_scale(feats) if act else 0.0
        return act, p, float(scale)

    # -- persistence ------------------------------------------------------- #

    def save(self, path: str | Path) -> str:
        import joblib
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"labeler": self.labeler, "source": self.source,
                     "report": self.labeler.report.to_dict()}, path)
        self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        return self.sha256

    @classmethod
    def load(cls, path: str | Path) -> "MetaGate":
        import joblib
        path = Path(path)
        payload = joblib.load(path)
        gate = cls(payload["labeler"], source=str(payload.get("source", "")),
                   sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        return gate


def fit_meta_gate(signal_log: Sequence[Dict[str, Any]], *, cost_return: float = 0.0003,
                  payoff_win: float = 2.0, payoff_loss: float = -1.0,
                  seed: int = 20260914) -> Tuple[Optional[MetaGate], Dict[str, Any]]:
    """Fit a filter on a training window's signals. Returns (gate or None, report)."""
    ts = training_set_from_log(signal_log, cost_return=cost_return)
    if ts is None:
        return None, {"trained": False, "reason": "fewer than 50 labelled signals"}
    labeler = MetaLabeler(seed=seed)
    report = labeler.fit(ts.X, ts.y, ts.names, payoff_win=payoff_win, payoff_loss=payoff_loss)
    out = dict(report.to_dict())
    out.update({"n_signals": ts.n_signals, "positive_rate": round(ts.positive_rate, 4),
                "features": ts.names})
    if not report.trained:
        return None, out
    return MetaGate(labeler, source="fit_meta_gate"), out
