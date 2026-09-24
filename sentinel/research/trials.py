"""The trial ledger: a durable count of everything that has ever been searched.

Why this module exists
----------------------

The acceptance protocol's multiple-testing gate (L5.1) deflates a candidate's
Sharpe by the number of trials that produced it. ``expected_max_sharpe`` in
``stats.py`` gives the bar: with 200 attempts and a trial-to-trial Sharpe
dispersion of 0.5, pure noise is *expected* to deliver a best Sharpe near 1.5.
An uncorrected 1.4 is therefore not evidence of anything -- it is the median
outcome of searching hard enough.

The gate is only as honest as the count it is given, and the count is the one
number an optimistic operator will understate without even noticing. Before
this ledger existed the count came from a command-line flag whose default was
1, which meant the default behaviour of the multiple-testing gate was to be
switched off.

The problem a large library creates
-----------------------------------

A strategy library of thirty is not thirty times more likely to find an edge.
It is a better overfitting machine, because "the best of thirty" is a maximum
over thirty draws and a maximum over thirty draws from a distribution centred
on zero is comfortably positive. Every strategy added, every parameter set
tried, every instrument set and every window is another draw.

So the size of the library must be *paid for* in the trial count, automatically
and without anyone having to remember. That is this module's whole job:

* every backtest records the exact combination it evaluated, hash-keyed so that
  re-running the same thing does not inflate the count;
* the count is per strategy AND per family, because choosing the best of six
  trend systems is one search over six trials, not six independent discoveries;
* ``scripts/run_acceptance.py`` reads the ledger and uses
  ``max(declared, ledger, variants_this_run, 2)`` -- the largest of every
  figure available, because every one of them is a lower bound on the search.

What this cannot do, stated plainly
-----------------------------------

The ledger counts what it observes. It cannot know about:

* backtests run before it existed, or on another machine, or in a notebook that
  never touched ``run_backtest``;
* the ideas discarded after a glance at a chart, which are real trials --
  arguably the most dangerous kind, because they are selected on the same data
  and leave no trace;
* the strategies in the library that were chosen, by whoever wrote them, from a
  much larger set of things that people have tried in this market since 1983.

For all three reasons the ledger count is a FLOOR, never the truth, and the
acceptance report says so. If you have searched outside this process, declare
the larger number: ``--declared-trials`` is still read and still wins when it
is bigger. The ledger exists to stop the count being *too low by accident*, not
to relieve anyone of judgement about how much searching really happened.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..core.clock import wall_ns

#: Identifies this process's research session, so the ledger can report "N
#: trials across M sessions". A single session searching 50 variants and 50
#: sessions searching one each are both 50 trials, but they are different
#: stories about how the search happened, and the report shows both.
SESSION_ID = os.environ.get("SENTINEL_RESEARCH_SESSION") or uuid.uuid4().hex[:16]

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    trial_key     TEXT PRIMARY KEY,
    strategy      TEXT NOT NULL,
    family        TEXT NOT NULL,
    timeframe     TEXT NOT NULL,
    instruments   TEXT NOT NULL,
    params        TEXT NOT NULL,
    data_window   TEXT NOT NULL,
    data_label    TEXT NOT NULL DEFAULT 'unknown',
    -- 'search'     a configuration evaluated in order to decide what to trade.
    --              These are the draws the multiple-testing correction prices.
    -- 'validation' the SAME configuration re-run to characterise it: a CPCV
    --              fold, a stress pass, a baseline. Counting these as extra
    --              trials would penalise thorough validation, which is exactly
    --              the behaviour this system wants more of.
    kind          TEXT NOT NULL DEFAULT 'search',
    first_seen_ns INTEGER NOT NULL,
    last_seen_ns  INTEGER NOT NULL,
    runs          INTEGER NOT NULL DEFAULT 1,
    note          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trials_strategy ON trials(strategy);
CREATE INDEX IF NOT EXISTS idx_trials_family   ON trials(family, kind);

CREATE TABLE IF NOT EXISTS trial_sessions (
    trial_key  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    seen_ns    INTEGER NOT NULL,
    PRIMARY KEY (trial_key, session_id)
);
CREATE INDEX IF NOT EXISTS idx_trial_sessions_key ON trial_sessions(trial_key);
"""


