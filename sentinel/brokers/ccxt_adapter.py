"""CCXT adapter for crypto venues.

Included because access from Iran to an offshore FX broker is the project's
single hardest constraint (research brief, section B-4), and a crypto venue is
often the only one that survives the full deposit -> hold -> *withdraw* cycle
test. The economics differ from FX and must not be assumed to transfer: funding
rates replace swap, volatility is several times higher, and venue risk is
larger, not smaller.

Most CCXT venues accept ``clientOrderId`` and reject duplicates, so the
idempotency guarantee usually holds -- but it is verified per-exchange at
construction time rather than assumed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from ..core.clock import wall_ns
from ..core.errors import (
    BrokerError, ConfigError, ConversionMissingError, PermanentError, TransientError,
    UnknownOutcomeError,
)
from ..core.money import AssetClass, D, Instrument, dec
from ..core.types import (
    AccountState, Fill, Order, OrderIntent, OrderState, OrderType, Position, Quote, Side,
)
from .base import Broker, BrokerCapabilities, SubmitResult


class CCXTBroker(Broker):
    def __init__(self, *, exchange_id: str, api_key: Optional[str] = None,
                 secret: Optional[str] = None, password: Optional[str] = None,
                 account_currency: str = "USDT", sandbox: bool = True,
                 default_type: str = "swap") -> None:
        try:
            import ccxt  # type: ignore
        except ImportError as exc:
            raise ConfigError("ccxt is not installed: pip install ccxt") from exc
        import os

        if not hasattr(ccxt, exchange_id):
            raise ConfigError(f"unknown exchange {exchange_id!r}")
        klass = getattr(ccxt, exchange_id)
        self._ex = klass({
            "apiKey": api_key or os.environ.get(f"{exchange_id.upper()}_API_KEY", ""),
            "secret": secret or os.environ.get(f"{exchange_id.upper()}_SECRET", ""),
            "password": password or os.environ.get(f"{exchange_id.upper()}_PASSWORD") or None,
            "enableRateLimit": True,
            "options": {"defaultType": default_type},
        })
        # Whether the sandbox was actually ENGAGED, not whether it was asked
        # for: an exchange with no sandbox silently stays live, and reporting
        # "demo" for a live venue is the worst possible direction to be wrong.
        self._sandbox = bool(sandbox and self._ex.has.get("sandbox"))
        if self._sandbox:
            self._ex.set_sandbox_mode(True)
        self._ccy = account_currency
        self._markets = self._ex.load_markets()
        self._instruments: Dict[str, Instrument] = {}
        for symbol, m in self._markets.items():
            if not m.get("active"):
                continue
            precision = m.get("precision", {}).get("price")
            tick = D("10") ** (-int(precision)) if isinstance(precision, (int, float)) else D("0.01")
            limits = m.get("limits", {}).get("amount", {}) or {}
            self._instruments[symbol] = Instrument(
                symbol=symbol, base=m.get("base", ""), quote=m.get("quote", self._ccy),
                asset_class=AssetClass.CRYPTO,
                pip=tick, tick=tick, contract_size=D("1"),
                min_lot=dec(limits.get("min") or "0.001"),
                lot_step=dec(limits.get("min") or "0.001"),
                max_lot=dec(limits.get("max") or "1000000"),
                margin_rate=D("0.1"), venue=exchange_id,
            )
        # `has["createOrder"]` is True for every ccxt exchange and says nothing
        # about duplicate rejection. Only a venue that can FETCH an order by
        # clientOrderId can be relied on to reject a duplicate, so that is what
        # is tested. Anything else drops to the degraded protocol.
        self.capabilities = BrokerCapabilities(
            supports_client_order_id=bool(
                self._ex.has.get("fetchOrder") and self._ex.has.get("createOrder")),
            supports_server_side_stop=bool(self._ex.has.get("createStopOrder")
                                           or self._ex.has.get("createStopLossOrder")),
            supports_transaction_stream=bool(self._ex.has.get("fetchMyTrades")),
            supports_partial_close=True, supports_fractional_lots=True,
            min_lot=D("0.001"), lot_step=D("0.001"), name=f"ccxt:{exchange_id}",
            notes="Crypto venue. Funding replaces swap; volatility and venue "
                  "risk are materially higher than FX majors.",
        )

    def instruments(self) -> Dict[str, Instrument]:
        return dict(self._instruments)

    def quote(self, symbol: str) -> Quote:
        t = self._ex.fetch_ticker(symbol)
        bid, ask = t.get("bid"), t.get("ask")
        if bid is None or ask is None:
            raise BrokerError(f"incomplete ticker for {symbol}", code="NO_QUOTE")
        return Quote(instrument=symbol, bid=dec(bid), ask=dec(ask),
                     ts_ns=int(t.get("timestamp") or 0) * 1_000_000,
                     received_ns=wall_ns(), source=self.capabilities.name)

    def conversion_rate(self, quote_ccy: str, account_ccy: str) -> Decimal:
        if quote_ccy == account_ccy:
            return D("1")
        for sym, invert in ((f"{quote_ccy}/{account_ccy}", False),
                            (f"{account_ccy}/{quote_ccy}", True)):
            if sym in self._markets:
                q = self.quote(sym)
                return (D("1") / q.mid) if invert else q.mid
        raise ConversionMissingError(f"no {quote_ccy}->{account_ccy} market")

    def account(self) -> AccountState:
        bal = self._ex.fetch_balance()
        total = dec(bal.get("total", {}).get(self._ccy, 0))
        free = dec(bal.get("free", {}).get(self._ccy, 0))
        return AccountState(account_id=self.capabilities.name, currency=self._ccy,
                            balance=total, equity=total, margin_used=total - free,
                            margin_available=free, open_positions=len(self.positions()),
                            ts_ns=wall_ns(), source="ccxt",
                            account_type="demo" if self._sandbox else "live",
                            venue_name=self._ex.id)

    def positions(self) -> List[Position]:
        """Open positions, WITH the protective stop that is live at the venue.

        Without reading the open stop orders every position reads as
        unprotected, so the tighten-only guard has nothing to compare against
        and the reconciler's repair path cannot tell "no stop" from "a stop I
        did not look for".
        """
        if not self._ex.has.get("fetchPositions"):
            return []
        stops = self._open_stops()
        out: List[Position] = []
        for p in self._ex.fetch_positions() or []:
            contracts = dec(p.get("contracts") or 0)
            if contracts == 0:
                continue
            symbol = p["symbol"]
            stop = p.get("stopLossPrice") or stops.get(symbol)
            out.append(Position(
                instrument=symbol,
                side=Side.BUY if p.get("side") == "long" else Side.SELL,
                lots=abs(contracts), entry_price=dec(p.get("entryPrice") or 0),
                opened_ns=int(p.get("timestamp") or 0) * 1_000_000,
                stop_loss=dec(stop) if stop else None,
                take_profit=(dec(p["takeProfitPrice"]) if p.get("takeProfitPrice")
                             else None),
                broker_stop_confirmed=stop is not None,
                venue_position_id=str(p.get("id") or ""),
            ))
        return out

    def _open_stops(self) -> Dict[str, Any]:
        """Trigger price of each live reduce-only stop order, by symbol."""
        if not self._ex.has.get("fetchOpenOrders"):
            return {}
        try:
            orders = self._ex.fetch_open_orders() or []
        except Exception as exc:  # noqa: BLE001
            # A failed read is NOT evidence that the stops are missing. Returning
            # {} here would mark every position naked, which cascades into an
            # entry veto, an emergency-stop repair, and a halt -- all from one
            # transient HTTP failure.
            raise TransientError(
                "could not read the open orders, so the protective stops on the open "
                "positions are unknown; this is not evidence that they are missing",
                detail=str(exc)) from exc
        out: Dict[str, Any] = {}
        for o in orders:
            trigger = (o.get("triggerPrice") or o.get("stopPrice")
                       or (o.get("info") or {}).get("stopPrice"))
            kind = str(o.get("type") or "").lower()
            if trigger and ("stop" in kind or o.get("reduceOnly")):
                out.setdefault(o.get("symbol"), trigger)
        return out

    def open_orders(self) -> List[Order]:
        return []

    def submit(self, intent: OrderIntent, *, timeout_ms: int = 5000) -> SubmitResult:
        import ccxt  # type: ignore

        params: Dict[str, Any] = {"clientOrderId": intent.client_order_id}
        if intent.stop_loss is not None:
            params["stopLossPrice"] = float(intent.stop_loss)
        if intent.take_profit is not None:
            params["takeProfitPrice"] = float(intent.take_profit)
        try:
            o = self._ex.create_order(
                intent.instrument,
                "market" if intent.order_type is OrderType.MARKET else "limit",
                "buy" if intent.side is Side.BUY else "sell",
                float(intent.lots),
                float(intent.limit_price) if intent.limit_price else None,
                params,
            )
        except ccxt.DuplicateOrderId:
            existing = self.query_order(intent.client_order_id)
            return existing or SubmitResult(state=OrderState.UNKNOWN,
                                            reject_reason="DUPLICATE_ID_UNRESOLVED")
        except ccxt.InsufficientFunds as exc:
            return SubmitResult(state=OrderState.REJECTED, reject_reason=f"INSUFFICIENT_FUNDS:{exc}")
        except ccxt.InvalidOrder as exc:
            return SubmitResult(state=OrderState.REJECTED, reject_reason=f"INVALID_ORDER:{exc}")
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            raise UnknownOutcomeError("network failure on order submit",
                                      detail=str(exc)) from exc
        filled = dec(o.get("filled") or 0)
        state = (OrderState.FILLED if filled >= dec(intent.lots)
                 else OrderState.PARTIAL if filled > 0 else OrderState.ACKED)
        fills = []
        if filled > 0:
            fills.append(Fill(client_order_id=intent.client_order_id,
                              venue_order_id=str(o.get("id")),
                              instrument=intent.instrument, side=intent.side,
                              lots=filled, price=dec(o.get("average") or o.get("price") or 0),
                              ts_ns=int(o.get("timestamp") or 0) * 1_000_000,
                              commission=dec((o.get("fee") or {}).get("cost") or 0)))
        # Did the venue actually keep the protective stop? Many ccxt venues
        # silently DROP an unsupported param, so a stop passed in `params` and
        # never echoed back is a stop that does not exist. Reporting
        # stop_confirmed=True by default would mean the OMS's re-assert path
        # could never fire here and the position would be naked until the next
        # reconciliation.
        stop_confirmed = True
        stop_reason = None
        if intent.stop_loss is not None:
            echoed = (o.get("stopLossPrice") or o.get("stopPrice")
                      or (o.get("info") or {}).get("stopLossPrice")
                      or (o.get("params") or {}).get("stopLossPrice"))
            if echoed is None:
                stop_confirmed = False
                stop_reason = ("the venue did not echo a stop price back; ccxt "
                               "venues silently drop unsupported parameters")
        return SubmitResult(state=state, venue_order_id=str(o.get("id")), fills=fills,
                            raw=o, stop_confirmed=stop_confirmed,
                            stop_reject_reason=stop_reason)

    def query_order(self, client_order_id: str) -> Optional[SubmitResult]:
        try:
            o = self._ex.fetch_order(None, None, {"clientOrderId": client_order_id})
        except Exception:
            return None
        if not o:
            return None
        status = o.get("status")
        mapping = {"closed": OrderState.FILLED, "open": OrderState.ACKED,
                   "canceled": OrderState.CANCELLED, "rejected": OrderState.REJECTED}
        return SubmitResult(state=mapping.get(status, OrderState.UNKNOWN),
                            venue_order_id=str(o.get("id")), raw=o)

    def cancel(self, client_order_id: str) -> bool:
        try:
            self._ex.cancel_order(None, None, {"clientOrderId": client_order_id})
            return True
        except Exception:
            return False

    def close_position(self, instrument: str, lots: Optional[Decimal] = None,
                       *, reason: str = "") -> SubmitResult:
        pos = [p for p in self.positions() if p.instrument == instrument]
        if not pos:
            return SubmitResult(state=OrderState.REJECTED, reject_reason="NO_POSITION")
        p = pos[0]
        amount = float(min(dec(lots), p.lots)) if lots else float(p.lots)
        o = self._ex.create_order(instrument, "market",
                                  "sell" if p.side is Side.BUY else "buy", amount,
                                  None, {"reduceOnly": True})
        return SubmitResult(state=OrderState.FILLED, venue_order_id=str(o.get("id")), raw=o)

    def modify_position(self, instrument: str, *, stop_loss: Optional[Decimal] = None,
                        take_profit: Optional[Decimal] = None) -> bool:
        """Attach or replace the protective stop.

        Two defects lived here. The closing side was hard-coded to "sell", which
        is the wrong direction for every short, and the `return True` sat outside
        the guard, so a call that placed nothing reported success -- on which the
        reconciler then set ``broker_stop_confirmed = True`` for a position with
        no stop at all.
        """
        if not self.capabilities.supports_server_side_stop or stop_loss is None:
            return False
        pos = next((p for p in self.positions() if p.instrument == instrument), None)
        if pos is None:
            return False
        # A stop only ever moves toward safety.
        if pos.stop_loss is not None:
            widening = (dec(stop_loss) < pos.stop_loss if pos.side is Side.BUY
                        else dec(stop_loss) > pos.stop_loss)
            if widening:
                raise PermanentError(
                    f"refusing to widen the {pos.side.value} stop on {instrument}",
                    instrument=instrument)
        closing_side = "sell" if pos.side is Side.BUY else "buy"
        try:
            self._ex.create_order(
                instrument, "stop_market", closing_side, float(pos.lots), None,
                {"stopPrice": float(stop_loss), "reduceOnly": True,
                 "closePosition": True})
        except Exception:
            return False
        return True

    def transactions_since(self, last_id: str) -> Iterable[Dict[str, Any]]:
        if not self._ex.has.get("fetchMyTrades"):
            return []
        try:
            return self._ex.fetch_my_trades(since=int(last_id) if last_id.isdigit() else None)
        except Exception:
            return []
