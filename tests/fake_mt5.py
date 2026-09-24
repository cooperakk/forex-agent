"""A fake MetaTrader 5 terminal, good enough to test the adapter against.

The real `MetaTrader5` package is a Windows-only binary. Without this, the MT5
adapter -- which is the path EVERY MetaTrader broker uses, including AMarkets
and Alpari -- ships completely untested, and every one of its symbol
translations, filling modes and stop-level rules is discovered in production
with money on.

This fake implements the parts of the API the adapter touches, and it is
deliberately AWKWARD in the ways real terminals are awkward:

* symbols carry a broker-specific suffix
* a minimum stop distance is enforced and orders inside it are rejected
* an unsupported filling mode is rejected
* `positions_get` returns namedtuple-ish objects, not dicts
* `order_send` returns a retcode, and success is one specific integer

It is not a market simulator -- `PaperBroker` is, and it is far more
adversarial. This exists only to prove the adapter speaks the protocol
correctly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_INVALID_FILL = 10030
TRADE_RETCODE_NO_MONEY = 10019

TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
# Values you SEND in an order request.
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2
ORDER_TIME_GTC = 0

# Flags the terminal REPORTS in symbol_info().filling_mode. A different
# namespace from the values above, and numerically overlapping with them --
# which is exactly how a FOK-only broker came to be sent IOC on every order.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
SYMBOL_FILLING_RETURN = 4

_SEND_TO_MASK = {ORDER_FILLING_FOK: SYMBOL_FILLING_FOK,
                 ORDER_FILLING_IOC: SYMBOL_FILLING_IOC,
                 ORDER_FILLING_RETURN: SYMBOL_FILLING_RETURN}
POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

# The real module's timeframe constants, as the adapter reads them by name.
TIMEFRAME_M1 = 1
TIMEFRAME_M5 = 5
TIMEFRAME_M15 = 15
TIMEFRAME_M30 = 30
TIMEFRAME_H1 = 16385
TIMEFRAME_H4 = 16388
TIMEFRAME_D1 = 16408
TIMEFRAME_W1 = 32769
_TIMEFRAME_SECONDS = {
    TIMEFRAME_M1: 60, TIMEFRAME_M5: 300, TIMEFRAME_M15: 900, TIMEFRAME_M30: 1800,
    TIMEFRAME_H1: 3600, TIMEFRAME_H4: 14400, TIMEFRAME_D1: 86400, TIMEFRAME_W1: 604800,
}


@dataclass
class _Symbol:
    name: str
    digits: int = 5
    trade_contract_size: float = 100000.0
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 100.0
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    #: A BITMASK of permitted modes, as a real terminal reports it.
    filling_mode: int = SYMBOL_FILLING_IOC
    #: SYMBOL_TRADE_MODE: 0 disabled, 4 full. Real terminals list retired and
    #: indicative symbols with mode 0 beside the tradeable ones.
    trade_mode: int = 4
    #: Overnight swap in POINTS (swap_mode 1), long and short.
    swap_mode: int = 1
    swap_long: float = -7.2
    swap_short: float = 2.1


@dataclass
class _Tick:
    bid: float
    ask: float
    time_msc: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class _Position:
    ticket: int
    symbol: str
    type: int
    volume: float
    price_open: float
    sl: float = 0.0
    tp: float = 0.0
    profit: float = 0.0
    magic: int = 0
    comment: str = ""
    identifier: int = 0
    time: int = field(default_factory=lambda: int(time.time()))
    time_msc: int = field(default_factory=lambda: int(time.time() * 1000))
    swap: float = 0.0

    def __post_init__(self):
        if not self.identifier:
            self.identifier = self.ticket


@dataclass
class _Result:
    retcode: int
    order: int = 0
    volume: float = 0.0
    price: float = 0.0
    comment: str = ""


@dataclass
class _Account:
    login: int = 1000001
    currency: str = "USD"
    balance: float = 10000.0
    equity: float = 10000.0
    margin: float = 0.0
    margin_free: float = 10000.0
    leverage: int = 200
    profit: float = 0.0
    #: ACCOUNT_TRADE_MODE: 0 demo, 1 contest, 2 real. Every real terminal
    #: reports it; the bridge envelope refuses to open a position without it.
    trade_mode: int = 0
    server: str = "FakeBroker-Demo"
    company: str = "Fake Broker Ltd"


@dataclass
class _Deal:
    ticket: int
    symbol: str
    type: int
    volume: float
    price: float
    commission: float = 0.0
    swap: float = 0.0
    profit: float = 0.0
    magic: int = 0
    comment: str = ""
    time_msc: int = field(default_factory=lambda: int(time.time() * 1000))
    entry: int = 0
    #: The order that produced the deal; the real TradeDeal carries it and the
    #: adapter's query_order reads it.
    order: int = 0
    #: The position the deal belongs to. Entry and exit deals of one round trip
    #: share it; the adapter's realised history is grouped on it.
    position_id: int = 0
    fee: float = 0.0

    def __post_init__(self):
        if not self.order:
            self.order = self.ticket
        if not self.position_id:
            self.position_id = self.ticket


@dataclass
class _Terminal:
    path: str = "C:/Program Files/MetaTrader 5/terminal64.exe"
    company: str = "MetaQuotes Software Corp."
    name: str = "MetaTrader 5"
    connected: bool = True


class FakeMT5:
    """One broker's terminal.

    `suffix` and `stops_level` are the two settings that differ most between
    real brokers and cause the most production surprises, so they are the two
    the tests vary.
    """

    #: The adapter reads these off the module, so the fake carries them too.
    TRADE_RETCODE_DONE = TRADE_RETCODE_DONE
    TRADE_RETCODE_INVALID_STOPS = TRADE_RETCODE_INVALID_STOPS
    TRADE_RETCODE_INVALID_FILL = TRADE_RETCODE_INVALID_FILL
    TRADE_RETCODE_NO_MONEY = TRADE_RETCODE_NO_MONEY
    TRADE_ACTION_DEAL = TRADE_ACTION_DEAL
    TRADE_ACTION_SLTP = TRADE_ACTION_SLTP
    ORDER_TYPE_BUY = ORDER_TYPE_BUY
    ORDER_TYPE_SELL = ORDER_TYPE_SELL
    ORDER_FILLING_FOK = ORDER_FILLING_FOK
    ORDER_FILLING_IOC = ORDER_FILLING_IOC
    ORDER_FILLING_RETURN = ORDER_FILLING_RETURN
    ORDER_TIME_GTC = ORDER_TIME_GTC
    SYMBOL_FILLING_FOK = SYMBOL_FILLING_FOK
    SYMBOL_FILLING_IOC = SYMBOL_FILLING_IOC
    SYMBOL_FILLING_RETURN = SYMBOL_FILLING_RETURN
    POSITION_TYPE_BUY = POSITION_TYPE_BUY
    POSITION_TYPE_SELL = POSITION_TYPE_SELL
    TIMEFRAME_M1 = TIMEFRAME_M1
    TIMEFRAME_M5 = TIMEFRAME_M5
    TIMEFRAME_M15 = TIMEFRAME_M15
    TIMEFRAME_M30 = TIMEFRAME_M30
    TIMEFRAME_H1 = TIMEFRAME_H1
    TIMEFRAME_H4 = TIMEFRAME_H4
    TIMEFRAME_D1 = TIMEFRAME_D1
    TIMEFRAME_W1 = TIMEFRAME_W1

    def __init__(self, *, suffix: str = "", stops_level: int = 0,
                 supported_filling: int = ORDER_FILLING_IOC,
                 contract_size: float = 100000.0,
                 symbols: Optional[List[str]] = None,
                 stops_by_symbol: Optional[Dict[str, int]] = None,
                 server_utc_offset_sec: int = 3 * 3600,
                 history_bars: int = 400,
                 disabled_symbols: Optional[List[str]] = None,
                 commission_per_lot: float = 3.5) -> None:
        self.suffix = suffix
        #: Per-side commission the fake charges on every deal, like an ECN book.
        self.commission_per_lot = commission_per_lot
        #: Real terminals stamp bars and ticks in the broker's SERVER clock and
        #: the Python package presents that number as though it were UTC. The
        #: fake does the same, so the adapter's conversion is exercised.
        self.server_utc_offset_sec = server_utc_offset_sec
        self.history_bars = history_bars
        self.selected: List[str] = []
        self.rates_requests: List[tuple] = []
        self._supported_filling = supported_filling
        cores = symbols or ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"]
        self._symbols: Dict[str, _Symbol] = {}
        for core in cores:
            digits = 3 if core.endswith("JPY") else 5
            self._symbols[core + suffix] = _Symbol(
                name=core + suffix, digits=digits,
                trade_contract_size=contract_size,
                trade_stops_level=stops_level,
                filling_mode=_SEND_TO_MASK.get(supported_filling,
                                               SYMBOL_FILLING_IOC))
        for core, level in (stops_by_symbol or {}).items():
            name = core + suffix
            if name in self._symbols:
                self._symbols[name].trade_stops_level = level
        self._ticks: Dict[str, _Tick] = {
            "EURUSD": _Tick(1.10000, 1.10012),
            "GBPUSD": _Tick(1.27000, 1.27015),
            "USDJPY": _Tick(150.000, 150.018),
            "AUDUSD": _Tick(0.65000, 0.65014),
            "USDCHF": _Tick(0.88000, 0.88016),
        }
        # Retired/indicative symbols: listed by the terminal, not tradeable.
        for name in (disabled_symbols or []):
            digits = 3 if name.endswith("JPY") else 5
            self._symbols[name] = _Symbol(name=name, digits=digits, trade_mode=0,
                                          trade_contract_size=contract_size)
        self._positions: List[_Position] = []
        self._deals: List[_Deal] = []
        self._next_ticket = 500001
        self.account = _Account()
        self.sent_requests: List[Dict[str, Any]] = []
        self._last_error = (0, "ok")

    # -- API surface the adapter uses ---------------------------------- #

    def initialize(self, *args, **kwargs) -> bool:
        return True

    def shutdown(self) -> None:
        return None

    def last_error(self):
        return self._last_error

    def symbols_get(self, *args, **kwargs):
        return list(self._symbols.values())

    def symbol_info(self, name: str):
        return self._symbols.get(name)

    def symbol_info_tick(self, name: str):
        core = name[: -len(self.suffix)] if self.suffix and name.endswith(self.suffix) \
            else name
        tick = self._ticks.get(core)
        if tick is None:
            return None
        # Stamp the tick NOW in server time, as a live terminal would.
        tick.time_msc = int((time.time() + self.server_utc_offset_sec) * 1000)
        return tick

    def symbol_select(self, name: str, enable: bool = True) -> bool:
        self.selected.append(name)
        return name in self._symbols

    def copy_rates_from_pos(self, name: str, timeframe: int, start_pos: int, count: int):
        """``count`` bars ending with the one still forming, oldest first.

        Rows are dicts with the real terminal's field names. Times are the bar
        OPEN in server seconds. A deterministic gentle drift around the tick
        price keeps the OHLC coherent (high >= max(open, close), etc.).
        """
        core = name[: -len(self.suffix)] if self.suffix and name.endswith(self.suffix) \
            else name
        if name not in self._symbols or core not in self._ticks:
            self._last_error = (4301, "unknown symbol")
            return None
        self.rates_requests.append((name, timeframe, start_pos, count))
        interval = _TIMEFRAME_SECONDS.get(timeframe)
        if interval is None:
            return None
        count = min(int(count), self.history_bars)
        now_server = time.time() + self.server_utc_offset_sec
        forming_start = int(now_server // interval) * interval
        mid = (self._ticks[core].bid + self._ticks[core].ask) / 2.0
        step = mid * 0.0004
        rows = []
        for i in range(count):
            k = count - 1 - i          # bars back from the forming one
            start = forming_start - k * interval
            o = mid + step * ((k * 7) % 11 - 5) / 5.0
            c = mid + step * ((k * 13) % 11 - 5) / 5.0
            h = max(o, c) + step * 0.4
            l = min(o, c) - step * 0.4
            rows.append({"time": start, "open": o, "high": h, "low": l, "close": c,
                         "tick_volume": 100 + k, "spread": 12, "real_volume": 0})
        return rows

    def account_info(self):
        return self.account

    def terminal_info(self):
        return _Terminal()

    def positions_get(self, symbol: Optional[str] = None, **kwargs):
        if symbol is None:
            return list(self._positions)
        return [p for p in self._positions if p.symbol == symbol]

    def history_deals_get(self, *args, **kwargs):
        return list(self._deals)

    def orders_get(self, *args, **kwargs):
        return []

    def order_send(self, request: Dict[str, Any]):
        self.sent_requests.append(dict(request))
        action = request.get("action")
        symbol = request.get("symbol", "")
        info = self._symbols.get(symbol)
        if info is None:
            self._last_error = (4106, "unknown symbol")
            return _Result(retcode=10013, comment="Invalid request")

        if action == TRADE_ACTION_SLTP:
            for pos in self._positions:
                if pos.ticket == request.get("position"):
                    new_sl = float(request.get("sl") or 0.0)
                    if new_sl and not self._stop_is_legal(info, pos, new_sl):
                        return _Result(retcode=TRADE_RETCODE_INVALID_STOPS,
                                       comment="Invalid stops")
                    pos.sl = new_sl or pos.sl
                    if request.get("tp"):
                        pos.tp = float(request["tp"])
                    return _Result(retcode=TRADE_RETCODE_DONE, order=pos.ticket)
            return _Result(retcode=10013, comment="Position not found")

        filling = request.get("type_filling")
        if filling is not None and filling != self._supported_filling:
            # Real terminals do exactly this, and the resulting "Unsupported
            # filling mode" is the single most common first-run failure.
            return _Result(retcode=TRADE_RETCODE_INVALID_FILL,
                           comment="Unsupported filling mode")

        order_type = request.get("type")
        volume = float(request.get("volume", 0))
        tick = self.symbol_info_tick(symbol)
        price = tick.ask if order_type == ORDER_TYPE_BUY else tick.bid

        sl = float(request.get("sl") or 0.0)
        if sl:
            point = 10 ** (-info.digits)
            distance = abs(price - sl)
            if info.trade_stops_level and distance < info.trade_stops_level * point:
                return _Result(retcode=TRADE_RETCODE_INVALID_STOPS,
                               comment="Invalid stops")

        # Closing an existing position?
        closing = [p for p in self._positions
                   if p.symbol == symbol and p.ticket == request.get("position")]
        if closing:
            pos = closing[0]
            closed = min(volume, pos.volume)
            pos.volume -= closed
            if pos.volume <= 1e-9:
                self._positions.remove(pos)
            ticket = self._next_ticket
            self._next_ticket += 1
            sign = 1.0 if pos.type == POSITION_TYPE_BUY else -1.0
            profit = (price - pos.price_open) * sign * closed * info.trade_contract_size
            self._deals.append(_Deal(ticket=ticket, symbol=symbol,
                                     type=order_type, volume=closed, price=price,
                                     magic=request.get("magic", 0),
                                     comment=request.get("comment", ""), entry=1,
                                     position_id=pos.ticket, profit=round(profit, 2),
                                     commission=-self.commission_per_lot * closed))
            return _Result(retcode=TRADE_RETCODE_DONE, order=ticket,
                           volume=closed, price=price)

        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions.append(_Position(
            ticket=ticket, symbol=symbol,
            type=POSITION_TYPE_BUY if order_type == ORDER_TYPE_BUY else POSITION_TYPE_SELL,
            volume=volume, price_open=price, sl=sl,
            tp=float(request.get("tp") or 0.0),
            magic=request.get("magic", 0), comment=request.get("comment", "")))
        self._deals.append(_Deal(ticket=ticket, symbol=symbol, type=order_type,
                                 volume=volume, price=price,
                                 magic=request.get("magic", 0),
                                 comment=request.get("comment", ""), entry=0,
                                 position_id=ticket,
                                 commission=-self.commission_per_lot * volume))
        return _Result(retcode=TRADE_RETCODE_DONE, order=ticket,
                       volume=volume, price=price)

    # -- tick history ------------------------------------------------------ #

    COPY_TICKS_INFO = 1

    def copy_ticks_range(self, name: str, date_from, date_to, flags: int = 1):
        """Synthetic bid/ask ticks, one every 15 s, stamped in SERVER time (ms)."""
        core = name[: -len(self.suffix)] if self.suffix and name.endswith(self.suffix) \
            else name
        if name not in self._symbols or core not in self._ticks:
            self._last_error = (4301, "unknown symbol")
            return None
        start = int(date_from.timestamp() * 1000)
        end = int(date_to.timestamp() * 1000)
        if end <= start:
            return []
        mid = (self._ticks[core].bid + self._ticks[core].ask) / 2.0
        spread = self._ticks[core].ask - self._ticks[core].bid
        rows = []
        t = start
        k = 0
        while t < end and len(rows) < 200_000:
            m = mid * (1.0 + 0.00002 * (((k * 37) % 11) - 5))
            half = spread / 2.0 * (1.0 + 0.5 * ((k * 13) % 3))
            rows.append({"time": t // 1000, "time_msc": t, "bid": m - half, "ask": m + half,
                         "last": 0.0, "volume": 0, "flags": 6})
            t += 15_000
            k += 1
        return rows

    # -- helpers -------------------------------------------------------- #

    @staticmethod
    def _stop_is_legal(info: _Symbol, pos: _Position, stop: float) -> bool:
        if not info.trade_stops_level:
            return True
        point = 10 ** (-info.digits)
        return abs(pos.price_open - stop) >= info.trade_stops_level * point

    def set_price(self, core: str, bid: float, ask: float) -> None:
        self._ticks[core] = _Tick(bid, ask)


def install(monkeypatch, fake: FakeMT5) -> None:
    """Make `import MetaTrader5` return the fake."""
    import sys
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
