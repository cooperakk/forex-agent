"""Statistical machinery for deciding whether a result is real.

The premise, from section B-1 of the research brief: with an honest prior of
2-5% that any given trading hypothesis is true, a p = 0.05 "discovery" is more
likely false than true. So the question is never "did it beat zero?" but "did
it beat zero *given how many things I tried*, on *how many effectively
independent observations*, against *the right null*?"

Implemented here:

* ``sharpe_ratio`` / ``probabilistic_sharpe_ratio`` -- Sharpe with the
  skew/kurtosis correction, because FX returns are neither normal nor iid.
* ``deflated_sharpe_ratio`` -- PSR against the expected maximum of N trials.
  Reporting the best of 200 configurations without this is not a result.
* ``min_track_record_length`` -- how much history is needed before a Sharpe
  can be called positive at all.
* ``probability_of_backtest_overfitting`` -- CSCV. Does the in-sample winner
  stay above median out of sample?
* ``clark_west`` -- the correct nested-model test against a random walk.
* ``hansen_spa`` / ``whites_reality_check`` -- multiple-testing corrections
  over a family of strategy variants.

References are in ``docs/ACCEPTANCE-PROTOCOL.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from math import comb, e, exp, log, sqrt
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import stats as sps

EULER_MASCHERONI = 0.5772156649015329


# --------------------------------------------------------------------------- #
# Sharpe family
# --------------------------------------------------------------------------- #


def _degenerate(x: np.ndarray, sd: float) -> bool:
    """True when the dispersion is floating-point noise rather than variation.

    ``np.std`` of a constant series is not exactly 0 -- for [0.001]*100 it is
    about 2e-19 -- so a bare ``sd <= 0`` guard lets a flat equity curve report a
    Sharpe of 7e16. That is not a hypothetical: the no-trade baseline and a
    halted agent both produce exactly this input. The test is relative, because
    the absolute scale of a return series is arbitrary.
    """
    if not np.isfinite(sd) or sd <= 0:
        return True
    scale = max(float(np.max(np.abs(x))), 1e-300)
    return sd < scale * 1e-12


def sharpe_ratio(returns: Sequence[float], periods_per_year: int = 252,
                 risk_free: float = 0.0) -> float:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return 0.0
    excess = r - risk_free / periods_per_year
    sd = float(excess.std(ddof=1))
    if _degenerate(excess, sd):
        return 0.0
    return float(excess.mean() / sd * sqrt(periods_per_year))


def _moments(returns: np.ndarray) -> Tuple[float, float, float]:
    """(non-annualised SR, skew, non-excess kurtosis).

    On a degenerate series scipy's moment calculation loses all precision to
    catastrophic cancellation and returns NaN. NaN then propagates through every
    downstream comparison as False, which happens to fail closed but is invisible
    and turns a dashboard into a wall of "NaN". Degenerate input gets the normal
    moments instead, and the zero Sharpe carries the real message.
    """
    sd = float(returns.std(ddof=1))
    if _degenerate(returns, sd):
        return 0.0, 0.0, 3.0
    with np.errstate(all="ignore"):
        skew = float(sps.skew(returns, bias=False)) if returns.size > 2 else 0.0
        kurt = (float(sps.kurtosis(returns, fisher=False, bias=False))
                if returns.size > 3 else 3.0)
    if not np.isfinite(skew):
        skew = 0.0
    if not np.isfinite(kurt):
        kurt = 3.0
    return 0.0 if not np.isfinite(sd) else float(returns.mean() / sd), skew, kurt


def probabilistic_sharpe_ratio(returns: Sequence[float], benchmark_sr: float = 0.0,
                               periods_per_year: int = 252) -> float:
    """P(true Sharpe > benchmark), adjusted for skew and fat tails.

    Negative skew and excess kurtosis both *inflate* the naive Sharpe of a
    strategy that sells tail risk; this correction is what stops a carry-like
    payoff from looking safe right up to the day it is not.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = r.size
    if n < 4:
        return 0.0
    sr, skew, kurt = _moments(r)
    bench = benchmark_sr / sqrt(periods_per_year)
    denom_sq = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2
    if denom_sq <= 0:
        return 0.0
    z = (sr - bench) * sqrt(n - 1) / sqrt(denom_sq)
    value = float(sps.norm.cdf(z))
    return value if np.isfinite(value) else 0.0


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """E[max SR] over ``n_trials`` independent trials of zero true skill.

    This is the bar a "best configuration" has to clear. With 200 trials and a
    trial-to-trial SR standard deviation of 0.5, pure noise is expected to
    produce a best Sharpe near 1.5 -- which is why an uncorrected 1.4 is not
    evidence of anything.
    """
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    sd = sqrt(sr_variance)
    a = sps.norm.ppf(1.0 - 1.0 / n_trials)
    b = sps.norm.ppf(1.0 - 1.0 / (n_trials * e))
    return float(sd * ((1.0 - EULER_MASCHERONI) * a + EULER_MASCHERONI * b))


