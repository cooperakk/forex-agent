"""Deterministic identifiers.

The single most important property here is that a *retry* of the same trading
intent produces the *same* client order id. That is what makes order
submission idempotent: if the HTTP response was lost but the venue accepted
the order, the retry is rejected as a duplicate instead of doubling the
position.

The id is derived from the intent, not from a random source, so it survives a
process restart (see ``execution/oms.py`` -- the OMS replays open intents from
the journal after a crash and regenerates byte-identical ids).
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from typing import Any, Mapping

# Most venues restrict client ids to a short alphanumeric token.
# OANDA: clientExtensions.id, <= 128 chars, ASCII.
# FIX ClOrdID: <= 64 chars by convention.
# We stay inside the tightest common denominator: 32 chars, [A-Za-z0-9_-].
_MAX_CLIENT_ID = 32
_SAFE = re.compile(r"[^A-Za-z0-9_-]")

# Process-wide run id. Used to TAG journal records with which process wrote
# them. It is deliberately NOT an input to the client order id: a fresh id per
# process would mean a crash-and-restart re-derives a different key for the same
# trading intent, the venue's duplicate rejection would not fire, and the
# position would double -- the exact failure the key exists to prevent.
RUN_ID = os.environ.get("SENTINEL_RUN_ID") or uuid.uuid4().hex[:8]


def _canonical(payload: Mapping[str, Any]) -> str:
    """Stable textual form of a mapping. Order-independent, type-explicit."""
    parts = []
    for k in sorted(payload):
        v = payload[k]
        if isinstance(v, float):
            # Floats are never allowed to decide identity; force a fixed form.
            v = f"{v:.10f}"
        parts.append(f"{k}={v}")
    return "|".join(parts)


def intent_hash(payload: Mapping[str, Any]) -> str:
    """Full 64-hex digest of a trading intent. Used for journal keys."""
    return hashlib.blake2b(_canonical(payload).encode("utf-8"), digest_size=32).hexdigest()


def client_order_id(
    *,
    strategy: str,
    instrument: str,
    side: str,
    decision_ns: int,
    seq: int = 0,
    account: str = "",
    attempt: int = 0,
) -> str:
    """Venue-safe idempotency key, derived ONLY from the trading intent.

    Every input must be a property of the decision itself, never of the process
    that made it. ``decision_ns`` is the timestamp of the BAR the signal was
    raised on (not ``wall_ns()``), and ``seq`` defaults to 0 because
    (account, strategy, instrument, side, bar) already identifies one intent.
    A restarted process replaying the same bar therefore regenerates a
    byte-identical key, and the venue rejects the duplicate.

    ``attempt`` MUST stay 0 for retries of the same intent -- it is present
    only for the deliberate case of *replacing* a rejected order with a new
    economic intent (e.g. after a price revalidation), where a fresh id is
    correct. Bumping it on a network timeout would defeat the whole mechanism,
    so ``execution/oms.py`` never touches it on retry.
    """
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    digest = hashlib.blake2b(
        _canonical(
            {
                "acct": account,
                "strat": strategy,
                "inst": instrument,
                "side": side,
                "t": decision_ns,
                "seq": seq,
                "att": attempt,
            }
        ).encode("utf-8"),
        digest_size=12,
    ).hexdigest()
    raw = f"SFX{digest}"
    return _SAFE.sub("", raw)[:_MAX_CLIENT_ID]


def new_correlation_id() -> str:
    """Non-deterministic id for tracing a request across components."""
    return uuid.uuid4().hex


def short_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"
