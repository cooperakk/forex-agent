"""News classification with a System One model (Jev, TypeSafe AI).

A text model is asked to WRITE a JSON extraction and is then checked for
fabricated quotes. Jev is asked typed questions instead -- "which of these
event types is this?", "does this correct an earlier report?" -- and returns a
probability for every answer. Nothing is generated, so nothing can be
fabricated, and the probability is the thing the policy acts on.

Design choices, and why:

* **The questions are the extraction schema.** event_type and direction_claim
  are Choices over exactly the schema's enums; correction, revision,
  contradiction and scheduled are Nouls (probability that the condition
  holds). The result is the same ``Extraction`` the text path produces, so
  ``NewsPolicy`` does not know or care which model answered.
* **Positional bias is cancelled.** A System One model prefers options listed
  first. Every Choice is asked twice, with the criteria in opposite orders, in
  the same request (answers are independent and latency is nearly flat in the
  number of questions), and the two distributions are averaged.
* **Blocking needs strong evidence.** ``contradicts_prior`` is set only when
  its probability is at least 0.75; a correction needs 0.6. The desk still
  honours a contradiction block only while fresh (4 h) and confident.
* **Evidence is the source itself.** Jev writes no quotes; the headline it
  classified is recorded as the evidence, verbatim.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..news.llm_extract import EXTRACTION_SCHEMA, Extraction

EVENT_TYPES = {
    "monetary_policy": "a central bank decision on interest rates, asset purchases or "
                       "policy guidance, or the minutes of such a decision",
    "inflation": "a consumer, producer or wage price release or an inflation statement",
    "employment": "a jobs, payrolls, unemployment or labour-market release",
    "growth": "GDP, output, retail sales, PMI or another activity release",
    "trade": "trade balance, tariffs, current account or trade negotiations",
    "fiscal": "government budget, taxes, debt issuance or spending",
    "geopolitical": "war, sanctions, elections or another political event",
    "central_bank_speech": "a speech, testimony or interview by a central banker",
    "market_structure": "market operations, liquidity facilities, regulation or "
                        "payment-system notices",
    "other": "none of the above",
}
DIRECTIONS = {
    "hawkish": "points to tighter monetary policy or higher interest rates",
    "dovish": "points to looser monetary policy or lower interest rates",
    "neutral": "about monetary policy but points in neither direction",
    "unclear": "does not say anything about the direction of monetary policy",
}
CONDITIONS = {
    "is_correction": "Does this item correct or retract a figure or statement that was "
                     "published earlier?",
    "is_revision": "Does this item revise previously released economic data?",
    "contradicts_prior": "Does this item contradict or reverse what the same institution "
                         "previously communicated?",
    "is_scheduled": "Is this a scheduled release, such as a planned data release or the "
                    "outcome of a scheduled policy meeting?",
}

assert set(EVENT_TYPES) == set(EXTRACTION_SCHEMA["event_type"]["values"])
assert set(DIRECTIONS) == set(EXTRACTION_SCHEMA["direction_claim"]["values"])

CONTRADICTION_THRESHOLD = 0.75
CORRECTION_THRESHOLD = 0.6


def build_questions() -> Dict[str, Dict[str, Any]]:
    questions: Dict[str, Dict[str, Any]] = {}
    for name, criteria, instructions in (
        ("event_type", EVENT_TYPES,
         "Classify the kind of economic or policy event this official item reports."),
        ("direction", DIRECTIONS,
         "What does this official item imply about the direction of monetary policy? "
         "Judge the text itself, not what markets did afterwards."),
    ):
        keys = list(criteria)
        questions[f"{name}"] = {"type": "choice", "instructions": instructions,
                                "criteria": {k: criteria[k] for k in keys}}
        questions[f"{name}_rev"] = {"type": "choice", "instructions": instructions,
                                    "criteria": {k: criteria[k] for k in reversed(keys)}}
    for name, text in CONDITIONS.items():
        questions[name] = {"type": "noul", "instructions": text}
    return questions


def _choice_probabilities(answer: Any, criteria: Dict[str, str]) -> Dict[str, float]:
    """Probabilities keyed by OPTION KEY, whether the answer used keys or labels."""
    if not isinstance(answer, dict):
        return {}
    raw = answer.get("probabilities")
    if not isinstance(raw, dict):
        choice = answer.get("choice")
        return {str(choice): 1.0} if choice in criteria else {}
    by_label = {label: key for key, label in criteria.items()}
    out: Dict[str, float] = {}
    for k, v in raw.items():
        key = k if k in criteria else by_label.get(k)
        if key is None:
            continue
        try:
            out[key] = out.get(key, 0.0) + max(0.0, float(v))
        except (TypeError, ValueError):
            continue
    total = sum(out.values())
    return {k: v / total for k, v in out.items()} if total > 0 else {}


def _averaged(answers: Dict[str, Any], name: str, criteria: Dict[str, str]) -> Dict[str, float]:
    parts = [p for p in (_choice_probabilities(answers.get(name), criteria),
                         _choice_probabilities(answers.get(f"{name}_rev"), criteria)) if p]
    if not parts:
        return {}
    return {k: sum(p.get(k, 0.0) for p in parts) / len(parts) for k in criteria}


def _noul(answers: Dict[str, Any], name: str) -> Optional[float]:
    answer = answers.get(name)
    value = answer.get("noul", answer.get("probability")) if isinstance(answer, dict) \
        else answer
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    return p if 0.0 <= p <= 1.0 else None


def extraction_from_answers(article_id: str, headline: str, answers: Dict[str, Any], *,
                            model: str, currencies, latency_ms: float,
                            training_cutoff: str = "") -> Extraction:
    events = _averaged(answers, "event_type", EVENT_TYPES)
    directions = _averaged(answers, "direction", DIRECTIONS)
    errors = []
    if not events or not directions:
        errors.append("the System One answer is missing a classification")
    event = max(events, key=events.get) if events else "other"
    direction = max(directions, key=directions.get) if directions else "unclear"
    p = {name: _noul(answers, name) for name in CONDITIONS}
    for name, value in p.items():
        if value is None:
            errors.append(f"no probability for {name}")
    # Concentration of the two averaged distributions: how sure the model is
    # about WHAT this item is. Used by the desk's block gate.
    confidence = (events.get(event, 0.0) + directions.get(direction, 0.0)) / 2.0 \
        if events and directions else 0.0
    ex = Extraction(
        article_id=article_id, event_type=event, currencies=list(currencies or []),
        direction_claim=direction,
        is_scheduled=(p["is_scheduled"] or 0.0) >= 0.5,
        is_revision=(p["is_revision"] or 0.0) >= 0.5,
        is_correction=(p["is_correction"] or 0.0) >= CORRECTION_THRESHOLD,
        contradicts_prior=(p["contradicts_prior"] or 0.0) >= CONTRADICTION_THRESHOLD,
        numeric_values=[{"name": f"p_{k}", "value": round(v, 4)}
                        for k, v in p.items() if v is not None],
        evidence_quotes=[headline[:400]],
        confidence=round(confidence, 4), model=f"jev:{model}",
        model_cutoff=training_cutoff, prompt_version="jev-1", latency_ms=latency_ms,
        calibrated=False, errors=errors)
    ex.valid = not any("missing a classification" in e for e in errors)
    return ex
