"""The complete, validated parameter surface of the agent.

Everything the dashboard can tune lives here. Three properties matter:

1. **Validated at the edge.** A bad number is rejected when it is *set*, not
   discovered when it produces a 40-lot order at 03:00.
2. **Versioned.** Every mutation produces a new ``version`` and is written to
   the audit chain, so any decision can be replayed against the exact config
   that produced it.
3. **Safe by construction.** Defaults are the conservative end of every range,
   ``mode`` starts at ADVISORY, and ``live`` execution is gated behind a
   separate explicit flag that the dashboard cannot flip on its own.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .clock import utc_now, wall_ns


class AgentMode(str, Enum):
    """How much authority the agent has.

    OBSERVE     -- computes everything, places nothing, not even paper orders.
    ADVISORY    -- emits signed proposals for a human to accept or reject.
    SEMI_AUTO   -- executes automatically, but only inside a pre-approved
                   envelope (instrument set, size band, session window);
                   anything outside becomes an advisory proposal.
    AUTONOMOUS  -- executes without per-trade approval, still fully subject to
                   the independent risk engine, which it cannot override.
    """

    OBSERVE = "observe"
    ADVISORY = "advisory"
    SEMI_AUTO = "semi_auto"
    AUTONOMOUS = "autonomous"


class ExecutionVenueMode(str, Enum):
    PAPER = "paper"      # internal simulator
    DEMO = "demo"        # broker demo account
    LIVE = "live"        # real money


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=False,
                              allow_inf_nan=False)


# --------------------------------------------------------------------------- #


class RiskConfig(StrictModel):
    """The independent risk envelope.

    None of these can be relaxed by a strategy, by the LLM, or by the agent's
    self-improvement loop. Changing them requires a confirmed write through the
    API (TOTP) and is recorded in the audit chain.
    """

    # --- per-trade -------------------------------------------------------- #
    risk_per_trade_pct: Decimal = Field(
        Decimal("0.50"), ge=Decimal("0.01"), le=Decimal("2.0"),
        description="Percent of equity risked between entry and stop, per trade.",
    )
    max_lots_per_trade: Decimal = Field(Decimal("5"), gt=0, le=Decimal("100"))
    min_stop_pips: Decimal = Field(
        Decimal("10"), ge=Decimal("1"),
        description="Below this the cost barrier (see money.break_even_win_rate) "
                    "makes positive expectancy implausible.",
    )
    max_stop_pips: Decimal = Field(Decimal("250"), gt=0)
    min_reward_risk: Decimal = Field(
        Decimal("1.2"), ge=Decimal("0.3"),
        description="Refuse entries whose take-profit/stop ratio is below this.",
    )
    require_broker_side_stop: bool = Field(
        True,
        description="Never hold a position whose stop lives only in our process. "
                    "An Iran->offshore link drops often enough that a local-only "
                    "stop is equivalent to no stop.",
    )

    # --- portfolio -------------------------------------------------------- #
    max_open_positions: int = Field(4, ge=0, le=50)
    max_positions_per_instrument: int = Field(1, ge=0, le=10)
    max_gross_leverage: Decimal = Field(Decimal("5"), gt=0, le=Decimal("100"))
    max_currency_exposure_pct: Decimal = Field(
        Decimal("1.25"), gt=0,
        description="Net risk on any single currency leg, in percent of equity. "
                    "Four 'different' long trades can be one short-USD bet.",
    )
    max_correlated_risk_pct: Decimal = Field(Decimal("0.90"), gt=0)
    max_total_open_risk_pct: Decimal = Field(
        Decimal("2.00"), gt=0,
        description="Total risk across open positions AND orders still in flight. "
                    "Without this, pending orders are invisible to every portfolio "
                    "limit and a burst of entries can commit far more than intended.",
    )
    # --- across accounts ---------------------------------------------------- #
    max_group_open_risk_pct: Decimal = Field(
        Decimal("3.00"), gt=0,
        description="Total committed risk across every engine sharing "
                    "ops.group_ledger_dir, as a percent of the GROUP's equity. Two "
                    "accounts each at 2% are 4% of the owner's capital; this is the "
                    "number that bounds the owner, not the account.",
    )
    max_group_currency_exposure_pct: Decimal = Field(
        Decimal("2.00"), gt=0,
        description="Net risk on one currency leg across the group, percent of group equity.",
    )
    group_ledger_stale_sec: int = Field(
        300, ge=30, le=3600,
        description="A member row older than this is treated as unknown, and an "
                    "unknown member blocks new entries: risk you cannot see is not "
                    "risk you have bounded.",
    )
    correlation_threshold: float = Field(0.65, ge=0.0, le=1.0)
    correlation_lookback_bars: int = Field(240, ge=30, le=5000)

    # --- loss budgets ------------------------------------------------------ #
    daily_loss_limit_pct: Decimal = Field(Decimal("2.0"), gt=0, le=Decimal("20"))
    weekly_loss_limit_pct: Decimal = Field(Decimal("4.0"), gt=0, le=Decimal("40"))
    monthly_loss_limit_pct: Decimal = Field(Decimal("6.0"), gt=0, le=Decimal("60"))
    max_drawdown_halt_pct: Decimal = Field(
        Decimal("10.0"), gt=0, le=Decimal("50"),
        description="Peak-to-trough equity drawdown that halts the agent "
                    "outright and requires a human restart.",
    )

    # --- frequency (a first-class risk limit, brief section B-3) ----------- #
    max_trades_per_day: int = Field(6, ge=0, le=500)
    max_trades_per_week: int = Field(20, ge=0, le=2000)
    max_trades_per_year: int = Field(500, ge=0, le=100000)
    min_seconds_between_entries: int = Field(300, ge=0, le=86400)
    max_annual_cost_pct_of_equity: Decimal = Field(
        Decimal("15"), gt=0,
        description="Projected yearly trading cost ceiling. Above this the "
                    "engine refuses new entries regardless of signal quality.",
    )

    # --- drawdown ladder --------------------------------------------------- #
    ladder_enabled: bool = True
    ladder: List[Dict[str, float]] = Field(
        default_factory=lambda: [
            {"drawdown_pct": 3.0, "risk_multiplier": 0.75},
            {"drawdown_pct": 5.0, "risk_multiplier": 0.50},
            {"drawdown_pct": 7.0, "risk_multiplier": 0.25},
            {"drawdown_pct": 8.5, "risk_multiplier": 0.0},
        ],
        description="Monotone de-risking schedule, fixed BEFORE evaluation. "
                    "Changing it after seeing a loss is a new experiment.",
    )

    # --- profit protection -------------------------------------------------- #
    breakeven_trigger_r: Decimal = Field(
        Decimal("1.0"), ge=0,
        description="Move stop to entry once open profit reaches this many R. "
                    "0 disables.",
    )
    partial_take_r: Decimal = Field(Decimal("1.5"), ge=0)
    partial_take_fraction: Decimal = Field(Decimal("0.5"), ge=0, le=Decimal("1"))
    trail_atr_multiple: Decimal = Field(Decimal("2.5"), ge=0)
    trail_activate_r: Decimal = Field(Decimal("1.0"), ge=0)

    # --- give-back ratchet -------------------------------------------------- #
    # The ATR trail is the only mechanism that converts an open gain into a
    # tighter stop, and it needs an ATR. When the frame is short, or the ATR is
    # NaN, the trail silently does nothing and a position at +8R is protected
    # only by the break-even stop. Measured over 132 trades the agent generated
    # for itself, the mean give-back from peak to exit was 0.65R and 11% of
    # trades that had been +1.3R or better closed at or below zero.
    #
    # This ratchet needs no indicator: the broker already maintains
    # max_favourable on the position. Once the peak reaches `arm`, the stop is
    # moved to the price that preserves `keep` of that peak.
    giveback_arm_r: Decimal = Field(
        Decimal("1.5"), ge=0,
        description="Arm the give-back ratchet once open profit has peaked at "
                    "this many R. 0 disables it.",
    )
    giveback_keep_fraction: Decimal = Field(
        Decimal("0.5"), ge=0, le=Decimal("0.95"),
        description="Fraction of the peak open profit the ratchet refuses to "
                    "give back. 0.5 keeps half of the best excursion.",
    )

    # --- horizon ------------------------------------------------------------ #
    max_hold_sec: int = Field(
        0, ge=0, le=2592000,
        description="Close a position held this long regardless of P&L. A trade "
                    "far past its intended horizon is no longer the trade that "
                    "was tested, and it pays financing the whole time. "
                    "0 disables; the agent derives a default from the signal "
                    "horizon when this is 0.",
    )

    daily_profit_lock_pct: Decimal = Field(
        Decimal("3.0"), ge=0,
        description="Once the day is this far up, stop opening new positions "
                    "AND tighten every open stop to preserve the day's gain. "
                    "The latch holds for the rest of the venue day.",
    )
    profit_lock_keep_fraction: Decimal = Field(
        Decimal("0.6"), ge=0, le=Decimal("1"),
        description="When the daily profit lock trips, protect at least this "
                    "fraction of the day's gain by tightening open stops.",
    )

    # --- drawdown ladder hysteresis ----------------------------------------- #
    ladder_hysteresis_pct: Decimal = Field(
        Decimal("0.5"), ge=0, le=Decimal("5"),
        description="The ladder steps DOWN at its drawdown threshold but steps "
                    "back UP only once drawdown has recovered this much further. "
                    "Without a band, two entries minutes apart on either side of "
                    "a boundary get different budgets for no reason an operator "
                    "can act on.",
    )

    # --- rolling loss budget ------------------------------------------------ #
    rolling_24h_loss_limit_pct: Decimal = Field(
        Decimal("2.5"), ge=0,
        description="Loss budget over a rolling 24h window, beside the calendar "
                    "day. The calendar day rolls at a fixed hour, so a loss "
                    "straddling that boundary otherwise gets two full budgets.",
    )

    # --- data / environment guards ----------------------------------------- #
    max_data_staleness_sec: int = Field(90, ge=1, le=3600)
    max_clock_skew_ms: float = Field(750.0, gt=0)
    max_spread_pips_multiple: Decimal = Field(
        Decimal("2.5"), gt=0,
        description="Refuse entry when live spread exceeds this multiple of the "
                    "instrument's normal spread.",
    )
    block_minutes_before_high_impact: int = Field(30, ge=0, le=600)
    block_minutes_after_high_impact: int = Field(30, ge=0, le=600)
    weekend_flat: bool = Field(True, description="Close everything before the weekend gap.")
    friday_close_utc_hour: int = Field(19, ge=0, le=23)
    max_offline_seconds_before_freeze: int = Field(120, ge=5, le=7200)

    @model_validator(mode="after")
    def _coherent(self) -> "RiskConfig":
        if self.min_stop_pips >= self.max_stop_pips:
            raise ValueError("min_stop_pips must be < max_stop_pips")
        if self.daily_loss_limit_pct > self.weekly_loss_limit_pct:
            raise ValueError("daily loss limit cannot exceed the weekly limit")
        if self.weekly_loss_limit_pct > self.monthly_loss_limit_pct:
            raise ValueError("weekly loss limit cannot exceed the monthly limit")
        if self.monthly_loss_limit_pct > self.max_drawdown_halt_pct:
            raise ValueError("monthly loss limit cannot exceed the halt drawdown")
        if self.max_trades_per_day * 5 > self.max_trades_per_week * 2:
            # Sanity, not arithmetic identity: a daily cap wildly above the
            # weekly cap means one of them is a typo.
            raise ValueError("max_trades_per_day is inconsistent with max_trades_per_week")
        if self.ladder_enabled:
            dds = [row["drawdown_pct"] for row in self.ladder]
            mults = [row["risk_multiplier"] for row in self.ladder]
            if dds != sorted(dds):
                raise ValueError("ladder drawdown_pct must be ascending")
            if mults != sorted(mults, reverse=True):
                raise ValueError("ladder risk_multiplier must be non-increasing")
            if any(not (0.0 <= m <= 1.0) for m in mults):
                raise ValueError("ladder risk_multiplier must be within [0, 1]")
            if dds and Decimal(str(dds[-1])) > self.max_drawdown_halt_pct:
                raise ValueError("ladder extends past the halt drawdown")
        if self.partial_take_fraction > 0 and self.partial_take_r <= 0:
            raise ValueError("partial_take_r must be > 0 when a partial fraction is set")
        if (self.trail_activate_r > 0 and self.breakeven_trigger_r > 0
                and self.trail_activate_r < self.breakeven_trigger_r):
            # A trail that fires first would move the stop to a level still
            # below entry, and (before this was fixed) permanently mark the
            # position as "break-even moved", so the break-even rule could
            # never run. Requiring the trail to activate no earlier than
            # break-even removes the ordering hazard entirely.
            raise ValueError(
                "trail_activate_r must be >= breakeven_trigger_r: a trail that "
                "activates first would pre-empt the break-even move")
        if self.giveback_arm_r > 0 and self.giveback_keep_fraction <= 0:
            raise ValueError("giveback_keep_fraction must be > 0 when the ratchet is armed")
        if self.rolling_24h_loss_limit_pct > 0 and \
                self.rolling_24h_loss_limit_pct > self.weekly_loss_limit_pct:
            raise ValueError("rolling 24h loss limit cannot exceed the weekly limit")
        # A single trade must always be able to pass the portfolio limits, or
        # the engine would be unable to ever open anything -- a configuration
        # that looks safe and is simply broken.
        if self.max_currency_exposure_pct < self.risk_per_trade_pct:
            raise ValueError(
                "max_currency_exposure_pct is below risk_per_trade_pct: no single "
                "trade could ever be approved"
            )
        if self.max_correlated_risk_pct < self.risk_per_trade_pct:
            raise ValueError(
                "max_correlated_risk_pct is below risk_per_trade_pct: no single "
                "trade could ever be approved"
            )
        if self.max_total_open_risk_pct < self.risk_per_trade_pct:
            raise ValueError(
                "max_total_open_risk_pct is below risk_per_trade_pct: no single "
                "trade could ever be approved"
            )
        # A total-risk ceiling below risk_per_trade x max_open_positions makes
        # the position cap unreachable: the operator configures four positions
        # and silently gets three. A limit nobody can reach is a limit nobody can
        # reason about, so the two have to be stated coherently.
        budget = self.risk_per_trade_pct * Decimal(self.max_open_positions)
        if self.max_open_positions > 0 and self.max_total_open_risk_pct < budget:
            raise ValueError(
                f"max_total_open_risk_pct ({self.max_total_open_risk_pct}%) is below "
                f"risk_per_trade_pct x max_open_positions ({budget}%), so the position "
                "cap can never be reached. Raise the total ceiling or lower "
                "max_open_positions so the two agree."
            )
        if self.risk_per_trade_pct * Decimal(self.max_open_positions) > self.daily_loss_limit_pct * Decimal("3"):
            raise ValueError(
                "risk_per_trade_pct x max_open_positions is far above the daily loss "
                "limit; the portfolio could breach the day budget on its first round "
                "of stops"
            )
        return self


class StrategyAllocation(StrictModel):
    name: str
    enabled: bool = False
    weight: Decimal = Field(Decimal("1.0"), ge=0, le=Decimal("10"))
    instruments: List[str] = Field(default_factory=list)
    timeframe: str = "H1"
    params: Dict[str, Any] = Field(default_factory=dict)
    # Promotion state -- a strategy may only trade real money from ACCEPTED.
    lifecycle: Literal["reference", "hypothesis", "experimental", "accepted", "suspended"] = "hypothesis"
    accepted_at_ns: Optional[int] = None
    acceptance_run_id: Optional[str] = None

    @model_validator(mode="after")
    def _lifecycle_guard(self) -> "StrategyAllocation":
        if self.lifecycle == "accepted" and not self.acceptance_run_id:
            raise ValueError(
                "a strategy cannot be marked accepted without the id of the "
                "validation run that accepted it"
            )
        return self


class ExecutionConfig(StrictModel):
    venue_mode: ExecutionVenueMode = ExecutionVenueMode.PAPER
    broker: str = Field(
        "paper",
        description="Adapter name (paper, oanda, mt5, ccxt) or a broker PROFILE "
                    "name (amarkets, alpari, generic_mt5, ...). A profile "
                    "carries that venue's symbol spelling, minimum stop "
                    "distance, filling mode and server clock, all of which are "
                    "reconciled against the live terminal at startup.",
    )
    account_currency: str = Field("USD", min_length=3, max_length=5)
    # The account this configuration is FOR. When set, the broker is wrapped in
    # AccountBoundBroker and every call re-checks that the venue still reports
    # this account, type and currency -- so a terminal someone signed into a
    # different account cannot silently receive this configuration's orders.
    # Empty means "unbound", which is acceptable for the paper venue only.
    expected_account_id: str = Field("", max_length=64)
    expected_account_server: str = Field("", max_length=120)
    # The venue's cost schedule as the OPERATOR verified it. The runtime cost
    # model, the break-even stop and the research replay all read these, so the
    # commission the strategy was accepted on is the commission it is charged.
    commission_per_lot_round_turn: Decimal = Field(Decimal("7"), ge=0)
    expected_slippage_pips: Decimal = Field(Decimal("0.15"), ge=0)
    order_type: Literal["market", "limit", "stop"] = "market"
    limit_offset_pips: Decimal = Field(Decimal("0.3"), ge=0)
    max_slippage_pips: Decimal = Field(Decimal("1.5"), ge=0)
    fill_or_kill_after_ms: int = Field(2500, ge=100, le=60000)
    submit_timeout_ms: int = Field(5000, ge=250, le=60000)
    max_submit_retries: int = Field(2, ge=0, le=5)
    retry_backoff_ms: int = Field(750, ge=50, le=30000)
    reconcile_on_start: bool = True
    reconcile_interval_sec: int = Field(30, ge=5, le=3600)
    quarantine_on_unknown_state: bool = Field(
        True,
        description="If an order's fate is unknown after retries, freeze new "
                    "entries until a human or a reconciliation resolves it.",
    )


    @field_validator("broker")
    @classmethod
    def _known_broker(cls, v: str) -> str:
        # NORMALISE, do not just validate. resolve_profile() lowercases and
        # strips before matching, so " Paper " validated fine and was then
        # returned unchanged -- and every downstream safety comparison is an
        # exact `== "paper"`. The result: `broker=" Paper "` with
        # `venue_mode=live` sailed past the guard that exists to stop exactly
        # that combination, and `" oanda "` skipped the live-credential
        # requirement. Guards defeated by whitespace are not guards.
        from ..brokers.profiles import resolve_profile
        from ..brokers.profiles import venues as _venues  # noqa: F401
        v = (v or "").strip().lower()
        if v in ("paper", "oanda", "mt5", "ccxt") or resolve_profile(v):
            return v
        from ..brokers.profiles import list_profiles
        raise ValueError(
            f"unknown broker {v!r}. Adapters: paper, oanda, mt5, ccxt. "
            f"Profiles: {', '.join(p.name for p in list_profiles())}")


class DataConfig(StrictModel):
    primary_feed: str = "broker"
    backup_feeds: List[str] = Field(default_factory=list)
    bar_timeframes: List[str] = Field(default_factory=lambda: ["M5", "M15", "H1", "H4", "D1"])
    history_bars: int = Field(5000, ge=200, le=200000)
    allow_stale_bars: bool = Field(
        False,
        description="Never silently forward-fill. A gap is a fact, not a zero.",
    )
    nan_policy: Literal["reject", "skip_bar"] = "reject"
    store_path: str = "var/market.db"


class NewsConfig(StrictModel):
    enabled: bool = True
    role: Literal["risk_filter", "meta_label", "signal"] = Field(
        "risk_filter",
        description="Start as a filter. Promotion to 'signal' requires a passed "
                    "acceptance run -- see docs/ACCEPTANCE-PROTOCOL.md.",
    )
    llm_enabled: bool = False
    llm_model: str = "claude-opus-4-5"
    llm_training_cutoff: str = Field(
        "2025-01-01",
        description="Any backtest touching news older than this is contaminated "
                    "and is labelled exploratory-only.",
    )
    llm_timeout_ms: int = Field(8000, ge=500, le=60000)
    llm_max_calls_per_hour: int = Field(60, ge=0, le=10000)
    require_post_cutoff_only: bool = True
    lap_test_required: bool = Field(
        True, description="Lookahead-propensity test must pass before promotion."
    )
    high_impact_events: List[str] = Field(
        default_factory=lambda: ["NFP", "CPI", "FOMC", "ECB", "BOE", "BOJ", "GDP", "PMI"]
    )
    calendar_source: str = "local"


class ResearchConfig(StrictModel):
    """Numeric acceptance thresholds, fixed BEFORE any run (brief section D-2)."""

    alpha: float = Field(0.01, gt=0, le=0.05)
    pbo_max: float = Field(0.20, gt=0, le=1.0)
    min_dsr: float = Field(0.95, ge=0.5, le=1.0)
    cpcv_positive_path_fraction: float = Field(0.70, ge=0.5, le=1.0)
    cost_stress_multiple: float = Field(2.0, ge=1.0, le=10.0)
    latency_stress_multiple: float = Field(2.0, ge=1.0, le=10.0)
    embargo_pct: float = Field(0.01, ge=0.0, le=0.2)
    cpcv_groups: int = Field(6, ge=4, le=20)
    cpcv_test_groups: int = Field(2, ge=1, le=5)
    declared_prior: float = Field(
        0.03, gt=0.0, lt=1.0,
        description="Honest prior that a given hypothesis holds. With a 3% "
                    "prior, a p=0.05 'discovery' is more likely false than true.",
    )
    require_random_walk_beat: bool = True
    require_factor_alpha: bool = True
    max_total_trials_declared: int = Field(200, ge=1, le=100000)

    @model_validator(mode="after")
    def _coherent(self) -> "ResearchConfig":
        if self.cpcv_test_groups >= self.cpcv_groups:
            raise ValueError("cpcv_test_groups must be < cpcv_groups")
        return self


class OpsConfig(StrictModel):
    heartbeat_interval_sec: int = Field(5, ge=1, le=120)
    # MUST exceed the decision interval by a margin: the heartbeat's liveness
    # stamp advances only when a cycle completes (ops/killswitch.Heartbeat), so
    # a 45 s dead-man against a 60 s cycle engaged the kill switch on every
    # healthy loop. The cross-check in SentinelConfig enforces the margin, and
    # it is the ONE rule the API, the runner and the watchdog all read.
    deadman_timeout_sec: int = Field(180, ge=5, le=3600)
    deadman_action: Literal["alert", "flatten", "close_only"] = "close_only"
    killswitch_file: str = "var/KILL"
    state_dir: str = "var"
    audit_log: str = "var/audit.jsonl"
    strategy_plugin_dir: Optional[str] = Field(
        None,
        description="Directory of user strategy files loaded at startup. Importing "
                    "a Python file executes it, so this directory is as trusted as "
                    "the process: it must not be writable by anyone who should not "
                    "be able to run code here. A strategy loaded from it still "
                    "cannot declare itself accepted.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    metrics_retention_days: int = Field(365, ge=7, le=3650)
    backup_dir: Optional[str] = None
    ntp_check_interval_sec: int = Field(300, ge=30, le=86400)
    group_ledger_dir: Optional[str] = Field(
        None,
        description="Shared directory of the cross-account risk ledger "
                    "(risk/portfolio.py). Every engine of one owner should point "
                    "here; None means this engine is alone.",
    )


class SecurityConfig(StrictModel):
    dashboard_read_only_default: bool = True
    require_totp_for_writes: bool = True
    session_ttl_minutes: int = Field(30, ge=5, le=480)
    max_login_attempts: int = Field(5, ge=1, le=20)
    lockout_minutes: int = Field(15, ge=1, le=1440)
    bind_host: str = Field(
        "127.0.0.1",
        description="Loopback by default. Exposing the dashboard to the public "
                    "internet is equivalent to publishing the account.",
    )
    bind_port: int = Field(8088, ge=1, le=65535)
    allowed_origins: List[str] = Field(default_factory=lambda: ["http://127.0.0.1:5173"])
    api_rate_limit_per_minute: int = Field(120, ge=10, le=10000)
    write_rate_limit_per_minute: int = Field(10, ge=1, le=600)

    @field_validator("bind_host")
    @classmethod
    def _warn_public(cls, v: str) -> str:
        if v in ("0.0.0.0", "::"):  # noqa: S104 - deliberate check, not a bind
            # Allowed, but the API layer logs a prominent warning and the
            # dashboard shows a permanent banner.
            return v
        return v


class AgentConfig(StrictModel):
    mode: AgentMode = AgentMode.ADVISORY
    decision_interval_sec: int = Field(60, ge=5, le=3600)
    session_windows_utc: List[List[int]] = Field(
        default_factory=lambda: [[7, 16]],
        description="Hours (UTC) when entries are permitted. The Asian session "
                    "carries a wider spread; see brief section B-3.",
    )
    trade_days: List[int] = Field(
        default_factory=lambda: [0, 1, 2, 3, 4], description="0=Mon .. 6=Sun"
    )
    semi_auto_envelope: Dict[str, Any] = Field(
        default_factory=lambda: {
            "instruments": ["EUR_USD", "GBP_USD", "USD_JPY"],
            "max_lots": 0.20,
            "max_risk_pct": 0.5,
        }
    )
    learning_enabled: bool = True
    proposal_min_sample: int = Field(
        40, ge=10, le=10000,
        description="Minimum closed trades before the learning loop is allowed "
                    "to propose a parameter change.",
    )
    proposal_requires_human: bool = Field(
        True,
        description="Self-improvement proposals never auto-apply to live "
                    "trading. They are queued for approval and must pass a "
                    "validation run first.",
    )
    regime_detection_enabled: bool = True
    explain_every_decision: bool = True
    # The performance guard: after this many closed trades of one strategy, if
    # the upper 95% confidence bound of its mean R is still below zero, the
    # strategy is SUSPENDED for new entries and the operator is told. A
    # strategy that is demonstrably losing does not get to keep proving it with
    # the account's money; nothing here re-enables it -- a human does.
    performance_guard_enabled: bool = True
    performance_guard_min_trades: int = Field(50, ge=30, le=1000)
    # A fitted meta-label filter (research/metalabel.MetaGate, saved by
    # scripts/run_acceptance.py --meta-label --save-meta). Consulted before
    # the risk engine on every primary signal. Its file hash is part of the
    # acceptance fingerprint: a different model is a different system.
    meta_model_path: Optional[str] = None

    @field_validator("session_windows_utc")
    @classmethod
    def _valid_windows(cls, v):
        for w in v:
            if len(w) != 2 or not (0 <= w[0] <= 23) or not (0 <= w[1] <= 24) or w[0] >= w[1]:
                raise ValueError(f"invalid session window {w}: expected [start, end] in 0..24")
        return v

    @field_validator("trade_days")
    @classmethod
    def _valid_days(cls, v):
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("trade_days entries must be 0..6")
        return sorted(set(v))


#: The dead-man timeout must exceed the decision interval by at least this
#: much: one venue round trip, one reconcile, and clock jitter. One constant,
#: read by the config validator, the account runner and the API.
DEADMAN_MARGIN_SEC = 60


def deadman_timeout_ok(deadman_timeout_sec: int, decision_interval_sec: int) -> bool:
    return int(deadman_timeout_sec) > int(decision_interval_sec) + DEADMAN_MARGIN_SEC


class SentinelConfig(StrictModel):
    """Root configuration object."""

    version: int = 1
    updated_at_ns: int = Field(default_factory=wall_ns)
    updated_by: str = "system"
    profile: str = Field("default", min_length=1, max_length=64)

    agent: AgentConfig = Field(default_factory=AgentConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    ops: OpsConfig = Field(default_factory=OpsConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    strategies: List[StrategyAllocation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cross_checks(self) -> "SentinelConfig":
        # Live money requires: autonomous/semi mode chosen deliberately, every
        # enabled strategy accepted, and broker-side stops on.
        if self.execution.venue_mode == ExecutionVenueMode.LIVE:
            if not self.risk.require_broker_side_stop:
                raise ValueError("live trading requires require_broker_side_stop=True")
            bad = [s.name for s in self.strategies if s.enabled and s.lifecycle != "accepted"]
            if bad:
                raise ValueError(
                    "live trading refused: strategies not in 'accepted' lifecycle: "
                    + ", ".join(bad)
                )
            if self.execution.broker == "paper":
                raise ValueError("venue_mode=live is incompatible with broker=paper")
        names = [s.name for s in self.strategies]
        if len(names) != len(set(names)):
            raise ValueError("duplicate strategy names")
        if not deadman_timeout_ok(self.ops.deadman_timeout_sec,
                                  self.agent.decision_interval_sec):
            raise ValueError(
                f"ops.deadman_timeout_sec ({self.ops.deadman_timeout_sec}) must exceed "
                f"agent.decision_interval_sec ({self.agent.decision_interval_sec}) by more "
                f"than {DEADMAN_MARGIN_SEC}s: the heartbeat advances once per cycle, so a "
                "tighter dead-man trips on a healthy loop")
        # An external venue without a declared account is a configuration that
        # trades whichever account the terminal happens to be signed into.
        if (self.execution.broker != "paper"
                and self.execution.venue_mode is ExecutionVenueMode.LIVE
                and not self.execution.expected_account_id):
            raise ValueError("live trading requires execution.expected_account_id")
        return self

    # -- persistence -------------------------------------------------------- #

    def bump(self, by: str) -> "SentinelConfig":
        data = self.model_dump(mode="json")
        data["version"] = self.version + 1
        data["updated_at_ns"] = wall_ns()
        data["updated_by"] = by
        return SentinelConfig.model_validate(data)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        os.replace(tmp, p)  # atomic on POSIX -- never a half-written config
        return p

    @classmethod
    def load(cls, path: str | Path) -> "SentinelConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.model_validate_json(p.read_text(encoding="utf-8"))


@dataclass
class ConfigChange:
    path: str
    old: Any
    new: Any


def diff_configs(a: SentinelConfig, b: SentinelConfig) -> List[ConfigChange]:
    """Flat, human-readable diff for the audit trail and the dashboard."""

    def flatten(obj: Any, prefix: str = "") -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                out.update(flatten(v, f"{prefix}[{i}]"))
        else:
            out[prefix] = obj
        return out

    fa = flatten(a.model_dump(mode="json"))
    fb = flatten(b.model_dump(mode="json"))
    skip = {"version", "updated_at_ns", "updated_by"}
    changes: List[ConfigChange] = []
    for key in sorted(set(fa) | set(fb)):
        if key in skip:
            continue
        if fa.get(key) != fb.get(key):
            changes.append(ConfigChange(key, fa.get(key), fb.get(key)))
    return changes
