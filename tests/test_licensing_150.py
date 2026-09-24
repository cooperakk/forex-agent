"""Licensing hardening in 1.5.0.

Four holes, each of which let a licensee trade live without a valid licence:

1. the vendor key came only from an environment variable the licensee
   controls -- unset it and licensing switched off, replace it and a
   self-signed "unlimited" licence verified;
2. deleting MANIFEST.sig turned the integrity check into "unsigned, fine";
3. ``activation_url`` was carried in every licence and implemented nowhere;
4. malformed input raised ValueError/TypeError before the signature check,
   escaping every ``except LicenseError``.
"""

from __future__ import annotations

import base64
import json
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sentinel.licensing import enforcement, vendor_key
from sentinel.licensing.activation import (
    ActivationClient, LeaseError, device_digest, sign_lease, verify_lease,
)
from sentinel.licensing.enforcement import LicenseGate, resolve_vendor_key
from sentinel.licensing.license import (
    LicenseInvalid, _canonical, generate_keypair, issue, parse,
)

ROOT = Path(__file__).resolve().parents[1]
MACHINE = {"machine_id": "a" * 16, "mac": "b" * 16, "cpu": "c" * 16,
           "rootfs": "d" * 16, "hostname": "e" * 16}
NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def keys():
    return generate_keypair()


@pytest.fixture(scope="module")
def lease_keys():
    return generate_keypair()


def _licence(keys, **kw):
    pem, _ = keys
    kw.setdefault("machine", MACHINE)
    return issue(private_key_pem=pem, issued_to="Acme", tier="live_single",
                 term_months=3, now=NOW, **kw)


def _gate(tmp_path, keys, doc, **kw):
    path = tmp_path / "licence.key"
    path.write_text(doc, encoding="utf-8")
    kw.setdefault("enable_clock_guard", False)
    return LicenseGate(licence_path=str(path), public_key=keys[1], fingerprint=MACHINE,
                       root=str(tmp_path), **kw)


# --------------------------------------------------------------------------- #
# 1. the key
# --------------------------------------------------------------------------- #


class TestVendorKeyResolution:
    def test_an_embedded_key_beats_the_environment(self, monkeypatch, keys):
        monkeypatch.setattr(vendor_key, "EMBEDDED_PUBLIC_KEY", keys[1])
        monkeypatch.setenv("SENTINEL_LICENSE_PUBKEY", "attacker-chosen-key")
        assert resolve_vendor_key() == keys[1]
        assert enforcement.is_distributed_build()

    def test_unsetting_the_environment_does_not_switch_licensing_off(
            self, monkeypatch, tmp_path, keys):
        monkeypatch.setattr(vendor_key, "EMBEDDED_PUBLIC_KEY", keys[1])
        monkeypatch.delenv("SENTINEL_LICENSE_PUBKEY", raising=False)
        gate = LicenseGate(licence_path=str(tmp_path / "none.key"),
                           root=str(tmp_path), enable_clock_guard=False)
        status = gate.check()
        assert status.unlicensed_mode is False and status.valid is False
        assert gate.may_trade_live()[0] is False

    def test_a_self_signed_licence_does_not_verify_in_a_distributed_build(
            self, monkeypatch, tmp_path, keys):
        monkeypatch.setattr(vendor_key, "EMBEDDED_PUBLIC_KEY", keys[1])
        pirate = generate_keypair()
        monkeypatch.setenv("SENTINEL_LICENSE_PUBKEY", pirate[1])
        doc = issue(private_key_pem=pirate[0], issued_to="Pirate", tier="unlimited",
                    perpetual=True, allow_unbound=True)
        (tmp_path / "licence.key").write_text(doc)
        gate = LicenseGate(licence_path=str(tmp_path / "licence.key"),
                           root=str(tmp_path), enable_clock_guard=False)
        assert gate.check().valid is False

    def test_self_hosted_builds_still_read_the_environment(self, monkeypatch, keys):
        monkeypatch.setattr(vendor_key, "EMBEDDED_PUBLIC_KEY", "")
        monkeypatch.setenv("SENTINEL_LICENSE_PUBKEY", keys[1])
        assert resolve_vendor_key() == keys[1]
        assert not enforcement.is_distributed_build()

    def test_embed_key_rewrites_the_module(self, tmp_path, keys, lease_keys):
        target = tmp_path / "vendor_key.py"
        target.write_text((ROOT / "sentinel/licensing/vendor_key.py").read_text())
        subprocess.run([sys.executable, str(ROOT / "scripts/licensegen.py"), "embed-key",
                        "--pubkey", keys[1], "--lease-pubkey", lease_keys[1],
                        "--target", str(target)], check=True, capture_output=True)
        text = target.read_text()
        assert f'EMBEDDED_PUBLIC_KEY = "{keys[1]}"' in text
        assert f'EMBEDDED_LEASE_PUBLIC_KEY = "{lease_keys[1]}"' in text

    def test_embed_key_refuses_a_malformed_key(self, tmp_path):
        target = tmp_path / "vendor_key.py"
        target.write_text((ROOT / "sentinel/licensing/vendor_key.py").read_text())
        r = subprocess.run([sys.executable, str(ROOT / "scripts/licensegen.py"),
                            "embed-key", "--pubkey", "x" * 44, "--target", str(target)],
                           capture_output=True, text=True)
        assert r.returncode != 0
        assert 'EMBEDDED_PUBLIC_KEY = ""' in target.read_text()


