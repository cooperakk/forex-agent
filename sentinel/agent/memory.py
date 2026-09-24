"""The agent's lesson store.

What a "lesson" is here, precisely: a statistically supported statement about
the agent's own trading, with the evidence attached, that can be *retrieved*
when a similar situation appears and shown in the decision explanation.

What a lesson is NOT: an override. A lesson never widens a risk limit, never
enables a strategy, never resizes a position on its own. Its only powers are to
(a) appear in the explanation of a decision, (b) contribute a bounded,
capped-at-1.0 caution multiplier that can only *reduce* risk, and (c) feed the
proposal engine, which requires validation and human approval.

That restriction is the whole design. A system that learns by loosening its own
constraints from recent experience is a system that will be at its most
confident immediately before its worst loss.

**Lessons expire, and that is not a loophole.** A lesson with no expiry is a
permanent bias: it was learned from one market, it will be recalled in every
market afterwards, and nothing in the system can ever tell it that the world
changed. So every lesson carries the time it was last CONFIRMED by fresh
evidence, and three things follow from it:

* ``review_lesson`` is called with new trades. Evidence that still supports the
  lesson refreshes it; evidence that contradicts it counts against it, and two
  contradictions -- or one sign reversal -- retire it.
* Between reviews the lesson's influence DECAYS toward 1.0 on a half-life.
  Decay is safe in one specific sense worth being precise about: a caution
  multiplier is bounded above by 1.0, so decaying toward 1.0 removes a
  restriction and returns the agent to its baseline risk. It cannot take the
  agent ABOVE baseline, because there is nothing above 1.0 to decay to.
* A lesson not confirmed for ``max_unconfirmed_days`` is retired outright.

The decay is also why a lesson is scoped. "Losses are bigger in high
volatility" as a global statement is a permanent tax on every trade the agent
ever takes. The same statement scoped to ``regime='volatile_range'`` applies
where the evidence came from, is checked against fresh evidence from that
regime, and lapses when it stops being true.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core.clock import wall_ns

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ns    INTEGER NOT NULL,
    updated_ns    INTEGER NOT NULL,
    scope         TEXT NOT NULL,
    strategy      TEXT,
    instrument    TEXT,
    regime        TEXT,
    session       TEXT,
    statement     TEXT NOT NULL,
    evidence      TEXT NOT NULL,
    sample_size   INTEGER NOT NULL,
    effect_r      REAL NOT NULL,
    p_value       REAL NOT NULL,
    confidence    REAL NOT NULL,
    caution       REAL NOT NULL DEFAULT 1.0,
    status        TEXT NOT NULL DEFAULT 'active',
    superseded_by INTEGER,
    last_confirmed_ns   INTEGER NOT NULL DEFAULT 0,
    review_count        INTEGER NOT NULL DEFAULT 0,
    contradiction_count INTEGER NOT NULL DEFAULT 0,
    half_life_days      REAL NOT NULL DEFAULT 90.0
);
CREATE INDEX IF NOT EXISTS idx_lessons_lookup
    ON lessons(status, strategy, instrument, regime);
CREATE TABLE IF NOT EXISTS autopsies (
    trade_id   TEXT PRIMARY KEY,
    created_ns INTEGER NOT NULL,
    strategy   TEXT NOT NULL,
    instrument TEXT NOT NULL,
    mode       TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    r_multiple REAL NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_autopsies_strategy ON autopsies(strategy, created_ns);
"""


