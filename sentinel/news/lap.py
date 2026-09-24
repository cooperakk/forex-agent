"""Lookahead-propensity test.

The question: when a language model scores a historical article, is it using
the text, or is it remembering what happened next? A model trained through 2025
that is shown a 2023 article already knows the outcome, and any backtest built
on that is measuring recall.

The test, following the lookahead-propensity literature:

1. Ask the model, for each article, whether it can date the article and what it
   knows about the subsequent period. That gives a per-article LAP score.
2. Regress the signal's realised performance on LAP.
3. If performance falls as LAP falls, the apparent skill was memory.

``lap_slope`` is the number that matters. A materially positive slope -- higher
familiarity going with better performance -- means the result does not transfer
to unseen articles, and the acceptance protocol treats the strategy as unproven
regardless of how good the backtest looked.

**How much this test can actually detect, measured rather than asserted.**

The slope regression is run against realised per-article performance, whose
noise is the noise of an R-multiple: a standard deviation around 1.0. Simulating
the test at that noise level gives its detection rate for a contamination of
0.5R per unit of familiarity -- an enormous effect, the difference between a
fully-remembered article and a completely unfamiliar one being half a unit of
risk:

    n =   20     10%
    n =  100     29%
    n =  300     71%
    n = 1000    100%

At the sample sizes a news study actually has, the slope test misses real
contamination most of the time. It only becomes sharp when performance is close
to deterministic, which is the one thing trade outcomes are not. So the slope is
a test that can CONVICT but very rarely ACQUITS, and a "no gradient detected"
result at n=40 means almost nothing.

That is why ``powered`` and ``mde_slope`` are computed and why a clean verdict
from an underpowered test is reported as INCONCLUSIVE rather than clean. A test
that passes because it could not see anything is the most dangerous output this
module could produce: it is an audit that reads like a clearance.

The second detector, ``share_recalling_outcome``, is a self-report. It is the
one doing most of the work, and it is trivially defeated by a model that
understates its familiarity -- which is exactly what a model optimised to be
helpful will do when asked whether it is cheating. ``date_accuracy`` exists
because it is the one OBJECTIVE signal available: a model that can date a
date-redacted article to the right month is demonstrating recall, whatever it
says about itself. Redaction is not optional for that number to mean anything;
see ``redact_dates``.

The only clean design remains: evaluate exclusively on articles published after
the model's training cutoff, collected forward in time. This test exists for the
period before enough such data has accumulated, and it is weaker than that
design, not an alternative to it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy import stats as sps

LAP_PROMPT = """You are being audited for information leakage, not asked to trade.

For the article below answer ONLY with JSON:
{"can_date": true/false, "estimated_date": "YYYY-MM" or null,
 "recalls_outcome": true/false, "familiarity": 0.0-1.0,
 "basis": "one short sentence on what made it recognisable"}

`familiarity` is how strongly you recognise this specific article or event from \
training, where 0 means completely unfamiliar and 1 means you remember it clearly \
including what followed. Be honest; understating it defeats the purpose of the audit.

