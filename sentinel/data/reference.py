"""The reference-price guard: does the broker's price agree with the market?

Why it exists
-------------
Every risk number the engine computes starts from the broker's bid/ask. If
that price is wrong, every check downstream is precisely wrong: the stop
distance, the size, the spread veto, the reward-to-risk. The feed module
already refuses a quote that is OLD; nothing refused a quote that is fresh
and WRONG -- a feed that stopped moving while its timestamp kept advancing, a
bad tick, a symbol mapped to a different contract, a server that lost its
upstream and keeps publishing its last price. Those faults are invisible from
inside one feed. They are obvious next to a second, independent one.

What it may do
--------------
Compare the broker's mid with an independent reference (TradingView) and,
for a NEW entry only:

* ``shrink`` -- divergence above the shrink threshold: the size is multiplied
  by ``shrink_multiplier`` (default 0.5);
* ``block``  -- divergence above the block threshold: the risk engine vetoes
  the entry (rule ``reference_divergence``).

It never opens, enlarges, closes or modifies a position, and exits and
protection are never gated by it.

When the reference is missing it does NOTHING
---------------------------------------------
No connection, a delayed quote, a stale quote, an unmapped symbol, a closed
session: each is reported on the dashboard and has no effect on trading. The
reference is an unofficial source that can disappear at any time; letting its
absence stop trading would hand the account's uptime to a website's protocol.
Its absence says nothing about the broker's price, so it changes nothing.

Thresholds
----------
Measured in basis points of price, so one setting works for EUR/USD, USD/JPY
and gold alike, and never tighter than a multiple of the broker's own spread,
so a wide-spread exotic is not flagged for being an exotic:

    shrink when  |broker_mid - reference_mid| > max(shrink_bp, k_s * spread_bp)
    block  when  |broker_mid - reference_mid| > max(block_bp,  k_b * spread_bp)

Defaults: 5 bp / 12 bp, k_s = 2, k_b = 4. For EUR/USD near 1.10 that is about
5.5 and 13 pips. Two independent FX feeds normally agree to well under a pip.
"""

from __future__ import annotations

import statistics
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Callable, Deque, Dict, Iterable, Mapping, Optional

from ..core.clock import wall_ns
from .tradingview import default_symbol, validate_symbol

D = Decimal
_SEC = 1_000_000_000


@dataclass
class ReferenceCheck:
    instrument: str
    symbol: str = ""
    # ok | shrink | block | stale | delayed | closed | unavailable | unmapped
    # | no_broker_quote
    status: str = "unavailable"
    reason: str = ""
    broker_mid: Optional[float] = None
    reference_mid: Optional[float] = None
    divergence_bp: Optional[float] = None
    divergence_pips: Optional[float] = None
    shrink_at_bp: Optional[float] = None
    block_at_bp: Optional[float] = None
    reference_age_sec: Optional[float] = None
    size_multiplier: float = 1.0
    ts_ns: int = 0

    @property
    def blocked(self) -> bool:
        return self.status == "block"

    @property
    def active(self) -> bool:
        """True when the check had BOTH prices and could judge."""
        return self.status in ("ok", "shrink", "block")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["blocked"] = self.blocked
        return d


@dataclass
class _History:
    samples: Deque[float] = field(default_factory=lambda: deque(maxlen=240))
    blocks: int = 0
    shrinks: int = 0
    checks: int = 0


