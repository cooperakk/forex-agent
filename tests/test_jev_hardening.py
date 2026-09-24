"""The System One model (Jev) earns authority; it is not granted it.

Each test pins one property of the hardening:

* a bare pick with no number is NOT certainty (it cannot satisfy a confidence
  gate), and a stated confidence is used when the full distribution is absent;
* shadow is the default: Jev is asked and recorded and changes nothing, while a
  text model feeds the filter and doubles as a second opinion;
* shrink_only can shrink but never block; active needs labelled evidence on the
  version that is answering;
* a new version answering demotes Jev to shadow until the owner accepts it,
  and its calibration starts from zero;
* a 429 opens an escalating breaker: no traffic, then recovery on success;
* calibration statistics report discrimination, not just calibration.
"""

from __future__ import annotations

import json
import os

import pyotp
import pytest
from fastapi.testclient import TestClient

from sentinel.ai import AIService
from sentinel.ai import calibration as cal
from sentinel.ai.jev import extraction_from_answers
from sentinel.ai.providers import ProviderError, served_model_id
from sentinel.api.main import create_app
from sentinel.api.security import SecurityManager
from sentinel.api.state import Runtime
from sentinel.core.audit import AuditLog
from sentinel.news.desk import NewsDesk
from sentinel.news.feeds import Feed
from tests.test_ai_news_manual import (  # noqa: F401 - shared fakes
    KEY,
    NOW_NS,
    RSS,
    JevWire,
    Wire,
    _agent,
    _openai,
)

FED = Feed("fed", "Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml",
           ("USD",))
TEXT_ANSWER = {"event_type": "monetary_policy", "currencies": ["USD"],
               "direction_claim": "hawkish", "is_scheduled": True, "is_revision": False,
               "is_correction": False, "contradicts_prior": False, "numeric_values": [],
               "evidence_quotes": ["Federal Reserve issues FOMC statement"],
               "confidence": 0.8, "novelty": "new"}


class VersionedJev(JevWire):
    """A Jev fake that reports a served version and can answer 429."""

    def __init__(self, *, version="jev-2026-09-15", status=200, **kw):
        super().__init__(**kw)
        self.version = version
        self.status = status

    def post(self, url, *, headers, json_body, timeout):
        code, text = super().post(url, headers=headers, json_body=json_body, timeout=timeout)
        if self.status != 200:
            return self.status, '{"detail": "slow down"}'
        body = json.loads(text)
        body["model"] = self.version
        return code, json.dumps(body)


def _ai(tmp_path, jev, *, text=None, text_first=False):
    audit = AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False)
    text_wire = text or Wire()

    def post(url, **kw):
        return (jev.post if "typesafe" in url else text_wire.post)(url, **kw)
    ai = AIService(tmp_path, audit, transport_post=post, transport_get=Wire().get)
    ai.save_provider("jev", enabled=True, api_key="ts-test-key-0123456789")
    chain = ["jev"]
    if text is not None:
        ai.save_provider("openai", enabled=True, api_key=KEY)
        chain = ["openai", "jev"] if text_first else ["jev", "openai"]
    ai.save_settings(primary=chain[0], fallbacks=chain[1:],
                     purposes={"news": True, "coach": True, "brief": True},
                     max_calls_per_hour=500, max_calls_per_day=5000)
    return ai


def _desk(ai):
    return NewsDesk(None, ai=ai, fetch=lambda url, t: RSS, feeds=(FED,))


# --------------------------------------------------------------------------- #
# parsing: a pick without a number is not certainty
# --------------------------------------------------------------------------- #


def _answers(choice_answer, noul=0.05):
    out = {}
    for q in ("event_type", "event_type_rev", "direction", "direction_rev"):
        out[q] = dict(choice_answer, choice=("monetary_policy" if q.startswith("event")
                                             else "hawkish"))
    for q in ("is_correction", "is_revision", "contradicts_prior", "is_scheduled"):
        out[q] = {"noul": 0.9 if q == "contradicts_prior" else noul}
    return out


