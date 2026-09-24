"""Online activation: short-lived, signed leases from the vendor's server.

Everything else in this package runs on a machine the licensee controls, so
everything else can in principle be patched out by someone with root. This is
the one control whose answer comes from ELSEWHERE: a licence that carries an
``activation_url`` must hold a current lease, signed by the vendor, before it
may open live positions. Revoking a licence, or refusing an extra installation
of it, takes effect on the vendor's server, where the licensee cannot edit it.

Protocol
--------
The client POSTs to ``activation_url``::

    {"licence": "<the full signed licence document>",
     "device": "<sha256 of this machine's hashed fingerprint>",
     "nonce": "<16 random bytes, base64>", "client_time": "<RFC3339>",
     "version": "<software version>"}

The server re-verifies the licence signature, checks its own registry
(revocation, seat count), and answers with a document signed by the lease
key (Ed25519)::

    {"payload": {"licence_id", "device", "nonce", "status", "issued_at",
                 "expires_at", "server_time", "message", "seats_used",
                 "seats_allowed", "format"},
     "signature": "<base64>", "algorithm": "Ed25519"}

The client accepts it only if the signature verifies, the NONCE is the one it
just sent (a recorded answer cannot be replayed), the licence id and the device
digest are its own, and the lease has not expired. The lease is cached on disk
and re-verified on every check -- except the nonce, which binds only the fresh
exchange; a cached lease is bounded by its own ``expires_at`` instead.

Failure policy (the rule of the whole package: a licence problem must never make
the system dangerous):

* the server is unreachable  -> the cached lease keeps working until it expires,
  so a vendor outage or a flaky link never stops a live book at 3am;
* no valid lease             -> no NEW live entries; paper, research, the
  dashboard and the management of OPEN positions are untouched;
* ``status == "revoked"``    -> as above, and the reason is shown verbatim.

The server's clock also gives the client a time reference it does not control:
a local clock more than the rollback tolerance behind the last ``server_time``
seen is treated as rolled back.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from cryptography.exceptions import InvalidSignature

from .license import License, _canonical, _parse_ts, _rfc3339, load_private_key, \
    load_public_key

LEASE_FORMAT = 1
#: A lease dated this far in the future is a clock problem, not a forgery.
_SERVER_SLACK = timedelta(minutes=10)
#: Local clock this far behind the server's last word is a rollback.
ROLLBACK_TOLERANCE = timedelta(hours=6)
#: Network budget for one activation round trip.
DEFAULT_TIMEOUT_SEC = 10.0

Transport = Callable[[str, Dict[str, Any], float], Dict[str, Any]]


class LeaseError(RuntimeError):
    """A lease that must not be trusted, with the reason."""


def device_digest(fingerprint: Dict[str, str]) -> str:
    """One stable digest of this machine's (already hashed) identifiers."""
    canon = json.dumps({k: v for k, v in sorted((fingerprint or {}).items()) if v},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def new_nonce() -> str:
    return base64.b64encode(secrets.token_bytes(16)).decode("ascii")


# --------------------------------------------------------------------------- #
# server side: signing
# --------------------------------------------------------------------------- #


def sign_lease(private_key_pem: str, *, licence_id: str, device: str, nonce: str,
               status: str = "active", lease_hours: float = 72.0,
               message: str = "", seats_used: int = 0,
               seats_allowed: Optional[int] = None,
               now: Optional[datetime] = None) -> Dict[str, Any]:
    """Produce a signed lease. Vendor server only."""
    if status not in ("active", "revoked", "suspended"):
        raise ValueError(f"unknown lease status {status!r}")
    if lease_hours <= 0 or lease_hours > 24 * 90:
        raise ValueError("lease_hours must be within (0, 2160]")
    current = now or datetime.now(timezone.utc)
    payload = {
        "format": LEASE_FORMAT,
        "licence_id": str(licence_id),
        "device": str(device),
        "nonce": str(nonce),
        "status": status,
        "issued_at": _rfc3339(current.timestamp()),
        "expires_at": _rfc3339((current + timedelta(hours=lease_hours)).timestamp()),
        "server_time": _rfc3339(current.timestamp()),
        "message": str(message)[:500],
        "seats_used": int(seats_used),
        "seats_allowed": None if seats_allowed is None else int(seats_allowed),
    }
    signature = load_private_key(private_key_pem).sign(_canonical(payload))
    return {"payload": payload, "algorithm": "Ed25519",
            "signature": base64.b64encode(signature).decode("ascii")}


# --------------------------------------------------------------------------- #
# client side: verification
# --------------------------------------------------------------------------- #


@dataclass
class Lease:
    licence_id: str
    device: str
    nonce: str
    status: str
    issued_at: str
    expires_at: str
    server_time: str
    message: str = ""
    seats_used: int = 0
    seats_allowed: Optional[int] = None
    format: int = LEASE_FORMAT

    @property
    def expiry(self) -> datetime:
        return _parse_ts(self.expires_at)

    @property
    def issued(self) -> datetime:
        return _parse_ts(self.issued_at)

    @property
    def server_clock(self) -> datetime:
        return _parse_ts(self.server_time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def verify_lease(document: Dict[str, Any], public_key_b64: str, *,
                 licence_id: str, device: str, nonce: Optional[str] = None,
                 now: Optional[datetime] = None) -> Lease:
    """Every check a lease must pass before it grants anything."""
    if not isinstance(document, dict):
        raise LeaseError("the activation answer is not a JSON object")
    if document.get("algorithm") != "Ed25519":
        raise LeaseError(f"unsupported lease algorithm {document.get('algorithm')!r}")
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise LeaseError("the lease has no payload")
    try:
        signature = base64.b64decode(str(document.get("signature", "")), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise LeaseError(f"the lease signature is not base64: {exc}") from exc
    try:
        load_public_key(public_key_b64).verify(signature, _canonical(payload))
    except InvalidSignature as exc:
        raise LeaseError("the lease signature does not verify: it was not issued by "
                         "the vendor's activation server, or it was edited") from exc
    except Exception as exc:  # noqa: BLE001
        raise LeaseError(f"the lease could not be verified: {exc}") from exc
    try:
        if int(payload.get("format", 0)) > LEASE_FORMAT:
            raise LeaseError("this lease format is newer than this software")
        known = set(Lease.__dataclass_fields__)
        lease = Lease(**{k: v for k, v in payload.items() if k in known})
        expiry, issued, server_clock = lease.expiry, lease.issued, lease.server_clock
    except LeaseError:
        raise
    except Exception as exc:  # noqa: BLE001 - signed but malformed is still malformed
        raise LeaseError(f"the lease is malformed: {exc}") from exc

    if lease.licence_id != licence_id:
        raise LeaseError("the lease belongs to a different licence")
    if lease.device != device:
        raise LeaseError("the lease was issued for a different machine")
    if nonce is not None and not secrets.compare_digest(str(lease.nonce), str(nonce)):
        raise LeaseError("the lease answers a different request (nonce mismatch); a "
                         "recorded answer cannot be replayed")
    current = now or datetime.now(timezone.utc)
    if issued > current + _SERVER_SLACK or server_clock > current + _SERVER_SLACK:
        raise LeaseError("the lease is dated in the future: check this machine's clock")
    if expiry <= current:
        raise LeaseError(f"the activation lease expired at {lease.expires_at}")
    return lease


def _http_transport(url: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """POST JSON over HTTPS. Plain HTTP only to a loopback server (testing)."""
    import httpx
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and host not in ("127.0.0.1", "localhost", "::1"):
        raise LeaseError("the activation URL must use https")
    response = httpx.post(url, json=body, timeout=timeout,
                          headers={"User-Agent": "Sentinel-FX activation"})
    if response.status_code >= 500:
        raise LeaseError(f"activation server error {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise LeaseError("the activation server did not answer with JSON") from exc
    if response.status_code >= 400:
        # A signed refusal (revoked, seats exhausted) arrives as a lease with a
        # non-active status and a 200; anything else is a transport problem.
        detail = data.get("detail") if isinstance(data, dict) else None
        raise LeaseError(f"activation refused ({response.status_code}): {detail}")
    return data


@dataclass
class LeaseStatus:
    required: bool
    ok: bool
    reason: str = ""
    lease: Optional[Lease] = None
    refreshed: bool = False
    network_error: str = ""
    rolled_back: bool = False
    checked_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"required": self.required, "ok": self.ok, "reason": self.reason,
                "lease": self.lease.to_dict() if self.lease else None,
                "refreshed": self.refreshed, "network_error": self.network_error,
                "rolled_back": self.rolled_back, "checked_at": self.checked_at}


class ActivationClient:
    """Keeps a current lease for one licence on this machine."""

    def __init__(self, lease_path: str | Path, *, public_key_b64: str,
                 transport: Optional[Transport] = None,
                 timeout_sec: float = DEFAULT_TIMEOUT_SEC,
                 software_version: str = "") -> None:
        self.lease_path = Path(lease_path)
        self.public_key = public_key_b64
        self.transport = transport or _http_transport
        self.timeout_sec = float(timeout_sec)
        self.software_version = software_version
        self._last_attempt: Optional[datetime] = None

    # -- cache -------------------------------------------------------------- #

    def _read_cached(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self.lease_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_cached(self, document: Dict[str, Any]) -> None:
        self.lease_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.lease_path.with_suffix(self.lease_path.suffix + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.write(fd, json.dumps(document, sort_keys=True).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.lease_path)

    # -- the question -------------------------------------------------------- #

    def status(self, licence: License, licence_document: str,
               fingerprint: Dict[str, str], *, now: Optional[datetime] = None,
               allow_network: bool = True) -> LeaseStatus:
        current = now or datetime.now(timezone.utc)
        stamp = _rfc3339(current.timestamp())
        if not licence.activation_url or licence.activation_interval_hours <= 0:
            return LeaseStatus(required=False, ok=True, checked_at=stamp)
        device = device_digest(fingerprint)
        interval = timedelta(hours=float(licence.activation_interval_hours))

        cached: Optional[Lease] = None
        cache_problem = ""
        document = self._read_cached()
        if document is not None:
            try:
                cached = verify_lease(document, self.public_key,
                                      licence_id=licence.licence_id, device=device,
                                      now=current)
            except LeaseError as exc:
                cache_problem = str(exc)
                cached = None

        due = cached is None or (current - cached.issued) >= interval
        # Do not hammer an unreachable server: one attempt per 5 minutes.
        throttled = (self._last_attempt is not None
                     and current - self._last_attempt < timedelta(minutes=5)
                     and cached is not None)
        network_error = ""
        refreshed = False
        if due and allow_network and not throttled:
            self._last_attempt = current
            nonce = new_nonce()
            body = {"licence": licence_document, "device": device, "nonce": nonce,
                    "client_time": stamp, "version": self.software_version}
            try:
                answer = self.transport(licence.activation_url, body, self.timeout_sec)
                fresh = verify_lease(answer, self.public_key,
                                     licence_id=licence.licence_id, device=device,
                                     nonce=nonce, now=current)
                self._write_cached(answer)
                cached, refreshed, cache_problem = fresh, True, ""
            except LeaseError as exc:
                network_error = str(exc)
            except Exception as exc:  # noqa: BLE001 - a network failure is data
                network_error = f"{type(exc).__name__}: {exc}"

        if cached is None:
            reason = ("this licence must be activated online before live trading, and "
                      "no valid activation lease is held")
            detail = network_error or cache_problem
            return LeaseStatus(required=True, ok=False,
                               reason=reason + (f" ({detail})" if detail else ""),
                               refreshed=refreshed, network_error=network_error,
                               checked_at=stamp)
        if current + ROLLBACK_TOLERANCE < cached.server_clock:
            return LeaseStatus(required=True, ok=False, lease=cached, rolled_back=True,
                               reason=("this machine's clock is behind the time the "
                                       "activation server last reported; correct the "
                                       "clock before live trading"),
                               refreshed=refreshed, network_error=network_error,
                               checked_at=stamp)
        if cached.status != "active":
            return LeaseStatus(required=True, ok=False, lease=cached,
                               reason=(f"the vendor's activation server reports this "
                                       f"licence as {cached.status}"
                                       + (f": {cached.message}" if cached.message else "")),
                               refreshed=refreshed, network_error=network_error,
                               checked_at=stamp)
        return LeaseStatus(required=True, ok=True, lease=cached, refreshed=refreshed,
                           network_error=network_error, checked_at=stamp)


__all__ = ["ActivationClient", "Lease", "LeaseError", "LeaseStatus", "device_digest",
           "new_nonce", "sign_lease", "verify_lease"]
