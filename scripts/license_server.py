#!/usr/bin/env python3
"""Sentinel-FX activation server -- VENDOR SIDE ONLY.

Issues the short-lived signed leases that a licence with an ``activation_url``
needs before it may open live positions (see sentinel/licensing/activation.py).
It is the one part of the licensing system that runs on a machine the licensee
does not control, which is why it is the only part that can revoke a licence
or refuse an extra installation after the fact.

Keys
----
Run it with a LEASE key, not the licence-signing key::

    ./licensegen.py keygen --out ./lease-keys            # a second keypair
    ./licensegen.py embed-key --pubkey ./vendor-keys/public.txt \\
        --lease-pubkey ./lease-keys/public.txt           # at release time

The licence key stays offline. If this server is ever compromised, the attacker
can mint leases for licences that already exist -- they still cannot mint a
licence -- and rotating the lease key is a release, not a re-issue of every
customer's licence.

Usage::

    # serve (behind TLS: a reverse proxy, or --certfile/--keyfile)
    python scripts/license_server.py serve --db var/licence-server.db \\
        --lease-key ./lease-keys/private.pem --vendor-pubkey ./vendor-keys/public.txt \\
        --host 127.0.0.1 --port 8443

    # administration (same database, no network)
    python scripts/license_server.py register --db ... --licence acme.key --seats 1
    python scripts/license_server.py revoke   --db ... --licence-id ABCD-... --reason "chargeback"
    python scripts/license_server.py reinstate --db ... --licence-id ABCD-...
    python scripts/license_server.py reset-devices --db ... --licence-id ABCD-...
    python scripts/license_server.py list --db ...
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from sentinel.licensing.activation import sign_lease  # noqa: E402
from sentinel.licensing.license import LicenseError, verify  # noqa: E402

class LeaseRequest(BaseModel):
    # Module level, like the fastapi imports: with postponed annotations,
    # names local to create_app() cannot be resolved by FastAPI, and the body
    # and the request were silently treated as missing query parameters.
    licence: str = Field(min_length=64, max_length=16384)
    device: str = Field(min_length=64, max_length=64)
    nonce: str = Field(min_length=8, max_length=64)
    client_time: str = Field(default="", max_length=40)
    version: str = Field(default="", max_length=40)


SCHEMA = """
CREATE TABLE IF NOT EXISTS licences (
    licence_id  TEXT PRIMARY KEY,
    issued_to   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    reason      TEXT NOT NULL DEFAULT '',
    seats       INTEGER,
    created_ns  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    licence_id  TEXT NOT NULL,
    device      TEXT NOT NULL,
    first_ns    INTEGER NOT NULL,
    last_ns     INTEGER NOT NULL,
    PRIMARY KEY (licence_id, device)
);
"""


class Registry:
    """Which licences exist, their status, and the machines using them."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def register(self, licence_id: str, issued_to: str, seats: Optional[int]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO licences (licence_id, issued_to, seats, created_ns) "
                "VALUES (?,?,?,?) ON CONFLICT(licence_id) DO UPDATE SET "
                "seats=excluded.seats, issued_to=excluded.issued_to",
                (licence_id, issued_to, seats, time.time_ns()))
            self._conn.commit()

    def get(self, licence_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM licences WHERE licence_id=?",
                                      (licence_id,)).fetchone()

    def set_status(self, licence_id: str, status: str, reason: str = "") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE licences SET status=?, reason=? WHERE licence_id=?",
                (status, reason, licence_id))
            self._conn.commit()
            return cur.rowcount > 0

    def devices(self, licence_id: str) -> List[str]:
        with self._lock:
            rows = self._conn.execute("SELECT device FROM devices WHERE licence_id=?",
                                      (licence_id,)).fetchall()
        return [r["device"] for r in rows]

    def touch_device(self, licence_id: str, device: str) -> None:
        now = time.time_ns()
        with self._lock:
            self._conn.execute(
                "INSERT INTO devices (licence_id, device, first_ns, last_ns) VALUES "
                "(?,?,?,?) ON CONFLICT(licence_id, device) DO UPDATE SET last_ns=?",
                (licence_id, device, now, now, now))
            self._conn.commit()

    def reset_devices(self, licence_id: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM devices WHERE licence_id=?",
                                     (licence_id,))
            self._conn.commit()
            return cur.rowcount

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM licences ORDER BY created_ns").fetchall()
        return [dict(r) | {"devices": len(self.devices(r["licence_id"]))} for r in rows]


