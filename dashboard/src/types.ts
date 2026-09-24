export type Mode = "observe" | "advisory" | "semi_auto" | "autonomous";
export type VenueMode = "paper" | "demo" | "live";

export interface Veto { rule: string; message: string; severity: string; observed?: string | null; limit?: string | null; }

export interface Decision {
  ts_ns: number; strategy: string; instrument: string;
  action: "proposed" | "executed" | "vetoed" | "skipped" | "queued";
  side?: string | null; lots?: string | null; entry?: string | null;
  stop?: string | null; target?: string | null;
  risk_amount?: string | null; risk_pct?: string | null;
  signal_strength: number; regime: string;
  vetoes: Veto[]; warnings: Veto[]; lessons: string[];
  diagnostics: Record<string, unknown>;
  rationale: string; explanation: string; client_order_id?: string | null;
}

export interface Position {
  instrument: string; side: string; lots: string; entry_price: string;
  stop_loss: string | null; take_profit: string | null; broker_stop_confirmed: boolean;
  strategy: string; opened_ns: number; initial_risk: string; financing_paid: string;
  current_price?: string | null; unrealised?: string; r_multiple?: string | null;
  spread_pips?: string;
}

export interface Trade {
  trade_id: string; strategy: string; instrument: string; side: string; lots: string;
  entry_price: string; exit_price: string; opened_ns: number; closed_ns: number;
  pnl: string; pnl_pips: string; r_multiple: string; exit_reason: string;
  commission: string; financing: string; duration_sec: number; regime: string;
  max_favourable_r: string; max_adverse_r: string;
}

export interface EquityPoint {
  ts_ns: number; equity: number; balance: number; drawdown_pct: number; open_positions: number;
}

export interface Status {
  ts_ns: number; uptime_sec: number; mode: Mode; venue_mode: VenueMode;
  halted: boolean; halt_reason: string;
  kill_switch: { engaged: boolean; reason: string; engaged_by?: string };
  cycles: number;
  account: Record<string, string | number | null>;
  regime: null | {
    regime: string; confidence: number; vol_percentile: number;
    trend_strength: number; correlation_dispersion: number;
    risk_multiplier: number; explanation: string; inputs: Record<string, number>;
  };
  health: Record<string, unknown>;
  broker: {
    name: string; supports_client_order_id: boolean; supports_server_side_stop: boolean;
    supports_transaction_stream: boolean; degradations: string[];
  };
  unresolved_orders: number; quarantined: string[];
  advisory_pending: number; proposals_pending: number;
  config_version: number; errors: string[];
  security_warning?: string;
  api_version?: string;
}

export interface RiskView {
  drawdown_pct: number; equity_peak: string; risk_multiplier: string;
  day_pnl: string; day_pnl_pct: number;
  trades_today: number; trades_this_week: number; trades_this_year: number;
  gross_leverage: number; pending_risk: string;
  currency_exposure: { currency: string; net_risk: string; gross_risk: string;
    net_risk_pct: number; contributors: string[] }[];
  alarms: Veto[];
  limits: Record<string, string | number>;
  ladder: { drawdown_pct: number; risk_multiplier: number }[];
}

export interface Performance {
  net_profit: number; net_return_pct: number; gross_profit: number; total_cost: number;
  cost_drag_pct: number; cagr_pct: number; sharpe: number; sortino: number; calmar: number;
  max_drawdown_pct: number; max_drawdown_amount: number; max_drawdown_duration_bars: number;
  time_to_recovery_bars: number | null; ulcer_index: number;
  n_trades: number; effective_n: number; win_rate: number; profit_factor: number;
  expectancy_r: number; avg_win_r: number; avg_loss_r: number; payoff_ratio: number;
  max_consecutive_losses: number; trades_without_defined_risk: number;
  return_skew: number; return_kurtosis: number; tail_ratio: number;
  var_95_r: number; cvar_95_r: number;
  avg_hold_hours: number; exposure_pct: number; trades_per_year: number;
  avg_mae_r: number; avg_mfe_r: number; edge_efficiency: number;
  exit_breakdown: Record<string, number>;
  by_instrument: Record<string, Record<string, number>>;
  notes: string[];
}

export interface Lesson {
  id: number | null; scope: string; statement: string; evidence: Record<string, unknown>;
  sample_size: number; effect_r: number; p_value: number; confidence: number;
  strategy: string | null; instrument: string | null; regime: string | null;
  caution: number; status: string; created_ns: number;
  // Added when lessons gained an expiry. `caution` is what was learned;
  // `effective_caution` is what the agent actually applies today, after decay
  // for the time since fresh evidence last confirmed the lesson. Showing the
  // stored number while the engine applies the decayed one is a dashboard that
  // disagrees with the system it is describing.
  effective_caution?: number; age_days?: number;
  last_confirmed_ns?: number; review_count?: number; contradiction_count?: number;
  half_life_days?: number;
}

export interface Proposal {
  id: string; created_ns: number; path: string; current_value: unknown;
  proposed_value: unknown; rationale: string; evidence: Record<string, unknown>;
  sample_size: number; expected_effect_r: number; effect_ci_low: number;
  effect_ci_high: number; p_value: number; status: string;
  validation_run_id: string | null; reviewed_by: string | null; strategy: string | null;
}

export interface Gate {
  id: string; name: string; passed: boolean; observed: string;
  threshold: string; detail: string; blocking: boolean;
}

