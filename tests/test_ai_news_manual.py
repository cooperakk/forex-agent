"""1.5.0 features: AI providers, live news, the AI coach, and manual tickets.

Every network call is replaced by a recorded fake; nothing here touches the
internet. The tests pin the SHAPE of each provider's request (so a key cannot
end up in the wrong header), the security rules (no redirects, no plain http,
no key in any response or journal), and the one invariant every assistant
shares: it may inform a person or shrink risk, never add it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal as D

import pyotp
import pytest
from fastapi.testclient import TestClient

from sentinel.agent.memory import MemoryStore
from sentinel.agent.orchestrator import Agent
from sentinel.agent.proposals import ProposalQueue
from sentinel.ai import AIService
from sentinel.ai.coach import TradeCoach, generate_brief, validate_review
from sentinel.ai.providers import (
    ProviderClient, ProviderConfig, ProviderError, validate_base_url,
)
from sentinel.api.main import create_app
from sentinel.api.security import SecurityManager
from sentinel.api.state import Runtime
from sentinel.brokers.paper import PaperBroker, SimProfile
from sentinel.core.audit import AuditLog
from sentinel.core.config import (
    AgentConfig, AgentMode, ExecutionConfig, ExecutionVenueMode, OpsConfig, SentinelConfig,
)
from sentinel.core.money import Instrument
from sentinel.core.types import Quote
from sentinel.data.feed import BarStore, MarketFeed
from sentinel.news.calendar import EconomicCalendar
from sentinel.news.desk import NewsDesk
from sentinel.news.feeds import (
    Feed, FeedError, ForexFactoryCalendar, fetch_official, parse_feed, parse_number,
)
from sentinel.news.policy import NewsPolicy

KEY = "sk-test-0123456789abcdefSECRET"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class Wire:
    """Records every request and answers with canned responses."""

    def __init__(self, answers=None, *, status=200):
        self.answers = list(answers or [])
        self.status = status
        self.requests = []

    def post(self, url, *, headers, json_body, timeout):
        self.requests.append({"url": url, "headers": headers, "body": json_body})
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, tuple):
            return answer
        return self.status, json.dumps(answer)

    def get(self, url, *, headers, timeout):
        self.requests.append({"url": url, "headers": headers})
        return 200, json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]})


def _anthropic(text):
    return {"content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 11, "output_tokens": 7}}


def _openai(text):
    return {"choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3}}


def _gemini(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2}}


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #


class TestProviderWireFormats:
    def test_claude(self):
        wire = Wire([_anthropic('{"ok": true}')])
        client = ProviderClient(ProviderConfig("anthropic", True), KEY, post=wire.post)
        result = client.complete("sys", "user", json_mode=True)
        req = wire.requests[0]
        assert req["url"] == "https://api.anthropic.com/v1/messages"
        assert req["headers"]["x-api-key"] == KEY
        assert req["headers"]["anthropic-version"] == "2023-06-01"
        assert req["body"]["system"] == "sys"
        assert req["body"]["messages"] == [{"role": "user", "content": "user"}]
        assert result.text == '{"ok": true}' and result.input_tokens == 11

    def test_chatgpt(self):
        wire = Wire([_openai("hi")])
        client = ProviderClient(ProviderConfig("openai", True), KEY, post=wire.post)
        client.complete("sys", "user", max_tokens=50, json_mode=True)
        req = wire.requests[0]
        assert req["url"] == "https://api.openai.com/v1/chat/completions"
        assert req["headers"]["authorization"] == f"Bearer {KEY}"
        assert req["body"]["max_completion_tokens"] == 50
        assert "temperature" not in req["body"] and "max_tokens" not in req["body"]
        assert req["body"]["response_format"] == {"type": "json_object"}

    def test_gemini(self):
        wire = Wire([_gemini("hello")])
        client = ProviderClient(ProviderConfig("gemini", True), KEY, post=wire.post)
        result = client.complete("sys", "user", json_mode=True)
        req = wire.requests[0]
        assert req["url"].endswith("/models/gemini-2.5-flash:generateContent")
        assert req["headers"]["x-goog-api-key"] == KEY
        assert KEY not in req["url"], "the key must never ride in the URL"
        assert req["body"]["generationConfig"]["responseMimeType"] == "application/json"
        assert result.text == "hello" and result.output_tokens == 2

    @pytest.mark.parametrize("pid,host", [("deepseek", "api.deepseek.com"),
                                          ("kimi", "api.moonshot.ai")])
    def test_openai_compatible(self, pid, host):
        wire = Wire([_openai("x")])
        ProviderClient(ProviderConfig(pid, True), KEY, post=wire.post).complete("s", "u")
        req = wire.requests[0]
        assert host in req["url"] and req["url"].endswith("/chat/completions")
        assert req["body"]["max_tokens"] == 800 and req["body"]["temperature"] == 0

    def test_a_redirect_is_not_followed(self):
        wire = Wire([(302, "")])
        client = ProviderClient(ProviderConfig("openai", True), KEY, post=wire.post)
        with pytest.raises(ProviderError, match="redirect"):
            client.complete("s", "u")

    def test_an_error_body_that_echoes_the_key_is_redacted(self):
        wire = Wire([(401, f'{{"error": "invalid key {KEY}"}}')])
        client = ProviderClient(ProviderConfig("openai", True), KEY, post=wire.post)
        with pytest.raises(ProviderError) as exc:
            client.complete("s", "u")
        assert KEY not in str(exc.value) and KEY[:12] not in str(exc.value)

    def test_the_model_list(self):
        wire = Wire()
        client = ProviderClient(ProviderConfig("deepseek", True), KEY, post=wire.post,
                                get=wire.get)
        assert client.list_models() == ["model-a", "model-b"]

    @pytest.mark.parametrize("url", [
        "http://api.example.com/v1", "ftp://x", "https://user:pw@host/v1",
        "https://host/v1?token=1", "https:///v1"])
    def test_unsafe_custom_endpoints_are_refused(self, url):
        with pytest.raises(ValueError):
            validate_base_url(url)

    def test_a_local_model_may_use_plain_http(self):
        assert validate_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #


@pytest.fixture
def ai(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False)
    wire = Wire()
    service = AIService(tmp_path, audit, transport_post=wire.post, transport_get=wire.get)
    service.wire = wire
    return service


class TestAIService:
    def test_keys_are_sealed_and_never_returned(self, ai, tmp_path):
        ai.save_provider("anthropic", enabled=True, api_key=KEY, by="owner")
        raw = (tmp_path / "ai-secrets.json").read_text()
        assert KEY not in raw
        public = json.dumps(ai.describe(include_private=False))
        private = json.dumps(ai.describe(include_private=True))
        assert KEY not in public and KEY not in private
        row = next(p for p in ai.describe(include_private=True)["providers"]
                   if p["id"] == "anthropic")
        assert row["key_stored"] is True and row["key_last4"] == KEY[-4:]
        viewer_row = next(p for p in ai.describe()["providers"] if p["id"] == "anthropic")
        assert viewer_row["key_last4"] is None and "base_url" not in viewer_row

    def test_the_fallback_answers_when_the_primary_fails(self, ai):
        ai.save_provider("anthropic", enabled=True, api_key=KEY)
        ai.save_provider("deepseek", enabled=True, api_key=KEY)
        ai.save_settings(primary="anthropic", fallbacks=["deepseek"],
                         purposes={"news": True}, max_calls_per_hour=10,
                         max_calls_per_day=10)
        ai.wire.answers = [(500, "down"), _openai("from deepseek")]
        result = ai.complete("news", "s", "u")
        assert result is not None and result.provider == "deepseek"

    def test_the_hourly_budget_is_enforced(self, ai):
        ai.save_provider("openai", enabled=True, api_key=KEY)
        ai.save_settings(primary="openai", fallbacks=[], purposes={"news": True},
                         max_calls_per_hour=2, max_calls_per_day=100)
        ai.wire.answers = [_openai("1"), _openai("2"), _openai("3")]
        assert ai.complete("news", "s", "u") is not None
        assert ai.complete("news", "s", "u") is not None
        assert ai.complete("news", "s", "u") is None, "a third call breaks the budget"

    def test_a_switched_off_purpose_makes_no_call(self, ai):
        ai.save_provider("openai", enabled=True, api_key=KEY)
        ai.save_settings(primary="openai", fallbacks=[], purposes={"brief": False},
                         max_calls_per_hour=10, max_calls_per_day=10)
        assert ai.complete("brief", "s", "u") is None
        assert ai.wire.requests == []

    def test_the_licence_capability_is_respected(self, tmp_path):
        audit = AuditLog(tmp_path / "a.jsonl", fsync_every_record=False)
        wire = Wire([_openai("x")])
        service = AIService(tmp_path, audit, transport_post=wire.post,
                            capability_check=lambda cap: (False, "evaluation tier"))
        service.save_provider("openai", enabled=True, api_key=KEY)
        assert service.complete("news", "s", "u") is None
        assert "licence" in service.available("news")[1]

    def test_the_journal_records_the_call_but_not_the_prompt(self, ai, tmp_path):
        ai.save_provider("openai", enabled=True, api_key=KEY)
        ai.wire.answers = [_openai("answer")]
        ai.complete("news", "s", "account 12345 secret prompt")
        text = (tmp_path / "audit.jsonl").read_text()
        assert '"ai.call"' in text and "prompt_sha256" in text
        assert "12345 secret prompt" not in text and KEY not in text

    def test_bad_model_names_are_refused(self, ai):
        with pytest.raises(ValueError):
            ai.save_provider("gemini", enabled=True, model="../../v1/evil")
        ai.save_provider("custom", enabled=True, model="org/model",
                         base_url="https://llm.example.com/v1")

    def test_a_provider_test_lists_models(self, ai):
        ai.save_provider("openai", enabled=True, api_key=KEY)
        ai.wire.answers = [_openai('{"ok": true}')]
        report = ai.test_provider("openai")
        assert report["ok"] and report["models"] == ["model-a", "model-b"]


# --------------------------------------------------------------------------- #
# live news
# --------------------------------------------------------------------------- #


NOW = dt.datetime(2026, 9, 24, 10, tzinfo=dt.UTC)
NOW_NS = int(NOW.timestamp() * 1e9)
FF_ROWS = [
    {"title": "Non-Farm Employment Change", "country": "USD",
     "date": "2026-09-24T08:30:00-04:00", "impact": "High", "forecast": "180K",
     "previous": "142K"},
    {"title": "Bank Holiday", "country": "JPY", "date": "2026-09-24T00:00:00+09:00",
     "impact": "Holiday", "forecast": "", "previous": ""},
    {"title": "CPI y/y", "country": "XYZ", "date": "2026-09-24T09:00:00+00:00",
     "impact": "High"},
    {"title": "No zone", "country": "EUR", "date": "2026-09-24T09:00:00", "impact": "High"},
]


class TestCalendar:
    def test_rows_become_confirmed_events_and_junk_is_skipped(self):
        src = ForexFactoryCalendar(fetch=lambda url, t: json.dumps(FF_ROWS).encode())
        rows = src.fetch(NOW_NS - 86_400 * 10**9, NOW_NS + 86_400 * 10**9)
        assert len(rows) == 1
        nfp = rows[0]
        assert nfp["certainty"] == "confirmed" and nfp["currency"] == "USD"
        assert nfp["event_ns"] == int(dt.datetime(2026, 9, 24, 12, 30,
                                                  tzinfo=dt.UTC).timestamp() * 1e9)
        assert nfp["forecast"] == 180_000.0

    def test_a_confirmed_release_creates_a_real_blackout(self, tmp_path):
        cal = EconomicCalendar(tmp_path / "cal.db")
        src = ForexFactoryCalendar(fetch=lambda url, t: json.dumps(FF_ROWS).encode())
        report = cal.ingest(src, NOW_NS - 86_400 * 10**9, NOW_NS + 86_400 * 10**9)
        assert report.inserted == 1
        policy = NewsPolicy(cal)
        at_release = int(dt.datetime(2026, 9, 24, 12, 20, tzinfo=dt.UTC).timestamp() * 1e9)
        assessed = policy.assess(at_release, ["EUR_USD", "EUR_GBP"])
        assert assessed["EUR_USD"].blocked is True
        assert assessed["EUR_GBP"].blocked is False

    def test_a_moved_release_updates_one_row(self, tmp_path):
        cal = EconomicCalendar(tmp_path / "cal.db")
        moved = [dict(FF_ROWS[0], date="2026-09-25T08:30:00-04:00")]
        for rows in (FF_ROWS, moved):
            cal.ingest(ForexFactoryCalendar(fetch=lambda u, t, r=rows: json.dumps(r).encode()),
                       NOW_NS - 86_400 * 10**9, NOW_NS + 3 * 86_400 * 10**9)
        events = cal.upcoming(NOW_NS - 86_400 * 10**9, 5 * 86_400, min_impact="high")
        assert len(events) == 1 and "2026-09-25" in dt.datetime.fromtimestamp(
            events[0].event_ns / 1e9, tz=dt.UTC).isoformat()

    @pytest.mark.parametrize("text,value", [("3.2%", 3.2), ("215K", 215_000.0),
                                            ("-0.1%", -0.1), ("", None), ("n/a", None)])
    def test_numbers(self, text, value):
        assert parse_number(text) == value


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Fed</title>
<item><title>Federal Reserve issues FOMC statement</title>
<link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260924a.htm</link>
<description>The Committee decided to maintain the target range.</description>
<pubDate>Wed, 24 Sep 2026 18:00:00 GMT</pubDate></item></channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>BoE</title>
<entry><title>Bank Rate maintained at 4%</title><link href="https://boe.example/x"/>
<updated>2026-09-18T11:00:00Z</updated><summary>MPC vote 7-2.</summary></entry></feed>"""

