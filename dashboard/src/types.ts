export type Mode = "observe" | "advisory" | "semi_auto" | "autonomous";
export type VenueMode = "paper" | "demo" | "live";

export interface Veto { rule: string; message: string; severity: string; observed?: string | null; limit?: string | null; }

export interface Decision {
  ts_ns: number; strategy: string; instrument: string;
  action: "proposed" | "executed" | "vetoed" | "skipped" | "queued" | "preview";
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
  /** strategy -> why the performance guard suspended it (1.5.0). */
  guard_suspended?: Record<string, string>;
  cooldowns?: Record<string, string>;
  day_pnl?: string | null;
  terminal?: TerminalView | null;
  /** Whether NEW live risk may be opened right now, as far as the licence goes. */
  entries_permitted?: { allowed: boolean; reason: string };
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

/* ---------------------------------------------------------------------- *
 * AI assistants and news (1.5.0). Fetched on demand, never in the snapshot.
 * ---------------------------------------------------------------------- */

export interface AIProviderRow {
  id: string; label: string; label_fa: string; enabled: boolean;
  model: string; default_model: string; notes_fa: string; console_url: string;
  key_prefix_hint: string; key_stored: boolean; key_last4: string | null;
  base_url?: string;
}

export interface AIOverview {
  enabled: boolean;
  providers?: AIProviderRow[];
  primary?: string; fallbacks?: string[];
  purposes?: Record<string, boolean>;
  max_calls_per_hour?: number; max_calls_per_day?: number;
  available?: Record<string, { ok: boolean; reason: string }>;
  key_storage?: string;
  usage?: { last_24h: any[]; recent: any[] } | null;
  calls_last_hour?: number;
}

export interface AIReview {
  trade_id: string; ts_ns: number; strategy: string; instrument: string;
  r_multiple: number; provider: string; model: string;
  payload: {
    summary_fa: string; what_went_right_fa: string | null;
    what_went_wrong_fa: string | null; category: string; avoidable: boolean;
    suggestion_fa: string; confidence: number;
  };
}

export interface Headline {
  article_id: string; feed: string; source: string; title: string; summary: string;
  link: string; published_ns: number | null; currencies: string[];
  extraction?: {
    valid: boolean; event_type: string; direction_claim: string; is_correction: boolean;
    contradicts_prior: boolean; confidence: number; errors: string[];
  };
}

export interface AIInsights {
  reviews: AIReview[];
  themes: null | { reviewed: number; avoidable: number; note_fa: string;
                   categories: { category: string; count: number }[] };
  brief: null | { ts_ns: number; provider: string; model: string;
                  payload: { headline_fa: string; market_fa: string; risks_fa: string[];
                             agent_state_fa: string; watch_fa: string[] } };
  headlines: Headline[];
  desk: null | Record<string, any>;
  background_errors: string[];
}

/* Independent reference price (TradingView). */
export interface ReferenceCheck {
  instrument: string; symbol: string;
  status: "ok" | "shrink" | "block" | "stale" | "delayed" | "closed" | "unavailable"
    | "unmapped" | "no_broker_quote";
  reason: string; broker_mid: number | null; reference_mid: number | null;
  divergence_bp: number | null; divergence_pips: number | null;
  shrink_at_bp: number | null; block_at_bp: number | null;
  reference_age_sec: number | null; size_multiplier: number; blocked: boolean;
  median_gap_bp: number | null; recent_gap_bp: number[];
  checks: number; shrinks: number; blocks: number; ts_ns: number;
}

export interface ReferenceQuote {
  symbol: string; mid: number | null; bid: number | null; ask: number | null;
  last: number | null; change_pct: number | null; description: string;
  update_mode: string; session: string; delayed: boolean; error: string;
  price_ns: number; received_ns: number;
}

export type TARating = { all: number | null; ma: number | null; other: number | null;
                         label: string };

export interface ReferenceConfig {
  enabled: boolean; provider: string; exchange: string; symbol_map: Record<string, string>;
  shrink_bp: number; block_bp: number; spread_multiple_shrink: number;
  spread_multiple_block: number; shrink_multiplier: number; max_age_sec: number;
  ta_ratings: boolean; ta_every_min: number;
}

export interface ReferenceView {
  available: boolean; enabled: boolean; reason?: string; provider?: string;
  config?: ReferenceConfig;
  stream?: { running: boolean; connected: boolean; connected_since_ns: number;
             last_message_ns: number; connects: number; reconnects: number;
             dropped_packets: number; last_error: string; last_error_ns: number;
             next_attempt_ns: number; server_release: string; symbols: string[];
             symbol_errors: Record<string, string> };
  mapping?: Record<string, string>;
  quotes?: Record<string, ReferenceQuote | null>;
  checks?: ReferenceCheck[];
  ta?: Record<string, Record<string, TARating>>;
  ta_last_ns?: number; ta_error?: string; last_tick_ns?: number;
}

/* The System One model (Jev): authority, versions, calibration. */
export interface CalibrationSummary {
  n: number; positives: number; brier: number | null; base_rate_brier: number | null;
  skill: number | null; auc: number | null;
  reliability: { lo: number; hi: number; n: number; mean_p: number | null;
                 observed: number | null }[];
  decision: { threshold: number; n: number; true_positive: number; false_positive: number;
              false_negative: number; true_negative: number; accuracy: number | null };
}

export interface JevAnswer {
  article_id: string; ts_ns: number; version: string; mode: string; headline: string;
  currencies: string; event_type: string; direction: string; confidence: number;
  confidence_known: boolean; p_correction: number | null; p_contradiction: number | null;
  label_correction: boolean | null; label_contradiction: boolean | null;
  label_direction: string | null; text_opinion: Record<string, any> | null;
}

export interface JevReport {
  mode: "shadow" | "shrink_only" | "active"; modes: string[];
  known_version: string; known_since_ns: number; pending_version: string;
  pending_since_ns: number; floating_alias: boolean; answers: number; labelled: number;
  correction: CalibrationSummary; contradiction: CalibrationSummary;
  direction: { n: number; accuracy: number | null; mean_confidence: number | null };
  agreement_with_text_model: { n: number; correction: number | null;
                               contradiction: number | null; direction: number | null };
  gate: { passed: boolean; missing: string[]; min_labels: number;
          contradiction_accuracy: number; correction_accuracy: number };
  versions: { version: string; n: number; first_ns: number; last_ns: number;
              labelled: number }[];
  breaker: null | { open: boolean; seconds_left: number; failures: number;
                    last_status: number | null };
  recent: JevAnswer[];
}

/* The brain (sentinel.brain): learning from every signal, shrink-only. */
export interface RSummary {
  n: number; mean_r: number | null; ci_low: number | null; ci_high: number | null;
  sum_r: number; win_rate: number | null;
}

export interface BrainRule extends RSummary {
  rule: string; verdict: "helped" | "hurt" | "unclear" | "insufficient";
}

export interface BrainLayerScore extends RSummary {
  layer: string; saved_r: number; verdict: "helped" | "hurt" | "unclear" | "insufficient";
}

export interface BrainStrategy {
  strategy: string; multiplier: number; reasons: string[]; layers: Record<string, number>;
  live: RSummary; baseline: { mean_r: number; sd_r: number; source: string };
  cusum: null | { stat: number; alarm: boolean; recovering: boolean;
                  first_alarm_index: number | null; path: number[] };
  drift_threshold: number;
}

export interface BrainModel {
  id: string; ts_ns: number; path: string; sha256: string; status: string;
  decided_by: string | null; decided_ns: number | null;
  report: { holdout?: { n: number; positives: number; brier: number | null;
                        skill: number | null; auc: number | null };
            n_train?: number; n_holdout?: number; n_purged?: number; eligible?: boolean;
            reason?: string };
}

export interface BrainLabRun {
  id: number; ts_ns: number; by?: string; seconds?: number; errors?: string[];
  strategies: { strategy: string; status: string; n?: number; mean_r?: number | null;
                boot_low?: number | null; boot_high?: number | null;
                stressed_mean_r?: number | null; note?: string; error?: string }[];
  proposals: { id: string; path: string; status: string;
               mean_r_delta_by_strategy?: number[] }[];
  meta: null | { trained: boolean; eligible?: boolean; reason?: string; model_id?: string;
                 holdout?: { auc: number | null; n: number } };
}

export interface BrainConfig {
  enabled: boolean; shadow_book: boolean; shadow_default_horizon_bars: number;
  loss_streak_limit: number; loss_streak_cooldown_hours: number;
  strategy_loss_streak_limit: number; strategy_cooldown_hours: number;
  drift_enabled: boolean; drift_min_trades: number; drift_k: number; drift_h: number;
  drift_multiplier: number; drift_expected_r: number;
  equity_filter_enabled: boolean; equity_filter_window: number;
  equity_filter_multiplier: number;
  similarity_enabled: boolean; similarity_k: number; similarity_min_samples: number;
  similarity_multiplier: number;
  allocation_enabled: boolean; allocation_prior_mean_r: number; allocation_prior_sd: number;
  allocation_floor: number;
  stress_enabled: boolean; stress_loss_limit_pct: number;
  stress_scenarios: Record<string, number>;
  lab_enabled: boolean; lab_hour_utc: number; lab_max_minutes: number; lab_max_bars: number;
  meta_auto_train: boolean; meta_min_auc: number;
  weekly_report_dow: number; weekly_report_hour_utc: number;
}

export interface BrainView {
  available: boolean; enabled: boolean; regime?: string; config?: BrainConfig;
  cooldowns?: Record<string, string>;
  streaks?: { account: number; strategies: Record<string, number> };
  shadow_counts?: Record<string, number>;
  scorecard?: { taken: RSummary; rules: BrainRule[]; layers: BrainLayerScore[];
                strategies: Record<string, { taken: RSummary; not_taken: RSummary }> };
  strategies?: BrainStrategy[];
  models?: BrainModel[];
  meta_live?: { n: number; brier?: number | null; skill?: number | null; auc?: number | null };
  lab_running?: boolean; lab_runs?: BrainLabRun[];
  reports?: ({ id: number; ts_ns: number; week_ending: string; trades: RSummary;
               by_strategy: Record<string, RSummary> } & Record<string, any>)[];
  baselines?: Record<string, { mean_r: number; sd_r: number; n: number }>;
  last_error?: string;
}

/* Telegram and Bale. */
export interface NotifyChannel {
  label_fa: string; enabled: boolean; chat_id: string; categories: string[];
  commands: boolean; sent: number; failed: number; last_error: string; last_ok_ns: number;
  token_stored: boolean;
}

export interface NotifyView {
  available: boolean; channels?: Record<"telegram" | "bale", NotifyChannel>;
  categories?: string[]; daily_hour_utc?: number; token_storage?: string; queued?: number;
}

/* Macro context: the dollar index and CFTC positioning (1.8.0). */
export interface CotRow {
  currency: string; code: string; weeks: number; report_date?: string; available_ns?: number;
  net?: number; net_pct_oi?: number; index?: number; change?: number;
  crowded_long: boolean; crowded_short: boolean;
}

export interface MacroConfig {
  enabled: boolean; dxy_enabled: boolean; dxy_timeframe: string; dxy_history_bars: number;
  dxy_headwind_enabled: boolean; dxy_headwind_score: number; dxy_headwind_multiplier: number;
  cot_enabled: boolean; cot_refresh_hours: number; cot_lookback_weeks: number;
  cot_min_weeks: number; cot_crowding_enabled: boolean; cot_extreme: number;
  cot_crowding_multiplier: number;
}

export interface MacroView {
  available: boolean; enabled?: boolean; config?: MacroConfig;
  dxy?: { available: boolean; error: string; components_on_server: string[];
          complete?: boolean; used?: string[]; missing?: string[]; notes?: string[];
          timeframe?: string; last?: number; last_bar?: string; points?: [number, number][];
          state?: { dxy_mom?: number; dxy_z?: number; dxy_vol_bp?: number } };
  cot?: { rows: CotRow[]; stored: number; latest: string | null; error: string;
          last_fetch_ns: number; running: boolean };
}

export interface TerminalView {
  available: boolean; applicable?: boolean; enabled?: boolean; state?: string;
  state_fa?: string; detail?: string; down_since_ns?: number; failures?: number;
  attempts?: number; next_attempt_ns?: number; last_action?: string; last_action_ns?: number;
  recoveries?: number; algo_trading?: boolean | null; ping_ms?: number | null;
  last_check_ns?: number; can_sign_in?: boolean;
  config?: { enabled: boolean; grace_checks: number; backoff_initial_sec: number;
             backoff_max_sec: number; kill_hung_after_failures: number;
             restore_account: boolean };
}
