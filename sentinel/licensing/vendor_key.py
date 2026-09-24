"""The vendor's public keys, embedded at release time.

This file is EMPTY in the source repository and is rewritten by
``scripts/licensegen.py embed-key`` (or ``scripts/build_protected.py``) when a
distributable build is produced. It is covered by the signed integrity
manifest, so a customer cannot swap in their own key without the manifest
failing -- and with an embedded key the manifest is REQUIRED for live trading.

Why this exists: before 1.5.0 the only source of the vendor key was the
``SENTINEL_LICENSE_PUBKEY`` environment variable. A customer could unset it,
which switched licensing off entirely ("unlicensed mode"), or set it to a key
they generated themselves and sign their own unlimited licence. A key that the
licensee controls is not a licence check.

Resolution order, implemented in ``enforcement.resolve_vendor_key``:

1. an explicit ``public_key=`` argument (tests, ``licensegen inspect``);
2. ``EMBEDDED_PUBLIC_KEY`` below, when non-empty -- and then the environment
   variable is IGNORED;
3. ``SENTINEL_LICENSE_PUBKEY``, only for self-hosted builds with no embedded
   key, where the customer is the vendor.
"""

#: Base64 of the raw 32-byte Ed25519 public key that signs licences.
EMBEDDED_PUBLIC_KEY = ""

#: Optional separate key for online activation leases. When empty, leases are
#: verified with the licence key above. A separate key lets the vendor keep
#: the licence-signing key offline while an internet-facing activation server
#: holds only the (less powerful) lease key.
EMBEDDED_LEASE_PUBLIC_KEY = ""
