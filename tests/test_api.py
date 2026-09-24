"""API surface: authentication, authorisation, and that reads never mutate."""

import os
import tempfile
from decimal import Decimal as D
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from sentinel.agent.memory import MemoryStore
from sentinel.agent.orchestrator import Agent
from sentinel.agent.proposals import ProposalQueue
from sentinel.api.main import create_app
from sentinel.api.security import SecurityManager
from sentinel.api.state import Runtime
from sentinel.brokers.paper import PaperBroker
from sentinel.core.audit import AuditLog
from sentinel.core.config import (
    AgentConfig, AgentMode, ExecutionConfig, OpsConfig, SecurityConfig, SentinelConfig,
)
from sentinel.core.money import Instrument
from sentinel.data.feed import BarStore, MarketFeed

INSTRUMENTS = {"EUR_USD": Instrument("EUR_USD", "EUR", "USD")}


@pytest.fixture
def system(tmp_path):
    os.environ["SENTINEL_JWT_SECRET"] = "t" * 48
    config = SentinelConfig(
        agent=AgentConfig(mode=AgentMode.ADVISORY),
        execution=ExecutionConfig(broker="paper"),
        ops=OpsConfig(state_dir=str(tmp_path), killswitch_file=str(tmp_path / "KILL"),
                      audit_log=str(tmp_path / "audit.jsonl")),
        security=SecurityConfig(bind_host="127.0.0.1"),
    )
    audit = AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False)
    broker = PaperBroker(instruments=INSTRUMENTS, starting_balance=D("10000"))
    store = BarStore(tmp_path / "m.db")
    feed = MarketFeed(broker, store)
    memory = MemoryStore(tmp_path / "mem.db")
    agent = Agent(config, broker, feed, audit, memory,
                  proposals=ProposalQueue(str(tmp_path / "p.json")))
    runtime = Runtime(agent, tmp_path / "config.json")
    security = SecurityManager(audit, secret="t" * 48)
    user, _ = security.add_user("owner1", "a-sufficiently-long-password", "owner")
    viewer, _ = security.add_user("viewer1", "another-long-password-x", "viewer")
    app = create_app(runtime, security)
    client = TestClient(app)
    return {"client": client, "runtime": runtime, "security": security,
            "owner": user, "viewer": viewer, "agent": agent}


def auth(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


class TestAuth:
    def test_read_requires_a_session(self, system):
        assert system["client"].get("/api/status").status_code == 401

    def test_bad_password_is_rejected(self, system):
        r = system["client"].post("/api/auth/login",
                                  json={"username": "owner1", "password": "wrong"})
        assert r.status_code == 401

    def test_forged_token_is_rejected(self, system):
        r = system["client"].get("/api/status",
                                 headers={"Authorization": "Bearer not.a.token"})
        assert r.status_code == 401

    def test_login_then_read(self, system):
        h = auth(system["client"], "owner1", "a-sufficiently-long-password")
        r = system["client"].get("/api/status", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "advisory"
        assert body["broker"]["name"] == "paper"


class TestWriteRequiresSecondFactor:
    def test_write_without_totp_is_denied(self, system):
        h = auth(system["client"], "owner1", "a-sufficiently-long-password")
        r = system["client"].post("/api/control/halt", json={"reason": "x"}, headers=h)
        assert r.status_code == 403
        assert system["agent"].halted is False

    def test_write_with_totp_succeeds(self, system):
        h = auth(system["client"], "owner1", "a-sufficiently-long-password")
        h["X-TOTP"] = pyotp.TOTP(system["owner"].totp_secret).now()
        r = system["client"].post("/api/control/halt", json={"reason": "test halt"},
                                  headers=h)
        assert r.status_code == 200
        assert system["agent"].halted is True

    def test_viewer_cannot_write_even_with_totp(self, system):
        h = auth(system["client"], "viewer1", "another-long-password-x")
        h["X-TOTP"] = pyotp.TOTP(system["viewer"].totp_secret).now()
        r = system["client"].post("/api/control/kill", json={"reason": "x"}, headers=h)
        assert r.status_code == 403
        assert system["agent"].kill.read().engaged is False


class TestRiskChangesNeedOwner:
    def test_operator_cannot_change_config(self, system):
        op, _ = system["security"].add_user("op2", "operator-password-long", "operator")
        h = auth(system["client"], "op2", "operator-password-long")
        h["X-TOTP"] = pyotp.TOTP(op.totp_secret).now()
        r = system["client"].post("/api/config",
                                  json={"patch": {"risk": {"risk_per_trade_pct": "1.0"}}},
                                  headers=h)
        assert r.status_code == 403

    def test_owner_can_change_config_and_it_is_validated(self, system):
        h = auth(system["client"], "owner1", "a-sufficiently-long-password")
        h["X-TOTP"] = pyotp.TOTP(system["owner"].totp_secret).now()
        r = system["client"].post("/api/config",
                                  json={"patch": {"risk": {"risk_per_trade_pct": "0.25"}}},
                                  headers=h)
        assert r.status_code == 200, r.text
        assert system["agent"].config.risk.risk_per_trade_pct == D("0.25")
        # the engine must pick up the new limits, not keep the old object
        assert system["agent"].risk.config.risk_per_trade_pct == D("0.25")

    def test_invalid_config_is_refused(self, system):
        h = auth(system["client"], "owner1", "a-sufficiently-long-password")
        h["X-TOTP"] = pyotp.TOTP(system["owner"].totp_secret).now()
        before = system["agent"].config.risk.daily_loss_limit_pct
        r = system["client"].post(
            "/api/config",
            json={"patch": {"risk": {"daily_loss_limit_pct": "99.0"}}}, headers=h)
        assert r.status_code == 400
        assert system["agent"].config.risk.daily_loss_limit_pct == before


class TestSecurityHeaders:
    def test_headers_present(self, system):
        h = auth(system["client"], "owner1", "a-sufficiently-long-password")
        r = system["client"].get("/api/status", headers=h)
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]
        assert r.headers["Cache-Control"] == "no-store"


class TestPublicBindRefused:
    def test_public_bind_raises(self, tmp_path, system):
        cfg = system["agent"].config
        cfg.security.bind_host = "0.0.0.0"  # noqa: S104 - deliberate negative test
        os.environ.pop("SENTINEL_ALLOW_PUBLIC_BIND", None)
        with pytest.raises(RuntimeError, match="loopback"):
            create_app(system["runtime"], system["security"])


class TestAuditChain:
    def test_actions_are_recorded_and_chain_holds(self, system):
        h = auth(system["client"], "owner1", "a-sufficiently-long-password")
        h["X-TOTP"] = pyotp.TOTP(system["owner"].totp_secret).now()
        system["client"].post("/api/control/halt", json={"reason": "audit test"}, headers=h)
        r = system["client"].get("/api/audit", headers={"Authorization": h["Authorization"]})
        assert r.status_code == 200
        body = r.json()
        assert body["chain_valid"] is True
        events = {rec["event"] for rec in body["records"]}
        assert "sec.auth_ok" in events
        assert "sec.write_action" in events or "risk.halt" in events