# --------------------------------------------------------------------------- #
# 2. the manifest
# --------------------------------------------------------------------------- #


class TestManifestRequired:
    def test_a_missing_manifest_refuses_live_in_a_distributed_build(self, tmp_path, keys):
        gate = _gate(tmp_path, keys, _licence(keys), require_manifest=True)
        allowed, why = gate.may_trade_live()
        assert allowed is False and "MANIFEST.sig" in why

    def test_a_source_checkout_may_still_run_unsigned(self, tmp_path, keys):
        gate = _gate(tmp_path, keys, _licence(keys), require_manifest=False)
        assert gate.may_trade_live() == (True, "")


# --------------------------------------------------------------------------- #
# 4. hostile input
# --------------------------------------------------------------------------- #


def _envelope(document: dict) -> str:
    body = base64.b64encode(json.dumps(document).encode()).decode()
    return f"-----BEGIN SENTINEL LICENCE-----\n{body}\n-----END SENTINEL LICENCE-----\n"


class TestHostileLicenceInput:
    @pytest.mark.parametrize("payload", [
        {"format": "abc"},
        {"format": 1},                                  # missing required fields
        {"format": 1, "licence_id": 5, "issued_to": "x", "issued_at": "2026-01-01",
         "expires_at": None, "tier": "research"},
        {"format": 1, "licence_id": "x", "issued_to": "x", "issued_at": "not-a-date",
         "expires_at": None, "tier": "research"},
        {"format": 1, "licence_id": "x", "issued_to": "x", "issued_at": "2026-01-01",
         "expires_at": None, "tier": "research", "machine": "not-a-map"},
    ])
    def test_malformed_payloads_raise_licence_invalid(self, payload):
        doc = _envelope({"algorithm": "Ed25519", "payload": payload,
                         "signature": base64.b64encode(b"x" * 64).decode()})
        with pytest.raises(LicenseInvalid):
            parse(doc)

    def test_a_body_that_is_not_an_object(self):
        with pytest.raises(LicenseInvalid):
            parse(_envelope(["not", "an", "object"]))  # type: ignore[arg-type]

    def test_ids_are_unique_within_one_second(self, keys):
        a = parse(_licence(keys))[0].licence_id
        b = parse(_licence(keys))[0].licence_id
        assert a != b


# --------------------------------------------------------------------------- #
# 3. online activation
# --------------------------------------------------------------------------- #


class FakeServer:
    """Signs leases like scripts/license_server.py, and can misbehave."""

    def __init__(self, lease_keys, *, status="active", lease_hours=72.0):
        self.pem = lease_keys[0]
        self.status = status
        self.lease_hours = lease_hours
        self.down = False
        self.wrong_nonce = False
        self.calls = 0
        self.now = NOW

    def __call__(self, url, body, timeout):
        self.calls += 1
        if self.down:
            raise ConnectionError("activation server unreachable")
        payload = json.loads(base64.b64decode(
            body["licence"].split("-----")[2].strip()))["payload"]
        return sign_lease(self.pem, licence_id=payload["licence_id"], device=body["device"],
                          nonce="forged" if self.wrong_nonce else body["nonce"],
                          status=self.status, lease_hours=self.lease_hours, now=self.now)


def _activated_gate(tmp_path, keys, lease_keys, server, **kw):
    doc = _licence(keys, activation_url="https://licence.example/v1/lease",
                   activation_interval_hours=24)
    return _gate(tmp_path, keys, doc, activation_transport=server,
                 lease_public_key=lease_keys[1], **kw)


