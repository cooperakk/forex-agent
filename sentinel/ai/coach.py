"""The AI coach: plain-language reviews of closed trades, and a market brief.

Where this sits in the learning loop
------------------------------------
The statistical loop (``agent/postmortem.py`` -> ``memory.py`` ->
``proposals.py``) is the part that is allowed to CHANGE behaviour: lessons with
a false-discovery-corrected p-value shrink size, and parameter proposals wait
for a human and a validation run. It is deliberately strict, which means it is
silent for the first forty or so trades and terse afterwards.

The coach fills the gap for the human. After each closed trade it explains, in
simple Persian, what happened and whether the outcome looks like the strategy
working as designed, bad luck, or a repeatable mistake -- and it offers ONE
hypothesis worth testing. It changes nothing: its output is stored, shown on the
dashboard and counted into recurring themes. A theme that keeps recurring is a
candidate for the statistical loop to confirm or refute; the coach's opinion is
never evidence by itself.

Prompt-injection note: the only free text a coach prompt contains is produced
by this system (strategy names, rationales, tags). Headlines never reach the
coach, and neither the coach nor the brief has a tool it could misuse.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, Optional

from ..news.llm_extract import parse_response

CATEGORIES = ("working_as_designed", "bad_luck", "late_entry", "early_exit", "late_exit",
              "stop_too_tight", "stop_too_wide", "against_trend", "news_shock",
              "high_cost", "regime_mismatch", "other")

COACH_SYSTEM = """You are a senior FX risk manager explaining ONE closed trade of an \
automated trading system to its owner, who is not a professional trader.

Rules:
1. Use ONLY the numbers provided. Never invent prices, news or events.
2. R is the result in units of the initial risk: -1 R means the full stop was lost.
   MAE/MFE are the worst/best open result in R during the trade.
3. Distinguish honestly between a sound trade that lost (bad luck / the strategy \
working as designed) and a repeatable mistake. One trade proves nothing; say so \
when appropriate.
4. Never recommend increasing risk, removing a stop, averaging down, or disabling \
a safety limit. Suggestions are hypotheses to TEST, not instructions.
5. Write the *_fa fields in simple, friendly Persian, 1-3 short sentences each.
6. Output ONLY a JSON object with exactly these keys:
{"summary_fa": str, "what_went_right_fa": str|null, "what_went_wrong_fa": str|null,
 "category": one of %s,
 "avoidable": bool, "suggestion_fa": str, "confidence": number 0..1}""" % (
    json.dumps(list(CATEGORIES)),)

BRIEF_SYSTEM = """You are the risk desk of an automated FX trading system writing a \
short morning brief for its owner, who is not a professional trader.

