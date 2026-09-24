#!/usr/bin/env python3
"""Manage dashboard accounts.

Accounts live in ``<state_dir>/users.db`` (0600). This is the supported way to
create the first owner, add a read-only viewer, rotate a password or disable an
account -- deliberately a command on the server rather than an API endpoint,
because an account-management endpoint is the highest-value target in the whole
surface and nothing about it needs to be reachable from a browser.

    python scripts/manage_users.py add    --username owner --role owner
    python scripts/manage_users.py list
    python scripts/manage_users.py passwd --username owner
    python scripts/manage_users.py role   --username alice --role viewer
    python scripts/manage_users.py disable --username bob
    python scripts/manage_users.py enable  --username bob

Passwords are read from a prompt, never from an argument: a password on the
command line lands in the shell history and in every process listing on the box.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.api.security import SecurityManager, UserStore, totp_uri  # noqa: E402
from sentinel.core.audit import AuditLog  # noqa: E402
from sentinel.core.config import SentinelConfig  # noqa: E402


def _prompt_password() -> str:
    first = getpass.getpass("password (min 12 chars): ")
    if len(first) < 12:
        print("error: password must be at least 12 characters", file=sys.stderr)
        raise SystemExit(2)
    if first != getpass.getpass("repeat: "):
        print("error: passwords do not match", file=sys.stderr)
        raise SystemExit(2)
    return first


def main() -> int:
    ap = argparse.ArgumentParser(description="Sentinel-FX account management")
    ap.add_argument("--config", default=os.environ.get("SENTINEL_CONFIG", "var/config.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="create an account")
    a.add_argument("--username", required=True)
    a.add_argument("--role", choices=["viewer", "operator", "owner"], default="viewer")

    sub.add_parser("list", help="list accounts")

    for name, help_text in (("passwd", "change a password"),
                            ("disable", "disable an account"),
                            ("enable", "re-enable an account")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--username", required=True)

    r = sub.add_parser("role", help="change a role")
    r.add_argument("--username", required=True)
    r.add_argument("--role", choices=["viewer", "operator", "owner"], required=True)

    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"error: no configuration at {cfg_path}. Start the server once first, "
              "or pass --config.", file=sys.stderr)
        return 2
    cfg = SentinelConfig.load(cfg_path)
    state_dir = Path(cfg.ops.state_dir)
    # Its own journal: see the note in ops/watchdog.py. Two processes sharing
    # one hash-chained file break the chain on the first concurrent append.
    audit = AuditLog(Path(cfg.ops.state_dir) / "admin.jsonl")

    # A throwaway signing secret: this process issues no sessions. It must still
    # satisfy the length check, which exists to stop a weak key in the server.
    security = SecurityManager(audit, secret="cli-" + "x" * 40,
                               store=UserStore(state_dir / "users.db"))

    if args.cmd == "add":
        if security.get_user(args.username):
            print(f"error: {args.username!r} already exists", file=sys.stderr)
            return 1
        password = _prompt_password()
        user, uri = security.add_user(args.username, password, args.role)
        print(f"created {user.username!r} with role {user.role}")
        print("\nEnrol an authenticator with this URI. It is printed ONCE:\n")
        print(f"  {uri}\n")
        print(f"  (manual entry secret: {user.totp_secret})")
        return 0

    if args.cmd == "list":
        users = security.list_users()
        if not users:
            print("no accounts. Create one with: manage_users.py add --username owner "
                  "--role owner")
            return 0
        print(f"{'username':<20} {'role':<10} {'state':<10} created")
        for u in users:
            created = datetime.fromtimestamp(u.created_ns / 1e9, timezone.utc)
            state = "disabled" if u.disabled else "active"
            print(f"{u.username:<20} {u.role:<10} {state:<10} "
                  f"{created:%Y-%m-%d %H:%M} UTC")
        return 0

    if security.get_user(args.username) is None:
        print(f"error: no such user {args.username!r}", file=sys.stderr)
        return 1

    if args.cmd == "passwd":
        security.set_password(args.username, _prompt_password())
        print(f"password changed for {args.username!r} "
              "(the TOTP secret is unchanged, by design)")
    elif args.cmd == "role":
        security.set_role(args.username, args.role)
        print(f"{args.username!r} is now {args.role}")
    elif args.cmd == "disable":
        security.set_disabled(args.username, True)
        print(f"{args.username!r} disabled; their active sessions were dropped")
    elif args.cmd == "enable":
        security.set_disabled(args.username, False)
        print(f"{args.username!r} enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
