"""The AI service: configured providers, sealed keys, budgets and a call log.

One object owns every model call the system makes, so four properties hold in
exactly one place:

* **Keys are sealed.** API keys live in the same AES-256-GCM store as broker
  credentials (``brokers/secrets.py``), under the names ``ai:<provider>``, and
  are read only for the duration of a call. The API reports whether a key is
  stored and its last four characters -- never the key.
* **Budgets are enforced.** Hourly and daily call ceilings, per purpose
  switches, and the licence's ``llm_news`` capability. A runaway loop costs a
  refused call, not an invoice.
* **Failure degrades, never blocks.** The primary provider is tried first and
  each fallback in turn; if all fail the caller gets ``None`` and carries on
  exactly as it would with no AI configured. Nothing on the trading path waits
  on a model: the consumers run in a background worker and the agent only
  reads their cached results.
* **Every call is journalled** -- provider, model, purpose, latency, tokens,
  outcome, and a hash of the prompt -- without the prompt or the answer, which
  may contain account details.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import string
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..core.audit import AuditLog
from ..core.clock import wall_ns
from .providers import (
    CATALOG, AIResult, ProviderClient, ProviderConfig, ProviderError, validate_base_url,
)

#: What the models are used for. Each can be switched off on its own.
PURPOSES = ("news", "coach", "brief")

AI_EVENT = "ai.call"

_MODEL_CHARS = set(string.ascii_letters + string.digits + "._:-@")


@dataclass
class AISettings:
    primary: str = ""
    fallbacks: List[str] = field(default_factory=list)
    purposes: Dict[str, bool] = field(
        default_factory=lambda: {"news": True, "coach": True, "brief": True})
    max_calls_per_hour: int = 60
    max_calls_per_day: int = 400
    timeout_sec: float = 25.0
    providers: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def provider(self, pid: str) -> ProviderConfig:
        row = dict(self.providers.get(pid) or {})
        return ProviderConfig(id=pid, enabled=bool(row.get("enabled", False)),
                              model=str(row.get("model", "") or ""),
                              base_url=str(row.get("base_url", "") or ""))

    def chain(self) -> List[str]:
        order = [self.primary] + [p for p in self.fallbacks if p != self.primary]
        return [p for p in order if p in CATALOG and self.provider(p).enabled]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AISettings":
        base = cls()
        purposes = dict(base.purposes)
        purposes.update({k: bool(v) for k, v in (data.get("purposes") or {}).items()
                         if k in PURPOSES})
        return cls(
            primary=str(data.get("primary", "") or ""),
            fallbacks=[str(p) for p in (data.get("fallbacks") or []) if str(p) in CATALOG],
            purposes=purposes,
            max_calls_per_hour=max(0, min(10_000, int(data.get("max_calls_per_hour", 60)))),
            max_calls_per_day=max(0, min(100_000, int(data.get("max_calls_per_day", 400)))),
            timeout_sec=max(3.0, min(120.0, float(data.get("timeout_sec", 25.0)))),
            providers={k: dict(v) for k, v in (data.get("providers") or {}).items()
                       if k in CATALOG and isinstance(v, dict)})


class AIStore:
    """Call log, coach reviews, briefs and news extractions (SQLite, 0600)."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS calls (
        ts_ns INTEGER NOT NULL, provider TEXT, model TEXT, purpose TEXT, ok INTEGER,
        latency_ms REAL, input_tokens INTEGER, output_tokens INTEGER, error TEXT);
    CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts_ns);
    CREATE TABLE IF NOT EXISTS reviews (
        trade_id TEXT PRIMARY KEY, ts_ns INTEGER NOT NULL, strategy TEXT, instrument TEXT,
        r_multiple REAL, provider TEXT, model TEXT, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS briefs (
        ts_ns INTEGER NOT NULL, provider TEXT, model TEXT, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS extractions (
        article_id TEXT PRIMARY KEY, ts_ns INTEGER NOT NULL, published_ns INTEGER,
        source TEXT, headline TEXT, payload TEXT NOT NULL);
    """

    def __init__(self, path: str | Path) -> None:
        import os
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except (FileExistsError, OSError):
            pass
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    def log_call(self, row: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?)",
                (row["ts_ns"], row.get("provider"), row.get("model"), row.get("purpose"),
                 int(bool(row.get("ok"))), row.get("latency_ms"), row.get("input_tokens", 0),
                 row.get("output_tokens", 0), (row.get("error") or "")[:300]))
            cutoff = wall_ns() - 90 * 86_400 * 10**9
            self._conn.execute("DELETE FROM calls WHERE ts_ns < ?", (cutoff,))
            self._conn.commit()

    def count_since(self, since_ns: int) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM calls WHERE ts_ns >= ?",
                                     (since_ns,)).fetchone()
        return int(row[0])

    def usage(self) -> Dict[str, Any]:
        now = wall_ns()
        day = now - 86_400 * 10**9
        with self._lock:
            rows = self._conn.execute(
                "SELECT provider, purpose, COUNT(*) n, SUM(ok) ok, "
                "SUM(input_tokens) tin, SUM(output_tokens) tout, AVG(latency_ms) lat "
                "FROM calls WHERE ts_ns >= ? GROUP BY provider, purpose", (day,)).fetchall()
            recent = self._conn.execute(
                "SELECT * FROM calls ORDER BY ts_ns DESC LIMIT 30").fetchall()
        return {"last_24h": [dict(r) for r in rows], "recent": [dict(r) for r in recent]}

    def save_review(self, trade_id: str, strategy: str, instrument: str, r: float,
                    provider: str, model: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?,?,?,?)",
                (trade_id, wall_ns(), strategy, instrument, float(r), provider, model,
                 json.dumps(payload, ensure_ascii=False)))
            self._conn.commit()

    def reviewed(self, trade_id: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM reviews WHERE trade_id=?",
                                      (trade_id,)).fetchone() is not None

    def reviews(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM reviews ORDER BY ts_ns DESC LIMIT ?",
                                      (int(limit),)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def save_brief(self, provider: str, model: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO briefs VALUES (?,?,?,?)",
                               (wall_ns(), provider, model,
                                json.dumps(payload, ensure_ascii=False)))
            self._conn.execute(
                "DELETE FROM briefs WHERE ts_ns NOT IN "
                "(SELECT ts_ns FROM briefs ORDER BY ts_ns DESC LIMIT 50)")
            self._conn.commit()

    def latest_brief(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM briefs ORDER BY ts_ns DESC LIMIT 1").fetchone()
        return (dict(row) | {"payload": json.loads(row["payload"])}) if row else None

    def save_extraction(self, article_id: str, published_ns: Optional[int], source: str,
                        headline: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO extractions VALUES (?,?,?,?,?,?)",
                (article_id, wall_ns(), published_ns, source, headline[:300],
                 json.dumps(payload, ensure_ascii=False)))
            cutoff = wall_ns() - 30 * 86_400 * 10**9
            self._conn.execute("DELETE FROM extractions WHERE ts_ns < ?", (cutoff,))
            self._conn.commit()

    def has_extraction(self, article_id: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM extractions WHERE article_id=?",
                                      (article_id,)).fetchone() is not None

    def extractions(self, since_ns: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM extractions WHERE ts_ns >= ? ORDER BY ts_ns DESC LIMIT ?",
                (int(since_ns), int(limit))).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]


class AIService:
    """Owns settings, keys, budgets and the provider chain."""

    def __init__(self, state_dir: str | Path, audit: AuditLog, *,
                 secrets=None, capability_check: Optional[Callable[[str], tuple]] = None,
                 transport_post=None, transport_get=None) -> None:
        self.state_dir = Path(state_dir)
        self.audit = audit
        self._secrets = secrets
        self._secrets_error = ""
        self.capability_check = capability_check
        self._post = transport_post
        self._get = transport_get
        self.settings_path = self.state_dir / "ai.json"
        self.store = AIStore(self.state_dir / "ai.db")
        self._lock = threading.RLock()
        self.settings = self._load()

    # -- persistence ---------------------------------------------------------- #

    def _load(self) -> AISettings:
        try:
            return AISettings.from_dict(json.loads(self.settings_path.read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            return AISettings()

    def _save(self) -> None:
        import os
        import tempfile
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_dir), prefix="ai.json.", suffix=".tmp")
        try:
            os.write(fd, json.dumps(self.settings.to_dict(), indent=2,
                                    ensure_ascii=False).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.settings_path)

    @property
    def secrets(self):
        """The sealed store, shared with the broker credentials (same key)."""
        if self._secrets is None and not self._secrets_error:
            from ..brokers.secrets import SecretStore, SecretStoreError
            try:
                self._secrets = SecretStore(
                    self.state_dir / "ai-secrets.json",
                    key_path=self.state_dir / "broker-secrets.key")
            except SecretStoreError as exc:
                self._secrets_error = str(exc)
        return self._secrets

    def _key(self, pid: str) -> str:
        store = self.secrets
        if store is None:
            return ""
        try:
            return store.get(f"ai:{pid}") or ""
        except Exception:  # noqa: BLE001 - an unreadable key is an absent key
            return ""

    # -- owner operations ------------------------------------------------------- #

    def save_provider(self, pid: str, *, enabled: bool, model: str = "",
                      base_url: str = "", api_key: Optional[str] = None,
                      by: str = "") -> Dict[str, Any]:
        if pid not in CATALOG:
            raise ValueError(f"unknown provider {pid!r}")
        model = (model or "").strip()
        # The model id travels inside a URL path for Gemini, so it is held to
        # the characters real model ids use. "/" is allowed only for a custom
        # (OpenAI-compatible) server, where ids like "org/model" are normal and
        # the id travels in the JSON body, never the path.
        allowed = _MODEL_CHARS | ({"/"} if pid == "custom" else set())
        if len(model) > 120 or not set(model) <= allowed or ".." in model:
            raise ValueError("the model name contains characters a model id cannot have")
        base = ""
        if pid == "custom":
            base = validate_base_url(base_url)
        elif base_url:
            # Built-in providers may only be re-pointed at an https host of the
            # SAME provider (e.g. Moonshot's .cn endpoint); anything else is a
            # custom provider and must be configured as one.
            base = validate_base_url(base_url)
            if not base.startswith("https://"):
                raise ValueError("a built-in provider must use https")
        if api_key is not None:
            store = self.secrets
            if store is None:
                raise ValueError("API keys cannot be stored: " + (self._secrets_error or
                                                                 "no credential key"))
            key = api_key.strip()
            if key and (len(key) < 8 or len(key) > 400 or any(c.isspace() for c in key)):
                raise ValueError("that does not look like an API key")
            if key:
                store.put(f"ai:{pid}", key)
            else:
                store.delete(f"ai:{pid}")
        with self._lock:
            self.settings.providers[pid] = {"enabled": bool(enabled), "model": model,
                                            "base_url": base}
            if enabled and not self.settings.primary:
                self.settings.primary = pid
            self._save()
        self.audit.append("config.change", {
            "action": "ai_provider_saved", "provider": pid, "enabled": bool(enabled),
            "model": model or CATALOG[pid].default_model,
            "key_changed": api_key is not None}, actor=by or "owner")
        return self.describe(include_private=True)

    def save_settings(self, *, primary: str, fallbacks: List[str],
                      purposes: Dict[str, bool], max_calls_per_hour: int,
                      max_calls_per_day: int, by: str = "") -> Dict[str, Any]:
        if primary and primary not in CATALOG:
            raise ValueError(f"unknown provider {primary!r}")
        with self._lock:
            merged = self.settings.to_dict()
            merged.update({"primary": primary, "fallbacks": fallbacks,
                           "purposes": purposes, "max_calls_per_hour": max_calls_per_hour,
                           "max_calls_per_day": max_calls_per_day})
            self.settings = AISettings.from_dict(merged)
            self._save()
        self.audit.append("config.change", {
            "action": "ai_settings_saved", "primary": primary, "fallbacks": fallbacks,
            "purposes": purposes}, actor=by or "owner")
        return self.describe(include_private=True)

    def test_provider(self, pid: str, by: str = "") -> Dict[str, Any]:
        """A tiny real call plus the provider's model list."""
        cfg = self.settings.provider(pid)
        key = self._key(pid)
        out: Dict[str, Any] = {"provider": pid, "ok": False}
        try:
            client = ProviderClient(cfg, key, post=self._post, get=self._get)
            result = client.complete(
                "Reply with the JSON object {\"ok\": true} and nothing else.",
                "ping", max_tokens=20, json_mode=True, timeout=self.settings.timeout_sec)
            out.update(ok=True, model=result.model, latency_ms=result.latency_ms,
                       reply=result.text[:80])
            self._journal(pid, result.model, "test", True, result=result)
        except (ProviderError, ValueError) as exc:
            out["error"] = str(exc)
            self._journal(pid, cfg.effective_model(), "test", False, error=str(exc))
            return out
        try:
            out["models"] = client.list_models(timeout=self.settings.timeout_sec)
        except ProviderError as exc:
            out["models"] = []
            out["models_error"] = str(exc)
        return out

    # -- the question every consumer asks ------------------------------------- #

    def available(self, purpose: str) -> tuple:
        if purpose not in PURPOSES:
            return False, f"unknown purpose {purpose!r}"
        if not self.settings.purposes.get(purpose, False):
            return False, f"AI use for '{purpose}' is switched off"
        if self.capability_check is not None:
            try:
                ok, why = self.capability_check("llm_news")
            except Exception as exc:  # noqa: BLE001
                ok, why = False, str(exc)
            if not ok:
                return False, f"the licence does not include AI features ({why})"
        if not self.settings.chain():
            return False, "no AI provider is enabled"
        return True, ""

    def _over_budget(self) -> Optional[str]:
        now = wall_ns()
        hour = self.store.count_since(now - 3600 * 10**9)
        if hour >= self.settings.max_calls_per_hour:
            return f"hourly AI budget reached ({self.settings.max_calls_per_hour})"
        day = self.store.count_since(now - 86_400 * 10**9)
        if day >= self.settings.max_calls_per_day:
            return f"daily AI budget reached ({self.settings.max_calls_per_day})"
        return None

    def complete(self, purpose: str, system: str, user: str, *, max_tokens: int = 800,
                 json_mode: bool = False) -> Optional[AIResult]:
        """The answer from the first provider in the chain that gives one."""
        ok, _why = self.available(purpose)
        if not ok:
            return None
        budget = self._over_budget()
        if budget:
            self._journal("-", "-", purpose, False, error=budget)
            return None
        for pid in self.settings.chain():
            cfg = self.settings.provider(pid)
            key = self._key(pid)
            try:
                client = ProviderClient(cfg, key, post=self._post, get=self._get)
                result = client.complete(system, user, max_tokens=max_tokens,
                                         json_mode=json_mode,
                                         timeout=self.settings.timeout_sec)
            except (ProviderError, ValueError) as exc:
                self._journal(pid, cfg.effective_model(), purpose, False, error=str(exc),
                              prompt=user)
                continue
            self._journal(pid, result.model, purpose, True, result=result, prompt=user)
            return result
        return None

    def caller(self, purpose: str) -> Callable[[str, str], str]:
        """A ``(system, user) -> text`` function for NewsExtractor and friends."""
        def call(system: str, user: str) -> str:
            result = self.complete(purpose, system, user, max_tokens=900, json_mode=True)
            if result is None:
                raise ProviderError("no AI provider answered")
            return result.text
        return call

    def _journal(self, provider: str, model: str, purpose: str, ok: bool, *,
                 result: Optional[AIResult] = None, error: str = "",
                 prompt: str = "") -> None:
        row = {"ts_ns": wall_ns(), "provider": provider, "model": model,
               "purpose": purpose, "ok": ok,
               "latency_ms": result.latency_ms if result else None,
               "input_tokens": result.input_tokens if result else 0,
               "output_tokens": result.output_tokens if result else 0,
               "error": error[:300]}
        try:
            self.store.log_call(row)
        except Exception:  # noqa: BLE001 - a log failure must not fail the call
            pass
        payload = {k: v for k, v in row.items() if k != "ts_ns"}
        if prompt:
            payload["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        try:
            self.audit.append(AI_EVENT, payload, actor="ai")
        except Exception:  # noqa: BLE001
            pass

    # -- read model ------------------------------------------------------------ #

    def describe(self, *, include_private: bool = False) -> Dict[str, Any]:
        providers = []
        store = self.secrets
        for pid, spec in CATALOG.items():
            cfg = self.settings.provider(pid)
            row: Dict[str, Any] = {
                "id": pid, "label": spec.label, "label_fa": spec.label_fa,
                "enabled": cfg.enabled, "model": cfg.effective_model(),
                "default_model": spec.default_model, "notes_fa": spec.notes_fa,
                "console_url": spec.console_url, "key_prefix_hint": spec.key_prefix_hint,
                "key_stored": False, "key_last4": None,
            }
            if store is not None:
                try:
                    has = store.has(f"ai:{pid}")
                    row["key_stored"] = has
                    if has and include_private:
                        key = self._key(pid)
                        row["key_last4"] = key[-4:] if len(key) >= 8 else None
                except Exception:  # noqa: BLE001
                    pass
            if include_private:
                row["base_url"] = cfg.effective_base_url()
            providers.append(row)
        ok_any = {p: self.available(p) for p in PURPOSES}
        return {
            "providers": providers,
            "primary": self.settings.primary,
            "fallbacks": list(self.settings.fallbacks),
            "purposes": dict(self.settings.purposes),
            "max_calls_per_hour": self.settings.max_calls_per_hour,
            "max_calls_per_day": self.settings.max_calls_per_day,
            "available": {p: {"ok": v[0], "reason": v[1]} for p, v in ok_any.items()},
            "key_storage": ("ok" if store is not None else
                            "unavailable" + (f": {self._secrets_error}"
                                             if include_private else "")),
            "usage": self.store.usage() if include_private else None,
            "calls_last_hour": self.store.count_since(wall_ns() - 3600 * 10**9),
        }


