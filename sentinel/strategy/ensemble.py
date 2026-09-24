"""Combining several strategies into one signal source.

What this is
------------

An ``Ensemble`` is a ``Strategy``. It produces ``Signal`` objects and nothing
else, exactly like a single strategy, which means every signal it emits goes
through ``RiskEngine.evaluate_entry`` on the same path with the same limits.
That is the entire safety story, and it is structural rather than a matter of
discipline: the ensemble has no access to the account, the positions or the
broker, so there is no limit for it to bypass even by mistake.

The one thing it must not do is net exposure itself. Two members signalling
long EUR/USD and long GBP/USD is one short-dollar bet at twice the size, and
``risk/exposure.py`` already decomposes exactly that -- by currency leg, which
is model-free, and by correlation cluster, which is an estimate. Reimplementing
that here would create a second place where a portfolio limit lives, and two
places where a limit lives is one place where it is wrong. What the ensemble
does instead is *report* cluster membership in the signal's features and damp
its own confidence when several correlated instruments fire together. The limit
stays where it was.

How members are combined
------------------------

**Votes, not averages.** Each member that fires contributes a signed vote. The
side is the sign of the weighted sum; if members disagree enough to cancel, no
signal is produced. Silence when the members disagree is the correct output --
an ensemble that always trades something has simply hidden the disagreement.

**Equal-risk weighting.** A member with a 3xATR stop and a member with a
1.5xATR stop are not making comparable statements, and equal-weighting their
strengths would give the wider-stopped member twice the influence for the same
nominal confidence. Each vote is therefore divided by its own stop distance in
ATR units, which is what "equal risk" means at the level of a signal. Explicit
per-member weights override this.

**Conservative levels.** The combined stop is the WIDEST of the agreeing
members' stops and the target is the NEAREST: the ensemble should not be
stopped out of a position one of its members would still be holding, and
should not hold for a target one of them has already abandoned. The cost is a
worse reward/risk ratio than any single member had, and that cost is not hidden
-- ``level_signal`` refuses the combination outright when the ratio no longer
clears the cost barrier, which is the honest outcome.

What it does not fix
--------------------

Combining strategies does not reduce the multiple-testing problem, it
*enlarges* it. Choosing which members to combine, and in what weights, is a
search over a much bigger space than choosing one strategy. The trial ledger
records the ensemble as its own trial with the member list in its parameters,
but it cannot know how many member combinations were tried and rejected before
this one -- and it cannot transfer the members' own family searches, which
still happened. Declare that count honestly: for an ensemble it is at least the
sum of its members' family counts, before adding anything for the combinations
you looked at.

For the same reason ``Ensemble`` is deliberately NOT registered in the strategy
registry. It has no meaningful default construction, and a registry entry that
could be built with no members would be an invitation to treat "the ensemble"
as a thing with a track record rather than as one specific combination that has
to earn a verdict of its own.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..core.types import Side, Signal
from .base import Strategy, StrategyMeta, atr
from .families._common import level_signal

WEIGHTING_SCHEMES = ("equal", "equal_risk")


class Ensemble(Strategy):
    """Weighted vote over member strategies, emitted as an ordinary Signal."""

    meta = StrategyMeta(
        name="ensemble", version="1.0.0", family="ensemble",
        timeframe="H4", horizon_bars=40, required_history=320,
        lifecycle="hypothesis",
        description="Weighted vote over several member strategies.",
        hypothesis="Members with genuinely different premises fail at different "
                   "times, so combining them should lower the variance of the "
                   "combined return more than it lowers its mean. This is the "
                   "only claim an ensemble can make: it cannot manufacture an "
                   "edge that none of its members has, and if every member is a "
                   "variation on one idea it does not even deliver the "
                   "diversification.",
        failure_conditions=[
            "Member signal timing is highly correlated (pairwise trade-date "
            "correlation above 0.7): there is one strategy here wearing several "
            "names, and the ensemble is a more expensive way to trade it.",
            "Combined Sharpe below the best single member out of sample -- the "
            "usual result when members are selected on the same history the "
            "ensemble is measured on.",
            "The conservative stop/target rule pushes reward/risk below the "
            "break-even threshold often enough that most votes are discarded.",
            "Performance depends on the member weights, which means the weights "
            "were fitted and the ensemble is one more overfitted parameter set.",
        ],
    )

    @staticmethod
    def default_params() -> Dict[str, Any]:
        return {
            "weighting": "equal_risk",
            # Net vote is normalised to roughly [-1, 1] (see `_vote`), so this
            # threshold reads as a fraction of full conviction. At 0.10 a
            # single member of typical strength in a three-member ensemble
            # clears it and a weak one does not; raise it toward 0.3 to require
            # two members to agree on the same bar. Note that members with
            # different entry triggers rarely fire on the SAME bar, so a high
            # threshold does not mean "a better ensemble", it means "almost no
            # trades" -- check the trade count before assuming the filter
            # helped.
            "min_net_vote": 0.10,
            "reference_risk_atr": 2.0,
            "min_agreement": 1,
            "correlation_window": 120,
            "correlation_threshold": 0.7,
            "min_reward_risk": 1.2,
        }

    def __init__(self, members: Sequence[Strategy], *,
                 weights: Optional[Dict[str, float]] = None, **params: Any) -> None:
        super().__init__(**params)
        members = list(members)
        if not members:
            raise ValueError("an ensemble needs at least one member")
        names = [m.meta.name for m in members]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate members in the ensemble: {names}")
        for m in members:
            # Baselines are the yardstick a candidate is measured against. Using
            # one as a component would put the benchmark inside the thing being
            # benchmarked and make every comparison meaningless.
            if m.meta.lifecycle == "reference":
                raise ValueError(
                    f"{m.meta.name!r} is a reference baseline and cannot be an "
                    "ensemble member: it exists to be compared against, not "
                    "traded with")
        if self.params["weighting"] not in WEIGHTING_SCHEMES:
            raise ValueError(f"weighting must be one of {WEIGHTING_SCHEMES}")
        if not 0 < float(self.params["min_net_vote"]) <= 1.0:
            raise ValueError("min_net_vote must lie inside (0, 1]: the net vote is "
                             "normalised, and a threshold above 1 can never be met")
        if float(self.params["reference_risk_atr"]) <= 0:
            raise ValueError("reference_risk_atr must be positive")
        if not 1 <= int(self.params["min_agreement"]) <= len(members):
            raise ValueError("min_agreement must be between 1 and the member count")

        self.members = members
        self.weights = {n: float(weights.get(n, 1.0)) if weights else 1.0 for n in names}
        if any(w < 0 for w in self.weights.values()):
            raise ValueError("member weights cannot be negative")
        total = sum(self.weights.values())
        if total <= 0:
            raise ValueError("member weights must not sum to zero")
        self.weights = {n: w / total for n, w in self.weights.items()}

        # The member list and weights ARE parameters: two ensembles over
        # different members are different trials, and the ledger keys on
        # `params`. Leaving them out would let every combination anyone tries
        # collapse into a single ledger entry.
        self.params["members"] = names
        self.params["weights"] = {n: round(w, 6) for n, w in self.weights.items()}

        # Per-instance meta so the name, timeframe and warmup describe THIS
        # combination. A class-level meta would report every ensemble as the
        # same strategy in the ledger and in every verdict.
        longest = max(m.meta.required_history for m in members)
        self.meta = replace(
            type(self).meta,
            name=f"ensemble[{'+'.join(sorted(names))}]",
            required_history=max(longest, type(self).meta.required_history),
            timeframe=members[0].meta.timeframe,
            horizon_bars=max(m.meta.horizon_bars for m in members),
            description=f"Weighted vote over {len(members)} members: {', '.join(names)}.",
        )
        self._corr: Dict[Tuple[str, str], pd.Series] = {}
        self._atr: Dict[str, pd.Series] = {}
        self._vote_cache: Tuple[int, Dict[str, Optional[Signal]]] = (-1, {})

    # ------------------------------------------------------------------ #

    def prepare(self, data: Dict[str, pd.DataFrame]) -> None:
        """Prepare every member once, and precompute pairwise correlations.

        Members precompute their own indicators here, which is what keeps the
        bar loop O(n) instead of O(n^2). The pairwise trailing correlations are
        computed once for the same reason -- a rolling correlation recomputed
        inside the loop for every instrument pair is the single easiest way to
        make an ensemble backtest take hours.
        """
        for m in self.members:
            m.prepare(data)
        # The ensemble's own ATR, used to express each member's stop in
        # comparable risk units. Precomputed here, once per series: slicing and
        # recomputing it at every bar was the original form of this code and
        # made the run quadratic in the number of bars.
        self._atr = {sym: atr(df, 14) for sym, df in data.items()}
        self._corr = {}
        window = int(self.params["correlation_window"])
        symbols = sorted(data)
        for i, a in enumerate(symbols):
            ra = data[a]["close"].astype(float).pct_change()
            for b in symbols[i + 1:]:
                rb = data[b]["close"].astype(float).pct_change().reindex(ra.index)
                self._corr[(a, b)] = ra.rolling(window, min_periods=window).corr(rb)
        self._vote_cache = (-1, {})

    def _member_signals(self, data, instrument: str, index: int) -> List[Signal]:
        out: List[Signal] = []
        for m in self.members:
            try:
                sig = m.generate(data, instrument, index)
            except Exception:  # noqa: BLE001 - one broken member must not silence
                continue        # the rest; the backtester reports generation errors
            if sig is not None and sig.side is not None and sig.stop_price is not None:
                out.append(sig)
        return out

    def _vote(self, sig: Signal, close: float, atr_value: float) -> float:
        """Signed, weighted contribution of one member signal.

        Under ``equal_risk`` the contribution is divided by the member's own
        stop distance in ATR units, so that conviction is expressed per unit of
        risk rather than per unit of nominal strength.
        """
        w = self.weights.get(sig.strategy, 0.0)
        sign = 1.0 if sig.side is Side.BUY else -1.0
        if self.params["weighting"] == "equal":
            return w * sign * float(sig.strength)
        risk = abs(close - float(sig.stop_price)) / atr_value if atr_value > 0 else 0.0
        if risk <= 0:
            return 0.0
        # Scale against a reference stop rather than dividing by the raw risk,
        # so that the net vote stays on a [-1, 1] scale whatever stop sizes the
        # members happen to use. Dividing outright made the threshold depend on
        # the members' stop widths, which is exactly the coupling equal-risk
        # weighting is supposed to remove. The clip stops a member with a
        # microscopic stop from dominating the vote on the strength of a stop
        # distance that is mostly noise.
        ratio = float(np.clip(float(self.params["reference_risk_atr"]) / risk, 0.25, 4.0))
        return w * sign * float(sig.strength) * ratio

    def _cluster_size(self, data, instrument: str, index: int,
                      firing: Sequence[str]) -> int:
        """How many of the instruments firing this bar sit in one correlated group.

        Uses ``risk.exposure.correlation_clusters`` -- the same single-linkage
        clustering the risk engine applies -- so the ensemble's own confidence
        and the engine's portfolio limit are at least reading the market the
        same way. The engine still enforces; this only damps.
        """
        from ..risk.exposure import correlation_clusters
        from ..core.money import D

        if len(firing) < 2:
            return 1
        corr: Dict[Tuple[str, str], float] = {}
        for (a, b), series in self._corr.items():
            if a not in firing or b not in firing or len(series) <= index:
                continue
            v = float(series.iloc[index])
            if np.isfinite(v):
                corr[(a, b)] = v
        clusters = correlation_clusters(
            list(firing), {s: D("1") for s in firing}, corr,
            float(self.params["correlation_threshold"]))
        for c in clusters:
            if instrument in c.instruments:
                return len(c.instruments)
        return 1

    def generate(self, data, instrument, index) -> Optional[Signal]:
        if index < self.warmup():
            return None

        # One-entry memo keyed on the bar index. The backtester calls generate
        # for every instrument at the same index in turn, and the cluster check
        # needs to know which OTHER instruments fired on this bar -- without the
        # memo that is a full re-evaluation of every member on every instrument,
        # for every instrument, i.e. quadratic in the universe size per bar.
        cached_index, cached = self._vote_cache
        if cached_index != index:
            cached = {}
            self._vote_cache = (index, cached)

        if instrument not in cached:
            cached[instrument] = self._combine(data, instrument, index)
        combined = cached[instrument]
        if combined is None:
            return None

        # Evaluate the remaining instruments only to learn who else fired.
        for sym in data:
            if sym not in cached:
                cached[sym] = self._combine(data, sym, index)
        firing = sorted(s for s, v in cached.items() if v is not None)
        cluster_size = self._cluster_size(data, instrument, index, firing)
        if cluster_size > 1:
            # Damp, do not veto. The decision to refuse a concentrated position
            # belongs to the risk engine, which sees the actual book; all this
            # says is that the ensemble is less confident in the marginal leg of
            # a correlated group than in a standalone one.
            damped = replace(combined,
                             strength=float(max(0.05, combined.strength / cluster_size)))
            damped.features = {**combined.features,
                               "correlation_cluster_size": float(cluster_size)}
            return damped
        return combined

    def _combine(self, data, instrument: str, index: int) -> Optional[Signal]:
        df = data.get(instrument)
        if df is None or len(df) <= index:
            return None
        signals = self._member_signals(data, instrument, index)
        if not signals:
            return None

        close = float(df["close"].iloc[index])
        a_series = self._atr.get(instrument)
        if a_series is None or len(a_series) <= index:
            return None  # not prepared: refuse rather than recompute per bar
        a = float(a_series.iloc[index])
        if not np.isfinite(a) or a <= 0:
            return None

        net = sum(self._vote(s, close, a) for s in signals)
        if abs(net) < float(self.params["min_net_vote"]):
            return None
        side = Side.BUY if net > 0 else Side.SELL
        agreeing = [s for s in signals if s.side is side]
        if len(agreeing) < int(self.params["min_agreement"]):
            return None

        sign = side.sign
        # Widest stop, nearest target -- see the module docstring. Both are the
        # minimum under the same ordering key: `sign * price` increases in the
        # direction the trade profits, so its minimum is the level furthest
        # behind for a stop and the level reached first for a target. Writing
        # the two selections with different keys is how this was wrong the
        # first time, and it was wrong in the dangerous direction: the FURTHEST
        # target combined with the widest stop, which inflates the reward/risk
        # ratio the cost check is supposed to police.
        order = lambda price: sign * price  # noqa: E731 - keeps the two calls identical
        stop = min((float(s.stop_price) for s in agreeing), key=order)
        targets = [float(s.target_price) for s in agreeing if s.target_price is not None]
        if not targets:
            return None
        target = min(targets, key=order)

        members = sorted(s.strategy for s in agreeing)
        return level_signal(
            strategy=self.meta.name, instrument=instrument, side=side, close=close,
            stop_price=stop, target_price=target,
            horizon_bars=self.meta.horizon_bars, timeframe=self.meta.timeframe,
            strength=float(min(1.0, abs(net))),
            min_reward_risk=float(self.params["min_reward_risk"]),
            features={"net_vote": net, "members_agreeing": float(len(agreeing)),
                      "members_firing": float(len(signals)), "atr": a},
            rationale=(f"{len(agreeing)}/{len(self.members)} members agree "
                       f"({', '.join(members)}); net vote {net:+.2f}"),
        )