@dataclass
class Lesson:
    scope: str                    # "global" | "strategy" | "instrument" | "regime"
    statement: str
    evidence: dict[str, Any]
    sample_size: int
    effect_r: float
    p_value: float
    strategy: str | None = None
    instrument: str | None = None
    regime: str | None = None
    session: str | None = None
    caution: float = 1.0
    id: int | None = None
    status: str = "active"
    created_ns: int = field(default_factory=wall_ns)
    last_confirmed_ns: int = 0
    review_count: int = 0
    contradiction_count: int = 0
    # How fast the lesson's influence fades without fresh confirmation. Ninety
    # days is about a quarter of trading: long enough that a lesson is not
    # forgotten between reviews, short enough that one learned in a regime that
    # has since ended stops taxing every trade within a season.
    half_life_days: float = 90.0

    def __post_init__(self) -> None:
        # A lesson can only ever counsel *less* risk.
        self.caution = float(min(1.0, max(0.25, self.caution)))
        if not self.last_confirmed_ns:
            self.last_confirmed_ns = self.created_ns

    def age_days(self, now_ns: int | None = None) -> float:
        now = now_ns or wall_ns()
        return max(0.0, (now - (self.last_confirmed_ns or self.created_ns)) / 86_400e9)

    def effective_caution(self, now_ns: int | None = None) -> float:
        """Caution, decayed toward 1.0 by the time since it was last confirmed.

        Decaying toward 1.0 RELAXES the lesson. That is the intended direction
        and it cannot become a risk increase: caution is capped at 1.0, so the
        limit of the decay is the agent's ordinary size, never more than it.
        The failure this prevents is the opposite one -- a lesson learned in one
        market quietly taxing every trade for the life of the deployment, with
        no mechanism that could ever notice it had stopped being true.
        """
        if self.caution >= 1.0:
            return 1.0
        hl = max(1.0, float(self.half_life_days))
        weight = 0.5 ** (self.age_days(now_ns) / hl)
        return float(min(1.0, 1.0 - (1.0 - self.caution) * weight))

    @property
    def confidence(self) -> float:
        """Bounded confidence from sample size and significance.

        Deliberately conservative: it saturates slowly in n and collapses as
        p approaches the threshold, so a lesson learned from 30 trades never
        speaks with the authority of one learned from 300.
        """
        import math

        n_term = min(1.0, math.log10(max(1, self.sample_size)) / 2.5)
        p_term = max(0.0, min(1.0, 1.0 - self.p_value / 0.05))
        return round(float(n_term * p_term), 4)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["confidence"] = self.confidence
        d["age_days"] = round(self.age_days(), 2)
        d["effective_caution"] = round(self.effective_caution(), 4)
        return d


#: How many contradicting observations retire a lesson. Two, because one is
#: noise and three is a policy that outlives its evidence.
_RETIRE_AFTER_CONTRADICTIONS = 2


