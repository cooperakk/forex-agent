"""Broker profiles: the differences between venues, written down once.

Every MetaTrader broker in the world speaks the same protocol and none of them
behaves the same way. The symbol is ``EURUSD`` at one, ``EURUSD.m`` at the next
and ``EURUSDmicro`` at a third. One rejects a stop closer than 20 points, one
allows zero. One fills with ``IOC``, one only accepts ``FOK`` and rejects every
order you send until you discover that. Swap is charged at 00:00 server time,
and the server is in UTC+2, or UTC+3, or moves between them twice a year.

None of that is exotic or optional. Every one of those differences produces an
order rejection, a mis-sized position or a phantom cost that is invisible in a
backtest, and the usual response -- discovering them one at a time, in
production, with money on -- is exactly the failure mode this whole system
exists to avoid.

So a profile is a *declaration* of a venue's behaviour, resolved before the
first order and verified against the live terminal at startup. Where the
declaration and the terminal disagree, **the terminal wins and the mismatch is
recorded**: a profile is a prior, not an authority. That asymmetry is the whole
design. A profile that silently overrode the venue would be a confident lie.

What a profile does NOT do is change how much you are charged, whether the
broker is solvent, or whether your withdrawal clears. Those are counterparty
questions, and `docs/BROKERS.md` treats them as first-class risk rather than
paperwork.
"""

from .base import (
    BrokerProfile,
    FillingMode,
    ProfileMismatch,
    SymbolMap,
    get_profile,
    list_profiles,
    register_profile,
    resolve_profile,
)

# Import for the side effect of registering the shipped profiles. A registry
# that is empty until someone happens to call the right factory is a trap: the
# first symptom is "unknown broker 'amarkets'" from a config that is correct.
from . import venues as _venues  # noqa: E402,F401

__all__ = [
    "BrokerProfile", "FillingMode", "ProfileMismatch", "SymbolMap",
    "get_profile", "list_profiles", "register_profile", "resolve_profile",
]
