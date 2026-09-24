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

#: How much authority the System One model (Jev) has over the news filter.
#:   shadow      -- asked and recorded, NO effect (the default). A text model,
#:                  if one is configured, feeds the filter and is compared.
#:   shrink_only -- a correction can shrink size; nothing can block.
#:   active      -- corrections shrink and contradictions block (for up to 4 h).
#: "active" requires calibration evidence on the version currently answering.
JEV_MODES = ("shadow", "shrink_only", "active")
JEV_DEFAULT_STATE: Dict[str, Any] = {
    "mode": "shadow", "known_version": "", "known_since_ns": 0,
    "pending_version": "", "pending_since_ns": 0,
}
#: Evidence required before "active": labelled headlines on the CURRENT
#: version, and decision accuracy on each flag at its own threshold.
JEV_GATE_MIN_LABELS = 20
JEV_GATE_CONTRADICTION_ACCURACY = 0.90
JEV_GATE_CORRECTION_ACCURACY = 0.85
#: Back-off when a provider says "too many requests" (429) or "overloaded"
#: (529): skipped with no network traffic, 1 min doubling to 1 h, cleared by
#: the next success. A retry loop against a rate limit is how keys get banned.
BREAKER_STATUSES = (429, 529)
BREAKER_BASE_SEC = 60
BREAKER_MAX_SEC = 3600


