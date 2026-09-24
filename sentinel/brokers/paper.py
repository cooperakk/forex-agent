"""Adversarial paper broker.

This is not a toy fill engine. A simulator that fills every order at the mid
price manufactures profitable strategies that die on contact with a real
venue, so this one is deliberately pessimistic and models the frictions that
actually decide retail outcomes:

* **Session-dependent spread.** The Asian session is 2-4x the London/NY
  spread on the majors; a strategy that only works on the headline spread is
  shown as failing here rather than in production.
* **Scheduled-event blow-out.** Spread multiplies around flagged releases.
* **Adverse-biased slippage.** Market orders slip against you more often than
  for you, which is what a real book does to a taker.
* **Last look.** A configurable fraction of requests is rejected, and the
  rejection is *conditional on the price having moved in the client's favour*
  -- the asymmetry that makes last look a free option for the liquidity
  provider. Symmetric random rejection would understate its cost.
* **Partial fills** above a size threshold.
* **Financing** charged at the daily rollover with the triple-swap weekday.
* **Margin close-out** at the venue's stop-out level.
* **Weekend gaps**: stops fill at the gapped open, not at the stop price --
  the reason a stop is a *request*, not a guarantee.

Every parameter is explicit and recorded, so a backtest can be re-run under a
doubled-cost / doubled-latency stress (research brief, section D-2).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from ..core.clock import wall_ns
from ..core.errors import BrokerError, ConversionMissingError, PermanentError
from ..core.money import D, Instrument, ZERO, dec
from ..core.types import (
    AccountState,
    ClosedTrade,
    Fill,
    Order,
    OrderIntent,
    OrderState,
    OrderType,
    Position,
    Quote,
    Side,
)
from .base import Broker, BrokerCapabilities, SubmitResult


@dataclass
class SimProfile:
    """Friction parameters. Defaults are a realistic ECN retail account."""

    base_spread_pips: Dict[str, Decimal] = field(default_factory=dict)
    default_spread_pips: Decimal = D("0.6")
    commission_per_lot_round_turn: Decimal = D("7.0")
    # Session multipliers keyed by UTC hour bucket.
    asia_spread_multiple: Decimal = D("2.6")     # 21:00-06:00 UTC
    london_spread_multiple: Decimal = D("1.0")   # 07:00-16:00 UTC
    rollover_spread_multiple: Decimal = D("6.0")  # 20:45-21:15 UTC
    news_spread_multiple: Decimal = D("5.0")
    # Execution
    latency_ms_median: float = 85.0
    latency_ms_tail: float = 420.0
    latency_tail_prob: float = 0.07
    slippage_pips_mean: Decimal = D("0.15")
    slippage_pips_sigma: Decimal = D("0.35")
    adverse_slippage_prob: float = 0.68          # taker disadvantage
    last_look_reject_prob: float = 0.015
    last_look_favourable_bias: float = 3.5       # rejects are 3.5x likelier when
                                                 # the move favoured the client
    partial_fill_threshold_lots: Decimal = D("2.0")
    partial_fill_ratio: Decimal = D("0.6")
    # Carry / margin
    swap_long_pips_per_day: Dict[str, Decimal] = field(default_factory=dict)
    swap_short_pips_per_day: Dict[str, Decimal] = field(default_factory=dict)
    triple_swap_weekday: int = 2                 # Wednesday
    stop_out_margin_level_pct: Decimal = D("50")
    margin_call_level_pct: Decimal = D("100")
    negative_balance_protection: bool = True
    # Stress knobs used by the acceptance protocol
    cost_multiplier: Decimal = D("1.0")
    latency_multiplier: float = 1.0

    def stressed(self, cost_x: float = 2.0, latency_x: float = 2.0) -> "SimProfile":
        import copy

        p = copy.deepcopy(self)
        p.cost_multiplier = self.cost_multiplier * dec(cost_x)
        p.latency_multiplier = self.latency_multiplier * latency_x
        return p


class PaperBroker(Broker):
    """Deterministic given a seed; adversarial by construction."""

    def __init__(
        self,
        *,
        instruments: Dict[str, Instrument],
        starting_balance: Decimal = D("10000"),
        account_currency: str = "USD",
        profile: Optional[SimProfile] = None,
        seed: int = 7,
        account_id: str = "PAPER-001",
        start_ns: Optional[int] = None,
    ) -> None:
        self._instruments = dict(instruments)
        self._profile = profile or SimProfile()
        self._rng = random.Random(seed)
        self._ccy = account_currency
        self._account_id = account_id

        self._balance = dec(starting_balance)
        self._quotes: Dict[str, Quote] = {}
        self._conversions: Dict[str, Decimal] = {f"{account_currency}_{account_currency}": D("1")}
        self._positions: Dict[str, Position] = {}
        self._orders: Dict[str, Order] = {}
        self._closed: List[ClosedTrade] = []
        self._transactions: List[Dict[str, Any]] = []
        self._txn_seq = 0
        self._now_ns = int(start_ns) if start_ns is not None else wall_ns()
        self._news_window = False
        self._last_rollover_day: Optional[int] = None
        self._peak_equity = self._balance
        self._trade_seq = 0

        self.capabilities = BrokerCapabilities(
            supports_client_order_id=True,
            supports_server_side_stop=True,
            supports_transaction_stream=True,
            supports_partial_close=True,
            supports_fractional_lots=True,
            min_lot=D("0.01"),
            lot_step=D("0.01"),
            name="paper",
            notes="Internal adversarial simulator. No counterparty risk, and "
                  "therefore no evidence about counterparty risk.",
        )

    # ------------------------------------------------------------------ #
    # Simulation control
    # ------------------------------------------------------------------ #

    @property
    def now_ns(self) -> int:
        return self._now_ns

    def set_time(self, ns: int) -> None:
        if ns < self._now_ns:
            raise ValueError("simulated time cannot move backwards")
        self._now_ns = ns

    def set_news_window(self, active: bool) -> None:
        self._news_window = active

    def set_conversion(self, quote_ccy: str, rate: Decimal) -> None:
        self._conversions[f"{quote_ccy}_{self._ccy}"] = dec(rate)

    def on_quote(self, quote: Quote) -> None:
        """Feed a new price. Triggers stop/target/margin evaluation."""
        self._quotes[quote.instrument] = quote
        if quote.ts_ns > self._now_ns:
            self._now_ns = quote.ts_ns
        inst = self._instruments.get(quote.instrument)
        if inst and inst.quote == self._ccy:
            self._conversions[f"{inst.quote}_{self._ccy}"] = D("1")
        self._apply_rollover()
        self._check_exits(quote)
        self._check_margin()

    def mark_mid(self, instrument: str, mid: Decimal, ts_ns: int) -> Quote:
        """Feed one mid price, spread out by the session's own spread.

        The entry point for a driver that produces a price path in wall-clock
        time (``data.synthetic_live``). It goes through ``on_quote`` so every
        stop, target, rollover and margin check runs exactly as it does for a
        replayed bar.
        """
        inst = self._instruments[instrument]
        half = self._spread_price(inst, ts_ns) / D("2")
        q = Quote(instrument=instrument, bid=inst.round_price(dec(mid) - half),
                  ask=inst.round_price(dec(mid) + half), ts_ns=ts_ns,
                  received_ns=ts_ns, source="sim")
        self.on_quote(q)
        return q

    def on_bar_prices(self, instrument: str, o: Decimal, h: Decimal, l: Decimal,
                      c: Decimal, start_ns: int, end_ns: int) -> None:
        """Replay one bar as a conservative O -> adverse extreme -> other -> C path.

        The order depends on the open position's DIRECTION: a long visits the low
        first, a short visits the high first. Hard-coding O->L->H->C -- as this
        did originally -- means that on any bar which reaches both barriers,
        every long is scored as a stop and every short as a win. That single line
        biases every short in every backtest, paper run and acceptance run in
        favour of the strategy, which is precisely the kind of flattering error
        the whole acceptance protocol exists to prevent.

        With no position open the low is visited first, matching the long case;
        entries in this harness are taken at the bar close, so the choice only
        affects an already-open position.
        """
        inst = self._instruments[instrument]
        pos = self._positions.get(instrument)
        adverse_first_is_low = pos is None or pos.side is Side.BUY
        first, second = (l, h) if adverse_first_is_low else (h, l)
        third = start_ns + (end_ns - start_ns) // 3
        two_thirds = start_ns + 2 * (end_ns - start_ns) // 3
        for price, ts in ((o, start_ns), (first, third), (second, two_thirds), (c, end_ns)):
            half = self._spread_price(inst, ts) / D("2")
            self.on_quote(
                Quote(instrument=instrument, bid=inst.round_price(price - half),
                      ask=inst.round_price(price + half), ts_ns=ts,
                      received_ns=ts, source="sim")
            )

    # ------------------------------------------------------------------ #
    # Friction model
    # ------------------------------------------------------------------ #

    def _session_multiple(self, ts_ns: int) -> Decimal:
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
        hm = dt.hour * 60 + dt.minute
        if 20 * 60 + 45 <= hm <= 21 * 60 + 15:
            return self._profile.rollover_spread_multiple
        if 7 * 60 <= hm < 16 * 60:
            return self._profile.london_spread_multiple
        if hm >= 21 * 60 or hm < 6 * 60:
            return self._profile.asia_spread_multiple
        return D("1.4")  # the thin hours either side of the main sessions

    def _spread_pips(self, inst: Instrument, ts_ns: int) -> Decimal:
        base = self._profile.base_spread_pips.get(inst.symbol, self._profile.default_spread_pips)
        mult = self._session_multiple(ts_ns) * self._profile.cost_multiplier
        if self._news_window:
            mult *= self._profile.news_spread_multiple
        return base * mult

    def _spread_price(self, inst: Instrument, ts_ns: int) -> Decimal:
        return self._spread_pips(inst, ts_ns) * inst.pip

    def _latency_ms(self) -> float:
        p = self._profile
        base = (p.latency_ms_tail if self._rng.random() < p.latency_tail_prob
                else p.latency_ms_median * self._rng.uniform(0.6, 1.6))
        return base * p.latency_multiplier

    def _slippage_pips(self, side: Side, latency_ms: Optional[float] = None) -> Decimal:
        """Fill slippage, in pips, positive when adverse.

        Latency moves the price. A quote read at t and filled at t + L is
        filled against a market that has diffused for L, so the expected
        distance grows with the square root of the delay. Without this term
        the latency stress in the acceptance protocol changed only whether an
        order timed out: doubling it left the equity curve byte-identical on
        a 38-trade run, and a gate labelled "2x latency stress" measured
        nothing. The scale is relative to the profile's median, so the median
        latency is the calibration point and the tail and any stress multiple
        push slippage out from there.
        """
        p = self._profile
        magnitude = abs(self._rng.gauss(float(p.slippage_pips_mean), float(p.slippage_pips_sigma)))
        if latency_ms is not None and p.latency_ms_median > 0:
            magnitude *= max(1.0, latency_ms / p.latency_ms_median) ** 0.5
        adverse = self._rng.random() < p.adverse_slippage_prob
        signed = magnitude if adverse else -magnitude
        # Positive => worse for the client, in both directions.
        return dec(round(signed, 5))

    def _last_look_rejects(self, favourable_move: bool) -> bool:
        p = self._profile.last_look_reject_prob
        if favourable_move:
            p = min(0.95, p * self._profile.last_look_favourable_bias)
        return self._rng.random() < p

    # ------------------------------------------------------------------ #
    # Broker interface
    # ------------------------------------------------------------------ #

    def instruments(self) -> Dict[str, Instrument]:
        return dict(self._instruments)

    def quote(self, symbol: str) -> Quote:
        q = self._quotes.get(symbol)
        if q is None:
            raise BrokerError(f"no quote for {symbol}", code="NO_QUOTE")
        return q

    def conversion_rate(self, quote_ccy: str, account_ccy: str) -> Decimal:
        if quote_ccy == account_ccy:
            return D("1")
        key = f"{quote_ccy}_{account_ccy}"
        if key in self._conversions:
            return self._conversions[key]
        inverse = f"{account_ccy}_{quote_ccy}"
        if inverse in self._conversions and self._conversions[inverse] != 0:
            return D("1") / self._conversions[inverse]
        raise ConversionMissingError(
            f"no {quote_ccy}->{account_ccy} rate available",
            quote_ccy=quote_ccy, account_ccy=account_ccy,
        )

    def _conv(self, inst: Instrument) -> Decimal:
        return self.conversion_rate(inst.quote, self._ccy)

    def account(self) -> AccountState:
        unreal = ZERO
        margin = ZERO
        self.unvalued_positions: List[str] = []
        for pos in self._positions.values():
            inst = self._instruments[pos.instrument]
            q = self._quotes.get(pos.instrument)
            if q is None:
                continue
            try:
                conv = self._conv(inst)
            except ConversionMissingError:
                # A real venue reports equity in the account currency regardless
                # of what we can convert, so the account read does not fail here.
                # The position is excluded from the mark and named, which is what
                # a venue statement footnote would do.
                self.unvalued_positions.append(pos.instrument)
                continue
            unreal += pos.unrealised(q, inst, conv)
            margin += inst.units(pos.lots) * q.mid * inst.margin_rate * conv
        equity = self._balance + unreal
        self._peak_equity = max(self._peak_equity, equity)
        return AccountState(
            account_id=self._account_id,
            currency=self._ccy,
            balance=self._balance,
            equity=equity,
            margin_used=margin,
            margin_available=equity - margin,
            unrealised_pnl=unreal,
            open_positions=len(self._positions),
            last_transaction_id=str(self._txn_seq),
            ts_ns=self._now_ns,
            source="paper",
            account_type="demo",
            leverage=0,
            venue_name="Sentinel simulator",
        )

    def positions(self) -> List[Position]:
        return list(self._positions.values())

    def open_orders(self) -> List[Order]:
        return [o for o in self._orders.values() if not o.state.terminal]

    def submit(self, intent: OrderIntent, *, timeout_ms: int = 5000) -> SubmitResult:
        # --- idempotency: the whole point of the client order id ----------- #
        existing = self._orders.get(intent.client_order_id)
        if existing is not None:
            return SubmitResult(
                state=existing.state,
                venue_order_id=existing.venue_order_id,
                fills=list(existing.fills),
                reject_reason="DUPLICATE_CLIENT_ORDER_ID",
                raw={"deduplicated": True},
                venue_ts_ns=self._now_ns,
            )

        inst = self._instruments.get(intent.instrument)
        if inst is None:
            return SubmitResult(state=OrderState.REJECTED,
                                reject_reason=f"UNKNOWN_INSTRUMENT:{intent.instrument}")
        q = self._quotes.get(intent.instrument)
        if q is None:
            return SubmitResult(state=OrderState.REJECTED, reject_reason="NO_PRICE")

        lots = inst.round_lots_down(intent.lots)
        if lots < inst.min_lot:
            return SubmitResult(state=OrderState.REJECTED,
                                reject_reason=f"BELOW_MIN_LOT:{inst.min_lot}")
        if lots > inst.max_lot:
            return SubmitResult(state=OrderState.REJECTED, reject_reason="ABOVE_MAX_LOT")

        # A real venue refuses an order it cannot margin. Without this check the
        # simulator happily opened a 5.5m notional position on a $500 account
        # and then margin-stopped it on the next tick, manufacturing trades that
        # could never have existed and flattering (or wrecking) the statistics
        # with fiction. Reject at submit, the way the venue does.
        existing_pos = self._positions.get(intent.instrument)
        opening = existing_pos is None or existing_pos.side is intent.side
        if opening:
            try:
                conv = self._conv(inst)
            except ConversionMissingError:
                conv = None
            if conv is not None:
                required = inst.units(lots) * q.mid * inst.margin_rate * conv
                available = self.account().margin_available
                if required > available:
                    self._record_txn("ORDER_REJECT", intent.client_order_id,
                                     {"reason": "INSUFFICIENT_MARGIN",
                                      "required": str(required), "available": str(available)})
                    return SubmitResult(
                        state=OrderState.REJECTED,
                        reject_reason="INSUFFICIENT_MARGIN",
                        raw={"required_margin": str(required),
                             "margin_available": str(available)})

        order = Order(intent=intent, state=OrderState.SENT, sent_ns=self._now_ns, attempts=1)
        self._orders[intent.client_order_id] = order

        latency = self._latency_ms()
        if latency > timeout_ms:
            # The classic dangerous case: we time out, the venue may still act.
            order.state = OrderState.UNKNOWN
            order.last_error = f"timeout after {timeout_ms}ms (venue latency {latency:.0f}ms)"
            self._record_txn("ORDER_TIMEOUT", intent.client_order_id, {"latency_ms": latency})
            # The simulator *does* complete the order behind our back, exactly
            # as a real venue would, so query_order() can later reveal it.
            self._complete(order, inst, q, lots, hidden=True, latency_ms=latency)
            return SubmitResult(state=OrderState.UNKNOWN, reject_reason="TIMEOUT",
                                raw={"latency_ms": latency})

        # Last look, biased toward rejecting the client's good fills.
        favourable = self._rng.random() < 0.5
        if self._last_look_rejects(favourable):
            order.state = OrderState.REJECTED
            order.reject_reason = "LAST_LOOK_REJECT"
            self._record_txn("ORDER_REJECT", intent.client_order_id,
                             {"reason": "LAST_LOOK", "favourable_to_client": favourable})
            return SubmitResult(state=OrderState.REJECTED, reject_reason="LAST_LOOK_REJECT",
                                raw={"favourable_to_client": favourable})

        self._complete(order, inst, q, lots, latency_ms=latency)
        return SubmitResult(state=order.state, venue_order_id=order.venue_order_id,
                            fills=list(order.fills), venue_ts_ns=self._now_ns)

    def _complete(self, order: Order, inst: Instrument, q: Quote, lots: Decimal,
                  *, hidden: bool = False, latency_ms: Optional[float] = None) -> None:
        intent = order.intent
        base_price = q.price_for(intent.side)
        slip_pips = self._slippage_pips(intent.side, latency_ms)
        # Positive slippage is always adverse: worse ask for a buy, worse bid
        # for a sell.
        price = inst.round_price(base_price + slip_pips * inst.pip * D(intent.side.sign))

        fill_lots = lots
        state = OrderState.FILLED
        if lots > self._profile.partial_fill_threshold_lots:
            fill_lots = inst.round_lots_down(lots * self._profile.partial_fill_ratio)
            state = OrderState.PARTIAL if fill_lots < lots else OrderState.FILLED

        commission = (self._profile.commission_per_lot_round_turn
                      * fill_lots * self._profile.cost_multiplier)
        self._txn_seq += 1
        fill = Fill(
            client_order_id=intent.client_order_id,
            venue_order_id=f"V{self._txn_seq:08d}",
            instrument=intent.instrument,
            side=intent.side,
            lots=fill_lots,
            price=price,
            ts_ns=self._now_ns,
            received_ns=self._now_ns,
            commission=commission,
            slippage_pips=slip_pips,
            liquidity_flag="taker",
        )
        order.venue_order_id = fill.venue_order_id
        order.fills.append(fill)
        order.acked_ns = self._now_ns
        order.state = OrderState.FILLED if state is OrderState.FILLED else OrderState.PARTIAL
        if hidden:
            # Venue acted but we never saw the response.
            order.state = OrderState.UNKNOWN
        self._balance -= commission
        self._apply_fill(fill, inst, intent)
        self._record_txn("ORDER_FILL", intent.client_order_id, {
            "price": str(price), "lots": str(fill_lots), "commission": str(commission),
            "slippage_pips": str(slip_pips), "hidden": hidden,
        })

    def _apply_fill(self, fill: Fill, inst: Instrument, intent: OrderIntent) -> None:
        """Apply a fill to the book.

        Two invariants are maintained here and they are both load-bearing:

        1. ``initial_risk`` always describes the risk of the lots ACTUALLY
           held. The intent's ``risk_amount`` describes the lots we asked for,
           and a partial fill or a size cap means those differ. Carrying the
           intent's number onto a smaller position makes every R-multiple read
           low by the fill ratio -- which in turn makes the trailing stop and
           the break-even trigger arm late, and teaches the learning loop that
           banking profit early is better than it is.
        2. The commission on a REVERSING fill is split between the lots that
           close the old position and the lots that open the new one. Charging
           the whole amount to the new position left the closing share in no
           ClosedTrade at all, so the ledger stopped matching the balance by
           exactly commission_per_lot x closing_lots on every reversal.
        """
        filled_fraction = (fill.lots / intent.lots) if intent.lots > 0 else D("1")
        risk_for_fill = intent.risk_amount * filled_fraction

        pos = self._positions.get(fill.instrument)
        if pos is None:
            self._positions[fill.instrument] = Position(
                instrument=fill.instrument, side=fill.side, lots=fill.lots,
                entry_price=fill.price, opened_ns=fill.ts_ns, strategy=intent.strategy,
                stop_loss=intent.stop_loss, take_profit=intent.take_profit,
                broker_stop_confirmed=intent.stop_loss is not None,
                client_order_id=fill.client_order_id,
                commission_paid=fill.commission, initial_risk=risk_for_fill,
            )
            return
        if pos.side is fill.side:
            total = pos.lots + fill.lots
            pos.entry_price = ((pos.entry_price * pos.lots) + (fill.price * fill.lots)) / total
            pos.lots = total
            pos.commission_paid += fill.commission
            pos.initial_risk += risk_for_fill
            if intent.stop_loss is not None:
                pos.stop_loss = intent.stop_loss
            if intent.take_profit is not None:
                pos.take_profit = intent.take_profit
        else:
            closing = min(pos.lots, fill.lots)
            remaining = fill.lots - closing
            closing_share = ((fill.commission * closing / fill.lots)
                             if fill.lots > 0 else ZERO)
            carried = fill.commission - closing_share
            # Hand the closing lots' commission to _realise so it lands in the
            # ClosedTrade rather than vanishing from the ledger.
            self._realise(pos, inst, fill.price, closing, fill.ts_ns, "opposite_fill",
                          extra_commission=closing_share)
            if remaining > 0:
                opened_fraction = (remaining / intent.lots) if intent.lots > 0 else D("1")
                self._positions[fill.instrument] = Position(
                    instrument=fill.instrument, side=fill.side, lots=remaining,
                    entry_price=fill.price, opened_ns=fill.ts_ns, strategy=intent.strategy,
                    stop_loss=intent.stop_loss, take_profit=intent.take_profit,
                    broker_stop_confirmed=intent.stop_loss is not None,
                    client_order_id=fill.client_order_id,
                    initial_risk=intent.risk_amount * opened_fraction,
                    commission_paid=carried,
                )

    def _realise(self, pos: Position, inst: Instrument, exit_price: Decimal,
                 lots: Decimal, ts_ns: int, reason: str,
                 extra_commission: Decimal = ZERO) -> None:
        """Close ``lots`` and record the round trip.

        ``ClosedTrade.pnl`` is NET of the commission and financing attributable
        to the closed portion -- the number that actually reached the balance.
        Recording a gross figure here and the costs beside it invites every
        downstream statistic to quietly ignore the costs, which is the single
        easiest way to make a losing system look profitable. R-multiples are
        computed on the same net figure for the same reason.
        """
        conv = self._conv(inst)
        delta = (exit_price - pos.entry_price) * D(pos.side.sign)
        price_pnl = delta * inst.units(lots) * conv
        self._balance += price_pnl
        fraction = lots / pos.lots if pos.lots > 0 else D("1")
        risk_share = pos.initial_risk * fraction
        commission_share = pos.commission_paid * fraction + extra_commission
        financing_share = pos.financing_paid * fraction
        # Commission was already debited at fill time and financing at rollover;
        # the balance is not touched again here. Only the trade's reported P&L
        # is made net, so that the trade ledger and the balance agree.
        net_pnl = price_pnl - commission_share + financing_share
        self._trade_seq += 1
        self._closed.append(ClosedTrade(
            trade_id=f"T{self._trade_seq:06d}",
            strategy=pos.strategy, instrument=pos.instrument, side=pos.side, lots=lots,
            entry_price=pos.entry_price, exit_price=exit_price,
            opened_ns=pos.opened_ns, closed_ns=ts_ns,
            pnl=net_pnl, pnl_pips=delta / inst.pip,
            commission=commission_share, financing=financing_share,
            initial_risk=risk_share,
            r_multiple=(net_pnl / risk_share) if risk_share > 0 else ZERO,
            exit_reason=reason,
            max_favourable_r=pos.max_favourable, max_adverse_r=pos.max_adverse,
        ))
        # Consume the attributed share so a later close cannot count it again:
        # without this, a 50% scale-out followed by a full close charges 150% of
        # the commission to the ledger and the trade log stops matching the balance.
        pos.commission_paid -= (commission_share - extra_commission)
        pos.financing_paid -= financing_share
        # The remaining lots carry the remaining risk. Leaving initial_risk at
        # its full-position value halved every R the runner reported after a
        # scale-out, so the leg carrying the profit became the least protected.
        pos.initial_risk -= risk_share
        pos.lots -= lots
        pos.realised_pnl += net_pnl
        if pos.lots <= 0:
            self._positions.pop(pos.instrument, None)
        if self._profile.negative_balance_protection and self._balance < ZERO:
            # Retail FX in most regulated jurisdictions cannot go below zero.
            # Simulating a debt the account could never owe manufactures losses
            # larger than the account and distorts every drawdown statistic.
            self._record_txn("NEGATIVE_BALANCE_RESET", "",
                             {"balance_before": str(self._balance)})
            self._balance = ZERO
        self._record_txn("POSITION_CLOSE", pos.client_order_id or "", {
            "instrument": pos.instrument, "lots": str(lots), "pnl": str(net_pnl),
            "gross_pnl": str(price_pnl), "reason": reason,
        })

    def _check_exits(self, q: Quote) -> None:
        pos = self._positions.get(q.instrument)
        if pos is None:
            return
        inst = self._instruments[q.instrument]
        exit_price = q.price_for(pos.side.opposite)
        # Track excursions in R for the post-mortem module.
        if pos.initial_risk > 0:
            r = pos.unrealised(q, inst, self._conv(inst)) / pos.initial_risk
            pos.max_favourable = max(pos.max_favourable, r)
            pos.max_adverse = min(pos.max_adverse, r)

        hit_stop = pos.stop_loss is not None and (
            (pos.side is Side.BUY and exit_price <= pos.stop_loss)
            or (pos.side is Side.SELL and exit_price >= pos.stop_loss)
        )
        hit_target = pos.take_profit is not None and (
            (pos.side is Side.BUY and exit_price >= pos.take_profit)
            or (pos.side is Side.SELL and exit_price <= pos.take_profit)
        )
        if hit_stop:
            # A stop is a market order once touched: fill at the worse of the
            # stop price and the currently available price (gap risk), and then
            # slip like any other market order. Filling a stop exactly on its
            # own price was the one place this "adversarial" simulator was
            # optimistic -- and a stop-out is the exit that happens most often
            # when things go wrong, so it is the worst place to be generous.
            #
            # No exit commission is added: the FULL round-turn is already
            # debited at entry, so charging again here would double-count.
            fill_px = min(exit_price, pos.stop_loss) if pos.side is Side.BUY \
                else max(exit_price, pos.stop_loss)
            slip = self._slippage_pips(pos.side.opposite)
            fill_px = fill_px + slip * inst.pip * D(pos.side.opposite.sign)
            self._realise(pos, inst, inst.round_price(fill_px), pos.lots, q.ts_ns, "stop_loss")
        elif hit_target:
            # Limit-style: no better than the target.
            fill_px = pos.take_profit
            self._realise(pos, inst, inst.round_price(fill_px), pos.lots, q.ts_ns, "take_profit")

    def _check_margin(self) -> None:
        acct = self.account()
        level = acct.margin_level_pct
        if level is None or level > self._profile.stop_out_margin_level_pct:
            return
        # Venue closes the largest loser first until the level recovers.
        while self._positions:
            worst = None
            worst_pnl = None
            for pos in self._positions.values():
                q = self._quotes.get(pos.instrument)
                if q is None:
                    continue
                inst = self._instruments[pos.instrument]
                pnl = pos.unrealised(q, inst, self._conv(inst))
                if worst_pnl is None or pnl < worst_pnl:
                    worst, worst_pnl = pos, pnl
            if worst is None:
                break
            q = self._quotes[worst.instrument]
            inst = self._instruments[worst.instrument]
            self._realise(worst, inst, q.price_for(worst.side.opposite), worst.lots,
                          self._now_ns, "margin_stop_out")
            acct = self.account()
            if acct.margin_level_pct is None or \
               acct.margin_level_pct > self._profile.margin_call_level_pct:
                break

    def _apply_rollover(self) -> None:
        """Charge carry for every rollover the clock has passed.

        This used to fire only on a quote whose UTC hour was >= 21. A D1 bar
        replayed as O/H/L/C emits quotes at 00:00, 08:00, 16:00 and 24:00 --
        never at 21:00 -- so a whole daily-timeframe backtest accrued ZERO
        financing. A carry strategy backtested with no carry is not testing its
        own hypothesis, and a negative-carry trend trade got a free ride.

        Driving it off elapsed calendar days instead means a gap of any size --
        a D1 bar, a weekend, a restart after three days -- charges the right
        number of days. Saturday and Sunday are skipped because the weekend is
        already paid for by the triple-swap weekday.
        """
        from datetime import datetime, timedelta, timezone

        dt = datetime.fromtimestamp(self._now_ns / 1e9, tz=timezone.utc)
        # The venue's value date rolls at 17:00 New York, i.e. 21:00/22:00 UTC.
        # A quote before that hour belongs to the previous value date.
        # The rollover is 17:00 New York, which is 21:00 UTC in summer and
        # 22:00 in winter. A fixed 21-hour shift put the Wednesday triple swap
        # on Thursday for half the year. tzrules carries the DST arithmetic.
        from ..core.tzrules import utc_offset_hours
        ny_offset = utc_offset_hours("America/New_York", dt.replace(tzinfo=None))
        value_date = (dt - timedelta(hours=17 - ny_offset)).date()
        day = value_date.toordinal()
        if self._last_rollover_day is None:
            self._last_rollover_day = day
            return
        if day <= self._last_rollover_day:
            return

        pending = list(range(self._last_rollover_day + 1, day + 1))
        self._last_rollover_day = day
        if not self._positions:
            return
        for ordinal in pending:
            from datetime import date as _date
            weekday = _date.fromordinal(ordinal).weekday()
            if weekday >= 5:            # Sat/Sun: covered by the triple swap
                continue
            multiplier = (D("3") if weekday == self._profile.triple_swap_weekday
                          else D("1"))
            for pos in list(self._positions.values()):
                inst = self._instruments[pos.instrument]
                table = (self._profile.swap_long_pips_per_day if pos.side is Side.BUY
                         else self._profile.swap_short_pips_per_day)
                pips = table.get(pos.instrument, D("-0.15"))  # retail carry is usually negative
                charge = pips * inst.pip * inst.units(pos.lots) * self._conv(inst) * multiplier
                self._balance += charge
                pos.financing_paid += charge
            self._record_txn("ROLLOVER", "", {"multiplier": str(multiplier),
                                              "value_date": str(_date.fromordinal(ordinal))})

    def query_order(self, client_order_id: str) -> Optional[SubmitResult]:
        order = self._orders.get(client_order_id)
        if order is None:
            return None
        # Resolving UNKNOWN is the *only* legitimate exit from that state, and
        # it reveals the fill that the timeout hid.
        state = OrderState.FILLED if order.fills else order.state
        if order.state is OrderState.UNKNOWN and order.fills:
            order.state = OrderState.FILLED
        return SubmitResult(state=state, venue_order_id=order.venue_order_id,
                            fills=list(order.fills), reject_reason=order.reject_reason,
                            venue_ts_ns=self._now_ns)

    def cancel(self, client_order_id: str) -> bool:
        order = self._orders.get(client_order_id)
        if order is None or order.state.terminal:
            return False
        order.state = OrderState.CANCELLED
        self._record_txn("ORDER_CANCEL", client_order_id, {})
        return True

    def close_position(self, instrument: str, lots: Optional[Decimal] = None,
                       *, reason: str = "manual") -> SubmitResult:
        pos = self._positions.get(instrument)
        if pos is None:
            return SubmitResult(state=OrderState.REJECTED, reject_reason="NO_POSITION")
        q = self._quotes.get(instrument)
        if q is None:
            return SubmitResult(state=OrderState.REJECTED, reject_reason="NO_PRICE")
        inst = self._instruments[instrument]
        # `if lots` is False for Decimal("0"), which silently closed the whole
        # position when the caller asked for none of it. Test against None.
        close_lots = (inst.round_lots_down(min(dec(lots), pos.lots))
                      if lots is not None else pos.lots)
        if close_lots <= 0:
            return SubmitResult(state=OrderState.REJECTED, reject_reason="BELOW_MIN_LOT")
        px = q.price_for(pos.side.opposite)
        slip = self._slippage_pips(pos.side.opposite)
        px = inst.round_price(px + slip * inst.pip * D(pos.side.opposite.sign))
        self._realise(pos, inst, px, close_lots, self._now_ns, reason)
        self._txn_seq += 1
        return SubmitResult(state=OrderState.FILLED, venue_order_id=f"V{self._txn_seq:08d}",
                            venue_ts_ns=self._now_ns)

    def modify_position(self, instrument: str, *, stop_loss: Optional[Decimal] = None,
                        take_profit: Optional[Decimal] = None) -> bool:
        pos = self._positions.get(instrument)
        if pos is None:
            return False
        inst = self._instruments[instrument]
        if stop_loss is not None:
            new_stop = inst.round_price(stop_loss)
            # A stop may only move toward safety while a position is open.
            # Widening a stop mid-trade is the single most common way a
            # controlled loss becomes an uncontrolled one, so it is refused here
            # rather than trusted to caller discipline.
            if pos.stop_loss is not None:
                if pos.side is Side.BUY and new_stop < pos.stop_loss:
                    raise PermanentError("refusing to widen a BUY stop downward",
                                         instrument=instrument)
                if pos.side is Side.SELL and new_stop > pos.stop_loss:
                    raise PermanentError("refusing to widen a SELL stop upward",
                                         instrument=instrument)
            pos.stop_loss = new_stop
            pos.broker_stop_confirmed = True
        if take_profit is not None:
            pos.take_profit = inst.round_price(take_profit)
        self._record_txn("POSITION_MODIFY", pos.client_order_id or "", {
            "instrument": instrument, "stop_loss": str(stop_loss), "take_profit": str(take_profit),
        })
        return True

    def transactions_since(self, last_id: str) -> Iterable[Dict[str, Any]]:
        try:
            start = int(last_id)
        except (TypeError, ValueError):
            start = 0
        return [t for t in self._transactions if t["id"] > start]

    def _record_txn(self, kind: str, client_order_id: str, data: Dict[str, Any]) -> None:
        self._txn_seq += 1
        self._transactions.append({
            "id": self._txn_seq, "kind": kind, "client_order_id": client_order_id,
            "ts_ns": self._now_ns, **data,
        })

    # -- reporting ---------------------------------------------------------- #

    @property
    def closed_trades(self) -> List[ClosedTrade]:
        return list(self._closed)

    @property
    def peak_equity(self) -> Decimal:
        return self._peak_equity
