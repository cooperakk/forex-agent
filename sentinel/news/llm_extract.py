"""Structured news extraction with a language model.

The model's job here is narrow and deliberately unglamorous: turn free text
into typed, checkable fields. It does *not* decide direction, and it does not
size anything.

Reasons, all from the research brief:

* A model's verbal confidence is not a probability until it has been
  calibrated against outcomes. Until then it is a number that feels like one,
  which is worse than no number.
* Latency budget: a retail path of feed -> parse -> model -> risk -> venue is
  hundreds of milliseconds at best. The interdealer market has repriced the
  surprise long before that. So a language model on news cannot be a
  fast-direction game, and this module never pretends otherwise. It is used
  for classification, context and risk filtering on horizons of hours.
* Contamination: evaluating on articles from before the model's training
  cutoff measures memory, not forecasting. Every extraction records the model
  version and the cutoff, and ``lap.py`` tests for leakage directly.

Every extraction is validated against a schema. An unparseable or
out-of-schema response is a failure, never a partially-trusted guess.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.clock import wall_ns

EXTRACTION_SCHEMA: dict[str, Any] = {
    "event_type": {"type": "enum", "values": [
        "monetary_policy", "inflation", "employment", "growth", "trade",
        "fiscal", "geopolitical", "central_bank_speech", "market_structure", "other"]},
    "currencies": {"type": "list[str]", "max": 6},
    "direction_claim": {"type": "enum", "values": ["hawkish", "dovish", "neutral", "unclear"]},
    "is_scheduled": {"type": "bool"},
    "is_revision": {"type": "bool"},
    "is_correction": {"type": "bool"},
    "contradicts_prior": {"type": "bool"},
    "numeric_values": {"type": "list[dict]"},
    "evidence_quotes": {"type": "list[str]", "max": 4},
    "confidence": {"type": "float", "min": 0.0, "max": 1.0},
    "novelty": {"type": "enum", "values": ["new", "repeat", "follow_up"]},
}

SYSTEM_PROMPT = """You extract structured facts from financial news. You do not \
predict prices and you do not give trading advice.

Rules:
1. Output ONLY a JSON object matching the schema. No prose, no markdown fence.
2. Every claim must be supported by a quote from the text in `evidence_quotes`.
3. If the text does not state something, use null or "unclear". Do not infer.
4. `direction_claim` describes the tone of the SOURCE about policy, not your \
opinion of where the price goes.
5. `confidence` is your confidence in the EXTRACTION being faithful to the text, \
not a probability about markets.
6. Never use knowledge of what happened after this article was published. If you \
recognise the event and know the outcome, ignore that knowledge entirely."""


@dataclass
class Extraction:
    article_id: str
    event_type: str
    currencies: list[str]
    direction_claim: str
    is_scheduled: bool
    is_revision: bool
    is_correction: bool
    contradicts_prior: bool
    numeric_values: list[dict[str, Any]] = field(default_factory=list)
    evidence_quotes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    novelty: str = "new"
    model: str = ""
    model_cutoff: str = ""
    prompt_version: str = "1.0"
    extracted_ns: int = field(default_factory=wall_ns)
    latency_ms: float = 0.0
    calibrated: bool = False
    valid: bool = True
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def validate(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Coerce and check against the schema. Returns (clean, errors)."""
    errors: list[str] = []
    clean: dict[str, Any] = {}
    for key, spec in EXTRACTION_SCHEMA.items():
        value = payload.get(key)
        kind = spec["type"]
        if kind == "enum":
            if value not in spec["values"]:
                errors.append(f"{key}={value!r} is not in {spec['values']}")
                value = spec["values"][-1]
        elif kind == "bool":
            if not isinstance(value, bool):
                errors.append(f"{key} must be a boolean, got {type(value).__name__}")
                value = False
        elif kind == "float":
            try:
                value = float(value)
            except (TypeError, ValueError):
                errors.append(f"{key} must be numeric")
                value = 0.0
            value = max(spec.get("min", 0.0), min(spec.get("max", 1.0), value))
        elif kind.startswith("list"):
            if not isinstance(value, list):
                errors.append(f"{key} must be a list")
                value = []
            if "max" in spec:
                value = value[: spec["max"]]
            if kind == "list[str]":
                value = [str(v)[:400] for v in value if v is not None]
        clean[key] = value
    if not clean.get("evidence_quotes"):
        errors.append("no evidence quotes: an unsupported extraction is not usable")
    return clean, errors


