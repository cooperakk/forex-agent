#!/usr/bin/env python3
"""Sentinel-FX licence generator -- VENDOR SIDE ONLY.

This tool holds the private key. It must never be shipped to a customer, and
the key must never leave the machine it was generated on.

    # once, offline, on a machine you control:
    ./licensegen.py keygen --out ./vendor-keys

    # the customer runs this on their server and sends you the output:
    ./licensegen.py fingerprint

    # you issue:
    ./licensegen.py issue --key ./vendor-keys/private.pem \
        --to "Acme Capital" --tier live_single --months 3 \
        --machine-file customer-fingerprint.json --out acme-q1.key

    # three months later, when they pay again -- the new term starts where the
    # old one ended, so renewing early costs the customer nothing:
    ./licensegen.py renew --key ./vendor-keys/private.pem \
        --pubkey ./vendor-keys/public.txt \
        --licence acme-q1.key --out acme-q2.key

    # at release time, so installs can verify they are unmodified:
    ./licensegen.py manifest --key ./vendor-keys/private.pem --version 1.0.0

    # anyone can check a licence against the public key:
    ./licensegen.py inspect --licence acme.key --pubkey ./vendor-keys/public.txt

WHAT THIS PROTECTS AGAINST, honestly: forging a licence (impossible without the
private key), editing one (breaks the signature), copying one to another
machine (fingerprint mismatch), and using one past its expiry.

WHAT IT DOES NOT: someone with root on the customer's server can delete the
check from the Python source. See sentinel/licensing/__init__.py. If you need a
guarantee rather than a deterrent, keep the valuable computation on your own
server -- that is the only structural answer.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.licensing.fingerprint import (  # noqa: E402
    fingerprint_components, machine_fingerprint,
)
from sentinel.licensing.integrity import (  # noqa: E402
    PROTECTED_PATHS, build_manifest, verify_manifest,
)
from sentinel.licensing.license import (
    STANDARD_TERMS,
    DEFAULT_TERM_MONTHS,
    renew,  # noqa: E402
    TIER_CAPABILITIES, generate_keypair, issue, parse, verify,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_private(path: Path, pem: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(pem)
    os.chmod(path, 0o600)


def _resolve_pubkey(value):
    """Accept a file path OR the base64 key itself.

    `keygen` prints the key to the terminal, so pasting it straight into
    --pubkey is the obvious thing to try; it used to raise a bare
    FileNotFoundError traceback.
    """
    if not value:
        return os.environ.get("SENTINEL_LICENSE_PUBKEY", "")
    candidate = Path(value)
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8").strip()
    text = value.strip()
    if len(text) >= 43 and "/" not in text.rstrip("=").replace("+", ""):
        return text
    if len(text) >= 43:
        return text
    raise SystemExit(f"--pubkey: {value!r} is neither a readable file nor a "
                     "base64 Ed25519 public key")


def cmd_keygen(args) -> int:
    out = Path(args.out)
    private = out / "private.pem"
    public = out / "public.txt"
    if private.exists() and not args.force:
        print(f"refusing to overwrite {private}.\n"
              "Regenerating the key invalidates EVERY licence already issued.\n"
              "Pass --force only if you mean exactly that.", file=sys.stderr)
        return 1
    if private.exists():
        private.unlink()
    pem, pub = generate_keypair()
    _write_private(private, pem)
    public.write_text(pub + "\n", encoding="utf-8")
    print(f"private key -> {private}   (0600, NEVER distribute this)")
    print(f"public key  -> {public}")
    print()
    print("Embed the public key in the build you ship:")
    print(f"    SENTINEL_LICENSE_PUBKEY={pub}")
    print()
    print("Back the private key up offline. If you lose it you cannot issue or")
    print("renew any licence; if it leaks, every licence becomes forgeable and")
    print("the only remedy is a new key and a re-issue of the whole field.")
    return 0


def cmd_fingerprint(args) -> int:
    """Run on the CUSTOMER's server. Output is safe to email: it is hashed."""
    fp = machine_fingerprint()
    payload = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0)
                        .isoformat().replace("+00:00", "Z"),
        "fingerprint": fp,
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"written to {args.out}")
    else:
        print(text)
    if args.verbose:
        print("\nRaw components (NOT sent; shown so you can see what is read):",
              file=sys.stderr)
        for key, value in fingerprint_components().items():
            print(f"  {key:12s} {value}", file=sys.stderr)
    print(f"\n{len(fp)} identifiers collected. Send this file to the vendor.",
          file=sys.stderr)
    return 0


