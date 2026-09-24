"""Portfolio exposure decomposition.

The trap this module exists to catch: long EUR/USD, long GBP/USD, long
AUD/USD and short USD/JPY look like four independent trades and are in fact
one short-dollar bet at four times the intended size. Netting by *currency
leg* makes that visible; netting by instrument does not.

Two complementary views are produced:

``currency_risk``
    Signed risk (in account currency) per currency, built from the base/quote
    decomposition of each position. Model-free -- no estimation, no lookback,
    nothing to be wrong about.

``correlation_clusters``
    Groups of instruments whose realised return correlation exceeds a
    threshold, with the summed risk of each cluster. This one *is* an estimate
    and is labelled as such: correlations move, and they move most at exactly
    the moment the cluster matters (August 2024, brief section B-8).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.money import D, Instrument, ZERO, dec
from ..core.types import Position, Side


@dataclass
class CurrencyExposure:
    currency: str
    net_risk: Decimal          # signed: + long that currency
    gross_risk: Decimal
    net_notional: Decimal
    contributors: List[str] = field(default_factory=list)


@dataclass
class Cluster:
    instruments: List[str]
    total_risk: Decimal
    max_pairwise_corr: float


def currency_exposure(
    positions: Sequence[Position],
    instruments: Dict[str, Instrument],
    *,
    risk_by_instrument: Optional[Dict[str, Decimal]] = None,
    conversions: Optional[Dict[str, Decimal]] = None,
    mid_prices: Optional[Dict[str, Decimal]] = None,
    account_currency: Optional[str] = None,
    unconvertible: Optional[List[str]] = None,
) -> Dict[str, CurrencyExposure]:
    """Decompose open positions into per-currency signed risk and notional.

    Long EUR/USD = long EUR, short USD. The risk attributed to each leg is the
    position's own risk-to-stop, which is the quantity the limits are written
    in; notional is tracked alongside for the leverage check.
    """
    risk_by_instrument = risk_by_instrument or {}
    conversions = conversions or {}
    mid_prices = mid_prices or {}

    net_risk: Dict[str, Decimal] = defaultdict(lambda: ZERO)
    gross_risk: Dict[str, Decimal] = defaultdict(lambda: ZERO)
    net_notional: Dict[str, Decimal] = defaultdict(lambda: ZERO)
    contributors: Dict[str, List[str]] = defaultdict(list)

    for pos in positions:
        inst = instruments.get(pos.instrument)
        if inst is None:
            continue
        risk = dec(risk_by_instrument.get(pos.instrument, pos.initial_risk or ZERO))
        sign = D(pos.side.sign)
        # A missing rate is recorded and the position skipped, never silently
        # converted at 1.0 -- an unconvertible leg understates the exposure, and
        # the caller has to know that rather than read a confidently wrong number.
        if inst.quote == account_currency:
            conv = D("1")
        elif inst.quote in conversions:
            conv = dec(conversions[inst.quote])
        else:
            if unconvertible is not None:
                unconvertible.append(pos.instrument)
            continue
        mid = dec(mid_prices.get(pos.instrument, pos.entry_price))
        notional_quote = inst.units(pos.lots) * mid * conv
        notional_base = inst.units(pos.lots) * mid * conv  # same account-ccy magnitude

        # base leg follows the position direction, quote leg is its mirror
        net_risk[inst.base] += risk * sign
        net_risk[inst.quote] -= risk * sign
        gross_risk[inst.base] += abs(risk)
        gross_risk[inst.quote] += abs(risk)
        net_notional[inst.base] += notional_base * sign
        net_notional[inst.quote] -= notional_quote * sign
        contributors[inst.base].append(pos.instrument)
        contributors[inst.quote].append(pos.instrument)

    return {
        ccy: CurrencyExposure(
            currency=ccy, net_risk=net_risk[ccy], gross_risk=gross_risk[ccy],
            net_notional=net_notional[ccy], contributors=sorted(set(contributors[ccy])),
        )
        for ccy in sorted(set(net_risk) | set(gross_risk))
    }


def gross_notional(
    positions: Sequence[Position],
    instruments: Dict[str, Instrument],
    mid_prices: Dict[str, Decimal],
    conversions: Optional[Dict[str, Decimal]] = None,
    account_currency: Optional[str] = None,
    unconvertible: Optional[List[str]] = None,
) -> Decimal:
    """Gross notional in the account currency.

    A leg whose quote currency cannot be converted is NOT counted at its
    quote-currency notional: those are different units, and mixing them is
    wrong in an unpredictable direction (21% understated on EUR_GBP, 150x
    overstated on USD_JPY). Such a leg is appended to ``unconvertible`` and
    excluded from the total, so the caller can block rather than act on a
    number that means nothing.
    """
    conversions = conversions or {}
    total = ZERO
    for pos in positions:
        inst = instruments.get(pos.instrument)
        if inst is None:
            continue
        mid = dec(mid_prices.get(pos.instrument, pos.entry_price))
        if inst.quote == account_currency:
            conv = D("1")
        else:
            rate = conversions.get(inst.quote)
            if rate is None or dec(rate) <= 0:
                # Substituting 1.0 is not "the conservative direction": the
                # UNITS are simply wrong. EUR_GBP on a USD account came out 21%
                # UNDERSTATED, so a leverage ceiling that should have bound did
                # not; USD_JPY came out 150x overstated, a spurious veto. Record
                # it as unconvertible -- exactly as currency_exposure already
                # does -- and let the caller block rather than invent a number.
                if unconvertible is not None and inst.quote not in unconvertible:
                    unconvertible.append(inst.quote)
                continue
            conv = dec(rate)
        total += inst.units(pos.lots) * mid * conv
    return total


def correlation_clusters(
    candidates: Sequence[str],
    risk_by_instrument: Dict[str, Decimal],
    correlations: Dict[Tuple[str, str], float],
    threshold: float,
) -> List[Cluster]:
    """Single-linkage clustering over the correlation graph.

    Single linkage (rather than complete linkage) is the conservative choice:
    a chain A~B~C is treated as one cluster even when A and C are not directly
    correlated, because in a stress episode the chain usually closes.
    """
    names = list(dict.fromkeys(candidates))
    parent = {n: n for n in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def corr(a: str, b: str) -> float:
        return correlations.get((a, b), correlations.get((b, a), 0.0))

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if abs(corr(a, b)) >= threshold:
                union(a, b)

    groups: Dict[str, List[str]] = defaultdict(list)
    for n in names:
        groups[find(n)].append(n)

    out: List[Cluster] = []
    for members in groups.values():
        if len(members) < 1:
            continue
        pairwise = [abs(corr(a, b)) for i, a in enumerate(members) for b in members[i + 1:]]
        out.append(Cluster(
            instruments=sorted(members),
            total_risk=sum((dec(risk_by_instrument.get(m, ZERO)) for m in members), ZERO),
            max_pairwise_corr=max(pairwise) if pairwise else 0.0,
        ))
    return sorted(out, key=lambda c: c.total_risk, reverse=True)


def rolling_correlation(series_a: Sequence[float], series_b: Sequence[float]) -> Optional[float]:
    """Pearson correlation of aligned return series. ``None`` when undefined.

    Returning ``None`` rather than 0.0 for a degenerate input matters: 0.0
    would be read as "independent" and would wave through a concentrated
    position. The engine treats ``None`` as "unknown" and falls back to the
    currency-leg limit, which needs no estimate.
    """
    n = min(len(series_a), len(series_b))
    if n < 20:
        return None
    a, b = list(series_a[-n:]), list(series_b[-n:])
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / ((va ** 0.5) * (vb ** 0.5))