class LedgerUnreadable(RuntimeError):
    """The trial ledger exists but cannot be read.

    Distinguished from "the ledger is empty" for the same reason
    ``RegistryUnreadable`` is in ``verdicts.py``: an empty ledger means no
    search has been recorded, and an unreadable one means the record of the
    search is gone. Treating the second as the first would silently reset the
    multiple-testing correction to its weakest setting at exactly the moment
    the evidence for a stronger one was lost.
    """


def trial_key(strategy: str, params: Dict[str, Any], instruments: Sequence[str],
              timeframe: str, data_window: str) -> str:
    """Stable identity of one (strategy, params, instruments, timeframe, window).

    Hash-keyed so that re-running the same backtest -- during development, in a
    test, after a crash -- does not inflate the count. Re-running an identical
    configuration is not a new trial; it produces the same number and reveals
    nothing new about the data.

    Every component is part of the key, and each for a reason:

    * ``params`` because a different channel length is a different trial. This
      is the component that grows fastest and the one people forget.
    * ``instruments`` because trying the same rule on five pairs and keeping
      the best one is five trials, not one.
    * ``timeframe`` because H1 and H4 are different trials on the same idea.
    * ``data_window`` because the SAME strategy evaluated on a longer or later
      history is genuinely new evidence, and charging it as a repeat would
      make the ledger punish honest out-of-sample extension.
    """
    body = json.dumps({
        "strategy": strategy,
        "params": params or {},
        "instruments": sorted(instruments or []),
        "timeframe": timeframe or "",
        "data_window": data_window or "",
    }, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def window_key(first_bar: Any, last_bar: Any, n_bars: int,
               data_label: str = "unknown") -> str:
    """Compact description of the data a trial was run on.

    Deliberately coarse -- first bar, last bar, bar count, provenance label --
    rather than a hash of the prices. A backtest re-run after a vendor revised
    two ticks is the same trial, and making it a different one would let a
    trivial data refresh reset the count to zero.
    """
    return f"{data_label}|{first_bar}|{last_bar}|{int(n_bars)}"


@dataclass
class TrialSummary:
    """What the ledger knows about the search behind one strategy."""

    strategy: str
    family: str
    strategy_trials: int = 0
    family_trials: int = 0
    sessions: int = 0
    total_runs: int = 0
    first_seen_ns: Optional[int] = None
    last_seen_ns: Optional[int] = None
    distinct_param_sets: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def charge(self) -> int:
        """The trial count this strategy should be charged with.

        The FAMILY count, not the strategy's own. Six trend systems evaluated
        on one history are six draws from the same idea, and the one that came
        out best was selected from all six -- so the selection has to be priced
        at six. Charging only the strategy's own parameter search would let a
        library grow without the multiple-testing bar moving, which is the
        exact failure this system is built to prevent.
        """
        return max(self.strategy_trials, self.family_trials)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy, "family": self.family,
            "strategy_trials": self.strategy_trials,
            "family_trials": self.family_trials,
            "charge": self.charge,
            "sessions": self.sessions, "total_runs": self.total_runs,
            "distinct_param_sets": self.distinct_param_sets,
            "first_seen_ns": self.first_seen_ns, "last_seen_ns": self.last_seen_ns,
            "notes": self.notes,
        }

    def sentence(self) -> str:
        """One line for the acceptance report. Plain words, no jargon."""
        if self.family_trials <= 0:
            return (f"no prior trials recorded for {self.strategy!r}; the ledger has "
                    "never seen this strategy, which is itself worth checking")
        return (f"the {self.family!r} family has been searched {self.family_trials} "
                f"times across {self.sessions} session(s) "
                f"({self.strategy_trials} of those on {self.strategy!r} itself)")


