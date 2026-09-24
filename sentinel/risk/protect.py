"""Profit protection.

Turning an open gain into a realised one is a separate problem from finding an
entry, and it is the half most retail systems leave to hope. Four mechanisms,
all pre-committed in config and all evaluated on every tick:

* **Break-even move** at a configured R. Removes the loss tail of a trade that
  has already worked. The stop is placed at entry plus the ROUND-TRIP COST, not
  at entry: a stop at the entry price realises a small loss, because the
  commission and the spread have already been paid.
* **Partial take** at a configured R. Banks a fraction and leaves a runner.
* **ATR trail** once activated. Follows the move without the fixed target that
  caps a trend.
* **Give-back ratchet** once the peak excursion arms it. This is the one that
  does not need an indicator, and it exists because the ATR trail silently does
  nothing whenever the ATR is unavailable -- which is exactly when a position
  can run to +8R protected by nothing but the break-even stop.

Three invariants are enforced here rather than trusted to callers:

1. A stop only ever moves toward safety. Every widening is refused.
2. Actions are idempotent -- ``breakeven_moved`` and ``partial_taken`` are
   flags on the position, so a tick storm cannot scale out four times.
3. Every rule is expressed in R, and R is computed from the risk of the lots
   ACTUALLY held. The broker keeps ``initial_risk`` in step with ``lots`` so a
   scale-out does not halve the runner's apparent excursion.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from ..core.config import RiskConfig
from ..core.money import CostModel, D, Instrument, ZERO, dec
from ..core.types import Position, Quote, Side


class ProtectAction(str, Enum):
    MOVE_STOP = "move_stop"
    PARTIAL_CLOSE = "partial_close"
    CLOSE = "close"


@dataclass
class ProtectInstruction:
    action: ProtectAction
    instrument: str
    new_stop: Optional[Decimal] = None
    close_lots: Optional[Decimal] = None
    reason: str = ""
    r_at_trigger: Decimal = ZERO
    # Which rule produced this. The caller needs it to know whether marking the
    # position "break-even moved" is warranted: marking it for ANY stop move
    # meant the first trail permanently disabled the break-even rule.
    rule: str = ""

    def to_dict(self) -> dict:
        return {"action": self.action.value, "instrument": self.instrument,
                "new_stop": str(self.new_stop) if self.new_stop is not None else None,
                "close_lots": str(self.close_lots) if self.close_lots is not None else None,
                "reason": self.reason, "r_at_trigger": str(self.r_at_trigger),
                "rule": self.rule}


def _tighter(side: Side, current: Optional[Decimal], candidate: Decimal) -> bool:
    """True only when ``candidate`` is strictly safer than ``current``."""
    if current is None:
        return True
    return candidate > current if side is Side.BUY else candidate < current


def _price_at_r(position: Position, instrument: Instrument, target_r: Decimal,
                risk_distance: Decimal) -> Optional[Decimal]:
    """The price at which this position stands at ``target_r``.

    ``risk_distance`` is the entry-to-stop distance for ONE R, in price terms.
    """
    if risk_distance <= 0:
        return None
    sign = D(position.side.sign)
    return instrument.round_price(position.entry_price + risk_distance * target_r * sign)


def evaluate_protection(
    position: Position,
    quote: Quote,
    instrument: Instrument,
    config: RiskConfig,
    *,
    atr: Optional[Decimal] = None,
    quote_to_account: Decimal = D("1"),
    cost_model: Optional[CostModel] = None,
) -> List[ProtectInstruction]:
    out: List[ProtectInstruction] = []
    if position.initial_risk <= 0:
        # Without a defined initial risk there is no R, and every rule below is
        # expressed in R. Refusing to guess is safer than inventing a scale.
        return out

    r = position.r_multiple(quote, instrument, quote_to_account)
    if r is None:
        return out

    entry = position.entry_price
    sign = D(position.side.sign)

    # ONE R, in price terms, derived from `initial_risk` -- the risk the trade
    # was SIZED for -- and never from the live stop.
    #
    # Reading it from `position.stop_loss` was subtly catastrophic: the stop
    # moves. Once break-even had tightened it to entry+1.8 pips, "one R" read
    # as 1.8 pips, so the give-back floor landed 7 pips above entry instead of
    # 200 -- looser than the stop already there, refused as a widening, and the
    # ratchet silently stopped protecting the +8R position it exists for. After
    # an ATR trail the error runs the other way: 1R read as 100 pips, the floor
    # landed beyond the market, and the position was CLOSED at +6R for no
    # reason. Same expression, opposite failures, both invisible in a backtest
    # where the stop happens not to have moved yet.
    risk_distance = ZERO
    pip_val = instrument.pip_value_quote(position.lots) * quote_to_account
    if position.initial_risk > 0 and pip_val > 0:
        risk_distance = (position.initial_risk / pip_val) * instrument.pip
    if risk_distance <= 0 and position.stop_loss:
        # Last resort only: no usable risk figure, so fall back to the stop.
        risk_distance = abs(entry - position.stop_loss)

    # --- 1. break-even ------------------------------------------------- #
    if (config.breakeven_trigger_r > 0 and not position.breakeven_moved
            and r >= config.breakeven_trigger_r and risk_distance > 0):
        # Entry plus the ROUND-TRIP COST, so the stop actually breaks even.
        #
        # The buffer used to be a hard-coded half pip. The system's own cost
        # model puts a 1.0-lot EUR_USD round trip at ~1.5 pips, of which 0.7 is
        # commission, so a half-pip buffer guaranteed a small LOSS on every
        # trade that came back to its "break-even" stop -- measured at a mean
        # of -0.01R across the trades where it fired. A rule named break-even
        # that reliably loses money is a rule nobody can reason about.
        buffer = _breakeven_buffer(instrument, position, quote_to_account, cost_model)
        candidate = instrument.round_price(entry + buffer * sign)
        # Rounding can land the candidate back on the entry when tick == pip.
        # Nudge it one tick further into profit rather than accept a flat stop.
        if (candidate - entry) * sign <= 0:
            candidate = instrument.round_price(entry + instrument.tick * sign)
        if _tighter(position.side, position.stop_loss, candidate):
            out.append(ProtectInstruction(
                ProtectAction.MOVE_STOP, position.instrument, new_stop=candidate,
                reason=f"break-even (net of cost) at {r:.2f}R", r_at_trigger=r,
                rule="breakeven"))

    # --- 2. partial take ------------------------------------------------ #
    if (config.partial_take_fraction > 0 and not position.partial_taken
            and config.partial_take_r > 0 and r >= config.partial_take_r):
        lots = instrument.round_lots_down(position.lots * config.partial_take_fraction)
        remaining = position.lots - lots
        # Do not create a residual below the minimum tradable size -- an
        # unclosable dust position is worse than no scale-out.
        if lots >= instrument.min_lot and remaining >= instrument.min_lot:
            out.append(ProtectInstruction(
                ProtectAction.PARTIAL_CLOSE, position.instrument, close_lots=lots,
                reason=f"scale out {config.partial_take_fraction} at {r:.2f}R",
                r_at_trigger=r, rule="partial_take"))

    # --- 3. ATR trail ---------------------------------------------------- #
    if (config.trail_atr_multiple > 0 and atr and atr > 0
            and r >= config.trail_activate_r):
        ref = quote.price_for(position.side.opposite)
        candidate = instrument.round_price(ref - dec(atr) * config.trail_atr_multiple * sign)
        if _tighter(position.side, position.stop_loss, candidate):
            # Never trail past the current price -- that would close at market
            # via the stop, which is not what a trail is for.
            beyond = (candidate >= ref) if position.side is Side.BUY else (candidate <= ref)
            if not beyond:
                out.append(ProtectInstruction(
                    ProtectAction.MOVE_STOP, position.instrument, new_stop=candidate,
                    reason=f"ATR trail {config.trail_atr_multiple}x at {r:.2f}R",
                    r_at_trigger=r, rule="atr_trail"))

    # --- 4. give-back ratchet -------------------------------------------- #
    # Needs no indicator, so it still works when the ATR is unavailable -- the
    # case where a position could previously run to +8R with nothing but the
    # break-even stop under it.
    peak = position.max_favourable
    if (config.giveback_arm_r > 0 and risk_distance > 0
            and peak >= config.giveback_arm_r):
        floor_r = peak * config.giveback_keep_fraction
        candidate = _price_at_r(position, instrument, floor_r, risk_distance)
        if candidate is not None and _tighter(position.side, position.stop_loss, candidate):
            ref = quote.price_for(position.side.opposite)
            beyond = (candidate >= ref) if position.side is Side.BUY else (candidate <= ref)
            if beyond:
                # The market has already fallen through the floor we want to
                # protect. A stop there would not fill at that price, so close
                # now rather than pretend otherwise.
                out.append(ProtectInstruction(
                    ProtectAction.CLOSE, position.instrument,
                    reason=(f"give-back stop: peaked at {peak:.2f}R, now {r:.2f}R, "
                            f"below the {floor_r:.2f}R floor"),
                    r_at_trigger=r, rule="giveback"))
            else:
                out.append(ProtectInstruction(
                    ProtectAction.MOVE_STOP, position.instrument, new_stop=candidate,
                    reason=(f"give-back ratchet: keep {config.giveback_keep_fraction} "
                            f"of the {peak:.2f}R peak"),
                    r_at_trigger=r, rule="giveback"))

    # A CLOSE supersedes every stop move: there is nothing left to protect.
    if any(i.action is ProtectAction.CLOSE for i in out):
        return [i for i in out if i.action is ProtectAction.CLOSE][:1]

    # Collapse competing stop moves to the single safest one, but keep the
    # break-even rule's identity if it won, so the caller marks the right flag.
    stop_moves = [i for i in out if i.action is ProtectAction.MOVE_STOP]
    if len(stop_moves) > 1:
        best = max(stop_moves, key=lambda i: i.new_stop) if position.side is Side.BUY \
            else min(stop_moves, key=lambda i: i.new_stop)
        out = [i for i in out if i.action is not ProtectAction.MOVE_STOP] + [best]
    return out


def _breakeven_buffer(instrument: Instrument, position: Position,
                      quote_to_account: Decimal,
                      cost_model: Optional[CostModel]) -> Decimal:
    """Price distance that covers the round trip on the lots still held."""
    if cost_model is None:
        # No cost model supplied: fall back to the spread plus a pip of
        # commission allowance. Still far better than half a pip.
        return instrument.pip * D("1.5")
    try:
        pip_val = instrument.pip_value_quote(position.lots) * quote_to_account
        cost_pips = cost_model.round_trip_pips(instrument, position.lots, pip_val)
    except Exception:  # noqa: BLE001 - a cost model that cannot price is not fatal
        return instrument.pip * D("1.5")
    if cost_pips <= 0:
        return instrument.pip * D("1.5")
    # A tick of headroom so the fill, after its own slippage, is still >= 0.
    return cost_pips * instrument.pip + instrument.tick


def time_stop(position: Position, now_ns: int, max_hold_sec: int) -> Optional[ProtectInstruction]:
    """Close a position that has stopped being about its thesis.

    A trade held far past its intended horizon is no longer the trade that was
    tested; its statistics belong to a different distribution.
    """
    if max_hold_sec <= 0:
        return None
    held = (now_ns - position.opened_ns) / 1e9
    if held >= max_hold_sec:
        return ProtectInstruction(
            ProtectAction.CLOSE, position.instrument,
            reason=f"time stop: held {held / 3600:.1f}h beyond the {max_hold_sec / 3600:.1f}h horizon",
            # The venue records a SHORT exit code, not this sentence. Without a
            # rule tag the caller passed the prose through, the broker truncated
            # it to 32 characters, and the autopsy's `slow_bleed` test -- which
            # compares against the literal "time_stop" -- could never match. The
            # exit fired correctly and the diagnostic that exists to learn from
            # it was invisible.
            rule="time_stop")
    return None


def _ny_utc_offset_hours(dt) -> int:
    """UTC offset of America/New_York, DST-aware, with no tz database needed.

    Delegates to ``core.tzrules``, which carries the same arithmetic generalised
    to the four zones an economic release can be scheduled in. Kept as a name
    here because the weekend-flat rule is the one caller that only ever needs
    New York, and because the news calendar now depends on the same rules -- two
    copies of a DST table drift apart exactly once, in the three weeks a year
    when the US and European switches disagree.
    """
    from ..core.tzrules import utc_offset_hours

    return utc_offset_hours("America/New_York", dt.replace(tzinfo=None))


def weekend_flat(position: Position, now_ns: int,
                 friday_close_utc_hour: Optional[int] = None,
                 *, ny_close_hour: int = 16) -> Optional[ProtectInstruction]:
    """Flatten before the weekend gap.

    A stop cannot protect across a gap: Monday opens where it opens. Holding
    over the weekend converts a bounded loss into an unbounded one.

    The FX week ends at 17:00 New York, which is 21:00 UTC in summer and 22:00
    in winter. A fixed UTC hour therefore flattens two to three hours early for
    half the year and the offset changes silently twice a year -- forfeiting the
    Friday New York afternoon every week. ``ny_close_hour`` is expressed in New
    York time and defaults to 16:00, an hour before the venue close, which is
    the margin the gap risk actually warrants.

    ``friday_close_utc_hour`` is still honoured when supplied, so an operator
    who has deliberately pinned a UTC hour keeps it.
    """
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(now_ns / 1e9, tz=timezone.utc)
    if dt.weekday() != 4:
        return None
    if friday_close_utc_hour is not None:
        cutoff_utc = friday_close_utc_hour
    else:
        cutoff_utc = ny_close_hour - _ny_utc_offset_hours(dt)
    if dt.hour >= cutoff_utc:
        return ProtectInstruction(
            ProtectAction.CLOSE, position.instrument,
            reason="weekend flat: a stop does not survive the Monday gap",
            rule="weekend_flat")
    return None