class TestActivation:
    def test_a_licence_without_activation_needs_no_lease(self, tmp_path, keys):
        gate = _gate(tmp_path, keys, _licence(keys))
        status = gate.check(now=NOW)
        assert status.activation is not None and status.activation.required is False
        assert gate.may_trade_live()[0] is True

    def test_a_fresh_lease_permits_live_and_is_cached_privately(
            self, tmp_path, keys, lease_keys):
        server = FakeServer(lease_keys)
        gate = _activated_gate(tmp_path, keys, lease_keys, server)
        status = gate.check(now=NOW)
        assert status.valid and status.activation.ok and status.activation.refreshed
        lease_file = tmp_path / "licence-lease.json"
        assert lease_file.exists()
        if sys.platform != "win32":
            assert stat.S_IMODE(lease_file.stat().st_mode) == 0o600

    def test_no_lease_and_no_server_blocks_live_but_not_the_licence(
            self, tmp_path, keys, lease_keys, monkeypatch):
        server = FakeServer(lease_keys)
        server.down = True
        gate = _activated_gate(tmp_path, keys, lease_keys, server)
        status = gate.check(now=NOW)
        assert status.valid is True, "paper and research keep working"
        assert status.activation.ok is False
        monkeypatch.setattr(gate, "check", lambda **kw: status)
        allowed, why = gate.may_trade_live()
        assert allowed is False and "فعال‌سازی" in why

    def test_a_cached_lease_survives_a_server_outage(self, tmp_path, keys, lease_keys):
        server = FakeServer(lease_keys)
        gate = _activated_gate(tmp_path, keys, lease_keys, server)
        assert gate.check(now=NOW).activation.ok
        server.down = True
        later = NOW + timedelta(hours=30)          # past the 24h interval, inside 72h
        status = gate.check(now=later, force=True)
        assert status.activation.ok, status.activation.reason
        assert "unreachable" in status.activation.network_error

    def test_an_expired_lease_with_no_server_blocks_live(self, tmp_path, keys, lease_keys):
        server = FakeServer(lease_keys)
        gate = _activated_gate(tmp_path, keys, lease_keys, server)
        gate.check(now=NOW)
        server.down = True
        status = gate.check(now=NOW + timedelta(hours=80), force=True)
        assert status.activation.ok is False

    def test_a_revoked_licence_is_invalid(self, tmp_path, keys, lease_keys):
        server = FakeServer(lease_keys, status="revoked")
        gate = _activated_gate(tmp_path, keys, lease_keys, server)
        status = gate.check(now=NOW)
        assert status.valid is False and status.stage == "revoked"

    def test_a_replayed_answer_is_refused(self, tmp_path, keys, lease_keys):
        server = FakeServer(lease_keys)
        server.wrong_nonce = True
        gate = _activated_gate(tmp_path, keys, lease_keys, server)
        status = gate.check(now=NOW)
        assert status.activation.ok is False
        assert "nonce" in status.activation.network_error

    def test_a_lease_from_another_key_is_refused(self, tmp_path, keys, lease_keys):
        server = FakeServer(generate_keypair())          # not the vendor's lease key
        gate = _activated_gate(tmp_path, keys, lease_keys, server)
        assert gate.check(now=NOW).activation.ok is False

    def test_a_lease_copied_from_another_machine_is_refused(self, keys, lease_keys):
        doc = sign_lease(lease_keys[0], licence_id="L1", device=device_digest(MACHINE),
                         nonce="n", now=NOW)
        other = dict(MACHINE, hostname="z" * 16)
        with pytest.raises(LeaseError, match="different machine"):
            verify_lease(doc, lease_keys[1], licence_id="L1",
                         device=device_digest(other), now=NOW)

    def test_an_edited_lease_is_refused(self, lease_keys):
        doc = sign_lease(lease_keys[0], licence_id="L1", device="d" * 64, nonce="n",
                         now=NOW, lease_hours=1)
        doc["payload"]["expires_at"] = "2099-01-01T00:00:00Z"
        with pytest.raises(LeaseError, match="does not verify"):
            verify_lease(doc, lease_keys[1], licence_id="L1", device="d" * 64, now=NOW)

    def test_a_clock_wound_back_behind_the_lease_is_refused(
            self, tmp_path, keys, lease_keys):
        server = FakeServer(lease_keys)
        gate = _activated_gate(tmp_path, keys, lease_keys, server)
        assert gate.check(now=NOW).activation.ok
        server.down = True
        # Twelve hours back: inside the licence's own clock slack, so only the
        # lease can notice -- it is dated in this machine's future.
        status = gate.check(now=NOW - timedelta(hours=12), force=True)
        assert status.activation.ok is False

    def test_the_server_is_not_hammered_while_unreachable(self, tmp_path, keys, lease_keys):
        server = FakeServer(lease_keys)
        client = ActivationClient(tmp_path / "lease.json", public_key_b64=lease_keys[1],
                                  transport=server)
        from sentinel.licensing.license import parse as _parse
        doc = _licence(keys, activation_url="https://x/v1/lease",
                       activation_interval_hours=1)
        licence = _parse(doc)[0]
        client.status(licence, doc, MACHINE, now=NOW)
        server.down = True
        for minute in range(61, 64):
            client.status(licence, doc, MACHINE, now=NOW + timedelta(minutes=minute))
        assert server.calls == 2, "one retry per five minutes, not one per check"


