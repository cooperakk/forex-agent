"""Cross-validation that respects time.

Plain k-fold on financial data leaks in two directions and both are fatal:

* **Label leakage.** A training label whose holding period overlaps a test
  observation contains information from the test period. *Purging* removes it.
* **Feature leakage (serial correlation).** Even with no overlap, a training
  sample immediately adjacent to the test set shares most of its features.
  *Embargo* drops a band after each test fold.

``PurgedKFold`` gives one path through history and answers "would this have
worked?". ``CombinatorialPurgedCV`` gives many paths and answers the more
useful question, "how often would this have worked?" -- a distribution of
Sharpe ratios rather than a single number that could be luck.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterator, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


@dataclass
class Split:
    train: np.ndarray
    test: np.ndarray
    test_groups: Tuple[int, ...] = ()
    purged: int = 0
    embargoed: int = 0


def _label_spans(t1: pd.Series) -> np.ndarray:
    """(start, end) index pairs for each label."""
    return np.column_stack([t1.index.to_numpy().astype(np.int64),
                            t1.to_numpy().astype(np.int64)])


def purge_train(train_idx: np.ndarray, test_idx: np.ndarray, t1: pd.Series) -> Tuple[np.ndarray, int]:
    """Drop training labels whose holding period overlaps the test window."""
    if len(test_idx) == 0 or len(train_idx) == 0:
        return train_idx, 0
    test_start, test_end = int(test_idx.min()), int(test_idx.max())
    spans = {int(i): int(v) for i, v in zip(t1.index.to_numpy(), t1.to_numpy())}
    keep = []
    for i in train_idx:
        start = int(i)
        end = spans.get(start, start)
        # overlap iff start <= test_end and end >= test_start
        if start <= test_end and end >= test_start:
            continue
        keep.append(i)
    kept = np.array(keep, dtype=np.int64)
    return kept, len(train_idx) - len(kept)


def apply_embargo(train_idx: np.ndarray, test_idx: np.ndarray, n_bars: int,
                  embargo_pct: float) -> Tuple[np.ndarray, int]:
    """Remove the band of training samples immediately after the test fold."""
    if embargo_pct <= 0 or len(test_idx) == 0:
        return train_idx, 0
    width = int(round(n_bars * embargo_pct))
    if width <= 0:
        return train_idx, 0
    test_end = int(test_idx.max())
    banned = set(range(test_end + 1, test_end + 1 + width))
    kept = np.array([i for i in train_idx if int(i) not in banned], dtype=np.int64)
    return kept, len(train_idx) - len(kept)


class PurgedKFold:
    """Contiguous, forward-ordered folds with purging and embargo."""

    def __init__(self, n_splits: int = 5, embargo_pct: float = 0.01) -> None:
        if n_splits < 2:
            raise ValueError("n_splits must be at least 2")
        if not (0.0 <= embargo_pct < 0.5):
            raise ValueError("embargo_pct must be in [0, 0.5)")
        self.n_splits = n_splits
        self.embargo_pct = embargo_pct

    def split(self, indices: Sequence[int], t1: pd.Series, n_bars: int) -> Iterator[Split]:
        idx = np.asarray(sorted(indices), dtype=np.int64)
        folds = np.array_split(idx, self.n_splits)
        for k, test in enumerate(folds):
            train = np.setdiff1d(idx, test, assume_unique=False)
            train, purged = purge_train(train, test, t1)
            train, embargoed = apply_embargo(train, test, n_bars, self.embargo_pct)
            yield Split(train=train, test=test, test_groups=(k,),
                        purged=purged, embargoed=embargoed)


class CombinatorialPurgedCV:
    """CPCV (de Prado ch. 12).

    History is cut into ``n_groups`` contiguous blocks; every combination of
    ``k`` blocks is used as a test set. With N=6, k=2 that is 15 splits which
    reassemble into 5 complete out-of-sample *paths* through history.

    The output is a distribution. "Sharpe 0.8" becomes "Sharpe 0.8 with an
    interquartile range of 0.3 to 1.2, positive on 73% of paths" -- and the
    second statement is the one an acceptance decision can rest on.
    """

    def __init__(self, n_groups: int = 6, test_groups: int = 2,
                 embargo_pct: float = 0.01) -> None:
        if test_groups >= n_groups:
            raise ValueError("test_groups must be smaller than n_groups")
        if n_groups < 3:
            raise ValueError("n_groups must be at least 3")
        self.n_groups = n_groups
        self.test_groups = test_groups
        self.embargo_pct = embargo_pct

    @property
    def n_splits(self) -> int:
        from math import comb
        return comb(self.n_groups, self.test_groups)

    @property
    def n_paths(self) -> int:
        """Number of complete out-of-sample paths reconstructable."""
        from math import comb
        return comb(self.n_groups - 1, self.test_groups - 1)

    def split(self, indices: Sequence[int], t1: pd.Series, n_bars: int) -> Iterator[Split]:
        idx = np.asarray(sorted(indices), dtype=np.int64)
        groups = np.array_split(idx, self.n_groups)
        for combo in combinations(range(self.n_groups), self.test_groups):
            test = np.concatenate([groups[g] for g in combo])
            train = np.setdiff1d(idx, test)
            purged_total = embargo_total = 0
            # Purge against each contiguous test block separately, otherwise a
            # non-adjacent pair of blocks would purge the entire middle.
            for g in combo:
                train, p = purge_train(train, groups[g], t1)
                train, e = apply_embargo(train, groups[g], n_bars, self.embargo_pct)
                purged_total += p
                embargo_total += e
            yield Split(train=train, test=np.sort(test), test_groups=combo,
                        purged=purged_total, embargoed=embargo_total)

    def assemble_paths(self, split_results: List[Tuple[Tuple[int, ...], dict]]) -> List[List[dict]]:
        """Group per-split, per-group results into complete history paths.

        Each path visits every group exactly once. Path ``p`` takes the
        ``p``-th available result for each group.
        """
        by_group: dict[int, list[dict]] = {g: [] for g in range(self.n_groups)}
        for combo, per_group in split_results:
            for g in combo:
                if g in per_group:
                    by_group[g].append(per_group[g])
        paths: List[List[dict]] = []
        for p in range(self.n_paths):
            path = []
            for g in range(self.n_groups):
                bucket = by_group[g]
                if len(bucket) > p:
                    path.append(bucket[p])
            if len(path) == self.n_groups:
                paths.append(path)
        return paths


def walk_forward(n_bars: int, train_bars: int, test_bars: int,
                 step: Optional[int] = None, anchored: bool = False,
                 *, gap_bars: int = 0, embargo_bars: int = 0) -> Iterator[Split]:
    """Rolling-origin evaluation (Hyndman & Athanasopoulos 5.10).

    The complement to CPCV, not a replacement: this is the only scheme that
    reproduces the actual deployment order, where the model is refitted as time
    passes and never sees a later bar.

    ``gap_bars`` purges the seam. With a zero gap -- which was the only
    behaviour available -- a label spanning N bars straddles the train/test
    boundary and leaks the test period's outcome straight into training, which
    is exactly the contamination PurgedKFold and CPCV go to such lengths to
    prevent. Set it to the maximum label horizon.

    ``embargo_bars`` additionally drops bars at the START of the test window,
    for the case where serial correlation makes the first few test bars
    partially predictable from the training tail.
    """
    if gap_bars < 0 or embargo_bars < 0:
        raise ValueError("gap_bars and embargo_bars must be non-negative")
    step = step or test_bars
    start = 0
    while start + train_bars + gap_bars + test_bars <= n_bars:
        train_start = 0 if anchored else start
        train = np.arange(train_start, start + train_bars, dtype=np.int64)
        test_start = start + train_bars + gap_bars + embargo_bars
        test_end = start + train_bars + gap_bars + test_bars
        if test_start >= test_end:
            break
        test = np.arange(test_start, test_end, dtype=np.int64)
        yield Split(train=train, test=test)
        start += step


# --------------------------------------------------------------------------- #
# CPCV over a matrix of variant returns: the selection procedure, out of sample
# --------------------------------------------------------------------------- #


@dataclass
class CPCVPathReport:
    """What the combinatorial run actually did, so the verdict can show it."""

    n_variants: int
    n_unique_variants: int
    n_splits: int
    n_paths: int
    purged: int
    embargoed: int
    chosen_per_split: List[int]
    paths: List[pd.Series]

    def to_dict(self) -> dict:
        return {"n_variants": self.n_variants, "n_unique_variants": self.n_unique_variants,
                "n_splits": self.n_splits, "n_paths": self.n_paths,
                "purged": self.purged, "embargoed": self.embargoed,
                "chosen_per_split": self.chosen_per_split}


def cpcv_paths_from_matrix(matrix: np.ndarray, *, n_groups: int = 6, test_groups: int = 2,
                           embargo_pct: float = 0.01, horizon_bars: int = 1,
                           periods_per_year: int = 252,
                           index: Optional[pd.Index] = None) -> CPCVPathReport:
    """Combinatorial purged CV of a SELECTION procedure over variant returns.

    ``matrix`` is T x K: one column of per-bar returns per configuration
    tried. For every combination of ``test_groups`` blocks the best variant is
    chosen on the purged, embargoed TRAIN rows by Sharpe -- that is the
    selection a researcher makes when they "pick the parameters that worked"
    -- and that variant's returns on the TEST blocks are what the path is made
    of. Paths are reassembled so each visits every block exactly once.

    This is what the acceptance script used to label CPCV while actually
    running one backtest per contiguous slice. A slice backtest measures
    whether the candidate was positive in each era, which is worth knowing;
    it does not measure whether choosing the candidate the way it was chosen
    survives out of sample, and it purges nothing, because there is no
    selection to purge. With one variant the two coincide; with many, only
    this one prices the search.

    ``horizon_bars`` is the holding period that makes a train bar's outcome
    overlap the test window -- the label end ``t1`` in de Prado's terms.
    """
    m = np.asarray(matrix, dtype=float)
    if m.ndim != 2 or m.shape[0] < n_groups * 4 or m.shape[1] < 1:
        raise ValueError("matrix must be T x K with at least 4 rows per group")
    t, k = m.shape
    cv = CombinatorialPurgedCV(n_groups=n_groups, test_groups=test_groups,
                               embargo_pct=embargo_pct)
    idx = np.arange(t, dtype=np.int64)
    t1 = pd.Series(np.minimum(idx + max(0, int(horizon_bars)), t - 1), index=idx)
    unique = len({tuple(np.round(m[:, j], 12)) for j in range(k)})

    def sharpe(rows: np.ndarray, col: int) -> float:
        x = m[rows, col]
        x = x[np.isfinite(x)]
        if x.size < 2:
            return float("-inf")
        sd = float(x.std(ddof=1))
        if sd <= 0:
            return float("-inf")
        return float(x.mean() / sd * np.sqrt(periods_per_year))

    groups = np.array_split(idx, n_groups)
    split_results: List[Tuple[Tuple[int, ...], dict]] = []
    chosen: List[int] = []
    purged = embargoed = 0
    for split in cv.split(idx, t1, t):
        purged += split.purged
        embargoed += split.embargoed
        scores = [sharpe(split.train, j) for j in range(k)]
        best = int(np.argmax(scores)) if scores else 0
        chosen.append(best)
        per_group = {g: {"rows": groups[g], "col": best} for g in split.test_groups}
        split_results.append((split.test_groups, per_group))

    paths: List[pd.Series] = []
    for path in cv.assemble_paths(split_results):
        rows = np.concatenate([p["rows"] for p in path])
        vals = np.concatenate([m[p["rows"], p["col"]] for p in path])
        ser = pd.Series(vals, index=(index[rows] if index is not None else rows))
        paths.append(ser.dropna())
    return CPCVPathReport(n_variants=k, n_unique_variants=unique, n_splits=cv.n_splits,
                          n_paths=len(paths), purged=purged, embargoed=embargoed,
                          chosen_per_split=chosen, paths=paths)
