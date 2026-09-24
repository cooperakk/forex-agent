"""How news is allowed to affect trading.

Three roles, in ascending order of authority, and a strategy may only hold the
role it has earned:

``risk_filter``  (default, always available)
    News can *stop* a trade and *shrink* a position. It can never start one or
    enlarge one. This direction is safe because being wrong costs opportunity,
    and the research brief's own recommendation is to test the filter path
    first -- noting that even a filter is not automatically profitable, since
    it also removes good trades.

``meta_label``   (requires a passed validation run)
    News features join the secondary act/skip model. Still cannot choose a
    side; still cannot enlarge beyond the risk engine's size.

``signal``       (requires the full acceptance protocol AND a clean LAP test
                  on post-cutoff data)
    News may originate a trade. Nothing reaches this state by default.

The asymmetry is the design. A filter that is wrong costs a missed trade; a
signal that is wrong costs money, and the evidence bar is set accordingly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from ..core.money import D, dec
from .calendar import EconomicCalendar
from .llm_extract import Extraction


@dataclass
class NewsAssessment:
    instrument: str
    blocked: bool
    size_multiplier: Decimal
    reasons: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    extractions: list[str] = field(default_factory=list)
    role: str = "risk_filter"
    # High-impact releases expected soon whose DATE the system does not actually
    # know. Reported, never enforced: see NewsPolicy.assess.
    advisories: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"instrument": self.instrument, "blocked": self.blocked,
                "size_multiplier": str(self.size_multiplier), "reasons": self.reasons,
                "events": self.events, "extractions": self.extractions,
                "role": self.role, "advisories": self.advisories}


class NewsPolicy:
    def __init__(self, calendar: EconomicCalendar | None, *, role: str = "risk_filter",
                 before_min: int = 30, after_min: int = 30,
                 min_impact: str = "high",
                 correction_size_multiplier: Decimal = D("0.5"),
                 contradiction_blocks: bool = True,
                 require_certain_dates: bool = True,
                 unconfirmed_size_multiplier: Decimal = D("0.75")) -> None:
        if role not in ("risk_filter", "meta_label", "signal"):
            raise ValueError(f"unknown news role {role!r}")
        self.calendar = calendar
        self.role = role
        self.before_min = before_min
        self.after_min = after_min
        self.min_impact = min_impact
        self.correction_size_multiplier = dec(correction_size_multiplier)
        self.contradiction_blocks = contradiction_blocks
        # A release whose date is a pattern estimate must not create a hard
        # blackout: blocking on a guess sits out the wrong day AND clears the
        # right one, so the agent trades into the print believing it is
        # protected. It does justify carrying less, which is a bounded,
        # one-directional response to a known unknown.
        self.require_certain_dates = require_certain_dates
        self.unconfirmed_size_multiplier = dec(unconfirmed_size_multiplier)

    def assess(self, now_ns: int, instruments: Sequence[str],
               extractions: Sequence[Extraction] | None = None
               ) -> dict[str, NewsAssessment]:
        out = {sym: NewsAssessment(sym, False, D("1"), role=self.role) for sym in instruments}

        if self.calendar is not None:
            blackout = self.calendar.instrument_blackout(
                now_ns, instruments, before_min=self.before_min,
                after_min=self.after_min, min_impact=self.min_impact,
                require_certain=self.require_certain_dates)
            for sym, label in blackout.items():
                a = out[sym]
                a.blocked = True
                a.events.append(label)
                a.reasons.append(
                    f"scheduled-event window ({label}): the spread widens several-fold "
                    "and the price reprices faster than this path can react")

            # Unconfirmed high-impact dates: advise and shrink, never block.
            try:
                pending = self.calendar.advisories(
                    now_ns, sorted({p for sym in instruments
                                    for p in sym.replace("/", "_").split("_")
                                    if len(p) == 3}),
                    horizon_sec=self.before_min * 60, min_impact=self.min_impact)
            except AttributeError:
                pending = []
            for adv in pending:
                for sym in instruments:
                    if adv["currency"] not in sym:
                        continue
                    a = out[sym]
                    a.advisories.append(f"{adv['currency']}: {adv['name']} (date unconfirmed)")
                    a.size_multiplier = min(a.size_multiplier,
                                            self.unconfirmed_size_multiplier)
                    a.reasons.append(
                        f"{adv['name']} is expected within the blackout horizon but its "
                        "date is a pattern estimate, not a confirmed release. That is not "
                        "grounds to stop trading -- it is grounds to carry less until a "
                        "feed confirms the date.")

        for ex in extractions or ():
            if not ex.valid:
                continue
            for sym in instruments:
                if not any(ccy in sym for ccy in ex.currencies):
                    continue
                a = out[sym]
                a.extractions.append(ex.article_id)
                if ex.is_correction:
                    a.size_multiplier = min(a.size_multiplier, self.correction_size_multiplier)
                    a.reasons.append(
                        f"{ex.article_id} is a correction to an earlier report; the first "
                        "print is now known to have been wrong, so size is reduced")
                if ex.contradicts_prior and self.contradiction_blocks:
                    a.blocked = True
                    a.reasons.append(
                        f"{ex.article_id} contradicts previous reporting; the state of the "
                        "world is genuinely unclear and a position is a guess")
                if ex.errors:
                    a.size_multiplier = min(a.size_multiplier, D("0.75"))
                    a.reasons.append(f"{ex.article_id} extraction carried warnings: "
                                     f"{ex.errors[0][:80]}")
        # A filter may never enlarge a position: clamp defensively even though
        # nothing above sets a multiplier above 1.
        for a in out.values():
            if a.size_multiplier > D("1"):
                a.size_multiplier = D("1")
        return out

    def may_originate_trade(self) -> bool:
        return self.role == "signal"

    def describe(self) -> dict:
        return {
            "role": self.role,
            "window_minutes": {"before": self.before_min, "after": self.after_min},
            "min_impact": self.min_impact,
            "requires_confirmed_dates_to_block": self.require_certain_dates,
            "can_start_a_trade": self.may_originate_trade(),
            "can_increase_size": False,
            "promotion_requirements": {
                "meta_label": ["a passed validation run on post-cutoff articles"],
                "signal": ["the full acceptance protocol",
                           "a clean lookahead-propensity test",
                           "post-cutoff articles only",
                           "positive under the 2x cost and 2x latency stress"],
            },
        }
