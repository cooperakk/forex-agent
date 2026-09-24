"""Position sizing.

Two ideas the research brief insists on and most retail systems skip:

1. **Rounding error is reported, not hidden.** With a 0.01-lot floor a small
   account cannot express a 0.5% risk on a 12-pip stop; the rounded size can
   be 30% away from the intent. ``SizingResult.rounding_error_pct`` makes that
   visible, and the engine refuses the trade when it exceeds the tolerance.

2. **Granularity sets a minimum account size.** ``E >= (lot_step/err) *
   pip_value * S / risk`` (section B-10). Below it, either the horizon has to
   lengthen or the project's goal has to change. The engine surfaces the
   required equity rather than silently trading a distorted size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from ..core.money import (LOT_GRANULARITY_TOLERANCE, D, Instrument, ZERO, dec)


@dataclass
class SizingResult:
    lots: Decimal
    risk_amount: Decimal
    risk_pct: Decimal
    intended_lots: Decimal
    rounding_error_pct: Decimal
    pip_value_account: Decimal
    stop_pips: Decimal
    required_min_equity: Decimal
    notes: List[str] = field(default_factory=list)
    feasible: bool = True

    @property
    def rejected_reason(self) -> Optional[str]:
        return None if self.feasible else "; ".join(self.notes)


def size_position(
    *,
    equity: Decimal,
    risk_pct: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    instrument: Instrument,
    quote_to_account: Optional[Decimal] = None,
    risk_multiplier: Decimal = D("1"),
    max_lots: Decimal = D("100"),
    max_rounding_error_pct: Decimal = LOT_GRANULARITY_TOLERANCE * D("100"),
) -> SizingResult:
    """Risk-based sizing in the account currency.

    ``risk_multiplier`` is the drawdown ladder's de-risking factor. It scales
    the budget, never the stop: shrinking risk by widening the stop would keep
    the loss and lose the protection.
    """
    notes: List[str] = []
    if quote_to_account is None:
        # money.pip_value_account refuses to assume 1.0 for exactly this reason;
        # silently assuming it here made the same mistake one layer up. On a USD
        # account trading EUR_GBP at 1.27 the omission oversizes by 27% while
        # SizingResult.risk_pct still reports the budget it was asked for; on
        # USD_JPY it undersizes by 150x.
        return SizingResult(ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO,
                            ["no quote->account conversion rate supplied; refusing to "
                             "assume 1.0, which would mis-size by the exchange rate"],
                            False)
    equity = dec(equity)
    stop_distance = abs(dec(entry_price) - dec(stop_price))
    if stop_distance <= 0:
        return SizingResult(ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO,
                            ["entry and stop are identical: no definable risk"], False)
    if equity <= 0:
        return SizingResult(ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO,
                            ["equity is not positive"], False)

    stop_pips = stop_distance / instrument.pip
    effective_risk_pct = dec(risk_pct) * dec(risk_multiplier)
    if effective_risk_pct <= 0:
        return SizingResult(ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, stop_pips, ZERO,
                            ["risk budget is zero (drawdown ladder at a halt step)"], False)

    risk_budget = equity * effective_risk_pct / D("100")
    pip_value_per_lot = instrument.pip_value_quote(D("1")) * dec(quote_to_account)
    if pip_value_per_lot <= 0:
        return SizingResult(ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, stop_pips, ZERO,
                            ["pip value resolves to zero: missing conversion rate"], False)

    intended = risk_budget / (stop_pips * pip_value_per_lot)
    # The cap and the lot step are different constraints and must be reported
    # separately. Measuring the rounding error against the UNCAPPED intent made
    # a deliberately size-capped trade look like a 50% granularity failure and
    # vetoed it with a message about the lot step.
    capped = min(intended, dec(max_lots))
    lots = instrument.round_lots_down(capped)

    # Minimum equity at which this stop can be expressed within tolerance.
    tolerance = dec(max_rounding_error_pct) / D("100")
    min_lots_needed = instrument.lot_step / tolerance
    required_min_equity = (min_lots_needed * pip_value_per_lot * stop_pips
                           * D("100") / effective_risk_pct)

    if lots < instrument.min_lot:
        notes.append(
            f"risk budget {risk_budget:.2f} cannot buy even the minimum "
            f"{instrument.min_lot} lot at a {stop_pips:.1f} pip stop; "
            f"an account of about {required_min_equity:.0f} (account currency) "
            f"is required for this stop distance"
        )
        return SizingResult(ZERO, ZERO, ZERO, intended, D("100"), pip_value_per_lot,
                            stop_pips, required_min_equity, notes, False)

    actual_risk = lots * stop_pips * pip_value_per_lot
    rounding_error_pct = (abs(capped - lots) / capped * D("100")) if capped > 0 else ZERO
    feasible = True
    if rounding_error_pct > dec(max_rounding_error_pct):
        notes.append(
            f"lot granularity forces a {rounding_error_pct:.1f}% deviation from the "
            f"intended size (limit {max_rounding_error_pct}%); the realised risk "
            f"would be {actual_risk / equity * 100:.2f}% not {effective_risk_pct:.2f}%"
        )
        feasible = False
    if capped < intended:
        # Informational, never a veto: a capped trade is a valid smaller trade.
        notes.append(f"size capped at max_lots={max_lots} "
                     f"(risk-based size would have been {intended:.2f} lots)")

    return SizingResult(
        lots=lots, risk_amount=actual_risk,
        risk_pct=(actual_risk / equity * D("100")),
        intended_lots=intended, rounding_error_pct=rounding_error_pct,
        pip_value_account=pip_value_per_lot, stop_pips=stop_pips,
        required_min_equity=required_min_equity, notes=notes, feasible=feasible,
    )


def ladder_rung(drawdown_pct: Decimal, ladder: List[dict], *,
                current_rung: int = 0, hysteresis_pct: Decimal = ZERO) -> int:
    """Which ladder rung applies, with a hysteresis band on the way back up.

    Stepping DOWN happens the moment the drawdown reaches a threshold: that is
    a risk reduction and should never be delayed. Stepping back UP requires the
    drawdown to have recovered ``hysteresis_pct`` BELOW the threshold that put
    us here.

    Without the band, two entries minutes apart on either side of a boundary
    receive different budgets (0.50% and 0.375%, say) with no explanation an
    operator can act on, and any acceptance run straddling a step picks up the
    resulting oscillation as variance.
    """
    if not ladder:
        return 0
    dd = dec(drawdown_pct)
    target = 0
    for i, row in enumerate(ladder, start=1):
        if dd >= dec(row["drawdown_pct"]):
            target = i
    if target >= current_rung:
        return target                      # tighten immediately
    # Loosening: only once clear of the band below the current rung's threshold.
    if current_rung > len(ladder):
        return target
    threshold = dec(ladder[current_rung - 1]["drawdown_pct"])
    if dd <= threshold - dec(hysteresis_pct):
        return target
    return current_rung


def ladder_multiplier(drawdown_pct: Decimal, ladder: List[dict], enabled: bool = True,
                      *, current_rung: int = 0,
                      hysteresis_pct: Decimal = ZERO) -> Decimal:
    """Risk multiplier for the current drawdown.

    The ladder is a *pre-committed* schedule. Evaluating it here, from config
    that was fixed before the run, is what stops "I'll reduce size after this
    one recovers" from becoming policy.
    """
    if not enabled or not ladder:
        return D("1")
    rung = ladder_rung(drawdown_pct, ladder, current_rung=current_rung,
                       hysteresis_pct=hysteresis_pct)
    if rung <= 0:
        return D("1")
    return dec(ladder[min(rung, len(ladder)) - 1]["risk_multiplier"])