def deflated_sharpe_ratio(returns: Sequence[float], n_trials: int,
                          trial_sharpes: Optional[Sequence[float]] = None,
                          periods_per_year: int = 252) -> Dict[str, float]:
    """PSR against the expected maximum Sharpe of ``n_trials``."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 4:
        return {"dsr": 0.0, "sr": 0.0, "sr_star": 0.0, "n_trials": float(n_trials),
                "psr_vs_zero": 0.0}
    if trial_sharpes is not None and len(trial_sharpes) > 1:
        # The trial Sharpes are already annualised, so their variance is in
        # annualised units and E[max] comes out annualised too.
        var = float(np.var(np.asarray(trial_sharpes, dtype=float), ddof=1))
        sr_star_ann = expected_max_sharpe(n_trials, var)
    else:
        # Fall back to the asymptotic variance of a single SR estimate,
        # Var(SR) = (1 + SR^2/2) / n, which holds ONLY when SR and the variance
        # are expressed in the same time units.
        #
        # The earlier form built the variance from the ANNUALISED Sharpe and
        # then divided the whole expression by periods_per_year, which turned
        # the leading 1 into 1/ppy and shrank the variance by ~200x. Since
        # acceptance.py always calls this with trial_sharpes=None, that was the
        # only branch that ever ran: the bar a "best of N" configuration had to
        # clear collapsed to almost nothing, and L5.1 -- the one gate whose job
        # is to price in multiple testing -- certified noise. Verified against
        # Monte-Carlo E[max SR].
        sr_na = sharpe_ratio(r, periods_per_year) / sqrt(periods_per_year)
        var_na = (1.0 + 0.5 * sr_na ** 2) / max(1, r.size)
        sr_star_ann = expected_max_sharpe(n_trials, var_na) * sqrt(periods_per_year)
    return {
        "dsr": probabilistic_sharpe_ratio(r, sr_star_ann, periods_per_year),
        "psr_vs_zero": probabilistic_sharpe_ratio(r, 0.0, periods_per_year),
        "sr": sharpe_ratio(r, periods_per_year),
        "sr_star": float(sr_star_ann),
        "n_trials": float(n_trials),
    }


def min_track_record_length(returns: Sequence[float], benchmark_sr: float = 0.0,
                            confidence: float = 0.95,
                            periods_per_year: int = 252) -> Optional[float]:
    """Observations needed before the Sharpe can be called above ``benchmark``.

    ``None`` means the observed Sharpe does not exceed the benchmark at all, so
    no amount of additional history would settle it in this strategy's favour.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 4:
        return None
    sr, skew, kurt = _moments(r)
    bench = benchmark_sr / sqrt(periods_per_year)
    if sr <= bench:
        return None
    z = sps.norm.ppf(confidence)
    numerator = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2
    return float(1.0 + numerator * (z / (sr - bench)) ** 2)


