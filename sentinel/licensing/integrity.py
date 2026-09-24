"""Code integrity: a signed manifest of file hashes.

The runtime checks that its own critical modules are the ones the vendor
shipped. Be clear about what this is worth:

* It DOES detect accidental corruption, a partial upgrade, a half-applied
  patch, and a casual edit of the licence check.
* It DOES NOT stop a determined attacker, because the code doing the checking
  can itself be edited. It raises the cost from "comment out one line" to
  "patch several modules consistently and regenerate a signature you do not
  have the key for" -- which in practice means deleting the check entirely,
  which is detectable by anyone who later compares the install to a release.

It is a tamper-EVIDENCE mechanism, exactly like the audit journal, and it is
described that way everywhere it appears.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from cryptography.exceptions import InvalidSignature

from .license import load_private_key, load_public_key

#: Modules whose modification would change what the system is allowed to do.
#: Deliberately short: a manifest covering every file breaks on every harmless
#: edit and trains the operator to ignore it.
PROTECTED_PATHS = (
    "sentinel/licensing/license.py",
    "sentinel/licensing/enforcement.py",
    "sentinel/licensing/integrity.py",
    "sentinel/licensing/fingerprint.py",
    "sentinel/risk/engine.py",
    "sentinel/risk/sizing.py",
    "sentinel/risk/protect.py",
    "sentinel/core/audit.py",
    "sentinel/core/money.py",
    "sentinel/core/ids.py",
    "sentinel/research/verdicts.py",
    "sentinel/api/security.py",
    # 1.5.0: where the vendor key lives, the online activation check, the
    # anti-rollback guard, and the two places the licence gate is wired in.
    # Editing any of them is the short path around every check above.
    "sentinel/licensing/vendor_key.py",
    "sentinel/licensing/activation.py",
    "sentinel/licensing/clock_guard.py",
    "sentinel/bootstrap.py",
    "sentinel/agent/orchestrator.py",
)


@dataclass
class IntegrityReport:
    ok: bool
    checked: int = 0
    missing: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    unsigned: bool = False
    error: Optional[str] = None

    def summary(self) -> str:
        if self.error:
            return f"integrity check could not run: {self.error}"
        if self.ok:
            return f"integrity verified: {self.checked} protected files match the release"
        parts = []
        if self.missing:
            parts.append(f"{len(self.missing)} missing ({', '.join(self.missing[:3])})")
        if self.modified:
            parts.append(f"{len(self.modified)} modified ({', '.join(self.modified[:3])})")
        return "integrity FAILED: " + "; ".join(parts)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checked": self.checked, "missing": self.missing,
                "modified": self.modified, "unsigned": self.unsigned,
                "error": self.error, "summary": self.summary()}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(root: str | Path, private_key_pem: str,
                   *, paths: Optional[List[str]] = None,
                   version: str = "") -> str:
    """Produce a signed manifest. Vendor side, at release time."""
    base = Path(root)
    entries: Dict[str, str] = {}
    for rel in (paths or PROTECTED_PATHS):
        target = base / rel
        if not target.is_file():
            raise FileNotFoundError(f"protected path missing at build time: {rel}")
        entries[rel] = _hash_file(target)

    payload = {"version": version, "algorithm": "sha256", "files": entries}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = load_private_key(private_key_pem).sign(canonical)
    return json.dumps({
        "payload": payload,
        "signature": base64.b64encode(signature).decode("ascii"),
    }, indent=2, sort_keys=True) + "\n"


def verify_manifest(root: str | Path, manifest_text: str,
                    public_key_b64: str) -> IntegrityReport:
    """Check this installation against a signed manifest.

    A manifest that is absent is reported as ``unsigned`` rather than as a
    failure: running from a source checkout is legitimate, and refusing to
    start without a manifest would make development impossible. The CALLER
    decides what an unsigned installation means -- see `enforcement.py`, where
    live trading requires a verified one and paper trading does not.
    """
    try:
        document = json.loads(manifest_text)
        payload = document["payload"]
        signature = base64.b64decode(document["signature"], validate=True)
    except Exception as exc:  # noqa: BLE001
        return IntegrityReport(ok=False, error=f"manifest is corrupt: {exc}")

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        load_public_key(public_key_b64).verify(signature, canonical)
    except InvalidSignature:
        return IntegrityReport(
            ok=False,
            error="the manifest's own signature does not verify: it was not "
                  "produced by this vendor, or it was edited")
    except Exception as exc:  # noqa: BLE001
        return IntegrityReport(ok=False, error=str(exc))

    base = Path(root)
    missing: List[str] = []
    modified: List[str] = []
    checked = 0
    for rel, expected in sorted(payload.get("files", {}).items()):
        target = base / rel
        if not target.is_file():
            missing.append(rel)
            continue
        checked += 1
        if _hash_file(target) != expected:
            modified.append(rel)
    return IntegrityReport(ok=not missing and not modified, checked=checked,
                           missing=missing, modified=modified)
