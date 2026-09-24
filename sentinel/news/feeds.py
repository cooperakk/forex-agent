"""Live news inputs: a confirmed economic calendar and official press feeds.

Until 1.5.0 the only calendar was the bundled recurrence PATTERN, whose dates
are estimates ("roughly the first Friday"). By design an estimated date never
creates a blackout -- blocking on a guess sits out the wrong day and clears
the right one -- so, in practice, the news filter never blocked anything. This
module supplies what the design was waiting for: confirmed dates and times.

Sources, chosen for reliability rather than volume
--------------------------------------------------
* **Economic calendar** -- the Forex Factory weekly export
  (``nfs.faireconomy.media``): release time, currency, impact, forecast and
  previous for the current week. It is the de-facto reference retail FX
  calendar and is published as plain JSON. Rows become ``confirmed`` events,
  which the existing policy turns into blackout windows around high-impact
  releases.
* **Official press feeds** -- the central banks and statistics offices whose
  releases actually move the majors (Fed, ECB, BoE, BoJ, RBA, BoC, US BLS).
  Official sources only: no aggregators, no social media, no opinion pieces.
  Headlines feed the optional AI extractor (``sentinel.ai``), whose output can
  only shrink or block, never originate a trade.

Safety
------
* HTTPS only; redirects are not followed; responses are size-capped.
* XML is refused if it declares a DOCTYPE or ENTITY (the whole class of
  entity-expansion attacks), before the parser sees it.
* A dead feed is data, not a crash: every failure is returned in the report
  and the previous calendar stays in force.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

MAX_BYTES = 2_000_000
USER_AGENT = "Sentinel-FX/1.5 (+risk filter; contact: operator)"
MAJORS = ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "CNY")

FOREX_FACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


@dataclass(frozen=True)
class Feed:
    id: str
    name: str
    url: str
    currencies: tuple
    kind: str = "central_bank"      # central_bank | statistics


#: Official sources only. Each tags the currencies its releases move.
OFFICIAL_FEEDS: tuple = (
    Feed("fed", "Federal Reserve press releases",
         "https://www.federalreserve.gov/feeds/press_all.xml", ("USD",)),
    Feed("ecb", "European Central Bank press",
         "https://www.ecb.europa.eu/rss/press.html", ("EUR",)),
    Feed("boe", "Bank of England news",
         "https://www.bankofengland.co.uk/rss/news", ("GBP",)),
    Feed("boj", "Bank of Japan what's new",
         "https://www.boj.or.jp/en/rss/whatsnew.xml", ("JPY",)),
    Feed("rba", "Reserve Bank of Australia media releases",
         "https://www.rba.gov.au/rss/rss-cb-media-releases.xml", ("AUD",)),
    Feed("boc", "Bank of Canada press releases",
         "https://www.bankofcanada.ca/content_type/press-releases/feed/", ("CAD",)),
    Feed("bls", "US Bureau of Labor Statistics releases",
         "https://www.bls.gov/feed/news_release.rss", ("USD",), kind="statistics"),
)

Fetcher = Callable[[str, float], bytes]


class FeedError(RuntimeError):
    pass


def https_get(url: str, timeout: float = 15.0) -> bytes:
    """GET over HTTPS with no redirects and a size cap."""
    import httpx

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise FeedError(f"refusing a non-https feed: {url}")
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=False,
                      headers={"User-Agent": USER_AGENT,
                               "Accept": "application/json, application/xml, "
                                         "application/rss+xml, text/xml"}) as resp:
        if resp.status_code in (301, 302, 303, 307, 308):
            raise FeedError(f"{parsed.hostname} redirected; redirects are not followed")
        if resp.status_code >= 400:
            raise FeedError(f"{parsed.hostname} answered HTTP {resp.status_code}")
        chunks: List[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > MAX_BYTES:
                raise FeedError(f"{parsed.hostname} sent more than {MAX_BYTES} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


# --------------------------------------------------------------------------- #
# economic calendar
# --------------------------------------------------------------------------- #


_IMPACT = {"high": "high", "medium": "medium", "low": "low"}
_NUMBER = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*([%KMBT]?)\s*$", re.I)


def parse_number(text: Any) -> Optional[float]:
    """'3.2%' -> 3.2, '215K' -> 215000, '' -> None. Units are the provider's."""
    if text is None:
        return None
    m = _NUMBER.match(str(text))
    if not m:
        return None
    value = float(m.group(1))
    scale = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}.get(m.group(2).lower(), 1.0)
    return value * scale


