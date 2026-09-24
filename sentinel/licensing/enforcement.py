"""The runtime gate.

Design rule, and the reason this module is short: **a licence problem must
never make the system dangerous.** Every refusal here reduces what the software
will do; none of them makes it abandon a position, widen a stop, or stop
managing a book that is already open.

Concretely, when a licence is missing, expired or bound elsewhere:

* new entries stop;
* the dashboard keeps working, read-only, and says exactly what is wrong;
* **existing positions keep being managed** -- stops, trails, the give-back
  ratchet, the weekend flatten. Refusing to protect an open trade because an
  invoice is unpaid would be indefensible, and a vendor who does it will one
  day be explaining a loss they caused.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .activation import ActivationClient, LeaseStatus
from .clock_guard import ClockGuard, GuardReport
from .integrity import IntegrityReport, verify_manifest
from .license import (
    License,
    LicenseError,
    LicenseExpired,
    LicenseInvalid,
    LicenseMachineMismatch,
    parse,
    read_license_file,
    verify,
)

from . import vendor_key as _vendor_key


def resolve_vendor_key(explicit: Optional[str] = None) -> str:
    """The key licences are verified against. See vendor_key.py for why.

    An EMBEDDED key always beats the environment. Reading the environment
    first let a licensee unset the variable (licensing off) or point it at a
    key they generated (self-signed unlimited licence).
    """
    if explicit:
        return explicit
    embedded = (_vendor_key.EMBEDDED_PUBLIC_KEY or "").strip()
    if embedded:
        return embedded
    return os.environ.get("SENTINEL_LICENSE_PUBKEY", "").strip()


def is_distributed_build() -> bool:
    """True when this build carries a vendor key of its own."""
    return bool((_vendor_key.EMBEDDED_PUBLIC_KEY or "").strip())


#: Kept for callers that read it; resolve_vendor_key() is the authority.
VENDOR_PUBLIC_KEY = resolve_vendor_key()

#: Days a licence keeps working past its expiry. Not generosity: a renewal that
#: arrives late must not flatten a live book at 3am.
DEFAULT_GRACE_DAYS = 14

#: Warn this far ahead, so a renewal is never a surprise. On a three-month
#: term this is a third of the licence, which is deliberate: a customer on a
#: quarterly cycle should meet the renewal notice once per term, calmly, not
#: three days before their agent stops taking entries.
RENEWAL_WARNING_DAYS = 30

#: What the operator is told at each stage, in plain Persian. The escalation is
#: in the WORDING, not only in a number, because "۲۹ روز" and "۲ روز" look
#: equally unalarming in a table.
RENEWAL_MESSAGES = {
    "approaching": ("تمدید نزدیک است", "لایسنس شما {days} روز دیگر تمام می‌شود. "
                    "الان وقت خوبی برای تمدید است — هیچ عجله‌ای نیست."),
    "due": ("وقت تمدید رسیده", "لایسنس شما {days} روز دیگر تمام می‌شود. "
            "اگر تمدید نشود، ربات معاملهٔ تازه باز نمی‌کند."),
    "critical": ("تمدید فوری", "فقط {days} روز تا پایان لایسنس مانده است. "
                 "بعد از آن، معامله‌های باز همچنان مدیریت می‌شوند ولی معاملهٔ "
                 "تازه‌ای باز نمی‌شود."),
    "expired": ("لایسنس تمام شده", "لایسنس {days} روز پیش تمام شد و الان در "
                "مهلت ارفاقی {grace} روزه است. با پایان این مهلت، ورود به "
                "معاملهٔ تازه متوقف می‌شود — ولی معامله‌های باز رها نمی‌شوند."),
}


def _software_version() -> str:
    try:
        from importlib.metadata import version
        return version("sentinel-fx")
    except Exception:  # noqa: BLE001 - a source checkout has no metadata
        return ""


@dataclass
class LicenseStatus:
    valid: bool
    reason: str = ""
    licence: Optional[License] = None
    integrity: Optional[IntegrityReport] = None
    warnings: List[str] = field(default_factory=list)
    in_grace: bool = False
    days_remaining: Optional[float] = None
    unlicensed_mode: bool = False
    clock: Optional[GuardReport] = None
    #: The online activation lease, when the licence requires one.
    activation: Optional[LeaseStatus] = None
    #: True when this build embeds its own vendor key (see vendor_key.py).
    distributed: bool = False
    #: healthy | approaching | due | critical | expired | perpetual | none
    stage: str = "none"
    #: The headline and the sentence under it, already in plain Persian.
    headline: str = ""
    advice: str = ""

    def to_dict(self) -> Dict[str, Any]:
        lic = self.licence
        return {
            "valid": self.valid,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "in_grace": self.in_grace,
            "days_remaining": (round(self.days_remaining, 1)
                               if self.days_remaining is not None else None),
            "unlicensed_mode": self.unlicensed_mode,
            "tier": lic.tier if lic else None,
            "issued_to": lic.issued_to if lic else None,
            "licence_id": lic.licence_id if lic else None,
            "expires_at": lic.expires_at if lic else None,
            "issued_at": lic.issued_at if lic else None,
            "term_months": lic.term_months if lic else None,
            "term_label": lic.term_label if lic else None,
            "term_index": lic.term_index if lic else None,
            "subscription_id": lic.subscription_id if lic else None,
            "machine_bound": bool(lic.machine) if lic else None,
            "effective_capabilities": (lic.to_dict().get("effective_capabilities")
                                       if lic else None),
            "stage": self.stage,
            "headline": self.headline,
            "advice": self.advice,
            "clock": self.clock.to_dict() if self.clock else None,
            "integrity": self.integrity.to_dict() if self.integrity else None,
            "activation": self.activation.to_dict() if self.activation else None,
            "distributed_build": self.distributed,
        }


class LicenseGate:
    """Reads the licence, and answers "may I?" questions about it."""

    def __init__(self, *, licence_path: Optional[str] = None,
                 manifest_path: Optional[str] = None,
                 root: Optional[str] = None,
                 public_key: Optional[str] = None,
                 grace_days: int = DEFAULT_GRACE_DAYS,
                 fingerprint: Optional[Dict[str, str]] = None,
                 guard_path: Optional[str] = None,
                 enable_clock_guard: bool = True,
                 require_manifest: Optional[bool] = None,
                 activation_transport=None,
                 lease_public_key: Optional[str] = None) -> None:
        # `fingerprint` overrides what this machine reports. Used by the tests,
        # and by `licensegen inspect` to check a licence against a machine other
        # than the one running the command.
        self._fingerprint = fingerprint
        self.licence_path = Path(
            licence_path or os.environ.get("SENTINEL_LICENSE", "var/licence.key"))
        self.root = Path(root or Path(__file__).resolve().parents[2])
        self.manifest_path = Path(manifest_path or (self.root / "MANIFEST.sig"))
        self.public_key = resolve_vendor_key(public_key)
        #: A distributed build must carry its signed manifest. Treating a
        #: MISSING manifest as "unsigned, fine" let `rm MANIFEST.sig` switch
        #: the integrity check off for live trading, which the docstring of
        #: integrity.verify_manifest says requires a verified installation.
        self.require_manifest = (is_distributed_build() if require_manifest is None
                                 else bool(require_manifest))
        self.grace_days = grace_days
        lease_key = (lease_public_key
                     or (_vendor_key.EMBEDDED_LEASE_PUBLIC_KEY or "").strip()
                     or self.public_key)
        self.activation = ActivationClient(
            self.licence_path.parent / "licence-lease.json",
            public_key_b64=lease_key, transport=activation_transport,
            software_version=_software_version())
        self._status: Optional[LicenseStatus] = None
        self._checked_monotonic: float = 0.0
        #: How long a verdict may be reused. Without a TTL the gate was
        #: evaluated exactly ONCE per process: a three-month licence on a
        #: server that stays up keeps authorising live entries months past its
        #: expiry, and the dashboard's countdown freezes at whatever it said
        #: the day the service started.
        self.recheck_seconds: float = 900.0
        self.guard: Optional[ClockGuard] = None
        if enable_clock_guard:
            self.guard = ClockGuard(
                guard_path or (self.licence_path.parent / "licence-timing.json"),
                public_key_b64=self.public_key, fingerprint=fingerprint)

    # -- evaluation --------------------------------------------------------- #

    def check(self, *, now: Optional[datetime] = None,
              force: bool = False) -> LicenseStatus:
        import time as _time
        fresh = (self._status is not None
                 and (_time.monotonic() - self._checked_monotonic) < self.recheck_seconds)
        # An explicit `now` is a caller asking about a SPECIFIC instant (the
        # tests, and `licensegen inspect`); a cached answer for a different
        # instant would be wrong, so it always re-evaluates.
        if fresh and not force and now is None:
            return self._status  # type: ignore[return-value]
        self._status = self._evaluate(now or datetime.now(timezone.utc))
        self._checked_monotonic = _time.monotonic()
        return self._status

    def _evaluate(self, now: datetime) -> LicenseStatus:
        # No vendor key compiled in: this is a self-hosted build where the
        # customer is the vendor. Licensing is inert, and says so rather than
        # pretending to enforce something.
        if not self.public_key:
            return LicenseStatus(
                valid=True, unlicensed_mode=True,
                reason="no vendor public key is configured, so licensing is not "
                       "enforced in this build",
                warnings=["This build has no licence enforcement. That is correct "
                          "for a self-hosted install and wrong for a distributed "
                          "one."])

        if not self.licence_path.exists():
            # Still run the guard: an installation whose licence file has just
            # been deleted is exactly the case where the timing record matters,
            # and skipping it here would let "delete, change clock, restore"
            # wash the history clean.
            clock = self._run_guard(now, "", False)
            return LicenseStatus(
                valid=False, clock=clock, stage="none",
                headline="لایسنسی پیدا نشد",
                advice="فایل لایسنس در مسیر مورد انتظار نیست. بدون لایسنس، "
                       "حالت تمرینی و داشبورد کار می‌کنند ولی معاملهٔ واقعی نه.",
                reason=f"no licence file at {self.licence_path}. Paper trading and "
                       "the dashboard still work; live trading does not.")

        document = read_license_file(self.licence_path)
        # Read the id and expiry WITHOUT trusting the signature yet, only so the
        # guard has something to key on. Nothing is granted on this basis.
        peeked_id, peeked_expired = "", False
        try:
            peeked, _sig, _canon = parse(document)
            peeked_id = peeked.licence_id
            # The SAME predicate verify() uses, grace period included. Without
            # the grace term, a licence two days past expiry and twelve days
            # inside a fourteen-day grace window -- one this gate would
            # otherwise report as valid -- was written into the guard's
            # permanently-expired list, which then overturns every later valid
            # verdict for ever.
            peeked_expired = (
                peeked.expiry is not None
                and now.timestamp() > peeked.expiry.timestamp()
                + self.grace_days * 86400)
            # Pin the anti-rollback state to the machine map the LICENCE
            # carries, so no change to this machine can rotate the key.
            if self.guard is not None:
                self.guard.rekey(peeked.machine)
        except LicenseError:
            pass

        try:
            licence = verify(document, self.public_key,
                             grace_days=self.grace_days, now=now,
                             current_fingerprint=self._fingerprint)
            in_grace = False
        except LicenseExpired as exc:
            clock = self._run_guard(now, peeked_id, True)
            return LicenseStatus(
                valid=False, reason=str(exc), clock=clock, stage="expired",
                headline="لایسنس منقضی شده است",
                advice="مهلت ارفاقی هم تمام شده. ربات معاملهٔ تازه باز نمی‌کند، "
                       "ولی معامله‌های باز همچنان مدیریت و محافظت می‌شوند.")
        except LicenseMachineMismatch as exc:
            clock = self._run_guard(now, peeked_id, peeked_expired)
            return LicenseStatus(
                valid=False, reason=str(exc), clock=clock, stage="none",
                headline="این لایسنس برای دستگاه دیگری است",
                advice="اگر سرور را عوض کرده‌اید یا سخت‌افزار را تغییر داده‌اید، "
                       "از فروشنده بخواهید لایسنس را برای این دستگاه دوباره صادر کند.")
        except LicenseInvalid as exc:
            clock = self._run_guard(now, peeked_id, peeked_expired)
            return LicenseStatus(
                valid=False, reason=str(exc), clock=clock, stage="none",
                headline="فایل لایسنس معتبر نیست",
                advice="یا فایل بعد از صدور دستکاری شده، یا اصلاً برای این "
                       "محصول صادر نشده است.")
        except LicenseError as exc:  # pragma: no cover - defensive
            clock = self._run_guard(now, peeked_id, peeked_expired)
            return LicenseStatus(valid=False, reason=str(exc), clock=clock)

        warnings: List[str] = []
        remaining = licence.days_remaining(now=now)
        stage = licence.renewal_stage(now=now)
        headline, advice = "", ""
        if remaining is not None:
            # PERSIAN: these are rendered verbatim in the dashboard, and the
            # routine renewal warning is the one line every customer reads
            # every single term.
            if remaining < 0:
                in_grace = True
                warnings.append(
                    f"لایسنس {abs(remaining):.0f} روز پیش تمام شد و الان در مهلت "
                    f"ارفاقی {self.grace_days} روزه است. با پایان این مهلت، ورود "
                    "به معاملهٔ تازه متوقف می‌شود.")
            elif remaining <= RENEWAL_WARNING_DAYS:
                warnings.append(
                    f"لایسنس {remaining:.0f} روز دیگر تمام می‌شود.")
        if stage in RENEWAL_MESSAGES:
            import math
            # Round AWAY from the comfortable answer: 0.4 days left is "1 day",
            # not "0 days", and a licence that lapsed 0.9 days ago lapsed
            # "1 day" ago, not "0". int() truncation reported both as zero.
            left = remaining or 0.0
            days = math.ceil(left) if left > 0 else math.floor(abs(left))
            headline, template = RENEWAL_MESSAGES[stage]
            advice = template.format(days=max(days, 0), grace=self.grace_days)
        elif stage == "perpetual":
            headline, advice = "لایسنس دائمی", "این لایسنس تاریخ پایان ندارد."
        else:
            headline = "لایسنس فعال است"
            advice = (f"{licence.term_label} — تا "
                      f"{(licence.expires_at or '')[:10]} معتبر است.")

        integrity = self._check_integrity()
        if integrity is not None and not integrity.ok and not integrity.unsigned:
            warnings.append(integrity.summary())

        # The clock guard runs LAST and can overturn a "valid" verdict, because
        # a signature that verifies against a clock somebody moved is not
        # evidence of anything.
        # verify() already refused anything past expiry+grace, so a licence
        # reaching this point is never expired-beyond-grace: passing that
        # condition here was dead code that LOOKED like it recorded grace
        # expiry. The recording happens on the LicenseExpired branch above,
        # which is the only path an expired licence actually takes.
        clock = self._run_guard(now, licence.licence_id, False)
        activation = self._activation_status(licence, document, now)
        valid = True
        reason = ""
        if clock is not None and not clock.ok:
            valid = False
            reason = clock.message
            headline = ("ساعت این دستگاه عقب کشیده شده"
                        if clock.rolled_back else "لایسنس قبلاً منقضی شده بود")
            advice = clock.message
            # A DISTINCT stage. Reusing "expired" produced a payload reading
            # «منقضی شده» beside «۱۸۴ روز باقی‌مانده» in the same card, which
            # tells the operator the software is confused rather than that
            # their clock is wrong.
            stage = "clock" if clock.rolled_back else "expired"
        if clock is not None and clock.degraded and clock.ok:
            warnings.append(
                "سابقهٔ زمانی این نصب پیدا نشد، خوانده نشد یا نوشته نشد، پس "
                "سامانه حافظهٔ قابل‌اتکایی از آخرین اجرا ندارد. یک نصب تازه، یک "
                "بازگردانی از پشتیبان و یک دستکاری، هر سه دقیقاً همین شکلی‌اند — "
                "پس فقط ثبت شده و جلوی کاری گرفته نشده."
                + (f" {clock.message}" if clock.message else ""))

        if activation is not None and activation.required and not activation.ok:
            if activation.lease is not None and activation.lease.status != "active":
                # The vendor's server has withdrawn this licence. That is a
                # verdict about the licence, not about the network.
                valid = False
                reason = activation.reason
                headline = "لایسنس از سوی فروشنده غیرفعال شده است"
                advice = ("سرور فعال‌سازی فروشنده این لایسنس را باطل یا معلق اعلام "
                          "کرده است. معامله‌های باز همچنان مدیریت می‌شوند ولی معاملهٔ "
                          "واقعی تازه باز نمی‌شود.")
                stage = "revoked"
            else:
                warnings.append(
                    "فعال‌سازی آنلاین لایسنس انجام نشده یا منقضی شده است؛ تا وقتی "
                    "سرور فعال‌سازی فروشنده در دسترس نباشد، معاملهٔ واقعی تازه باز "
                    "نمی‌شود. " + activation.reason)
        elif activation is not None and activation.network_error and activation.ok:
            warnings.append(
                "سرور فعال‌سازی در دسترس نبود؛ مجوز قبلی تا "
                f"{(activation.lease.expires_at if activation.lease else '')[:16]} "
                "معتبر است.")

        return LicenseStatus(valid=valid, reason=reason, licence=licence,
                             integrity=integrity, warnings=warnings,
                             in_grace=in_grace, days_remaining=remaining,
                             clock=clock, stage=stage, headline=headline,
                             advice=advice, activation=activation,
                             distributed=is_distributed_build())

    def _activation_status(self, licence: License, document: str,
                           now: datetime) -> Optional[LeaseStatus]:
        try:
            from .fingerprint import machine_fingerprint
            fingerprint = self._fingerprint or machine_fingerprint()
            return self.activation.status(licence, document, fingerprint, now=now)
        except Exception as exc:  # noqa: BLE001 - a broken client is a missing lease
            required = bool(licence.activation_url and licence.activation_interval_hours)
            return LeaseStatus(required=required, ok=not required,
                               reason=f"activation check failed: {exc}")

    def _run_guard(self, now: datetime, licence_id: str,
                   expired: bool) -> Optional[GuardReport]:
        if self.guard is None:
            return None
        try:
            return self.guard.evaluate(now_ns=int(now.timestamp() * 1e9),
                                       licence_id=licence_id,
                                       licence_expired=expired)
        except Exception:  # noqa: BLE001 - the guard must never break the gate
            return None

    def _check_integrity(self) -> Optional[IntegrityReport]:
        if not self.manifest_path.exists():
            if self.require_manifest:
                return IntegrityReport(
                    ok=False, unsigned=False,
                    error=(f"{self.manifest_path.name} is missing from a distributed "
                           "build; the installation cannot prove it is unmodified"))
            report = IntegrityReport(ok=True, unsigned=True)
            report.error = None
            return report
        try:
            text = self.manifest_path.read_text(encoding="utf-8")
        except OSError as exc:
            return IntegrityReport(ok=False, error=f"cannot read the manifest: {exc}")
        return verify_manifest(self.root, text, self.public_key)

    # -- questions the rest of the system asks ------------------------------ #

    def may_trade_live(self) -> tuple:
        """(allowed, reason). The single highest-value gate."""
        status = self.check()
        if status.unlicensed_mode:
            return True, ""
        if not status.valid:
            return False, ("معاملهٔ واقعی به یک لایسنس معتبر نیاز دارد: "
                           f"{status.headline or status.reason}")
        assert status.licence is not None
        if not status.licence.capability("live_trading", False):
            return False, (
                f"سطح این لایسنس «{status.licence.tier}» است و معاملهٔ واقعی را "
                "شامل نمی‌شود. حالت تمرینی و آزمایشگاه پژوهش بدون تغییر کار "
                "می‌کنند.")
        activation = status.activation
        if activation is not None and activation.required and not activation.ok:
            return False, ("معاملهٔ واقعی به فعال‌سازی آنلاین لایسنس نیاز دارد: "
                           + activation.reason)
        integrity = status.integrity
        if integrity is not None and not integrity.ok and not integrity.unsigned:
            # Live money on an installation whose protected modules do not match
            # the release is refused. This is the one place integrity BLOCKS
            # rather than warns, and it protects the customer as much as the
            # vendor: those modules are the risk engine and the audit chain.
            return False, (
                "معاملهٔ واقعی رد شد چون فایل‌های حساس برنامه با نسخهٔ منتشرشده "
                f"یکی نیستند ({integrity.summary()}). از یک نسخهٔ سالم دوباره "
                "نصب کنید، یا در حالت تمرینی کار کنید.")
        return True, ""

    def may_use(self, capability: str) -> tuple:
        status = self.check()
        if status.unlicensed_mode:
            return True, ""
        if not status.valid:
            return False, status.reason
        assert status.licence is not None
        if not status.licence.capability(capability, False):
            return False, (f"'{capability}' is not included in the "
                           f"'{status.licence.tier}' tier")
        return True, ""

    def check_limits(self, *, instruments: int = 0,
                     equity: Optional[Any] = None) -> List[str]:
        """Limit breaches, as human sentences. Empty means compliant.

        These are reported as warnings by the caller rather than enforced by
        truncation: silently trading fewer instruments than the operator
        configured would be a worse surprise than a clear message.
        """
        status = self.check()
        if status.unlicensed_mode or not status.valid or status.licence is None:
            return []
        out: List[str] = []
        max_instruments = status.licence.capability("max_instruments")
        if max_instruments is not None and instruments > int(max_instruments):
            out.append(
                f"{instruments} instruments are enabled but this licence covers "
                f"{max_instruments}. Reduce the list or upgrade the licence.")
        max_equity = status.licence.capability("max_equity")
        if max_equity is not None and equity is not None:
            try:
                from decimal import Decimal
                if Decimal(str(equity)) > Decimal(str(max_equity)):
                    out.append(
                        f"account equity {equity} exceeds the {max_equity} ceiling of "
                        f"the '{status.licence.tier}' tier.")
            except Exception:  # noqa: BLE001
                pass
        return out