class ReferenceGuard:
    """Assess broker quotes against the reference stream.

    ``config`` is a callable returning the live ``ReferenceConfig`` (so a
    dashboard change applies at the next cycle), ``stream`` anything with a
    ``quote(symbol)`` method returning a ``TVQuote``.
    """

    def __init__(self, config: Callable[[], Any], stream=None, *,
                 audit: Optional[Callable[[Dict[str, Any]], None]] = None,
                 clock: Callable[[], int] = wall_ns) -> None:
        self._config = config
        self.stream = stream
        self._audit = audit
        self._clock = clock
        self._lock = threading.RLock()
        self._history: Dict[str, _History] = {}
        self._last: Dict[str, ReferenceCheck] = {}
        self._blocked: set = set()

    # -- mapping ----------------------------------------------------------------- #

    def symbol_for(self, instrument: str) -> Optional[str]:
        cfg = self._config()
        mapped = (cfg.symbol_map or {}).get(instrument)
        if mapped:
            try:
                return validate_symbol(mapped)
            except ValueError:
                return None
        return default_symbol(instrument, cfg.exchange)

    def symbols(self, instruments: Iterable[str]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for inst in instruments:
            sym = self.symbol_for(inst)
            if sym:
                out[inst] = sym
        return out

    # -- the check ---------------------------------------------------------------- #

    def assess(self, now_ns: int, quotes: Mapping[str, Any],
               instruments: Optional[Mapping[str, Any]] = None,
               *, only: Optional[Iterable[str]] = None) -> Dict[str, ReferenceCheck]:
        cfg = self._config()
        if not getattr(cfg, "enabled", False) or self.stream is None:
            return {}
        names = list(only) if only is not None else list(quotes)
        out: Dict[str, ReferenceCheck] = {}
        for inst in names:
            check = self._check_one(now_ns, inst, quotes.get(inst),
                                    (instruments or {}).get(inst), cfg)
            out[inst] = check
            self._remember(check)
        return out

    def _check_one(self, now_ns: int, inst: str, quote, spec, cfg) -> ReferenceCheck:
        c = ReferenceCheck(instrument=inst, ts_ns=now_ns)
        sym = self.symbol_for(inst)
        if not sym:
            c.status, c.reason = "unmapped", "no valid TradingView symbol for this instrument"
            return c
        c.symbol = sym
        ref = self.stream.quote(sym)
        if ref is None or ref.mid is None:
            c.status = "unavailable"
            c.reason = (ref.error if ref is not None and ref.error
                        else "no reference quote yet")
            return c
        c.reference_mid = ref.mid
        stamp = ref.price_ns or ref.received_ns
        age = (now_ns - stamp) / _SEC if stamp else None
        c.reference_age_sec = round(age, 1) if age is not None else None
        if ref.delayed:
            c.status, c.reason = "delayed", f"the reference is delayed ({ref.update_mode})"
            return c
        if ref.session and ref.session not in ("market", ""):
            c.status, c.reason = "closed", f"reference session: {ref.session}"
            return c
        if age is None or age > cfg.max_age_sec:
            c.status = "stale"
            c.reason = (f"the reference price has not moved for {age:.0f}s"
                        if age is not None else "the reference has no price time")
            return c
        if quote is None:
            c.status, c.reason = "no_broker_quote", "no broker quote"
            return c
        try:
            bid, ask = float(quote.bid), float(quote.ask)
        except (TypeError, ValueError, AttributeError):
            c.status, c.reason = "no_broker_quote", "unreadable broker quote"
            return c
        received = getattr(quote, "received_ns", 0) or 0
        if received and (now_ns - received) / _SEC > cfg.max_age_sec:
            c.status = "no_broker_quote"
            c.reason = "the broker quote is older than the reference window"
            return c
        if bid <= 0 or ask <= 0 or ask < bid:
            c.status, c.reason = "no_broker_quote", "invalid broker quote"
            return c
        mid = (bid + ask) / 2.0
        c.broker_mid = mid
        div = abs(mid - ref.mid)
        div_bp = div / ref.mid * 10_000.0
        spread_bp = (ask - bid) / mid * 10_000.0
        c.divergence_bp = round(div_bp, 3)
        pip = getattr(spec, "pip", None)
        if pip:
            try:
                c.divergence_pips = round(div / float(pip), 2)
            except (TypeError, ValueError, ZeroDivisionError):
                c.divergence_pips = None
        c.shrink_at_bp = round(max(cfg.shrink_bp, cfg.spread_multiple_shrink * spread_bp), 3)
        c.block_at_bp = round(max(cfg.block_bp, cfg.spread_multiple_block * spread_bp), 3)
        where = (f"broker {mid:.6g} vs reference {ref.mid:.6g} ({sym}): "
                 f"{div_bp:.1f} bp"
                 + (f" / {c.divergence_pips:.1f} pips" if c.divergence_pips is not None else ""))
        if div_bp > c.block_at_bp:
            c.status, c.size_multiplier = "block", 0.0
            c.reason = f"price disagrees with the market: {where} > {c.block_at_bp:.1f} bp"
        elif div_bp > c.shrink_at_bp:
            c.status, c.size_multiplier = "shrink", float(cfg.shrink_multiplier)
            c.reason = (f"price differs from the market: {where} > {c.shrink_at_bp:.1f} bp; "
                        f"size x{cfg.shrink_multiplier:g}")
        else:
            c.status, c.reason = "ok", where
        return c

    def _remember(self, check: ReferenceCheck) -> None:
        with self._lock:
            self._last[check.instrument] = check
            h = self._history.setdefault(check.instrument, _History())
            if check.active and check.broker_mid is not None and check.reference_mid:
                h.checks += 1
                signed = (check.broker_mid - check.reference_mid) / check.reference_mid * 1e4
                h.samples.append(round(signed, 3))
                if check.status == "block":
                    h.blocks += 1
                elif check.status == "shrink":
                    h.shrinks += 1
            was = check.instrument in self._blocked
            now_blocked = check.blocked
            if now_blocked and not was:
                self._blocked.add(check.instrument)
                self._emit({"reference_block": check.instrument, "symbol": check.symbol,
                            "divergence_bp": check.divergence_bp,
                            "block_at_bp": check.block_at_bp, "reason": check.reason})
            elif was and not now_blocked and check.active:
                self._blocked.discard(check.instrument)
                self._emit({"reference_block_cleared": check.instrument,
                            "divergence_bp": check.divergence_bp})

    def _emit(self, payload: Dict[str, Any]) -> None:
        if self._audit is None:
            return
        try:
            self._audit(payload)
        except Exception:  # noqa: BLE001 - journalling must never break a check
            pass

    # -- read model --------------------------------------------------------------- #

    def view(self) -> Dict[str, Any]:
        with self._lock:
            rows = []
            for inst, check in sorted(self._last.items()):
                h = self._history.get(inst) or _History()
                samples = list(h.samples)
                rows.append({
                    **check.to_dict(),
                    # The median SIGNED gap: a steady non-zero value is a basis
                    # (a CFD on futures, a different fixing), not a fault --
                    # shown so the owner can map a better reference symbol.
                    "median_gap_bp": round(statistics.median(samples), 3) if samples else None,
                    "recent_gap_bp": samples[-60:],
                    "checks": h.checks, "shrinks": h.shrinks, "blocks": h.blocks,
                })
        return {"checks": rows}

    def size_multiplier(self, instrument: str) -> tuple[Decimal, str]:
        with self._lock:
            c = self._last.get(instrument)
        if c is None or c.status not in ("shrink", "block"):
            return D("1"), ""
        return D(str(c.size_multiplier)), c.reason


class ReferenceDesk:
    """Keeps the reference stream subscribed and the technical ratings current.

    Driven by the runtime's background worker (like the news desk), never by
    the decision thread. Switching ``reference.enabled`` on or off on the
    dashboard takes effect at the next tick: the stream is started or stopped
    and the subscription follows the instruments the engine actually trades.
    """

    def __init__(self, config: Callable[[], Any], guard: ReferenceGuard, stream,
                 instruments: Callable[[], Iterable[str]], *,
                 ta_fetch: Optional[Callable[..., Dict[str, Any]]] = None,
                 clock: Callable[[], int] = wall_ns) -> None:
        from .tradingview import fetch_ta

        self._config = config
        self.guard = guard
        self.stream = stream
        self._instruments = instruments
        self._ta_fetch = ta_fetch or fetch_ta
        self._clock = clock
        self._lock = threading.RLock()
        self.mapping: Dict[str, str] = {}
        self.ta: Dict[str, Any] = {}
        self.ta_last_ns = 0
        self.ta_error = ""
        self.last_tick_ns = 0

    def tick(self, now_ns: Optional[int] = None, *, force_ta: bool = False,
             with_ta: bool = True) -> None:
        now = int(now_ns or self._clock())
        self.last_tick_ns = now
        cfg = self._config()
        if not cfg.enabled:
            if self.stream.running:
                self.stream.stop()
            self.stream.set_symbols([])
            with self._lock:
                self.mapping = {}
            return
        wanted = []
        for inst in self._instruments():
            if inst not in wanted:
                wanted.append(inst)
        mapping = self.guard.symbols(wanted[:40])
        with self._lock:
            self.mapping = mapping
        self.stream.set_symbols(mapping.values())
        if not self.stream.running:
            self.stream.start()
        due = now - self.ta_last_ns >= cfg.ta_every_min * 60 * _SEC
        if with_ta and cfg.ta_ratings and mapping and (due or force_ta):
            self.refresh_ta(now, mapping)

    def refresh_ta(self, now_ns: int, mapping: Dict[str, str]) -> None:
        self.ta_last_ns = now_ns
        try:
            ratings = self._ta_fetch(list(mapping.values()))
        except Exception as exc:  # noqa: BLE001 - context is optional
            self.ta_error = f"{type(exc).__name__}: {exc}"[:200]
            return
        by_instrument = {inst: ratings.get(sym) for inst, sym in mapping.items()
                         if ratings.get(sym)}
        with self._lock:
            self.ta = by_instrument
            self.ta_error = ""

    def stop(self) -> None:
        try:
            self.stream.stop()
        except Exception:  # noqa: BLE001
            pass

    def view(self) -> Dict[str, Any]:
        cfg = self._config()
        snap = self.stream.snapshot()
        with self._lock:
            mapping = dict(self.mapping)
            ta = dict(self.ta)
        quotes = {inst: snap["quotes"].get(sym) for inst, sym in mapping.items()}
        return {
            "enabled": bool(cfg.enabled),
            "provider": cfg.provider,
            "config": cfg.model_dump(mode="json") if hasattr(cfg, "model_dump") else {},
            "stream": snap["status"],
            "mapping": mapping,
            "quotes": quotes,
            "checks": self.guard.view()["checks"],
            "ta": ta,
            "ta_last_ns": self.ta_last_ns,
            "ta_error": self.ta_error,
            "last_tick_ns": self.last_tick_ns,
        }