export interface AuditRecord {
  seq: number; ts_ns: number; run_id: string; event: string; actor: string;
  payload: Record<string, unknown>; prev_hash: string; hash: string;
}

export interface StrategyInfo {
  name: string; version: string; description: string; timeframe: string;
  horizon_bars: number; lifecycle: string; hypothesis: string;
  failure_conditions: string[]; required_history: number;
  default_params: Record<string, unknown>;
}

export interface ExecutionQuality {
  n: number; fills?: number; rejects?: number; reject_rate?: number;
  median_latency_ms?: number; p95_latency_ms?: number;
  mean_slippage_pips?: number; median_slippage_pips?: number;
  adverse_slippage_share?: number; last_look_asymmetry?: number; last_look_note?: string;
}

export interface Snapshot {
  status: Status; positions: Position[]; trades: Trade[]; equity: EquityPoint[];
  decisions: Decision[]; risk: RiskView; performance: Performance;
  lessons: Lesson[]; proposals: Proposal[]; audit: AuditRecord[];
  strategies: StrategyInfo[]; allocations: Record<string, any>;
  execution: ExecutionQuality; gates: Gate[]; config: any;
  advice: Decision[]; health: Record<string, any>;
  costTable: { target: number; raw: number; standard: number }[];
  capitalTable: { stop: number; minEquity: number }[];
  cpcvSharpes: number[];
  monthly: { year: number; months: (number | null)[] }[];
}

/* ---------------------------------------------------------------------- *
 * Venue configuration, licensing and accounts.
 * These are fetched on demand rather than in the main snapshot: they change
 * rarely, they are owner-only in places, and polling them every 15 seconds
 * would put the machine fingerprint and the account list on the wire
 * constantly for a page nobody is looking at.
 * ---------------------------------------------------------------------- */

export interface ProbeCheck {
  id: string; title: string; passed: boolean | null; detail: string;
  severity: "block" | "warn" | "info";
}

/** What a connection STORES and what the dashboard reads back.
 *
 *  Deliberately not the raw probe result. The raw report carries the venue's
 *  real account number, balance and equity; this summary keeps the verdict,
 *  the checks and a MASKED account id, because /api/brokers is readable by any
 *  authenticated session and a balance has no business being on that wire. */
export interface ProbeReport {
  connection_id: string; profile: string; adapter: string; ok: boolean;
  started_ns: number; finished_ns: number; duration_sec: number;
  checks: ProbeCheck[];
  symbols_total: number; symbol_examples: Record<string, string>;
  mismatches: string[]; degradations: string[]; error: string;
  blocking_failures: string[];
  /** Masked — "••••••217". Enough to tell two accounts apart. */
  account_id: string;
  account_currency: string;
  account_type: string;
}

export interface Connection {
  id: string; display_name: string; profile: string; adapter: string;
  origin: string; server: string; login: string; login_full_length: number;
  terminal_path: string; account_currency: string; exchange_id: string;
  declared_account_type: "demo" | "live";
  enabled: boolean; created_ns: number; updated_ns: number;
  has_credential: boolean; notes: string;
  last_probe: ProbeReport | null;
}

export interface ProfileCard {
  name: string; display_name: string; adapter: string; regulator: string;
  max_leverage: number; min_stop_level_points: number;
  commission_per_lot_round_turn: string; default_spread_pips: string;
  supports_server_side_stop: boolean; supports_hedging: boolean;
  segregated_client_funds: boolean | null;
  negative_balance_protection: boolean | null;
  symbol_suffix: string; notes: string; verify_before_live: string[];
}

export interface BrokerOverview {
  active_profile: string; active_adapter: string; venue_mode: string;
  degradations: string[]; open_positions: number;
  connections: Connection[]; damaged: string[]; profiles: ProfileCard[];
  credential_storage: {
    level: "good" | "fair" | "weak" | "unavailable";
    note: string; key_source: string | null; key_path: string | null;
  };
  restart_required: boolean;
}

export interface Discovered {
  source: string; profile: string; display_name: string; server: string;
  login: string; account_currency: string; terminal_path?: string;
  company?: string; declared_account_type: "demo" | "live";
  leverage?: number; note: string; needs_credential: boolean;
}

export interface LicenceView {
  enforced?: boolean; valid: boolean; reason: string; warnings: string[];
  in_grace: boolean; days_remaining: number | null; unlicensed_mode: boolean;
  tier: string | null; issued_to: string | null; licence_id: string | null;
  expires_at: string | null; issued_at: string | null;
  term_months: number | null; term_label: string | null;
  term_index: number | null; subscription_id: string | null;
  machine_bound: boolean | null;
  effective_capabilities: Record<string, any> | null;
  stage: string; headline: string; advice: string;
  live_trading_allowed?: boolean; live_trading_reason?: string;
  clock: null | {
    ok: boolean; rolled_back: boolean; rollback_seconds: number;
    previously_expired: boolean; state_missing: boolean;
    state_unreadable: boolean; message: string; checks: number;
    first_seen_ns: number;
  };
  integrity: null | Record<string, any>;
  note?: string;
}

export interface AccountRow {
  username: string; role: "owner" | "operator" | "viewer"; role_label: string;
  disabled: boolean; created_ns: number; can_write: boolean;
  can_change_risk: boolean; active_sessions: number; is_last_owner: boolean;
}

export interface AccountsView {
  users: AccountRow[];
  roles: { id: string; label: string; description: string }[];
  min_password_length: number;
}
