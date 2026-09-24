"""Exception taxonomy.

The split matters operationally: ``TransientError`` may be retried,
``PermanentError`` must not be, and ``UnknownOutcomeError`` must NEVER be
retried blindly -- it means we do not know whether the venue acted, which is
precisely the case that duplicates positions.
"""

from __future__ import annotations

from typing import Any, Optional


class SentinelError(Exception):
    """Base class. Carries structured context for the audit journal."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict:
        return {"error": type(self).__name__, "message": self.message, **self.context}


class ConfigError(SentinelError):
    """Invalid or incoherent configuration."""


class DataError(SentinelError):
    """Missing, stale or malformed market data."""


class StaleDataError(DataError):
    pass


class ConversionMissingError(DataError):
    """No FX conversion rate to the account currency. Never assume 1.0."""


class TransientError(SentinelError):
    """Safe to retry with backoff (timeout on a *read*, 5xx, rate limit)."""


class PermanentError(SentinelError):
    """Retrying cannot help (bad instrument, insufficient margin, auth)."""


class UnknownOutcomeError(SentinelError):
    """The venue may or may not have acted.

    Resolution is by *query*, never by resend. The OMS quarantines the
    instrument until reconciliation proves the state.
    """


class RiskVeto(SentinelError):
    """The risk engine refused an action. Not an error condition -- a control."""

    def __init__(self, rule: str, message: str, **context: Any) -> None:
        super().__init__(message, rule=rule, **context)
        self.rule = rule


class HaltedError(SentinelError):
    """The agent is halted and refuses to open risk."""


class BrokerError(SentinelError):
    def __init__(self, message: str, *, code: Optional[str] = None, **ctx: Any) -> None:
        super().__init__(message, code=code, **ctx)
        self.code = code


class AuthError(SentinelError):
    pass


class NotAcceptedError(SentinelError):
    """A strategy tried to touch real money before passing acceptance."""