class TestParsing:
    def test_a_bare_pick_has_unknown_confidence_and_cannot_block(self):
        ex = extraction_from_answers("a", "h", _answers({}), model="jev", currencies=["USD"],
                                     latency_ms=1)
        assert ex.event_type == "monetary_policy" and ex.contradicts_prior is True
        assert ex.confidence == 0.0, "no number was given, so no confidence is claimed"
        assert {"name": "confidence_known", "value": 0} in ex.numeric_values

    def test_a_stated_confidence_is_used_when_there_is_no_distribution(self):
        ex = extraction_from_answers("a", "h", _answers({"confidence": 0.7}), model="jev",
                                     currencies=["USD"], latency_ms=1)
        assert ex.confidence == pytest.approx(0.7)
        assert {"name": "confidence_known", "value": 1} in ex.numeric_values

    def test_out_of_range_numbers_are_not_probabilities(self):
        bad = {"probabilities": {"monetary_policy": 7, "inflation": -2}, "confidence": 3}
        ex = extraction_from_answers("a", "h", _answers(bad), model="jev",
                                     currencies=["USD"], latency_ms=1)
        assert ex.confidence == 0.0

    def test_the_desk_does_not_honour_a_block_of_unknown_confidence(self, tmp_path):
        from sentinel.news.llm_extract import Extraction
        desk = NewsDesk(None)
        ex = Extraction(article_id="a", event_type="monetary_policy", currencies=["USD"],
                        direction_claim="hawkish", is_scheduled=False, is_revision=False,
                        is_correction=False, contradicts_prior=True, confidence=0.0)
        desk._extractions["a"] = (NOW_NS, ex)
        [out] = desk.recent_extractions(NOW_NS)
        assert out.contradicts_prior is False

    @pytest.mark.parametrize("body,want", [
        ({"model": "jev-2026-09-15"}, "jev-2026-09-15"),
        ({"model_version": "v3.1", "model": "jev-latest"}, "v3.1"),
        ({"model": "bad model<script>"}, ""),
        ({"model": "x" * 200}, ""),
        ({}, ""),
    ])
    def test_the_served_version_is_validated(self, body, want):
        assert served_model_id(body) == want


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #


class TestModes:
    def test_shadow_is_the_default_and_changes_nothing(self, tmp_path):
        ai = _ai(tmp_path, VersionedJev(noul={"contradicts_prior": 0.95}))
        assert ai.jev_mode() == "shadow"
        desk = _desk(ai)
        desk.refresh_feeds(NOW_NS)
        assert desk.recent_extractions(NOW_NS) == [], "shadow feeds nothing to the policy"
        row = desk.headlines()[0]
        assert row["jev"]["mode"] == "shadow" and row["jev"]["used"] is False
        assert row["jev"]["p_contradicts_prior"] == 0.95
        assert ai.jev_report()["answers"] == 1

    def test_in_shadow_the_text_model_decides_and_is_compared(self, tmp_path):
        text = Wire([_openai(json.dumps(TEXT_ANSWER))])
        ai = _ai(tmp_path, VersionedJev(noul={"contradicts_prior": 0.95}), text=text)
        desk = _desk(ai)
        desk.refresh_feeds(NOW_NS)
        [ex] = desk.recent_extractions(NOW_NS)
        assert not ex.model.startswith("jev") and ex.contradicts_prior is False
        agreement = ai.jev_report()["agreement_with_text_model"]
        assert agreement["n"] == 1 and agreement["contradiction"] == 0.0
        assert agreement["direction"] == 1.0

    def test_shrink_only_can_shrink_but_never_block(self, tmp_path):
        ai = _ai(tmp_path, VersionedJev(noul={"contradicts_prior": 0.95,
                                              "is_correction": 0.9}))
        ai.set_jev_mode("shrink_only", by="owner")
        desk = _desk(ai)
        desk.refresh_feeds(NOW_NS)
        [ex] = desk.recent_extractions(NOW_NS)
        assert ex.is_correction is True and ex.contradicts_prior is False

    def test_active_needs_evidence_on_this_version(self, tmp_path):
        ai = _ai(tmp_path, VersionedJev())
        _desk(ai).refresh_feeds(NOW_NS)
        with pytest.raises(ValueError, match="labelled headlines"):
            ai.set_jev_mode("active", by="owner")

    def test_labels_open_the_gate_and_active_blocks(self, tmp_path, monkeypatch):
        import sentinel.ai.service as svc
        monkeypatch.setattr(svc, "JEV_GATE_MIN_LABELS", 1)
        jev = VersionedJev(noul={"contradicts_prior": 0.9})
        ai = _ai(tmp_path, jev)
        desk = _desk(ai)
        desk.refresh_feeds(NOW_NS)
        aid = ai.jev_report()["recent"][0]["article_id"]
        report = ai.save_jev_labels([{"article_id": aid, "is_correction": False,
                                      "contradicts_prior": True, "direction": "hawkish"}],
                                    by="owner")
        assert report["gate"]["passed"], report["gate"]
        ai.set_jev_mode("active", by="owner")
        desk2 = _desk(ai)
        ai.store._conn.execute("DELETE FROM extractions")
        desk2.refresh_feeds(NOW_NS)
        [ex] = desk2.recent_extractions(NOW_NS)
        assert ex.contradicts_prior is True and ex.model.startswith("jev:")

    def test_unknown_modes_and_labels_are_refused(self, tmp_path):
        ai = _ai(tmp_path, VersionedJev())
        with pytest.raises(ValueError):
            ai.set_jev_mode("yolo", by="owner")
        with pytest.raises(ValueError):
            ai.save_jev_labels([{"article_id": "x", "direction": "sideways"}], by="owner")
        assert ai.save_jev_labels([{"article_id": "nope"}], by="owner")["labelled"] == 0


