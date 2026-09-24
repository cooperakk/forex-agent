"""Risk across accounts: a shared ledger, one row per engine.

An engine bounds its own book -- 2% total open risk, 1.25% per currency leg,
4 positions. Two engines on two accounts of the same owner each bound their
own book, and the owner holds 4% open risk, 2.5% on a currency leg, 8
positions, and a drawdown that is the sum of two ladders that cannot see each
other. Risk that is invisible to the thing enforcing it is not bounded.

This module is the visibility. Every engine in a group writes a small JSON
row into a shared directory once per cycle -- its equity, its open risk, its
currency legs, its drawdown -- and reads everyone else's before deciding on
an entry. The risk engine then sees the GROUP's committed risk beside its own
and refuses an entry that would push the group over ``max_group_open_risk_pct``.

Deliberately simple: files, not a service. A row older than
``stale_after_sec`` is ignored for the total (that engine may be down) but
reported, so a group whose members cannot see each other says so rather than
trading as if the others had no risk. There is no leader and no lock; each
engine owns its own file and reads the rest.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from ..core.clock import wall_ns
from ..core.money import D, ZERO, dec


@dataclass
class GroupRow:
    account: str
    currency: str
    equity: Decimal
    open_risk: Decimal
    pending_risk: Decimal
    drawdown_pct: Decimal
    positions: int
    currency_risk: Dict[str, Decimal]            # signed, in account currency
    written_ns: int
    halted: bool = False

    def to_dict(self) -> dict:
        return {"account": self.account, "currency": self.currency,
                "equity": str(self.equity), "open_risk": str(self.open_risk),
                "pending_risk": str(self.pending_risk), "drawdown_pct": str(self.drawdown_pct),
                "positions": self.positions,
                "currency_risk": {k: str(v) for k, v in self.currency_risk.items()},
                "written_ns": self.written_ns, "halted": self.halted}

    @classmethod
    def from_dict(cls, d: dict) -> "GroupRow":
        return cls(account=str(d["account"]), currency=str(d.get("currency", "")),
                   equity=dec(d.get("equity", 0)), open_risk=dec(d.get("open_risk", 0)),
                   pending_risk=dec(d.get("pending_risk", 0)),
                   drawdown_pct=dec(d.get("drawdown_pct", 0)),
                   positions=int(d.get("positions", 0)),
                   currency_risk={k: dec(v) for k, v in (d.get("currency_risk") or {}).items()},
                   written_ns=int(d.get("written_ns", 0)), halted=bool(d.get("halted", False)))


@dataclass
class GroupView:
    """What the rest of the group looks like from one engine."""

    members: List[GroupRow] = field(default_factory=list)
    stale: List[str] = field(default_factory=list)
    unreadable: List[str] = field(default_factory=list)
    #: Sum over FRESH members other than self, in this engine's currency.
    others_open_risk: Decimal = ZERO
    others_equity: Decimal = ZERO
    others_positions: int = 0
    others_currency_risk: Dict[str, Decimal] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"members": [m.account for m in self.members], "stale": self.stale,
                "unreadable": self.unreadable,
                "others_open_risk": str(self.others_open_risk),
                "others_equity": str(self.others_equity),
                "others_positions": self.others_positions}


class GroupLedger:
    def __init__(self, directory: str | Path, account: str, *,
                 stale_after_sec: int = 300) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.account = str(account)
        self.stale_after_ns = int(stale_after_sec) * 10**9
        self.path = self.dir / f"{self._safe(self.account)}.json"

    @staticmethod
    def _safe(name: str) -> str:
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:64]

    # -- write --------------------------------------------------------------- #

    def publish(self, row: GroupRow) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(row.to_dict(), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # -- read ---------------------------------------------------------------- #

    def view(self, now_ns: Optional[int] = None,
             conversions: Optional[Dict[str, Decimal]] = None,
             own_currency: str = "") -> GroupView:
        """Everyone else's row, converted to this engine's currency when a
        rate is known. A member in another currency with no rate is reported
        as unreadable and EXCLUDED -- which understates the group, so the
        caller must treat a non-empty ``unreadable`` as a reason to be
        conservative."""
        now = now_ns if now_ns is not None else wall_ns()
        out = GroupView()
        for file in sorted(self.dir.glob("*.json")):
            if file == self.path:
                continue
            try:
                row = GroupRow.from_dict(json.loads(file.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                out.unreadable.append(file.stem)
                continue
            out.members.append(row)
            if now - row.written_ns > self.stale_after_ns:
                out.stale.append(row.account)
                continue
            rate = D("1")
            if own_currency and row.currency and row.currency != own_currency:
                rate = (conversions or {}).get(row.currency)
                if rate is None or rate <= 0:
                    out.unreadable.append(row.account)
                    continue
            out.others_open_risk += (row.open_risk + row.pending_risk) * rate
            out.others_equity += row.equity * rate
            out.others_positions += row.positions
            for ccy, v in row.currency_risk.items():
                out.others_currency_risk[ccy] = out.others_currency_risk.get(ccy, ZERO) + v * rate
        return out