def _family_from_registry(strategy: str) -> Optional[str]:
    """The family a registered strategy belongs to, if it is registered.

    Imported lazily and defensively: the ledger must work in a bare research
    script with no registry loaded, and a registry import failure must never
    turn into "this strategy has never been searched".
    """
    try:
        from ..strategy.registry import family_of
    except Exception:  # noqa: BLE001
        return None
    try:
        return family_of(strategy)
    except Exception:  # noqa: BLE001
        return None


class TrialLedger:
    """Durable, append-only record of every backtested combination.

    Concurrency: one SQLite connection guarded by a lock, in WAL mode, exactly
    like ``VerdictStore``. Several processes may hold the same file; WAL makes
    concurrent readers safe and the writes here are single-row upserts.
    """

    def __init__(self, path: str | Path = "var/trials.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists() and self.path.stat().st_size > 0
        self._lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            with self._lock:
                # Migrate BEFORE creating the schema. A ledger written before
                # `kind` existed still has the old table, and the index in
                # SCHEMA names that column -- so creating the schema first
                # fails on a database that is perfectly readable and would be
                # reported as corrupt. The column defaults to 'search', which
                # is what every row in such a ledger was.
                existing = {r["name"] for r in self._conn.execute(
                    "PRAGMA table_info(trials)").fetchall()}
                if existing and "kind" not in existing:
                    self._conn.execute(
                        "ALTER TABLE trials ADD COLUMN kind TEXT NOT NULL "
                        "DEFAULT 'search'")
                self._conn.executescript(SCHEMA)
                self._conn.commit()
        except sqlite3.DatabaseError as exc:
            raise LedgerUnreadable(
                f"the trial ledger at {self.path} exists but is not a readable "
                f"database ({exc}). This is NOT evidence that nothing was ever "
                "searched: treating it that way would reset the multiple-testing "
                "correction to its weakest setting. Restore the file, or move it "
                "aside deliberately and declare the trial count by hand."
            ) from exc
        self._existed_on_open = existed

    # ------------------------------------------------------------------ #

    def record(
        self,
        *,
        strategy: str,
        family: str = "unclassified",
        params: Optional[Dict[str, Any]] = None,
        instruments: Sequence[str] = (),
        timeframe: str = "",
        data_window: str = "",
        data_label: str = "unknown",
        kind: str = "search",
        note: str = "",
        session_id: Optional[str] = None,
    ) -> str:
        """Record one trial. Returns its key.

        Idempotent on the key: a repeat bumps ``runs`` and ``last_seen_ns`` but
        does not create a second trial, because running the same configuration
        twice is not two searches.

        ``kind`` separates a *search* -- a configuration evaluated so that the
        best one can be chosen -- from a *validation* run of a configuration
        already counted. Only searches are counted, because only searches are
        draws from the distribution the deflated Sharpe corrects for. A run
        that arrives as a validation of a key first seen as a search does not
        downgrade it: the stricter classification wins.
        """
        if kind not in ("search", "validation"):
            raise ValueError("kind must be 'search' or 'validation'")
        key = trial_key(strategy, params or {}, instruments, timeframe, data_window)
        now = wall_ns()
        payload = json.dumps(params or {}, sort_keys=True, default=str)
        instr = json.dumps(sorted(instruments or []))
        with self._lock:
            self._conn.execute(
                "INSERT INTO trials (trial_key, strategy, family, timeframe, instruments,"
                " params, data_window, data_label, kind, first_seen_ns, last_seen_ns,"
                " runs, note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)"
                " ON CONFLICT(trial_key) DO UPDATE SET"
                "   last_seen_ns = excluded.last_seen_ns,"
                "   runs = trials.runs + 1,"
                # Once a key has been seen as a search it stays one. A later
                # validation pass over the same configuration must not be able
                # to erase the fact that it was searched over.
                "   kind = CASE WHEN trials.kind = 'search' OR excluded.kind = 'search'"
                "               THEN 'search' ELSE trials.kind END",
                (key, strategy, family, timeframe, instr, payload, data_window,
                 data_label, kind, now, now, note))
            self._conn.execute(
                "INSERT OR IGNORE INTO trial_sessions (trial_key, session_id, seen_ns)"
                " VALUES (?,?,?)",
                (key, session_id or SESSION_ID, now))
            self._conn.commit()
        self._existed_on_open = True
        return key

    def record_many(self, trials: Iterable[Dict[str, Any]]) -> List[str]:
        return [self.record(**t) for t in trials]

    # ------------------------------------------------------------------ #

    def count(self, *, strategy: Optional[str] = None,
              family: Optional[str] = None, kind: Optional[str] = "search") -> int:
        """Distinct trials matching the filter. Trials, not runs.

        ``kind`` defaults to ``"search"``: validation re-runs of an
        already-counted configuration are recorded for the audit trail but are
        not additional draws. Pass ``None`` to count everything.
        """
        q = "SELECT COUNT(*) AS c FROM trials"
        clauses, args = [], []
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        if strategy:
            clauses.append("strategy = ?")
            args.append(strategy)
        if family:
            clauses.append("family = ?")
            args.append(family)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        with self._lock:
            row = self._conn.execute(q, args).fetchone()
        return int(row["c"]) if row else 0

    def session_count(self, *, strategy: Optional[str] = None,
                      family: Optional[str] = None,
                      kind: Optional[str] = "search") -> int:
        q = ("SELECT COUNT(DISTINCT s.session_id) AS c FROM trial_sessions s "
             "JOIN trials t ON t.trial_key = s.trial_key")
        clauses, args = [], []
        if kind:
            clauses.append("t.kind = ?")
            args.append(kind)
        if strategy:
            clauses.append("t.strategy = ?")
            args.append(strategy)
        if family:
            clauses.append("t.family = ?")
            args.append(family)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        with self._lock:
            row = self._conn.execute(q, args).fetchone()
        return int(row["c"]) if row else 0

    def summary(self, strategy: str, family: Optional[str] = None) -> TrialSummary:
        """Everything the acceptance report needs about one strategy's search.

        ``family`` is resolved from the registry when not supplied, and falls
        back to whatever the ledger itself recorded. Getting it wrong
        understates the charge, so both sources are tried before giving up.
        """
        if family is None:
            # Ask the REGISTRY, then the ledger's own record, then give up.
            # The docstring above promised the registry and the code never
            # consulted it: a strategy the ledger had not seen resolved to
            # "unclassified", which has no trials, so a freshly-renamed
            # strategy was charged ZERO -- the whole multiple-testing bar
            # silently switched off for exactly the case (a new variant of an
            # existing idea) where it matters most.
            family = (_family_from_registry(strategy)
                      or self._recorded_family(strategy)
                      or "unclassified")
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS trials, SUM(runs) AS runs,"
                " MIN(first_seen_ns) AS first_ns, MAX(last_seen_ns) AS last_ns,"
                " COUNT(DISTINCT params) AS param_sets"
                " FROM trials WHERE strategy = ? AND kind = 'search'",
                (strategy,)).fetchone()
        summary = TrialSummary(
            strategy=strategy, family=family,
            strategy_trials=int(row["trials"] or 0),
            total_runs=int(row["runs"] or 0),
            first_seen_ns=row["first_ns"], last_seen_ns=row["last_ns"],
            distinct_param_sets=int(row["param_sets"] or 0),
        )
        summary.family_trials = self.count(family=family)
        summary.sessions = self.session_count(family=family)
        if summary.family_trials > summary.strategy_trials:
            summary.notes.append(
                f"charged at the {family!r} family count ({summary.family_trials}) "
                f"rather than this strategy's own ({summary.strategy_trials}): the "
                "winner of a family search was selected from the whole family")
        summary.notes.append(
            "the ledger is a FLOOR on the search: it cannot see runs made before it "
            "existed, on another machine, or by eye. Declare a larger number if you "
            "know of one -- the larger figure is the one that is used")
        return summary

    def _recorded_family(self, strategy: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT family FROM trials WHERE strategy = ? ORDER BY last_seen_ns DESC"
                " LIMIT 1", (strategy,)).fetchone()
        return row["family"] if row else None

    def list(self, *, strategy: Optional[str] = None, family: Optional[str] = None,
             limit: int = 200) -> List[Dict[str, Any]]:
        q = ("SELECT trial_key, strategy, family, timeframe, instruments, params,"
             " data_window, data_label, kind, first_seen_ns, last_seen_ns, runs"
             " FROM trials")
        clauses, args = [], []
        if strategy:
            clauses.append("strategy = ?")
            args.append(strategy)
        if family:
            clauses.append("family = ?")
            args.append(family)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY last_seen_ns DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    @property
    def was_present(self) -> bool:
        if self._existed_on_open:
            return True
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM trials").fetchone()
        return bool(row and row["c"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------- #
# Process-wide default, so recording is automatic rather than remembered
# --------------------------------------------------------------------------- #

_DEFAULT: Optional[TrialLedger] = None
_DEFAULT_LOCK = threading.Lock()


def set_default_ledger(ledger: Optional[TrialLedger | str | Path]) -> Optional[TrialLedger]:
    """Install the ledger that ``run_backtest`` records into. ``None`` disables it."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if ledger is None or isinstance(ledger, TrialLedger):
            _DEFAULT = ledger
        else:
            _DEFAULT = TrialLedger(ledger)
        return _DEFAULT


def default_ledger() -> Optional[TrialLedger]:
    """The active ledger, opening one from ``SENTINEL_TRIAL_LEDGER`` if set.

    Returns ``None`` when nothing is configured, and callers treat that as "do
    not record". That is a real hole and it is left open on purpose: making
    every backtest in every test and notebook write to a shared database would
    be worse, and a silent failure to open a database is worse still.

    The hole is closed where it matters. ``scripts/run_acceptance.py`` always
    installs a ledger from its ``--state-dir``, so the one workflow that can
    promote a strategy to real money always counts its own search, and always
    prints how much of the search it could see.
    """
    global _DEFAULT
    if _DEFAULT is not None:
        return _DEFAULT
    env = os.environ.get("SENTINEL_TRIAL_LEDGER")
    if not env:
        return None
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = TrialLedger(env)
    return _DEFAULT


def record_trial(**kwargs: Any) -> Optional[str]:
    """Record into the default ledger if there is one. Never raises.

    A failure to write the ledger must not abort a research run: the run is the
    thing that produces evidence, and losing it to a disk error while trying to
    write bookkeeping would be the wrong trade. The cost is an undercount, and
    an undercount is visible in the acceptance report as a low trial number
    next to the warning that the ledger is only a floor.
    """
    ledger = default_ledger()
    if ledger is None:
        return None
    try:
        return ledger.record(**kwargs)
    except (sqlite3.Error, LedgerUnreadable, OSError, ValueError, TypeError):
        return None


def effective_trial_count(declared: int, ledger: int = 0, variants: int = 0,
                          floor: int = 2) -> int:
    """The count the deflated-Sharpe gate must use.

    The largest of every available figure, because each is a lower bound on the
    search and none of them can see the others:

    * ``declared``  -- what the operator says they tried, including work this
      process never saw.
    * ``ledger``    -- what has actually been recorded for this family.
    * ``variants``  -- what THIS run evaluated. A trial you just ran is a trial
      that counts, whatever you declared beforehand.
    * ``floor``     -- never below 2. ``expected_max_sharpe`` returns 0 for
      n < 2, so a count of 1 would set the bar to zero and turn the one gate
      whose purpose is to price in multiple testing into "is the Sharpe
      positive".
    """
    return max(int(declared or 0), int(ledger or 0), int(variants or 0), int(floor))