class ForexFactoryCalendar:
    """A ``CalendarSource`` for the Forex Factory weekly export.

    Keyed ``(series, ISO week)``: the export has no reference period, and a
    release moved within its week must update ONE row rather than open a
    second blackout window beside the first.
    """

    name = "forexfactory"

    def __init__(self, fetch: Optional[Fetcher] = None, *, url: str = FOREX_FACTORY_URL,
                 currencies: Iterable[str] = MAJORS, timeout: float = 15.0) -> None:
        self._fetch = fetch or https_get
        self.url = url
        self.currencies = {c.upper() for c in currencies}
        self.timeout = timeout

    def fetch(self, start_ns: int, end_ns: int) -> List[Dict[str, Any]]:
        import json

        raw = self._fetch(self.url, self.timeout)
        try:
            rows = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise FeedError(f"the calendar is not JSON: {exc}") from exc
        if not isinstance(rows, list):
            raise FeedError("the calendar is not a list of events")
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            event = self.normalise(row)
            if event is None or not (start_ns <= event["event_ns"] <= end_ns):
                continue
            out.append(event)
        return out

    def normalise(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        currency = str(row.get("country") or "").strip().upper()
        title = str(row.get("title") or "").strip()[:160]
        impact = _IMPACT.get(str(row.get("impact") or "").strip().lower())
        if currency not in self.currencies or not title or impact is None:
            return None                      # holidays, non-economic rows, other ccys
        try:
            when = dt.datetime.fromisoformat(str(row.get("date") or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            return None                      # an instant without a zone is a guess
        when_utc = when.astimezone(dt.UTC)
        iso = when_utc.isocalendar()
        series = f"ff:{currency}:{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')}"
        period = f"{iso[0]}-W{iso[1]:02d}"
        return {
            "event_id": f"{series}:{period}",
            "series_id": series, "period": period,
            "event_ns": int(when_utc.timestamp() * 1e9),
            "country": currency, "currency": currency, "name": title,
            "impact": impact, "curated_impact": impact,
            "certainty": "confirmed", "source": self.name, "zone": "UTC",
            "forecast": parse_number(row.get("forecast")),
            "previous": parse_number(row.get("previous")),
        }


# --------------------------------------------------------------------------- #
# official press feeds
# --------------------------------------------------------------------------- #


@dataclass
class Article:
    article_id: str
    feed: str
    source: str
    title: str
    summary: str
    link: str
    published_ns: Optional[int]
    currencies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_TAG = re.compile(r"<[^>]+>")


def _text(node, *names: str) -> str:
    for name in names:
        found = node.find(name)
        if found is not None:
            value = found.get("href") if found.text is None and found.get("href") else \
                found.text
            if value:
                return _TAG.sub("", value).strip()
    return ""


def _when(text: str) -> Optional[int]:
    if not text:
        return None
    from email.utils import parsedate_to_datetime
    try:
        stamp = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            stamp = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.UTC)
    return int(stamp.timestamp() * 1e9)


def parse_feed(feed: Feed, raw: bytes, *, limit: int = 30) -> List[Article]:
    """RSS 2.0 or Atom. Refuses any document that declares a DTD or entity."""
    import xml.etree.ElementTree as ET

    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise FeedError(f"{feed.id}: the document declares a DTD/entity and is refused")
    try:
        # Entity expansion and external entities both need a DTD, which is
        # refused above before the parser sees a byte of the document.
        root = ET.fromstring(raw)  # noqa: S314
    except ET.ParseError as exc:
        raise FeedError(f"{feed.id}: not valid XML ({exc})") from exc
    atom = "{http://www.w3.org/2005/Atom}"
    items = root.findall(".//item") or root.findall(f".//{atom}entry")
    out: List[Article] = []
    for item in items[:limit]:
        title = _text(item, "title", f"{atom}title")[:300]
        link = _text(item, "link", f"{atom}link")[:500]
        summary = _text(item, "description", f"{atom}summary", f"{atom}content")[:1500]
        published = _when(_text(item, "pubDate", f"{atom}updated", f"{atom}published",
                                "{http://purl.org/dc/elements/1.1/}date"))
        if not title:
            continue
        key = hashlib.sha256(f"{feed.id}|{link or title}".encode("utf-8")).hexdigest()[:24]
        out.append(Article(article_id=f"{feed.id}:{key}", feed=feed.id, source=feed.name,
                           title=title, summary=summary, link=link,
                           published_ns=published, currencies=list(feed.currencies)))
    return out


def fetch_official(feeds: Iterable[Feed] = OFFICIAL_FEEDS, *,
                   fetch: Optional[Fetcher] = None, timeout: float = 15.0
                   ) -> tuple:
    """(articles, errors). One dead feed never hides the others."""
    getter = fetch or https_get
    articles: List[Article] = []
    errors: Dict[str, str] = {}
    for feed in feeds:
        try:
            articles.extend(parse_feed(feed, getter(feed.url, timeout)))
        except Exception as exc:  # noqa: BLE001 - a dead feed is data
            errors[feed.id] = str(exc)[:200]
    return articles, errors
