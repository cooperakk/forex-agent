"""The notifier: journal events -> Telegram / Bale, and two safe commands back.

Outbound
--------
The notifier listens to the audit journal (``AuditLog.add_listener``), turns
the events the owner cares about into short Persian messages
(``notify.messages``) and sends them from a worker thread, never from the
thread that wrote the event. Repeats of the same situation inside ten minutes
are collapsed; each channel is rate-limited; a failing channel is reported on
the dashboard and never slows anything down.

Inbound (off by default)
------------------------
With ``commands`` on, the notifier polls the bot for messages and answers
exactly three, and only from the configured chat:

* ``/status`` (or «وضعیت») -- mode, equity, today's result, open positions;
* ``/stop`` (or «توقف») -- ENGAGE the kill switch: no new positions;
* ``/help`` (or «راهنما»).

There is deliberately no command that RELEASES the kill switch, opens a
trade, or changes a setting. A stolen phone can therefore stop the robot but
never make it take risk; releasing still needs the dashboard, the password
and the second factor.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..core.clock import wall_ns
from .channels import HOSTS, LABELS_FA, BotChannel, ChannelError, validate_chat, validate_token
from .messages import CATEGORIES, DEFAULT_CATEGORIES, Message, render

DEDUPE_SEC = 600
MIN_GAP_SEC = 1.1                      # per channel; Telegram allows ~1 msg/s per chat
AUTH_BURST = 3                         # failed sign-ins within ten minutes
COMMAND_MAX_AGE_SEC = 300


def _default_channel() -> Dict[str, Any]:
    return {"enabled": False, "chat_id": "", "categories": list(DEFAULT_CATEGORIES),
            "commands": False}


class Notifier:
    def __init__(self, state_dir: str | Path, audit, *, secrets=None,
                 transport=None, status_fn: Optional[Callable[[], Dict[str, Any]]] = None,
                 kill_fn: Optional[Callable[[str, str], Any]] = None,
                 clock: Callable[[], int] = wall_ns) -> None:
        self.state_dir = Path(state_dir)
        self.audit = audit
        self._secrets = secrets
        self._secrets_error = ""
        self._transport = transport
        self.status_fn = status_fn
        self.kill_fn = kill_fn
        self._clock = clock
        self.settings_path = self.state_dir / "notify.json"
        self._lock = threading.RLock()
        self.settings = self._load()
        self._queue: Deque[Tuple[str, Message]] = collections.deque(maxlen=500)
        self._recent: Dict[Tuple[str, str], float] = {}
        self._last_sent: Dict[str, float] = {}
        self._auth_fails: Deque[float] = collections.deque(maxlen=50)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._poller: Optional[threading.Thread] = None
        self.status: Dict[str, Dict[str, Any]] = {k: {"sent": 0, "failed": 0, "last_error": "",
                                                      "last_ok_ns": 0} for k in HOSTS}

    # -- settings and keys ------------------------------------------------------ #

    def _load(self) -> Dict[str, Any]:
        base = {"channels": {k: _default_channel() for k in HOSTS}, "daily_hour_utc": 17,
                "daily_last": "", "offsets": {k: 0 for k in HOSTS}}
        try:
            data = json.loads(self.settings_path.read_text("utf-8"))
        except (OSError, ValueError):
            return base
        for kind in HOSTS:
            row = dict((data.get("channels") or {}).get(kind) or {})
            ch = _default_channel()
            ch["enabled"] = bool(row.get("enabled", False))
            try:
                ch["chat_id"] = validate_chat(row.get("chat_id", "")) if row.get("chat_id") \
                    else ""
            except ValueError:
                ch["chat_id"] = ""
            cats = [c for c in (row.get("categories") or []) if c in CATEGORIES]
            ch["categories"] = cats if row.get("categories") is not None else ch["categories"]
            ch["commands"] = bool(row.get("commands", False))
            base["channels"][kind] = ch
        try:
            base["daily_hour_utc"] = max(0, min(23, int(data.get("daily_hour_utc", 17))))
        except (TypeError, ValueError):
            pass
        base["daily_last"] = str(data.get("daily_last", ""))[:12]
        for kind in HOSTS:
            try:
                base["offsets"][kind] = max(0, int((data.get("offsets") or {}).get(kind, 0)))
            except (TypeError, ValueError):
                pass
        return base

    def _save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_dir), prefix="notify.json.",
                                   suffix=".tmp")
        try:
            os.write(fd, json.dumps(self.settings, indent=2, ensure_ascii=False).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.settings_path)

    @property
    def secrets(self):
        if self._secrets is None and not self._secrets_error:
            from ..brokers.secrets import SecretStore, SecretStoreError
            try:
                self._secrets = SecretStore(self.state_dir / "notify-secrets.json",
                                            key_path=self.state_dir / "broker-secrets.key")
            except SecretStoreError as exc:
                self._secrets_error = str(exc)
        return self._secrets

    def _token(self, kind: str) -> str:
        store = self.secrets
        if store is None:
            return ""
        try:
            return store.get(f"notify:{kind}") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _channel(self, kind: str) -> Optional[BotChannel]:
        token = self._token(kind)
        if not token:
            return None
        try:
            return BotChannel(kind, token, post=self._transport)
        except ValueError:
            return None

    # -- owner operations ------------------------------------------------------- #

    def save_channel(self, kind: str, *, enabled: bool, chat_id: Optional[str] = None,
                     categories: Optional[List[str]] = None, commands: bool = False,
                     token: Optional[str] = None, daily_hour_utc: Optional[int] = None,
                     by: str = "") -> Dict[str, Any]:
        """``chat_id`` / ``categories`` of None keep the stored value; "" clears
        the chat id."""
        if kind not in HOSTS:
            raise ValueError(f"unknown channel {kind!r}")
        with self._lock:
            current = dict(self.settings["channels"].get(kind) or _default_channel())
        if chat_id is None:
            chat = str(current.get("chat_id") or "")
        else:
            chat = validate_chat(chat_id) if chat_id else ""
        source = categories if categories is not None else (current.get("categories")
                                                            or DEFAULT_CATEGORIES)
        cats = [c for c in source if c in CATEGORIES]
        if token is not None:
            store = self.secrets
            if store is None:
                raise ValueError("tokens cannot be stored: " + (self._secrets_error or
                                                                "no credential key"))
            if token.strip():
                store.put(f"notify:{kind}", validate_token(token))
            else:
                store.delete(f"notify:{kind}")
        if enabled and not chat:
            raise ValueError("a chat id is needed to enable the channel "
                             "(use 'find my chat id' after messaging the bot)")
        with self._lock:
            self.settings["channels"][kind] = {"enabled": bool(enabled), "chat_id": chat,
                                               "categories": cats, "commands": bool(commands)}
            if daily_hour_utc is not None:
                self.settings["daily_hour_utc"] = max(0, min(23, int(daily_hour_utc)))
            self._save()
        self.audit.append("config.change", {"action": "notify_channel_saved", "channel": kind,
                                            "enabled": bool(enabled), "categories": cats,
                                            "commands": bool(commands),
                                            "token_changed": token is not None},
                          actor=by or "owner")
        self.start()
        return self.describe(include_private=True)

    def test(self, kind: str, by: str = "") -> Dict[str, Any]:
        ch = self._channel(kind)
        chat = self.settings["channels"][kind].get("chat_id")
        if ch is None:
            return {"ok": False, "error": "no bot token is stored for this channel"}
        if not chat:
            return {"ok": False, "error": "no chat id is configured"}
        try:
            ch.send(chat, f"✅ پیام آزمایشی Sentinel-FX از طریق {LABELS_FA[kind]}.\n"
                          "اگر این را می‌بینید، اعلان‌ها کار می‌کنند.")
        except (ChannelError, ValueError) as exc:
            self._fail(kind, str(exc))
            return {"ok": False, "error": str(exc)}
        self._ok(kind)
        return {"ok": True}

    def discover(self, kind: str) -> Dict[str, Any]:
        """Recent chats that messaged the bot: how a non-technical owner finds the
        chat id -- send the bot any message, then press the button."""
        ch = self._channel(kind)
        if ch is None:
            return {"ok": False, "error": "save the bot token first", "chats": []}
        try:
            me = ch.me()
            updates = ch.updates(0, 0)
        except ChannelError as exc:
            return {"ok": False, "error": str(exc), "chats": []}
        chats: Dict[str, Dict[str, Any]] = {}
        for u in updates[-50:]:
            msg = u.get("message") if isinstance(u, dict) else None
            chat = (msg or {}).get("chat") or {}
            cid = chat.get("id")
            if cid is None:
                continue
            chats[str(cid)] = {"id": str(cid), "type": str(chat.get("type", ""))[:20],
                               "name": str(chat.get("title") or chat.get("username") or
                                           chat.get("first_name") or "")[:60]}
        return {"ok": True, "bot": str(me.get("username", ""))[:60],
                "chats": list(chats.values())}

    # -- the journal listener ---------------------------------------------------- #

    def on_audit(self, rec) -> None:
        """AuditLog listener: render and enqueue. Must be quick and never raise."""
        try:
            event, payload, actor = rec.event, rec.payload, rec.actor
            if actor == "notify":
                return
            if event == "sec.auth_fail":
                now = time.monotonic()
                self._auth_fails.append(now)
                recent = [t for t in self._auth_fails if now - t < 600]
                if len(recent) >= AUTH_BURST:
                    self.enqueue(Message("security", "authburst",
                                         f"🔐 {len(recent)} ورود ناموفق در ده دقیقهٔ اخیر به "
                                         "داشبورد. اگر خودتان نبودید، رمز را عوض کنید."))
                return
            msg = render(event, payload if isinstance(payload, dict) else {}, actor)
            if msg is not None:
                self.enqueue(msg)
        except Exception:  # noqa: BLE001 - a side channel never fails the journal
            pass

    def enqueue(self, msg: Message) -> int:
        now = time.monotonic()
        queued = 0
        with self._lock:
            channels = dict(self.settings["channels"])
        for kind, ch in channels.items():
            if not ch.get("enabled") or msg.category not in (ch.get("categories") or []):
                continue
            key = (kind, msg.key)
            if now - self._recent.get(key, -1e9) < DEDUPE_SEC:
                continue
            self._recent[key] = now
            self._queue.append((kind, msg))
            queued += 1
        if len(self._recent) > 2000:
            cutoff = now - DEDUPE_SEC
            self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}
        if queued:
            self._wake.set()
        return queued

    # -- workers --------------------------------------------------------------- #

    def start(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._stop.clear()
                self._worker = threading.Thread(target=self._send_loop, name="notify-send",
                                                daemon=True)
                self._worker.start()
            wants_commands = any(c.get("enabled") and c.get("commands")
                                 for c in self.settings["channels"].values())
            if wants_commands and (self._poller is None or not self._poller.is_alive()):
                self._poller = threading.Thread(target=self._poll_loop, name="notify-poll",
                                                daemon=True)
                self._poller.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def drain(self, max_items: int = 100) -> int:
        """Send what is queued now (the worker's body; also used by tests)."""
        sent = 0
        for _ in range(max_items):
            try:
                kind, msg = self._queue.popleft()
            except IndexError:
                break
            gap = time.monotonic() - self._last_sent.get(kind, -1e9)
            if gap < MIN_GAP_SEC and self._worker is not None \
                    and threading.current_thread() is self._worker:
                time.sleep(MIN_GAP_SEC - gap)
            ch = self._channel(kind)
            chat = self.settings["channels"][kind].get("chat_id")
            if ch is None or not chat:
                self._fail(kind, "no token or chat id")
                continue
            try:
                ch.send(chat, msg.text)
                self._last_sent[kind] = time.monotonic()
                self._ok(kind)
                sent += 1
            except (ChannelError, ValueError) as exc:
                self._fail(kind, str(exc))
                status = getattr(exc, "status", None)
                if status == 429:
                    time.sleep(5.0 if threading.current_thread() is self._worker else 0)
        return sent

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=30.0)
            self._wake.clear()
            try:
                self.drain()
            except Exception as exc:  # noqa: BLE001 - the worker must survive
                for k in HOSTS:
                    self.status[k]["last_error"] = f"worker: {exc}"[:200]

    def _poll_loop(self) -> None:
        offsets = {k: int((self.settings.get("offsets") or {}).get(k, 0)) for k in HOSTS}
        while not self._stop.is_set():
            active = [k for k, c in self.settings["channels"].items()
                      if c.get("enabled") and c.get("commands")]
            if not active:
                return
            for kind in active:
                try:
                    new = self.poll_once(kind, offsets[kind], timeout=20)
                    if new != offsets[kind]:
                        # Persisted, so a restart does not replay (and re-obey)
                        # a command the bot API still holds for 24 hours.
                        offsets[kind] = new
                        with self._lock:
                            self.settings.setdefault("offsets", {})[kind] = new
                            self._save()
                except Exception as exc:  # noqa: BLE001
                    self._fail(kind, f"commands: {exc}")
                    self._stop.wait(10.0)

    def poll_once(self, kind: str, offset: int, timeout: int = 0) -> int:
        ch = self._channel(kind)
        chat = self.settings["channels"][kind].get("chat_id")
        if ch is None or not chat:
            return offset
        for u in ch.updates(offset, timeout):
            offset = max(offset, int(u.get("update_id", 0)) + 1)
            msg = u.get("message") or {}
            from_chat = str((msg.get("chat") or {}).get("id", ""))
            text = str(msg.get("text") or "").strip()
            if from_chat != str(chat) or not text:
                continue                 # only the owner's configured chat is heard
            try:
                sent_s = float(msg.get("date") or 0)
            except (TypeError, ValueError):
                sent_s = 0.0
            if sent_s and self._clock() / 1e9 - sent_s > COMMAND_MAX_AGE_SEC:
                continue                 # a stale command (the API keeps them 24 h) is not obeyed
            reply = self.command(text, source=f"{kind}:{from_chat}")
            if reply:
                ch.send(chat, reply)
        return offset

    def command(self, text: str, *, source: str) -> Optional[str]:
        word = text.split()[0].lower().split("@")[0]
        if word in ("/status", "وضعیت"):
            return self.status_text()
        if word in ("/stop", "توقف"):
            if self.kill_fn is None:
                return "توقف اضطراری از این مسیر در دسترس نیست."
            try:
                self.kill_fn(f"requested from {source}", source)
            except Exception as exc:  # noqa: BLE001
                return f"توقف اضطراری انجام نشد: {exc}"[:200]
            return ("🛑 توقف اضطراری فعال شد. هیچ معاملهٔ تازه‌ای باز نمی‌شود.\n"
                    "برداشتن آن فقط از داشبورد و با کد دومرحله‌ای ممکن است.")
        if word in ("/help", "/start", "راهنما"):
            return ("دستورها:\n/status یا «وضعیت» — وضعیت ربات\n"
                    "/stop یا «توقف» — توقف اضطراری (معاملهٔ تازه باز نمی‌شود)\n"
                    "هیچ دستوری برای باز کردن معامله یا برداشتن توقف وجود ندارد؛ این عمدی است.")
        return None

    # -- the daily summary ----------------------------------------------------- #

    def status_text(self) -> str:
        if self.status_fn is None:
            return "وضعیت در دسترس نیست."
        try:
            s = self.status_fn() or {}
        except Exception as exc:  # noqa: BLE001
            return f"وضعیت خوانده نشد: {exc}"[:200]
        acct = s.get("account") or {}
        from .messages import MODE_FA
        lines = [f"🤖 حالت: {MODE_FA.get(s.get('mode'), s.get('mode'))} "
                 f"({'حساب واقعی' if s.get('venue_mode') == 'live' else 'آزمایشی/دمو'})"]
        if acct.get("equity") is not None:
            lines.append(f"💰 موجودی: {acct.get('equity')} {acct.get('currency', '')}")
        if s.get("day_pnl") is not None:
            lines.append(f"📅 نتیجهٔ امروز: {s.get('day_pnl')}")
        lines.append(f"📂 معامله‌های باز: {acct.get('open_positions', 0)}")
        if (s.get("kill_switch") or {}).get("engaged"):
            lines.append("🛑 توقف اضطراری: فعال")
        if s.get("halted"):
            lines.append(f"⛔ متوقف: {s.get('halt_reason', '')}"[:200])
        if s.get("cooldowns"):
            lines.append("😮‍💨 استراحت اجباری: " + "، ".join(s["cooldowns"]))
        return "\n".join(lines)

    def tick(self, now_ns: Optional[int] = None) -> None:
        now = int(now_ns or self._clock())
        t = dt.datetime.fromtimestamp(now / 1e9, tz=dt.timezone.utc)
        if t.hour != self.settings.get("daily_hour_utc", 17):
            return
        today = t.date().isoformat()
        if self.settings.get("daily_last") == today:
            return
        with self._lock:
            self.settings["daily_last"] = today
            self._save()
        self.enqueue(Message("daily", f"daily:{today}", "📊 گزارش روزانه\n" + self.status_text()))

    # -- bookkeeping ------------------------------------------------------------ #

    def _ok(self, kind: str) -> None:
        st = self.status[kind]
        st["sent"] += 1
        st["last_ok_ns"] = self._clock()
        st["last_error"] = ""

    def _fail(self, kind: str, error: str) -> None:
        st = self.status[kind]
        st["failed"] += 1
        st["last_error"] = error[:200]

    def describe(self, *, include_private: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {"channels": {}, "categories": list(CATEGORIES),
                               "daily_hour_utc": self.settings.get("daily_hour_utc", 17),
                               "token_storage": "ok" if self.secrets is not None else
                               "unavailable"}
        store = self.secrets
        for kind, ch in self.settings["channels"].items():
            row = {"label_fa": LABELS_FA[kind], **ch, **self.status[kind],
                   "token_stored": False}
            if store is not None:
                try:
                    row["token_stored"] = store.has(f"notify:{kind}")
                except Exception:  # noqa: BLE001
                    pass
            if not include_private:
                row["chat_id"] = "***" if row.get("chat_id") else ""
            out["channels"][kind] = row
        out["queued"] = len(self._queue)
        return out