# --------------------------------------------------------------------------- #
# version guard
# --------------------------------------------------------------------------- #


class TestVersionGuard:
    def test_a_new_version_demotes_to_shadow_until_accepted(self, tmp_path):
        jev = VersionedJev(version="jev-a")
        ai = _ai(tmp_path, jev)
        ai.classify_news("a:1", "Bank holds rates", "", ["USD"])
        assert ai.jev_report()["known_version"] == "jev-a"
        ai.set_jev_mode("shrink_only", by="owner")
        jev.version = "jev-b"
        ai.classify_news("a:2", "Bank holds rates", "", ["USD"])
        report = ai.jev_report()
        assert report["mode"] == "shadow" and report["pending_version"] == "jev-b"
        with pytest.raises(ValueError, match="new Jev version"):
            ai.set_jev_mode("shrink_only", by="owner")
        report = ai.accept_jev_version(by="owner")
        assert report["known_version"] == "jev-b" and report["pending_version"] == ""
        # Only the new version's own answer counts; the old one's evidence
        # (answers and labels) does not carry over.
        assert report["answers"] == 1 and report["labelled"] == 0
        records = [json.loads(line) for line in
                   (tmp_path / "audit.jsonl").read_text().splitlines()]
        actions = [r["payload"].get("action") for r in records if "payload" in r]
        assert "jev_version_changed" in actions and "jev_version_accepted" in actions

    def test_an_unreported_version_is_keyed_by_the_configured_name(self, tmp_path):
        ai = _ai(tmp_path, JevWire())         # the plain fake reports "jev-latest"
        ai.classify_news("a:1", "x", "", ["USD"])
        report = ai.jev_report()
        assert report["known_version"] == "jev-latest" and report["floating_alias"] is True


# --------------------------------------------------------------------------- #
# breaker
# --------------------------------------------------------------------------- #


class TestBreaker:
    def test_a_429_opens_an_escalating_breaker_and_a_success_closes_it(self, tmp_path):
        jev = VersionedJev(status=429)
        ai = _ai(tmp_path, jev)
        assert ai.classify_news("a:1", "x", "", ["USD"]) is None
        first = ai.breakers()["jev"]
        assert first["open"] and 55 <= first["seconds_left"] <= 60
        calls = len(jev.requests)
        assert ai.classify_news("a:2", "x", "", ["USD"]) is None
        assert len(jev.requests) == calls, "no traffic while the breaker is open"
        ai._breaker_note("jev", ProviderError("again", status=429))
        assert ai.breakers()["jev"]["seconds_left"] > 60, "the cooldown escalates"
        ai._breakers["jev"]["open_until_ns"] = 0
        jev.status = 200
        assert ai.classify_news("a:3", "x", "", ["USD"]) is not None
        assert ai.breakers()["jev"]["open"] is False and ai.breakers()["jev"]["failures"] == 0

    def test_other_errors_do_not_open_it(self, tmp_path):
        ai = _ai(tmp_path, VersionedJev(status=500))
        ai.classify_news("a:1", "x", "", ["USD"])
        assert ai.breakers()["jev"]["open"] is False

    def test_text_providers_are_protected_too(self, tmp_path):
        text = Wire(status=429)
        ai = _ai(tmp_path, VersionedJev(), text=text, text_first=True)
        assert ai.complete("brief", "s", "u") is None
        n = len(text.requests)
        assert ai.complete("brief", "s", "u") is None and len(text.requests) == n