def decide(registry: Registry, *, licence_doc: str, device: str, nonce: str,
           vendor_pubkey: str, lease_key_pem: str, lease_hours: float,
           auto_register: bool, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The whole server policy, as a pure function of the request."""
    current = now or datetime.now(timezone.utc)
    if not device or len(device) != 64 or not nonce or len(nonce) > 64:
        raise ValueError("malformed request")
    try:
        # Grace is generous here on purpose: the CLIENT enforces expiry with
        # its own grace period; the server's question is only "is this a
        # genuine licence of ours, and has it been withdrawn?".
        licence = verify(licence_doc, vendor_pubkey, check_machine=False,
                         grace_days=3650, now=current)
    except LicenseError as exc:
        raise ValueError(f"licence rejected: {exc}") from exc

    row = registry.get(licence.licence_id)
    if row is None:
        if not auto_register:
            return sign_lease(lease_key_pem, licence_id=licence.licence_id, device=device,
                              nonce=nonce, status="suspended", lease_hours=1,
                              message="this licence is not registered with the vendor",
                              now=current)
        seats = licence.capability("max_accounts")
        registry.register(licence.licence_id, licence.issued_to,
                          int(seats) if seats is not None else None)
        row = registry.get(licence.licence_id)
    assert row is not None

    if row["status"] != "active":
        return sign_lease(lease_key_pem, licence_id=licence.licence_id, device=device,
                          nonce=nonce, status=row["status"], lease_hours=24,
                          message=row["reason"] or "withdrawn by the vendor", now=current)

    known = registry.devices(licence.licence_id)
    seats = row["seats"]
    if device not in known and seats is not None and len(known) >= int(seats):
        return sign_lease(lease_key_pem, licence_id=licence.licence_id, device=device,
                          nonce=nonce, status="suspended", lease_hours=1,
                          message=(f"all {seats} installation(s) of this licence are in "
                                   "use; ask the vendor to release one"),
                          seats_used=len(known), seats_allowed=int(seats), now=current)
    registry.touch_device(licence.licence_id, device)
    used = len(set(known) | {device})
    return sign_lease(lease_key_pem, licence_id=licence.licence_id, device=device,
                      nonce=nonce, status="active", lease_hours=lease_hours,
                      seats_used=used,
                      seats_allowed=int(seats) if seats is not None else None,
                      now=current)


def create_app(registry: Registry, *, vendor_pubkey: str, lease_key_pem: str,
               lease_hours: float = 72.0, auto_register: bool = False,
               rate_per_minute: int = 30):
    app = FastAPI(title="Sentinel-FX activation", docs_url=None, redoc_url=None,
                  openapi_url=None)
    hits: Dict[str, List[float]] = {}
    hits_lock = threading.Lock()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/v1/lease")
    def lease(body: LeaseRequest, request: Request):
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with hits_lock:
            window = [t for t in hits.get(ip, []) if now - t < 60]
            if len(window) >= rate_per_minute:
                raise HTTPException(429, "too many requests")
            window.append(now)
            hits[ip] = window
            if len(hits) > 10000:
                hits.clear()
        try:
            return decide(registry, licence_doc=body.licence, device=body.device,
                          nonce=body.nonce, vendor_pubkey=vendor_pubkey,
                          lease_key_pem=lease_key_pem, lease_hours=lease_hours,
                          auto_register=auto_register)
        except ValueError as exc:
            raise HTTPException(400, str(exc)[:300]) from exc

    return app


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the activation endpoint")
    p.add_argument("--db", required=True)
    p.add_argument("--lease-key", required=True, help="PEM of the LEASE private key")
    p.add_argument("--vendor-pubkey", required=True, help="file with the licence public key")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--lease-hours", type=float, default=72.0)
    p.add_argument("--auto-register", action="store_true",
                   help="accept any genuine licence on first sight (seats from its tier)")
    p.add_argument("--certfile")
    p.add_argument("--keyfile")

    p = sub.add_parser("register", help="register a licence file")
    p.add_argument("--db", required=True)
    p.add_argument("--licence", required=True)
    p.add_argument("--vendor-pubkey", required=True)
    p.add_argument("--seats", type=int, default=None,
                   help="installations allowed (default: the licence's max_accounts)")

    for name in ("revoke", "suspend", "reinstate", "reset-devices"):
        p = sub.add_parser(name)
        p.add_argument("--db", required=True)
        p.add_argument("--licence-id", required=True)
        p.add_argument("--reason", default="")

    p = sub.add_parser("list")
    p.add_argument("--db", required=True)

    args = ap.parse_args(argv)
    registry = Registry(args.db)

    if args.cmd == "serve":
        import uvicorn
        app = create_app(registry, vendor_pubkey=_read(args.vendor_pubkey),
                         lease_key_pem=Path(args.lease_key).read_text(encoding="utf-8"),
                         lease_hours=args.lease_hours, auto_register=args.auto_register)
        if not args.certfile and args.host not in ("127.0.0.1", "localhost", "::1"):
            print("refusing to serve leases over plain HTTP on a public address; pass "
                  "--certfile/--keyfile or bind to loopback behind a TLS proxy",
                  file=sys.stderr)
            return 2
        uvicorn.run(app, host=args.host, port=args.port,
                    ssl_certfile=args.certfile, ssl_keyfile=args.keyfile)
        return 0
    if args.cmd == "register":
        doc = Path(args.licence).read_text(encoding="utf-8")
        licence = verify(doc, _read(args.vendor_pubkey), check_machine=False,
                         grace_days=3650)
        seats = args.seats if args.seats is not None else licence.capability("max_accounts")
        registry.register(licence.licence_id, licence.issued_to,
                          int(seats) if seats is not None else None)
        print(f"registered {licence.licence_id} ({licence.issued_to}), seats={seats}")
        return 0
    if args.cmd in ("revoke", "suspend", "reinstate"):
        status = {"revoke": "revoked", "suspend": "suspended", "reinstate": "active"}[args.cmd]
        ok = registry.set_status(args.licence_id, status, args.reason)
        print(f"{args.licence_id}: {status}" if ok else "no such licence")
        return 0 if ok else 1
    if args.cmd == "reset-devices":
        print(f"released {registry.reset_devices(args.licence_id)} installation(s)")
        return 0
    if args.cmd == "list":
        for row in registry.all():
            print(f"{row['licence_id']}  {row['status']:<9} seats={row['seats']} "
                  f"devices={row['devices']}  {row['issued_to']}  {row['reason']}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
