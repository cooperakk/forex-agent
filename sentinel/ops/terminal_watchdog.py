"""Keep the MetaTrader 5 terminal open, connected and signed in.

The engine talks to MetaTrader through a desktop program that can close,
freeze, lose its connection, sit at the login dialog after a Windows update,
or be switched to another account by someone at the keyboard. Before this
module each of those was a silent stall: the cycle degraded, no entries were
taken, and nobody knew why until they looked.

The watchdog runs at the top of every agent cycle, on the agent's thread
(MetaTrader tolerates calls from one thread only), and climbs a ladder:

1. ``grace_checks`` unhealthy readings in a row before anything happens --
   one failed read is usually a blip;
2. ``reconnect``: shut the session down and initialise again. MetaTrader5
   starts the terminal if it is not running, and with a stored credential
   signs in to this service's account;
3. retries back off -- 30 s, 60 s, 120 s ... up to ``backoff_max_sec`` -- so a
   broker outage is not hammered;
4. after ``kill_hung_after_failures`` failed attempts on a terminal that does
   not answer at all, END that process (the one at the configured path,
   nothing else) so the next attempt starts it clean.

What it never does: open, modify or close a position. Open positions keep
their stop-loss at the broker throughout. New entries stay blocked by the
connectivity veto (the cycle cannot read the account) until the terminal is
back, and the account binding refuses a wrong account independently.

Every step is journalled as ``ops.mt5_watchdog`` and so reaches Telegram and
Bale: "the terminal was closed; restarted and signed in again after 2 min".
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..core.clock import wall_ns

EVENT = "ops.mt5_watchdog"
_SEC = 1_000_000_000

STATE_FA = {
    "ok": "وصل و سالم",
    "terminal_down": "متاتریدر بسته است یا جواب نمی‌دهد",
    "broker_disconnected": "متاتریدر باز است ولی به سرور بروکر وصل نیست",
    "not_logged_in": "متاتریدر وارد هیچ حسابی نشده",
    "wrong_account": "متاتریدر روی حساب دیگری است",
}


class TerminalWatchdog:
    def __init__(self, broker: Any, audit, config: Callable[[], Any], *,
                 clock: Callable[[], int] = wall_ns) -> None:
        # Through the account binding to the adapter itself: the binding
        # refuses every call on a wrong account, which is exactly the state
        # this watchdog has to be able to see.
        self.broker = getattr(broker, "inner", broker)
        self.audit = audit
        self._config = config
        self._clock = clock
        self.state = "unknown"
        self.detail = ""
        self.bad_checks = 0
        self.failures = 0
        self.attempts = 0
        self.down_since_ns = 0
        self.next_attempt_ns = 0
        self.last_check_ns = 0
        self.last_action = ""
        self.last_action_ns = 0
        self.recoveries = 0
        self.algo_trading: Optional[bool] = None
        self.ping_ms: Optional[float] = None
        self._announced_down = False
        self._algo_alerted = False

    @property
    def cfg(self):
        return self._config()

    @property
    def applicable(self) -> bool:
        return callable(getattr(self.broker, "health", None)) and \
            callable(getattr(self.broker, "reconnect", None))

    def check(self, now_ns: Optional[int] = None) -> Dict[str, Any]:
        cfg = self.cfg
        now = int(now_ns or self._clock())
        if not (cfg.enabled and self.applicable):
            return self.status()
        self.last_check_ns = now
        try:
            h = self.broker.health()
        except Exception as exc:  # noqa: BLE001 - a failing probe is a reading
            h = {"state": "terminal_down", "detail": f"{type(exc).__name__}: {exc}"[:200]}
        state = str(h.get("state") or "terminal_down")
        self.state, self.detail = state, str(h.get("detail") or "")

        if state == "ok":
            self.ping_ms = h.get("ping_ms")
            self._on_healthy(now, bool(h.get("algo_trading", True)))
            return self.status()

        self.bad_checks += 1
        if not self.down_since_ns:
            self.down_since_ns = now
        if self.bad_checks < cfg.grace_checks:
            return self.status()
        if not self._announced_down:
            self._announced_down = True
            self._emit("down", now, state=state, detail=self.detail)

        if state == "wrong_account" and not (cfg.restore_account and
                                             getattr(self.broker, "can_sign_in", False)):
            # In attach mode the terminal is shared with a person; switching
            # it back would take it away from them. The binding already
            # refuses to route orders; say so and wait.
            return self.status()
        if now < self.next_attempt_ns:
            return self.status()

        if (cfg.kill_hung_after_failures and state == "terminal_down"
                and self.failures >= cfg.kill_hung_after_failures
                and callable(getattr(self.broker, "kill_terminal", None))):
            ok, detail = self.broker.kill_terminal()
            self._act("killed" if ok else "kill_skipped", now, detail=detail)

        self.attempts += 1
        try:
            ok, detail = self.broker.reconnect()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"[:200]
        if ok:
            self._act("reconnected", now, detail=detail, previous_state=state,
                      down_sec=round((now - self.down_since_ns) / _SEC))
            self._reset(now)
            self.state, self.detail = "ok", ""
        else:
            self.failures += 1
            delay = min(cfg.backoff_max_sec, cfg.backoff_initial_sec * 2 ** (self.failures - 1))
            self.next_attempt_ns = now + int(delay * _SEC)
            self._act("reconnect_failed", now, detail=detail, state=state,
                      failures=self.failures, retry_in_sec=int(delay))
        return self.status()

    # ------------------------------------------------------------------ #

    def _on_healthy(self, now: int, algo: bool) -> None:
        if self.down_since_ns and self._announced_down:
            self._act("recovered", now, down_sec=round((now - self.down_since_ns) / _SEC))
        if self.down_since_ns:
            self._reset(now)
        if not algo and not self._algo_alerted:
            self._algo_alerted = True
            self._emit("algo_off", now)
        elif algo and self._algo_alerted:
            self._algo_alerted = False
            self._emit("algo_on", now)
        self.algo_trading = algo

    def _reset(self, now: int) -> None:
        if self.down_since_ns:
            self.recoveries += 1
        self.bad_checks = 0
        self.failures = 0
        self.down_since_ns = 0
        self.next_attempt_ns = 0
        self._announced_down = False

    def _act(self, action: str, now: int, **payload: Any) -> None:
        self.last_action, self.last_action_ns = action, now
        self._emit(action, now, **payload)

    def _emit(self, action: str, now: int, **payload: Any) -> None:
        # Which outage this belongs to: lets a notifier tell a second outage
        # from a repeat of the first.
        payload.setdefault("since_ns", self.down_since_ns)
        payload.setdefault("state_fa", STATE_FA.get(self.state, self.state))
        try:
            self.audit.append(EVENT, {"action": action, **payload}, actor="watchdog")
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> Dict[str, Any]:
        return {
            "applicable": self.applicable, "enabled": bool(self.cfg.enabled),
            "state": self.state, "state_fa": STATE_FA.get(self.state, self.state),
            "detail": self.detail, "down_since_ns": self.down_since_ns,
            "failures": self.failures, "attempts": self.attempts,
            "next_attempt_ns": self.next_attempt_ns, "last_action": self.last_action,
            "last_action_ns": self.last_action_ns, "recoveries": self.recoveries,
            "algo_trading": self.algo_trading, "ping_ms": self.ping_ms,
            "last_check_ns": self.last_check_ns,
            "can_sign_in": bool(getattr(self.broker, "can_sign_in", False)),
        }
