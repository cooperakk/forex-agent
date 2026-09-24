"""The profile type, the registry, and verification against a live terminal."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from ...core.money import D, Instrument, dec


class FillingMode(str, Enum):
    """How the venue wants a market order filled.

    Getting this wrong is not subtle: MT5 rejects every order with
    ``Unsupported filling mode`` and the system looks broken until someone
    reads the terminal's symbol properties. It is per-symbol, not per-broker,
    which is why the profile carries a default and the live check corrects it.
    """

    FOK = "fok"          # fill completely or cancel
    IOC = "ioc"          # fill what you can, cancel the rest
    RETURN = "return"    # leave the remainder as a resting order
    AUTO = "auto"        # ask the terminal per symbol


@dataclass
class SymbolMap:
    """How this broker spells instrument names.

    The canonical form inside this system is always ``EUR_USD``. A venue sees
    whatever this maps it to, and nothing outside the adapter should ever hold
    a venue-specific spelling -- a suffix that leaks into the risk engine or
    the audit journal turns two names for one instrument into two instruments,
    and a netting calculation that thinks EURUSD and EURUSD.m are unrelated is
    a netting calculation that under-counts exposure.
    """

    #: Appended to every symbol: ".m", ".raw", "-ECN", "micro", ...
    suffix: str = ""
    #: Prepended, rare but real.
    prefix: str = ""
    #: Canonical -> venue, for symbols that follow no rule at all.
    overrides: Dict[str, str] = field(default_factory=dict)
    #: Separator in the canonical name that the venue omits.
    strip_separator: bool = True

    def to_venue(self, canonical: str) -> str:
        if canonical in self.overrides:
            return self.overrides[canonical]
        core = canonical.replace("_", "") if self.strip_separator else canonical
        return f"{self.prefix}{core}{self.suffix}"

    def to_canonical(self, venue_symbol: str) -> str:
        for canon, venue in self.overrides.items():
            if venue == venue_symbol:
                return canon
        core = venue_symbol
        if self.prefix and core.startswith(self.prefix):
            core = core[len(self.prefix):]
        if self.suffix and core.endswith(self.suffix):
            core = core[: -len(self.suffix)]
        # FX majors are six characters; anything else is left alone rather than
        # split at a guess, because a wrong split is worse than no split.
        if len(core) == 6 and core.isalpha():
            return f"{core[:3].upper()}_{core[3:].upper()}"
        return core.upper()


def _filling_from_terminal(reported: Any) -> Optional["FillingMode"]:
    """Interpret whatever the terminal reported as a filling mode.

    MT5 reports a BITMASK of permitted modes (FOK=1, IOC=2, RETURN=4) using a
    namespace that overlaps numerically with the values an order carries
    (FOK=0, IOC=1, RETURN=2). Comparing the raw number against the enum's
    string -- which an earlier version did -- could never match, so every
    symbol produced a spurious mismatch and no correction was ever adopted.
    """
    if isinstance(reported, FillingMode):
        return reported
    if isinstance(reported, str):
        try:
            return FillingMode(reported.lower())
        except ValueError:
            return None
    try:
        mask = int(reported)
    except (TypeError, ValueError):
        return None
    if mask & 1:
        return FillingMode.FOK
    if mask & 2:
        return FillingMode.IOC
    if mask & 4:
        return FillingMode.RETURN
    return None


@dataclass
class ProfileMismatch:
    """A declared value that the live terminal contradicts."""

    field: str
    symbol: str
    declared: str
    observed: str

    def __str__(self) -> str:
        return (f"{self.symbol}: {self.field} declared {self.declared}, "
                f"terminal reports {self.observed} (using the terminal's value)")


@dataclass
class BrokerProfile:
    """Everything that differs between one venue and the next."""

    name: str
    display_name: str
    #: Which adapter drives it. Nearly every retail FX broker is "mt5".
    adapter: str = "mt5"
    kind: str = "fx-cfd"

    symbols: SymbolMap = field(default_factory=SymbolMap)

    #: Minimum distance, in POINTS, between price and a stop or target.
    #: A stop closer than this is rejected outright, so the risk engine must
    #: know it BEFORE sizing -- otherwise it computes a position from a stop
    #: the venue will never accept.
    min_stop_level_points: int = 0
    #: Distance within which a pending order cannot be placed.
    freeze_level_points: int = 0

    default_filling: FillingMode = FillingMode.AUTO
    #: Maximum price deviation tolerated on a market order, in points.
    deviation_points: int = 20

    default_contract_size: Decimal = D("100000")
    default_min_lot: Decimal = D("0.01")
    default_lot_step: Decimal = D("0.01")
    default_max_lot: Decimal = D("100")

    #: Commission per lot per ROUND TURN, in the account currency. Zero on a
    #: "commission-free" account where the cost is in the spread instead --
    #: which is not cheaper, only less visible.
    commission_per_lot_round_turn: Decimal = D("0")
    #: Typical spread in pips per instrument, for the cost model's baseline.
    typical_spread_pips: Dict[str, Decimal] = field(default_factory=dict)
    default_spread_pips: Decimal = D("1.2")

    #: Server clock offset from UTC in hours, and whether it observes DST.
    #: Swap is charged at 00:00 SERVER time, so an hour of error puts the
    #: triple-swap on the wrong day for a quarter of the year.
    server_utc_offset_hours: int = 2
    server_observes_dst: bool = True
    triple_swap_weekday: int = 2          # 0=Mon .. Wednesday is the norm

    #: Leverage cap. Affects margin, which affects whether an order is even
    #: accepted -- not just how it is reported.
    max_leverage: int = 30

    #: Capability declarations, checked against the adapter's own report.
    supports_client_order_id: bool = False
    supports_server_side_stop: bool = True
    supports_partial_close: bool = True
    supports_hedging: bool = False

    #: Regulatory and counterparty facts. Stated because they change the
    #: probability that you get your money back, which no strategy can offset.
    regulator: str = "unknown"
    segregated_client_funds: Optional[bool] = None
    negative_balance_protection: Optional[bool] = None

    notes: str = ""
    #: Things a live account has to confirm; see docs/BROKERS.md.
    verify_before_live: List[str] = field(default_factory=list)

    # -- helpers --------------------------------------------------------- #

    def spread_for(self, canonical: str) -> Decimal:
        return self.typical_spread_pips.get(canonical, self.default_spread_pips)

    def min_stop_distance(self, instrument: Instrument) -> Decimal:
        """Minimum stop distance in PRICE terms for this instrument.

        Points are not pips. On a 5-digit EURUSD feed one pip is ten points; on
        a 3-digit JPY feed one pip is ten points too. Using the instrument's
        own tick keeps that conversion in one place.
        """
        return dec(self.min_stop_level_points) * instrument.tick

    def min_stop_pips(self, instrument: Instrument) -> Decimal:
        distance = self.min_stop_distance(instrument)
        return (distance / instrument.pip) if instrument.pip > 0 else D("0")

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["default_filling"] = self.default_filling.value
        for key in ("default_contract_size", "default_min_lot", "default_lot_step",
                    "default_max_lot", "commission_per_lot_round_turn",
                    "default_spread_pips"):
            out[key] = str(out[key])
        out["typical_spread_pips"] = {k: str(v) for k, v
                                      in self.typical_spread_pips.items()}
        return out

    # -- verification ---------------------------------------------------- #

    def verify_against(self, observed: Dict[str, Dict[str, Any]]
                       ) -> Tuple["BrokerProfile", List[ProfileMismatch]]:
        """Reconcile this profile with what the terminal actually reports.

        ``observed`` maps a CANONICAL symbol to the properties the adapter read
        from the venue. Every disagreement is recorded and **the observed value
        wins**. A profile is a prior that lets the system start correctly and
        warn early; it is never allowed to override the venue, because a
        confident wrong number is more dangerous than an unknown one.
        """
        import copy
        mismatches: List[ProfileMismatch] = []
        corrected = copy.deepcopy(self)
        worst_stop_level = self.min_stop_level_points
        #: The observed value adopted for each field, and the symbol it came
        #: from. Adopting the LAST symbol's value would make the result depend
        #: on dictionary order.
        adopted: Dict[str, Any] = {}

        for symbol in sorted(observed):
            props = observed[symbol]

            level = props.get("stop_level_points")
            if level is not None and int(level) != self.min_stop_level_points:
                mismatches.append(ProfileMismatch(
                    "min_stop_level_points", symbol,
                    str(self.min_stop_level_points), str(level)))
                # A profile-wide fallback only. The adapter records the real
                # per-symbol floor and uses that; this is what a symbol the
                # terminal did not describe falls back to, so the safe
                # direction is the widest observed.
                worst_stop_level = max(worst_stop_level, int(level))

            size = props.get("contract_size")
            if size is not None and dec(size) != self.default_contract_size:
                mismatches.append(ProfileMismatch(
                    "contract_size", symbol,
                    str(self.default_contract_size), str(size)))
                adopted.setdefault("default_contract_size", dec(size))

            step = props.get("lot_step")
            if step is not None and dec(step) != self.default_lot_step:
                mismatches.append(ProfileMismatch(
                    "lot_step", symbol, str(self.default_lot_step), str(step)))
                adopted.setdefault("default_lot_step", dec(step))

            freeze = props.get("freeze_level_points")
            if freeze is not None and int(freeze) != self.freeze_level_points:
                mismatches.append(ProfileMismatch(
                    "freeze_level_points", symbol,
                    str(self.freeze_level_points), str(freeze)))
                adopted["freeze_level_points"] = max(
                    int(adopted.get("freeze_level_points", 0)), int(freeze))

            filling = props.get("filling_mode")
            if filling is not None and self.default_filling is not FillingMode.AUTO:
                observed_mode = _filling_from_terminal(filling)
                if observed_mode is not None and observed_mode is not self.default_filling:
                    mismatches.append(ProfileMismatch(
                        "filling_mode", symbol,
                        self.default_filling.value, observed_mode.value))
                    adopted.setdefault("default_filling", observed_mode)

        # The TERMINAL WINS for every field it reported, not only for the stop
        # level. Adopting the observation for one field and silently keeping
        # the declaration for the rest meant a profile that got the contract
        # size wrong sized every position by that factor, while the mismatch
        # sat in a list nobody acted on.
        for field_name, value in adopted.items():
            setattr(corrected, field_name, value)
        corrected.min_stop_level_points = worst_stop_level
        return corrected, mismatches


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

_PROFILES: Dict[str, BrokerProfile] = {}


def register_profile(profile: BrokerProfile, *, replace: bool = False) -> BrokerProfile:
    key = profile.name.lower()
    if key in _PROFILES and not replace:
        raise ValueError(
            f"a broker profile named {profile.name!r} is already registered. "
            "Two profiles for one venue means half the system uses each.")
    _PROFILES[key] = profile
    return profile


def get_profile(name: str) -> BrokerProfile:
    key = (name or "").lower().strip()
    if key not in _PROFILES:
        raise KeyError(
            f"no broker profile named {name!r}. Known: "
            f"{', '.join(sorted(_PROFILES))}. Use 'generic_mt5' for an "
            "unlisted MetaTrader broker -- it declares nothing it cannot "
            "verify and reads everything from the terminal.")
    return _PROFILES[key]


def list_profiles() -> List[BrokerProfile]:
    return [_PROFILES[k] for k in sorted(_PROFILES)]


def resolve_profile(name: Optional[str]) -> Optional[BrokerProfile]:
    """Look up a profile, tolerating None and unknown names.

    Returns None rather than raising for an unknown name, because a missing
    profile must degrade to "read everything from the terminal", never to a
    process that will not start.
    """
    if not name:
        return None
    try:
        return get_profile(name)
    except KeyError:
        return None


def infer_symbol_map(venue_symbols: List[str]) -> SymbolMap:
    """Guess a broker's symbol convention from the list it advertises.

    Used by `generic_mt5`, which declares nothing. Looks for the majors under
    a common suffix; if it cannot find at least two, it returns an empty map
    and the adapter falls back to exact names. Guessing loudly and being
    checked is fine; guessing quietly is not.
    """
    majors = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD",
              "NZDUSD", "EURJPY", "EURGBP", "GBPJPY")
    suffix_votes: Dict[str, int] = {}
    for symbol in venue_symbols:
        upper = symbol.upper()
        for major in majors:
            if upper == major:
                suffix_votes[""] = suffix_votes.get("", 0) + 1
            elif upper.startswith(major) and len(symbol) > len(major):
                found = symbol[len(major):]
                suffix_votes[found] = suffix_votes.get(found, 0) + 1
    if not suffix_votes:
        return SymbolMap()

    ranked = sorted(suffix_votes.items(), key=lambda kv: (-kv[1], kv[0]))
    best, votes = ranked[0]
    # REFUSE ON A TIE. A broker offering both a plain and a suffixed book --
    # Standard plus ECN/raw, which is very common -- would otherwise be decided
    # by whichever the terminal happened to list first, and orders would route
    # to one book while conversion rates came from the other. Two books under
    # one name, inconsistently, is worse than no inference at all.
    if len(ranked) > 1 and ranked[1][1] == votes:
        import logging
        logging.getLogger(__name__).warning(
            "cannot infer a symbol suffix: %s are equally represented in the "
            "terminal's symbol list. Falling back to exact names. Set the "
            "suffix explicitly in a broker profile if orders are rejected.",
            ", ".join(repr(name) for name, count in ranked if count == votes))
        return SymbolMap()
    return SymbolMap(suffix=best)