Explicit dates have been removed from the text. `estimated_date` is your best \
guess at when this was published from what you recognise, not from what the text \
states. Guess even when unsure -- a refusal to guess is scored the same as a \
wrong guess, so declining does not make the corpus look cleaner."""


# Above this share of unscorable articles the test refuses to return a verdict.
_MAX_LAP_ERROR_RATE = 0.20


@dataclass
class LAPScore:
    article_id: str
    familiarity: float
    can_date: bool
    recalls_outcome: bool
    estimated_date: str | None = None
    basis: str = ""
    error: str | None = None
    # The article's REAL publication month, supplied by the harness from the
    # corpus metadata -- never by the model. This is what makes date_accuracy an
    # objective measurement instead of another self-report.
    true_date: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class LAPReport:
    n: int
    mean_familiarity: float
    share_recalling_outcome: float
    lap_slope: float
    lap_slope_t: float
    lap_slope_p: float
    performance_low_lap: float
    performance_high_lap: float
    contaminated: bool
    post_cutoff_only: bool
    verdict: str
    notes: list[str] = field(default_factory=list)
    # Smallest slope this run could have detected at 80% power. When it is
    # larger than the threshold being tested, a null result is uninformative.
    mde_slope: float = float("inf")
    powered: bool = False
    # Objective leakage signal: share of DATE-REDACTED articles the model dated
    # to within a month. Unlike familiarity it cannot be understated away.
    date_accuracy: float | None = None
    date_accuracy_n: int = 0
    conclusive: bool = False
    error_rate: float = 0.0

    def to_dict(self) -> dict:
        return {
            "n": self.n, "mean_familiarity": round(self.mean_familiarity, 4),
            "share_recalling_outcome": round(self.share_recalling_outcome, 4),
            "lap_slope": round(self.lap_slope, 5),
            "lap_slope_t": round(self.lap_slope_t, 3),
            "lap_slope_p": round(self.lap_slope_p, 5),
            "performance_low_lap": round(self.performance_low_lap, 5),
            "performance_high_lap": round(self.performance_high_lap, 5),
            "contaminated": self.contaminated, "post_cutoff_only": self.post_cutoff_only,
            "verdict": self.verdict, "notes": self.notes,
            "mde_slope": (round(self.mde_slope, 5) if np.isfinite(self.mde_slope)
                          else None),
            "powered": self.powered, "conclusive": self.conclusive,
            "date_accuracy": (round(self.date_accuracy, 4)
                              if self.date_accuracy is not None else None),
            "date_accuracy_n": self.date_accuracy_n,
            "error_rate": round(self.error_rate, 4),
        }


def score_articles(articles: Sequence[dict[str, str]],
                   call_model: Callable[[str, str], str]) -> list[LAPScore]:
    import json

    out: list[LAPScore] = []
    for art in articles:
        try:
            # Redact BEFORE the model sees it: an unredacted dateline turns the
            # objective date check into a reading test that always reads as
            # maximum contamination.
            raw = call_model(LAP_PROMPT, json.dumps(
                {"headline": redact_dates(art.get("headline", "")),
                 "body": redact_dates(art.get("body", "")[:4000])},
                ensure_ascii=False))
            payload = json.loads(raw.strip().strip("`").removeprefix("json").strip())
            out.append(LAPScore(
                article_id=art.get("id", ""),
                familiarity=float(payload.get("familiarity", 0.0)),
                can_date=bool(payload.get("can_date", False)),
                recalls_outcome=bool(payload.get("recalls_outcome", False)),
                estimated_date=payload.get("estimated_date"),
                basis=str(payload.get("basis", ""))[:200],
                true_date=art.get("published_month") or art.get("true_date")))
        except Exception as exc:  # noqa: BLE001
            out.append(LAPScore(
                article_id=art.get("id", ""), familiarity=float("nan"),
                can_date=False, recalls_outcome=False, error=str(exc),
                true_date=art.get("published_month") or art.get("true_date")))
    return out


def redact_dates(text: str) -> str:
    """Strip explicit dates from an article before LAP scoring.

    Without this, ``date_accuracy`` measures reading comprehension. Nearly every
    wire story carries its own date, and a model that reads "March 12, 2023" off
    the dateline scores a perfect date accuracy while recalling nothing -- so the
    one objective leakage signal in this module reads as maximum contamination on
    a corpus that is completely clean, and gets ignored ever after.

    Deliberately blunt. Over-redaction costs a little context on a task that is
    about recognition, not comprehension; under-redaction destroys the
    measurement.
    """
    import re

    out = text
    months = (r"January|February|March|April|May|June|July|August|September|"
              r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
    patterns = (
        rf"\b(?:{months})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b",
        rf"\b\d{{1,2}}\s+(?:{months})\.?\s+\d{{4}}\b",
        rf"\b(?:{months})\.?\s+\d{{4}}\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b",
        r"\b(?:19|20)\d{2}\b",
        r"\bQ[1-4]\s*(?:19|20)?\d{2}\b",
        r"\b(?:FY|H[12])\s?(?:19|20)?\d{2}\b",
    )
    for pat in patterns:
        out = re.sub(pat, "[DATE]", out, flags=re.IGNORECASE)
    return out


def _month_distance(a: str, b: str) -> int | None:
    """Whole months between two ``YYYY-MM`` strings."""
    try:
        ay, am = int(a[:4]), int(a[5:7])
        by, bm = int(b[:4]), int(b[5:7])
    except (ValueError, IndexError, TypeError):
        return None
    return abs((ay * 12 + am) - (by * 12 + bm))


def date_accuracy(scores: Sequence[LAPScore], *, tolerance_months: int = 1
                  ) -> tuple[float | None, int]:
    """Share of articles the model dated to within ``tolerance_months``.

    Only articles carrying a ``true_date`` are counted, and a score with no
    ``estimated_date`` counts as a MISS rather than being dropped. Dropping the
    refusals would let a model clean its own record by declining on every
    article it recognised, which is the one evasion this measure exists to close.
    """
    graded = [s for s in scores if s.true_date and s.error is None]
    if not graded:
        return None, 0
    hits = 0
    for sc in graded:
        if not sc.estimated_date:
            continue
        d = _month_distance(sc.estimated_date, sc.true_date)
        if d is not None and d <= tolerance_months:
            hits += 1
    return hits / len(graded), len(graded)


def minimum_detectable_slope(fam: np.ndarray, perf: np.ndarray, *,
                             alpha: float = 0.05, power: float = 0.80) -> float:
    """The smallest slope this sample could reliably have found.

    Standard two-sample-free power arithmetic for a simple regression:
    ``se(beta) = sd_resid / (sd_x * sqrt(n - 1))`` and the detectable effect is
    ``(z_alpha + z_power) * se``. Reported because the alternative -- quoting a
    non-significant p-value as evidence of cleanliness -- is how an underpowered
    audit becomes a clearance certificate.
    """
    n = fam.size
    if n < 3:
        return float("inf")
    sd_x = float(np.std(fam, ddof=1))
    if sd_x <= 0:
        return float("inf")
    reg = sps.linregress(fam, perf)
    resid = perf - (reg.intercept + reg.slope * fam)
    sd_resid = float(np.std(resid, ddof=2)) if n > 2 else float("inf")
    se = sd_resid / (sd_x * np.sqrt(n - 1))
    z_alpha = float(sps.norm.ppf(1 - alpha))      # one-sided: we only care about +
    z_power = float(sps.norm.ppf(power))
    return float((z_alpha + z_power) * se)


def analyse(scores: Sequence[LAPScore], performance: Sequence[float], *,
            post_cutoff_only: bool = False, slope_threshold: float = 0.02,
            alpha: float = 0.05, require_power: bool = True,
            max_date_accuracy: float = 0.5) -> LAPReport:
    """Turn per-article LAP scores into a verdict.

    Fails closed in four distinct ways, and they are four different facts:

    * too few scorable articles;
    * too many failed model calls (an audit that could not run is not an audit
      that passed);
    * a detected familiarity gradient or a high self-reported recall share;
    * a *clean* result from a test with no power to detect the contamination it
      is looking for -- unless the corpus is post-cutoff only, in which case the
      design settles the question and the test's power is beside the point.

    The fourth is the one that is easy to leave out and the one that matters
    most in practice, because at realistic sample sizes it is the usual case.
    """
    fam = np.array([s.familiarity for s in scores], dtype=float)
    perf = np.array(performance, dtype=float)
    n = min(fam.size, perf.size)
    mask = np.isfinite(fam[:n]) & np.isfinite(perf[:n])
    fam, perf = fam[:n][mask], perf[:n][mask]
    notes: list[str] = []

    # Computed over SUCCESSFULLY scored articles only. A failed LLM call leaves
    # recalls_outcome=False, so averaging over the unmasked list let errors
    # dilute the measured recall share -- i.e. the contamination test failed
    # OPEN: the more calls failed, the cleaner the corpus appeared.
    scored = [sc for sc in scores if sc.error is None]
    error_rate = 1.0 - (len(scored) / len(scores)) if scores else 1.0
    share_recall = float(np.mean([sc.recalls_outcome for sc in scored])) if scored else 0.0
    acc, acc_n = date_accuracy(scores)

    if fam.size < 20:
        return LAPReport(
            int(fam.size), float(np.nanmean(fam)) if fam.size else 0.0,
            share_recall, 0.0, 0.0, 1.0, 0.0, 0.0, contaminated=True,
            post_cutoff_only=post_cutoff_only,
            verdict="too few scored articles to test for leakage; treat any news result "
                    "as exploratory",
            notes=["minimum 20 scored articles required"],
            date_accuracy=acc, date_accuracy_n=acc_n, error_rate=error_rate,
            conclusive=False)

    if np.std(fam) < 1e-9:
        slope = t = 0.0
        p = 1.0
        mde = float("inf")
        notes.append("no variation in familiarity: the slope test is uninformative. A "
                     "model that answers 0.0 for every article defeats this detector "
                     "entirely, which is why it is not the only one.")
    else:
        reg = sps.linregress(fam, perf)
        slope, t, p = float(reg.slope), float(reg.slope / reg.stderr if reg.stderr else 0.0), \
            float(reg.pvalue)
        mde = minimum_detectable_slope(fam, perf, alpha=alpha)

    median = float(np.median(fam))
    low = perf[fam <= median]
    high = perf[fam > median]
    perf_low = float(low.mean()) if low.size else 0.0
    perf_high = float(high.mean()) if high.size else 0.0
    powered = bool(np.isfinite(mde) and mde <= slope_threshold)

    slope_fired = recall_fired = date_fired = False
    inconclusive_reason = ""

    if error_rate > _MAX_LAP_ERROR_RATE:
        # Refuse the test rather than report a result derived from a fraction
        # of the corpus. An inconclusive audit is not a passing audit.
        inconclusive_reason = (
            f"{error_rate * 100:.0f}% of articles could not be scored (limit "
            f"{_MAX_LAP_ERROR_RATE * 100:.0f}%)")
        contaminated = True
        conclusive = False
    else:
        slope_fired = slope > slope_threshold and p < alpha
        recall_fired = share_recall > 0.3
        # The objective detector. A model that dates DATE-REDACTED articles to
        # the right month is recalling them, whatever it says about itself.
        date_fired = acc is not None and acc_n >= 20 and acc > max_date_accuracy
        contaminated = slope_fired or recall_fired or date_fired
        conclusive = contaminated or powered or post_cutoff_only
        if not conclusive and require_power:
            inconclusive_reason = (
                f"the smallest familiarity gradient this sample could have detected is "
                f"{mde:.3f} R per unit, against a threshold of {slope_threshold:.3f}")
            contaminated = True

    if inconclusive_reason:
        notes.append(
            f"{inconclusive_reason}. This test is INCONCLUSIVE and is treated as a "
            "failure: a contamination audit that could not see the effect it is "
            "looking for is not an audit that passed.")
        verdict = ("inconclusive: " + inconclusive_reason +
                   ". Not evidence of leakage, and not evidence against it.")
        if slope_fired or recall_fired or date_fired:
            verdict = "leakage detected despite an inconclusive run; treat as contaminated"
        notes.append("Acceptance requires a re-run on articles published strictly after "
                     "the model's training cutoff.")
    elif contaminated:
        reasons = []
        if slope_fired:
            reasons.append(
                f"performance rises {slope:.4f} per unit of familiarity (p={p:.4f}); "
                f"high-familiarity articles return {perf_high:.4f} against "
                f"{perf_low:.4f} for low-familiarity ones")
        if recall_fired:
            reasons.append(
                f"the model reports recalling the outcome for "
                f"{share_recall * 100:.0f}% of articles, which is direct evidence of "
                "memory rather than inference")
        if date_fired:
            reasons.append(
                f"the model dated {acc * 100:.0f}% of {acc_n} date-redacted articles to "
                "within a month, which is recall and cannot be explained by the text")
        verdict = ("leakage detected: " + "; ".join(reasons) +
                   ". The apparent skill is at least partly memory.")
        notes.append("Acceptance requires a re-run on articles published strictly after "
                     "the model's training cutoff.")
    elif post_cutoff_only:
        verdict = ("clean: every article postdates the model's training cutoff and no "
                   "familiarity gradient is detectable")
    else:
        verdict = (f"no leakage gradient detected, and the sample could have detected one "
                   f"of {mde:.3f} R per unit. The sample is NOT restricted to post-cutoff "
                   "articles, so this is evidence, not proof.")
        notes.append("Collect post-cutoff articles forward in time; that is the only "
                     "design that settles the question.")

    if acc is not None and acc_n < 20:
        notes.append(f"date accuracy measured on only {acc_n} articles; it is reported "
                     "but does not gate anything below 20")
    if acc is None:
        notes.append("no true publication dates were supplied, so the one OBJECTIVE "
                     "leakage detector did not run. The verdict rests on the model's own "
                     "account of its familiarity, which is the weakest evidence here.")

    return LAPReport(
        n=int(fam.size), mean_familiarity=float(fam.mean()),
        share_recalling_outcome=share_recall, lap_slope=slope, lap_slope_t=t,
        lap_slope_p=p, performance_low_lap=perf_low, performance_high_lap=perf_high,
        contaminated=contaminated, post_cutoff_only=post_cutoff_only,
        verdict=verdict, notes=notes, mde_slope=mde, powered=powered,
        date_accuracy=acc, date_accuracy_n=acc_n, error_rate=error_rate,
        conclusive=bool(conclusive) if not inconclusive_reason else False)
