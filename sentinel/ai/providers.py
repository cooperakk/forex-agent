"""Language-model providers behind one small interface.

Supported out of the box:

==========  ======================  =============================================
id          provider                wire format
==========  ======================  =============================================
anthropic   Claude (Anthropic)      Messages API (``/v1/messages``)
openai      ChatGPT (OpenAI)        Chat Completions (``/v1/chat/completions``)
gemini      Gemini (Google)         ``models/{model}:generateContent``
deepseek    DeepSeek                OpenAI-compatible
kimi        Kimi (Moonshot AI)      OpenAI-compatible
custom      any compatible server   OpenAI-compatible, owner-supplied https URL
==========  ======================  =============================================

What a provider is allowed to be, and what it is not
----------------------------------------------------
A model here turns text into typed, checkable fields and into explanations
for a human. It never places an order, never sizes one, never widens a limit
and never promotes a strategy: every consumer in ``sentinel.ai`` can only
*shrink* risk (news filter) or *inform* a person (reviews, briefs). A model
that is wrong, slow, down or prompt-injected costs a missed trade or a bad
paragraph -- never an exposure.

Security properties of this module:

* Keys are passed in per call from the sealed credential store and are never
  logged, returned by the API, or included in an error message (any echo of
  the key in a provider's error body is redacted).
* Built-in providers call fixed HTTPS endpoints. A custom endpoint must be
  ``https://`` (plain ``http`` only to loopback, for a local model server) and
  may not embed credentials.
* Redirects are NOT followed: a redirect would carry the key to wherever the
  ``Location`` header points.
* Every call has a hard timeout and a response-size cap.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

#: Longest text accepted back from a provider. An extraction is a few hundred
#: characters; a brief is a few thousand. Anything larger is a malfunction.
MAX_RESPONSE_CHARS = 24_000


class ProviderError(RuntimeError):
    """A provider call failed. The message never contains the API key."""


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    label_fa: str
    kind: str                     # anthropic | openai | gemini
    base_url: str
    default_model: str
    key_prefix_hint: str = ""
    console_url: str = ""
    notes_fa: str = ""


CATALOG: Dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        "anthropic", "Claude (Anthropic)", "کلاد (Anthropic)", "anthropic",
        "https://api.anthropic.com", "claude-sonnet-5", "sk-ant-",
        "https://console.anthropic.com/settings/keys",
        "دقیق در استخراج ساختاریافته و توضیح فارسی."),
    "openai": ProviderSpec(
        "openai", "ChatGPT (OpenAI)", "چت‌جی‌پی‌تی (OpenAI)", "openai",
        "https://api.openai.com/v1", "gpt-5-mini", "sk-",
        "https://platform.openai.com/api-keys",
        "کلید را از بخش API Keys حساب OpenAI بسازید."),
    "gemini": ProviderSpec(
        "gemini", "Gemini (Google)", "جمینای (گوگل)", "gemini",
        "https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash", "AIza",
        "https://aistudio.google.com/app/apikey",
        "کلید رایگان/پولی از Google AI Studio."),
    "deepseek": ProviderSpec(
        "deepseek", "DeepSeek", "دیپ‌سیک", "openai",
        "https://api.deepseek.com/v1", "deepseek-chat", "sk-",
        "https://platform.deepseek.com/api_keys",
        "ارزان؛ مناسب برای تحلیل پرتعداد اخبار."),
    "kimi": ProviderSpec(
        "kimi", "Kimi (Moonshot AI)", "کیمی (Moonshot)", "openai",
        "https://api.moonshot.ai/v1", "kimi-k2-turbo-preview", "sk-",
        "https://platform.moonshot.ai/console/api-keys",
        "برای حساب‌های چینی، نشانی را به api.moonshot.cn تغییر دهید (گزینهٔ سفارشی)."),
    "custom": ProviderSpec(
        "custom", "Custom (OpenAI-compatible)", "سرویس سفارشی (سازگار با OpenAI)",
        "openai", "", "", "",
        "", "هر سرویس سازگار با OpenAI، مثلاً Qwen، Grok یا یک مدل محلی."),
}


@dataclass
class AIResult:
    text: str
    provider: str
    model: str
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderConfig:
    """What the owner chose for one provider. The key is NOT here."""

    id: str
    enabled: bool = False
    model: str = ""
    base_url: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def spec(self) -> ProviderSpec:
        return CATALOG[self.id]

    def effective_model(self) -> str:
        return (self.model or self.spec().default_model).strip()

    def effective_base_url(self) -> str:
        return (self.base_url or self.spec().base_url).strip().rstrip("/")


def validate_base_url(url: str) -> str:
    """Refuse endpoints that could leak the key or reach the wrong host."""
    text = (url or "").strip().rstrip("/")
    if not text:
        raise ValueError("an endpoint URL is required for a custom provider")
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https", "http"):
        raise ValueError("the endpoint must start with https://")
    if parsed.scheme == "http" and host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("plain http is only allowed to this machine (a local model); "
                         "anything else must use https so the key is encrypted in transit")
    if parsed.username or parsed.password:
        raise ValueError("the endpoint URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("the endpoint URL must not contain ? or #")
    if not host:
        raise ValueError("the endpoint URL has no host")
    return text


def _redact(text: str, secret: str) -> str:
    out = str(text or "")
    if secret:
        out = out.replace(secret, "[redacted]")
        if len(secret) > 12:
            out = out.replace(secret[:12], "[redacted]")
    return out[:400]


Transport = Callable[..., Any]


def _default_post(url: str, *, headers: Dict[str, str], json_body: Dict[str, Any],
                  timeout: float) -> tuple:
    import httpx
    response = httpx.post(url, headers=headers, json=json_body, timeout=timeout,
                          follow_redirects=False)
    return response.status_code, response.text


def _default_get(url: str, *, headers: Dict[str, str], timeout: float) -> tuple:
    import httpx
    response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=False)
    return response.status_code, response.text


class ProviderClient:
    """One call to one provider. Stateless apart from the injected transport."""

    def __init__(self, config: ProviderConfig, api_key: str, *,
                 post: Optional[Transport] = None, get: Optional[Transport] = None) -> None:
        if config.id not in CATALOG:
            raise ValueError(f"unknown AI provider {config.id!r}")
        if not api_key and config.id != "custom":
            raise ProviderError(f"no API key is stored for {config.spec().label}")
        self.config = config
        self.api_key = api_key or ""
        self._post = post or _default_post
        self._get = get or _default_get
        self.base_url = config.effective_base_url()
        if config.id == "custom":
            self.base_url = validate_base_url(self.base_url)
        self.model = config.effective_model()
        if not self.model:
            raise ProviderError("no model is configured for this provider")

    # -- request shapes ------------------------------------------------------ #

    def _request(self, system: str, user: str, max_tokens: int,
                 json_mode: bool) -> tuple:
        kind = self.config.spec().kind
        if kind == "anthropic":
            url = f"{self.base_url}/v1/messages"
            headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
            body: Dict[str, Any] = {
                "model": self.model, "max_tokens": int(max_tokens), "system": system,
                "messages": [{"role": "user", "content": user}]}
            return url, headers, body
        if kind == "gemini":
            url = f"{self.base_url}/models/{self.model}:generateContent"
            headers = {"x-goog-api-key": self.api_key, "content-type": "application/json"}
            generation: Dict[str, Any] = {"maxOutputTokens": int(max_tokens)}
            if json_mode:
                generation["responseMimeType"] = "application/json"
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": generation}
            return url, headers, body
        # OpenAI-compatible (openai, deepseek, kimi, custom)
        url = f"{self.base_url}/chat/completions"
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        body = {"model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if self.config.id == "openai":
            # Current OpenAI models take max_completion_tokens and reject a
            # non-default temperature, so neither max_tokens nor temperature
            # is sent to them.
            body["max_completion_tokens"] = int(max_tokens)
        else:
            body["max_tokens"] = int(max_tokens)
            body["temperature"] = 0
        if json_mode and self.config.id != "custom":
            body["response_format"] = {"type": "json_object"}
        return url, headers, body

    @staticmethod
    def _parse(kind: str, data: Dict[str, Any]) -> tuple:
        if kind == "anthropic":
            parts = [c.get("text", "") for c in data.get("content", [])
                     if isinstance(c, dict) and c.get("type") == "text"]
            usage = data.get("usage") or {}
            return ("".join(parts), int(usage.get("input_tokens", 0) or 0),
                    int(usage.get("output_tokens", 0) or 0))
        if kind == "gemini":
            candidates = data.get("candidates") or []
            if not candidates:
                reason = (data.get("promptFeedback") or {}).get("blockReason", "")
                raise ProviderError(f"the model returned no answer {reason}".strip())
            parts = (candidates[0].get("content") or {}).get("parts") or []
            usage = data.get("usageMetadata") or {}
            return ("".join(p.get("text", "") for p in parts if isinstance(p, dict)),
                    int(usage.get("promptTokenCount", 0) or 0),
                    int(usage.get("candidatesTokenCount", 0) or 0))
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("the model returned no choices")
        message = choices[0].get("message") or {}
        usage = data.get("usage") or {}
        return (str(message.get("content") or ""),
                int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0))

    # -- the calls ------------------------------------------------------------ #

    def complete(self, system: str, user: str, *, max_tokens: int = 800,
                 json_mode: bool = False, timeout: float = 20.0) -> AIResult:
        import json as _json
        url, headers, body = self._request(system, user, max_tokens, json_mode)
        started = time.monotonic()
        try:
            status, text = self._post(url, headers=headers, json_body=body,
                                      timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - network failures are data
            raise ProviderError(_redact(f"{type(exc).__name__}: {exc}", self.api_key)) \
                from None
        latency = (time.monotonic() - started) * 1000.0
        if status in (301, 302, 303, 307, 308):
            raise ProviderError("the provider answered with a redirect, which is not "
                                "followed (it would carry the API key elsewhere)")
        if status >= 400:
            raise ProviderError(_redact(f"HTTP {status}: {text}", self.api_key))
        try:
            data = _json.loads(text)
        except ValueError:
            raise ProviderError("the provider did not answer with JSON") from None
        if not isinstance(data, dict):
            raise ProviderError("the provider's answer is not a JSON object")
        content, tokens_in, tokens_out = self._parse(self.config.spec().kind, data)
        return AIResult(text=content[:MAX_RESPONSE_CHARS], provider=self.config.id,
                        model=self.model, latency_ms=round(latency, 1),
                        input_tokens=tokens_in, output_tokens=tokens_out)

    def list_models(self, *, timeout: float = 15.0) -> List[str]:
        import json as _json
        kind = self.config.spec().kind
        if kind == "anthropic":
            url = f"{self.base_url}/v1/models"
            headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        elif kind == "gemini":
            url = f"{self.base_url}/models"
            headers = {"x-goog-api-key": self.api_key}
        else:
            url = f"{self.base_url}/models"
            headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            status, text = self._get(url, headers=headers, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(_redact(f"{type(exc).__name__}: {exc}", self.api_key)) \
                from None
        if status >= 400:
            raise ProviderError(_redact(f"HTTP {status}: {text}", self.api_key))
        try:
            data = _json.loads(text)
        except ValueError:
            raise ProviderError("the provider did not answer with JSON") from None
        rows = data.get("data") or data.get("models") or []
        names: List[str] = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                name = str(row.get("id") or row.get("name") or "")
                if name.startswith("models/"):
                    name = name[len("models/"):]
                if name:
                    names.append(name)
        return sorted(set(names))[:300]
