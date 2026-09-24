"""OANDA v20 adapter.

Chosen as the reference live adapter because it satisfies the capability gates
the research brief puts *before* any strategy work:

* ``clientExtensions.id`` is a caller-supplied identifier the venue rejects on
  duplication -- real idempotency, not a hopeful lock.
* Stop-loss and take-profit are attached to the order and live on OANDA's
  servers, so they survive our process dying or the link dropping.
* ``/transactions/sinceid`` gives an ordered, resumable event stream, which is
  what makes restart reconciliation exact.

Credentials come from the environment only. There is no code path that reads a
token from a config file or a request body.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

import httpx

from ..core.clock import wall_ns
from ..core.errors import (
    AuthError,
    BrokerError,
    ConversionMissingError,
    PermanentError,
    TransientError,
    UnknownOutcomeError,
)
from ..core.money import D, Instrument, ZERO, dec
from ..core.types import (
    AccountState,
    Bar,
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

_HOSTS = {
    "live": "https://api-fxtrade.oanda.com",
    "practice": "https://api-fxpractice.oanda.com",
}

_TIMEFRAME_SEC = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800,
}

# Business rejections that must never be retried.
_PERMANENT_CODES = {
    "INSUFFICIENT_MARGIN", "INSTRUMENT_NOT_TRADEABLE", "MARKET_HALTED",
    "ACCOUNT_NOT_TRADEABLE", "INVALID_INSTRUMENT", "UNITS_LIMIT_EXCEEDED",
    "TAKE_PROFIT_ON_FILL_LOSS", "STOP_LOSS_ON_FILL_LOSS", "CLIENT_ORDER_ID_ALREADY_EXISTS",
}


def _rfc3339_ns(value: str) -> int:
    """OANDA returns RFC3339 with nanosecond precision: 2026-03-02T10:00:00.123456789Z."""
    if not value:
        return wall_ns()
    body = value.rstrip("Z")
    if "." in body:
        head, frac = body.split(".", 1)
        frac = (frac + "000000000")[:9]
    else:
        head, frac = body, "000000000"
    from datetime import datetime, timezone

    dt = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1_000_000_000 + int(frac)


def _leverage_from_margin_rate(rate) -> int:
    """OANDA states a margin RATE (0.0333); everyone else states leverage (30).

    Converted here so the dashboard shows one unit. A rate of zero or nonsense
    yields 0, which reads as "not reported" everywhere downstream rather than
    as infinite leverage.
    """
    try:
        value = dec(rate)
    except Exception:  # noqa: BLE001
        return 0
    if value <= 0:
        return 0
    return int(D("1") / value)


class OandaBroker(Broker):
    def __init__(
        self,
        *,
        account_id: Optional[str] = None,
        token: Optional[str] = None,
        environment: str = "practice",
        instruments: Optional[Dict[str, Instrument]] = None,
        timeout_s: float = 10.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.account_id = account_id or os.environ.get("OANDA_ACCOUNT_ID", "")
        token = token or os.environ.get("OANDA_API_TOKEN", "")
        if not self.account_id or not token:
            raise AuthError(
                "OANDA_ACCOUNT_ID and OANDA_API_TOKEN must be set in the environment"
            )
        if environment not in _HOSTS:
            raise PermanentError(f"unknown OANDA environment {environment!r}")
        self.environment = environment
        self._base = _HOSTS[environment]
        self._client = client or httpx.Client(
            base_url=self._base,
            timeout=timeout_s,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept-Datetime-Format": "RFC3339",
            },
        )
        self._instruments: Dict[str, Instrument] = instruments or {}
        self._account_ccy = "USD"
        self._last_txn_id = "0"

        self.capabilities = BrokerCapabilities(
            supports_client_order_id=True,
            supports_server_side_stop=True,
            supports_transaction_stream=True,
            supports_partial_close=True,
            supports_fractional_lots=True,
            min_lot=D("0.01"),
            lot_step=D("0.01"),
            name=f"oanda:{environment}",
            notes="v20 REST. clientExtensions.id gives true duplicate rejection.",
            supports_bar_history=True,
        )
        if not self._instruments:
            self._instruments = self._load_instruments()

    # -- transport ---------------------------------------------------------- #

    def _request(self, method: str, path: str, *, json: Any = None,
                 params: Any = None, idempotent_write: bool = False) -> Dict[str, Any]:
        try:
            resp = self._client.request(method, path, json=json, params=params)
        except httpx.TimeoutException as exc:
            if idempotent_write:
                # We do not know whether the order was accepted. Resolving this
                # by resending would risk a duplicate position; the only safe
                # resolution is a query.
                raise UnknownOutcomeError("timeout on a write request", path=path) from exc
            raise TransientError("timeout", path=path) from exc
        except httpx.HTTPError as exc:
            if idempotent_write:
                raise UnknownOutcomeError("transport failure on a write request",
                                          path=path, detail=str(exc)) from exc
            raise TransientError("transport failure", path=path, detail=str(exc)) from exc

        if resp.status_code in (401, 403):
            raise AuthError("OANDA rejected the credentials", status=resp.status_code)
        if resp.status_code == 429:
            raise TransientError("rate limited", status=429)
        if resp.status_code >= 500:
            if idempotent_write:
                raise UnknownOutcomeError("server error on a write request",
                                          status=resp.status_code)
            raise TransientError("server error", status=resp.status_code)
        try:
            body = resp.json()
        except ValueError:
            raise BrokerError("non-JSON response", status=resp.status_code)
        if resp.status_code >= 400:
            code = body.get("errorCode") or body.get("orderRejectTransaction", {}).get("reason")
            msg = body.get("errorMessage", resp.text[:300])
            if code in _PERMANENT_CODES:
                raise PermanentError(msg, code=code)
            raise BrokerError(msg, code=code, status=resp.status_code)
        return body

    # -- reference data ----------------------------------------------------- #

    def _load_instruments(self) -> Dict[str, Instrument]:
        body = self._request("GET", f"/v3/accounts/{self.account_id}/instruments")
        out: Dict[str, Instrument] = {}
        for row in body.get("instruments", []):
            name = row["name"]
            if "_" not in name:
                continue
            base, quote = name.split("_", 1)
            precision = int(row.get("displayPrecision", 5))
            pip_location = int(row.get("pipLocation", -4))
            contract_size = D("100000")
            # OANDA reports sizes in UNITS and a units precision (0 = whole
            # units). The step is 10^-precision units; the minimum is
            # minimumTradeSize units. Both convert to lots by the contract size,
            # and the step can never exceed the minimum -- deriving them
            # independently, as this did, produced min_lot=0.00001 against
            # lot_step=0.01 and the Instrument constructor refused to build.
            units_precision = int(row.get("tradeUnitsPrecision", 0) or 0)
            step_units = D("10") ** (-units_precision)
            min_units = dec(row.get("minimumTradeSize", "1"))
            min_units = max(min_units, step_units)
            out[name] = Instrument(
                symbol=name, base=base, quote=quote,
                pip=D("10") ** pip_location,
                tick=D("10") ** (-precision),
                contract_size=contract_size,
                min_lot=min_units / contract_size,
                lot_step=step_units / contract_size,
                max_lot=dec(row.get("maximumOrderUnits", "100000000")) / contract_size,
                margin_rate=dec(row.get("marginRate", "0.033")),
                venue="oanda",
            )
        return out

    def instruments(self) -> Dict[str, Instrument]:
        return dict(self._instruments)

    # -- market data -------------------------------------------------------- #

    def quote(self, symbol: str) -> Quote:
        return self.quotes([symbol])[symbol]

    def quotes(self, symbols) -> Dict[str, Quote]:
        body = self._request("GET", f"/v3/accounts/{self.account_id}/pricing",
                             params={"instruments": ",".join(symbols)})
        out: Dict[str, Quote] = {}
        for p in body.get("prices", []):
            bids, asks = p.get("bids", []), p.get("asks", [])
            if not bids or not asks:
                continue
            if p.get("tradeable") is False:
                # A non-tradeable price is information, not a quote to act on.
                continue
            out[p["instrument"]] = Quote(
                instrument=p["instrument"],
                bid=dec(bids[0]["price"]), ask=dec(asks[0]["price"]),
                ts_ns=_rfc3339_ns(p.get("time", "")),
                received_ns=wall_ns(), source="oanda",
                liquidity=dec(bids[0].get("liquidity", 0)) or None,
            )
        missing = set(symbols) - set(out)
        if missing:
            raise BrokerError(f"no tradeable price for {sorted(missing)}", code="NO_QUOTE")
        return out

    # -- bar history ---------------------------------------------------------- #

    #: OANDA granularity names happen to match ours for everything we use.
    _GRANULARITIES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"}
    #: The v20 hard limit per request.
    _MAX_CANDLES = 5000

    def fetch_bars(self, symbol: str, timeframe: str, count: int, *,
                   end_ns: Optional[int] = None) -> List[Bar]:
        if timeframe not in self._GRANULARITIES:
            raise BrokerError(f"OANDA has no {timeframe} granularity", code="BAD_TIMEFRAME")
        if count <= 0:
            return []
        want = min(int(count) + 1, self._MAX_CANDLES)
        # MID prices for the bars. The strategies are validated on mid bars and
        # the quote the risk engine sizes against is a live bid/ask, which is
        # the same split the backtester uses; asking for BA here would double
        # the payload for a series nothing reads.
        params: Dict[str, Any] = {"granularity": timeframe, "count": want, "price": "M"}
        if end_ns is not None:
            from datetime import datetime, timezone
            params["to"] = datetime.fromtimestamp(end_ns / 1e9, tz=timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z")
        body = self._request("GET", f"/v3/instruments/{symbol}/candles", params=params)
        interval_ns = _TIMEFRAME_SEC.get(timeframe, 3600) * 1_000_000_000
        out: List[Bar] = []
        for c in body.get("candles", []):
            mid = c.get("mid") or {}
            try:
                start = _rfc3339_ns(c.get("time", ""))
                out.append(Bar(
                    instrument=symbol, timeframe=timeframe,
                    open=dec(mid["o"]), high=dec(mid["h"]), low=dec(mid["l"]),
                    close=dec(mid["c"]), volume=dec(c.get("volume", 0) or 0),
                    start_ns=start, end_ns=start + interval_ns,
                    # OANDA says which bar is still forming; believe it rather
                    # than re-deriving from the clock.
                    complete=bool(c.get("complete", True)), source="oanda",
                ))
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda b: b.start_ns)
        return out[-int(count):] if len(out) > count else out

    def conversion_rate(self, quote_ccy: str, account_ccy: str) -> Decimal:
        if quote_ccy == account_ccy:
            return D("1")
        direct, inverse = f"{quote_ccy}_{account_ccy}", f"{account_ccy}_{quote_ccy}"
        for pair, invert in ((direct, False), (inverse, True)):
            if pair in self._instruments:
                try:
                    q = self.quote(pair)
                except BrokerError:
                    continue
                return (D("1") / q.mid) if invert else q.mid
        raise ConversionMissingError(f"no {quote_ccy}->{account_ccy} pair available",
                                     quote_ccy=quote_ccy, account_ccy=account_ccy)

    # -- account ------------------------------------------------------------ #

    def account(self) -> AccountState:
        body = self._request("GET", f"/v3/accounts/{self.account_id}/summary")
        a = body["account"]
        self._account_ccy = a.get("currency", self._account_ccy)
        self._last_txn_id = str(body.get("lastTransactionID", self._last_txn_id))
        return AccountState(
            account_id=a["id"], currency=self._account_ccy,
            balance=dec(a["balance"]), equity=dec(a["NAV"]),
            margin_used=dec(a.get("marginUsed", "0")),
            margin_available=dec(a.get("marginAvailable", "0")),
            unrealised_pnl=dec(a.get("unrealizedPL", "0")),
            open_positions=int(a.get("openPositionCount", 0)),
            last_transaction_id=self._last_txn_id,
            ts_ns=wall_ns(), source="oanda",
            # The environment IS the account kind at OANDA: a practice token
            # cannot reach the live host and vice versa.
            account_type="live" if self.environment == "live" else "demo",
            leverage=_leverage_from_margin_rate(a.get("marginRate")),
            venue_name=f"OANDA ({self.environment})",
        )

    def positions(self) -> List[Position]:
        """Open positions, WITH their venue-side protective orders.

        ``/openPositions`` does not carry the stop, so the open trades are read
        too and the tightest stop per instrument is attached. Without this every
        position reads as unprotected: the reconciler then "repairs" each one on
        every pass by attaching a fresh 50-pip stop measured from the CURRENT
        price -- which silently widens the strategy's own stop every 30 seconds
        -- and the profit-protection layer, which needs a defined risk, does
        nothing at all.
        """
        body = self._request("GET", f"/v3/accounts/{self.account_id}/openPositions")
        trades = self._open_trades_by_instrument()   # raises if the stops are unreadable
        out: List[Position] = []
        for p in body.get("positions", []):
            symbol = p["instrument"]
            inst = self._instruments.get(symbol)
            cs = inst.contract_size if inst else D("100000")
            for side_key, side in (("long", Side.BUY), ("short", Side.SELL)):
                leg = p.get(side_key, {})
                units = dec(leg.get("units", "0"))
                if units == 0:
                    continue
                info = trades.get((symbol, side), {})
                stop = info.get("stop_loss")
                out.append(Position(
                    instrument=symbol, side=side, lots=abs(units) / cs,
                    entry_price=dec(leg.get("averagePrice", "0")),
                    opened_ns=info.get("opened_ns", wall_ns()),
                    stop_loss=stop,
                    take_profit=info.get("take_profit"),
                    broker_stop_confirmed=stop is not None,
                    realised_pnl=dec(leg.get("realizedPL", "0")),
                    financing_paid=dec(leg.get("financing", "0")),
                    venue_position_id=",".join(leg.get("tradeIDs", [])),
                ))
        return out

    def _open_trades_by_instrument(self) -> Dict[tuple, Dict[str, Any]]:
        """Tightest stop and nearest target per (instrument, side).

        A failed read RAISES rather than returning an empty map. Returning {} is
        indistinguishable from "these positions have no stops", which would send
        the reconciler into its emergency-repair path -- and, since that repair
        is now correctly refused as a widening, would halt the agent on a single
        transient HTTP failure.
        """
        try:
            body = self._request("GET", f"/v3/accounts/{self.account_id}/openTrades")
        except (TransientError, BrokerError) as exc:
            raise TransientError(
                "could not read the open trades, so the protective stops on the open "
                "positions are unknown; this is not evidence that they are missing",
                detail=str(exc)) from exc
        out: Dict[tuple, Dict[str, Any]] = {}
        for t in body.get("trades", []):
            symbol = t.get("instrument")
            units = dec(t.get("currentUnits", "0"))
            if not symbol or units == 0:
                continue
            side = Side.BUY if units > 0 else Side.SELL
            stop = t.get("stopLossOrder", {}).get("price")
            target = t.get("takeProfitOrder", {}).get("price")
            entry = out.setdefault((symbol, side), {
                "stop_loss": None, "take_profit": None,
                "opened_ns": _rfc3339_ns(t.get("openTime", "")),
            })
            if stop is not None:
                price = dec(stop)
                current = entry["stop_loss"]
                # Tightest stop wins: it is the one that actually binds.
                if current is None or (price > current if side is Side.BUY
                                       else price < current):
                    entry["stop_loss"] = price
            if target is not None:
                entry["take_profit"] = dec(target)
        return out

    def open_orders(self) -> List[Order]:
        # Pending orders are returned; the OMS owns their local state machine.
        self._request("GET", f"/v3/accounts/{self.account_id}/pendingOrders")
        return []

    # -- trading ------------------------------------------------------------ #

    def submit(self, intent: OrderIntent, *, timeout_ms: int = 5000) -> SubmitResult:
        inst = self.instrument(intent.instrument)
        units = int(inst.units(intent.lots) * D(intent.side.sign))
        order: Dict[str, Any] = {
            "instrument": intent.instrument,
            "units": str(units),
            "timeInForce": "FOK" if intent.order_type is OrderType.MARKET else "GTC",
            "positionFill": "DEFAULT",
            # This is the idempotency key. OANDA rejects a second order that
            # reuses it with CLIENT_ORDER_ID_ALREADY_EXISTS.
            "clientExtensions": {
                "id": intent.client_order_id,
                "tag": intent.strategy[:30],
                "comment": intent.reason[:100],
            },
        }
        if intent.order_type is OrderType.MARKET:
            order["type"] = "MARKET"
        elif intent.order_type is OrderType.LIMIT:
            order["type"] = "LIMIT"
            order["price"] = str(inst.round_price(intent.limit_price))
        else:
            order["type"] = "STOP"
            order["price"] = str(inst.round_price(intent.limit_price))
        if intent.stop_loss is not None:
            order["stopLossOnFill"] = {"price": str(inst.round_price(intent.stop_loss)),
                                       "timeInForce": "GTC"}
        if intent.take_profit is not None:
            order["takeProfitOnFill"] = {"price": str(inst.round_price(intent.take_profit)),
                                         "timeInForce": "GTC"}

        try:
            body = self._request("POST", f"/v3/accounts/{self.account_id}/orders",
                                 json={"order": order}, idempotent_write=True)
        except PermanentError as exc:
            if exc.context.get("code") == "CLIENT_ORDER_ID_ALREADY_EXISTS":
                # The retry did its job: the venue already has this order.
                existing = self.query_order(intent.client_order_id)
                if existing:
                    return existing
                return SubmitResult(state=OrderState.UNKNOWN,
                                    reject_reason="DUPLICATE_ID_UNRESOLVED")
            return SubmitResult(state=OrderState.REJECTED, reject_reason=str(exc.message))

        if "orderRejectTransaction" in body:
            rej = body["orderRejectTransaction"]
            return SubmitResult(state=OrderState.REJECTED,
                                reject_reason=rej.get("reason", "REJECTED"), raw=body)

        fill_txn = body.get("orderFillTransaction")
        create = body.get("orderCreateTransaction", {})
        venue_id = (fill_txn or create).get("orderID") or create.get("id")
        self._last_txn_id = str(body.get("lastTransactionID", self._last_txn_id))

        if not fill_txn:
            return SubmitResult(state=OrderState.ACKED, venue_order_id=venue_id, raw=body)

        filled_units = abs(dec(fill_txn.get("units", "0")))
        fill = Fill(
            client_order_id=intent.client_order_id,
            venue_order_id=str(venue_id),
            instrument=intent.instrument, side=intent.side,
            lots=filled_units / inst.contract_size,
            price=dec(fill_txn.get("price", "0")),
            ts_ns=_rfc3339_ns(fill_txn.get("time", "")),
            received_ns=wall_ns(),
            commission=abs(dec(fill_txn.get("commission", "0"))),
            financing=dec(fill_txn.get("financing", "0")),
            liquidity_flag=fill_txn.get("fullVWAP", ""),
        )
        requested = inst.units(intent.lots)
        state = OrderState.FILLED if filled_units >= requested else OrderState.PARTIAL

        # A fill and a REJECTED protective stop arrive in the same response.
        # OANDA fills the market order and reports the stop's rejection as a
        # SIBLING transaction (stopLossOnFillRejectTransaction, or
        # stopLossOrderRejectTransaction) for STOP_LOSS_ON_FILL_LOSS,
        # LOSS_TOLERANCE_EXCEEDED, or a stop too close to the market. Inspecting
        # only "orderRejectTransaction" meant the caller was told FILLED, the
        # position was recorded as protected, and the book was NAKED until the
        # next reconcile -- up to an hour later, at which point it received a
        # generic emergency stop rather than the strategy's.
        stop_rejects = [k for k in body
                        if k.endswith("RejectTransaction") and "stopLoss" in k.lower()]
        stop_confirmed = intent.stop_loss is None or not stop_rejects
        if stop_rejects:
            reasons = "; ".join(
                f"{k}:{(body.get(k) or {}).get('reason', 'REJECTED')}" for k in stop_rejects)
            self._log_stop_rejection(intent, reasons)

        return SubmitResult(state=state, venue_order_id=str(venue_id), fills=[fill],
                            raw=body, venue_ts_ns=fill.ts_ns,
                            stop_confirmed=stop_confirmed,
                            stop_reject_reason=(reasons if stop_rejects else None))

    def _log_stop_rejection(self, intent, reasons: str) -> None:
        """Surface a rejected protective stop as loudly as the transport allows."""
        import logging
        logging.getLogger(__name__).error(
            "OANDA filled %s %s but REJECTED its protective stop (%s). The position "
            "is unprotected until the stop is re-asserted.",
            intent.side.value, intent.instrument, reasons)

    def query_order(self, client_order_id: str) -> Optional[SubmitResult]:
        """Resolve an unknown outcome by asking the venue, never by resending."""
        try:
            body = self._request(
                "GET", f"/v3/accounts/{self.account_id}/orders",
                params={"clientExtensions.id": client_order_id, "state": "ALL", "count": 5},
            )
        except BrokerError:
            return None
        orders = body.get("orders", [])
        if not orders:
            return None
        o = orders[0]
        state_map = {
            "PENDING": OrderState.ACKED, "FILLED": OrderState.FILLED,
            "TRIGGERED": OrderState.ACKED, "CANCELLED": OrderState.CANCELLED,
        }
        state = state_map.get(o.get("state", ""), OrderState.UNKNOWN)
        fills: List[Fill] = []
        if state is OrderState.FILLED:
            # Without reconstructing the fill, the OMS transitions the order to
            # FILLED with zero lots and never fires on_fill: the venue holds a
            # position the system believes has no size.
            inst = self._instruments.get(o.get("instrument", ""))
            cs = inst.contract_size if inst else D("100000")
            units = abs(dec(o.get("units", "0")))
            price = o.get("averageFillPrice") or o.get("price") or "0"
            fills.append(Fill(
                client_order_id=client_order_id,
                venue_order_id=str(o.get("id")),
                instrument=o.get("instrument", ""),
                side=Side.BUY if dec(o.get("units", "0")) > 0 else Side.SELL,
                lots=units / cs, price=dec(price),
                ts_ns=_rfc3339_ns(o.get("filledTime") or o.get("createTime", "")),
                received_ns=wall_ns(),
            ))
            if not units:
                # A FILLED order with no units is incoherent; do not claim a fill.
                state = OrderState.UNKNOWN
                fills = []
        return SubmitResult(state=state, venue_order_id=str(o.get("id")),
                            fills=fills, raw=o)

    def cancel(self, client_order_id: str) -> bool:
        res = self.query_order(client_order_id)
        if not res or not res.venue_order_id:
            return False
        try:
            self._request("PUT",
                          f"/v3/accounts/{self.account_id}/orders/{res.venue_order_id}/cancel")
            return True
        except BrokerError:
            return False

    def close_position(self, instrument: str, lots: Optional[Decimal] = None,
                       *, reason: str = "") -> SubmitResult:
        inst = self.instrument(instrument)
        current = [p for p in self.positions() if p.instrument == instrument]
        if not current:
            return SubmitResult(state=OrderState.REJECTED, reject_reason="NO_POSITION")
        pos = current[0]
        amount = "ALL" if lots is None else str(int(inst.units(min(dec(lots), pos.lots))))
        key = "longUnits" if pos.side is Side.BUY else "shortUnits"
        body = self._request("PUT", f"/v3/accounts/{self.account_id}/positions/{instrument}/close",
                             json={key: amount}, idempotent_write=True)
        return SubmitResult(state=OrderState.FILLED, raw=body)

    def modify_position(self, instrument: str, *, stop_loss: Optional[Decimal] = None,
                        take_profit: Optional[Decimal] = None) -> bool:
        """Modify the protective orders. A stop may only move toward safety.

        The tighten-only guard lives here, in the adapter, rather than only in
        the caller: widening a stop mid-trade is how a bounded loss becomes an
        unbounded one, and the reconciler's own repair path used to do exactly
        that on every pass.
        """
        inst = self.instrument(instrument)
        body = self._request("GET", f"/v3/accounts/{self.account_id}/openTrades")
        trades = [t for t in body.get("trades", []) if t["instrument"] == instrument]
        if not trades:
            return False
        ok = True
        for t in trades:
            payload: Dict[str, Any] = {}
            if stop_loss is not None:
                new_stop = inst.round_price(stop_loss)
                units = dec(t.get("currentUnits", "0"))
                side = Side.BUY if units > 0 else Side.SELL
                existing = t.get("stopLossOrder", {}).get("price")
                if existing is not None:
                    current = dec(existing)
                    widening = (new_stop < current if side is Side.BUY
                                else new_stop > current)
                    if widening:
                        raise PermanentError(
                            f"refusing to widen the {side.value} stop on {instrument} "
                            f"from {current} to {new_stop}",
                            instrument=instrument)
                payload["stopLoss"] = {"price": str(new_stop), "timeInForce": "GTC"}
            if take_profit is not None:
                payload["takeProfit"] = {"price": str(inst.round_price(take_profit)),
                                         "timeInForce": "GTC"}
            if not payload:
                continue
            try:
                self._request("PUT",
                              f"/v3/accounts/{self.account_id}/trades/{t['id']}/orders",
                              json=payload, idempotent_write=True)
            except BrokerError:
                ok = False
        return ok

    @property
    def supports_closed_trade_history(self) -> bool:
        return True

    def fetch_closed_trades(self, since_id: str = "") -> tuple[List[ClosedTrade], str]:
        """Completed round trips from OANDA's own trade history.

        This is what makes the post-mortem, the lesson store and the proposal
        engine work outside the simulator. ``realizedPL`` already nets financing
        and commission, which matches the convention used everywhere else in this
        system: a trade's P&L is what actually reached the balance.
        """
        params: Dict[str, Any] = {"state": "CLOSED", "count": 500}
        if since_id:
            params["beforeID"] = None
        try:
            body = self._request("GET", f"/v3/accounts/{self.account_id}/trades",
                                 params=params)
        except BrokerError:
            return [], since_id
        cursor = int(since_id) if str(since_id).isdigit() else 0
        out: List[ClosedTrade] = []
        highest = cursor
        for t in body.get("trades", []):
            try:
                trade_id = int(t.get("id", 0))
            except (TypeError, ValueError):
                continue
            if trade_id <= cursor:
                continue
            highest = max(highest, trade_id)
            symbol = t.get("instrument", "")
            inst = self._instruments.get(symbol)
            cs = inst.contract_size if inst else D("100000")
            pip = inst.pip if inst else D("0.0001")
            units = dec(t.get("initialUnits", "0"))
            side = Side.BUY if units > 0 else Side.SELL
            entry = dec(t.get("price", "0"))
            exit_price = dec(t.get("averageClosePrice", t.get("price", "0")))
            pnl = dec(t.get("realizedPL", "0"))
            financing = dec(t.get("financing", "0"))
            delta = (exit_price - entry) * D(side.sign)
            out.append(ClosedTrade(
                trade_id=str(trade_id),
                strategy=(t.get("clientExtensions", {}) or {}).get("tag", ""),
                instrument=symbol, side=side, lots=abs(units) / cs,
                entry_price=entry, exit_price=exit_price,
                opened_ns=_rfc3339_ns(t.get("openTime", "")),
                closed_ns=_rfc3339_ns(t.get("closeTime", "")),
                pnl=pnl, pnl_pips=(delta / pip) if pip else ZERO,
                financing=financing,
                exit_reason=self._exit_reason(t),
            ))
        out.sort(key=lambda c: c.closed_ns)
        return out, str(highest)

    @staticmethod
    def _exit_reason(trade: Dict[str, Any]) -> str:
        """Which protective order closed the trade, when the venue says so."""
        for txn in trade.get("closingTransactionIDs", []) or []:
            pass
        if trade.get("stopLossOrder", {}).get("state") == "FILLED":
            return "stop_loss"
        if trade.get("takeProfitOrder", {}).get("state") == "FILLED":
            return "take_profit"
        if trade.get("trailingStopLossOrder", {}).get("state") == "FILLED":
            return "trail_stop"
        return "closed"

    def transactions_since(self, last_id: str) -> Iterable[Dict[str, Any]]:
        body = self._request("GET", f"/v3/accounts/{self.account_id}/transactions/sinceid",
                             params={"id": last_id or "0"})
        txns = body.get("transactions", [])
        if txns:
            self._last_txn_id = str(txns[-1].get("id", last_id))
        return txns

    def close(self) -> None:
        self._client.close()