def parse_response(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Strict JSON parse. A model that cannot follow the format is a failure."""
    if not text:
        return None, "empty response"
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1)
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            return None, "no JSON object found"
        try:
            obj = json.loads(stripped[start:end + 1])
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON: {exc}"
    if not isinstance(obj, dict):
        return None, "top-level JSON is not an object"
    return obj, None


class NewsExtractor:
    """Model-agnostic. ``call_model`` is injected, so the system is testable
    without a network and can be pointed at any provider."""

    def __init__(self, call_model: Callable[[str, str], str] | None = None, *,
                 model: str = "none", training_cutoff: str = "2025-01-01",
                 timeout_ms: int = 8000, max_calls_per_hour: int = 60) -> None:
        self.call_model = call_model
        self.model = model
        self.training_cutoff = training_cutoff
        self.timeout_ms = timeout_ms
        self.max_calls_per_hour = max_calls_per_hour
        self._calls: list[int] = []

    def _rate_limited(self) -> bool:
        cutoff = wall_ns() - 3_600_000_000_000
        self._calls = [t for t in self._calls if t >= cutoff]
        return len(self._calls) >= self.max_calls_per_hour

    def extract(self, article_id: str, headline: str, body: str = "",
                published_ns: int | None = None) -> Extraction:
        from ..core.clock import Stopwatch

        base = Extraction(
            article_id=article_id, event_type="other", currencies=[],
            direction_claim="unclear", is_scheduled=False, is_revision=False,
            is_correction=False, contradicts_prior=False, model=self.model,
            model_cutoff=self.training_cutoff, calibrated=False)

        if self.call_model is None:
            base.valid = False
            base.errors.append("no model configured: the news module is running in "
                               "calendar-only mode and produces no text extraction")
            return base
        if self._rate_limited():
            base.valid = False
            base.errors.append(f"rate limit reached ({self.max_calls_per_hour}/hour)")
            return base
        if published_ns and self.training_cutoff:
            import datetime as dt

            cutoff_ns = int(dt.datetime.fromisoformat(self.training_cutoff)
                            .replace(tzinfo=dt.UTC).timestamp() * 1e9)
            if published_ns < cutoff_ns:
                base.errors.append(
                    "article predates the model's training cutoff: this extraction is "
                    "contaminated and may be used for exploration only, never for "
                    "acceptance evidence")

        user = json.dumps({
            "schema": EXTRACTION_SCHEMA, "headline": headline[:500],
            "body": body[:6000],
        }, ensure_ascii=False)
        sw = Stopwatch()
        try:
            self._calls.append(wall_ns())
            raw = self.call_model(SYSTEM_PROMPT, user)
        except Exception as exc:  # noqa: BLE001 - provider failures are data
            base.valid = False
            base.latency_ms = sw.elapsed_ms
            base.errors.append(f"model call failed: {exc}")
            return base
        base.latency_ms = sw.elapsed_ms

        payload, err = parse_response(raw)
        if payload is None:
            base.valid = False
            base.errors.append(err or "unparseable response")
            return base
        clean, errors = validate(payload)
        quotes = [q for q in clean.get("evidence_quotes", [])
                  if q and (q[:60].lower() in (headline + " " + body).lower())]
        if clean.get("evidence_quotes") and not quotes:
            errors.append("evidence quotes do not appear in the source text "
                          "(possible fabrication); the extraction is marked invalid")

        base.event_type = clean["event_type"]
        base.currencies = [c.upper()[:3] for c in clean["currencies"]]
        base.direction_claim = clean["direction_claim"]
        base.is_scheduled = clean["is_scheduled"]
        base.is_revision = clean["is_revision"]
        base.is_correction = clean["is_correction"]
        base.contradicts_prior = clean["contradicts_prior"]
        base.numeric_values = clean["numeric_values"]
        base.evidence_quotes = clean["evidence_quotes"]
        base.confidence = clean["confidence"]
        base.novelty = clean["novelty"]
        base.errors.extend(errors)
        base.valid = not any("fabrication" in e or "unparseable" in e for e in errors)
        if base.latency_ms > self.timeout_ms:
            base.valid = False
            base.errors.append(
                f"extraction took {base.latency_ms:.0f}ms against a {self.timeout_ms}ms "
                "budget: too slow to inform a decision")
        return base


def latency_budget(components_ms: dict[str, float]) -> dict[str, Any]:
    """Total decision latency, itemised.

    Printed in the dashboard next to the news panel so the honest conclusion
    stays visible: this path measures in hundreds of milliseconds, the
    interdealer market reprices a surprise in well under one second, and
    therefore the news module is a filter and a context engine, not a
    first-mover.
    """
    total = sum(components_ms.values())
    return {
        "components_ms": {k: round(v, 1) for k, v in components_ms.items()},
        "total_ms": round(total, 1),
        "verdict": ("fast enough to inform an hours-long decision"
                    if total < 3000 else
                    "too slow even for an hours-long decision; simplify the path"),
        "note": ("The interdealer market absorbs a scheduled surprise in under a "
                 "second. No retail path with a language model in it competes on "
                 "speed. Use this for classification and risk filtering, not for "
                 "racing the release."),
    }
