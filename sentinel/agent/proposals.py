"""Self-improvement proposals.

The agent may propose changes to its own parameters. It may not apply them.

A proposal carries: the evidence that produced it, the exact parameter change,
the expected effect with its uncertainty, and the validation run that must pass
before it can be adopted. The dashboard shows the queue; a human approves or
rejects; approval triggers a validation backtest; only a passing validation
makes the change eligible; and the change is then applied to the *paper*
configuration first.

This is slow on purpose. The alternative -- an agent that retunes itself from
recent results -- is a machine for fitting the last hundred trades, and it
reaches maximum confidence exactly when the recent past is least like the
future.

Three hard rules, enforced in code below:

* **Risk limits are never proposable.** Loss budgets, drawdown ceilings, the
  ladder, the frequency caps and the venue-side-stop requirement are outside
  the agent's reach entirely.
* **Every proposal is bounded.** A single change may move a parameter by at
  most ``max_relative_change``, and only within the schema's own range.
* **Proposals expire.** Evidence goes stale; an unreviewed proposal is dropped
  rather than accumulating into a backlog someone eventually rubber-stamps.

And three statistical rules, because the guards above only constrain what a
proposal may ask for, not whether it should have existed:

* **The p-value must be the corrected one.** ``aggregate`` tests fifteen to
  twenty-five patterns against the same trades. At alpha 0.05 that finds a
  winner by chance on nearly every pass, and a proposal built on it is
  indistinguishable from one built on evidence. ``propose`` therefore reads
  ``finding.p_value_adjusted`` and refuses a finding that ``aggregate`` did not
  mark significant after Benjamini-Hochberg.

* **The diagnosis must be "parameter".** A significant pattern has three common
  explanations and only one of them is a wrong setting: the market state
  changed, we were unlucky, or the parameter is wrong -- in descending order of
  likelihood. A regime-scoped finding becomes a regime-scoped LESSON, which is
  reversible and applies only where the evidence came from; it does not become a
  global parameter change.

* **The evidence must be computable.** A counterfactual that needed price data
  nobody recorded is excluded upstream rather than filled in, so no proposal can
  rest on it. ``wider_stop_1.5x`` is the whole reason: it needed prices from
  after the position was closed, and the old formula supplied a favourable
  answer every time.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from ..core.clock import wall_ns
from .postmortem import PatternFinding

# Parameters the agent may never touch, at any confidence, for any reason.
FROZEN_PATHS = {
    "risk.daily_loss_limit_pct", "risk.weekly_loss_limit_pct",
    "risk.monthly_loss_limit_pct", "risk.max_drawdown_halt_pct",
    "risk.ladder", "risk.ladder_enabled", "risk.require_broker_side_stop",
    "risk.max_trades_per_day", "risk.max_trades_per_week", "risk.max_trades_per_year",
    "risk.max_annual_cost_pct_of_equity", "risk.max_gross_leverage",
    "risk.max_currency_exposure_pct", "risk.max_correlated_risk_pct",
    "risk.risk_per_trade_pct", "risk.max_lots_per_trade",
    "execution.venue_mode", "execution.broker",
    "agent.mode", "agent.proposal_requires_human",
    "security.dashboard_read_only_default", "security.require_totp_for_writes",
    "security.bind_host", "security.allowed_origins",
    "research.alpha", "research.pbo_max", "research.min_dsr",
    "research.declared_prior", "research.cpcv_positive_path_fraction",
}

# Parameters the agent may propose to change, with their absolute bounds.
# Every entry carries a DIRECTION. The architecture's central claim is that a
# lesson can only ever counsel LESS risk; the proposal table was the one place
# that claim was not enforced in code, and all nine risk-increasing directions
# were accepted. The 35% step cap only slowed it down, because each approval
# re-bases the current value: the news blackout could go 30 -> 19 -> 12 -> 8
# minutes in three approvals, and the only give-back protection could be
# proposed to zero.
#
#   "safer_is_lower"   a DECREASE reduces risk (so only decreases are allowed)
#   "safer_is_higher"  an INCREASE reduces risk
#   "either"           genuinely two-sided; no risk direction
PROPOSABLE: dict[str, dict[str, Any]] = {
    # Protect earlier, bank earlier, trail tighter: all safer when LOWER.
    "risk.breakeven_trigger_r": {"min": 0.0, "max": 3.0, "kind": "float",
                                 "direction": "safer_is_lower"},
    "risk.partial_take_r": {"min": 0.5, "max": 5.0, "kind": "float",
                            "direction": "safer_is_lower"},
    "risk.trail_atr_multiple": {"min": 0.5, "max": 6.0, "kind": "float",
                                "direction": "safer_is_lower"},
    "risk.trail_activate_r": {"min": 0.0, "max": 3.0, "kind": "float",
                              "direction": "safer_is_lower"},
    "risk.max_spread_pips_multiple": {"min": 1.1, "max": 5.0, "kind": "float",
                                      "direction": "safer_is_lower"},
    "risk.giveback_arm_r": {"min": 0.5, "max": 5.0, "kind": "float",
                            "direction": "safer_is_lower"},
    # Bank MORE of the position, wait LONGER between entries, demand a BETTER
    # reward:risk, keep a WIDER minimum stop, sit out a LONGER news window:
    # all safer when HIGHER.
    "risk.partial_take_fraction": {"min": 0.1, "max": 0.8, "kind": "float",
                                   "direction": "safer_is_higher"},
    "risk.min_reward_risk": {"min": 1.0, "max": 5.0, "kind": "float",
                             "direction": "safer_is_higher"},
    "risk.min_stop_pips": {"min": 5.0, "max": 100.0, "kind": "float",
                           "direction": "safer_is_higher"},
    "risk.min_seconds_between_entries": {"min": 60, "max": 86400, "kind": "int",
                                         "direction": "safer_is_higher"},
    "risk.block_minutes_before_high_impact": {"min": 5, "max": 240, "kind": "int",
                                              "direction": "safer_is_higher"},
    "risk.block_minutes_after_high_impact": {"min": 5, "max": 240, "kind": "int",
                                             "direction": "safer_is_higher"},
    "risk.giveback_keep_fraction": {"min": 0.2, "max": 0.9, "kind": "float",
                                    "direction": "safer_is_higher"},
    # A strategy's own parameters carry no inherent risk direction.
    "strategy.params": {"kind": "dict", "direction": "either"},
}


@dataclass
class Proposal:
    id: str
    created_ns: int
    path: str
    current_value: Any
    proposed_value: Any
    rationale: str
    evidence: dict[str, Any]
    sample_size: int
    expected_effect_r: float
    effect_ci_low: float
    effect_ci_high: float
    p_value: float
    status: str = "pending"            # pending|approved|rejected|validated|applied|expired
    validation_run_id: str | None = None
    reviewed_by: str | None = None
    reviewed_ns: int | None = None
    expires_ns: int = 0
    strategy: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class ProposalError(ValueError):
    pass


def _clamp(path: str, value: Any) -> Any:
    spec = PROPOSABLE.get(path)
    if spec is None:
        raise ProposalError(f"{path} is not a proposable parameter")
    if spec["kind"] == "dict":
        return value
    lo, hi = spec["min"], spec["max"]
    v = max(lo, min(hi, float(value)))
    return int(round(v)) if spec["kind"] == "int" else round(v, 4)


def propose(
    *,
    path: str,
    current_value: Any,
    proposed_value: Any,
    rationale: str,
    finding: PatternFinding,
    strategy: str | None = None,
    max_relative_change: float = 0.35,
    ttl_days: int = 21,
    min_sample: int = 40,
    alpha: float = 0.01,
) -> Proposal:
    if path in FROZEN_PATHS:
        raise ProposalError(
            f"{path} is a hard risk control and is outside the agent's reach. "
            "Only a human, through an authenticated write, can change it.")
    if path not in PROPOSABLE:
        raise ProposalError(f"{path} is not in the proposable set")
    if finding.n < min_sample:
        raise ProposalError(
            f"only {finding.n} observations (minimum {min_sample}); the evidence is "
            "too thin to propose a change")

    # A proposal may only ever move a parameter in the SAFER direction. The
    # learning loop exists to counsel caution; without this, an approved
    # sequence could walk the news blackout to zero or switch the give-back
    # ratchet off, one legal 35% step at a time.
    spec = PROPOSABLE[path]

    # CLAMP FIRST, then check the direction against the value that would
    # actually be stored. Checking the raw proposal instead let the clamp flip
    # the sign afterwards: giveback_arm_r 0.3 -> a "safer" 0.2 clamps up to the
    # table's 0.5 minimum, which is an INCREASE, and the guard had already
    # waved it through. The value that matters is the one that lands in the
    # config, not the one that was asked for.
    clamped = _clamp(path, proposed_value)

    direction = spec.get("direction", "either")
    if direction != "either" and spec["kind"] != "dict" and current_value is not None:
        try:
            cur_f, new_f = float(current_value), float(clamped)
        except (TypeError, ValueError):
            cur_f = new_f = None
        if cur_f is not None and new_f != cur_f:
            increasing = new_f > cur_f
            if (direction == "safer_is_lower" and increasing) or \
               (direction == "safer_is_higher" and not increasing):
                raise ProposalError(
                    f"{path}: {cur_f} -> {new_f} moves toward MORE risk "
                    f"(the proposed {proposed_value} clamps to {new_f}). The "
                    "learning loop may only propose changes that reduce risk; a "
                    "change in this direction is a decision for a human.")

    # Statistical gates run AFTER the direction guard, so the refusal an
    # operator sees for a risk-increasing proposal is the safety one. Both
    # refuse; the more serious reason should be the one reported.
    #
    # The RAW p-value is not the one that matters. `aggregate` corrects across
    # the family it tested in one pass; a finding it did not mark significant is
    # one of the several a 20-hypothesis screen throws up by chance.
    effective_p = finding.p_value_adjusted if finding.n_tests_in_family else finding.p_value
    if effective_p >= alpha:
        raise ProposalError(
            f"p = {effective_p:.4f} (raw {finding.p_value:.4f}, corrected across "
            f"{finding.n_tests_in_family or 1} tests) does not clear the declared alpha "
            f"of {alpha}")
    if finding.n_tests_in_family and not finding.significant:
        raise ProposalError(
            f"{finding.pattern} did not survive false-discovery correction across the "
            f"{finding.n_tests_in_family} patterns tested in the same pass; a screen that "
            "wide produces a winner by chance on nearly every run")
    # "" means undiagnosed -- a finding built by hand or by an older caller.
    # Every finding `aggregate` produces carries a real diagnosis, so in the
    # agent's own loop this gate always has something to check.
    if finding.diagnosis in ("luck", "regime", "insufficient", "descriptive"):
        raise ProposalError(
            f"{finding.pattern} is diagnosed as {finding.diagnosis!r}, not a wrong "
            f"parameter. {finding.diagnosis_note} A parameter change is the wrong "
            "instrument for this: it applies everywhere and it is not reversible by "
            "the evidence that produced it.")

    # Dry-run the merged configuration so an out-of-range combination is caught
    # at proposal time rather than when someone tries to apply it.
    if path.startswith("risk.") and spec["kind"] != "dict":
        try:
            from ..core.config import RiskConfig
            field = path.split(".", 1)[1]
            RiskConfig(**{field: clamped})
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001 - pydantic validation error
            raise ProposalError(
                f"{path}={clamped} would produce an invalid risk configuration: {exc}"
            ) from exc

    if PROPOSABLE[path]["kind"] != "dict" and current_value not in (None, 0):
        try:
            rel = abs(float(clamped) - float(current_value)) / max(1e-9, abs(float(current_value)))
            if rel > max_relative_change:
                # Move only as far as the cap allows, in the proposed direction.
                # Named `sign`, not `direction`: `direction` is the parameter's
                # RISK direction and rebinding it here would quietly disable the
                # post-step re-check below.
                sign = 1.0 if float(clamped) > float(current_value) else -1.0
                limited = float(current_value) * (1 + sign * max_relative_change)
                clamped = _clamp(path, limited)
                rationale += (f" (step limited to {max_relative_change * 100:.0f}% of the "
                              "current value: large parameter jumps are not learning, "
                              "they are a different strategy)")
        except (TypeError, ValueError):
            pass

    # Re-check the direction against the FINAL value. The step limiter runs
    # after the direction guard and re-clamps its own output, so the invariant
    # has to be asserted on the number that actually lands in the config rather
    # than on the one the guard happened to see. Belt and braces: the limiter
    # interpolates toward the already-checked value, so this should be
    # unreachable -- which is exactly why it is cheap to assert and expensive to
    # assume.
    if direction != "either" and spec["kind"] != "dict" and current_value is not None:
        try:
            cur_f, final_f = float(current_value), float(clamped)
        except (TypeError, ValueError):
            cur_f = final_f = None
        if cur_f is not None and final_f != cur_f:
            if (direction == "safer_is_lower" and final_f > cur_f) or \
               (direction == "safer_is_higher" and final_f < cur_f):
                raise ProposalError(
                    f"{path}: the bounded step landed on {final_f}, which moves from "
                    f"{cur_f} toward MORE risk. Refused after the step limit, not only "
                    "before it.")

    se = abs(finding.mean_delta_r) / max(1e-9, abs(finding.t_stat)) if finding.t_stat else 0.0
    half = 1.96 * se
    return Proposal(
        id=f"P{wall_ns() // 1_000_000:x}",
        created_ns=wall_ns(), path=path, current_value=current_value,
        proposed_value=clamped, rationale=rationale,
        evidence=finding.to_dict(), sample_size=finding.n,
        expected_effect_r=finding.mean_delta_r,
        effect_ci_low=round(finding.mean_delta_r - half, 4),
        effect_ci_high=round(finding.mean_delta_r + half, 4),
        p_value=finding.p_value, strategy=strategy,
        expires_ns=wall_ns() + ttl_days * 86400 * 1_000_000_000,
    )


def derive_proposals(
    findings: Sequence[PatternFinding],
    current_config: dict[str, Any],
    *,
    strategy: str | None = None,
    min_sample: int = 40,
    alpha: float = 0.01,
    min_effect_r: float = 0.10,
) -> list[Proposal]:
    """Map supported findings onto concrete, bounded parameter changes.

    "Supported" now means: significant after false-discovery correction, large
    enough to be worth a change, and diagnosed as a parameter problem rather
    than a regime shift or a run of luck. A finding that fails the last test is
    not discarded -- ``regime_lessons`` turns it into a scoped, reversible
    lesson instead, which is the honest response to "this is true in stress and
    nowhere else".
    """
    out: list[Proposal] = []

    def cur(path: str, default: Any) -> Any:
        node: Any = current_config
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def supported(f: PatternFinding) -> bool:
        p_eff = f.p_value_adjusted if f.n_tests_in_family else f.p_value
        return (p_eff < alpha and f.n >= min_sample
                and (f.significant or not f.n_tests_in_family)
                and f.diagnosis not in ("luck", "regime", "insufficient", "descriptive"))

    for f in findings:
        if not supported(f):
            continue
        try:
            if f.pattern == "counterfactual:partial_at_1R" and f.mean_delta_r > min_effect_r:
                current = float(cur("risk.partial_take_r", 1.5))
                out.append(propose(
                    path="risk.partial_take_r", current_value=current,
                    proposed_value=max(1.0, current * 0.75),
                    rationale=(f"over {f.n} trades that reached +1R -- winners included, "
                               f"where banking half COSTS the runner -- taking half there "
                               f"would have added {f.mean_delta_r:+.2f}R on average"),
                    finding=f, strategy=strategy, min_sample=min_sample, alpha=alpha))
            elif f.pattern == "counterfactual:breakeven_at_1R" and f.mean_delta_r > min_effect_r:
                current = float(cur("risk.breakeven_trigger_r", 1.0))
                out.append(propose(
                    path="risk.breakeven_trigger_r", current_value=current,
                    proposed_value=max(0.5, current * 0.8),
                    rationale=(f"on {f.n} trades that armed the rule, moving the stop to "
                               f"entry was worth {f.mean_delta_r:+.2f}R against what "
                               "actually happened, measured on the real price path"),
                    finding=f, strategy=strategy, min_sample=min_sample, alpha=alpha))
            elif f.pattern == "counterfactual:trail_tighter" and f.mean_delta_r > min_effect_r:
                current = float(cur("risk.trail_atr_multiple", 2.5))
                out.append(propose(
                    path="risk.trail_atr_multiple", current_value=current,
                    proposed_value=max(1.0, current * 0.8),
                    rationale=(f"a tighter give-back floor was worth {f.mean_delta_r:+.2f}R "
                               f"over {f.n} trades that armed it"),
                    finding=f, strategy=strategy, min_sample=min_sample, alpha=alpha))
            elif f.pattern == "tag:entry_slippage" and f.mean_delta_r < -min_effect_r:
                # A tag finding's mean_delta_r is tagged-minus-untagged, so an
                # underperforming tag is NEGATIVE. The old guard demanded
                # `mean_delta_r > 0.10` for every finding before reaching this
                # branch, which made it unreachable: the one proposal derived
                # from a tag could never fire, and nobody noticed because the
                # loop simply produced nothing.
                current = float(cur("risk.max_spread_pips_multiple", 2.5))
                out.append(propose(
                    path="risk.max_spread_pips_multiple", current_value=current,
                    proposed_value=max(1.2, current * 0.8),
                    rationale=(f"trades with entry slippage underperform by "
                               f"{abs(f.mean_delta_r):.2f}R over {f.n} of them; tighten "
                               "the spread gate"),
                    finding=f, strategy=strategy, min_sample=min_sample, alpha=alpha))
            elif f.pattern == "tag:news_adjacent" and f.mean_delta_r < -min_effect_r:
                current = float(cur("risk.block_minutes_before_high_impact", 30))
                out.append(propose(
                    path="risk.block_minutes_before_high_impact", current_value=current,
                    proposed_value=min(240, current * 1.3),
                    rationale=(f"trades taken near a news event underperform by "
                               f"{abs(f.mean_delta_r):.2f}R over {f.n} of them; sit out "
                               "longer before the release"),
                    finding=f, strategy=strategy, min_sample=min_sample, alpha=alpha))
            # NOTE: there is deliberately no proposal derived from
            # `counterfactual:wider_stop_1.5x`. That counterfactual is marked
            # not-computable upstream, because scoring it needs prices from
            # after the position was closed -- which were never recorded. The
            # old version proposed a 30% WIDER minimum stop from a formula that
            # assumed the stopped-out trade recovered. It cleared the direction
            # guard (a wider stop with a constant risk budget is a smaller
            # position, so nominally safer) and was pure invention underneath.
        except ProposalError:
            continue
    return out


def regime_lessons(findings: Sequence[PatternFinding], *,
                   strategy: str | None = None,
                   min_sample: int = 40,
                   alpha: float = 0.01) -> list[dict[str, Any]]:
    """Findings that are real but confined to one market state.

    These must NOT become parameter changes. A parameter applies in every
    regime; the evidence came from one. What they become instead is a lesson
    scoped to that regime -- recalled only there, capped at a caution multiplier
    that can only shrink a position, and retired automatically when fresh trades
    stop supporting it.

    Two filters that matter more than they look:

    * **Only adverse effects become lessons.** A lesson's single power is a
      caution multiplier bounded above by 1.0, so a favourable regime finding
      has nothing to express: it would be a regime-scoped parameter change,
      which this system deliberately cannot represent. Emitting it anyway
      produces a lesson with caution 1.0 -- an entry in the store that does
      nothing, which is how a lesson list becomes noise nobody reads.

    * **One lesson per regime per pass.** ``tag:regime:stress`` and
      ``tag:entry_slippage`` routinely describe the SAME fifty-four trades. Two
      lessons from one body of evidence multiply their cautions together --
      0.70 x 0.70 = 0.49 -- so the agent halves its size on the strength of
      counting the same trades twice. The largest effect wins and the rest are
      recorded in its evidence.

    Returned as plain dictionaries so this module does not import the memory
    store. The caller builds the ``Lesson``.
    """
    best: dict[str, dict[str, Any]] = {}
    for f in findings:
        if f.diagnosis != "regime" or f.n < min_sample:
            continue
        if f.dominant_regime in ("", "unknown"):
            continue
        p_eff = f.p_value_adjusted if f.n_tests_in_family else f.p_value
        if p_eff >= alpha:
            continue
        if f.mean_delta_r >= 0:
            # Nothing a lesson can say. See the docstring.
            continue
        # Caution is bounded and one-directional: an effect of -0.4R maps to a
        # 0.8 multiplier, and nothing here can ever produce a number above 1.0.
        caution = max(0.5, min(1.0, 1.0 + f.mean_delta_r / 2.0))
        candidate = {
            "scope": "regime",
            "strategy": strategy,
            "regime": f.dominant_regime,
            "statement": (f"in the {f.dominant_regime} regime, {f.pattern} runs "
                          f"{f.mean_delta_r:+.2f}R against the rest of the sample "
                          f"({f.regime_concentration * 100:.0f}% of the effect is in "
                          "that regime alone)"),
            "evidence": dict(f.to_dict(), also_seen=[]),
            "sample_size": f.n,
            "effect_r": f.mean_delta_r,
            "p_value": p_eff,
            "caution": caution,
        }
        prior = best.get(f.dominant_regime)
        if prior is None:
            best[f.dominant_regime] = candidate
        elif abs(f.mean_delta_r) > abs(prior["effect_r"]):
            candidate["evidence"]["also_seen"] = (
                prior["evidence"].get("also_seen", []) + [prior["evidence"]["pattern"]])
            best[f.dominant_regime] = candidate
        else:
            prior["evidence"].setdefault("also_seen", []).append(f.pattern)
    return list(best.values())


class ProposalQueue:
    """In-memory queue with a JSON file behind it. Small by design."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self.items: dict[str, Proposal] = {}
        if path:
            self._load()

    def _load(self) -> None:
        import os

        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                for row in json.load(fh):
                    self.items[row["id"]] = Proposal(**row)
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    def _save(self) -> None:
        """Rewrite the queue atomically, with a UNIQUE staging file.

        A fixed ``<name>.tmp`` is shared by every writer, so two concurrent
        saves raced: a reader observed an unparseable file 141 times out of
        286, and `_load` swallows JSONDecodeError -- which would silently
        discard every pending proposal at the next restart.
        """
        if not self.path:
            return
        import os
        import tempfile

        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix="proposals.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump([p.to_dict() for p in self.items.values()], fh,
                          ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def add(self, proposal: Proposal) -> None:
        # Supersede an existing pending proposal for the same path rather than
        # letting near-duplicates pile up.
        for existing in list(self.items.values()):
            if existing.path == proposal.path and existing.status == "pending":
                existing.status = "expired"
        self.items[proposal.id] = proposal
        self._save()

    def expire_stale(self, now_ns: int | None = None) -> int:
        now = now_ns or wall_ns()
        n = 0
        for p in self.items.values():
            if p.status == "pending" and p.expires_ns and now > p.expires_ns:
                p.status = "expired"
                n += 1
        if n:
            self._save()
        return n

    def pending(self) -> list[Proposal]:
        self.expire_stale()
        return [p for p in self.items.values() if p.status == "pending"]

    def review(self, proposal_id: str, approve: bool, reviewer: str) -> Proposal:
        p = self.items.get(proposal_id)
        if p is None:
            raise KeyError(proposal_id)
        if p.status != "pending":
            raise ProposalError(f"proposal {proposal_id} is {p.status}, not pending")
        p.status = "approved" if approve else "rejected"
        p.reviewed_by = reviewer
        p.reviewed_ns = wall_ns()
        self._save()
        return p

    def mark_validated(self, proposal_id: str, run_id: str, passed: bool) -> Proposal:
        p = self.items.get(proposal_id)
        if p is None:
            raise KeyError(proposal_id)
        if p.status != "approved":
            raise ProposalError("only an approved proposal can be validated")
        p.validation_run_id = run_id
        p.status = "validated" if passed else "rejected"
        self._save()
        return p

    def mark_applied(self, proposal_id: str) -> Proposal:
        p = self.items.get(proposal_id)
        if p is None:
            raise KeyError(proposal_id)
        if p.status != "validated":
            raise ProposalError(
                "a proposal must pass a validation run before it can be applied")
        p.status = "applied"
        self._save()
        return p

    def all(self) -> list[Proposal]:
        return sorted(self.items.values(), key=lambda p: p.created_ns, reverse=True)