def _clean_jev_state(raw: Any) -> Dict[str, Any]:
    state = dict(JEV_DEFAULT_STATE)
    if isinstance(raw, dict):
        if raw.get("mode") in JEV_MODES:
            state["mode"] = raw["mode"]
        for key in ("known_version", "pending_version"):
            if isinstance(raw.get(key), str) and len(raw[key]) <= 120:
                state[key] = raw[key]
        for key in ("known_since_ns", "pending_since_ns"):
            try:
                state[key] = max(0, int(raw.get(key) or 0))
            except (TypeError, ValueError):
                pass
    return state

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
    #: The System One model's operating state (see JEV_MODES and the version
    #: guard in AIService.classify_news).
    jev: Dict[str, Any] = field(default_factory=lambda: dict(JEV_DEFAULT_STATE))

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
                       if k in CATALOG and isinstance(v, dict)},
            jev=_clean_jev_state(data.get("jev")))


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
    CREATE TABLE IF NOT EXISTS jev_answers (
        article_id TEXT PRIMARY KEY, ts_ns INTEGER NOT NULL, version TEXT NOT NULL,
        mode TEXT, headline TEXT, currencies TEXT, event_type TEXT, direction TEXT,
        confidence REAL, confidence_known INTEGER, p_correction REAL, p_revision REAL,
        p_contradiction REAL, p_scheduled REAL, text_opinion TEXT);
    CREATE INDEX IF NOT EXISTS idx_jev_version ON jev_answers(version, ts_ns);
    CREATE TABLE IF NOT EXISTS jev_labels (
        article_id TEXT PRIMARY KEY, ts_ns INTEGER NOT NULL, labelled_by TEXT,
        is_correction INTEGER, contradicts_prior INTEGER, direction TEXT);
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

    # -- System One answers and the owner's labels --------------------------- #

    def save_jev_answer(self, row: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO jev_answers (article_id, ts_ns, version, mode, "
                "headline, currencies, event_type, direction, confidence, "
                "confidence_known, p_correction, p_revision, p_contradiction, p_scheduled, "
                "text_opinion) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "(SELECT text_opinion FROM jev_answers WHERE article_id = ?))",
                (row["article_id"], row.get("ts_ns") or wall_ns(), row["version"],
                 row.get("mode"), (row.get("headline") or "")[:300],
                 ",".join(row.get("currencies") or [])[:60], row.get("event_type"),
                 row.get("direction"), row.get("confidence"),
                 int(bool(row.get("confidence_known"))), row.get("p_correction"),
                 row.get("p_revision"), row.get("p_contradiction"), row.get("p_scheduled"),
                 row["article_id"]))
            cutoff = wall_ns() - 180 * 86_400 * 10**9
            self._conn.execute("DELETE FROM jev_answers WHERE ts_ns < ?", (cutoff,))
            self._conn.execute("DELETE FROM jev_labels WHERE article_id NOT IN "
                               "(SELECT article_id FROM jev_answers)")
            self._conn.commit()

    def attach_text_opinion(self, article_id: str, opinion: Dict[str, Any]) -> None:
        """A text model's flags on the same item: the automatic second opinion."""
        keep = {k: opinion.get(k) for k in ("model", "event_type", "direction_claim",
                                             "is_correction", "contradicts_prior",
                                             "confidence", "valid")}
        with self._lock:
            self._conn.execute("UPDATE jev_answers SET text_opinion = ? WHERE article_id = ?",
                               (json.dumps(keep, ensure_ascii=False), article_id))
            self._conn.commit()

    def jev_answers(self, *, version: Optional[str] = None,
                    limit: int = 2000) -> List[Dict[str, Any]]:
        sql = ("SELECT a.*, l.is_correction AS label_correction, "
               "l.contradicts_prior AS label_contradiction, l.direction AS label_direction, "
               "l.labelled_by, l.ts_ns AS labelled_ns FROM jev_answers a "
               "LEFT JOIN jev_labels l ON l.article_id = a.article_id")
        args: List[Any] = []
        if version is not None:
            sql += " WHERE a.version = ?"
            args.append(version)
        sql += " ORDER BY a.ts_ns DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["text_opinion"] = json.loads(d["text_opinion"]) if d["text_opinion"] else None
            except ValueError:
                d["text_opinion"] = None
            for k in ("label_correction", "label_contradiction"):
                d[k] = None if d[k] is None else bool(d[k])
            d["confidence_known"] = bool(d["confidence_known"])
            out.append(d)
        return out

    def jev_versions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.version, COUNT(*) n, MIN(a.ts_ns) first_ns, MAX(a.ts_ns) last_ns, "
                "COUNT(l.article_id) labelled FROM jev_answers a LEFT JOIN jev_labels l "
                "ON l.article_id = a.article_id GROUP BY a.version "
                "ORDER BY last_ns DESC").fetchall()
        return [dict(r) for r in rows]

    def save_jev_labels(self, labels: List[Dict[str, Any]], by: str) -> int:
        now = wall_ns()
        saved = 0
        with self._lock:
            for lab in labels:
                if self._conn.execute("SELECT 1 FROM jev_answers WHERE article_id = ?",
                                      (lab["article_id"],)).fetchone() is None:
                    continue
                def b(v):
                    return None if v is None else int(bool(v))
                self._conn.execute(
                    "INSERT OR REPLACE INTO jev_labels VALUES (?,?,?,?,?,?)",
                    (lab["article_id"], now, by[:64], b(lab.get("is_correction")),
                     b(lab.get("contradicts_prior")), lab.get("direction")))
                saved += 1
            self._conn.commit()
        return saved

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
        #: provider -> {"failures", "open_until_ns", "last_status", "last_ns"}
        self._breakers: Dict[str, Dict[str, Any]] = {}

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
        if CATALOG[pid].kind == "typesafe":
            try:
                client = ProviderClient(cfg, key, post=self._post, get=self._get)
                answers, usage, latency, served = client.system_one(
                    {"text": "The central bank raised its policy rate by 25 basis points."},
                    {"ping": {"type": "noul",
                              "instructions": "Does this text describe a rate increase?"}},
                    timeout=self.settings.timeout_sec)
                self._breaker_note(pid, None)
                out.update(ok=True, model=cfg.effective_model(), latency_ms=latency,
                           served_model=served or "(not reported)",
                           reply=str(answers.get("ping"))[:80],
                           models=client.list_models())
                self._journal(pid, cfg.effective_model(), "test", True,
                              result=AIResult("", pid, cfg.effective_model(), latency))
            except (ProviderError, ValueError) as exc:
                self._breaker_note(pid, exc)
                out["error"] = str(exc)
                self._journal(pid, cfg.effective_model(), "test", False, error=str(exc))
            return out
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
        chain = self.settings.chain()
        if not chain:
            return False, "no AI provider is enabled"
        if purpose != "news" and all(CATALOG[p].kind == "typesafe" for p in chain):
            return False, ("the enabled providers answer typed questions only; this "
                           "purpose needs a model that writes text")
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
            if CATALOG[pid].kind == "typesafe":
                continue            # answers questions, does not write text
            if self._breaker_open(pid):
                continue            # backing off after a 429/529; no traffic
            cfg = self.settings.provider(pid)
            key = self._key(pid)
            try:
                client = ProviderClient(cfg, key, post=self._post, get=self._get)
                result = client.complete(system, user, max_tokens=max_tokens,
                                         json_mode=json_mode,
                                         timeout=self.settings.timeout_sec)
            except (ProviderError, ValueError) as exc:
                self._breaker_note(pid, exc)
                self._journal(pid, cfg.effective_model(), purpose, False, error=str(exc),
                              prompt=user)
                continue
            self._breaker_note(pid, None)
            self._journal(pid, result.model, purpose, True, result=result, prompt=user)
            return result
        return None

    # -- rate-limit breaker ------------------------------------------------------ #

    def _breaker_open(self, pid: str) -> bool:
        b = self._breakers.get(pid)
        return bool(b) and wall_ns() < int(b.get("open_until_ns", 0))

    def _breaker_note(self, pid: str, exc: Optional[BaseException]) -> None:
        """Record an outcome. A 429/529 opens (or re-opens, longer) the breaker;
        any success closes it. Other failures leave it alone -- they are not a
        request to slow down."""
        now = wall_ns()
        with self._lock:
            b = self._breakers.setdefault(pid, {"failures": 0, "open_until_ns": 0,
                                                "last_status": None, "last_ns": 0})
            if exc is None:
                b.update(failures=0, open_until_ns=0, last_status=200, last_ns=now)
                return
            status = getattr(exc, "status", None)
            b.update(last_status=status, last_ns=now)
            if status in BREAKER_STATUSES:
                b["failures"] += 1
                wait = min(BREAKER_MAX_SEC, BREAKER_BASE_SEC * 2 ** (b["failures"] - 1))
                b["open_until_ns"] = now + int(wait * 1e9)

    def breakers(self) -> Dict[str, Dict[str, Any]]:
        now = wall_ns()
        with self._lock:
            return {pid: {**b, "open": now < int(b.get("open_until_ns", 0)),
                          "seconds_left": max(0, round((int(b.get("open_until_ns", 0)) - now)
                                                       / 1e9))}
                    for pid, b in self._breakers.items()}

    def structured_news_provider(self) -> Optional[str]:
        """The System One provider the news desk should use, if one leads.

        Only when it comes FIRST among the enabled providers: the owner's
        ordering decides, and a Jev key sitting behind a preferred text model
        does not silently take over the news path.
        """
        chain = self.settings.chain()
        if chain and CATALOG[chain[0]].kind == "typesafe":
            return chain[0]
        return None

    def classify_news(self, article_id: str, headline: str, summary: str,
                      currencies, *, training_cutoff: str = ""):
        """An ``Extraction`` from a System One model, or None to use the text path.

        What the extraction may DO is decided separately, by the operating
        mode (``jev_policy_view``): in the default shadow mode it is recorded
        and shown, and changes nothing.
        """
        from .jev import build_questions, extraction_from_answers

        pid = self.structured_news_provider()
        if pid is None:
            return None
        ok, _why = self.available("news")
        if not ok:
            return None
        if self._breaker_open(pid):
            return None
        budget = self._over_budget()
        if budget:
            self._journal("-", "-", "news", False, error=budget)
            return None
        cfg = self.settings.provider(pid)
        state = {"source_text": {"headline": headline[:500], "summary": summary[:3000]},
                 "currencies_the_source_covers": list(currencies or [])}
        try:
            client = ProviderClient(cfg, self._key(pid), post=self._post, get=self._get)
            answers, usage, latency, served = client.system_one(
                state, build_questions(), timeout=self.settings.timeout_sec)
        except (ProviderError, ValueError) as exc:
            self._breaker_note(pid, exc)
            self._journal(pid, cfg.effective_model(), "news", False, error=str(exc),
                          prompt=headline)
            return None
        self._breaker_note(pid, None)
        version = self._observe_jev_version(served, cfg.effective_model())
        result = AIResult(text="", provider=pid, model=served or cfg.effective_model(),
                          latency_ms=latency,
                          input_tokens=int(usage.get("input_tokens", 0) or 0),
                          output_tokens=int(usage.get("output_tokens", 0) or 0))
        self._journal(pid, result.model, "news", True, result=result, prompt=headline)
        ex = extraction_from_answers(article_id, headline, answers, model=result.model,
                                     currencies=currencies, latency_ms=latency,
                                     training_cutoff=training_cutoff)
        p = {v["name"]: v["value"] for v in ex.numeric_values}
        try:
            self.store.save_jev_answer({
                "article_id": article_id, "version": version, "mode": self.jev_mode(),
                "headline": headline, "currencies": list(currencies or []),
                "event_type": ex.event_type, "direction": ex.direction_claim,
                "confidence": ex.confidence, "confidence_known": bool(p.get("confidence_known")),
                "p_correction": p.get("p_is_correction"), "p_revision": p.get("p_is_revision"),
                "p_contradiction": p.get("p_contradicts_prior"),
                "p_scheduled": p.get("p_is_scheduled")})
        except Exception:  # noqa: BLE001 - recording must never cost the answer
            pass
        return ex

    # -- the System One model's authority ---------------------------------------- #

    def jev_mode(self) -> str:
        return self.settings.jev.get("mode", "shadow")

    def jev_policy_view(self, ex):
        """What the news POLICY may see of a System One extraction, by mode."""
        from dataclasses import replace

        mode = self.jev_mode()
        if ex is None or mode == "shadow":
            return None
        if mode == "shrink_only":
            return replace(ex, contradicts_prior=False)
        return ex

    def has_text_provider(self) -> bool:
        return any(CATALOG[p].kind != "typesafe" for p in self.settings.chain())

    def _observe_jev_version(self, served: str, configured: str) -> str:
        """Record which version answered; a CHANGE drops Jev to shadow mode.

        The thresholds (0.6 correction, 0.75 contradiction) and every label the
        owner has given were earned on one version. A floating alias such as
        ``jev-latest`` can move under them without anything in this system
        changing, so a new version is treated like a new model: recorded,
        journalled, demoted to shadow until the owner accepts it, and its
        calibration starts from zero.
        """
        key = served or f"unreported:{configured}"
        now = wall_ns()
        event = None
        with self._lock:
            st = self.settings.jev
            if not st.get("known_version"):
                st.update(known_version=key, known_since_ns=now)
                event = {"action": "jev_version_first_seen", "version": key}
            elif key != st["known_version"] and key != st.get("pending_version"):
                was = st.get("mode", "shadow")
                st.update(pending_version=key, pending_since_ns=now, mode="shadow")
                event = {"action": "jev_version_changed", "from": st["known_version"],
                         "to": key, "mode_was": was, "mode_now": "shadow"}
            if event is not None:
                self._save()
        if event is not None:
            try:
                self.audit.append("config.change", event, actor="ai")
            except Exception:  # noqa: BLE001
                pass
        return key

    def accept_jev_version(self, by: str) -> Dict[str, Any]:
        with self._lock:
            st = self.settings.jev
            pending = st.get("pending_version")
            if not pending:
                raise ValueError("no new Jev version is waiting for acceptance")
            previous = st.get("known_version")
            st.update(known_version=pending, known_since_ns=wall_ns(),
                      pending_version="", pending_since_ns=0, mode="shadow")
            self._save()
        self.audit.append("config.change", {"action": "jev_version_accepted",
                                            "from": previous, "to": pending,
                                            "mode": "shadow"}, actor=by or "owner")
        return self.jev_report()

    def set_jev_mode(self, mode: str, by: str) -> Dict[str, Any]:
        if mode not in JEV_MODES:
            raise ValueError(f"mode must be one of {', '.join(JEV_MODES)}")
        if mode != "shadow" and self.settings.jev.get("pending_version"):
            raise ValueError("a new Jev version is answering; accept it (and let it earn "
                             "its evidence) before giving it any authority")
        if mode == "active":
            gate = self.jev_report()["gate"]
            if not gate["passed"]:
                raise ValueError("not enough evidence for 'active' on this version: "
                                 + "; ".join(gate["missing"]))
        with self._lock:
            was = self.settings.jev.get("mode")
            self.settings.jev["mode"] = mode
            self._save()
        self.audit.append("config.change", {"action": "jev_mode", "from": was, "to": mode},
                          actor=by or "owner")
        return self.jev_report()

    def save_jev_labels(self, labels: List[Dict[str, Any]], by: str) -> Dict[str, Any]:
        from .jev import DIRECTIONS
        clean = []
        for lab in labels[:100]:
            aid = str(lab.get("article_id") or "")[:200]
            direction = lab.get("direction")
            if not aid or (direction is not None and direction not in DIRECTIONS):
                raise ValueError(f"invalid label for {aid or '?'}")
            clean.append({"article_id": aid, "is_correction": lab.get("is_correction"),
                          "contradicts_prior": lab.get("contradicts_prior"),
                          "direction": direction})
        saved = self.store.save_jev_labels(clean, by)
        self.audit.append("config.change", {"action": "jev_labels", "saved": saved},
                          actor=by or "owner")
        return self.jev_report()

    def jev_report(self) -> Dict[str, Any]:
        """Calibration, agreement and the promotion gate, for the current version."""
        from . import calibration as cal
        from .jev import CONTRADICTION_THRESHOLD, CORRECTION_THRESHOLD

        st = dict(self.settings.jev)
        version = st.get("known_version") or ""
        rows = self.store.jev_answers(version=version) if version else []
        corr = [(r["p_correction"], r["label_correction"]) for r in rows
                if r["p_correction"] is not None and r["label_correction"] is not None]
        contra = [(r["p_contradiction"], r["label_contradiction"]) for r in rows
                  if r["p_contradiction"] is not None and r["label_contradiction"] is not None]
        labelled = [r for r in rows if r["labelled_by"]]
        directions = [r for r in labelled if r["label_direction"]]
        dir_hits = [r for r in directions if r["direction"] == r["label_direction"]]
        known_conf = [r["confidence"] for r in directions if r["confidence_known"]]

        compared = [r for r in rows if isinstance(r["text_opinion"], dict)
                    and r["text_opinion"].get("valid")]

        def agree(fn):
            hits = [fn(r) for r in compared]
            hits = [h for h in hits if h is not None]
            return round(sum(hits) / len(hits), 3) if hits else None

        agreement = {
            "n": len(compared),
            "correction": agree(lambda r: ((r["p_correction"] or 0) >= CORRECTION_THRESHOLD)
                                == bool(r["text_opinion"].get("is_correction"))),
            "contradiction": agree(
                lambda r: ((r["p_contradiction"] or 0) >= CONTRADICTION_THRESHOLD)
                == bool(r["text_opinion"].get("contradicts_prior"))),
            "direction": agree(lambda r: r["direction"]
                               == r["text_opinion"].get("direction_claim")),
        }
        corr_s = cal.summarise(corr, CORRECTION_THRESHOLD)
        contra_s = cal.summarise(contra, CONTRADICTION_THRESHOLD)
        missing = []
        if st.get("pending_version"):
            missing.append("a new version is answering and has not been accepted")
        if len(labelled) < JEV_GATE_MIN_LABELS:
            missing.append(f"{len(labelled)}/{JEV_GATE_MIN_LABELS} labelled headlines on "
                           "this version")
        for name, summ, need in (("contradiction", contra_s, JEV_GATE_CONTRADICTION_ACCURACY),
                                 ("correction", corr_s, JEV_GATE_CORRECTION_ACCURACY)):
            acc = summ["decision"]["accuracy"]
            if acc is None or acc < need:
                missing.append(f"{name} accuracy {acc if acc is not None else '-'} "
                               f"< {need}")
        recent = self.store.jev_answers(limit=60)
        return {
            "mode": st.get("mode", "shadow"), "modes": list(JEV_MODES),
            "known_version": version, "known_since_ns": st.get("known_since_ns", 0),
            "pending_version": st.get("pending_version", ""),
            "pending_since_ns": st.get("pending_since_ns", 0),
            "floating_alias": self.settings.provider("jev").effective_model().endswith(
                "latest"),
            "answers": len(rows), "labelled": len(labelled),
            "correction": corr_s, "contradiction": contra_s,
            "direction": {"n": len(directions),
                          "accuracy": (round(len(dir_hits) / len(directions), 3)
                                       if directions else None),
                          "mean_confidence": (round(sum(known_conf) / len(known_conf), 3)
                                              if known_conf else None)},
            "agreement_with_text_model": agreement,
            "gate": {"passed": not missing, "missing": missing,
                     "min_labels": JEV_GATE_MIN_LABELS,
                     "contradiction_accuracy": JEV_GATE_CONTRADICTION_ACCURACY,
                     "correction_accuracy": JEV_GATE_CORRECTION_ACCURACY},
            "versions": self.store.jev_versions(),
            "breaker": self.breakers().get("jev"),
            "recent": [{k: r[k] for k in (
                "article_id", "ts_ns", "version", "mode", "headline", "currencies",
                "event_type", "direction", "confidence", "confidence_known",
                "p_correction", "p_contradiction", "label_correction",
                "label_contradiction", "label_direction", "text_opinion")}
                for r in recent],
        }

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
            "jev": {k: self.settings.jev.get(k) for k in ("mode", "known_version",
                                                          "pending_version")},
            "breakers": self.breakers(),
        }