# --------------------------------------------------------------------------- #
# calibration maths
# --------------------------------------------------------------------------- #


class TestCalibration:
    def test_a_coin_flip_is_calibrated_and_useless(self):
        pairs = [(0.5, True), (0.5, False)] * 20
        s = cal.summarise(pairs, 0.5)
        assert s["brier"] == 0.25 and s["skill"] == 0.0 and s["auc"] == 0.5

    def test_a_discriminating_model_has_skill(self):
        pairs = [(0.9, True)] * 5 + [(0.1, False)] * 15
        s = cal.summarise(pairs, 0.75)
        assert s["skill"] > 0.9 and s["auc"] == 1.0
        assert s["decision"]["accuracy"] == 1.0

    def test_undefined_statistics_are_none_not_zero(self):
        s = cal.summarise([(0.2, False)] * 5, 0.75)
        assert s["auc"] is None and s["skill"] is None
        assert cal.summarise([], 0.5)["brier"] is None

    def test_reliability_bins(self):
        bins = cal.reliability([(0.1, False), (0.15, False), (0.9, True), (1.0, True)])
        assert bins[0]["n"] == 2 and bins[0]["observed"] == 0.0
        assert bins[-1]["n"] == 2 and bins[-1]["observed"] == 1.0


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(tmp_path):
    os.environ["SENTINEL_JWT_SECRET"] = "t" * 48
    agent, _broker = _agent(tmp_path)
    runtime = Runtime(agent, tmp_path / "config.json")
    runtime.ai = _ai(tmp_path, VersionedJev())
    security = SecurityManager(agent.audit, secret="t" * 48)
    owner, _ = security.add_user("owner1", "a-sufficiently-long-password", "owner")
    security.add_user("viewer1", "another-long-password-x", "viewer")
    client = TestClient(create_app(runtime, security))

    def login(name, pw):
        r = client.post("/api/auth/login", json={"username": name, "password": pw})
        return {"Authorization": f"Bearer {r.json()['token']}"}
    oh = login("owner1", "a-sufficiently-long-password")

    def totp():
        security._used_totp.clear()
        return dict(oh, **{"X-TOTP": pyotp.TOTP(owner.totp_secret).now()})
    return {"client": client, "runtime": runtime, "oh": oh, "totp": totp,
            "vh": login("viewer1", "another-long-password-x")}


class TestApi:
    def test_the_report_is_readable_and_changes_need_the_owner(self, api):
        c = api["client"]
        assert c.get("/api/ai/jev", headers=api["vh"]).json()["mode"] == "shadow"
        assert c.post("/api/ai/jev/mode", json={"mode": "shrink_only"},
                      headers=api["vh"]).status_code == 403
        assert c.post("/api/ai/jev/mode", json={"mode": "shrink_only"},
                      headers=api["oh"]).status_code == 403          # no second factor
        r = c.post("/api/ai/jev/mode", json={"mode": "shrink_only"}, headers=api["totp"]())
        assert r.status_code == 200 and r.json()["mode"] == "shrink_only"

    def test_active_without_evidence_is_a_conflict(self, api):
        r = api["client"].post("/api/ai/jev/mode", json={"mode": "active"},
                               headers=api["totp"]())
        assert r.status_code == 409 and "labelled" in r.text

    def test_labels_are_a_batch_under_one_code(self, api):
        ai = api["runtime"].ai
        for i in range(3):
            ai.classify_news(f"a:{i}", "Bank holds rates", "", ["USD"])
        body = {"labels": [{"article_id": f"a:{i}", "is_correction": False,
                            "contradicts_prior": False} for i in range(3)]}
        r = api["client"].post("/api/ai/jev/labels", json=body, headers=api["totp"]())
        assert r.status_code == 200 and r.json()["labelled"] == 3
        bad = {"labels": [{"article_id": "a:0", "direction": "sideways"}]}
        assert api["client"].post("/api/ai/jev/labels", json=bad,
                                  headers=api["totp"]()).status_code == 422

    def test_accepting_when_nothing_changed_is_a_conflict(self, api):
        r = api["client"].post("/api/ai/jev/accept-version", headers=api["totp"]())
        assert r.status_code == 409
