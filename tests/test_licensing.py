"""Licensing behaviour.

The tests that matter are the negative ones: a licensing system is only worth
anything if forging, editing, moving and expiring a licence all fail, and if
none of those failures makes the trading system DANGEROUS.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from sentinel.licensing.enforcement import LicenseGate
from sentinel.licensing.fingerprint import fingerprint_matches, machine_fingerprint
from sentinel.licensing.integrity import build_manifest, verify_manifest
from sentinel.licensing.license import (
    LicenseExpired,
    LicenseInvalid,
    LicenseMachineMismatch,
    generate_keypair,
    issue,
    parse,
    verify,
)

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def keys():
    return generate_keypair()


@pytest.fixture
def machine():
    return {"machine_id": "a" * 16, "mac": "b" * 16, "cpu": "c" * 16,
            "root_fs": "d" * 16, "hostname": "e" * 16}


def _issue(keys, machine, **kwargs):
    private, _ = keys
    params = dict(private_key_pem=private, issued_to="Acme Capital",
                  tier="live_single", valid_days=365, machine=machine, now=NOW)
    params.update(kwargs)
    return issue(**params)


class TestSignature:
    def test_a_valid_licence_verifies(self, keys, machine):
        _, public = keys
        licence = verify(_issue(keys, machine), public, now=NOW,
                         current_fingerprint=machine)
        assert licence.issued_to == "Acme Capital"
        assert licence.tier == "live_single"
        assert licence.capability("live_trading") is True

    def test_an_edited_payload_is_rejected(self, keys, machine):
        """The whole point. Someone who extends their own expiry, upgrades
        their own tier, or changes the machine binding must fail."""
        import base64

        private, public = keys
        document = _issue(keys, machine, valid_days=30)
        body = "".join(document.split("-----")[2].split())
        parsed = json.loads(base64.b64decode(body))
        parsed["payload"]["tier"] = "unlimited"
        parsed["payload"]["expires_at"] = "2099-01-01T00:00:00Z"
        forged = base64.b64encode(
            json.dumps(parsed, sort_keys=True).encode()).decode()
        doc = (f"-----BEGIN SENTINEL LICENCE-----\n{forged}\n"
               "-----END SENTINEL LICENCE-----\n")

        with pytest.raises(LicenseInvalid, match="signature does not verify"):
            verify(doc, public, now=NOW, current_fingerprint=machine)

    def test_a_licence_from_a_different_issuer_is_rejected(self, machine):
        other_private, _ = generate_keypair()
        _, our_public = generate_keypair()
        document = issue(private_key_pem=other_private, issued_to="Impostor",
                         tier="unlimited", machine=machine, now=NOW)
        with pytest.raises(LicenseInvalid):
            verify(document, our_public, now=NOW, current_fingerprint=machine)

    def test_a_truncated_file_is_rejected_with_a_useful_message(self, keys, machine):
        _, public = keys
        document = _issue(keys, machine)
        with pytest.raises(LicenseInvalid, match="BEGIN/END markers"):
            verify(document.split("\n")[3], public, now=NOW)


class TestValidityWindow:
    def test_an_expired_licence_is_rejected(self, keys, machine):
        _, public = keys
        document = _issue(keys, machine, valid_days=30)
        later = NOW + timedelta(days=45)
        with pytest.raises(LicenseExpired):
            verify(document, public, now=later, current_fingerprint=machine)

    def test_the_grace_period_keeps_a_recent_expiry_working(self, keys, machine):
        """A renewal that arrives a day late must not stop a live book."""
        _, public = keys
        document = _issue(keys, machine, valid_days=30)
        later = NOW + timedelta(days=35)
        licence = verify(document, public, now=later, grace_days=14,
                         current_fingerprint=machine)
        assert licence.days_remaining(now=later) < 0

    def test_a_perpetual_licence_never_expires(self, keys, machine):
        _, public = keys
        document = _issue(keys, machine, valid_days=None)
        far = NOW + timedelta(days=4000)
        licence = verify(document, public, now=far, current_fingerprint=machine)
        assert licence.days_remaining(now=far) is None

    def test_a_licence_dated_in_the_future_is_refused(self, keys, machine):
        _, public = keys
        document = _issue(keys, machine)
        with pytest.raises(LicenseInvalid, match="clock"):
            verify(document, public, now=NOW - timedelta(days=5),
                   current_fingerprint=machine)


class TestMachineBinding:
    def test_a_licence_does_not_work_on_another_machine(self, keys, machine):
        _, public = keys
        document = _issue(keys, machine)
        elsewhere = {k: "z" * 16 for k in machine}
        with pytest.raises(LicenseMachineMismatch):
            verify(document, public, now=NOW, current_fingerprint=elsewhere)

    def test_replacing_one_component_does_not_break_the_licence(self, keys, machine):
        """A fingerprint that breaks when a network card is replaced turns a
        paying customer into a support ticket, and the natural fix -- ignore
        mismatches -- removes the control entirely."""
        _, public = keys
        document = _issue(keys, machine)
        swapped = dict(machine)
        swapped["mac"] = "f" * 16
        licence = verify(document, public, now=NOW, current_fingerprint=swapped)
        assert licence.licence_id

    def test_changing_most_components_does_break_it(self, keys, machine):
        _, public = keys
        document = _issue(keys, machine)
        moved = dict(machine)
        for key in ("mac", "machine_id", "root_fs"):
            moved[key] = "f" * 16
        with pytest.raises(LicenseMachineMismatch):
            verify(document, public, now=NOW, current_fingerprint=moved)

    def test_issuing_an_unbound_licence_requires_saying_so(self, keys):
        private, _ = keys
        with pytest.raises(LicenseInvalid, match="unbound"):
            issue(private_key_pem=private, issued_to="X", tier="research",
                  machine={}, now=NOW)
        document = issue(private_key_pem=private, issued_to="X", tier="research",
                         machine={}, allow_unbound=True, now=NOW)
        assert "BEGIN SENTINEL LICENCE" in document

    def test_this_machines_fingerprint_matches_itself(self):
        current = machine_fingerprint()
        assert current, "no machine identifiers could be collected at all"
        ok, matched, _ = fingerprint_matches(current, current)
        assert ok and matched == len(current)


class TestTiers:
    def test_an_evaluation_licence_cannot_trade_live(self, keys, machine, tmp_path):
        _, public = keys
        path = tmp_path / "licence.key"
        path.write_text(_issue(keys, machine, tier="evaluation"), encoding="utf-8")

        gate = LicenseGate(licence_path=str(path), public_key=public,
                           manifest_path=str(tmp_path / "absent.sig"),
                           fingerprint=machine)
        gate.check(now=NOW, force=True)

        allowed, reason = gate.may_trade_live()
        assert not allowed
        # The operator-facing reasons are Persian: they are rendered
        # verbatim in the dashboard.
        assert "معاملهٔ واقعی را شامل نمی‌شود" in reason

    def test_a_per_licence_capability_overrides_the_tier(self, keys, machine):
        _, public = keys
        document = _issue(keys, machine, tier="live_single",
                          capabilities={"max_instruments": 20})
        licence = verify(document, public, now=NOW, current_fingerprint=machine)
        assert licence.capability("max_instruments") == 20
        assert licence.capability("live_trading") is True


class TestIntegrityManifest:
    def test_a_modified_protected_file_is_detected(self, keys, tmp_path):
        private, public = keys
        root = tmp_path / "install"
        (root / "sentinel").mkdir(parents=True)
        target = root / "sentinel" / "thing.py"
        target.write_text("original\n", encoding="utf-8")

        manifest = build_manifest(root, private, paths=["sentinel/thing.py"],
                                  version="1.0.0")
        assert verify_manifest(root, manifest, public).ok

        target.write_text("original\n# patched out the licence check\n",
                          encoding="utf-8")
        report = verify_manifest(root, manifest, public)
        assert not report.ok
        assert "sentinel/thing.py" in report.modified

    def test_a_deleted_protected_file_is_detected(self, keys, tmp_path):
        private, public = keys
        root = tmp_path / "install"
        (root / "sentinel").mkdir(parents=True)
        target = root / "sentinel" / "thing.py"
        target.write_text("original\n", encoding="utf-8")
        manifest = build_manifest(root, private, paths=["sentinel/thing.py"])
        target.unlink()
        report = verify_manifest(root, manifest, public)
        assert not report.ok and "sentinel/thing.py" in report.missing

    def test_a_forged_manifest_is_rejected(self, keys, tmp_path):
        _, public = keys
        other_private, _ = generate_keypair()
        root = tmp_path / "install"
        (root / "sentinel").mkdir(parents=True)
        (root / "sentinel" / "thing.py").write_text("x\n", encoding="utf-8")
        manifest = build_manifest(root, other_private, paths=["sentinel/thing.py"])
        report = verify_manifest(root, manifest, public)
        assert not report.ok and "does not verify" in (report.error or "")


class TestGateBehaviour:
    """A licence problem must reduce what the system DOES, never make it
    dangerous."""

    def test_a_missing_licence_blocks_live_trading_but_not_the_system(self, tmp_path):
        _, public = generate_keypair()
        gate = LicenseGate(licence_path=str(tmp_path / "nothing.key"),
                           public_key=public,
                           manifest_path=str(tmp_path / "absent.sig"))
        status = gate.check()
        assert not status.valid
        assert "no licence file" in status.reason
        allowed, reason = gate.may_trade_live()
        assert not allowed and "لایسنس معتبر" in reason

    def test_a_build_with_no_vendor_key_does_not_enforce_anything(self, tmp_path):
        """Self-hosted builds where the customer IS the vendor. It says so
        rather than pretending to enforce something."""
        gate = LicenseGate(licence_path=str(tmp_path / "nothing.key"),
                           public_key="", manifest_path=str(tmp_path / "absent.sig"))
        status = gate.check()
        assert status.valid and status.unlicensed_mode
        assert gate.may_trade_live()[0]

    def test_limit_breaches_are_reported_not_silently_enforced(self, keys, machine,
                                                               tmp_path):
        _, public = keys
        path = tmp_path / "licence.key"
        path.write_text(_issue(keys, machine, tier="live_single"), encoding="utf-8")
        gate = LicenseGate(licence_path=str(path), public_key=public,
                           manifest_path=str(tmp_path / "absent.sig"),
                           fingerprint=machine)
        gate.check(now=NOW, force=True)
        breaches = gate.check_limits(instruments=30)
        assert breaches and "covers 8" in breaches[0]

    def test_an_absent_manifest_is_unsigned_not_failed(self, keys, machine, tmp_path):
        """Running from a source checkout is legitimate; refusing to start
        without a manifest would make development impossible."""
        _, public = keys
        path = tmp_path / "licence.key"
        path.write_text(_issue(keys, machine), encoding="utf-8")
        gate = LicenseGate(licence_path=str(path), public_key=public,
                           manifest_path=str(tmp_path / "absent.sig"),
                           fingerprint=machine)
        gate.check(now=NOW, force=True)
        assert gate._status.integrity.unsigned
        assert gate._status.valid


def test_a_licence_file_does_not_leak_the_customers_identifiers(keys, machine):
    """The file is emailed around, so it carries HASHES, never the hostname,
    MAC address or disk ids themselves."""
    _, public = keys
    document = _issue(keys, machine)
    licence, _, _ = parse(document)
    import platform
    assert platform.node() not in document
    for value in licence.machine.values():
        assert len(value) == 16 and all(c in "0123456789abcdef" for c in value)
