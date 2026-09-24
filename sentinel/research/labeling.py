"""Event labelling and sample weighting.

Three ideas from de Prado's *Advances in Financial Machine Learning*, all of
which the research brief flags as missing from the original design document:

**Triple-barrier labelling.** A label is the outcome of the trade you would
actually have taken: profit target, stop, or time limit, whichever is touched
first. Labelling by "return over the next N bars" instead is a different
question and produces a model that cannot be traded -- it ignores the path,
and the path is what stops you out.

**Average uniqueness.** Overlapping labels are not independent observations.
A thousand trades whose holding periods overlap can carry the information of
a hundred. Every sample count in this system is reported alongside its
*effective* count.

**Volatility-scaled barriers.** Fixed pip barriers mean different things in
different regimes; scaling by recent volatility keeps the label comparable
across time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd


@dataclass
class BarrierConfig:
    profit_mult: float = 2.0      # take-profit in units of sigma
    stop_mult: float = 1.0        # stop in units of sigma
    max_hold_bars: int = 24       # vertical barrier
    min_sigma: float = 1e-6

    def __post_init__(self) -> None:
        if self.profit_mult <= 0 or self.stop_mult <= 0:
            raise ValueError("barrier multiples must be positive")
        if self.max_hold_bars < 1:
            raise ValueError("max_hold_bars must be at least 1")


def realised_volatility(close: pd.Series, span: int = 50) -> pd.Series:
    """EWMA of log returns. Used to scale barriers to the current regime."""
    if len(close) < 2:
        return pd.Series(np.zeros(len(close)), index=close.index)
    logret = np.log(close.astype(float)).diff()
    return logret.ewm(span=span, min_periods=max(2, span // 4)).std().fillna(0.0)


def triple_barrier_labels(
    close: pd.Series,
    events: Sequence[int],
    sigma: pd.Series,
    config: BarrierConfig,
    side: Optional[pd.Series] = None,
    high: Optional[pd.Series] = None,
    low: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """Label each event by which barrier it touches first.

    ``side`` lets the same machinery serve meta-labelling: when a side is
    supplied the label becomes {0, 1} (was the primary model's call correct?)
    rather than {-1, 0, +1}.

    ``high``/``low`` make the touch test path-aware. Without them an intrabar
    stop that was hit before the target is invisible, which flatters every
    result. When both barriers are touched inside the same bar, the STOP is
    scored -- the pessimistic convention, and the one that matches a real
    order book where the adverse side fills first often enough to assume it.
    """
    close = close.astype(float)
    n = len(close)
    hi = high.astype(float).to_numpy() if high is not None else close.to_numpy()
    lo = low.astype(float).to_numpy() if low is not None else close.to_numpy()
    px = close.to_numpy()
    sig = sigma.astype(float).to_numpy()

    rows = []
    for t0 in events:
        if t0 < 0 or t0 >= n - 1:
            continue
        s = max(float(sig[t0]), config.min_sigma)
        direction = 1.0
        if side is not None:
            direction = float(side.iloc[t0])
            if direction == 0:
                continue
        entry = px[t0]
        # For a long the target is above and the stop below; for a short it is
        # the other way round. Both barriers must straddle the entry.
        #
        # The earlier expression put BOTH of a short's barriers below the entry,
        # so every short was stopped on its first bar -- and reported a positive
        # return alongside a "stop" label, a row contradicting itself. Any
        # meta-model trained through this path learned "shorts always lose".
        if direction > 0:
            up = entry * np.exp(config.profit_mult * s)     # target
            dn = entry * np.exp(-config.stop_mult * s)      # stop
        else:
            up = entry * np.exp(config.stop_mult * s)       # stop
            dn = entry * np.exp(-config.profit_mult * s)    # target
        t1 = min(t0 + config.max_hold_bars, n - 1)
        touch_idx, touched = t1, "time"
        for i in range(t0 + 1, t1 + 1):
            hit_up = hi[i] >= up
            hit_dn = lo[i] <= dn
            if hit_up and hit_dn:
                # Ambiguous bar: score the adverse side, whichever it is.
                touch_idx, touched = i, "stop"
                break
            if hit_up:
                touch_idx, touched = i, ("target" if direction > 0 else "stop")
                break
            if hit_dn:
                touch_idx, touched = i, ("stop" if direction > 0 else "target")
                break
        ret = (px[touch_idx] / entry - 1.0) * direction
        if side is None:
            label = 1 if touched == "target" else (-1 if touched == "stop" else int(np.sign(ret)))
        else:
            label = 1 if touched == "target" else 0
        rows.append({
            "t0": t0, "t1": touch_idx, "touched": touched, "ret": ret,
            "label": label, "sigma": s, "direction": direction,
            "target_price": up if direction > 0 else dn,
            "stop_price": dn if direction > 0 else up,
            "bars_held": touch_idx - t0,
        })
    return pd.DataFrame(rows).set_index("t0") if rows else pd.DataFrame(
        columns=["t1", "touched", "ret", "label", "sigma", "direction",
                 "target_price", "stop_price", "bars_held"])


def concurrency(events: pd.DataFrame, n_bars: int) -> np.ndarray:
    """Number of labels whose holding period spans each bar."""
    counts = np.zeros(n_bars, dtype=np.int64)
    for t0, t1 in zip(events.index.to_numpy(), events["t1"].to_numpy()):
        counts[int(t0):int(t1) + 1] += 1
    return counts


def average_uniqueness(events: pd.DataFrame, n_bars: int) -> pd.Series:
    """Per-label average uniqueness in [0, 1].

    1.0 means the label shares its holding period with nothing. 0.1 means ten
    labels were open at the same time and it carries a tenth of an independent
    observation.
    """
    if events.empty:
        return pd.Series(dtype=float)
    counts = concurrency(events, n_bars)
    out = {}
    for t0, t1 in zip(events.index.to_numpy(), events["t1"].to_numpy()):
        span = counts[int(t0):int(t1) + 1]
        span = np.where(span == 0, 1, span)
        out[int(t0)] = float(np.mean(1.0 / span))
    return pd.Series(out, name="uniqueness")


def effective_sample_size(events: pd.DataFrame, n_bars: int) -> float:
    """Sum of average uniqueness: the honest ``n`` for any significance test."""
    u = average_uniqueness(events, n_bars)
    return float(u.sum()) if len(u) else 0.0


def sample_weights(events: pd.DataFrame, n_bars: int, *,
                   by_return: bool = True, decay: float = 1.0) -> pd.Series:
    """Weights combining uniqueness, |return| and optional time decay.

    ``decay`` in (0, 1] down-weights old observations linearly, which is the
    standard concession to a market whose structure changes. ``decay=1``
    disables it; the choice must be declared before the run, not tuned after.
    """
    if events.empty:
        return pd.Series(dtype=float)
    u = average_uniqueness(events, n_bars)
    w = u.copy()
    if by_return:
        w = w * events.loc[u.index, "ret"].abs().to_numpy()
    if not (0.0 < decay <= 1.0):
        raise ValueError("decay must be in (0, 1]")
    if decay < 1.0:
        cum = u.cumsum() / u.sum()
        time_factor = decay + (1.0 - decay) * cum
        w = w * time_factor
    total = w.sum()
    return (w / total * len(w)) if total > 0 else u


def sequential_bootstrap(events: pd.DataFrame, n_bars: int, size: Optional[int] = None,
                         rng: Optional[np.random.Generator] = None) -> list[int]:
    """Draw samples with a probability inversely proportional to overlap.

    A plain bootstrap over overlapping labels resamples the same information
    repeatedly and understates the variance of every statistic built on it.
    """
    if events.empty:
        return []
    rng = rng or np.random.default_rng(0)
    idx = events.index.to_numpy()
    size = size or len(idx)
    spans = {int(t0): (int(t0), int(t1)) for t0, t1 in zip(idx, events["t1"].to_numpy())}
    drawn: list[int] = []
    counts = np.zeros(n_bars, dtype=np.float64)
    for _ in range(size):
        avg_u = np.empty(len(idx))
        for j, t0 in enumerate(idx):
            a, b = spans[int(t0)]
            c = counts[a:b + 1] + 1.0
            avg_u[j] = float(np.mean(1.0 / c))
        probs = avg_u / avg_u.sum()
        pick = int(rng.choice(idx, p=probs))
        drawn.append(pick)
        a, b = spans[pick]
        counts[a:b + 1] += 1.0
    return drawn
