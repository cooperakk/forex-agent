"""The news desk: keeps the calendar current and reads official releases.

Runs OFF the trading thread (``Runtime`` drives it from a background worker),
so a slow feed or a slow model can never delay a decision cycle. The agent only
reads what the desk has already cached:

* the calendar it keeps current is the one ``NewsPolicy`` already consults for
  blackout windows -- now with CONFIRMED dates, so the windows actually close;
* the extractions it produces from official headlines are handed to
  ``NewsPolicy.assess`` and can only shrink a position (a correction) or block
  one (contradictory reporting). Nothing here can start or enlarge a trade.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.clock import wall_ns
from .feeds import OFFICIAL_FEEDS, Article, ForexFactoryCalendar, fetch_official
from .llm_extract import Extraction, NewsExtractor

_MIN = 60 * 1_000_000_000


@dataclass
class DeskStatus:
    calendar_last_ns: int = 0
    calendar_ok: bool = False
    calendar_report: Dict[str, Any] = field(default_factory=dict)
    feeds_last_ns: int = 0
    feed_errors: Dict[str, str] = field(default_factory=dict)
    articles_seen: int = 0
    extracted: int = 0
    ai_available: bool = False
    ai_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class NewsDesk:
    def __init__(self, calendar, *, ai=None, calendar_source=None, fetch=None,
                 feeds=OFFICIAL_FEEDS, calendar_every_min: int = 60,
                 feeds_every_min: int = 15, max_extractions_per_tick: int = 6,
                 training_cutoff: str = "2025-01-01") -> None:
        self.calendar = calendar
        self.ai = ai
        self.calendar_source = calendar_source or ForexFactoryCalendar(fetch=fetch)
        self._fetch = fetch
        self.feeds = tuple(feeds)
        self.calendar_every = calendar_every_min * _MIN
        self.feeds_every = feeds_every_min * _MIN
        self.max_extractions = max_extractions_per_tick
        self.training_cutoff = training_cutoff
        self.status = DeskStatus()
        self._articles: Dict[str, Article] = {}
        self._extractions: Dict[str, tuple] = {}     # article_id -> (ts_ns, Extraction)
        self._lock = threading.RLock()

    # -- the background step --------------------------------------------------- #

    def tick(self, now_ns: Optional[int] = None) -> DeskStatus:
        now = int(now_ns or wall_ns())
        if now - self.status.calendar_last_ns >= self.calendar_every:
            self.refresh_calendar(now)
        if now - self.status.feeds_last_ns >= self.feeds_every:
            self.refresh_feeds(now)
        return self.status

    def refresh_calendar(self, now_ns: int) -> Dict[str, Any]:
        self.status.calendar_last_ns = now_ns
        if self.calendar is None:
            return {}
        start = now_ns - 2 * 86_400 * 10**9
        end = now_ns + 8 * 86_400 * 10**9
        report = self.calendar.ingest(self.calendar_source, start, end)
        self.status.calendar_ok = not report.errors or bool(report.inserted
                                                            or report.unchanged
                                                            or report.updated)
        self.status.calendar_report = report.to_dict()
        return self.status.calendar_report

    def refresh_feeds(self, now_ns: int) -> None:
        self.status.feeds_last_ns = now_ns
        articles, errors = fetch_official(self.feeds, fetch=self._fetch)
        self.status.feed_errors = errors
        with self._lock:
            for a in articles:
                self._articles.setdefault(a.article_id, a)
            # Keep the most recent 400 headlines.
            if len(self._articles) > 400:
                ordered = sorted(self._articles.values(),
                                 key=lambda x: x.published_ns or 0, reverse=True)[:400]
                self._articles = {a.article_id: a for a in ordered}
            self.status.articles_seen = len(self._articles)
        self._extract_new(now_ns)

    def _extract_new(self, now_ns: int) -> None:
        if self.ai is None:
            self.status.ai_available, self.status.ai_reason = False, "no AI service"
            return
        ok, why = self.ai.available("news")
        self.status.ai_available, self.status.ai_reason = ok, why
        if not ok:
            return
        extractor = NewsExtractor(self.ai.caller("news"), model="configured-chain",
                                  training_cutoff=self.training_cutoff,
                                  timeout_ms=int(self.ai.settings.timeout_sec * 1000) + 1000,
                                  max_calls_per_hour=self.ai.settings.max_calls_per_hour)
        horizon = now_ns - 36 * 60 * _MIN
        with self._lock:
            pending = [a for a in sorted(self._articles.values(),
                                         key=lambda x: x.published_ns or 0, reverse=True)
                       if a.article_id not in self._extractions
                       and (a.published_ns or now_ns) >= horizon
                       and not self.ai.store.has_extraction(a.article_id)]
        for article in pending[: self.max_extractions]:
            ex = extractor.extract(article.article_id, article.title, article.summary,
                                   published_ns=article.published_ns)
            # The source already tells us which currency it moves. A model that
            # lists none, or lists an unrelated one, is corrected towards the
            # source, never away from it.
            if not ex.currencies:
                ex.currencies = list(article.currencies)
            with self._lock:
                self._extractions[article.article_id] = (now_ns, ex)
            try:
                self.ai.store.save_extraction(
                    article.article_id, article.published_ns, article.source,
                    article.title, ex.to_dict())
            except Exception:  # noqa: BLE001
                pass
            self.status.extracted += 1

    # -- what the trading thread reads ------------------------------------------ #

    def recent_extractions(self, now_ns: int, hours: float = 24.0,
                           block_hours: float = 4.0,
                           block_min_confidence: float = 0.6) -> List[Extraction]:
        """Valid extractions from the last ``hours``, safe for the policy.

        A correction may shrink size for the whole window. A "contradicts
        prior reporting" flag -- which BLOCKS the currency -- is honoured only
        while fresh and only at a confident extraction: one misread press
        release must not sit a currency out for a day.
        """
        from dataclasses import replace

        cutoff = now_ns - int(hours * 60) * _MIN
        block_cutoff = now_ns - int(block_hours * 60) * _MIN
        out: List[Extraction] = []
        with self._lock:
            pairs = list(self._extractions.values())
        for ts, ex in pairs:
            if ts < cutoff or not ex.valid:
                continue
            if ex.contradicts_prior and (ts < block_cutoff
                                         or ex.confidence < block_min_confidence):
                ex = replace(ex, contradicts_prior=False)
            out.append(ex)
        return out

    def headlines(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            ordered = sorted(self._articles.values(),
                             key=lambda x: x.published_ns or 0, reverse=True)[:limit]
            rows = []
            for a in ordered:
                row = a.to_dict()
                pair = self._extractions.get(a.article_id)
                if pair:
                    ex = pair[1]
                    row["extraction"] = {
                        "valid": ex.valid, "event_type": ex.event_type,
                        "direction_claim": ex.direction_claim,
                        "is_correction": ex.is_correction,
                        "contradicts_prior": ex.contradicts_prior,
                        "confidence": ex.confidence, "errors": ex.errors[:2]}
                rows.append(row)
        return rows
