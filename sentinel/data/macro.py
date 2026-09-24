"""The macro desk: the dollar index and speculators' positioning, for every signal.

One object, three callers:

* the AGENT, on its own thread: ``refresh_prices`` keeps the six DXY
  components' bars current through the feed (MetaTrader tolerates calls from
  one thread only), and for every signal ``features`` / ``layers``;
* the RUNTIME's background worker: ``tick`` refreshes the weekly COT data
  over HTTP -- never from the agent's thread, so a slow CFTC server cannot
  delay a trading cycle;
* the LAB: the same ``features`` function, called "as of" each historical
  signal, so a meta-label filter learns from exactly what the live agent will
  show it, with no look-ahead.

Every feature is centred so that 0 means "neutral or unknown": a missing
input can never look like a strong reading to a model.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..core.clock import wall_ns
from .cot import COT_CODES, CotClient, CotError, CotStore, parse_file, positioning
from .dxy import DXY_WEIGHTS, DxySeries, build_dxy, dxy_state, usd_sign

EVENT_COT = "macro.cot"


class MacroDesk:
    def __init__(self, config: Callable[[], Any], store: CotStore, bar_store, audit, *,
                 client: Optional[CotClient] = None,
                 clock: Callable[[], int] = wall_ns) -> None:
        self._config = config
        self.store = store
        self.bar_store = bar_store
        self.audit = audit
        self.client = client or CotClient()
        self._clock = clock
        self._lock = threading.RLock()
        self._dxy: Optional[DxySeries] = None
        self._dxy_built_ns = 0
        self._instruments: Dict[str, Any] = {}
        self.dxy_error = ""
        self.cot_error = ""
        self.cot_last_fetch_ns = int(store.get_state("cot_last_fetch_ns", 0) or 0)
        self._cot_running = False

    @property
    def cfg(self):
        return self._config()

    # ------------------------------------------------------------------ #
    # the dollar index (agent thread)
    # ------------------------------------------------------------------ #

    def dxy_components(self, instruments: Mapping[str, Any]) -> List[str]:
        return [s for s in DXY_WEIGHTS if s in instruments]

    def refresh_prices(self, feed, now_ns: int, instruments: Mapping[str, Any]) -> None:
        """Keep the components' bars current and rebuild the index. Never raises."""
        cfg = self.cfg
        with self._lock:
            if instruments:
                self._instruments = dict(instruments)
        if not (cfg.enabled and cfg.dxy_enabled):
            return
        try:
            comps = self.dxy_components(instruments)
            fresh = {}
            if comps:
                fresh = feed.refresh(comps, now_ns, timeframes=[cfg.dxy_timeframe]) or {}
            new_bar = any(k.endswith("@" + cfg.dxy_timeframe) and n for k, n in fresh.items())
            # Rebuild only when a component bar closed (or every ten minutes):
            # reading 6 x 3000 bars from the store on every cycle is waste.
            if new_bar or self._dxy is None or now_ns - self._dxy_built_ns > 600 * 10**9:
                self.rebuild_dxy()
                self._dxy_built_ns = now_ns
        except Exception as exc:  # noqa: BLE001 - context, never a gate
            self.dxy_error = f"{type(exc).__name__}: {exc}"[:300]

    def rebuild_dxy(self, limit: Optional[int] = None) -> Optional[DxySeries]:
        series = self.dxy_history(limit)
        with self._lock:
            self._dxy = series
            self._dxy_built_ns = self._clock()
        self.dxy_error = "" if series is not None else self._dxy_missing_reason()
        return series

    def _dxy_missing_reason(self) -> str:
        return ("not enough dollar pairs on this server, or no bars for them yet "
                "(EUR/USD and pairs carrying at least 75% of the index are needed)")

    def dxy_history(self, limit: Optional[int] = None) -> Optional[DxySeries]:
        """The index from stored bars, without touching the live copy (the lab
        calls this from its own thread)."""
        cfg = self.cfg
        closes = {}
        for sym in DXY_WEIGHTS:
            try:
                f = self.bar_store.frame(sym, cfg.dxy_timeframe, limit or cfg.dxy_history_bars)
            except Exception:  # noqa: BLE001 - one missing series is not fatal
                continue
            if f is not None and len(f):
                closes[sym] = f["close"].astype(float)
        return build_dxy(closes, cfg.dxy_timeframe)

    # ------------------------------------------------------------------ #
    # COT (background thread)
    # ------------------------------------------------------------------ #

    def tick(self, now_ns: Optional[int] = None) -> None:
        cfg = self.cfg
        if not (cfg.enabled and cfg.cot_enabled):
            return
        now = int(now_ns or self._clock())
        if now - self.cot_last_fetch_ns < cfg.cot_refresh_hours * 3600 * 10**9:
            return
        self.refresh_cot(now)

    def refresh_cot(self, now_ns: Optional[int] = None, *, by: str = "schedule"
                    ) -> Dict[str, Any]:
        now = int(now_ns or self._clock())
        if self._cot_running:
            return {"ok": False, "error": "already running"}
        self._cot_running = True
        try:
            latest = self.store.latest_date()
            if latest:
                since = (dt.date.fromisoformat(latest) - dt.timedelta(days=21)).isoformat()
            else:
                years = max(3, self.cfg.cot_lookback_weeks // 52 + 1)
                since = (dt.datetime.fromtimestamp(now / 1e9, tz=dt.timezone.utc).date()
                         - dt.timedelta(days=365 * years)).isoformat()
            before = self.store.latest_date()
            reports = self.client.fetch(list(COT_CODES.values()), since)
            self.store.upsert(reports)
            self.cot_error = ""
            self.cot_last_fetch_ns = now
            self.store.set_state("cot_last_fetch_ns", now)
            after = self.store.latest_date()
            if after and after != before:
                self._announce(now, by)
            return {"ok": True, "reports": len(reports), "latest": after}
        except (CotError, ValueError) as exc:
            self.cot_error = str(exc)[:300]
            # Back off: try again in an hour, not every minute.
            self.cot_last_fetch_ns = now - max(0, self.cfg.cot_refresh_hours - 1) * 3600 * 10**9
            return {"ok": False, "error": self.cot_error}
        finally:
            self._cot_running = False

    def import_file(self, path: str, by: str = "owner") -> Dict[str, Any]:
        reports = parse_file(path)
        n = self.store.upsert(reports)
        self.audit.append("config.change", {"action": "cot_import", "rows": len(reports),
                                            "file": str(path)[-120:]}, actor=by)
        return {"ok": True, "reports": len(reports), "stored": n,
                "latest": self.store.latest_date()}

    def _announce(self, now_ns: int, by: str) -> None:
        cfg = self.cfg
        rows = []
        for ccy in COT_CODES:
            p = positioning(self.store.history(ccy), now_ns, lookback=cfg.cot_lookback_weeks,
                            min_weeks=cfg.cot_min_weeks)
            if p is not None:
                rows.append({"currency": ccy, "index": p["index"], "change": p["change"],
                             "report_date": p["report_date"]})
        if not rows:
            return
        try:
            self.audit.append(EVENT_COT, {"report_date": rows[0]["report_date"],
                                          "extreme": cfg.cot_extreme, "positions": rows},
                              actor="macro")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # per-signal context (agent thread and lab)
    # ------------------------------------------------------------------ #

    def _legs(self, instrument: str) -> Tuple[str, str]:
        inst = self._instruments.get(instrument)
        if inst is not None and getattr(inst, "base", None):
            return str(inst.base), str(inst.quote)
        base, _, quote = instrument.partition("_")
        return base, quote

    def features(self, instrument: str, side_sign: int, as_of_ns: int,
                 dxy: Optional[DxySeries] = None) -> Dict[str, float]:
        """Centred macro features for one signal; {} when nothing is known."""
        cfg = self.cfg
        if not cfg.enabled:
            return {}
        out: Dict[str, float] = {}
        base, quote = self._legs(instrument)
        try:
            if cfg.dxy_enabled:
                series = dxy if dxy is not None else self._dxy
                st = dxy_state(series, as_of_ns)
                us = usd_sign(base, quote, side_sign)
                if st:
                    out["dxy_mom"] = st["dxy_mom"]
                    out["dxy_z"] = st["dxy_z"]
                    out["dxy_align"] = round(us * st["dxy_mom"], 4)
                out["usd_side"] = float(us)
            if cfg.cot_enabled:
                pb = self._pos(base, as_of_ns)
                pq = self._pos(quote, as_of_ns)
                if pb is not None:
                    out["cot_base"] = round((pb["index"] - 50.0) / 50.0, 4)
                if pq is not None:
                    out["cot_quote"] = round((pq["index"] - 50.0) / 50.0, 4)
                if pb is not None or pq is not None:
                    # Positive: speculators are positioned the SAME way as the
                    # trade (long base / short quote for a buy).
                    lean = ((pb["index"] - 50.0) if pb else 0.0) - \
                           ((pq["index"] - 50.0) if pq else 0.0)
                    out["cot_with_trade"] = round(side_sign * lean / 100.0, 4)
        except Exception:  # noqa: BLE001 - context, never a gate
            return out
        return out

    def _pos(self, ccy: str, as_of_ns: int) -> Optional[Dict[str, Any]]:
        if ccy not in COT_CODES:
            return None
        cfg = self.cfg
        return positioning(self.store.history(ccy), as_of_ns, lookback=cfg.cot_lookback_weeks,
                           min_weeks=cfg.cot_min_weeks)

    def layers(self, instrument: str, side_sign: int, as_of_ns: int
               ) -> Tuple[float, List[str], Dict[str, float]]:
        """Shrink-only size multipliers: (multiplier <= 1, reasons, per-layer)."""
        cfg = self.cfg
        if not cfg.enabled:
            return 1.0, [], {}
        layers: Dict[str, float] = {}
        reasons: List[str] = []
        try:
            base, quote = self._legs(instrument)
            if cfg.cot_enabled and cfg.cot_crowding_enabled:
                hi, lo = cfg.cot_extreme, 100.0 - cfg.cot_extreme
                crowded = []
                pb, pq = self._pos(base, as_of_ns), self._pos(quote, as_of_ns)
                # A BUY is long the base and short the quote.
                if pb is not None and ((side_sign > 0 and pb["index"] >= hi)
                                       or (side_sign < 0 and pb["index"] <= lo)):
                    crowded.append(f"{base} index {pb['index']:.0f}")
                if pq is not None and ((side_sign > 0 and pq["index"] <= lo)
                                       or (side_sign < 0 and pq["index"] >= hi)):
                    crowded.append(f"{quote} index {pq['index']:.0f}")
                if crowded:
                    layers["cot_crowding"] = float(cfg.cot_crowding_multiplier)
                    reasons.append("crowded: speculators are at a multi-year extreme on "
                                   "this side (" + ", ".join(crowded) + ")")
            if cfg.dxy_enabled and cfg.dxy_headwind_enabled:
                st = dxy_state(self._dxy, as_of_ns)
                us = usd_sign(base, quote, side_sign)
                if st and us and us * st["dxy_mom"] <= -cfg.dxy_headwind_score:
                    layers["dxy_headwind"] = float(cfg.dxy_headwind_multiplier)
                    reasons.append(f"dollar headwind: the dollar index is moving against "
                                   f"this trade (momentum {st['dxy_mom']:+.1f})")
        except Exception:  # noqa: BLE001
            return 1.0, [], {}
        return min([1.0] + list(layers.values())), reasons, layers

    # ------------------------------------------------------------------ #
    # read model
    # ------------------------------------------------------------------ #

    def view(self) -> Dict[str, Any]:
        cfg = self.cfg
        now = self._clock()
        with self._lock:
            series = self._dxy
            instruments = dict(self._instruments)
        dxy: Dict[str, Any] = {"available": series is not None, "error": self.dxy_error,
                               "components_on_server": self.dxy_components(instruments)
                               if instruments else []}
        if series is not None:
            level = series.level
            tail = level.iloc[-240:]
            dxy.update({
                "complete": series.complete, "used": series.used, "missing": series.missing,
                "notes": series.notes, "timeframe": series.timeframe,
                "last": round(float(level.iloc[-1]), 4),
                "last_bar": str(level.index[-1]),
                "points": [[int(t // 1_000_000), round(float(v), 5)]
                           for t, v in zip(tail.index.as_unit("ns").asi8, tail.to_numpy(),
                                           strict=True)],
                "state": dxy_state(series, now + series.bar_ns),
            })
        cot_rows = []
        for ccy in COT_CODES:
            p = positioning(self.store.history(ccy), now, lookback=cfg.cot_lookback_weeks,
                            min_weeks=cfg.cot_min_weeks)
            hist = self.store.history(ccy)
            cot_rows.append({"currency": ccy, "code": COT_CODES[ccy], "weeks": len(hist),
                             **({} if p is None else p),
                             "crowded_long": bool(p and p["index"] >= cfg.cot_extreme),
                             "crowded_short": bool(p and p["index"] <= 100 - cfg.cot_extreme)})
        return {
            "enabled": bool(cfg.enabled),
            "config": cfg.model_dump(mode="json") if hasattr(cfg, "model_dump") else {},
            "dxy": dxy,
            "cot": {"rows": cot_rows, "stored": self.store.count(),
                    "latest": self.store.latest_date(), "error": self.cot_error,
                    "last_fetch_ns": self.cot_last_fetch_ns, "running": self._cot_running},
        }

