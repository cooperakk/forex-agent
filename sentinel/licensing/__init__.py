"""Licensing: signed grants, machine binding, and integrity verification.

READ THIS BEFORE TRUSTING IT
============================

What this system genuinely guarantees, by cryptography rather than by
obfuscation:

* **A licence cannot be forged.** Every licence is signed with Ed25519. The
  private key exists only on the vendor's machine; the software embeds only the
  public key. Nobody can produce a licence this software will accept without
  that private key, and no amount of reading the source helps.
* **A licence cannot be edited.** Changing one byte of the payload -- the
  expiry, the machine id, the tier -- invalidates the signature.
* **A licence can be bound to one machine** and to a validity window.
* **Modification of the code is detectable.** A signed manifest of file hashes
  lets the running process check that its own critical modules are the ones the
  vendor shipped.

What it CANNOT guarantee, stated plainly because a licensing module that
oversells itself is worse than none:

* **It cannot stop the machine's owner.** This software runs as Python source
  on a server the customer controls. Someone with root there can delete the
  check. That is true of every client-side licence in every language; native
  compilation and packers raise the cost, they do not change the outcome.
* **The integrity manifest is checked by code that can itself be edited.** It
  raises the bar from "comment out one line" to "understand and patch several
  modules consistently"; it is not a proof.

So the honest security model is: this stops casual copying, accidental
over-deployment, and licence expiry going unnoticed. It does not stop a
determined reverse engineer with root.

**If you need a guarantee rather than a deterrent**, the only structural
answer is to move the valuable part off the customer's machine: keep the
signal generation or the acceptance protocol on a server you run and have the
agent call it. ``ONLINE_ACTIVATION`` below is the hook for that. Everything
else is a lock on a door the customer already owns.
"""

from .fingerprint import machine_fingerprint, fingerprint_components
from .license import (
    License,
    LicenseError,
    LicenseExpired,
    LicenseInvalid,
    LicenseMachineMismatch,
    LicenseTier,
    issue,
    load_public_key,
    parse,
    verify,
)
from .integrity import IntegrityReport, build_manifest, verify_manifest
from .enforcement import LicenseGate, LicenseStatus

__all__ = [
    "License", "LicenseError", "LicenseExpired", "LicenseInvalid",
    "LicenseMachineMismatch", "LicenseTier", "issue", "parse", "verify",
    "load_public_key", "machine_fingerprint", "fingerprint_components",
    "IntegrityReport", "build_manifest", "verify_manifest",
    "LicenseGate", "LicenseStatus",
]
