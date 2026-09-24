"""Meta-labelling.

The primary model decides the *side*. A secondary model decides whether to
*act*, and at what size. This split is de Prado's meta-labelling, and it is the
formal name for what the research brief proposes when it says news should
first be tested as a filter on existing entries rather than as a signal
generator.

Why it works better than one big model: the side problem and the act problem
have different natural features and very different base rates. A primary model
with 45% accuracy and good payoff asymmetry can be highly profitable once a
filter removes its worst setups -- and a filter is a far easier thing to learn
than a direction.

Two properties are enforced here:

* **Calibration.** A raw classifier score is not a probability. Isotonic
  calibration on held-out data turns it into one; until that has happened the
  output is explicitly marked ``calibrated=False`` and the sizing layer
  refuses to treat it as a probability.
* **Honest thresholds.** The act/skip threshold is chosen on validation data
  against expected value *after cost*, not on the accuracy that looks best.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

try:  # scikit-learn is optional; the fallback keeps the system runnable.
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import brier_score_loss, roc_auc_score
    _SKLEARN = True
    try:
        # scikit-learn >= 1.6 replaced cv="prefit" with an explicit wrapper.
        from sklearn.frozen import FrozenEstimator
        _FROZEN = True
    except ImportError:  # pragma: no cover - older scikit-learn
        FrozenEstimator = None  # type: ignore[assignment]
        _FROZEN = False
except ImportError:  # pragma: no cover
    _SKLEARN = False
    _FROZEN = False


@dataclass
class MetaModelReport:
    trained: bool
    n_samples: int
    effective_n: float
    auc: Optional[float] = None
    brier: Optional[float] = None
    threshold: float = 0.5
    expected_value_at_threshold: float = 0.0
    precision_at_threshold: float = 0.0
    recall_at_threshold: float = 0.0
    feature_importance: Dict[str, float] = field(default_factory=dict)
    calibrated: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trained": self.trained, "n_samples": self.n_samples,
            "effective_n": round(self.effective_n, 1),
            "auc": round(self.auc, 4) if self.auc is not None else None,
            "brier": round(self.brier, 5) if self.brier is not None else None,
            "threshold": round(self.threshold, 3),
            "expected_value_at_threshold": round(self.expected_value_at_threshold, 5),
            "precision": round(self.precision_at_threshold, 4),
            "recall": round(self.recall_at_threshold, 4),
            "calibrated": self.calibrated,
            "feature_importance": {k: round(v, 4) for k, v in self.feature_importance.items()},
            "notes": self.notes,
        }


class MetaLabeler:
    """Secondary act/skip model over the primary strategy's signals."""

    def __init__(self, *, min_samples: int = 200, max_depth: int = 4,
                 n_estimators: int = 200, seed: int = 20260914) -> None:
        self.min_samples = min_samples
        self.max_depth = max_depth
        self.n_estimators = n_estimators
        self.seed = seed
        self.model = None
        self.feature_names: List[str] = []
        self.report = MetaModelReport(trained=False, n_samples=0, effective_n=0.0)

    # ------------------------------------------------------------------ #

    def fit(
        self,
        features: np.ndarray,
        labels: np.ndarray,
        feature_names: Sequence[str],
        *,
        sample_weights: Optional[np.ndarray] = None,
        effective_n: Optional[float] = None,
        payoff_win: float = 2.0,
        payoff_loss: float = -1.0,
        cost_r: float = 0.05,
        validation_fraction: float = 0.3,
        min_keep_fraction: float = 0.15,
    ) -> MetaModelReport:
        notes: List[str] = []
        X = np.asarray(features, dtype=float)
        y = np.asarray(labels, dtype=int)
        n = X.shape[0]
        eff = float(effective_n if effective_n is not None else n)
        self.feature_names = list(feature_names)

        if not _SKLEARN:
            notes.append("scikit-learn unavailable: the filter is disabled and every "
                         "primary signal passes through unchanged")
            self.report = MetaModelReport(False, n, eff, notes=notes)
            return self.report
        if n < self.min_samples:
            notes.append(f"only {n} samples (minimum {self.min_samples}); refusing to fit")
            self.report = MetaModelReport(False, n, eff, notes=notes)
            return self.report
        if eff < self.min_samples * 0.5:
            notes.append(
                f"effective sample size is {eff:.0f} against {n} raw samples: the "
                "observations overlap heavily and any fitted threshold is fragile")
        if len(np.unique(y)) < 2:
            notes.append("labels have a single class; nothing to learn")
            self.report = MetaModelReport(False, n, eff, notes=notes)
            return self.report

        # Time-ordered split. Shuffling here would leak the future.
        cut = int(n * (1 - validation_fraction))
        cut = max(self.min_samples // 2, min(cut, n - 30))
        X_tr, y_tr = X[:cut], y[:cut]
        X_va, y_va = X[cut:], y[cut:]
        w_tr = sample_weights[:cut] if sample_weights is not None else None
        if len(np.unique(y_tr)) < 2 or len(np.unique(y_va)) < 2:
            notes.append("a split contains a single class; refusing to fit")
            self.report = MetaModelReport(False, n, eff, notes=notes)
            return self.report

        base = RandomForestClassifier(
            n_estimators=self.n_estimators, max_depth=self.max_depth,
            min_samples_leaf=max(5, cut // 50), class_weight="balanced_subsample",
            random_state=self.seed, n_jobs=1,
        )
        base.fit(X_tr, y_tr, sample_weight=w_tr)

        calibrated = False
        model = base
        # Calibrate on one half of the validation rows and evaluate on the
        # other. Fitting the isotonic map on the same rows the threshold is
        # then tuned on made the calibration look perfect by construction.
        calibration_cut = len(y_va) // 2
        X_cal, y_cal = X_va[:calibration_cut], y_va[:calibration_cut]
        X_va, y_va = X_va[calibration_cut:], y_va[calibration_cut:]
        if len(y_cal) >= 50 and len(np.unique(y_cal)) == 2:
            try:
                wrapped = FrozenEstimator(base) if _FROZEN else base
                model = (CalibratedClassifierCV(wrapped, method="isotonic")
                         if _FROZEN else
                         CalibratedClassifierCV(base, method="isotonic", cv="prefit"))
                model.fit(X_cal, y_cal)
                calibrated = True
            except (ValueError, RuntimeError, TypeError) as exc:  # pragma: no cover
                model = base
                notes.append(f"isotonic calibration failed ({exc.__class__.__name__}); "
                             "scores are NOT probabilities and size scaling stays at 1.0")
        else:
            notes.append("validation set too small to calibrate; scores are NOT probabilities")

        proba = model.predict_proba(X_va)[:, 1]
        auc = float(roc_auc_score(y_va, proba)) if len(np.unique(y_va)) > 1 else None
        brier = float(brier_score_loss(y_va, proba))

        threshold, ev, prec, rec = self._choose_threshold(
            y_va, proba, payoff_win, payoff_loss, cost_r, min_keep_fraction)
        if ev <= 0:
            notes.append(
                "no threshold produces positive expected value after cost on the "
                "validation set: this filter should not be deployed")

        importance = {}
        if hasattr(base, "feature_importances_"):
            importance = {self.feature_names[i]: float(v)
                          for i, v in enumerate(base.feature_importances_)
                          if i < len(self.feature_names)}

        self.model = model
        self.report = MetaModelReport(
            trained=True, n_samples=n, effective_n=eff, auc=auc, brier=brier,
            threshold=threshold, expected_value_at_threshold=ev,
            precision_at_threshold=prec, recall_at_threshold=rec,
            feature_importance=dict(sorted(importance.items(),
                                           key=lambda kv: kv[1], reverse=True)),
            calibrated=calibrated, notes=notes,
        )
        return self.report

    @staticmethod
    def _choose_threshold(y: np.ndarray, proba: np.ndarray, payoff_win: float,
                          payoff_loss: float, cost_r: float,
                          min_keep_fraction: float = 0.15) -> Tuple[float, float, float, float]:
        """Pick the threshold maximising expected value *after cost*.

        Accuracy is the wrong objective: a filter that keeps 95% of trades and
        is right 60% of the time can be worse than one that keeps 20% and is
        right 70%, once the cost of each trade is charged.

        ``min_keep_fraction`` is a deliberate constraint on that optimisation.
        Pure EV maximisation converges on "trade only the certainties", which
        looks excellent per trade and produces two trades a year -- a result
        with no statistical power and no practical use. The threshold must keep
        at least this share of the primary model's signals.
        """
        n = len(proba)
        min_keep = max(10, int(np.ceil(n * max(0.0, min(1.0, min_keep_fraction)))))
        best = (0.5, -1e9, 0.0, 0.0)
        for thr in np.unique(np.round(proba, 3)):
            taken = proba >= thr
            k = int(taken.sum())
            if k < min_keep:
                continue
            wins = float(y[taken].sum())
            losses = k - wins
            ev = (wins * payoff_win + losses * payoff_loss) / k - cost_r
            precision = wins / k
            recall = wins / max(1.0, float(y.sum()))
            if ev > best[1]:
                best = (float(thr), float(ev), precision, recall)
        return best

    # ------------------------------------------------------------------ #

    def act_probability(self, features: Dict[str, float]) -> Optional[float]:
        """Probability that the primary signal is worth acting on.

        ``None`` when no usable model exists -- and ``None`` means "pass the
        signal through", not "skip". A broken filter must not silently stop
        all trading.
        """
        if self.model is None or not self.feature_names:
            return None
        try:
            x = np.array([[float(features.get(k, 0.0)) for k in self.feature_names]])
            return float(self.model.predict_proba(x)[0, 1])
        except (ValueError, AttributeError):  # pragma: no cover
            return None

    def should_act(self, features: Dict[str, float]) -> Tuple[bool, Optional[float]]:
        p = self.act_probability(features)
        if p is None:
            return True, None
        return p >= self.report.threshold, p

    def size_scale(self, features: Dict[str, float], *, floor: float = 0.4,
                   cap: float = 1.0) -> float:
        """Confidence-scaled size multiplier, only when calibrated.

        Without calibration the score has no probabilistic meaning and scaling
        by it would be numerology, so the multiplier stays at 1.0.
        """
        if not self.report.calibrated:
            return 1.0
        p = self.act_probability(features)
        if p is None:
            return 1.0
        thr = self.report.threshold
        if p < thr:
            return floor
        span = 1.0 - thr
        if span <= 1e-6:
            # A degenerate threshold at 1.0 carries no gradient to scale along.
            return cap
        return float(min(cap, floor + (cap - floor) * (p - thr) / span))