EVIL = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>
<rss><channel><item><title>&lol;</title></item></channel></rss>"""

FED = Feed("fed", "Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml",
           ("USD",))


class TestOfficialFeeds:
    def test_rss(self):
        [a] = parse_feed(FED, RSS)
        assert a.title.startswith("Federal Reserve") and a.currencies == ["USD"]
        assert a.published_ns == int(dt.datetime(2026, 9, 24, 18,
                                                 tzinfo=dt.UTC).timestamp() * 1e9)

    def test_atom(self):
        [a] = parse_feed(Feed("boe", "BoE", "https://x", ("GBP",)), ATOM)
        assert a.link == "https://boe.example/x" and "7-2" in a.summary

    def test_entity_declarations_are_refused_before_parsing(self):
        with pytest.raises(FeedError, match="DTD"):
            parse_feed(FED, EVIL)

    def test_one_dead_feed_does_not_hide_the_others(self):
        feeds = [FED, Feed("dead", "Dead", "https://dead.example/x", ("EUR",))]

        def fetch(url, timeout):
            if "dead" in url:
                raise ConnectionError("unreachable")
            return RSS
        articles, errors = fetch_official(feeds, fetch=fetch)
        assert len(articles) == 1 and "dead" in errors


class TestNewsDesk:
    def _desk(self, ai, tmp_path, answer):
        ai.save_provider("openai", enabled=True, api_key=KEY)
        ai.wire.answers = [_openai(json.dumps(answer))]
        return NewsDesk(None, ai=ai, fetch=lambda url, t: RSS, feeds=(FED,))

    def test_a_confident_fresh_contradiction_blocks_then_expires(self, ai, tmp_path):
        answer = {"event_type": "monetary_policy", "currencies": ["USD"],
                  "direction_claim": "hawkish", "is_scheduled": True, "is_revision": False,
                  "is_correction": False, "contradicts_prior": True, "numeric_values": [],
                  "evidence_quotes": ["Federal Reserve issues FOMC statement"],
                  "confidence": 0.9, "novelty": "new"}
        desk = self._desk(ai, tmp_path, answer)
        desk.refresh_feeds(NOW_NS)
        policy = NewsPolicy(None, extractions_source=desk.recent_extractions)
        assert policy.assess(NOW_NS, ["EUR_USD"])["EUR_USD"].blocked is True
        later = NOW_NS + 5 * 3600 * 10**9
        assert policy.assess(later, ["EUR_USD"])["EUR_USD"].blocked is False, \
            "a contradiction flag must not block for a whole day"

    def test_a_fabricated_quote_is_not_used(self, ai, tmp_path):
        answer = {"event_type": "monetary_policy", "currencies": ["USD"],
                  "direction_claim": "hawkish", "is_scheduled": True, "is_revision": False,
                  "is_correction": False, "contradicts_prior": True, "numeric_values": [],
                  "evidence_quotes": ["The Fed will hike 100bp tomorrow"],
                  "confidence": 0.99, "novelty": "new"}
        desk = self._desk(ai, tmp_path, answer)
        desk.refresh_feeds(NOW_NS)
        assert desk.recent_extractions(NOW_NS) == []

    def test_with_no_ai_the_desk_still_collects_headlines(self, tmp_path):
        desk = NewsDesk(None, ai=None, fetch=lambda url, t: RSS, feeds=(FED,))
        desk.refresh_feeds(NOW_NS)
        assert desk.headlines()[0]["source"] == "Federal Reserve"
        assert desk.status.ai_available is False


# --------------------------------------------------------------------------- #
# the coach
# --------------------------------------------------------------------------- #


class TestCoach:
    def test_losses_are_reviewed_and_stored(self, ai, tmp_path):
        memory = MemoryStore(tmp_path / "mem.db")
        memory.record_autopsy({"trade_id": "L1", "strategy": "donchian_trend",
                               "instrument": "EUR_USD", "outcome": "loss", "mode": "stop",
                               "r_multiple": -1.0, "mae_r": -1.0, "mfe_r": 0.4,
                               "capture_ratio": 0.0, "tags": [], "counterfactuals": []})
        ai.save_provider("anthropic", enabled=True, api_key=KEY)
        ai.wire.answers = [_anthropic(json.dumps({
            "summary_fa": "معامله با حد ضرر بسته شد.", "what_went_right_fa": None,
            "what_went_wrong_fa": "ورود دیر بود.", "category": "late_entry",
            "avoidable": True, "suggestion_fa": "آزمایش ورود زودتر.", "confidence": 0.6}))]
        coach = TradeCoach(ai, memory)
        assert coach.tick() == 1
        [review] = ai.store.reviews()
        assert review["payload"]["category"] == "late_entry"
        assert review["payload"]["advisory_only"] is True
        assert coach.tick() == 0, "a reviewed trade is not reviewed twice"
        assert coach.themes()["categories"][0]["category"] == "late_entry"

    def test_an_unknown_category_is_normalised(self):
        assert validate_review({"summary_fa": "x", "category": "buy_more"})["category"] \
            == "other"

    def test_an_empty_review_is_refused(self):
        with pytest.raises(ValueError):
            validate_review({"category": "other"})


# --------------------------------------------------------------------------- #
# manual tickets
# --------------------------------------------------------------------------- #


EU = Instrument("EUR_USD", "EUR", "USD")
T0 = int(dt.datetime(2026, 3, 3, 10, tzinfo=dt.timezone.utc).timestamp() * 1e9)


def _agent(tmp_path, **agent_kw):
    broker = PaperBroker(instruments={"EUR_USD": EU}, starting_balance=D("10000"),
                         profile=SimProfile(last_look_reject_prob=0.0,
                                            slippage_pips_mean=D("0"),
                                            slippage_pips_sigma=D("0")),
                         seed=3, start_ns=T0)
    broker.on_quote(Quote("EUR_USD", D("1.08497"), D("1.08503"), ts_ns=T0))
    agent_kw.setdefault("session_windows_utc", [[0, 24]])
    agent_kw.setdefault("trade_days", [0, 1, 2, 3, 4, 5, 6])
    cfg = SentinelConfig(
        agent=AgentConfig(mode=AgentMode.ADVISORY, **agent_kw),
        execution=ExecutionConfig(broker="paper"),
        ops=OpsConfig(state_dir=str(tmp_path), killswitch_file=str(tmp_path / "KILL"),
                      audit_log=str(tmp_path / "audit.jsonl")))
    agent = Agent(cfg, broker, MarketFeed(broker, BarStore(tmp_path / "m.db")),
                  AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False),
                  MemoryStore(tmp_path / "mem.db"),
                  proposals=ProposalQueue(str(tmp_path / "p.json")),
                  clock_fn=lambda: broker.now_ns)
    # The empty bar store would veto on data quality; the manual path's own
    # gates are what these tests are about.
    from sentinel.core.types import DataQuality
    agent.feed.store.passport = lambda *a, **k: type(
        "P", (), {"quality": DataQuality.OK, "age_sec": 0.0})()
    agent.start()
    return agent, broker


TICKET = dict(instrument="EUR_USD", side="BUY", stop_loss=D("1.08003"),
              take_profit=D("1.09503"), by="owner")


class TestManualTicket:
    def test_a_preview_sizes_and_sends_nothing(self, tmp_path):
        agent, broker = _agent(tmp_path)
        d = agent.manual_order(**TICKET, preview=True)
        assert d.action == "preview", d.vetoes
        assert D(d.lots) > 0 and D(d.risk_pct) <= D("0.50")
        assert broker.positions() == []

    def test_a_ticket_executes_through_the_engine(self, tmp_path):
        agent, broker = _agent(tmp_path)
        d = agent.manual_order(**TICKET)
        assert d.action == "executed", d.vetoes
        assert broker.positions()[0].strategy == "manual"
        assert agent._position_meta["EUR_USD"]["strategy"] == "manual"

    def test_a_ticket_can_ask_for_less_risk_never_more(self, tmp_path):
        agent, _ = _agent(tmp_path)
        full = agent.manual_order(**TICKET, preview=True)
        half = agent.manual_order(**TICKET, risk_pct=D("0.25"), preview=True)
        more = agent.manual_order(**TICKET, risk_pct=D("2.0"), preview=True)
        assert D(half.lots) < D(full.lots)
        assert D(more.lots) == D(full.lots), "a ticket cannot raise the risk budget"

    def test_the_engine_still_vetoes(self, tmp_path):
        agent, broker = _agent(tmp_path)
        d = agent.manual_order(**{**TICKET, "stop_loss": D("1.08450")})   # 5 pips
        assert d.action == "vetoed"
        assert any(v["rule"] == "stop_too_tight" for v in d.vetoes)
        assert broker.positions() == []

    def test_real_money_is_off_until_the_owner_enables_it(self, tmp_path):
        agent, broker = _agent(tmp_path)
        agent.config.execution.venue_mode = ExecutionVenueMode.LIVE
        d = agent.manual_order(**TICKET)
        assert d.action == "vetoed" and d.vetoes[0]["rule"] == "manual_live_disabled"
        agent.config.agent.manual_trading_live = True
        assert agent.manual_order(**TICKET, preview=True).action == "preview"

    def test_out_of_session_is_refused(self, tmp_path):
        agent, _ = _agent(tmp_path, session_windows_utc=[[0, 1]])
        d = agent.manual_order(**TICKET, preview=True)
        assert d.action == "vetoed" and d.vetoes[0]["rule"] == "out_of_session"

    def test_a_halted_agent_takes_no_manual_risk(self, tmp_path):
        agent, broker = _agent(tmp_path)
        agent.halt("test")
        d = agent.manual_order(**TICKET)
        assert d.action == "vetoed" and any(v["rule"] == "halted" for v in d.vetoes)


@pytest.fixture
def api(tmp_path):
    os.environ["SENTINEL_JWT_SECRET"] = "t" * 48
    agent, broker = _agent(tmp_path)
    runtime = Runtime(agent, tmp_path / "config.json")
    runtime.ai = AIService(tmp_path, agent.audit, transport_post=Wire().post)
    security = SecurityManager(agent.audit, secret="t" * 48)
    owner, _ = security.add_user("owner1", "a-sufficiently-long-password", "owner")
    security.add_user("viewer1", "another-long-password-x", "viewer")
    client = TestClient(create_app(runtime, security))

    def login(name, pw):
        r = client.post("/api/auth/login", json={"username": name, "password": pw})
        return {"Authorization": f"Bearer {r.json()['token']}"}
    return {"client": client, "owner": owner, "broker": broker, "runtime": runtime,
            "oh": login("owner1", "a-sufficiently-long-password"),
            "vh": login("viewer1", "another-long-password-x")}


class TestApi:
    def test_preview_is_a_read_but_not_for_viewers(self, api):
        q = "/api/trade/preview?instrument=EUR_USD&side=BUY&stop_loss=1.08003&take_profit=1.09503"
        assert api["client"].get(q, headers=api["vh"]).status_code == 403
        r = api["client"].get(q, headers=api["oh"])
        assert r.status_code == 200 and r.json()["action"] == "preview"

    def test_a_manual_order_needs_the_second_factor(self, api):
        body = {"instrument": "EUR_USD", "side": "BUY", "stop_loss": "1.08003",
                "take_profit": "1.09503"}
        assert api["client"].post("/api/trade/manual", json=body,
                                  headers=api["oh"]).status_code == 403
        h = dict(api["oh"], **{"X-TOTP": pyotp.TOTP(api["owner"].totp_secret).now()})
        r = api["client"].post("/api/trade/manual", json=body, headers=h)
        assert r.status_code == 200 and r.json()["action"] == "executed", r.text

    def test_a_malformed_ticket_is_rejected_at_the_edge(self, api):
        h = dict(api["oh"], **{"X-TOTP": pyotp.TOTP(api["owner"].totp_secret).now()})
        r = api["client"].post("/api/trade/manual", headers=h, json={
            "instrument": "EUR_USD", "side": "BUY", "stop_loss": "1e9"})
        assert r.status_code == 422

    def test_the_ai_key_is_write_only(self, api):
        h = dict(api["oh"], **{"X-TOTP": pyotp.TOTP(api["owner"].totp_secret).now()})
        r = api["client"].post("/api/ai/provider", headers=h, json={
            "provider": "openai", "enabled": True, "api_key": KEY})
        assert r.status_code == 200 and KEY not in r.text
        for headers in (api["oh"], api["vh"]):
            assert KEY not in api["client"].get("/api/ai", headers=headers).text

    def test_viewers_cannot_configure_ai(self, api):
        r = api["client"].post("/api/ai/provider", headers=api["vh"],
                               json={"provider": "openai", "enabled": True})
        assert r.status_code == 403
