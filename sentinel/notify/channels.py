"""Telegram and Bale bot transports.

Both speak the same Bot API shape: ``POST https://<host>/bot<token>/<method>``
with a JSON body, answering ``{"ok": true, "result": ...}`` or
``{"ok": false, "error_code": ..., "description": ...}``. Bale (the Iranian
messenger) implements the Telegram Bot API at ``tapi.bale.ai``, so one client
serves both; only the host differs.

Security properties:

* the token travels only in the URL path of an HTTPS request to a FIXED host
  (no configurable base URL, so a settings change cannot redirect it);
* redirects are not followed; responses are size- and time-capped;
* every error message is scrubbed of the token before it is logged or shown;
* chat ids and tokens are validated before use.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

HOSTS = {"telegram": "https://api.telegram.org", "bale": "https://tapi.bale.ai"}
LABELS_FA = {"telegram": "تلگرام", "bale": "بله"}
TOKEN_RE = re.compile(r"^\d{3,15}:[A-Za-z0-9_-]{16,80}$")
CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{5,32})$")
MAX_TEXT = 3900                     # Telegram's limit is 4096 characters
MAX_RESPONSE = 1_000_000

Transport = Callable[..., Tuple[int, str]]


class ChannelError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def _default_post(url: str, *, json_body: Dict[str, Any], timeout: float) -> Tuple[int, str]:
    import httpx
    with httpx.stream("POST", url, json=json_body, timeout=timeout,
                      follow_redirects=False) as resp:
        chunks, total = [], 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE:
                raise ChannelError("the bot API answered with more than 1 MB")
            chunks.append(chunk)
        return resp.status_code, b"".join(chunks).decode("utf-8", errors="replace")


def validate_token(token: str) -> str:
    t = (token or "").strip()
    if not TOKEN_RE.match(t):
        raise ValueError("that does not look like a bot token (digits:letters, from BotFather)")
    return t


def validate_chat(chat_id: str) -> str:
    c = str(chat_id or "").strip()
    if not CHAT_RE.match(c):
        raise ValueError("a chat id is a number (e.g. 123456789 or -100...) or @channelname")
    return c


def split_text(text: str, limit: int = MAX_TEXT) -> List[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            parts.append(cur + line[: limit - len(cur)])
            line, cur = line[limit - len(cur):], ""
        if len(cur) + len(line) > limit:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    return parts


class BotChannel:
    def __init__(self, kind: str, token: str, *, post: Optional[Transport] = None,
                 timeout: float = 15.0) -> None:
        if kind not in HOSTS:
            raise ValueError(f"unknown channel {kind!r}")
        self.kind = kind
        self.token = validate_token(token)
        self._post = post or _default_post
        self.timeout = timeout

    def _scrub(self, text: str) -> str:
        return str(text).replace(self.token, "***")

    def call(self, method: str, payload: Dict[str, Any], *,
             timeout: Optional[float] = None) -> Any:
        url = f"{HOSTS[self.kind]}/bot{self.token}/{method}"
        try:
            status, body = self._post(url, json_body=payload,
                                      timeout=timeout or self.timeout)
        except ChannelError:
            raise
        except Exception as exc:  # noqa: BLE001 - network failures are data
            raise ChannelError(self._scrub(f"{type(exc).__name__}: {exc}")[:300]) from None
        if status in (301, 302, 303, 307, 308):
            raise ChannelError("the bot API answered with a redirect, which is not followed",
                               status=status)
        try:
            data = json.loads(body)
        except ValueError:
            raise ChannelError(f"HTTP {status}: not JSON", status=status) from None
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else ""
            code = data.get("error_code") if isinstance(data, dict) else status
            raise ChannelError(self._scrub(f"{code}: {desc}")[:300],
                               status=int(code) if isinstance(code, int) else status)
        return data.get("result")

    def send(self, chat_id: str, text: str) -> int:
        chat = validate_chat(chat_id)
        sent = 0
        for part in split_text(text):
            self.call("sendMessage", {"chat_id": chat, "text": part,
                                      "disable_web_page_preview": True})
            sent += 1
        return sent

    def updates(self, offset: int = 0, timeout: int = 0) -> List[Dict[str, Any]]:
        result = self.call("getUpdates", {"offset": int(offset), "timeout": int(timeout),
                                          "allowed_updates": ["message"]},
                           timeout=float(timeout) + 10.0)
        return result if isinstance(result, list) else []

    def me(self) -> Dict[str, Any]:
        result = self.call("getMe", {})
        return result if isinstance(result, dict) else {}