def cmd_issue(args) -> int:
    pem = Path(args.key).read_text(encoding="utf-8")

    machine = {}
    if args.machine_file:
        raw = json.loads(Path(args.machine_file).read_text(encoding="utf-8"))
        machine = raw.get("fingerprint", raw)
        if not isinstance(machine, dict) or not machine:
            print("the machine file has no fingerprint map", file=sys.stderr)
            return 1
    elif args.this_machine:
        machine = machine_fingerprint()

    capabilities = {}
    for item in args.capability or []:
        if "=" not in item:
            print(f"--capability expects name=value, got {item!r}", file=sys.stderr)
            return 1
        name, _, value = item.partition("=")
        if value.lower() in ("true", "false"):
            parsed = value.lower() == "true"
        elif value.lower() in ("none", "null", "unlimited"):
            parsed = None
        else:
            try:
                parsed = int(value)
            except ValueError:
                parsed = value
        capabilities[name.strip()] = parsed

    # Build the term arguments CONDITIONALLY. `valid_days=None` is the
    # historical spelling of "perpetual", so passing it whenever --days was
    # absent -- which is almost always -- issued a licence that never expires,
    # whatever --months said. Every licence this command produced was
    # perpetual, silently, and the summary printed below said so in a line
    # nobody reads twice.
    term_kwargs: dict = {}
    if args.perpetual:
        term_kwargs["perpetual"] = True
    elif args.days:
        term_kwargs["valid_days"] = args.days
    else:
        term_kwargs["term_months"] = args.months

    try:
        document = issue(
            private_key_pem=pem,
            issued_to=args.to,
            tier=args.tier,
            machine=machine,
            capabilities=capabilities,
            notes=args.notes or "",
            activation_url=args.activation_url,
            activation_interval_hours=args.activation_interval,
            allow_unbound=args.unbound,
            **term_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else None
    if out:
        out.write_text(document, encoding="utf-8")
        os.chmod(out, 0o600)
        print(f"licence -> {out}")
    else:
        print(document)

    licence, _, _ = parse(document)
    print(f"  id        {licence.licence_id}", file=sys.stderr)
    print(f"  to        {licence.issued_to}", file=sys.stderr)
    print(f"  tier      {licence.tier}", file=sys.stderr)
    print(f"  term      {licence.term_label} "
          f"(term {licence.term_index} of subscription {licence.subscription_id})",
          file=sys.stderr)
    if licence.expires_at is None:
        print("  WARNING: this licence NEVER EXPIRES. If that was not intended, "
              "re-issue with --months.", file=sys.stderr)
    print(f"  expires   {licence.expires_at or 'never'}", file=sys.stderr)
    print(f"  machine   {len(licence.machine)} identifiers"
          f"{' (UNBOUND - runs anywhere)' if not licence.machine else ''}",
          file=sys.stderr)
    return 0


def cmd_renew(args) -> int:
    """Extend an existing subscription by one more term.

    This exists as its own command rather than "issue another one" because the
    two differ in ways a vendor gets wrong by hand: the new term must start
    where the old one ended (not today), the subscription id must carry over
    (so the customer's history stays one thread), and the machine binding and
    capabilities must be inherited unless deliberately changed.
    """
    pem = Path(args.key).read_text(encoding="utf-8")
    document = Path(args.licence).read_text(encoding="utf-8")
    pub = _resolve_pubkey(args.pubkey)
    if not pub:
        print("--pubkey is required: the old licence is verified before it is "
              "extended, so that a forged file cannot be laundered into a "
              "genuine one by renewing it.", file=sys.stderr)
        return 1

    machine = None
    if args.machine_file:
        raw = json.loads(Path(args.machine_file).read_text(encoding="utf-8"))
        machine = raw.get("fingerprint", raw)
    elif args.this_machine:
        machine = machine_fingerprint()

    try:
        renewed = renew(document=document, private_key_pem=pem,
                        public_key_b64=pub,
                        term_months=args.months,
                        machine=machine,
                        tier=args.tier or None,
                        late_renewal_grace_days=args.late_grace)
    except Exception as exc:  # noqa: BLE001
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    before, _, _ = parse(document)
    after, _, _ = parse(renewed)
    out = Path(args.out) if args.out else None
    if out:
        out.write_text(renewed, encoding="utf-8")
        os.chmod(out, 0o600)
        print(f"renewed licence -> {out}")
    else:
        print(renewed)
    print(f"  subscription {after.subscription_id}", file=sys.stderr)
    print(f"  term         {before.term_index} -> {after.term_index} "
          f"({after.term_label})", file=sys.stderr)
    print(f"  expiry       {before.expires_at} -> {after.expires_at}",
          file=sys.stderr)
    if after.expires_at and before.expires_at and \
            after.expires_at <= before.expires_at:
        print("  WARNING: the new expiry is not later than the old one.",
              file=sys.stderr)
    return 0


def cmd_inspect(args) -> int:
    document = Path(args.licence).read_text(encoding="utf-8")
    pub = _resolve_pubkey(args.pubkey)
    if not pub:
        licence, _, _ = parse(document)
        print("NO PUBLIC KEY GIVEN -- contents shown UNVERIFIED:\n")
        print(json.dumps(licence.to_dict(), indent=2, ensure_ascii=False))
        return 2
    try:
        licence = verify(document, pub, check_machine=args.check_machine)
    except Exception as exc:  # noqa: BLE001
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print("VALID")
    print(json.dumps(licence.to_dict(), indent=2, ensure_ascii=False))
    remaining = licence.days_remaining()
    if remaining is not None:
        print(f"\n{remaining:.1f} days remaining")
    return 0


def cmd_manifest(args) -> int:
    pem = Path(args.key).read_text(encoding="utf-8")
    text = build_manifest(ROOT, pem, version=args.version)
    out = Path(args.out or (ROOT / "MANIFEST.sig"))
    out.write_text(text, encoding="utf-8")
    print(f"manifest -> {out}  ({len(PROTECTED_PATHS)} protected files)")
    return 0


def cmd_check_manifest(args) -> int:
    pub = _resolve_pubkey(args.pubkey)
    if not pub:
        print("a public key is required", file=sys.stderr)
        return 1
    manifest = Path(args.manifest or (ROOT / "MANIFEST.sig"))
    if not manifest.exists():
        print(f"no manifest at {manifest}", file=sys.stderr)
        return 1
    report = verify_manifest(ROOT, manifest.read_text(encoding="utf-8"), pub)
    print(report.summary())
    for path in report.modified:
        print(f"  MODIFIED {path}")
    for path in report.missing:
        print(f"  MISSING  {path}")
    return 0 if report.ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sentinel-FX licence generator (vendor side)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("keygen", help="generate the vendor keypair (once, offline)")
    p.add_argument("--out", default="./vendor-keys")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing key -- invalidates every issued licence")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser("fingerprint", help="print this machine's identifiers (customer side)")
    p.add_argument("--out")
    p.add_argument("--verbose", action="store_true",
                   help="also show the raw values that are hashed")
    p.set_defaults(func=cmd_fingerprint)

    p = sub.add_parser("issue", help="sign a licence")
    p.add_argument("--key", required=True, help="path to private.pem")
    p.add_argument("--to", required=True, help="licensee name")
    p.add_argument("--tier", required=True, choices=sorted(TIER_CAPABILITIES))
    p.add_argument("--months", type=int, default=DEFAULT_TERM_MONTHS,
                   help="term length in CALENDAR months (default 3 = one "
                        "quarter). Calendar months, not 90 days: four 90-day "
                        "quarters are 360 days and the renewal date drifts.")
    p.add_argument("--days", type=int, default=0,
                   help="an exact number of days instead of a term. Only for "
                        "an agreement a month count cannot express.")
    p.add_argument("--perpetual", action="store_true", help="never expires")
    p.add_argument("--machine-file", help="the customer's fingerprint JSON")
    p.add_argument("--this-machine", action="store_true",
                   help="bind to the machine running this command")
    p.add_argument("--unbound", action="store_true",
                   help="issue a site licence that runs on ANY machine")
    p.add_argument("--capability", action="append",
                   help="override, e.g. --capability max_instruments=12")
    p.add_argument("--notes", default="")
    p.add_argument("--activation-url", help="licence server to check in with")
    p.add_argument("--activation-interval", type=int, default=0,
                   help="hours between check-ins")
    p.add_argument("--out")
    p.set_defaults(func=cmd_issue)

    p = sub.add_parser("inspect", help="verify and print a licence")
    p.add_argument("--licence", required=True)
    p.add_argument("--pubkey", help="path to public.txt, or the base64 key itself")
    p.add_argument("--check-machine", action="store_true",
                   help="also require the machine binding to match HERE")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("renew", help="extend an existing licence by one term")
    p.set_defaults(func=cmd_renew)
    p.add_argument("--key", required=True, help="path to private.pem")
    p.add_argument("--pubkey", required=True,
                   help="path to public.txt, or the base64 key itself")
    p.add_argument("--licence", required=True, help="the licence being renewed")
    p.add_argument("--months", type=int, default=None,
                   help="term length; defaults to the same term as before")
    p.add_argument("--tier", choices=sorted(TIER_CAPABILITIES),
                   help="change the tier at renewal (upgrade or downgrade)")
    p.add_argument("--machine-file", help="re-bind to a different machine")
    p.add_argument("--this-machine", action="store_true")
    p.add_argument("--late-grace", type=int, default=14,
                   help="renewing within this many days of expiry continues "
                        "from the old expiry; later than that, the new term "
                        "starts today")
    p.add_argument("--out")

    p = sub.add_parser("manifest", help="sign the release integrity manifest")
    p.add_argument("--key", required=True)
    p.add_argument("--version", default="")
    p.add_argument("--out")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("check-manifest", help="verify this install against a manifest")
    p.add_argument("--manifest")
    p.add_argument("--pubkey", help="path to public.txt, or the base64 key itself")
    p.set_defaults(func=cmd_check_manifest)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