# --------------------------------------------------------------------------- #
# Backtest overfitting (CSCV)
# --------------------------------------------------------------------------- #


@dataclass
class PBOResult:
    pbo: float
    n_configs: int
    n_splits: int
    logits: List[float] = field(default_factory=list)
    oos_ranks: List[float] = field(default_factory=list)
    performance_degradation_slope: float = 0.0
    prob_oos_loss: float = 0.0
    is_sr_of_winner: List[float] = field(default_factory=list)
    oos_sr_of_winner: List[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pbo": round(self.pbo, 4), "n_configs": self.n_configs,
            "n_splits": self.n_splits,
            "performance_degradation_slope": round(self.performance_degradation_slope, 4),
            "prob_oos_loss": round(self.prob_oos_loss, 4),
            "median_logit": round(float(np.median(self.logits)), 4) if self.logits else 0.0,
        }


def probability_of_backtest_overfitting(
    perf_matrix: np.ndarray, n_subsets: int = 10, periods_per_year: int = 252,
) -> PBOResult:
    """CSCV (Bailey, Borwein, Lopez de Prado, Zhu).

    ``perf_matrix`` is T x N: rows are time, columns are strategy
    configurations. History is cut into ``n_subsets`` blocks; for every way of
    choosing half the blocks as in-sample, the in-sample winner's rank in the
    out-of-sample half is recorded. If the winner lands below the OOS median as
    often as not, the selection procedure has been fitting noise.
    """
    M = np.asarray(perf_matrix, dtype=float)
    if M.ndim != 2 or M.shape[1] < 2:
        raise ValueError("perf_matrix must be T x N with N >= 2")
    if n_subsets % 2 != 0:
        raise ValueError("n_subsets must be even")
    T, N = M.shape
    if T < n_subsets * 2:
        n_subsets = max(2, (T // 2) - (T // 2) % 2)
    blocks = np.array_split(np.arange(T), n_subsets)
    half = n_subsets // 2

    logits: List[float] = []
    ranks: List[float] = []
    is_sr: List[float] = []
    oos_sr: List[float] = []

    for combo in combinations(range(n_subsets), half):
        is_idx = np.concatenate([blocks[b] for b in combo])
        oos_idx = np.concatenate([blocks[b] for b in range(n_subsets) if b not in combo])
        is_perf = np.array([sharpe_ratio(M[is_idx, k], periods_per_year) for k in range(N)])
        oos_perf = np.array([sharpe_ratio(M[oos_idx, k], periods_per_year) for k in range(N)])
        best = int(np.argmax(is_perf))
        # Rank of the IS winner among OOS performances (1 = worst).
        order = sps.rankdata(oos_perf, method="average")
        omega = float(order[best]) / (N + 1)
        omega = min(max(omega, 1e-9), 1 - 1e-9)
        logits.append(log(omega / (1 - omega)))
        ranks.append(omega)
        is_sr.append(float(is_perf[best]))
        oos_sr.append(float(oos_perf[best]))

    pbo = float(np.mean([l <= 0 for l in logits])) if logits else 0.0
    slope = 0.0
    if len(is_sr) > 2 and np.std(is_sr) > 0:
        slope = float(np.polyfit(is_sr, oos_sr, 1)[0])
    prob_loss = float(np.mean([s <= 0 for s in oos_sr])) if oos_sr else 0.0
    return PBOResult(pbo=pbo, n_configs=N, n_splits=len(logits), logits=logits,
                     oos_ranks=ranks, performance_degradation_slope=slope,
                     prob_oos_loss=prob_loss, is_sr_of_winner=is_sr, oos_sr_of_winner=oos_sr)


# --------------------------------------------------------------------------- #
# Forecast comparison
# --------------------------------------------------------------------------- #


def newey_west_se(x: np.ndarray, lags: Optional[int] = None) -> float:
    """HAC standard error of a mean. Overlapping forecasts are autocorrelated."""
    n = x.size
    if n < 2:
        return float("inf")
    if lags is None:
        lags = int(np.floor(4 * (n / 100.0) ** (2.0 / 9.0)))
    lags = max(0, min(lags, n - 1))
    xc = x - x.mean()
    gamma0 = float(np.dot(xc, xc) / n)
    var = gamma0
    for l in range(1, lags + 1):
        w = 1.0 - l / (lags + 1.0)
        gl = float(np.dot(xc[l:], xc[:-l]) / n)
        var += 2.0 * w * gl
    if var <= 0:
        return float("inf")
    return sqrt(var / n)


@dataclass
class TestResult:
    statistic: float
    p_value: float
    name: str
    detail: Dict[str, float] = field(default_factory=dict)

    def passed(self, alpha: float) -> bool:
        return self.p_value < alpha

    def to_dict(self) -> dict:
        return {"test": self.name, "statistic": round(self.statistic, 4),
                "p_value": round(self.p_value, 6), **{k: round(v, 6) for k, v in self.detail.items()}}


def clark_west(actual: Sequence[float], pred_small: Sequence[float],
               pred_large: Sequence[float], lags: Optional[int] = None) -> TestResult:
    """Clark-West test for nested forecast models.

    The null is the *parsimonious* model -- here the random walk, ``pred_small
    = 0`` for returns. Diebold-Mariano is the wrong test for nested models: it
    is undersized, because the larger model's extra parameters add estimation
    noise under the null and mechanically inflate its MSE. Clark-West adds the
    adjustment term that corrects for exactly that.

    A model that cannot pass this has not beaten "tomorrow's price is today's
    price", which is the benchmark that has defeated exchange-rate forecasting
    since Meese and Rogoff (1983).
    """
    y = np.asarray(actual, dtype=float)
    f1 = np.asarray(pred_small, dtype=float)
    f2 = np.asarray(pred_large, dtype=float)
    n = min(y.size, f1.size, f2.size)
    if n < 10:
        return TestResult(0.0, 1.0, "clark_west", {"n": float(n)})
    y, f1, f2 = y[-n:], f1[-n:], f2[-n:]
    e1 = (y - f1) ** 2
    e2 = (y - f2) ** 2
    adj = (f1 - f2) ** 2
    f_t = e1 - (e2 - adj)
    mean_f = float(f_t.mean())
    se = newey_west_se(f_t, lags)
    if not np.isfinite(se) or se <= 0:
        return TestResult(0.0, 1.0, "clark_west", {"n": float(n)})
    stat = mean_f / se
    # One-sided: the large model is better only if the statistic is positive.
    p = float(1.0 - sps.norm.cdf(stat))
    return TestResult(stat, p, "clark_west",
                      {"n": float(n), "mse_small": float(e1.mean()),
                       "mse_large": float(e2.mean()), "mean_adjusted_diff": mean_f})


def diebold_mariano(actual: Sequence[float], pred_a: Sequence[float],
                    pred_b: Sequence[float], lags: Optional[int] = None) -> TestResult:
    """Two-sided DM test. Valid only for NON-nested models."""
    y = np.asarray(actual, dtype=float)
    a = np.asarray(pred_a, dtype=float)
    b = np.asarray(pred_b, dtype=float)
    n = min(y.size, a.size, b.size)
    if n < 10:
        return TestResult(0.0, 1.0, "diebold_mariano", {"n": float(n)})
    d = (y[-n:] - a[-n:]) ** 2 - (y[-n:] - b[-n:]) ** 2
    se = newey_west_se(d, lags)
    if not np.isfinite(se) or se <= 0:
        return TestResult(0.0, 1.0, "diebold_mariano", {"n": float(n)})
    stat = float(d.mean() / se)
    return TestResult(stat, float(2 * (1 - sps.norm.cdf(abs(stat)))), "diebold_mariano",
                      {"n": float(n)})


# --------------------------------------------------------------------------- #
# Multiple testing over a family of strategies
# --------------------------------------------------------------------------- #


def _stationary_bootstrap_indices(n: int, block_mean: float,
                                  rng: np.random.Generator) -> np.ndarray:
    """Politis-Romano stationary bootstrap. Preserves serial dependence.

    An iid bootstrap over trade-level returns destroys loss clustering, which
    is precisely the feature that decides whether a drawdown is survivable.
    """
    p = 1.0 / max(1.0, block_mean)
    idx = np.empty(n, dtype=np.int64)
    idx[0] = rng.integers(0, n)
    for t in range(1, n):
        if rng.random() < p:
            idx[t] = rng.integers(0, n)
        else:
            idx[t] = (idx[t - 1] + 1) % n
    return idx


def hansen_spa(loss_differentials: np.ndarray, n_boot: int = 1000,
               block_mean: float = 10.0, seed: int = 0) -> TestResult:
    """Hansen's Superior Predictive Ability test.

    ``loss_differentials`` is T x K: column k is the per-period performance of
    variant k *minus* the benchmark. The null is that no variant beats the
    benchmark. Studentisation and the recentring rule are what make SPA less
    conservative than White's Reality Check when the family contains many poor
    variants -- which it always does, because the family is every parameter
    combination that was tried.
    """
    d = np.asarray(loss_differentials, dtype=float)
    if d.ndim == 1:
        d = d[:, None]
    T, K = d.shape
    if T < 20:
        return TestResult(0.0, 1.0, "hansen_spa", {"T": float(T), "K": float(K)})
    rng = np.random.default_rng(seed)
    mean_d = d.mean(axis=0)
    omega = np.array([max(newey_west_se(d[:, k]) * sqrt(T), 1e-12) for k in range(K)])
    stat = float(max(0.0, np.max(sqrt(T) * mean_d / omega)))

    # Recentring: variants that are too far below zero are excluded from the
    # null, which is Hansen's key improvement over the Reality Check.
    threshold = -omega * sqrt(2.0 * log(log(max(T, 3)))) / sqrt(T)
    centre = np.where(mean_d >= threshold, mean_d, 0.0)

    boot_stats = np.empty(n_boot)
    for b in range(n_boot):
        idx = _stationary_bootstrap_indices(T, block_mean, rng)
        db = d[idx, :]
        mb = db.mean(axis=0) - centre
        boot_stats[b] = max(0.0, np.max(sqrt(T) * mb / omega))
    p = float(np.mean(boot_stats >= stat))
    return TestResult(stat, p, "hansen_spa",
                      {"T": float(T), "K": float(K),
                       "best_variant": float(int(np.argmax(mean_d))),
                       "best_mean": float(np.max(mean_d))})


def whites_reality_check(loss_differentials: np.ndarray, n_boot: int = 1000,
                         block_mean: float = 10.0, seed: int = 0) -> TestResult:
    """White's Reality Check. More conservative than SPA; reported alongside it."""
    d = np.asarray(loss_differentials, dtype=float)
    if d.ndim == 1:
        d = d[:, None]
    T, K = d.shape
    if T < 20:
        return TestResult(0.0, 1.0, "whites_reality_check", {"T": float(T), "K": float(K)})
    rng = np.random.default_rng(seed)
    mean_d = d.mean(axis=0)
    stat = float(max(0.0, np.max(sqrt(T) * mean_d)))
    boot = np.empty(n_boot)
    for b in range(n_boot):
        idx = _stationary_bootstrap_indices(T, block_mean, rng)
        mb = d[idx, :].mean(axis=0) - mean_d
        boot[b] = max(0.0, np.max(sqrt(T) * mb))
    return TestResult(stat, float(np.mean(boot >= stat)), "whites_reality_check",
                      {"T": float(T), "K": float(K)})


def benjamini_hochberg(p_values: Sequence[float], alpha: float = 0.05) -> List[bool]:
    """FDR control. Useful when screening many hypotheses at once."""
    p = np.asarray(p_values, dtype=float)
    n = p.size
    if n == 0:
        return []
    order = np.argsort(p)
    thresholds = alpha * (np.arange(1, n + 1) / n)
    passed_sorted = p[order] <= thresholds
    cutoff = np.where(passed_sorted)[0]
    k = cutoff.max() + 1 if cutoff.size else 0
    out = np.zeros(n, dtype=bool)
    if k:
        out[order[:k]] = True
    return out.tolist()


# --------------------------------------------------------------------------- #
# Decision arithmetic (brief section B-1)
# --------------------------------------------------------------------------- #


def posterior_given_significant(prior: float, alpha: float, power: float) -> float:
    """P(hypothesis true | test was significant).

    This single line is the most important number in the whole acceptance
    protocol. With prior = 0.03, alpha = 0.05 and power = 0.5, a "significant"
    result is true about 24% of the time. With alpha = 0.001 it is about 94%.
    """
    if not (0 < prior < 1) or not (0 < alpha < 1) or not (0 < power <= 1):
        raise ValueError("prior, alpha in (0,1); power in (0,1]")
    tp = prior * power
    fp = (1 - prior) * alpha
    return float(tp / (tp + fp)) if (tp + fp) > 0 else 0.0


def required_alpha_for_posterior(prior: float, power: float, target_posterior: float) -> float:
    """Alpha needed so a significant result carries ``target_posterior`` belief."""
    if not (0 < target_posterior < 1):
        raise ValueError("target_posterior must be in (0, 1)")
    tp = prior * power
    return float(tp * (1 - target_posterior) / (target_posterior * (1 - prior)))


def directional_accuracy(signed_forward_returns: Sequence[float],
                         lags: Optional[int] = None) -> TestResult:
    """Does the strategy's directional call carry information about what follows?

    Input: one number per signal RAISED -- the side the rule called, times the
    return that actually followed over the rule's own horizon. Positive means
    the call was right. Two statistics on that sample:

    * the mean signed return, against zero, with a Newey-West standard error
      because consecutive signals overlap in time (one-sided);
    * the hit rate against 1/2, as a plain binomial, for the report.

    The gate uses the first. A rule whose calls are right 55% of the time on
    tiny moves and wrong 45% of the time on large ones has a positive hit rate
    and a negative mean, and it is the mean that pays.

    This replaces a misuse of the Clark-West statistic: that test compares two
    NESTED forecasts of the same series, and the protocol fed it the
    strategy's own equity returns as "actual" and half of the previous equity
    return as the "model forecast". Nothing in that arrangement is a forecast
    the strategy made about the exchange rate, so passing it said nothing
    about beating a random walk. Clark-West stays available for the case it is
    for -- a model that emits a numeric forecast -- and ``news/lap.py`` uses
    it that way.
    """
    x = np.asarray(signed_forward_returns, dtype=float)
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n < 10:
        return TestResult(0.0, 1.0, "directional_accuracy",
                          {"n": float(n), "hit_rate": float("nan")})
    mean = float(x.mean())
    se = newey_west_se(x, lags)
    if not np.isfinite(se) or se <= 0:
        return TestResult(0.0, 1.0, "directional_accuracy", {"n": float(n)})
    stat = mean / se
    p_mean = float(1.0 - sps.norm.cdf(stat))
    hits = int((x > 0).sum())
    p_hit = float(sps.binomtest(hits, n, 0.5, alternative="greater").pvalue)
    return TestResult(stat, p_mean, "directional_accuracy", {
        "n": float(n), "mean_signed_return": mean, "se": float(se),
        "hit_rate": hits / n, "p_hit_rate": p_hit,
    })
