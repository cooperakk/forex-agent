"""The independent risk engine.

Everything else in this system is advisory. This module has the final word.

Design constraints that follow from the research brief:

* It takes a *snapshot* (``RiskContext``) and an *intent*, and returns a
  decision. It does not call strategies, the LLM, or the broker. Nothing it
  depends on can be influenced by the thing it is judging.
* Every rule is named. A veto says which rule fired, with the numbers, so the
  dashboard can show "why not" as clearly as "why".
* Limits are checked against the state *including* the proposed trade, and
  against pending orders, not just filled positions.
* Failure is closed. A missing price, an unknown conversion rate, a stale bar,
  an unresolved order -- each blocks new risk rather than being ignored.

The engine is pure and synchronous, which makes it exhaustively testable; see
``tests/test_risk_engine.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from ..core.config import RiskConfig
from ..core.money import (
    CostModel, D, Instrument, ZERO, annual_cost_pct_of_equity, break_even_win_rate, dec,
)
from ..core.types import AccountState, DataQuality, OrderIntent, Position, Quote, Side
from .exposure import correlation_clusters, currency_exposure, gross_notional
from .sizing import SizingResult, ladder_multiplier, size_position


class Severity(str, Enum):
    BLOCK = "block"      # refuse this action
    HALT = "halt"        # refuse everything until a human intervenes
    WARN = "warn"        # allow, but record


@dataclass(frozen=True)
class Veto:
    rule: str
    message: str
    severity: Severity = Severity.BLOCK
    observed: Optional[str] = None
    limit: Optional[str] = None

    def to_dict(self) -> dict:
        return {"rule": self.rule, "message": self.message, "severity": self.severity.value,
                "observed": self.observed, "limit": self.limit}


@dataclass
class RiskContext:
    """Everything the engine is allowed to know, captured at one instant."""

    now_ns: int
    account: AccountState
    positions: Sequence[Position]
    instruments: Dict[str, Instrument]
    quotes: Dict[str, Quote]
    conversions: Dict[str, Decimal]                 # quote ccy -> account ccy
    equity_peak: Decimal

    day_pnl: Decimal = ZERO
    week_pnl: Decimal = ZERO
    month_pnl: Decimal = ZERO
    week_start_equity: Decimal = ZERO
    month_start_equity: Decimal = ZERO
    rolling_24h_pnl: Decimal = ZERO
    # Latched for the rest of the venue day once the daily profit lock trips.
    day_profit_locked: bool = False
    # Current drawdown-ladder rung, so the hysteresis band has memory.
    ladder_rung: int = 0
    #: Venue-enforced minimum stop distance in PIPS, per instrument. A stop
    #: inside this is not a tight stop; it is an order the broker rejects.
    #: Empty means "the venue imposes none" -- which is genuinely common, and
    #: is why the field is not required.
    venue_min_stop_pips: Dict[str, Decimal] = field(default_factory=dict)
    day_start_equity: Decimal = ZERO

    trades_today: int = 0
    trades_this_week: int = 0
    trades_this_year: int = 0
    last_entry_ns: Optional[int] = None

    pending_risk: Decimal = ZERO                    # risk of orders not yet filled
    unresolved_orders: int = 0

    missing_conversions: List[str] = field(default_factory=list)
    blocking_alarms: List[str] = field(default_factory=list)
    regime_risk_multiplier: Decimal = D("1")
    clock_skew_ms: Optional[float] = None
    connectivity_ok: bool = True
    offline_seconds: float = 0.0
    data_age_sec: Dict[str, float] = field(default_factory=dict)
    data_quality: Dict[str, DataQuality] = field(default_factory=dict)
    normal_spread_pips: Dict[str, Decimal] = field(default_factory=dict)

    news_blackout: Dict[str, str] = field(default_factory=dict)   # instrument -> event
    #: Instruments whose broker price disagrees with an independent reference
    #: (sentinel.data.reference), with the reason. Empty when the reference is
    #: off or unavailable -- its absence never blocks anything.
    price_divergence: Dict[str, str] = field(default_factory=dict)
    correlations: Dict[Tuple[str, str], float] = field(default_factory=dict)
    cost_models: Dict[str, CostModel] = field(default_factory=dict)

    halted: bool = False
    halt_reason: str = ""
    kill_switch: bool = False
    strategy_lifecycles: Dict[str, str] = field(default_factory=dict)
    live_money: bool = False
    projected_trades_per_year: Optional[int] = None
    #: The rest of the owner's engines, from risk/portfolio.GroupLedger. None
    #: when this engine is not in a group.
    group_others_open_risk: Optional[Decimal] = None
    group_others_equity: Decimal = ZERO
    group_others_currency_risk: Dict[str, Decimal] = field(default_factory=dict)
    group_unknown_members: List[str] = field(default_factory=list)

    def conversion(self, quote_ccy: str) -> Optional[Decimal]:
        """Quote -> account rate, or None when it is unknown.

        Returning None rather than 1.0 is the whole point. A JPY-quoted risk
        budget silently converted at 1.0 is ~150x too large, and the sizing
        layer would still report the intended percentage.
        """
        if quote_ccy == self.account.currency:
            return D("1")
        return self.conversions.get(quote_ccy)

    @property
    def drawdown_pct(self) -> Decimal:
        if self.equity_peak <= 0:
            return ZERO
        return max(ZERO, (self.equity_peak - self.account.equity) / self.equity_peak * D("100"))

    @property
    def day_pnl_pct(self) -> Decimal:
        base = self.day_start_equity or self.account.equity
        return (self.day_pnl / base * D("100")) if base > 0 else ZERO


@dataclass
class RiskDecision:
    approved: bool
    vetoes: List[Veto] = field(default_factory=list)
    warnings: List[Veto] = field(default_factory=list)
    sizing: Optional[SizingResult] = None
    risk_multiplier: Decimal = D("1")
    approved_lots: Decimal = ZERO
    risk_amount: Decimal = ZERO
    risk_pct: Decimal = ZERO
    expected_cost_pips: Decimal = ZERO
    break_even_win_rate: Optional[Decimal] = None
    diagnostics: Dict[str, object] = field(default_factory=dict)

    @property
    def halting(self) -> bool:
        return any(v.severity is Severity.HALT for v in self.vetoes)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "vetoes": [v.to_dict() for v in self.vetoes],
            "warnings": [v.to_dict() for v in self.warnings],
            "approved_lots": str(self.approved_lots),
            "risk_amount": str(self.risk_amount),
            "risk_pct": str(self.risk_pct),
            "risk_multiplier": str(self.risk_multiplier),
            "expected_cost_pips": str(self.expected_cost_pips),
            "break_even_win_rate": (str(self.break_even_win_rate)
                                    if self.break_even_win_rate is not None else None),
            "diagnostics": self.diagnostics,
        }


class RiskEngine:
    """Stateless evaluator. All state arrives in the context."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #

    def evaluate_entry(self, intent: OrderIntent, ctx: RiskContext) -> RiskDecision:
        cfg = self.config
        vetoes: List[Veto] = []
        warnings: List[Veto] = []
        diag: Dict[str, object] = {}

        # ---- 0. absolute stops ---------------------------------------- #
        #
        # These run FIRST and unconditionally, because four blocks below return
        # early (unknown instrument, no quote, missing conversion, unsafe
        # sizing). With the drawdown halt evaluated only after them, a
        # malformed intent at a 12% drawdown against a 10% halt threshold came
        # back with `halting=False` -- the caller was told the trade was merely
        # rejected when the whole system should have stopped.
        if ctx.kill_switch:
            vetoes.append(Veto("kill_switch", "kill switch is engaged", Severity.HALT))
        if ctx.halted:
            vetoes.append(Veto("halted", f"agent halted: {ctx.halt_reason}", Severity.HALT))
        if ctx.equity_peak > 0:
            _dd = (ctx.equity_peak - ctx.account.equity) / ctx.equity_peak * D("100")
            if _dd >= cfg.max_drawdown_halt_pct:
                vetoes.append(Veto(
                    "max_drawdown",
                    f"drawdown {_dd:.2f}% has reached the halt threshold",
                    Severity.HALT, observed=f"{_dd:.2f}%",
                    limit=f"{cfg.max_drawdown_halt_pct}%"))
        if ctx.unresolved_orders > 0:
            vetoes.append(Veto(
                "unresolved_orders",
                f"{ctx.unresolved_orders} order(s) in an unknown state; resolve by "
                "reconciliation before opening new risk",
                Severity.BLOCK, observed=str(ctx.unresolved_orders), limit="0"))
        if not ctx.connectivity_ok or ctx.offline_seconds > cfg.max_offline_seconds_before_freeze:
            vetoes.append(Veto(
                "connectivity",
                f"link down or degraded for {ctx.offline_seconds:.0f}s; no new entries "
                "until reconciliation completes",
                Severity.BLOCK, observed=f"{ctx.offline_seconds:.0f}s",
                limit=f"{cfg.max_offline_seconds_before_freeze}s"))

        # ---- 1. lifecycle & venue ------------------------------------- #
        lifecycle = ctx.strategy_lifecycles.get(intent.strategy, "hypothesis")
        if ctx.live_money and lifecycle != "accepted":
            vetoes.append(Veto(
                "lifecycle",
                f"strategy '{intent.strategy}' is '{lifecycle}', not 'accepted'; "
                "real money requires a passed acceptance run",
                Severity.BLOCK, observed=lifecycle, limit="accepted"))

        inst = ctx.instruments.get(intent.instrument)
        if inst is None:
            vetoes.append(Veto("unknown_instrument",
                               f"{intent.instrument} is not in the instrument table"))
            return RiskDecision(False, vetoes, warnings, diagnostics=diag)

        quote = ctx.quotes.get(intent.instrument)
        if quote is None:
            vetoes.append(Veto("no_price", f"no live quote for {intent.instrument}"))
            return RiskDecision(False, vetoes, warnings, diagnostics=diag)

        conv = ctx.conversion(inst.quote)
        if conv is None or conv <= 0:
            vetoes.append(Veto(
                "missing_conversion",
                f"no {inst.quote}->{ctx.account.currency} rate is available, so the "
                "risk of this trade cannot be expressed in the account currency. "
                "Assuming 1.0 here would mis-size the position by the exchange rate "
                "itself",
                observed="unavailable", limit="required"))
            return RiskDecision(False, vetoes, warnings, diagnostics=diag)

        # A standing portfolio alarm at BLOCK severity stops new risk. These are
        # conditions like an unprotected open position -- which must not be
        # allowed to sit alongside a fresh entry.
        for rule in ctx.blocking_alarms:
            vetoes.append(Veto(
                "portfolio_alarm",
                f"a standing portfolio alarm is blocking new risk: {rule}",
                observed=rule))

        # ---- 2. data integrity ---------------------------------------- #
        # An instrument MISSING from a map that has entries has an UNKNOWN
        # age, not a zero one. Defaulting to 0.0 meant "we have no idea how
        # old this price is" was approved with no veto at all, by the control
        # whose entire job is to refuse to trade on a stale price. The
        # orchestrator one layer up already uses inf; this default disagreed
        # with it.
        #
        # An EMPTY map is different and legitimate: the caller is not tracking
        # ages at all, which is how the backtest runs -- every bar is by
        # construction current. Treating that as infinitely stale would veto
        # every entry in every backtest, so the two cases are distinguished
        # rather than collapsed.
        age = (ctx.data_age_sec.get(intent.instrument, float("inf"))
               if ctx.data_age_sec else 0.0)
        if age > cfg.max_data_staleness_sec:
            vetoes.append(Veto("stale_data",
                               f"price for {intent.instrument} is {age:.0f}s old",
                               observed=f"{age:.0f}s", limit=f"{cfg.max_data_staleness_sec}s"))
        quality = ctx.data_quality.get(intent.instrument, DataQuality.OK)
        if quality is not DataQuality.OK:
            vetoes.append(Veto("data_quality",
                               f"data quality for {intent.instrument} is '{quality.value}'",
                               observed=quality.value, limit="ok"))
        if ctx.clock_skew_ms is not None and abs(ctx.clock_skew_ms) > cfg.max_clock_skew_ms:
            vetoes.append(Veto("clock_skew",
                               f"clock skew {ctx.clock_skew_ms:.0f}ms exceeds the ceiling; "
                               "event ordering cannot be trusted",
                               observed=f"{ctx.clock_skew_ms:.0f}ms",
                               limit=f"{cfg.max_clock_skew_ms:.0f}ms"))

        # ---- 3. spread & cost ----------------------------------------- #
        spread = quote.spread_pips(inst)
        normal = dec(ctx.normal_spread_pips.get(intent.instrument, spread))
        diag["spread_pips"] = str(spread)
        if normal > 0 and spread > normal * cfg.max_spread_pips_multiple:
            vetoes.append(Veto("spread",
                               f"spread {spread:.2f}p is {spread / normal:.1f}x normal",
                               observed=f"{spread:.2f}p",
                               limit=f"{normal * cfg.max_spread_pips_multiple:.2f}p"))

        # ---- 4. news blackout ----------------------------------------- #
        if intent.instrument in ctx.news_blackout:
            vetoes.append(Veto("news_blackout",
                               f"scheduled event window: {ctx.news_blackout[intent.instrument]}",
                               observed=ctx.news_blackout[intent.instrument]))

        # ---- 4b. broker price vs an independent reference ------------- #
        if intent.instrument in ctx.price_divergence:
            vetoes.append(Veto("reference_divergence",
                               "the broker's price disagrees with an independent "
                               f"reference: {ctx.price_divergence[intent.instrument]}",
                               observed=ctx.price_divergence[intent.instrument][:120]))

        # ---- 5. stop discipline --------------------------------------- #
        if intent.stop_loss is None:
            vetoes.append(Veto("no_stop", "entry without a stop loss is refused"))
            return RiskDecision(False, vetoes, warnings, diagnostics=diag)
        entry_ref = intent.limit_price or quote.price_for(intent.side)
        stop_pips = abs(entry_ref - intent.stop_loss) / inst.pip
        diag["stop_pips"] = str(stop_pips)
        if stop_pips < cfg.min_stop_pips:
            vetoes.append(Veto("stop_too_tight",
                               f"stop of {stop_pips:.1f}p is below the {cfg.min_stop_pips}p floor; "
                               "at that distance the cost barrier makes positive expectancy "
                               "implausible",
                               observed=f"{stop_pips:.1f}p", limit=f"{cfg.min_stop_pips}p"))

        # The VENUE's own floor, which is separate from ours and frequently
        # higher. A stop inside the broker's minimum distance is rejected
        # outright, so discovering it after sizing means the risk engine has
        # computed a position from a stop that cannot exist -- and the order
        # comes back "Invalid stops" with no indication that the geometry, not
        # the software, is wrong.
        venue_floor = ctx.venue_min_stop_pips.get(intent.instrument, ZERO)
        if venue_floor > 0 and stop_pips < venue_floor:
            vetoes.append(Veto(
                "venue_stop_distance",
                f"stop of {stop_pips:.1f}p is inside this broker's minimum of "
                f"{venue_floor:.1f}p; the venue would reject the order",
                observed=f"{stop_pips:.1f}p", limit=f"{venue_floor:.1f}p"))
        if stop_pips > cfg.max_stop_pips:
            vetoes.append(Veto("stop_too_wide", f"stop of {stop_pips:.1f}p exceeds the ceiling",
                               observed=f"{stop_pips:.1f}p", limit=f"{cfg.max_stop_pips}p"))
        # direction sanity: a stop on the wrong side is a catastrophic bug
        if intent.side is Side.BUY and intent.stop_loss >= entry_ref:
            vetoes.append(Veto("stop_side", "BUY stop is at or above the entry price"))
        if intent.side is Side.SELL and intent.stop_loss <= entry_ref:
            vetoes.append(Veto("stop_side", "SELL stop is at or below the entry price"))

        if intent.take_profit is not None and stop_pips > 0:
            target_pips = abs(intent.take_profit - entry_ref) / inst.pip
            rr = target_pips / stop_pips
            diag["reward_risk"] = f"{rr:.2f}"
            if rr < cfg.min_reward_risk:
                vetoes.append(Veto("reward_risk",
                                   f"reward/risk {rr:.2f} is below the {cfg.min_reward_risk} floor",
                                   observed=f"{rr:.2f}", limit=str(cfg.min_reward_risk)))

        # ---- 6. cost feasibility (brief section B-3) ------------------- #
        cost_model = ctx.cost_models.get(intent.instrument, CostModel(spread_pips=spread))
        pip_val_per_lot = inst.pip_value_quote(D("1")) * conv
        try:
            cost_pips = cost_model.round_trip_pips(
                inst, D("1"), pip_val_per_lot,
                during_news=intent.instrument in ctx.news_blackout)
        except ValueError:
            cost_pips = spread
        diag["round_trip_cost_pips"] = str(cost_pips)
        be = None
        if intent.take_profit is not None and stop_pips > 0:
            target_pips = abs(intent.take_profit - entry_ref) / inst.pip
            be = break_even_win_rate(target_pips, stop_pips, cost_pips)
            diag["break_even_win_rate"] = f"{be:.4f}"
            if be >= D("0.65"):
                vetoes.append(Veto(
                    "cost_barrier",
                    f"this target/stop needs a {be * 100:.1f}% win rate merely to break even "
                    f"after {cost_pips:.2f}p of cost; widen the target or lengthen the horizon",
                    observed=f"{be * 100:.1f}%", limit="65%"))
            elif be >= D("0.58"):
                warnings.append(Veto("cost_barrier", f"break-even win rate is {be * 100:.1f}%",
                                     Severity.WARN, observed=f"{be * 100:.1f}%"))

        # ---- 7. loss budgets & drawdown -------------------------------- #
        dd = ctx.drawdown_pct
        diag["drawdown_pct"] = f"{dd:.2f}"
        # (the halt itself was already raised in section 0, before any early
        # return could skip it -- do not raise it twice)
        day_loss_pct = -ctx.day_pnl_pct
        if day_loss_pct >= cfg.daily_loss_limit_pct:
            vetoes.append(Veto("daily_loss", f"daily loss {day_loss_pct:.2f}% reached the limit",
                               observed=f"{day_loss_pct:.2f}%", limit=f"{cfg.daily_loss_limit_pct}%"))
        base_eq = ctx.account.equity or D("1")
        # Every period is measured against the equity at the START of that
        # period, exactly like the day. Measuring the week against CURRENT
        # equity gave the three limits different denominators while the config
        # cross-validates them as though they measured the same thing.
        week_base = ctx.week_start_equity or ctx.account.equity or D("1")
        week_loss_pct = -(ctx.week_pnl / week_base * D("100"))
        if week_loss_pct >= cfg.weekly_loss_limit_pct:
            vetoes.append(Veto("weekly_loss", f"weekly loss {week_loss_pct:.2f}% reached the limit",
                               observed=f"{week_loss_pct:.2f}%",
                               limit=f"{cfg.weekly_loss_limit_pct}%"))
        month_base = ctx.month_start_equity or ctx.account.equity or D("1")
        month_loss_pct = -(ctx.month_pnl / month_base * D("100"))
        if month_loss_pct >= cfg.monthly_loss_limit_pct:
            vetoes.append(Veto("monthly_loss",
                               f"monthly loss {month_loss_pct:.2f}% reached the limit",
                               observed=f"{month_loss_pct:.2f}%",
                               limit=f"{cfg.monthly_loss_limit_pct}%"))

        # Rolling 24h, beside the calendar day. The calendar boundary is what
        # the broker statement agrees with; this is what actually bounds the
        # loss, because a drawdown straddling the boundary would otherwise be
        # granted two full daily budgets an hour apart.
        if cfg.rolling_24h_loss_limit_pct > 0 and ctx.rolling_24h_pnl < 0:
            roll_base = ctx.account.equity - ctx.rolling_24h_pnl
            if roll_base > 0:
                roll_loss_pct = -(ctx.rolling_24h_pnl / roll_base * D("100"))
                if roll_loss_pct >= cfg.rolling_24h_loss_limit_pct:
                    vetoes.append(Veto(
                        "rolling_24h_loss",
                        f"loss over the last 24h is {roll_loss_pct:.2f}%",
                        observed=f"{roll_loss_pct:.2f}%",
                        limit=f"{cfg.rolling_24h_loss_limit_pct}%"))

        # The profit lock LATCHES. As a bare level test it released itself the
        # moment the day's gain fell back below the threshold, so the agent
        # simply resumed trading into the give-back it was meant to prevent.
        if cfg.daily_profit_lock_pct > 0 and (
                ctx.day_profit_locked or ctx.day_pnl_pct >= cfg.daily_profit_lock_pct):
            vetoes.append(Veto("profit_lock",
                               f"day is +{ctx.day_pnl_pct:.2f}%; new entries are paused for "
                               "the rest of the venue day to protect the gain",
                               observed=f"+{ctx.day_pnl_pct:.2f}%",
                               limit=f"{cfg.daily_profit_lock_pct}%"))

        # ---- 8. frequency (a risk limit, not a preference) ------------- #
        if ctx.trades_today >= cfg.max_trades_per_day:
            vetoes.append(Veto("frequency_day", f"{ctx.trades_today} trades today",
                               observed=str(ctx.trades_today), limit=str(cfg.max_trades_per_day)))
        if ctx.trades_this_week >= cfg.max_trades_per_week:
            vetoes.append(Veto("frequency_week", f"{ctx.trades_this_week} trades this week",
                               observed=str(ctx.trades_this_week),
                               limit=str(cfg.max_trades_per_week)))
        if ctx.trades_this_year >= cfg.max_trades_per_year:
            vetoes.append(Veto("frequency_year", f"{ctx.trades_this_year} trades this year",
                               observed=str(ctx.trades_this_year),
                               limit=str(cfg.max_trades_per_year)))
        if ctx.last_entry_ns is not None:
            gap = (ctx.now_ns - ctx.last_entry_ns) / 1e9
            if gap < cfg.min_seconds_between_entries:
                vetoes.append(Veto("entry_spacing",
                                   f"only {gap:.0f}s since the last entry",
                                   observed=f"{gap:.0f}s",
                                   limit=f"{cfg.min_seconds_between_entries}s"))

        # projected annual cost
        projected = ctx.projected_trades_per_year
        if projected is None and ctx.trades_this_year > 0:
            dt = datetime.fromtimestamp(ctx.now_ns / 1e9, tz=timezone.utc)
            elapsed = max(1, dt.timetuple().tm_yday)
            projected = int(ctx.trades_this_year * 365 / elapsed)
        if projected:
            notional = inst.units(D("1")) * quote.mid * conv
            cost_bp = ((cost_pips * inst.pip * inst.units(D("1")) * conv) / notional
                       * D("10000")) if notional > 0 else ZERO
            # account_currency was omitted here and supplied at the other call
            # site, so this one worked only because ctx.conversions happens to
            # map the account currency to 1.
            # `unconvertible` is collected here too. Omitting it silently
            # DROPPED an unpriceable leg from the gross, understating leverage
            # (measured at 50% on a mixed EUR_USD/EUR_GBP book) and therefore
            # understating the projected annual cost -- making the cost veto
            # more permissive in exactly the situation where the numbers are
            # least trustworthy.
            cost_unconvertible: List[str] = []
            leverage = (gross_notional(list(ctx.positions), ctx.instruments,
                                       {k: v.mid for k, v in ctx.quotes.items()},
                                       ctx.conversions,
                                       account_currency=ctx.account.currency,
                                       unconvertible=cost_unconvertible)
                        / base_eq) if base_eq > 0 else ZERO
            if cost_unconvertible:
                diag["annual_cost_note"] = (
                    "leverage excludes legs with no conversion rate: "
                    + ", ".join(sorted(set(cost_unconvertible))))
            leverage = max(leverage, D("1"))
            annual = annual_cost_pct_of_equity(round_trip_cost_bp=cost_bp, leverage=leverage,
                                               trades_per_year=projected)
            diag["projected_annual_cost_pct"] = f"{annual:.2f}"
            diag["projected_trades_per_year"] = projected
            if annual > cfg.max_annual_cost_pct_of_equity:
                vetoes.append(Veto(
                    "annual_cost",
                    f"at this pace the yearly cost bill is {annual:.1f}% of equity",
                    observed=f"{annual:.1f}%",
                    limit=f"{cfg.max_annual_cost_pct_of_equity}%"))

        # ---- 9. session windows ---------------------------------------- #
        # (Session/day gating lives in the agent; the engine only records it.)

        # ---- 10. sizing ------------------------------------------------ #
        ladder = ladder_multiplier(dd, cfg.ladder, cfg.ladder_enabled,
                                   current_rung=ctx.ladder_rung,
                                   hysteresis_pct=cfg.ladder_hysteresis_pct)
        regime_mult = max(D("0"), min(D("1"), dec(ctx.regime_risk_multiplier)))
        # Both can only shrink the budget, and they compose. A stress regime with
        # a drawdown already in progress gets both reductions, not the larger one.
        multiplier = ladder * regime_mult
        diag["ladder_multiplier"] = str(ladder)
        diag["regime_multiplier"] = str(regime_mult)
        diag["risk_multiplier"] = str(multiplier)
        if multiplier <= 0:
            vetoes.append(Veto("ladder_halt",
                               f"drawdown ladder has reduced the risk budget to zero at "
                               f"{dd:.2f}% drawdown",
                               observed=f"{dd:.2f}%"))

        sizing = size_position(
            equity=ctx.account.equity, risk_pct=cfg.risk_per_trade_pct,
            entry_price=entry_ref, stop_price=intent.stop_loss, instrument=inst,
            quote_to_account=conv, risk_multiplier=multiplier,
            max_lots=cfg.max_lots_per_trade,
        )
        if not sizing.feasible:
            vetoes.append(Veto("sizing", "; ".join(sizing.notes) or "position is not sizeable",
                               observed=str(sizing.lots)))
        for note in sizing.notes:
            if sizing.feasible:
                warnings.append(Veto("sizing", note, Severity.WARN))

        # ---- 11. portfolio limits (including the proposed trade) ------- #
        open_count = len(ctx.positions)
        if open_count >= cfg.max_open_positions:
            vetoes.append(Veto("max_positions", f"{open_count} positions already open",
                               observed=str(open_count), limit=str(cfg.max_open_positions)))
        same_inst = sum(1 for p in ctx.positions if p.instrument == intent.instrument)
        if same_inst >= cfg.max_positions_per_instrument:
            vetoes.append(Veto("per_instrument",
                               f"{same_inst} position(s) already open on {intent.instrument}",
                               observed=str(same_inst),
                               limit=str(cfg.max_positions_per_instrument)))

        mids = {k: v.mid for k, v in ctx.quotes.items()}
        prospective = list(ctx.positions) + [Position(
            instrument=intent.instrument, side=intent.side, lots=sizing.lots,
            entry_price=entry_ref, opened_ns=ctx.now_ns, strategy=intent.strategy,
            initial_risk=sizing.risk_amount)]

        gross_unconvertible: List[str] = []
        gross = gross_notional(prospective, ctx.instruments, mids, ctx.conversions,
                               account_currency=ctx.account.currency,
                               unconvertible=gross_unconvertible)
        if gross_unconvertible:
            vetoes.append(Veto(
                "exposure_unconvertible",
                f"gross notional cannot be expressed in {ctx.account.currency}: no rate "
                f"for {', '.join(sorted(set(gross_unconvertible)))}",
                observed="unconvertible", limit="required"))
        leverage = (gross / base_eq) if base_eq > 0 else ZERO
        diag["gross_leverage"] = f"{leverage:.2f}"
        if leverage > cfg.max_gross_leverage:
            vetoes.append(Veto("leverage", f"gross leverage would reach {leverage:.2f}x",
                               observed=f"{leverage:.2f}x", limit=f"{cfg.max_gross_leverage}x"))

        # SUM by instrument rather than overwrite. A dict comprehension keeps
        # only the last position on each symbol, so with
        # max_positions_per_instrument > 1 two positions on one pair contributed
        # one position's risk to the correlated-risk ceiling.
        risk_map: Dict[str, Decimal] = {}
        for p in prospective:
            if p.instrument == intent.instrument and p.entry_price == entry_ref:
                continue        # the prospective leg is added explicitly below
            risk_map[p.instrument] = risk_map.get(p.instrument, ZERO) + (p.initial_risk or ZERO)
        risk_map[intent.instrument] = (risk_map.get(intent.instrument, ZERO)
                                       + sizing.risk_amount)
        unconvertible: List[str] = []
        exposures = currency_exposure(prospective, ctx.instruments,
                                      risk_by_instrument=risk_map,
                                      conversions=ctx.conversions, mid_prices=mids,
                                      account_currency=ctx.account.currency,
                                      unconvertible=unconvertible)
        if unconvertible:
            vetoes.append(Veto(
                "exposure_unconvertible",
                f"the currency exposure of {', '.join(sorted(set(unconvertible)))} "
                "cannot be expressed in the account currency, so the portfolio "
                "limits cannot be evaluated",
                observed=", ".join(sorted(set(unconvertible)))))
        worst_ccy, worst_pct = None, ZERO
        for ccy, e in exposures.items():
            pct = abs(e.net_risk) / base_eq * D("100") if base_eq > 0 else ZERO
            if pct > worst_pct:
                worst_ccy, worst_pct = ccy, pct
            if pct > cfg.max_currency_exposure_pct:
                vetoes.append(Veto(
                    "currency_exposure",
                    f"net {ccy} risk would be {pct:.2f}% of equity across "
                    f"{', '.join(e.contributors)} -- these are one bet, not several",
                    observed=f"{pct:.2f}%", limit=f"{cfg.max_currency_exposure_pct}%"))
        diag["max_currency_exposure"] = {"currency": worst_ccy, "pct": f"{worst_pct:.2f}"}

        clusters = correlation_clusters(
            [p.instrument for p in prospective], risk_map, ctx.correlations,
            cfg.correlation_threshold)
        for cluster in clusters:
            if len(cluster.instruments) < 2:
                continue
            pct = cluster.total_risk / base_eq * D("100") if base_eq > 0 else ZERO
            if pct > cfg.max_correlated_risk_pct:
                vetoes.append(Veto(
                    "correlated_risk",
                    f"correlated cluster {cluster.instruments} (max |rho|="
                    f"{cluster.max_pairwise_corr:.2f}) would carry {pct:.2f}% of equity",
                    observed=f"{pct:.2f}%", limit=f"{cfg.max_correlated_risk_pct}%"))
        diag["clusters"] = [{"instruments": c.instruments, "risk": str(c.total_risk),
                             "max_corr": round(c.max_pairwise_corr, 3)} for c in clusters]

        # margin headroom -- with a deliberate buffer, because the margin that
        # matters is the margin *after* an adverse move, not at entry.
        required_margin = (inst.units(sizing.lots) * quote.mid * inst.margin_rate * conv)
        if required_margin > ctx.account.margin_available * D("0.5"):
            vetoes.append(Veto(
                "margin",
                f"the position needs {required_margin:.2f} margin against "
                f"{ctx.account.margin_available:.2f} available; the engine keeps a 2x buffer",
                observed=f"{required_margin:.2f}",
                limit=f"{ctx.account.margin_available * D('0.5'):.2f}"))

        # Total committed risk: open positions + this trade + anything still in
        # flight. Previously computed, formatted into the diagnostics, and never
        # compared to anything.
        # A position whose risk we do not know is NOT a position with no risk.
        # Live adapters cannot report initial_risk, so if the agent's own
        # metadata is lost the book reads as almost risk-free: in one measured
        # case the engine saw 0.49% committed against a real 2.50%, and
        # approved a sixth entry on the strength of it. Charge an unknown
        # position the full per-trade budget until it is identified.
        unknown_risk_positions = [p for p in ctx.positions
                                  if not p.initial_risk or p.initial_risk <= 0]
        assumed = (base_eq * cfg.risk_per_trade_pct / D("100")
                   * D(len(unknown_risk_positions)))
        if unknown_risk_positions:
            diag["positions_with_unknown_risk"] = ",".join(
                p.instrument for p in unknown_risk_positions)
            diag["assumed_risk_for_unknown"] = str(assumed)
            warnings.append(Veto(
                "unknown_position_risk",
                f"{len(unknown_risk_positions)} open position(s) have no recorded risk; "
                f"each is charged the full {cfg.risk_per_trade_pct}% budget until "
                "reconciliation identifies them",
                Severity.WARN,
                observed=str(len(unknown_risk_positions)), limit="0"))
        open_risk = sum((p.initial_risk or ZERO for p in ctx.positions), ZERO) + assumed
        total_risk = open_risk + sizing.risk_amount + ctx.pending_risk
        total_risk_pct = total_risk / base_eq * D("100") if base_eq > 0 else ZERO
        diag["total_risk_pct_incl_pending"] = f"{total_risk_pct:.3f}"
        if total_risk_pct > cfg.max_total_open_risk_pct:
            vetoes.append(Veto(
                "total_open_risk",
                f"committed risk would reach {total_risk_pct:.2f}% of equity across "
                f"{len(ctx.positions)} open position(s) plus orders in flight",
                observed=f"{total_risk_pct:.2f}%",
                limit=f"{cfg.max_total_open_risk_pct}%"))

        # ---- 11b. the owner's other accounts ----------------------------- #
        # Each engine bounds its own book; this bounds the group. The total
        # is this engine's committed risk plus every fresh member's, over the
        # group's equity. A member whose row is stale or unreadable is
        # UNKNOWN risk, and unknown risk blocks -- an engine that cannot see
        # its sibling cannot claim to have bounded the owner.
        if ctx.group_others_open_risk is not None:
            if ctx.group_unknown_members:
                vetoes.append(Veto(
                    "group_visibility",
                    "cannot see the risk of " + ", ".join(ctx.group_unknown_members)
                    + " (ledger row stale or unreadable); the group total is unknown",
                    observed=", ".join(ctx.group_unknown_members), limit="all visible"))
            group_equity = base_eq + ctx.group_others_equity
            group_risk = total_risk + ctx.group_others_open_risk
            group_pct = group_risk / group_equity * D("100") if group_equity > 0 else ZERO
            diag["group_total_risk_pct"] = f"{group_pct:.3f}"
            if group_pct > cfg.max_group_open_risk_pct:
                vetoes.append(Veto(
                    "group_total_risk",
                    f"committed risk across the owner's accounts would reach "
                    f"{group_pct:.2f}% of {group_equity:.0f} group equity",
                    observed=f"{group_pct:.2f}%", limit=f"{cfg.max_group_open_risk_pct}%"))
            for ccy, e in exposures.items():
                combined = e.net_risk + ctx.group_others_currency_risk.get(ccy, ZERO)
                pct = abs(combined) / group_equity * D("100") if group_equity > 0 else ZERO
                if pct > cfg.max_group_currency_exposure_pct:
                    vetoes.append(Veto(
                        "group_currency_exposure",
                        f"net {ccy} risk across the owner's accounts would be {pct:.2f}% "
                        "of group equity -- the same bet in two books",
                        observed=f"{pct:.2f}%",
                        limit=f"{cfg.max_group_currency_exposure_pct}%"))

        # ---- 12. every open position must already be protected ---------- #
        # The `no_stop` check above covers this intent. This covers the book:
        # opening new risk beside a position whose stop is not confirmed at the
        # venue compounds an exposure that cannot be closed by a dropped link.
        if cfg.require_broker_side_stop:
            naked = [p.instrument for p in ctx.positions if not p.broker_stop_confirmed]
            if naked:
                vetoes.append(Veto(
                    "unprotected_book",
                    f"these open positions have no venue-confirmed stop: "
                    f"{', '.join(naked)}; no new risk until they are protected",
                    observed=", ".join(naked)))

        approved = not vetoes
        return RiskDecision(
            approved=approved, vetoes=vetoes, warnings=warnings, sizing=sizing,
            risk_multiplier=multiplier,
            approved_lots=sizing.lots if approved else ZERO,
            risk_amount=sizing.risk_amount if approved else ZERO,
            risk_pct=sizing.risk_pct if approved else ZERO,
            expected_cost_pips=cost_pips, break_even_win_rate=be, diagnostics=diag,
        )

    # ------------------------------------------------------------------ #

    def evaluate_exit(self, position: Position, ctx: RiskContext) -> RiskDecision:
        """Closing risk is never blocked.

        Deliberate asymmetry: the engine can refuse to *open* for a dozen
        reasons and refuses to *close* for none. A control that can trap you in
        a position is not a risk control.
        """
        return RiskDecision(approved=True, approved_lots=position.lots,
                            diagnostics={"note": "exits are always permitted"})

    def portfolio_alarms(self, ctx: RiskContext) -> List[Veto]:
        """Standing checks independent of any proposed trade."""
        cfg = self.config
        out: List[Veto] = []
        dd = ctx.drawdown_pct
        if dd >= cfg.max_drawdown_halt_pct:
            out.append(Veto("max_drawdown", f"drawdown {dd:.2f}% -- halt", Severity.HALT,
                            observed=f"{dd:.2f}%", limit=f"{cfg.max_drawdown_halt_pct}%"))
        level = ctx.account.margin_level_pct
        if level is not None and level < D("200"):
            out.append(Veto("margin_level", f"margin level {level:.0f}%",
                            Severity.WARN if level >= D("150") else Severity.BLOCK,
                            observed=f"{level:.0f}%", limit="200%"))
        for pos in ctx.positions:
            if cfg.require_broker_side_stop and not pos.broker_stop_confirmed:
                out.append(Veto(
                    "unprotected_position",
                    f"{pos.instrument} has no confirmed venue-side stop",
                    Severity.BLOCK, observed=pos.instrument))
        if ctx.clock_skew_ms is not None and abs(ctx.clock_skew_ms) > cfg.max_clock_skew_ms:
            out.append(Veto("clock_skew", f"clock skew {ctx.clock_skew_ms:.0f}ms",
                            Severity.BLOCK, observed=f"{ctx.clock_skew_ms:.0f}ms"))
        return out