class MemoryStore:
    """SQLite-backed lesson and autopsy store. Thread-safe."""

    def __init__(self, path: str | Path = "var/memory.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            # An existing var/memory.db predates the expiry columns. Adding them
            # rather than recreating the table keeps the lesson history, which is
            # the record of what the agent believed and when -- and is the only
            # way to tell a lesson that was never reviewed from one that was
            # reviewed and held.
            have = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(lessons)").fetchall()}
            for col, decl in (("last_confirmed_ns", "INTEGER NOT NULL DEFAULT 0"),
                              ("review_count", "INTEGER NOT NULL DEFAULT 0"),
                              ("contradiction_count", "INTEGER NOT NULL DEFAULT 0"),
                              ("half_life_days", "REAL NOT NULL DEFAULT 90.0")):
                if col not in have:
                    self._conn.execute(f"ALTER TABLE lessons ADD COLUMN {col} {decl}")
            self._conn.execute(
                "UPDATE lessons SET last_confirmed_ns=created_ns WHERE last_confirmed_ns=0")
            self._conn.commit()

    # -- autopsies ---------------------------------------------------------- #

    def record_autopsy(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO autopsies "
                "(trade_id, created_ns, strategy, instrument, mode, outcome, r_multiple, payload) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (payload["trade_id"], wall_ns(), payload["strategy"], payload["instrument"],
                 payload["mode"], payload["outcome"], float(payload["r_multiple"]),
                 json.dumps(payload, ensure_ascii=False)),
            )
            self._conn.commit()

    def autopsies(self, strategy: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        q = "SELECT payload FROM autopsies"
        args: list[Any] = []
        if strategy:
            q += " WHERE strategy = ?"
            args.append(strategy)
        q += " ORDER BY created_ns DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def autopsy_count(self, strategy: str | None = None) -> int:
        q = "SELECT COUNT(*) AS c FROM autopsies"
        args: list[Any] = []
        if strategy:
            q += " WHERE strategy = ?"
            args.append(strategy)
        with self._lock:
            return int(self._conn.execute(q, args).fetchone()["c"])

    # -- lessons ------------------------------------------------------------ #

    def add_lesson(self, lesson: Lesson) -> int:
        now = wall_ns()
        with self._lock:
            # Supersede rather than duplicate: the same statement in the same
            # scope updates the previous version and keeps the history.
            #
            # RETIRED rows are matched too. Looking only at active ones meant a
            # lesson could resurrect itself with a clean record: retired for a
            # sign reversal (the effect it claimed had reversed), or retired
            # deliberately by an operator, and then re-minted by the next
            # postmortem that produced the same recommendation string -- with
            # contradiction_count back at zero and its caution multiplier back
            # in force. An operator's decision to retire a lesson has to
            # survive the next scheduled run, or it is not a decision.
            prior = self._conn.execute(
                "SELECT id, status, contradiction_count, "
                "       json_extract(evidence,'$.retire_reason') AS retire_reason "
                "FROM lessons "
                "WHERE statement=? AND IFNULL(strategy,'')=? "
                "AND IFNULL(instrument,'')=? AND IFNULL(regime,'')=? "
                "AND status IN ('active','retired') "
                "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id DESC "
                "LIMIT 1",
                (lesson.statement, lesson.strategy or "", lesson.instrument or "",
                 lesson.regime or "")).fetchone()
            if prior and prior["status"] == "retired":
                # WHY it was retired decides whether it may come back.
                #
                # Retired for being unconfirmed -- nobody re-tested it, so it
                # simply went stale -- is a reason fresh evidence legitimately
                # overturns, and re-minting is correct.
                #
                # Retired because the evidence CONTRADICTED it, or because an
                # operator retired it deliberately, is a decision. Re-minting
                # it the next time a postmortem produces the same sentence
                # would erase both, with contradiction_count back at zero and
                # the caution multiplier back in force -- and the operator's
                # action would not survive the next scheduled run.
                reason = str(prior["retire_reason"] or "")
                carried = int(prior["contradiction_count"] or 0)
                if carried > 0 or "contradict" in reason or "reversal" in reason \
                        or "operator" in reason or "manual" in reason:
                    return int(prior["id"])
                lesson.contradiction_count = max(lesson.contradiction_count, carried)
            cur = self._conn.execute(
                "INSERT INTO lessons (created_ns, updated_ns, scope, strategy, instrument, "
                "regime, session, statement, evidence, sample_size, effect_r, p_value, "
                "confidence, caution, status, last_confirmed_ns, review_count, "
                "contradiction_count, half_life_days) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?)",
                (lesson.created_ns, now, lesson.scope, lesson.strategy, lesson.instrument,
                 lesson.regime, lesson.session, lesson.statement,
                 json.dumps(lesson.evidence, ensure_ascii=False), lesson.sample_size,
                 lesson.effect_r, lesson.p_value, lesson.confidence, lesson.caution,
                 lesson.last_confirmed_ns or lesson.created_ns, lesson.review_count,
                 lesson.contradiction_count, lesson.half_life_days),
            )
            new_id = int(cur.lastrowid)
            if prior:
                self._conn.execute(
                    "UPDATE lessons SET status='superseded', superseded_by=?, updated_ns=? "
                    "WHERE id=?", (new_id, now, prior["id"]))
            self._conn.commit()
        return new_id

    def recall(self, *, strategy: str | None = None, instrument: str | None = None,
               regime: str | None = None, session: str | None = None,
               limit: int = 8) -> list[Lesson]:
        """Lessons relevant to a decision context, most confident first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lessons WHERE status='active' "
                "AND (strategy IS NULL OR strategy = ?) "
                "AND (instrument IS NULL OR instrument = ?) "
                "AND (regime IS NULL OR regime = ?) "
                "AND (session IS NULL OR session = ?) "
                "ORDER BY confidence DESC, sample_size DESC LIMIT ?",
                (strategy, instrument, regime, session, limit)).fetchall()
        return [self._row_to_lesson(r) for r in rows]

    def all_lessons(self, include_superseded: bool = False) -> list[Lesson]:
        q = "SELECT * FROM lessons"
        if not include_superseded:
            q += " WHERE status='active'"
        q += " ORDER BY updated_ns DESC"
        with self._lock:
            return [self._row_to_lesson(r) for r in self._conn.execute(q).fetchall()]

    def retire(self, lesson_id: int, reason: str = "") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE lessons SET status='retired', updated_ns=?, "
                "evidence=json_set(evidence,'$.retire_reason',?) WHERE id=? AND status='active'",
                (wall_ns(), reason, lesson_id))
            self._conn.commit()
            return cur.rowcount > 0

    # -- expiry ------------------------------------------------------------- #

    def review_lesson(self, lesson_id: int, *, supported: bool, sample_size: int,
                      effect_r: float, p_value: float,
                      now_ns: int | None = None,
                      max_contradictions: int = 2) -> str:
        """Re-test one lesson against fresh evidence.

        Returns what happened: ``confirmed``, ``contradicted``, ``retired`` or
        ``missing``.

        A lesson is retired on the second contradiction, or immediately when the
        effect REVERSES SIGN -- which is a stronger statement than "the evidence
        got weaker". A lesson saying trades in stress lose 0.4R, re-tested on
        fresh stress trades that gain 0.3R, is not a lesson with a smaller
        effect. It is wrong, and leaving it in place at reduced confidence is a
        slower way of being wrong.
        """
        now = now_ns or wall_ns()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lessons WHERE id=? AND status='active'",
                (lesson_id,)).fetchone()
            if row is None:
                return "missing"
            reversed_sign = (float(row["effect_r"]) != 0 and effect_r != 0
                             and (float(row["effect_r"]) > 0) != (effect_r > 0))
            if supported and not reversed_sign:
                # Blend the evidence rather than replacing it: a lesson
                # confirmed by 30 fresh trades on top of 200 old ones should
                # not have its sample size reset to 30.
                merged_n = int(row["sample_size"]) + int(sample_size)
                self._conn.execute(
                    "UPDATE lessons SET last_confirmed_ns=?, updated_ns=?, "
                    "review_count=review_count+1, sample_size=?, effect_r=?, p_value=?, "
                    "confidence=? WHERE id=?",
                    (now, now, merged_n, effect_r, p_value,
                     Lesson(scope=row["scope"], statement=row["statement"], evidence={},
                            sample_size=merged_n, effect_r=effect_r,
                            p_value=p_value).confidence, lesson_id))
                self._conn.commit()
                return "confirmed"
            contradictions = int(row["contradiction_count"]) + 1
            if reversed_sign or contradictions >= max_contradictions:
                reason = ("the effect reversed sign on fresh evidence"
                          if reversed_sign else
                          f"contradicted by fresh evidence {contradictions} times")
                self._conn.execute(
                    "UPDATE lessons SET status='retired', updated_ns=?, "
                    "contradiction_count=?, "
                    "evidence=json_set(evidence,'$.retire_reason',?) WHERE id=?",
                    (now, contradictions, reason, lesson_id))
                self._conn.commit()
                return "retired"
            self._conn.execute(
                "UPDATE lessons SET contradiction_count=?, updated_ns=? WHERE id=?",
                (contradictions, now, lesson_id))
            self._conn.commit()
            return "contradicted"

    def expire_unconfirmed(self, *, now_ns: int | None = None,
                           max_unconfirmed_days: float = 365.0) -> int:
        """Retire lessons that no fresh evidence has spoken to in a long time.

        The decay in ``effective_caution`` already reduces a stale lesson's
        influence toward nothing. This removes it outright, so the lesson list
        an operator reads is the set of things the agent currently believes,
        rather than an archive with a long tail of claims about a market that
        ended two years ago.
        """
        now = now_ns or wall_ns()
        cutoff = now - int(max_unconfirmed_days * 86_400e9)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE lessons SET status='retired', updated_ns=?, "
                "evidence=json_set(evidence,'$.retire_reason',?) "
                "WHERE status='active' AND "
                "CASE WHEN last_confirmed_ns > 0 THEN last_confirmed_ns ELSE created_ns END < ?",
                (now, f"no confirming evidence in {max_unconfirmed_days:.0f} days", cutoff))
            self._conn.commit()
            return cur.rowcount

    def review_against(self, findings: Sequence[Any], *,
                       alpha: float = 0.05,
                       now_ns: int | None = None) -> dict[str, int]:
        """Re-test every active lesson against a fresh set of pattern findings.

        A lesson is matched to a finding by the pattern recorded in its
        evidence. A lesson whose pattern is no longer tested at all -- because
        the tag stopped appearing, or the counterfactual stopped firing -- is
        left to the decay and to ``expire_unconfirmed``: absence of evidence is
        not contradiction, and treating it as such would retire a lesson every
        time a quiet month produced too few trades to test anything.
        """
        by_pattern = {}
        for f in findings:
            pat = getattr(f, "pattern", None)
            if pat:
                by_pattern[pat] = f
        counts = {"confirmed": 0, "contradicted": 0, "retired": 0, "untested": 0}
        for lesson in self.all_lessons():
            pat = (lesson.evidence or {}).get("pattern")
            f = by_pattern.get(pat) if pat else None
            if f is None or lesson.id is None:
                counts["untested"] += 1
                continue
            p_eff = getattr(f, "p_value_adjusted", None) or f.p_value
            supported = bool(p_eff < alpha and getattr(f, "n", 0) >= 10)
            result = self.review_lesson(
                lesson.id, supported=supported, sample_size=int(getattr(f, "n", 0)),
                effect_r=float(getattr(f, "mean_delta_r", lesson.effect_r)),
                p_value=float(p_eff), now_ns=now_ns)
            if result in counts:
                counts[result] += 1
        return counts

    @staticmethod
    def _row_to_lesson(row: sqlite3.Row) -> Lesson:
        return Lesson(
            id=row["id"], scope=row["scope"], statement=row["statement"],
            evidence=json.loads(row["evidence"]), sample_size=row["sample_size"],
            effect_r=row["effect_r"], p_value=row["p_value"], strategy=row["strategy"],
            instrument=row["instrument"], regime=row["regime"], session=row["session"],
            caution=row["caution"], status=row["status"], created_ns=row["created_ns"],
            last_confirmed_ns=(row["last_confirmed_ns"]
                               if "last_confirmed_ns" in row.keys() else 0),
            review_count=(row["review_count"] if "review_count" in row.keys() else 0),
            contradiction_count=(row["contradiction_count"]
                                 if "contradiction_count" in row.keys() else 0),
            half_life_days=(row["half_life_days"]
                            if "half_life_days" in row.keys() else 90.0),
        )

    # -- influence ----------------------------------------------------------- #

    def caution_multiplier(self, *, strategy: str | None = None,
                           instrument: str | None = None,
                           regime: str | None = None,
                           floor: float = 0.4,
                           now_ns: int | None = None) -> tuple[float, list[str]]:
        """Combined caution from applicable lessons. Never exceeds 1.0.

        Each lesson's caution is blended toward 1.0 by its own confidence AND by
        how long it has been since fresh evidence confirmed it, so a
        weakly-supported or stale lesson barely moves the number. The product is
        floored so that an accumulation of cautions cannot silently stop all
        trading -- if the agent should stop, that is a risk-engine decision, not
        a drift.
        """
        lessons = self.recall(strategy=strategy, instrument=instrument, regime=regime, limit=12)
        multiplier = 1.0
        reasons: list[str] = []
        for lesson in lessons:
            decayed = lesson.effective_caution(now_ns)
            if decayed >= 1.0:
                continue
            weighted = 1.0 - (1.0 - decayed) * lesson.confidence
            multiplier *= weighted
            age = lesson.age_days(now_ns)
            stale = f", unconfirmed for {age:.0f}d" if age > lesson.half_life_days else ""
            reasons.append(f"{lesson.statement} (x{weighted:.2f}{stale})")
        return max(floor, round(multiplier, 4)), reasons

    def close(self) -> None:
        with self._lock:
            self._conn.close()
