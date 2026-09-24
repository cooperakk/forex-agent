"""The venue, account and licence endpoints, exercised through real HTTP.

Unit tests prove a guard works when it is called. These prove it is actually
WIRED: that the role gate is on the route, that the second factor is demanded,
that no response body carries a secret, and that a viewer sees a 403 rather
than a screen full of account numbers. Every defect this file covers was one
where the guarded function was correct and the endpoint reached past it.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal as D
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from sentinel.agent.memory import MemoryStore
from sentinel.agent.orchestrator import Agent
from sentinel.agent.proposals import ProposalQueue
from sentinel.api.main import create_app
from sentinel.api.security import SecurityManager, UserStore
from sentinel.api.state import Runtime
from sentinel.brokers.paper import PaperBroker
from sentinel.core.audit import AuditLog
from sentinel.core.config import (
    AgentConfig, AgentMode, ExecutionConfig, OpsConfig, SecurityConfig, SentinelConfig,
)
from sentinel.core.money import Instrument
from sentinel.data.feed import BarStore, MarketFeed

INSTRUMENTS = {"EUR_USD": Instrument("EUR_USD", "EUR", "USD")}
OWNER_PW = "a-sufficiently-long-password"
HAND_PW = "another-long-password-x"
EYES_PW = "a-third-long-password-yz"


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
    agent = Agent(config, broker, MarketFeed(broker, BarStore(tmp_path / "m.db")),
                  audit, MemoryStore(tmp_path / "mem.db"),
                  proposals=ProposalQueue(str(tmp_path / "p.json")))
    runtime = Runtime(agent, tmp_path / "config.json")
    security = SecurityManager(audit, secret="t" * 48,
                               store=UserStore(tmp_path / "users.db"))
    owner, _ = security.add_user("owner1", OWNER_PW, "owner")
    hand, _ = security.add_user("hand1", HAND_PW, "operator")
    eyes, _ = security.add_user("eyes1", EYES_PW, "viewer")
    return {"client": TestClient(create_app(runtime, security)),
            "runtime": runtime, "security": security, "tmp": tmp_path,
            "owner": owner, "hand": hand, "eyes": eyes}


def auth(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def totp(user):
    return pyotp.TOTP(user.totp_secret).now()


def write(system, who, path, body):
    """A real authenticated, second-factor-authorised POST.

    The replay cache is cleared first. A TOTP code is single-use inside its
    window -- correctly, and there is a test for that -- but a test making
    several writes in one 30-second window would otherwise be exercising the
    replay guard rather than the endpoint it is aiming at.
    """
    system["security"]._used_totp.clear()
    headers = auth(system["client"], who.username,
                   {"owner1": OWNER_PW, "hand1": HAND_PW, "eyes1": EYES_PW}[who.username])
    headers["X-TOTP"] = totp(who)
    return system["client"].post(path, json=body, headers=headers)


# --------------------------------------------------------------------------- #
# accounts
# --------------------------------------------------------------------------- #


class TestAccountEndpoints:
    def test_the_account_list_is_owner_only(self, system):
        c = system["client"]
        assert c.get("/api/users", headers=auth(c, "owner1", OWNER_PW)).status_code == 200
        # An operator can close positions but has no business knowing which
        # names exist and which of them can move money.
        assert c.get("/api/users", headers=auth(c, "hand1", HAND_PW)).status_code == 403
        assert c.get("/api/users", headers=auth(c, "eyes1", EYES_PW)).status_code == 403

    def test_the_listing_contains_no_hash_and_no_totp_secret(self, system):
        c = system["client"]
        body = c.get("/api/users", headers=auth(c, "owner1", OWNER_PW)).text
        assert "password_hash" not in body and "totp_secret" not in body
        assert "$argon2" not in body
        assert system["owner"].totp_secret not in body

    def test_creating_a_user_needs_owner_and_a_second_factor(self, system):
        c = system["client"]
        body = {"username": "newbie", "password": "a-brand-new-long-password",
                "role": "viewer"}
        # No second factor at all.
        assert c.post("/api/users/create", json=body,
                      headers=auth(c, "owner1", OWNER_PW)).status_code == 403
        # Right factor, wrong role.
        assert write(system, system["hand"], "/api/users/create", body).status_code == 403
        r = write(system, system["owner"], "/api/users/create", body)
        assert r.status_code == 200, r.text
        assert r.json()["totp_uri"].startswith("otpauth://")

    def test_a_weak_password_is_refused_with_a_reason(self, system):
        r = write(system, system["owner"], "/api/users/create",
                  {"username": "weakling", "password": "passwordpassword",
                   "role": "viewer"})
        assert r.status_code == 400
        assert "گذرواژه" in r.text

    def test_the_last_owner_cannot_be_demoted_over_http(self, system):
        r = write(system, system["owner"], "/api/users/role",
                  {"username": "owner1", "role": "viewer"})
        assert r.status_code == 409
        assert "تنها مدیر" in r.text
        # And the account really is untouched.
        assert system["security"].get_user("owner1").role == "owner"

    def test_an_owner_cannot_delete_themselves(self, system):
        r = write(system, system["owner"], "/api/users/delete", {"username": "owner1"})
        assert r.status_code == 409

    def test_a_rotated_factor_invalidates_the_old_one(self, system):
        before = system["security"].get_user("hand1").totp_secret
        r = write(system, system["owner"], "/api/users/totp", {"username": "hand1"})
        assert r.status_code == 200
        after = system["security"].get_user("hand1").totp_secret
        assert after != before
        assert after not in r.text or r.json()["totp_uri"].startswith("otpauth://")


# --------------------------------------------------------------------------- #
# venues
# --------------------------------------------------------------------------- #


class TestVenueEndpoints:
    def test_the_overview_is_readable_but_carries_no_key_path_for_a_viewer(self, system):
        c = system["client"]
        as_owner = c.get("/api/brokers", headers=auth(c, "owner1", OWNER_PW)).json()
        as_viewer = c.get("/api/brokers", headers=auth(c, "eyes1", EYES_PW)).json()
        assert as_viewer["active_profile"] == "paper"
        # The credential key's location is filesystem reconnaissance.
        assert as_viewer["credential_storage"]["key_path"] is None
        assert "level" in as_viewer["credential_storage"]
        assert as_owner["credential_storage"].get("key_path") is not None \
            or as_owner["credential_storage"]["key_source"] == "environment"

    def test_discovery_and_testing_are_owner_only(self, system):
        """Both reach a MetaTrader terminal: discovery reads the signed-in
        account out of it, and testing signs one in."""
        assert write(system, system["hand"], "/api/brokers/discover", {}).status_code == 403
        assert write(system, system["owner"], "/api/brokers/discover", {}).status_code == 200

    def test_a_connection_round_trips_and_hides_its_credential(self, system):
        payload = {"id": "sim-1", "display_name": "شبیه‌ساز", "profile": "paper",
                   "declared_account_type": "demo", "secret": "a-broker-password"}
        r = write(system, system["owner"], "/api/brokers/save", payload)
        assert r.status_code == 200, r.text
        assert "a-broker-password" not in r.text
        assert "secret_ref" not in r.text
        assert r.json()["has_credential"] is True

        c = system["client"]
        listing = c.get("/api/brokers", headers=auth(c, "eyes1", EYES_PW)).text
        assert "a-broker-password" not in listing

        # And it really is sealed on disk.
        raw = (system["tmp"] / "broker-secrets.json").read_text(encoding="utf-8")
        assert "a-broker-password" not in raw

    def test_a_hostile_terminal_path_is_refused_at_the_boundary(self, system):
        for bad in ("relative/path.exe", "C:\\MT5; rm -rf /", "/opt/mt5/"):
            r = write(system, system["owner"], "/api/brokers/save",
                      {"id": "bad-1", "display_name": "X", "profile": "generic_mt5",
                       "terminal_path": bad})
            assert r.status_code == 422, f"{bad} was accepted"

    def test_testing_then_activating_a_connection_works_end_to_end(self, system):
        write(system, system["owner"], "/api/brokers/save",
              {"id": "sim-1", "display_name": "S", "profile": "paper",
               "declared_account_type": "demo"})

        # Activation before any test is refused, in words.
        blocked = write(system, system["owner"], "/api/brokers/activate", {"id": "sim-1"})
        assert blocked.status_code == 409
        assert "آزمایش" in blocked.text

        tested = write(system, system["owner"], "/api/brokers/test", {"id": "sim-1"})
        assert tested.status_code == 200, tested.text
        report = tested.json()
        assert report["ok"] is True, report["blocking_failures"]

        activated = write(system, system["owner"], "/api/brokers/activate", {"id": "sim-1"})
        assert activated.status_code == 200, activated.text
        assert activated.json()["restart_required"] is True

        c = system["client"]
        after = c.get("/api/brokers", headers=auth(c, "owner1", OWNER_PW)).json()
        enabled = [x for x in after["connections"] if x["enabled"]]
        assert [x["id"] for x in enabled] == ["sim-1"]

    def test_editing_a_connections_identity_withdraws_its_activation(self, system):
        """Losing the evidence must lose the authority with it -- otherwise an
        already-enabled connection can be re-pointed at a different account and
        used at the next restart with no fresh test."""
        write(system, system["owner"], "/api/brokers/save",
              {"id": "sim-1", "display_name": "S", "profile": "paper",
               "declared_account_type": "demo"})
        write(system, system["owner"], "/api/brokers/test", {"id": "sim-1"})
        write(system, system["owner"], "/api/brokers/activate", {"id": "sim-1"})

        moved = write(system, system["owner"], "/api/brokers/save",
                      {"id": "sim-1", "display_name": "S", "profile": "paper",
                       "declared_account_type": "demo", "login": "998877"})
        assert moved.status_code == 200
        assert moved.json()["enabled"] is False
        assert moved.json()["last_probe"] is None

    def test_the_active_connection_cannot_be_deleted(self, system):
        write(system, system["owner"], "/api/brokers/save",
              {"id": "sim-1", "display_name": "S", "profile": "paper",
               "declared_account_type": "demo"})
        write(system, system["owner"], "/api/brokers/test", {"id": "sim-1"})
        write(system, system["owner"], "/api/brokers/activate", {"id": "sim-1"})
        r = write(system, system["owner"], "/api/brokers/delete", {"id": "sim-1"})
        assert r.status_code == 409

    def test_a_venue_write_is_journalled(self, system):
        write(system, system["owner"], "/api/brokers/save",
              {"id": "sim-1", "display_name": "S", "profile": "paper",
               "declared_account_type": "demo"})
        records = system["runtime"].agent.audit.read()
        actions = [(r.get("payload") or {}).get("action") for r in records]
        assert "broker_connection_saved" in actions
        # And the credential itself is not in the chain.
        assert "a-broker-password" not in json.dumps(records, ensure_ascii=False,
                                                     default=str)


# --------------------------------------------------------------------------- #
# licence
# --------------------------------------------------------------------------- #


class TestLicenceEndpoints:
    def test_the_fingerprint_is_owner_only(self, system):
        c = system["client"]
        assert c.get("/api/licence/fingerprint",
                     headers=auth(c, "eyes1", EYES_PW)).status_code == 403
        r = c.get("/api/licence/fingerprint", headers=auth(c, "owner1", OWNER_PW))
        assert r.status_code == 200
        assert r.json()["fingerprint"]

    def test_installing_a_licence_into_a_build_without_a_gate_is_refused(self, system):
        r = write(system, system["owner"], "/api/licence/install",
                  {"document": "-----BEGIN SENTINEL LICENCE-----\n"
                               + "A" * 100 + "\n-----END SENTINEL LICENCE-----"})
        assert r.status_code == 409

    def test_the_licence_view_is_readable_by_any_role(self, system):
        c = system["client"]
        for name, pw in (("owner1", OWNER_PW), ("hand1", HAND_PW), ("eyes1", EYES_PW)):
            r = c.get("/api/licence", headers=auth(c, name, pw))
            assert r.status_code == 200, name
            # An operator who cannot see WHY the system refuses to trade cannot
            # do their job, and nothing here is secret.
            assert "enforced" in r.json()


class TestNoWriteWithoutASecondFactor:
    @pytest.mark.parametrize("path,body", [
        ("/api/users/create", {"username": "x1", "password": "long-enough-pw-here",
                               "role": "viewer"}),
        ("/api/users/role", {"username": "hand1", "role": "viewer"}),
        ("/api/users/disable", {"username": "hand1", "disabled": True}),
        ("/api/users/password", {"username": "hand1", "password": "long-enough-pw-x"}),
        ("/api/users/totp", {"username": "hand1"}),
        ("/api/users/delete", {"username": "hand1"}),
        ("/api/brokers/save", {"id": "ab", "display_name": "X", "profile": "paper"}),
        ("/api/brokers/test", {"id": "ab"}),
        ("/api/brokers/activate", {"id": "ab"}),
        ("/api/brokers/delete", {"id": "ab"}),
        ("/api/brokers/discover", {}),
        ("/api/licence/install", {"document": "x" * 100}),
    ])
    def test_every_new_write_demands_a_fresh_code(self, system, path, body):
        c = system["client"]
        r = c.post(path, json=body, headers=auth(c, "owner1", OWNER_PW))
        assert r.status_code == 403, f"{path} accepted a session with no TOTP"


class TestFirstBootMessages:
    """A bad environment variable at first boot must produce an instruction,
    not a traceback from three frames down."""

    def test_a_rejected_admin_password_names_the_variable(self, tmp_path,
                                                          monkeypatch):
        from sentinel.bootstrap import build_runtime

        config = {
            "agent": {"mode": "advisory"},
            "execution": {"broker": "paper", "venue_mode": "paper"},
            "ops": {"state_dir": str(tmp_path),
                    "audit_log": str(tmp_path / "audit.jsonl"),
                    "killswitch_file": str(tmp_path / "KILL")},
            "security": {"bind_host": "127.0.0.1"},
        }
        (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setenv("SENTINEL_JWT_SECRET", "s" * 48)
        monkeypatch.setenv("SENTINEL_ADMIN_USER", "owner")
        # Contains the username, which the policy refuses.
        monkeypatch.setenv("SENTINEL_ADMIN_PASSWORD", "a-really-long-owner-password")

        with pytest.raises(RuntimeError) as exc:
            build_runtime(tmp_path / "config.json")
        text = str(exc.value)
        assert "SENTINEL_ADMIN_PASSWORD" in text
        assert "12 characters" in text
        assert "نام کاربری" in text or "username" in text

    def test_a_good_environment_boots_and_writes_the_enrolment_file_0600(
            self, tmp_path, monkeypatch):
        from sentinel.bootstrap import build_runtime

        config = {
            "agent": {"mode": "advisory"},
            "execution": {"broker": "paper", "venue_mode": "paper"},
            "ops": {"state_dir": str(tmp_path),
                    "audit_log": str(tmp_path / "audit.jsonl"),
                    "killswitch_file": str(tmp_path / "KILL")},
            "security": {"bind_host": "127.0.0.1"},
        }
        (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setenv("SENTINEL_JWT_SECRET", "s" * 48)
        monkeypatch.setenv("SENTINEL_ADMIN_USER", "chief")
        monkeypatch.setenv("SENTINEL_ADMIN_PASSWORD", "a-long-unrelated-passphrase")

        runtime, security = build_runtime(tmp_path / "config.json")
        assert security.get_user("chief").role == "owner"

        enrolments = list(Path(tmp_path).rglob("enrolment-chief.txt"))
        assert enrolments, "no enrolment file was written"
        mode = oct(enrolments[0].stat().st_mode & 0o777)
        assert mode == "0o600", f"the second factor was written {mode}"