# --------------------------------------------------------------------------- #
# the vendor's server
# --------------------------------------------------------------------------- #


class TestLicenceServer:
    @pytest.fixture
    def server(self, tmp_path, keys, lease_keys):
        sys.path.insert(0, str(ROOT / "scripts"))
        import license_server
        registry = license_server.Registry(tmp_path / "reg.db")
        return license_server, registry

    def _ask(self, mod, registry, keys, lease_keys, doc, device, *, auto=True):
        return mod.decide(registry, licence_doc=doc, device=device, nonce="nonce-1",
                          vendor_pubkey=keys[1], lease_key_pem=lease_keys[0],
                          lease_hours=72, auto_register=auto, now=NOW)

    def test_seats_are_counted_per_machine(self, server, keys, lease_keys):
        mod, registry = server
        doc = _licence(keys)                          # live_single: max_accounts=1
        first = self._ask(mod, registry, keys, lease_keys, doc, "a" * 64)
        again = self._ask(mod, registry, keys, lease_keys, doc, "a" * 64)
        second = self._ask(mod, registry, keys, lease_keys, doc, "b" * 64)
        assert first["payload"]["status"] == "active"
        assert again["payload"]["status"] == "active"
        assert second["payload"]["status"] == "suspended"

    def test_revocation_reaches_the_client(self, server, keys, lease_keys):
        mod, registry = server
        doc = _licence(keys)
        lid = parse(doc)[0].licence_id
        self._ask(mod, registry, keys, lease_keys, doc, "a" * 64)
        registry.set_status(lid, "revoked", "chargeback")
        answer = self._ask(mod, registry, keys, lease_keys, doc, "a" * 64)
        lease = verify_lease(answer, lease_keys[1], licence_id=lid, device="a" * 64,
                             nonce="nonce-1", now=NOW)
        assert lease.status == "revoked" and lease.message == "chargeback"

    def test_an_unregistered_licence_is_suspended_without_auto_register(
            self, server, keys, lease_keys):
        mod, registry = server
        answer = self._ask(mod, registry, keys, lease_keys, _licence(keys), "a" * 64,
                           auto=False)
        assert answer["payload"]["status"] == "suspended"

    def test_a_forged_licence_gets_no_lease(self, server, keys, lease_keys):
        mod, registry = server
        forged = issue(private_key_pem=generate_keypair()[0], issued_to="x",
                       tier="unlimited", perpetual=True, allow_unbound=True)
        with pytest.raises(ValueError, match="licence rejected"):
            self._ask(mod, registry, keys, lease_keys, forged, "a" * 64)

    def test_the_http_endpoint(self, server, keys, lease_keys):
        from fastapi.testclient import TestClient
        mod, registry = server
        app = mod.create_app(registry, vendor_pubkey=keys[1], lease_key_pem=lease_keys[0],
                             auto_register=True)
        client = TestClient(app)
        doc = _licence(keys)
        r = client.post("/v1/lease", json={"licence": doc, "device": "c" * 64,
                                           "nonce": "abcdefgh"})
        assert r.status_code == 200 and r.json()["payload"]["status"] == "active"
        bad = client.post("/v1/lease", json={"licence": "x" * 100, "device": "c" * 64,
                                             "nonce": "abcdefgh"})
        assert bad.status_code == 400


def test_canonical_form_is_stable():
    """The lease and the licence share one canonical serialisation."""
    assert _canonical({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode("utf-8")


class TestProtectedBuild:
    """scripts/build_protected.py, without the (slow, compiler-dependent)
    --compile step, which is exercised by the release pipeline."""

    def test_a_release_embeds_the_key_signs_the_manifest_and_ships_no_vendor_tools(
            self, tmp_path, keys):
        priv = tmp_path / "private.pem"
        priv.write_text(keys[0])
        out = tmp_path / "release"
        subprocess.run([sys.executable, str(ROOT / "scripts/build_protected.py"),
                        "--version", "9.9.9", "--key", str(priv), "--pubkey", keys[1],
                        "--out", str(out)], check=True, capture_output=True)
        tree = out / "sentinel-fx-9.9.9"
        assert (out / "sentinel-fx-9.9.9.tar.gz").is_file()
        assert not (tree / "scripts/license_server.py").exists()
        assert not (tree / "scripts/build_protected.py").exists()
        assert (tree / "scripts/licensegen.py").exists(), "customers need `fingerprint`"
        assert keys[1] in (tree / "sentinel/licensing/vendor_key.py").read_text()
        from sentinel.licensing.integrity import verify_manifest
        report = verify_manifest(tree, (tree / "MANIFEST.sig").read_text(), keys[1])
        assert report.ok, report.summary()
