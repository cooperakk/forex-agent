"""How a release gets its impact tier.

The tier decides whether an event blacks trading out, and the honest ordering
of evidence for it is:

1. **What the market actually did.** For each past release of a series, the
   realised move in the window around it, divided by the same instrument's
   ordinary move over a window of the same length. A ratio of 3 means that
   release moved the market three times as far as a random hour. This is a
   measurement and it is the only input here that is one.

2. **A curated judgement**, used when there is not enough of (1).
   ``CURATED_TIER`` below is a *judgement*, not a measurement: it encodes the
   conventional view of which releases move FX, held by people who have watched
   them, and it is wrong in the usual ways -- it is a point-in-time view that
   goes stale (payrolls dominated the 2010s; through the 2021-2023 inflation
   episode CPI mattered more), it does not know which currency pair is being
   traded, and it does not know what the market is currently focused on. It is
   a prior to be overwritten, not a fact.

3. Nothing. An unknown release defaults to ``medium``, which does not black out
   trading at the default ``min_impact="high"``.

The measurement path needs a minimum number of observations before it may
overrule the judgement, and the reason is not statistical fastidiousness: a
single crisis-day release with a 6x ratio would otherwise promote a whole
series to ``high`` forever, and a single holiday-thin release would demote
payrolls.

**What this does NOT do:** it never promotes an event above its curated tier on
thin evidence, and it never demotes below ``high`` a series the curation calls
``high`` unless the evidence is both plentiful and clear. The asymmetry is
deliberate. Over-blacking-out costs opportunity; under-blacking-out costs money
during the one hour of the month when the spread is eight times normal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

# A judgement, not a measurement. See the module docstring.
CURATED_TIER: dict[str, str] = {
    "US.NFP": "high", "US.CPI": "high", "US.FOMC": "high", "US.PCE": "high",
    "US.GDP_ADV": "medium", "US.RETAIL_SALES": "medium", "US.ISM_MFG": "medium",
    "US.ISM_SERVICES": "medium", "US.JOLTS": "low", "US.CLAIMS": "low",
    "EU.ECB": "high", "EU.CPI_FLASH": "medium", "EU.PMI_FLASH": "medium",
    "EU.GDP": "medium", "EU.IFO": "low",
    "UK.BOE": "high", "UK.CPI": "medium", "UK.EMPLOYMENT": "medium", "UK.GDP": "medium",
    "JP.BOJ": "high", "JP.CPI": "low", "JP.TANKAN": "low",
    "AU.RBA": "high", "AU.EMPLOYMENT": "medium", "AU.CPI": "medium",
    "CA.BOC": "high", "CA.EMPLOYMENT": "medium", "CA.CPI": "medium",
    "CH.SNB": "high", "NZ.RBNZ": "high",
}

# Name fragments, for events that arrive from a feed with no series id. A
# weaker instrument than a series key and it is only consulted as a last resort:
# "Core CPI (ex-food)" and "CPI expectations survey" both contain "CPI" and only
# one of them empties the order book.
NAME_HINTS: Sequence[tuple] = (
    ("non-farm", "high"), ("nonfarm", "high"), ("payroll", "high"),
    ("rate decision", "high"), ("interest rate", "high"), ("fomc", "high"),
    ("monetary policy", "high"), ("consumer price", "high"), ("cpi", "high"),
    ("gdp", "medium"), ("retail sales", "medium"), ("pmi", "medium"),
    ("unemployment", "medium"), ("trade balance", "low"), ("sentiment", "low"),
)

TIERS = ("low", "medium", "high")


@dataclass
class TierVerdict:
    series_id: str
    tier: str
    source: str                   # measured | curated | name_hint | default
    n_observations: int = 0
    median_ratio: float | None = None
    iqr_ratio: float | None = None
    rationale: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"series_id": self.series_id, "tier": self.tier, "source": self.source,
                "n_observations": self.n_observations,
                "median_ratio": (round(self.median_ratio, 3)
                                 if self.median_ratio is not None else None),
                "iqr_ratio": round(self.iqr_ratio, 3) if self.iqr_ratio is not None else None,
                "rationale": self.rationale, "warnings": self.warnings}


def curated_tier(series_id: str | None, name: str = "") -> TierVerdict:
    """The fallback. Judgement first, then a name match, then ``medium``."""
    if series_id and series_id in CURATED_TIER:
        return TierVerdict(series_id or name, CURATED_TIER[series_id], "curated",
                           rationale="curated tier: a conventional judgement about which "
                                     "releases move FX, not a measurement of this one")
    lowered = (name or "").lower()
    for fragment, tier in NAME_HINTS:
        if fragment in lowered:
            return TierVerdict(series_id or name, tier, "name_hint",
                               rationale=f"matched the name fragment {fragment!r}",
                               warnings=["tiered by a substring of the event name; this is "
                                         "the weakest evidence in the module and a "
                                         "same-named survey will be tiered like the release"])
    return TierVerdict(series_id or name, "medium", "default",
                       rationale="unknown release; defaulted to medium, which does not "
                                 "black out trading at the default high-impact gate")


def tier_from_observations(series_id: str, ratios: Sequence[float], *,
                           curated: str | None = None,
                           min_observations: int = 12,
                           high_ratio: float = 2.0,
                           low_ratio: float = 1.25) -> TierVerdict:
    """Tier one series from realised move / baseline move ratios.

    The median is used rather than the mean because one crisis print otherwise
    carries the whole series. The interquartile range is reported alongside it:
    a series whose ratio swings between 1.0 and 5.0 is not really a tier, it is
    two different events sharing a name, and the operator should see that rather
    than a single confident label.
    """
    arr = np.array([r for r in ratios if np.isfinite(r) and r > 0], dtype=float)
    fallback = curated or (CURATED_TIER.get(series_id) or "medium")
    if arr.size < min_observations:
        v = curated_tier(series_id)
        v.n_observations = int(arr.size)
        v.warnings.append(
            f"only {arr.size} realised-volatility observations for {series_id} "
            f"(need {min_observations}); the measured tier cannot yet overrule the "
            "judgement")
        return v

    median = float(np.median(arr))
    q1, q3 = float(np.percentile(arr, 25)), float(np.percentile(arr, 75))
    iqr = q3 - q1
    warnings: list[str] = []
    if median > 0 and iqr / median > 1.0:
        warnings.append(
            f"the ratio spread is wider than its own median (IQR {iqr:.2f} vs median "
            f"{median:.2f}): this series behaves like two different events and a single "
            "tier is a summary of something that is not one thing")

    if median >= high_ratio:
        measured = "high"
    elif median >= low_ratio:
        measured = "medium"
    else:
        measured = "low"

    # The asymmetry: evidence may freely PROMOTE a series, but demoting one the
    # curation calls high needs both plenty of observations and a clear result.
    # Being wrong about a promotion costs a missed trade. Being wrong about a
    # demotion means holding a position through a payroll print.
    tier = measured
    if TIERS.index(measured) < TIERS.index(fallback):
        demote_ok = arr.size >= 2 * min_observations and q3 < high_ratio
        if not demote_ok:
            tier = fallback
            warnings.append(
                f"measured tier {measured!r} is below the curated {fallback!r}; kept the "
                f"curated tier because a demotion needs {2 * min_observations}+ "
                "observations and an upper quartile that is also quiet. An unjustified "
                "demotion means trading through the release.")
    return TierVerdict(
        series_id=series_id, tier=tier, source="measured" if tier == measured else "curated",
        n_observations=int(arr.size), median_ratio=median, iqr_ratio=iqr,
        rationale=(f"{arr.size} releases moved the market a median {median:.2f}x its "
                   f"ordinary move over the same window (IQR {iqr:.2f})"),
        warnings=warnings)


def classify_calendar(calendar, *, as_of_ns: int | None = None,
                      min_observations: int = 12,
                      write_back: bool = True,
                      allow_full_history: bool = False) -> dict[str, TierVerdict]:
    """Tier every series in the calendar and optionally persist the result.

    ``as_of_ns`` keeps this causal: tiering a 2024 event using volatility
    observed in 2025 is look-ahead, and it is the kind that quietly improves a
    backtest by blacking out exactly the releases that turned out to matter.

    It now DEFAULTS to the current time rather than to "all of history".
    Defaulting to None meant the honest, causal behaviour was opt-in and the
    look-ahead was what you got by forgetting an argument -- which is the wrong
    way round for anything whose failure mode is a backtest that looks better
    than the strategy. Pass ``allow_full_history=True`` to deliberately tier
    over everything, which is correct for a report about the past and wrong
    for anything feeding a decision.
    """
    if as_of_ns is None and not allow_full_history:
        from ..core.clock import wall_ns
        as_of_ns = wall_ns()
    out: dict[str, TierVerdict] = {}
    for series_id in calendar.series_ids():
        obs = calendar.volatility_observations(series_id, before_ns=as_of_ns)
        ratios = [o["window_move_pips"] / o["baseline_move_pips"]
                  for o in obs if o["baseline_move_pips"] > 0]
        verdict = tier_from_observations(series_id, ratios,
                                         min_observations=min_observations)
        out[series_id] = verdict
        # Write back ONLY a measured tier. Calling set_measured_impact(None)
        # resets the effective impact to the curated one, which would silently
        # demote an event an operator had deliberately marked high -- removing
        # its blackout window on the strength of having no evidence either way.
        # Absence of measurement is not evidence for the curated value.
        if write_back and verdict.source == "measured":
            calendar.set_measured_impact(series_id, verdict.tier)
    return out


def observe_release_volatility(frame, event_ns: int, *, pip: float,
                               window_bars: int = 2,
                               baseline_bars: int = 60) -> dict[str, float] | None:
    """Measure the move around one release from a bar frame.

    ``frame`` is a UTC-indexed OHLC DataFrame. The window is the ``window_bars``
    bars beginning at the release; the baseline is the median move over the
    ``baseline_bars`` bars BEFORE it -- before, never around, because a baseline
    that includes the release is a baseline the release inflates, and every
    ratio then collapses toward one.
    """
    if frame is None or len(frame) == 0:
        return None
    try:
        # Compare integer nanoseconds, not datetimes. A tz-aware DatetimeIndex
        # refuses to be compared with a tz-naive numpy datetime64 and raises --
        # which this function used to swallow, so every measurement returned
        # None and the whole measured-tier path silently never ran.
        idx = frame.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_convert("UTC")
        # `.as_unit("ns")` before the cast, NOT a bare `.view("int64")`. A
        # pandas DatetimeIndex may carry microsecond resolution, and viewing it
        # as int64 then yields MICROseconds -- a thousandfold error that makes
        # every search land past the end of the frame, so the function returned
        # None every time and the measured-tier path silently never ran.
        ints = idx.as_unit("ns").astype("int64").to_numpy()
        pos = int(ints.searchsorted(int(event_ns), side="left"))
    except Exception:  # noqa: BLE001
        return None
    if pos <= baseline_bars or pos + window_bars > len(frame):
        return None
    win = frame.iloc[pos: pos + window_bars]
    base = frame.iloc[pos - baseline_bars: pos]
    window_move = float(win["high"].max() - win["low"].min()) / pip
    base_moves = ((base["high"] - base["low"]) / pip).rolling(window_bars).sum().dropna()
    if base_moves.empty:
        return None
    baseline = float(base_moves.median())
    if baseline <= 0:
        return None
    return {"window_move_pips": window_move, "baseline_move_pips": baseline,
            "ratio": window_move / baseline}
