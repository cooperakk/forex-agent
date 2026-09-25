"""A broker that refuses to route anything once the venue's account changes.

A MetaTrader terminal is a shared, stateful thing: whoever sits at it can sign
into a different account, and from that moment every call the engine makes
goes to that account -- the risk limits, the drawdown ladder and the open
positions the engine remembers all belong to the previous one. Nothing in the
protocol tells the caller this happened.

So the engine declares which account it is for (``execution.expected_account_id``,
``expected_account_server``, ``account_currency`` and the venue mode), and this
wrapper re-reads the venue's own statement of identity before EVERY call --
reads included, because a stale quote from the wrong account is still the
wrong account. A mismatch raises ``BrokerError`` and nothing is forwarded.

The check is one ``account_info`` round trip per call. On a local terminal
that is microseconds; over the bridge it is one extra message, which is the
right price for never trading the wrong account.

An empty account id or currency is not checked. Bootstrap uses exactly that
-- a binding with only the venue mode -- for an external venue whose
configuration declares no account: whichever account the terminal is signed
into, a configuration that is not in live mode must never trade real money.
Before 1.8.2 such a configuration traded a real-money account with every
live-only gate (the acceptance lifecycle above all) switched off, because
those gates read the configured mode and not the account.
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.errors import BrokerError
from ..core.types import AccountState


class AccountBoundBroker:
    def __init__(self, broker: Any, account_id: str, account_type: str, currency: str,
                 server: str = "", *, strict_start: bool = True) -> None:
        self._broker = broker
        self._account_id = str(account_id)
        self._account_type = str(account_type)
        self._currency = str(currency).upper()
        self._server = str(server or "")
        self.capabilities = broker.capabilities
        self.bound_to = {"account_id": self._account_id, "account_type": self._account_type,
                         "currency": self._currency, "server": self._server}
        try:
            self._check()
        except Exception as exc:  # noqa: BLE001
            # A declared account is verified before the engine starts. The
            # mode-only guard (strict_start=False) refuses to start on a
            # mismatch too -- a real-money account under a non-live
            # configuration -- but an account that merely cannot be read yet
            # is checked again on every call instead of failing the boot.
            if strict_start or getattr(exc, "code", None) == "ACCOUNT_MISMATCH":
                raise

    # -- the guard ---------------------------------------------------------- #

    def _check(self) -> AccountState:
        account = self._broker.account()
        problems = []
        if self._account_id and str(account.account_id) != self._account_id:
            problems.append(f"account {account.account_id!r} != bound {self._account_id!r}")
        if self._currency and str(account.currency).upper() != self._currency:
            problems.append(f"currency {account.currency!r} != bound {self._currency!r}")
        # The venue mode "paper"/"demo"/"live" is compared against what the
        # venue REPORTS. A venue that reports nothing ("") cannot be bound to
        # live money, because the one fact that matters most would be unknown.
        reported = str(account.account_type or "")
        if self._account_type == "live" and reported != "live":
            problems.append(f"venue reports {reported or 'unknown'!r} account, bound to live")
        if self._account_type == "demo" and reported not in ("demo", ""):
            problems.append(f"venue reports {reported!r} account, bound to demo")
        if self._account_type == "paper" and reported == "live":
            problems.append("venue reports a live (real-money) account, but the "
                            "configuration is not in live mode")
        if self._server:
            terminal = getattr(self._broker, "_mt5", None)
            if terminal is not None:
                try:
                    info = terminal.account_info()
                except Exception as exc:  # noqa: BLE001
                    raise BrokerError(f"cannot read the terminal account: {exc}",
                                      code="ACCOUNT_UNREADABLE") from exc
                observed = str(getattr(info, "server", "") or "") if info is not None else ""
                if observed != self._server:
                    problems.append(f"server {observed!r} != bound {self._server!r}")
        if problems:
            raise BrokerError(
                "account binding violated; all routing refused: " + "; ".join(problems),
                code="ACCOUNT_MISMATCH")
        return account

    # -- forwarded surface -------------------------------------------------- #

    def account(self) -> AccountState:
        return self._check()

    def close(self) -> None:
        return self._broker.close()

    @property
    def supports_closed_trade_history(self) -> bool:
        return bool(getattr(self._broker, "supports_closed_trade_history", False))

    @property
    def supports_bar_history(self) -> bool:
        return bool(getattr(self._broker, "supports_bar_history", False))

    @property
    def inner(self) -> Any:
        """The wrapped adapter, for callers that need adapter-specific reads."""
        return self._broker

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._broker, name)
        if not callable(value) or name.startswith("_"):
            return value

        def guarded(*args, **kwargs):
            self._check()
            return value(*args, **kwargs)
        guarded.__name__ = getattr(value, "__name__", name)
        return guarded
