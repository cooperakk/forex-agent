"""Signed licences.

A licence is a small JSON document plus an Ed25519 signature over its canonical
serialisation. The vendor holds the private key; the software embeds only the
public key, so reading this file -- or the whole repository -- does not help
anyone produce a licence that will verify.

The format is deliberately readable. A customer can open their licence and see
exactly what they were granted and until when; there is nothing to hide, and an
opaque blob invites the suspicion that it phones home or carries more than it
says.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

#: 2 added the quarterly-term fields. Format 1 licences still verify and still
#: work -- the reader accepts anything up to this number and fills the new
#: fields from their defaults, so an existing customer is not broken by an
#: upgrade they did not ask for.
LICENSE_FORMAT = 2

#: The terms actually sold, in months. Quarterly is the default because it is
#: what the product is priced on; the others exist so a discount for paying
#: ahead does not require a bespoke licence type.
STANDARD_TERMS: Dict[int, str] = {
    1: "یک‌ماهه (آزمایشی)",
    3: "سه‌ماهه",
    6: "شش‌ماهه",
    12: "یک‌ساله",
}
DEFAULT_TERM_MONTHS = 3

#: Distinguishes "the caller did not mention valid_days" from "the caller
#: passed None", which is the historical spelling of "perpetual".
_UNSET: Any = object()
_ENVELOPE_RE = re.compile(
    r"-----BEGIN SENTINEL LICENCE-----\s*(.+?)\s*-----END SENTINEL LICENCE-----",
    re.DOTALL)


class LicenseError(RuntimeError):
    """Base class. Never raised directly."""


class LicenseInvalid(LicenseError):
    """Malformed, or the signature does not verify."""


class LicenseExpired(LicenseError):
    """Valid signature, but outside its validity window."""


class LicenseMachineMismatch(LicenseError):
    """Valid signature, but issued for a different machine."""


class LicenseTier(str, Enum):
    """What a licence permits.

    The tiers gate CAPABILITY, not quality: every tier gets the same risk
    engine, the same audit chain and the same acceptance protocol, because
    shipping a "cheaper" version with weaker safety would be indefensible.
    """

    EVALUATION = "evaluation"   # paper only, time limited
    RESEARCH = "research"       # paper + full research lab, no live venue
    LIVE_SINGLE = "live_single"  # one live account
    LIVE_MULTI = "live_multi"   # several live accounts
    UNLIMITED = "unlimited"     # no capability limits


#: Capability matrix. `max_*` of None means "no limit".
TIER_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    LicenseTier.EVALUATION.value: {
        "live_trading": False, "max_instruments": 3, "max_accounts": 1,
        "max_equity": "25000", "research_lab": False, "llm_news": False,
    },
    LicenseTier.RESEARCH.value: {
        "live_trading": False, "max_instruments": None, "max_accounts": 1,
        "max_equity": None, "research_lab": True, "llm_news": True,
    },
    LicenseTier.LIVE_SINGLE.value: {
        "live_trading": True, "max_instruments": 8, "max_accounts": 1,
        "max_equity": None, "research_lab": True, "llm_news": True,
    },
    LicenseTier.LIVE_MULTI.value: {
        "live_trading": True, "max_instruments": None, "max_accounts": 5,
        "max_equity": None, "research_lab": True, "llm_news": True,
    },
    LicenseTier.UNLIMITED.value: {
        "live_trading": True, "max_instruments": None, "max_accounts": None,
        "max_equity": None, "research_lab": True, "llm_news": True,
    },
}


@dataclass
class License:
    """The signed grant.

    ``machine`` binds the licence to one installation. An EMPTY machine map is
    an explicitly unbound licence: it runs anywhere, which is correct for a
    site licence and wrong for everything else, so `issue` requires the caller
    to say so deliberately.
    """

    licence_id: str
    issued_to: str
    issued_at: str                      # RFC3339 UTC
    expires_at: Optional[str]           # RFC3339 UTC; None = perpetual
    tier: str
    machine: Dict[str, str] = field(default_factory=dict)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    format: int = LICENSE_FORMAT
    # Set by the vendor when the deployment must check in with a licence
    # server. This is the only control in the whole module that a customer
    # with root cannot simply delete, because the answer comes from elsewhere.
    activation_url: Optional[str] = None
    activation_interval_hours: int = 0

    #: Length of THIS term, in calendar months. 0 means a perpetual licence or
    #: one issued before terms existed.
    term_months: int = 0
    #: Which term of the subscription this is: 1 for the first, 2 for the first
    #: renewal, and so on. Purely informational, and worth carrying because
    #: "this customer is on their fourth quarter" is the question a support
    #: conversation actually opens with.
    term_index: int = 1
    #: Stable across renewals, so every term of one subscription can be traced
    #: to the same customer relationship even though each is a separate file.
    subscription_id: str = ""
    #: Day of the month the SUBSCRIPTION started on. Carried because month
    #: arithmetic clamps and clamping is lossy: chaining quarters from 31 March
    #: gives 30 June, 30 September, 30 December, 30 March -- one day lost per
    #: year, for ever, and a renewal date that no longer matches the invoice.
    #: Re-anchoring each term to this day fixes it. 0 means "not anchored",
    #: which is what a format-1 licence has.
    anchor_day: int = 0

    # -- derived ------------------------------------------------------------ #

    @property
    def expiry(self) -> Optional[datetime]:
        return _parse_ts(self.expires_at) if self.expires_at else None

    @property
    def issued(self) -> datetime:
        return _parse_ts(self.issued_at)

    def capability(self, name: str, default: Any = None) -> Any:
        """A capability, with the tier's value as the fallback.

        Explicit per-licence capabilities win, so a customer can be granted an
        exception without inventing a new tier.
        """
        if name in self.capabilities:
            return self.capabilities[name]
        return TIER_CAPABILITIES.get(self.tier, {}).get(name, default)

    def days_remaining(self, *, now: Optional[datetime] = None) -> Optional[float]:
        if self.expiry is None:
            return None
        current = now or datetime.now(timezone.utc)
        return (self.expiry - current).total_seconds() / 86400.0

    @property
    def term_label(self) -> str:
        """The term in words, for a dashboard that must not say "90 days"."""
        if self.expires_at is None:
            return "دائمی"
        if self.term_months in STANDARD_TERMS:
            return STANDARD_TERMS[self.term_months]
        if self.term_months:
            return f"{self.term_months} ماهه"
        return "مدت‌دار"

    def renewal_stage(self, *, now: Optional[datetime] = None) -> str:
        """Where this licence sits on the renewal ladder.

        Five named stages rather than a raw day count, because the action a
        customer should take is different at each one and a number leaves them
        to work that out at the worst moment.
        """
        remaining = self.days_remaining(now=now)
        if remaining is None:
            return "perpetual"
        if remaining < 0:
            return "expired"
        if remaining <= 3:
            return "critical"
        if remaining <= 14:
            return "due"
        if remaining <= 30:
            return "approaching"
        return "healthy"

    def payload(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items()}

    def to_dict(self) -> Dict[str, Any]:
        out = self.payload()
        out["effective_capabilities"] = {
            k: self.capability(k) for k in
            ("live_trading", "max_instruments", "max_accounts", "max_equity",
             "research_lab", "llm_news")
        }
        out["term_label"] = self.term_label
        out["renewal_stage"] = self.renewal_stage()
        return out


def add_months(when: datetime, months: int) -> datetime:
    """Advance by CALENDAR months, clamping the day to the target month.

    Not 90 days. A quarter is three calendar months, and the difference is not
    pedantry:

    * four 90-day quarters are 360 days, so a customer renewing quarterly
      gains five free days a year and their renewal date walks backwards
      through the calendar until it no longer matches the invoice;
    * 31 January plus three months is 30 April, and a naive day-of-month copy
      raises ValueError on a date that must not be allowed to fail inside a
      signing routine.

    Clamping down is the only safe direction: 31 January + 1 month = 28 or 29
    February, never 1 or 2 March, because a licence must never silently gain a
    day it was not sold.
    """
    if months < 0:
        raise ValueError("months must be non-negative")
    total = when.month - 1 + months
    year = when.year + total // 12
    month = total % 12 + 1
    import calendar
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


def term_end(begins: datetime, months: int, anchor_day: int = 0) -> datetime:
    """The end of a term of ``months`` calendar months starting at ``begins``.

    ``anchor_day`` is the day of the month the SUBSCRIPTION began on. Without
    it, chained terms ratchet downwards and never recover, because February
    clamps every quarter that passes through it:

        31 Mar -> 30 Jun -> 30 Sep -> 30 Dec -> 30 Mar   (a day lost, for ever)

    With it, each term re-reaches for the original day when the month is long
    enough:

        31 Mar -> 30 Jun -> 30 Sep -> 31 Dec -> 31 Mar   (stable)

    The clamp still applies, so a 31st anchor lands on the 28th in February and
    returns to the 31st in March. It never overshoots into the next month.
    """
    end = add_months(begins, months)
    if anchor_day and anchor_day > end.day:
        import calendar
        end = end.replace(day=min(anchor_day, calendar.monthrange(end.year,
                                                                 end.month)[1]))
    return end


def _parse_ts(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _canonical(payload: Dict[str, Any]) -> bytes:
    """Byte-exact serialisation for signing.

    sort_keys and tight separators so that a re-serialised payload produces the
    same bytes on any platform and any Python version. A signature over a
    non-canonical form is a signature that fails for reasons nobody can debug.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- #