Rules:
1. Use ONLY the facts provided (regime, account, positions, calendar, official \
headlines, recent decisions). Never invent prices, forecasts or events.
2. Do not predict price direction and do not give buy/sell advice. Describe \
conditions, scheduled risks and what the system's own rules will do about them.
3. Mention every high-impact event in the next 24 hours by name and time (UTC).
4. Write in simple, friendly Persian.
5. Output ONLY a JSON object:
{"headline_fa": str, "market_fa": str, "risks_fa": [str, ...],
 "agent_state_fa": str, "watch_fa": [str, ...], "confidence": number 0..1}"""


def _clip(value: Any, n: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:n] if text else None


def validate_review(obj: Dict[str, Any]) -> Dict[str, Any]:
    category = obj.get("category")
    if category not in CATEGORIES:
        category = "other"
    try:
        confidence = max(0.0, min(1.0, float(obj.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    out = {
        "summary_fa": _clip(obj.get("summary_fa"), 600) or "",
        "what_went_right_fa": _clip(obj.get("what_went_right_fa"), 400),
        "what_went_wrong_fa": _clip(obj.get("what_went_wrong_fa"), 400),
        "category": category,
        "avoidable": bool(obj.get("avoidable", False)),
        "suggestion_fa": _clip(obj.get("suggestion_fa"), 400) or "",
        "confidence": round(confidence, 2),
        "advisory_only": True,
    }
    if not out["summary_fa"]:
        raise ValueError("the review has no summary")
    return out


def validate_brief(obj: Dict[str, Any]) -> Dict[str, Any]:
    def strings(value, n, k):
        if not isinstance(value, list):
            return []
        return [s for s in (_clip(v, n) for v in value[:k]) if s]
    out = {
        "headline_fa": _clip(obj.get("headline_fa"), 200) or "",
        "market_fa": _clip(obj.get("market_fa"), 1200) or "",
        "risks_fa": strings(obj.get("risks_fa"), 300, 8),
        "agent_state_fa": _clip(obj.get("agent_state_fa"), 600) or "",
        "watch_fa": strings(obj.get("watch_fa"), 300, 8),
        "advisory_only": True,
    }
    if not out["headline_fa"] and not out["market_fa"]:
        raise ValueError("the brief is empty")
    return out


class TradeCoach:
    """Reviews closed trades in the background, a few per tick."""

    def __init__(self, ai, memory, *, max_per_tick: int = 3,
                 review_every_nth_win: int = 4) -> None:
        self.ai = ai
        self.memory = memory
        self.max_per_tick = max_per_tick
        self.review_every_nth_win = max(1, review_every_nth_win)
        self.last_error = ""

    def _context(self, strategy: str) -> Dict[str, Any]:
        rows = self.memory.autopsies(strategy, limit=50)
        rs = [float(r.get("r_multiple", 0.0)) for r in rows]
        wins = [r for r in rs if r > 0]
        return {"recent_trades": len(rs),
                "win_rate": round(len(wins) / len(rs), 3) if rs else None,
                "mean_r": round(sum(rs) / len(rs), 3) if rs else None}

    def _wanted(self, row: Dict[str, Any], index: int) -> bool:
        if float(row.get("r_multiple", 0.0)) <= 0:
            return True                                  # every loss is reviewed
        return index % self.review_every_nth_win == 0    # a sample of wins

    def tick(self) -> int:
        ok, why = self.ai.available("coach")
        if not ok:
            self.last_error = why
            return 0
        done = 0
        rows = self.memory.autopsies(None, limit=40)
        for index, row in enumerate(rows):
            if done >= self.max_per_tick:
                break
            trade_id = str(row.get("trade_id", ""))
            if not trade_id or self.ai.store.reviewed(trade_id) or not self._wanted(row, index):
                continue
            facts = {k: row.get(k) for k in (
                "strategy", "instrument", "outcome", "mode", "r_multiple", "mae_r",
                "mfe_r", "capture_ratio", "tags", "regime", "narrative", "had_path")}
            facts["counterfactuals"] = [
                {k: c.get(k) for k in ("name", "delta_r", "computable")}
                for c in (row.get("counterfactuals") or [])[:6] if isinstance(c, dict)]
            facts["strategy_context"] = self._context(str(row.get("strategy", "")))
            result = self.ai.complete("coach", COACH_SYSTEM,
                                      json.dumps(facts, ensure_ascii=False, default=str),
                                      max_tokens=700, json_mode=True)
            if result is None:
                self.last_error = "no AI provider answered"
                break
            payload, err = parse_response(result.text)
            try:
                review = validate_review(payload or {})
            except ValueError as exc:
                self.last_error = err or str(exc)
                continue
            self.ai.store.save_review(trade_id, str(row.get("strategy", "")),
                                      str(row.get("instrument", "")),
                                      float(row.get("r_multiple", 0.0) or 0.0),
                                      result.provider, result.model, review)
            done += 1
        return done

    def themes(self, limit: int = 200) -> Dict[str, Any]:
        """Recurring categories across recent reviews -- counting, not a model."""
        reviews = self.ai.store.reviews(limit)
        cats = Counter(r["payload"].get("category", "other") for r in reviews)
        avoidable = sum(1 for r in reviews if r["payload"].get("avoidable"))
        return {"reviewed": len(reviews),
                "categories": [{"category": c, "count": n} for c, n in cats.most_common()],
                "avoidable": avoidable,
                "note_fa": ("این شمارش فقط نشان می‌دهد چه چیزی تکرار می‌شود. تا وقتی "
                            "حلقهٔ آماری ربات آن را تأیید نکند، مدرک به حساب نمی‌آید.")}


def build_brief_facts(runtime) -> Dict[str, Any]:
    """Everything the brief may mention, and nothing it may not."""
    agent = runtime.agent
    status = runtime.status()
    facts: Dict[str, Any] = {
        "mode": status.get("mode"), "venue": status.get("venue_mode"),
        "halted": status.get("halted"), "halt_reason": status.get("halt_reason"),
        "kill_switch": (status.get("kill_switch") or {}).get("engaged"),
        "equity": (status.get("account") or {}).get("equity"),
        "regime": status.get("regime"),
        "guard_suspended": status.get("guard_suspended"),
        "positions": [{k: p.get(k) for k in ("instrument", "side", "lots", "r_multiple")}
                      for p in runtime.positions() if isinstance(p, dict)][:10],
    }
    try:
        risk = runtime.risk_view()
        facts["drawdown_pct"] = risk.get("drawdown_pct")
        facts["day_pnl_pct"] = risk.get("day_pnl_pct")
    except Exception:  # noqa: BLE001
        pass
    calendar = getattr(getattr(agent, "news", None), "calendar", None)
    if calendar is not None:
        try:
            from ..core.clock import wall_ns
            now = wall_ns()
            upcoming = calendar.upcoming(now, 86_400)
            facts["calendar_next_24h"] = [
                {k: e.get(k) if isinstance(e, dict) else getattr(e, k, None)
                 for k in ("name", "currency", "impact", "certainty", "event_ns")}
                for e in list(upcoming)[:20]]
        except Exception:  # noqa: BLE001
            facts["calendar_next_24h"] = []
    desk = getattr(runtime, "news_desk", None)
    if desk is not None:
        facts["official_headlines"] = [
            {"source": h["source"], "title": h["title"]} for h in desk.headlines(12)]
    decisions = agent.decisions[-200:]
    facts["recent_decisions"] = {
        a: sum(1 for d in decisions if d.action == a)
        for a in ("executed", "vetoed", "queued", "skipped", "proposed")}
    vetoes = Counter(v.get("rule") for d in decisions for v in (d.vetoes or []))
    facts["top_vetoes"] = [r for r, _ in vetoes.most_common(5)]
    return facts


def generate_brief(ai, runtime) -> Optional[Dict[str, Any]]:
    ok, why = ai.available("brief")
    if not ok:
        raise ValueError(why)
    facts = build_brief_facts(runtime)
    result = ai.complete("brief", BRIEF_SYSTEM,
                         json.dumps(facts, ensure_ascii=False, default=str),
                         max_tokens=1200, json_mode=True)
    if result is None:
        raise ValueError("no AI provider answered")
    payload, err = parse_response(result.text)
    brief = validate_brief(payload or {})
    brief["provider"], brief["model"] = result.provider, result.model
    ai.store.save_brief(result.provider, result.model, brief)
    return brief