# keys
# --------------------------------------------------------------------------- #


def generate_keypair() -> tuple:
    """(private_pem, public_b64). Run ONCE, by the vendor, offline.

    The private key must never reach a customer's machine, a repository, a CI
    secret store that customers can read, or a backup that leaves the vendor's
    control. If it does, every licence ever issued becomes forgeable and the
    only remedy is a new key and a re-issue of every licence in the field.
    """
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return pem, base64.b64encode(public_raw).decode("ascii")


def load_private_key(pem: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise LicenseInvalid("not an Ed25519 private key")
    return key


def load_public_key(b64: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(b64.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise LicenseInvalid(f"public key is not valid base64: {exc}") from exc
    if len(raw) != 32:
        raise LicenseInvalid(f"an Ed25519 public key is 32 bytes, got {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw)


# --------------------------------------------------------------------------- #
# issue / parse / verify
# --------------------------------------------------------------------------- #


def issue(*, private_key_pem: str, issued_to: str, tier: str,
          term_months: Optional[int] = _UNSET,  # type: ignore[assignment]
          valid_days: Optional[int] = _UNSET,  # type: ignore[assignment]
          machine: Optional[Dict[str, str]] = None,
          capabilities: Optional[Dict[str, Any]] = None,
          notes: str = "",
          activation_url: Optional[str] = None,
          activation_interval_hours: int = 0,
          allow_unbound: bool = False,
          perpetual: bool = False,
          subscription_id: str = "",
          term_index: int = 1,
          anchor_day: int = 0,
          starts_at: Optional[datetime] = None,
          now: Optional[datetime] = None) -> str:
    """Produce a signed licence document. Vendor side only.

    ``allow_unbound`` must be passed explicitly to issue a licence with no
    machine binding: an unbound licence runs on every machine forever, which is
    occasionally what a site licence means and is otherwise a mistake that is
    invisible until it has been copied.
    """
    if tier not in TIER_CAPABILITIES:
        raise LicenseInvalid(
            f"unknown tier {tier!r}; expected one of {sorted(TIER_CAPABILITIES)}")
    if not issued_to.strip():
        raise LicenseInvalid("issued_to must name the licensee")
    if not machine and not allow_unbound:
        raise LicenseInvalid(
            "refusing to issue an unbound licence: it would run on any machine, "
            "for ever. Pass the customer's machine fingerprint, or set "
            "allow_unbound=True if a site licence is genuinely intended.")

    current = now or datetime.now(timezone.utc)
    # `starts_at` is what makes a RENEWAL continuous: the new term begins where
    # the old one ended, so a customer who renews four days early does not pay
    # for four days twice.
    begins = starts_at or current

    # `valid_days=None` has meant "perpetual" since the first release, and a
    # vendor's issuing script that says so must not quietly start producing
    # three-month licences after an upgrade. A sentinel distinguishes "not
    # passed" from "passed as None", so both spellings keep working and neither
    # is ambiguous.
    # Sentinels on BOTH parameters, so "not mentioned" is distinguishable from
    # every value a caller can pass. Without that, `term_months=0` read as
    # "use the default" and silently sold a quarter against an invoice for
    # nothing, and `perpetual=True, term_months=12` silently issued a
    # never-expiring licence with no complaint.
    days_given = valid_days is not _UNSET
    months_given = term_months is not _UNSET
    if perpetual and (months_given or (days_given and valid_days is not None)):
        raise LicenseInvalid(
            "a perpetual licence has no term; pass perpetual=True alone.")
    if days_given and months_given and valid_days is not None:
        raise LicenseInvalid(
            "pass either term_months or valid_days, not both: two sources of "
            "truth for an expiry date is how a licence ends up disagreeing "
            "with the invoice.")
    if anchor_day and not 1 <= int(anchor_day) <= 31:
        raise LicenseInvalid(
            f"anchor_day must be a day of the month (1-31), got {anchor_day}. "
            "It is signed into the licence and pins every future renewal.")

    if perpetual or (days_given and valid_days is None):
        expires, months = None, 0
    elif days_given:
        # Kept for the one case a term cannot express: a specific number of
        # days agreed with a customer.
        if valid_days <= 0:
            raise LicenseInvalid("valid_days must be positive")
        expires = _rfc3339(begins.timestamp() + valid_days * 86400)
        months = 0
    else:
        months = int(DEFAULT_TERM_MONTHS if not months_given or term_months is None
                     else term_months)
        if months <= 0:
            raise LicenseInvalid(
                "term_months must be positive. For a licence that never expires "
                "pass perpetual=True, which has to be said out loud.")
        if months > 60:
            raise LicenseInvalid(
                "terms longer than five years are refused: the machine this is "
                "bound to will not exist, and nobody will remember the terms.")
        anchor = anchor_day or begins.day
        expires = _rfc3339(term_end(begins, months, anchor).timestamp())

    # M9: a back-dated `starts_at` could produce a licence that expired before
    # it was issued -- signed, parseable, and dead on arrival. The vendor finds
    # out when the customer calls.
    if expires is not None and _parse_ts(expires) <= current:
        raise LicenseInvalid(
            f"this licence would expire at {expires}, which is not after the "
            f"moment it is being issued ({_rfc3339(current.timestamp())}). "
            "Check starts_at and the term length.")

    licence_id = _licence_id(issued_to, current)
    payload = License(
        licence_id=licence_id,
        issued_to=issued_to.strip(),
        issued_at=_rfc3339(current.timestamp()),
        expires_at=expires,
        tier=tier,
        machine=dict(machine or {}),
        capabilities=dict(capabilities or {}),
        notes=notes.strip(),
        activation_url=activation_url,
        activation_interval_hours=int(activation_interval_hours),
        term_months=months,
        term_index=max(1, int(term_index)),
        subscription_id=(subscription_id or licence_id).strip(),
        anchor_day=(anchor_day or (begins.day if expires else 0)),
    ).payload()

    key = load_private_key(private_key_pem)
    signature = key.sign(_canonical(payload))
    document = {
        "payload": payload,
        "signature": base64.b64encode(signature).decode("ascii"),
        "algorithm": "Ed25519",
    }
    body = base64.b64encode(
        json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    wrapped = "\n".join(body[i:i + 72] for i in range(0, len(body), 72))
    return (f"-----BEGIN SENTINEL LICENCE-----\n{wrapped}\n"
            f"-----END SENTINEL LICENCE-----\n")


def _licence_id(issued_to: str, when: datetime) -> str:
    # A random component. Derived from (customer, second) alone, two licences
    # issued to one customer in the same second -- a batch for several
    # machines -- shared an id, and the anti-rollback guard, which remembers
    # ids it has watched expire, could then condemn the wrong one.
    import secrets as _secrets
    seed = f"{issued_to}|{when.isoformat()}|{_secrets.token_hex(8)}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()[:16].upper()
    return "-".join(digest[i:i + 4] for i in range(0, 16, 4))


def _rfc3339(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def parse(document: str) -> tuple:
    """(License, signature_bytes, canonical_payload_bytes). No verification."""
    match = _ENVELOPE_RE.search(document)
    if not match:
        raise LicenseInvalid(
            "this does not look like a licence file: the BEGIN/END markers are "
            "missing. Paste the whole file, including both marker lines.")
    try:
        raw = base64.b64decode("".join(match.group(1).split()), validate=True)
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise LicenseInvalid(f"licence body is corrupt: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LicenseInvalid("licence body is not a JSON object")
    if parsed.get("algorithm") != "Ed25519":
        raise LicenseInvalid(
            f"unsupported signature algorithm {parsed.get('algorithm')!r}")
    payload = parsed.get("payload")
    if not isinstance(payload, dict):
        raise LicenseInvalid("licence has no payload")
    # Everything below runs BEFORE the signature is checked, on input anyone
    # can craft. It must fail as LicenseInvalid -- never as a bare ValueError
    # or TypeError, which escaped every caller's `except LicenseError` and
    # turned a pasted typo into an HTTP 500 from the licence endpoint.
    try:
        fmt = int(payload.get("format", 0))
    except (TypeError, ValueError) as exc:
        raise LicenseInvalid(f"licence format is not a number: {exc}") from exc
    if fmt > LICENSE_FORMAT:
        raise LicenseInvalid(
            f"this licence is format {payload.get('format')}, and this build "
            f"understands up to {LICENSE_FORMAT}. Upgrade the software.")
    try:
        signature = base64.b64decode(parsed["signature"], validate=True)
    except Exception as exc:  # noqa: BLE001
        raise LicenseInvalid(f"signature is not valid base64: {exc}") from exc

    known = {f for f in License.__dataclass_fields__}
    try:
        licence = License(**{k: v for k, v in payload.items() if k in known})
        _check_types(licence)
    except LicenseInvalid:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LicenseInvalid(f"licence payload is malformed: {exc}") from exc
    return licence, signature, _canonical(payload)


def _check_types(licence: "License") -> None:
    """Refuse a payload whose fields would crash a later reader."""
    for name in ("licence_id", "issued_to", "issued_at", "tier"):
        if not isinstance(getattr(licence, name), str):
            raise LicenseInvalid(f"licence field {name!r} must be text")
    if licence.expires_at is not None and not isinstance(licence.expires_at, str):
        raise LicenseInvalid("licence field 'expires_at' must be text or null")
    if not isinstance(licence.machine, dict) or not isinstance(licence.capabilities, dict):
        raise LicenseInvalid("licence 'machine' and 'capabilities' must be objects")
    for name in ("activation_interval_hours", "term_months", "term_index", "anchor_day"):
        if not isinstance(getattr(licence, name), int):
            raise LicenseInvalid(f"licence field {name!r} must be an integer")
    _parse_ts(licence.issued_at)
    if licence.expires_at:
        _parse_ts(licence.expires_at)


def verify(document: str, public_key_b64: str, *,
           check_machine: bool = True,
           grace_days: int = 0,
           now: Optional[datetime] = None,
           current_fingerprint: Optional[Dict[str, str]] = None) -> License:
    """Parse, check the signature, the window and the machine binding.

    Raises a specific subclass so the caller can distinguish "expired" (renew
    it) from "wrong machine" (re-issue it) from "forged" (something is wrong).
    """
    licence, signature, canonical = parse(document)

    try:
        load_public_key(public_key_b64).verify(signature, canonical)
    except InvalidSignature as exc:
        raise LicenseInvalid(
            "the licence signature does not verify. Either the file was edited "
            "after it was issued, or it was not issued by this vendor."
        ) from exc

    current = now or datetime.now(timezone.utc)
    if licence.issued > current + _CLOCK_SLACK:
        raise LicenseInvalid(
            f"this licence is dated {licence.issued_at}, which is in the future. "
            "Check this machine's clock.")
    if licence.expiry is not None:
        deadline = licence.expiry.timestamp() + grace_days * 86400
        if current.timestamp() > deadline:
            raise LicenseExpired(
                f"the licence expired on {licence.expires_at}"
                + (f" and the {grace_days}-day grace period has also passed"
                   if grace_days else ""))

    if check_machine and licence.machine:
        from .fingerprint import fingerprint_matches
        ok, matched, detail = fingerprint_matches(
            licence.machine, current_fingerprint)
        if not ok:
            changed = sorted(k for k, good in detail.items() if not good)
            raise LicenseMachineMismatch(
                f"this licence is issued for a different machine "
                f"({matched} of {len(licence.machine)} identifiers match; "
                f"these differ: {', '.join(changed) or 'all'}). If you moved the "
                "installation or replaced hardware, ask for a re-issue.")
    return licence


# A licence dated slightly in the future is a clock skew, not a forgery.
from datetime import timedelta  # noqa: E402
_CLOCK_SLACK = timedelta(hours=24)


def renew(*, document: str, private_key_pem: str, public_key_b64: str,
          term_months: Optional[int] = None,
          machine: Optional[Dict[str, str]] = None,
          tier: Optional[str] = None,
          capabilities: Optional[Dict[str, Any]] = None,
          notes: Optional[str] = None,
          late_renewal_grace_days: int = 14,
          now: Optional[datetime] = None) -> str:
    """Extend an existing subscription by another term.

    Two rules, and both exist because the naive version costs somebody money:

    **Renewing early does not throw away the remaining days.** The new term
    starts where the old one ended, not today. Without this, a customer who
    renews a week ahead of expiry -- exactly the behaviour a vendor wants to
    encourage -- pays for that week twice.

    **Renewing long after expiry does not back-date the term.** If the licence
    lapsed more than ``late_renewal_grace_days`` ago, the new term starts today
    instead. Otherwise a customer returning after six months would receive a
    licence that expired three months ago, which is not a renewal but a
    puzzle. Inside the grace window the old expiry still wins, so an invoice
    settled a few days late is continuous.

    The signature on the OLD document is verified first, with the machine check
    switched off: renewal happens on the vendor's machine, which is by
    definition not the customer's. Skipping the check entirely would let a
    forged document be laundered into a genuine one by renewing it.
    """
    current = now or datetime.now(timezone.utc)
    previous = verify(document, public_key_b64, check_machine=False,
                      grace_days=10 ** 6, now=current)

    if previous.expires_at is None:
        raise LicenseInvalid(
            "this licence is perpetual, so there is nothing to renew.")

    old_expiry = previous.expiry
    assert old_expiry is not None
    lapsed_days = (current - old_expiry).total_seconds() / 86400.0
    starts_at = old_expiry if lapsed_days <= late_renewal_grace_days else current

    # A licence sold as a number of DAYS stores term_months=0. Falling back to
    # the default quarter there renewed a 30-day customer for 90 days against a
    # 30-day invoice, and relabelled their licence "سه‌ماهه" on the dashboard.
    # Carry the original shape forward instead.
    day_term: Optional[int] = None
    if term_months is not None:
        months = term_months
    elif previous.term_months:
        months = previous.term_months
    else:
        months = None
        day_term = max(1, round(
            (old_expiry - previous.issued).total_seconds() / 86400.0))

    if day_term is not None:
        return issue(
            private_key_pem=private_key_pem,
            issued_to=previous.issued_to,
            tier=tier or previous.tier,
            valid_days=day_term,
            machine=dict(machine if machine is not None else previous.machine),
            capabilities=dict(capabilities if capabilities is not None
                              else previous.capabilities),
            notes=previous.notes if notes is None else notes,
            activation_url=previous.activation_url,
            activation_interval_hours=previous.activation_interval_hours,
            allow_unbound=not (machine if machine is not None else previous.machine),
            subscription_id=previous.subscription_id or previous.licence_id,
            term_index=previous.term_index + 1,
            starts_at=starts_at,
            now=current,
        )
    return issue(
        private_key_pem=private_key_pem,
        issued_to=previous.issued_to,
        tier=tier or previous.tier,
        term_months=months,
        machine=dict(machine if machine is not None else previous.machine),
        capabilities=dict(capabilities if capabilities is not None
                          else previous.capabilities),
        notes=previous.notes if notes is None else notes,
        activation_url=previous.activation_url,
        activation_interval_hours=previous.activation_interval_hours,
        allow_unbound=not (machine if machine is not None else previous.machine),
        subscription_id=previous.subscription_id or previous.licence_id,
        term_index=previous.term_index + 1,
        # A format-1 licence has no anchor; fall back to the day the ORIGINAL
        # term ended, which is the closest thing to a billing day it carries.
        anchor_day=previous.anchor_day or old_expiry.day,
        starts_at=starts_at,
        now=current,
    )


def read_license_file(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise LicenseInvalid(f"cannot read the licence file {path}: {exc}") from exc
