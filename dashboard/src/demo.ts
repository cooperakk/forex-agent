/**
 * Demo dataset.
 *
 * Deterministic, generated from a seeded PRNG so the page looks the same on
 * every load. The numbers are deliberately *modest*: a demo account, a few
 * months, a Sharpe under 1, a real drawdown, and an acceptance verdict that
 * FAILS. A demo that shows a rising line and a 90% win rate would misrepresent
 * what this system is for.
 */
import type {
  AuditRecord, Decision, EquityPoint, Gate, Lesson, Performance, Position,
  Proposal, RiskView, Snapshot, Status, StrategyInfo, Trade,
} from "./types";

function mulberry32(a: number) {
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rnd = mulberry32(20260914);
const gauss = () => {
  let u = 0, v = 0;
  while (u === 0) u = rnd();
  while (v === 0) v = rnd();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
};
const pick = <T,>(xs: T[]) => xs[Math.floor(rnd() * xs.length)];

const NOW = Date.UTC(2026, 8, 14, 9, 0, 0);
const MS = 1;
const ns = (ms: number) => ms * 1_000_000;

const SYMBOLS = ["EUR_USD", "GBP_USD", "AUD_USD", "USD_JPY", "USD_CHF", "NZD_USD"];
const STRATS = ["donchian_trend", "vol_reversion", "xs_momentum"];
const REGIMES = ["trending", "quiet_range", "volatile_range", "stress"];
/** Market state in plain words, for the sample explanations below. */
const REGIME_FA: Record<string, string> = {
  trending: "بازار جهت‌دار", quiet_range: "بازار آرام",
  volatile_range: "بازار پرنوسان", stress: "بازار بحرانی",
};

/* ---------------- equity curve ---------------- */

const BARS = 460;
const START_EQ = 10000;
const equity: EquityPoint[] = [];
{
  let eq = START_EQ;
  let peak = START_EQ;
  let drift = 0.00028;
  for (let i = 0; i < BARS; i++) {
    // A regime-switching drift with a deliberate losing stretch in the middle:
    // a demo without a drawdown teaches the wrong thing.
    if (i === 150) drift = -0.00055;
    if (i === 235) drift = 0.00042;
    if (i === 360) drift = -0.0002;
    if (i === 400) drift = 0.0005;
    const shock = gauss() * 0.0042 + (rnd() < 0.02 ? gauss() * 0.011 : 0);
    eq = eq * (1 + drift + shock);
    peak = Math.max(peak, eq);
    equity.push({
      ts_ns: ns(NOW - (BARS - i) * 4 * 3600 * 1000 * MS),
      equity: +eq.toFixed(2),
      balance: +(eq * (1 - 0.0015 * Math.abs(Math.sin(i / 9)))).toFixed(2),
      drawdown_pct: +(((peak - eq) / peak) * 100).toFixed(3),
      open_positions: Math.max(0, Math.round(1.4 + gauss() * 0.9)),
    });
  }
}
const finalEq = equity[equity.length - 1].equity;
const maxDD = Math.max(...equity.map((p) => p.drawdown_pct));

/* ---------------- trades ---------------- */

const EXITS = ["stop_loss", "take_profit", "partial_take", "trail_stop", "time_stop", "weekend_flat"];
const trades: Trade[] = [];
for (let i = 0; i < 134; i++) {
  const sym = pick(SYMBOLS);
  const strat = rnd() < 0.55 ? STRATS[0] : pick(STRATS);
  const side = rnd() < 0.52 ? "BUY" : "SELL";
  const win = rnd() < 0.43;
  const r = win ? 0.4 + Math.abs(gauss()) * 1.25 : -(0.35 + Math.abs(gauss()) * 0.55);
  const rr = Math.max(-2.4, Math.min(5.2, r));
  const risk = 42 + rnd() * 18;
  const opened = NOW - (134 - i) * 11 * 3600 * 1000 - rnd() * 6 * 3600 * 1000;
  const held = (4 + rnd() * 70) * 3600 * 1000;
  const jpy = sym.includes("JPY");
  const entry = jpy ? 149 + rnd() * 3 : sym.startsWith("AUD") || sym.startsWith("NZD") ? 0.6 + rnd() * 0.1 : 1.05 + rnd() * 0.25;
  const pipSize = jpy ? 0.01 : 0.0001;
  const stopPips = 26 + rnd() * 40;
  const pips = rr * stopPips;
  const exit = entry + pips * pipSize * (side === "BUY" ? 1 : -1);
  trades.push({
    trade_id: `T${String(i + 1).padStart(5, "0")}`,
    strategy: strat, instrument: sym, side, lots: (0.04 + rnd() * 0.14).toFixed(2),
    entry_price: entry.toFixed(jpy ? 3 : 5), exit_price: exit.toFixed(jpy ? 3 : 5),
    opened_ns: ns(opened), closed_ns: ns(opened + held),
    pnl: (rr * risk).toFixed(2), pnl_pips: pips.toFixed(1), r_multiple: rr.toFixed(3),
    exit_reason: rr <= -0.9 ? "stop_loss" : rr >= 1.8 ? "take_profit" : pick(EXITS),
    commission: (0.55 + rnd() * 0.5).toFixed(2),
    financing: (-(rnd() * 0.9)).toFixed(2),
    duration_sec: held / 1000, regime: pick(REGIMES),
    max_favourable_r: Math.max(rr, rnd() * 1.9).toFixed(2),
    max_adverse_r: Math.min(rr, -(rnd() * 1.15)).toFixed(2),
  });
}
trades.sort((a, b) => a.closed_ns - b.closed_ns);

/* ---------------- positions ---------------- */

const positions: Position[] = [
  {
    instrument: "EUR_USD", side: "BUY", lots: "0.12", entry_price: "1.08412",
    stop_loss: "1.07960", take_profit: "1.09520", broker_stop_confirmed: true,
    strategy: "donchian_trend", opened_ns: ns(NOW - 19 * 3600 * 1000),
    initial_risk: "54.24", financing_paid: "-0.42", current_price: "1.08688",
    unrealised: "33.12", r_multiple: "0.611", spread_pips: "0.6",
  },
  {
    instrument: "USD_JPY", side: "SELL", lots: "0.08", entry_price: "150.842",
    stop_loss: "151.560", take_profit: "149.100", broker_stop_confirmed: true,
    strategy: "xs_momentum", opened_ns: ns(NOW - 41 * 3600 * 1000),
    initial_risk: "38.10", financing_paid: "-1.18", current_price: "151.104",
    unrealised: "-13.90", r_multiple: "-0.365", spread_pips: "0.9",
  },
  {
    instrument: "AUD_USD", side: "BUY", lots: "0.10", entry_price: "0.66120",
    stop_loss: "0.65780", take_profit: "0.66980", broker_stop_confirmed: true,
    strategy: "donchian_trend", opened_ns: ns(NOW - 6 * 3600 * 1000),
    initial_risk: "34.00", financing_paid: "-0.08", current_price: "0.66245",
    unrealised: "12.50", r_multiple: "0.368", spread_pips: "1.1",
  },
];

/* ---------------- decisions ---------------- */

const VETOES: [string, string, string, string][] = [
  ["currency_exposure", "سه معامله باز، همگی در عمل یک شرط علیه دلار هستند: روی‌هم ۱٫۴۲٪ حساب روی دلار در خطر است، و سقف ۱٫۲۵٪ است", "1.42%", "1.25%"],
  ["spread", "اختلاف قیمت خرید و فروش ۲٫۹ پیپ است، یعنی ۴٫۸ برابر حالت عادی این جفت‌ارز — معامله از همان ابتدا خیلی گران می‌شد", "2.9p", "1.50p"],
  ["news_blackout", "تا ۲۲ دقیقه دیگر آمار تورم آمریکا اعلام می‌شود؛ در این پنجره قیمت‌ها غیرقابل پیش‌بینی می‌پرند", "US CPI", "—"],
  ["cost_barrier", "با این هدف و این حد ضرر، باید در ۶۷٫۲٪ مواقع برنده باشیم تا فقط سر به سر شویم — این عملاً دست‌نیافتنی است", "67.2%", "65%"],
  ["entry_spacing", "فقط ۱۸۰ ثانیه از معامله قبلی گذشته و حداقل فاصله ۳۰۰ ثانیه است", "180s", "300s"],
  ["frequency_day", "سقف ۶ معامله در روز پر شده است", "6", "6"],
  ["stop_too_tight", "حد ضرر ۷٫۲ پیپ است، زیر کف مجاز ۱۰ پیپ؛ در این فاصله هزینه‌ها سود احتمالی را می‌بلعند", "7.2p", "10p"],
  ["correlated_risk", "EUR/USD و GBP/USD تقریباً با هم حرکت می‌کنند؛ این دو روی‌هم ۱٫۰۲٪ حساب را در خطر می‌گذارند و سقف ۰٫۹۰٪ است", "1.02%", "0.90%"],
  ["profit_lock", "سود امروز به ۳٫۴٪ رسیده؛ برای اینکه یک روز خوب پس داده نشود، معامله تازه متوقف شد", "+3.4%", "3.0%"],
  ["stale_data", "آخرین قیمت EUR/USD مال ۱۴۷ ثانیه پیش است؛ معامله با قیمتی که دیگر وجود ندارد انجام نمی‌شود", "147s", "90s"],
  ["max_positions", "همین حالا ۴ معامله باز است و این سقف مجاز است", "4", "4"],
  ["clock_skew", "ساعت ما ۱٬۱۰۰ میلی‌ثانیه با ساعت بروکر اختلاف دارد؛ با این اختلاف نمی‌شود گفت کدام اتفاق زودتر رخ داده", "1100ms", "750ms"],
];

// Each strategy states its own reason, in the words a person would use. A
// breakout does not explain itself with a z-score, and a demo that mixes the
// two reads as fabricated.
const RATIONALES: Record<string, ((side: string) => string)[]> = {
  donchian_trend: [
    (side) => side === "BUY"
      ? `قیمت از بالاترین حد ۵۵ کندل گذشته بیرون زد و بازار جهت‌دار است (قدرت جهت ${28 + Math.floor(rnd() * 8)} از ۱۰۰)؛ حد ضرر به اندازه ۲ برابر نوسان معمول و هدف ۵ برابر آن`
      : `قیمت از پایین‌ترین حد ۵۵ کندل گذشته بیرون زد و بازار جهت‌دار است (قدرت جهت ${28 + Math.floor(rnd() * 8)} از ۱۰۰)؛ حد ضرر به اندازه ۲ برابر نوسان معمول و هدف ۵ برابر آن`,
  ],
  vol_reversion: [
    (side) => side === "BUY"
      ? `قیمت ${(2.2 + rnd() * 0.8).toFixed(1)} برابر نوسان معمولش زیر میانگین رفته و نشانه‌های فروش بیش از حد دارد (${Math.floor(22 + rnd() * 9)} از ۱۰۰)، در حالی که بازار جهت مشخصی ندارد — انتظار بازگشت به میانگین`
      : `قیمت ${(2.2 + rnd() * 0.8).toFixed(1)} برابر نوسان معمولش بالای میانگین رفته و نشانه‌های خرید بیش از حد دارد (${Math.floor(69 + rnd() * 9)} از ۱۰۰)، در حالی که بازار جهت مشخصی ندارد — انتظار بازگشت به میانگین`,
  ],
  xs_momentum: [
    (side) => side === "BUY"
      ? `در ۶۰ کندل گذشته، قوی‌ترین ارز از میان ۶ ارز زیر نظر بوده است`
      : `در ۶۰ کندل گذشته، ضعیف‌ترین ارز از میان ۶ ارز زیر نظر بوده است`,
  ],
};

const decisions: Decision[] = [];
for (let i = 0; i < 95; i++) {
  const vetoed = rnd() < 0.62;
  const sym = pick(SYMBOLS);
  const strat = pick(STRATS);
  const side = rnd() < 0.5 ? "BUY" : "SELL";
  const chosen = vetoed
    ? Array.from({ length: 1 + (rnd() < 0.25 ? 1 : 0) }, () => pick(VETOES))
    : [];
  const jpy = sym.includes("JPY");
  const px = jpy ? 150.4 + rnd() : 1.07 + rnd() * 0.2;
  const be = 0.42 + rnd() * 0.22;
  decisions.push({
    ts_ns: ns(NOW - i * 47 * 60 * 1000 - rnd() * 1e6),
    strategy: strat, instrument: sym,
    action: vetoed ? "vetoed" : rnd() < 0.25 ? "queued" : "executed",
    side, lots: (0.04 + rnd() * 0.12).toFixed(2),
    entry: px.toFixed(jpy ? 3 : 5),
    stop: (px * (side === "BUY" ? 0.997 : 1.003)).toFixed(jpy ? 3 : 5),
    target: (px * (side === "BUY" ? 1.007 : 0.993)).toFixed(jpy ? 3 : 5),
    risk_amount: (40 + rnd() * 16).toFixed(2), risk_pct: (0.38 + rnd() * 0.14).toFixed(3),
    signal_strength: +(0.35 + rnd() * 0.6).toFixed(2), regime: pick(REGIMES),
    vetoes: chosen.map(([rule, message, observed, limit]) => ({
      rule, message, severity: "block", observed, limit,
    })),
    warnings: rnd() < 0.2 ? [{ rule: "cost_barrier",
      message: `برای سر به سر شدن باید در ${(be * 100).toFixed(1)}٪ مواقع برنده بود`, severity: "warn",
      observed: `${(be * 100).toFixed(1)}%`, limit: null }] : [],
    lessons: rnd() < 0.3
      ? ["در گذشته، معامله‌هایی که در پرنوسان‌ترین شرایط باز شده‌اند به‌طور میانگین ضرر داده‌اند؛ حجم این معامله به همین دلیل ۲۶٪ کوچک‌تر شد"] : [],
    diagnostics: {
      spread_pips: (0.4 + rnd() * 1.8).toFixed(2),
      stop_pips: (22 + rnd() * 30).toFixed(1),
      reward_risk: (1.3 + rnd() * 1.6).toFixed(2),
      round_trip_cost_pips: (0.45 + rnd() * 0.9).toFixed(2),
      break_even_win_rate: be.toFixed(4),
      drawdown_pct: (rnd() * 6).toFixed(2),
      risk_multiplier: rnd() < 0.8 ? "1" : "0.75",
      gross_leverage: (0.6 + rnd() * 2.4).toFixed(2),
    },
    rationale: pick(RATIONALES[strat])(side),
    explanation: "",
    client_order_id: `SFX${Math.floor(rnd() * 1e12).toString(16).padStart(12, "0")}`,
  });
}
decisions.forEach((d) => {
  const parts = [`استراتژی ${d.strategy} یک فرصت ${d.side === "BUY" ? "خرید" : "فروش"} روی ${d.instrument} دیده: ${d.rationale}.`];
  parts.push(`حال‌وهوای بازار در آن لحظه: ${REGIME_FA[d.regime] ?? d.regime}.`);
  parts.push(`هزینه باز و بسته کردن این معامله ${d.diagnostics.round_trip_cost_pips} پیپ است، پس برای اینکه فقط سر به سر شود باید در ${(Number(d.diagnostics.break_even_win_rate) * 100).toFixed(1)}٪ مواقع درست از آب در بیاید.`);
  if (d.lessons.length) parts.push(`چیزی که از گذشته آموخته شده، حجم این معامله را ۲۶٪ کوچک‌تر کرد: ${d.lessons[0]}`);
  if (d.vetoes.length) parts.push("در نهایت انجام نشد، چون " + d.vetoes.map((v) => v.message).join("؛ ") + ".");
  d.explanation = parts.join(" ");
});
decisions.sort((a, b) => b.ts_ns - a.ts_ns);

/* ---------------- performance ---------------- */

const rs = trades.map((t) => Number(t.r_multiple));
const pnls = trades.map((t) => Number(t.pnl));
const wins = pnls.filter((p) => p > 0);
const losses = pnls.filter((p) => p < 0);
const exitBreak: Record<string, number> = {};
trades.forEach((t) => { exitBreak[t.exit_reason] = (exitBreak[t.exit_reason] ?? 0) + 1; });
const byInstrument: Record<string, Record<string, number>> = {};
trades.forEach((t) => {
  const d = (byInstrument[t.instrument] ??= { n: 0, pnl: 0, wins: 0, win_rate: 0, avg_pnl: 0 });
  d.n += 1; d.pnl += Number(t.pnl); d.wins += Number(t.pnl) > 0 ? 1 : 0;
});
Object.values(byInstrument).forEach((d) => {
  d.win_rate = d.wins / d.n; d.avg_pnl = d.pnl / d.n;
  d.pnl = +d.pnl.toFixed(2); d.avg_pnl = +d.avg_pnl.toFixed(2);
});
const rets = equity.slice(1).map((p, i) => p.equity / equity[i].equity - 1);
const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1));
const downside = rets.filter((r) => r < 0);
const dsd = Math.sqrt(downside.reduce((a, b) => a + b * b, 0) / Math.max(1, downside.length - 1));
const sharpe = (mean / sd) * Math.sqrt(1512);
const years = BARS / 1512;
const cagr = ((finalEq / START_EQ) ** (1 / years) - 1) * 100;
const totalCost = trades.reduce((a, t) => a + Number(t.commission) + Math.abs(Number(t.financing)), 0);

const performance: Performance = {
  net_profit: +(finalEq - START_EQ).toFixed(2),
  net_return_pct: +(((finalEq / START_EQ) - 1) * 100).toFixed(3),
  gross_profit: +(finalEq - START_EQ + totalCost).toFixed(2),
  total_cost: +totalCost.toFixed(2),
  cost_drag_pct: +((totalCost / START_EQ) * 100).toFixed(3),
  cagr_pct: +cagr.toFixed(3),
  sharpe: +sharpe.toFixed(3),
  sortino: +((mean / dsd) * Math.sqrt(1512)).toFixed(3),
  calmar: +(cagr / maxDD).toFixed(3),
  max_drawdown_pct: +maxDD.toFixed(3),
  max_drawdown_amount: +((maxDD / 100) * START_EQ).toFixed(2),
  max_drawdown_duration_bars: 96,
  time_to_recovery_bars: 141,
  ulcer_index: +Math.sqrt(equity.reduce((a, p) => a + p.drawdown_pct ** 2, 0) / equity.length).toFixed(3),
  n_trades: trades.length,
  effective_n: +(trades.length * 0.71).toFixed(1),
  win_rate: +(wins.length / pnls.length).toFixed(4),
  profit_factor: +(wins.reduce((a, b) => a + b, 0) / Math.abs(losses.reduce((a, b) => a + b, 0))).toFixed(3),
  expectancy_r: +(rs.reduce((a, b) => a + b, 0) / rs.length).toFixed(4),
  avg_win_r: +(rs.filter((r) => r > 0).reduce((a, b) => a + b, 0) / rs.filter((r) => r > 0).length).toFixed(3),
  avg_loss_r: +(rs.filter((r) => r < 0).reduce((a, b) => a + b, 0) / rs.filter((r) => r < 0).length).toFixed(3),
  payoff_ratio: 0,
  max_consecutive_losses: 7,
  trades_without_defined_risk: 0,
  return_skew: -0.83, return_kurtosis: 6.42, tail_ratio: 0.91,
  var_95_r: +[...rs].sort((a, b) => a - b)[Math.floor(rs.length * 0.05)].toFixed(3),
  cvar_95_r: +([...rs].sort((a, b) => a - b).slice(0, Math.max(1, Math.floor(rs.length * 0.05)))
    .reduce((a, b) => a + b, 0) / Math.max(1, Math.floor(rs.length * 0.05))).toFixed(3),
  avg_hold_hours: +(trades.reduce((a, t) => a + t.duration_sec, 0) / trades.length / 3600).toFixed(1),
  exposure_pct: 58.4, trades_per_year: +(trades.length / years).toFixed(0),
  avg_mae_r: +(trades.reduce((a, t) => a + Number(t.max_adverse_r), 0) / trades.length).toFixed(3),
  avg_mfe_r: +(trades.reduce((a, t) => a + Number(t.max_favourable_r), 0) / trades.length).toFixed(3),
  edge_efficiency: 0,
  exit_breakdown: exitBreak, by_instrument: byInstrument,
  notes: [
    `از ${trades.length} معامله ثبت‌شده، فقط حدود ${(trades.length * 0.71).toFixed(0)} تا واقعاً مستقل بوده‌اند: خیلی از آن‌ها هم‌زمان و هم‌جهت باز شده‌اند، پس در عمل یک آزمایش‌اند نه چند تا`,
    "شکل نتایج نگران‌کننده است: بردهای کوچکِ پرتکرار و ضررهای بزرگِ نادر. با این شکل، نمره‌های عملکرد خطر را کمتر از واقع نشان می‌دهند.",
  ],
};
performance.payoff_ratio = +Math.abs(performance.avg_win_r / performance.avg_loss_r).toFixed(3);
performance.edge_efficiency = +(performance.expectancy_r / performance.avg_mfe_r).toFixed(3);

/* ---------------- risk ---------------- */

const riskView: RiskView = {
  drawdown_pct: +equity[equity.length - 1].drawdown_pct.toFixed(3),
  equity_peak: Math.max(...equity.map((p) => p.equity)).toFixed(2),
  risk_multiplier: "1",
  day_pnl: "18.44", day_pnl_pct: 0.183,
  trades_today: 2, trades_this_week: 9, trades_this_year: 134,
  gross_leverage: 1.94, pending_risk: "0",
  currency_exposure: [
    { currency: "USD", net_risk: "-88.34", gross_risk: "126.34", net_risk_pct: 0.86,
      contributors: ["AUD_USD", "EUR_USD", "USD_JPY"] },
    { currency: "EUR", net_risk: "54.24", gross_risk: "54.24", net_risk_pct: 0.53, contributors: ["EUR_USD"] },
    { currency: "JPY", net_risk: "38.10", gross_risk: "38.10", net_risk_pct: 0.37, contributors: ["USD_JPY"] },
    { currency: "AUD", net_risk: "34.00", gross_risk: "34.00", net_risk_pct: 0.33, contributors: ["AUD_USD"] },
  ],
  alarms: [],
  limits: {
    risk_per_trade_pct: "0.50", daily_loss_limit_pct: "2.0", weekly_loss_limit_pct: "4.0",
    monthly_loss_limit_pct: "6.0", max_drawdown_halt_pct: "10.0", max_open_positions: 4,
    max_trades_per_day: 6, max_currency_exposure_pct: "1.25",
    max_correlated_risk_pct: "0.90", max_gross_leverage: "5",
  },
  ladder: [
    { drawdown_pct: 3, risk_multiplier: 0.75 }, { drawdown_pct: 5, risk_multiplier: 0.5 },
    { drawdown_pct: 7, risk_multiplier: 0.25 }, { drawdown_pct: 8.5, risk_multiplier: 0 },
  ],
};

/* ---------------- agent knowledge ---------------- */

const lessons: Lesson[] = [
  { id: 3, scope: "strategy", strategy: "donchian_trend", instrument: null, regime: null,
    statement: "معامله‌هایی که در پرنوسان‌ترین ۱۰٪ شرایط باز شده‌اند، به‌طور میانگین ۴۱٪ مبلغ ریسک‌شده ضرر داده‌اند، در حالی که بقیه معامله‌ها ۱۹٪ سود داده‌اند",
    evidence: { n_tagged: 44, n_other: 90, t_stat: -3.41 }, sample_size: 44,
    effect_r: -0.41, p_value: 0.0009, confidence: 0.86, caution: 0.79, status: "active",
    created_ns: ns(NOW - 9 * 86400000) },
  { id: 5, scope: "instrument", strategy: null, instrument: "USD_JPY", regime: null,
    statement: "معامله روی USD/JPY در ساعت‌های آسیا به‌طور میانگین ۲٫۴ برابر ساعت‌های لندن هزینه داشته است — همان معامله، فقط گران‌تر",
    evidence: { n_tagged: 31, median_spread_ratio: 2.4 }, sample_size: 31,
    effect_r: -0.23, p_value: 0.0042, confidence: 0.71, caution: 0.85, status: "active",
    created_ns: ns(NOW - 5 * 86400000) },
  { id: 7, scope: "global", strategy: null, instrument: null, regime: "stress",
    statement: "در روزهای بحرانی بازار، جفت‌ارزهایی که شبیه هم حرکت می‌کنند همه با هم ضرر می‌دهند؛ ضرر آن روزها ۲٫۱ برابر چیزی بوده که با فرض استقلال تخمین زده می‌شد",
    evidence: { n_tagged: 26, observed_vs_independent: 2.1 }, sample_size: 26,
    effect_r: -0.58, p_value: 0.0031, confidence: 0.63, caution: 0.65, status: "active",
    created_ns: ns(NOW - 2 * 86400000) },
];

const proposals: Proposal[] = [
  { id: "P19f3a2c", created_ns: ns(NOW - 36 * 3600000), path: "risk.trail_atr_multiple",
    current_value: 2.5, proposed_value: 2.0, strategy: "donchian_trend",
    rationale: "در ۶۱ معامله، سودی که در نهایت گرفته شد خیلی کمتر از بیشترین سودی بود که وسط راه روی میز آمد. اگر حد ضرر دنبال‌کننده نزدیک‌تر دنبال قیمت بیاید، تخمین می‌زنیم هر معامله به‌طور میانگین ۴۳٪ مبلغ ریسک‌شده بهتر شود",
    evidence: { pattern: "counterfactual:trail_tighter", n: 61, mean_delta_r: 0.43, t_stat: 5.21 },
    sample_size: 61, expected_effect_r: 0.43, effect_ci_low: 0.27, effect_ci_high: 0.59,
    p_value: 0.00002, status: "pending", validation_run_id: null, reviewed_by: null },
  { id: "P19f1b80", created_ns: ns(NOW - 61 * 3600000), path: "risk.partial_take_r",
    current_value: 1.5, proposed_value: 1.2, strategy: "donchian_trend",
    rationale: "۳۱ معامله یک‌بار به اندازه مبلغ ریسک‌شده در سود رفتند و بعد همه‌اش را پس دادند. اگر در آن نقطه نصف معامله بسته می‌شد، تخمین می‌زنیم هر معامله به‌طور میانگین ۲۲٪ مبلغ ریسک‌شده بهتر شود",
    evidence: { pattern: "counterfactual:partial_at_1R", n: 31, mean_delta_r: 0.22, t_stat: 4.02 },
    sample_size: 31, expected_effect_r: 0.22, effect_ci_low: 0.11, effect_ci_high: 0.33,
    p_value: 0.0002, status: "approved", validation_run_id: null, reviewed_by: "lord" },
  { id: "P19e7d41", created_ns: ns(NOW - 9 * 86400000), path: "risk.max_spread_pips_multiple",
    current_value: 2.5, proposed_value: 2.0, strategy: null,
    rationale: "معامله‌هایی که موقع ورود بدتر از قیمت درخواستی پر شده‌اند، در نهایت ۳۱٪ مبلغ ریسک‌شده بدتر تمام شده‌اند. پیشنهاد: در شرایطی که اختلاف قیمت خرید و فروش زیاد است، سخت‌گیرتر باشیم",
    evidence: { pattern: "tag:entry_slippage", n: 47, mean_delta_r: -0.31, t_stat: -3.9 },
    sample_size: 47, expected_effect_r: 0.31, effect_ci_low: 0.15, effect_ci_high: 0.47,
    p_value: 0.0004, status: "validated", validation_run_id: "RUN-2026-0913-07", reviewed_by: "lord" },
];

/* ---------------- acceptance gates ---------------- */

const gates: Gate[] = [
  { id: "L0.1", name: "آیا هزینه‌ها اجازه سود می‌دهند؟", passed: true,
    observed: "باید در ۲۹٫۴٪ مواقع برنده باشیم", threshold: "کمتر از ۶۰٪",
    detail: "با هدف ۷۵ پیپ، حد ضرر ۳۰ پیپ و هزینه ۰٫۹ پیپ، درصدی که برای سر به سر شدن لازم است پایین و قابل دستیابی می‌ماند",
    blocking: true },
  { id: "L0.2", name: "آیا سرمایه برای اندازه‌گیری درست ریسک کافی است؟", passed: true,
    observed: "حساب ۱۰٬۰۰۰ دلاری", threshold: "دست‌کم ۳٬۰۰۰ دلار",
    detail: "زیر این مقدار، کوچک‌ترین معامله ممکن (۰٫۰۱ لات) باعث می‌شود مبلغی که واقعاً ریسک می‌شود بیش از ۲۰٪ با آنچه می‌خواستیم فرق کند",
    blocking: true },
  { id: "L0.3", name: "آیا هر سفارش شماره یکتا می‌گیرد؟", passed: true,
    observed: "بله، بروکر پشتیبانی می‌کند", threshold: "لازم است",
    detail: "بدون آن، اگر پاسخ بروکر گم شود نمی‌شود با اطمینان فهمید سفارش ثبت شده یا نه",
    blocking: false },
  { id: "L0.4", name: "آیا حد ضرر نزد خود بروکر ثبت می‌شود؟", passed: true,
    observed: "بله، بروکر پشتیبانی می‌کند", threshold: "لازم است",
    detail: "حد ضرری که فقط در برنامه ما زندگی می‌کند، هنگام قطع اتصال عملاً وجود ندارد",
    blocking: true },
  { id: "L0.5", name: "آیا اتصال به اندازه کافی پایدار است؟", passed: false,
    observed: "۹۷٫۸۲٪ مواقع وصل بوده‌ایم", threshold: "دست‌کم ۹۹٪",
    detail: "۹۷٫۸۲٪ یعنی حدود ۳۱ دقیقه قطعی در شبانه‌روز. با این کیفیت اتصال، استراتژی‌های کوتاه‌مدت فقط به دلیل زیرساخت کنار گذاشته می‌شوند — مستقل از اینکه چقدر خوب باشند",
    blocking: true },
  { id: "L0.6", name: "آیا پول واقعاً وارد و خارج می‌شود؟", passed: true,
    observed: "واریز، نگهداری و برداشت با موفقیت انجام شد", threshold: "لازم است",
    detail: "اگر بروکر پول را پس ندهد، هیچ استراتژی‌ای آن را جبران نمی‌کند. این آزمون پیش از هر تحلیل مالی انجام می‌شود",
    blocking: true },
  { id: "L1", name: "آیا از ساده‌ترین جایگزین‌ها بهتر است؟", passed: true,
    observed: "نمره عملکرد این استراتژی ۰٫۷۴", threshold: "بالاتر از همه جایگزین‌ها و بالاتر از صفر",
    detail: "معامله تصادفی نمره -۱٫۹۷ گرفت؛ خرید و نگه داشتن ۰٫۱۲؛ معامله نکردن ۰٫۰۰",
    blocking: true },
  { id: "L2", name: "آیا از حدس «فردا مثل امروز» بهتر است؟", passed: false,
    observed: "احتمال تصادفی بودن ۰٫۰۸۳", threshold: "کمتر از ۰٫۰۱",
    detail: "این آزمون (Clark-West) مدل را با ساده‌ترین حدس ممکن مقایسه می‌کند. مدلی که نتواند آن را شکست بدهد، هیچ اطلاعاتی اضافه نکرده — فقط پیچیده‌تر است",
    blocking: true },
  { id: "L3", name: "آیا سودش از عوامل عمومی بازار نیامده؟", passed: false,
    observed: "احتمال تصادفی بودن ۰٫۰۸۰", threshold: "کمتر از ۰٫۰۱",
    detail: "اگر یک استراتژی فقط وقتی سود می‌دهد که دلار ضعیف شود، آن سود مال استراتژی نیست؛ مال یک روند عمومی است که هر کسی می‌توانست بگیرد",
    blocking: true },
  { id: "L4", name: "آیا نتیجه فقط حاصل امتحان کردن نسخه‌های زیاد است؟", passed: true,
    observed: "احتمال ۰٫۱۸", threshold: "حداکثر ۰٫۲۰",
    detail: "روی ۲۵۲ برش مختلف سنجیده شد: احتمال اینکه نسخه انتخاب‌شده در داده تازه زیان بدهد، ۰٫۳۱ برآورد شده",
    blocking: true },
  { id: "L5.1", name: "نمره عملکرد پس از کم کردن اثر شانس", passed: false,
    observed: "۰٫۳۱ (نمره خام ۰٫۷۴ در برابر سدِ شانس ۱٫۱۹)", threshold: "دست‌کم ۰٫۹۵",
    detail: "۴۸ نسخه امتحان شده است. حتی اگر هیچ‌کدام هیچ مزیتی نداشته باشند، انتظار می‌رود بهترینشان نمره‌ای حدود ۱٫۱۹ بگیرد — فقط از شانس. نمره واقعی ۰٫۷۴ است، یعنی پایین‌تر از همان سد",
    blocking: true },
  { id: "L5.2", name: "آیا بهترین نسخه واقعاً بهتر است یا شانسی؟", passed: true,
    observed: "احتمال تصادفی بودن ۰٫۰۰۶", threshold: "کمتر از ۰٫۰۱",
    detail: "این آزمون (Hansen SPA) همه نسخه‌های امتحان‌شده را با هم در نظر می‌گیرد و ترتیب زمانی داده را هم حفظ می‌کند. یکی از معدود آزمون‌های عبورشده",
    blocking: true },
  { id: "L6", name: "آیا روی برش‌های مختلف تاریخ پایدار است؟", passed: false,
    observed: "فقط ۶۰٪ از ۱۵ برش سودده", threshold: "دست‌کم ۷۰٪",
    detail: "بدترین برش‌ها نمره -۰٫۳۳، برش میانه ۰٫۲۱ و بهترین‌ها ۱٫۱۱ گرفته‌اند — یعنی ۶ برش از ۱۵ برش مستقل تاریخ، زیان‌ده بوده‌اند",
    blocking: true },
  { id: "L7", name: "آیا با هزینه و کندی دو برابر هم سودده می‌ماند؟", passed: false,
    observed: "نمره -۰٫۲۲ و بازده -۱٫۸۴٪", threshold: "بالاتر از صفر",
    detail: "استراتژی‌ای که فقط با خوش‌بینانه‌ترین فرض هزینه سودده است، در دنیای واقعی سودده نخواهد بود",
    blocking: true },
  { id: "L8", name: "آیا بدترین افت حساب قابل تحمل بوده؟", passed: true,
    observed: `بدترین مسیر ${maxDD.toFixed(2)}٪`, threshold: "حداکثر ۱۰٪",
    detail: "این حد پیش از اجرا اعلام شده. عدد گزارش‌شده بدترین مسیر مشاهده‌شده است، نه میانگین مسیرها",
    blocking: true },
  { id: "L9", name: "آیا اصلاً به اندازه کافی داده داریم؟", passed: false,
    observed: "۴۵۹ معامله", threshold: "دست‌کم ۲٬۹۷۸ معامله",
    detail: "با این مقدار نوسان و این شکل ضررها، کمتر از ۲٬۹۷۸ معامله نمی‌تواند سود واقعی را از شانس جدا کند. یعنی حتی اگر همه‌چیز درست باشد، هنوز نمی‌شود نتیجه گرفت",
    blocking: true },
  { id: "L10", name: "آیا داده‌ها واقعی‌اند؟", passed: false,
    observed: "داده ساختگی", threshold: "داده واقعی خود بروکر",
    detail: "برای پذیرش، به قیمت‌های واقعی خرید و فروش خود بروکر و جدول کارمزد واقعی نیاز است. داده ساختگی می‌تواند یک استراتژی را رد کند، ولی هرگز نمی‌تواند آن را تأیید کند",
    blocking: true },
];

/* ---------------- audit ---------------- */

const AUDIT_EVENTS = [
  "decision.signal", "decision.risk_veto", "order.intent", "order.sent", "order.filled",
  "position.modify", "position.close", "ops.reconcile", "system.heartbeat",
  "learn.postmortem", "config.change", "sec.auth_ok", "sec.write_action", "risk.ladder_step",
];
const audit: AuditRecord[] = Array.from({ length: 60 }, (_, i) => ({
  seq: 4821 - i,
  ts_ns: ns(NOW - i * 7 * 60 * 1000),
  run_id: "a3f19c02",
  event: pick(AUDIT_EVENTS),
  actor: rnd() < 0.15 ? "lord" : "system",
  payload: { instrument: pick(SYMBOLS), detail: "…" },
  prev_hash: Math.floor(rnd() * 1e16).toString(16).padStart(64, "0"),
  hash: Math.floor(rnd() * 1e16).toString(16).padStart(64, "0"),
}));

/* ---------------- strategies ---------------- */

const strategies: StrategyInfo[] = [
  { name: "donchian_trend", version: "1.1.0", timeframe: "H4", horizon_bars: 60,
    lifecycle: "experimental", required_history: 250,
    description: "وقتی قیمت از بالاترین یا پایین‌ترین حد چند روز اخیر بیرون می‌زند، وارد می‌شود — به شرطی که بازار واقعاً جهت‌دار باشد",
    hypothesis: "نرخ ارزها روندهای کند و طولانی دارند، چون انتظارها درباره نرخ بهره کشورها به‌تدریج تغییر می‌کند. وقتی قیمت از محدوده چند روز اخیرش بیرون می‌زند، احتمال ادامه همان جهت بیشتر است — به شرطی که حد ضرر آن‌قدر دور باشد که نوسان‌های معمولی آن را نزنند",
    failure_conditions: [
      "اگر معلوم شود سودش فقط از روندهای عمومی دلار می‌آید و نه از خود استراتژی",
      "اگر نتواند از حدس ساده «فردا مثل امروز» بهتر عمل کند",
      "اگر فقط با خوش‌بینانه‌ترین فرض هزینه سودده باشد",
      "اگر درصد بردش از آن حداقلی که هزینه‌های واقعی می‌طلبند کمتر باشد",
    ],
    default_params: { channel: 55, exit_channel: 20, atr_window: 20, stop_atr: 2.0,
      target_atr: 5.0, adx_window: 14, adx_min: 20, vol_filter_quantile: 0.9 } },
  { name: "vol_reversion", version: "1.0.0", timeframe: "H1", horizon_bars: 18,
    lifecycle: "hypothesis", required_history: 200,
    description: "وقتی قیمت به‌طور غیرعادی از میانگین خودش دور می‌شود، روی بازگشت آن شرط می‌بندد — فقط در بازارهای بی‌جهت",
    hypothesis: "در بازاری که جهت مشخصی ندارد، هجوم موقتی سفارش‌ها قیمت را از حد معمولش دور می‌کند و ظرف چند ساعت به جای خودش برمی‌گردد",
    failure_conditions: ["اگر پس از احتساب هزینه واقعی، میانگین هر معامله منفی باشد",
      "اگر در پرنوسان‌ترین ۱۰٪ شرایط ضرر بدهد — یعنی تشخیص حال‌وهوای بازار کار نمی‌کند",
      "اگر شکل نتایج «بردهای خیلی کوچک و ضررهای خیلی بزرگ» باشد"],
    default_params: { z_window: 60, z_entry: 2.2, rsi_window: 14, stop_atr: 2.0, target_atr: 2.6 } },
  { name: "xs_momentum", version: "1.0.0", timeframe: "D1", horizon_bars: 20,
    lifecycle: "hypothesis", required_history: 300,
    description: "ارزها را با هم مقایسه می‌کند و روی قوی‌ترین‌ها در برابر ضعیف‌ترین‌ها شرط می‌بندد",
    hypothesis: "ارزی که در چند هفته گذشته بهتر از بقیه عمل کرده، در هفته‌های بعد هم احتمالاً بهتر عمل می‌کند",
    failure_conditions: ["اگر معلوم شود سودش از روندهای عمومی می‌آید و نه از خود استراتژی",
      "اگر با تعداد معامله‌های واقع‌بینانه، هزینه‌ها از سود بیشتر شوند",
      "اگر این اثر در یک‌سوم آخر داده دیده نشود — یعنی از وقتی همه از آن باخبر شدند، از بین رفته"],
    default_params: { formation: 60, skip: 1, top_n: 1, stop_atr: 2.5, target_atr: 5.0 } },
  { name: "carry_tilt", version: "1.0.0", timeframe: "D1", horizon_bars: 40,
    lifecycle: "hypothesis", required_history: 200,
    description: "ارزی را نگه می‌دارد که بهره شبانه بیشتری می‌دهد، و در بازار بحرانی کنار می‌کشد",
    hypothesis: "اختلاف نرخ بهره کشورها یک سود آرام و پیوسته می‌دهد، تا وقتی که بازار آرام است",
    failure_conditions: ["اگر شکل نتایج «سودهای کوچک پیوسته و یک ضرر بزرگ ناگهانی» باشد — در آن صورت آن سود آرام، در واقع اجاره ریسک سقوط است",
      "اگر افت حساب در یک روز بحرانی از سقف اعلام‌شده بیشتر شود",
      "اگر معلوم شود کل سودش همان اختلاف نرخ بهره است و هیچ چیز اضافه‌ای ندارد"],
    default_params: { vol_window: 60, vol_stress_quantile: 0.85, stop_atr: 3.0, target_atr: 6.0 } },
  { name: "baseline_coin_flip", version: "1.0.0", timeframe: "H1", horizon_bars: 24,
    lifecycle: "reference", required_history: 30,
    description: "جهت را با شیر یا خط انتخاب می‌کند، ولی دقیقاً همان حد ضرر، همان هدف و همان حجم استراتژی اصلی را به کار می‌برد",
    hypothesis: "این برای مقایسه است، نه برای سود. اگر استراتژی اصلی نتواند از انتخاب تصادفی بهتر عمل کند، یعنی هر سودی که دیده می‌شود از قواعد خروج و مدیریت ریسک آمده، نه از توانایی تشخیص فرصت",
    failure_conditions: [], default_params: { entry_probability: 0.05 } },
];

/* ---------------- tables from the research brief ---------------- */

const costTable = [
  { target: 3, raw: 57.5, standard: 66.7 },
  { target: 5, raw: 54.5, standard: 60.0 },
  { target: 10, raw: 52.2, standard: 55.0 },
  { target: 30, raw: 50.7, standard: 51.7 },
  { target: 100, raw: 50.2, standard: 50.5 },
];
const capitalTable = [
  { stop: 10, minEquity: 500 }, { stop: 30, minEquity: 1500 },
  { stop: 50, minEquity: 2500 }, { stop: 100, minEquity: 5000 },
];
const cpcvSharpes = [-0.41, -0.33, -0.21, -0.12, -0.04, -0.01, 0.09, 0.21, 0.38,
  0.52, 0.66, 0.81, 0.97, 1.11, 1.24];

const monthly = [
  { year: 1404, months: [null, null, null, null, null, null, 1.42, -0.86, 2.31, 0.54, -1.92, 0.77] },
  { year: 1405, months: [2.08, -0.44, 1.19, -2.61, 0.33, 1.87, -0.75, 1.02, 0.41, null, null, null] },
];

/* ---------------- status ---------------- */

const status: Status = {
  ts_ns: ns(NOW), uptime_sec: 412_800, mode: "advisory", venue_mode: "paper",
  halted: false, halt_reason: "",
  kill_switch: { engaged: false, reason: "" },
  cycles: 6881,
  account: {
    id: "PAPER-001", currency: "USD",
    balance: (finalEq - 31.72).toFixed(2), equity: finalEq.toFixed(2),
    margin_used: "486.20", margin_available: (finalEq - 486.2).toFixed(2),
    unrealised_pnl: "31.72", margin_level_pct: ((finalEq / 486.2) * 100).toFixed(0),
    open_positions: positions.length,
  },
  regime: {
    regime: "trending", confidence: 0.68, vol_percentile: 0.54, trend_strength: 26.4,
    correlation_dispersion: 0.41, risk_multiplier: 1.0,
    explanation: "قیمت جهت نسبتاً مشخصی دارد و میزان نوسان تقریباً در حد معمول خودش است",
    inputs: { vol_percentile: 0.54, avg_abs_correlation: 0.41, mean_adx: 26.4, n_pairs: 15 },
  },
  health: {
    connected: true, uptime_pct: 97.82, offline_seconds: 0,
    median_latency_ms: 214, p95_latency_ms: 786, clock_skew_ms: -38.4,
    clock_regressions: 1, outages_24h: 3, longest_outage_sec: 412,
    warnings: ["در ۲۴ ساعت گذشته فقط ۹۷٫۸۲٪ مواقع به بروکر وصل بوده‌ایم، یعنی حدود ۳۱ دقیقه قطعی. حد لازم ۹۹٪ است.",
      "طولانی‌ترین قطعی اخیر ۶٫۹ دقیقه طول کشیده. هر استراتژی‌ای که معامله‌هایش کوتاه‌تر از این باشند، ممکن است کل عمر یک معامله را در قطعی بگذراند."],
  },
  broker: {
    name: "paper", supports_client_order_id: true, supports_server_side_stop: true,
    supports_transaction_stream: true,
    degradations: [],
  },
  unresolved_orders: 0, quarantined: [],
  advisory_pending: 4, proposals_pending: 1, config_version: 12,
  errors: [], api_version: "1.0.0",
};

const execution = {
  n: 176, fills: 159, rejects: 17, reject_rate: 0.0966,
  median_latency_ms: 214, p95_latency_ms: 786,
  mean_slippage_pips: 0.182, median_slippage_pips: 0.15,
  adverse_slippage_share: 0.673, last_look_asymmetry: 0.71,
  last_look_note: "بیشتر سفارش‌هایی که رد شده‌اند، دقیقاً در لحظه‌ای رد شده‌اند که بازار به نفع ما حرکت می‌کرد. عدد منصفانه حدود ۵۰٪ است؛ بالاتر از آن یعنی طرف مقابل فقط وقتی معامله را می‌پذیرد که به ضرر ما باشد.",
};

const advice: Decision[] = decisions.filter((d) => d.action === "queued").slice(0, 4);

export const demoSnapshot: Snapshot = {
  status, positions, trades, equity, decisions, risk: riskView, performance,
  lessons, proposals, audit, strategies, execution, gates,
  allocations: {
    donchian_trend: { name: "donchian_trend", enabled: true, weight: "1.0",
      instruments: ["EUR_USD", "GBP_USD", "AUD_USD", "USD_JPY"], timeframe: "H4",
      lifecycle: "experimental", params: {} },
    vol_reversion: { name: "vol_reversion", enabled: true, weight: "0.6",
      instruments: ["EUR_USD", "GBP_USD"], timeframe: "H1", lifecycle: "hypothesis", params: {} },
    xs_momentum: { name: "xs_momentum", enabled: false, weight: "1.0",
      instruments: SYMBOLS, timeframe: "D1", lifecycle: "hypothesis", params: {} },
  },
  config: {
    version: 12,
    agent: { mode: "advisory", decision_interval_sec: 60, session_windows_utc: [[7, 16]],
      trade_days: [0, 1, 2, 3, 4], learning_enabled: true, proposal_min_sample: 40,
      proposal_requires_human: true, regime_detection_enabled: true, explain_every_decision: true },
    risk: {
      risk_per_trade_pct: "0.50", max_lots_per_trade: "5", min_stop_pips: "10",
      max_stop_pips: "250", min_reward_risk: "1.2", require_broker_side_stop: true,
      max_open_positions: 4, max_positions_per_instrument: 1, max_gross_leverage: "5",
      max_currency_exposure_pct: "1.25", max_correlated_risk_pct: "0.90",
      correlation_threshold: 0.65, daily_loss_limit_pct: "2.0", weekly_loss_limit_pct: "4.0",
      monthly_loss_limit_pct: "6.0", max_drawdown_halt_pct: "10.0",
      max_trades_per_day: 6, max_trades_per_week: 20, max_trades_per_year: 500,
      min_seconds_between_entries: 300, max_annual_cost_pct_of_equity: "15",
      ladder_enabled: true,
      ladder: riskView.ladder,
      breakeven_trigger_r: "1.0", partial_take_r: "1.5", partial_take_fraction: "0.5",
      trail_atr_multiple: "2.5", trail_activate_r: "1.0", daily_profit_lock_pct: "3.0",
      max_data_staleness_sec: 90, max_clock_skew_ms: 750, max_spread_pips_multiple: "2.5",
      block_minutes_before_high_impact: 30, block_minutes_after_high_impact: 30,
      weekend_flat: true, friday_close_utc_hour: 19, max_offline_seconds_before_freeze: 120,
    },
    execution: { venue_mode: "paper", broker: "paper", account_currency: "USD",
      order_type: "market", max_slippage_pips: "1.5", submit_timeout_ms: 5000,
      max_submit_retries: 2, reconcile_on_start: true, reconcile_interval_sec: 30,
      quarantine_on_unknown_state: true },
    news: { enabled: true, role: "risk_filter", llm_enabled: false,
      llm_model: "claude-opus-4-5", llm_training_cutoff: "2025-01-01",
      require_post_cutoff_only: true, lap_test_required: true },
    research: { alpha: 0.01, pbo_max: 0.2, min_dsr: 0.95,
      cpcv_positive_path_fraction: 0.7, cost_stress_multiple: 2, latency_stress_multiple: 2,
      declared_prior: 0.03, max_total_trials_declared: 200,
      require_random_walk_beat: true, require_factor_alpha: true },
    security: { dashboard_read_only_default: true, require_totp_for_writes: true,
      session_ttl_minutes: 30, bind_host: "127.0.0.1", bind_port: 8088,
      api_rate_limit_per_minute: 120, write_rate_limit_per_minute: 10 },
    ops: { heartbeat_interval_sec: 5, deadman_timeout_sec: 45, deadman_action: "close_only",
      killswitch_file: "var/KILL", log_level: "INFO" },
  },
  advice, health: status.health as Record<string, any>,
  costTable, capitalTable, cpcvSharpes, monthly,
};

/* ---------------------------------------------------------------------- *
 * Demo fixtures for the on-demand endpoints.
 *
 * Same principle as the rest of the demo dataset: the numbers are chosen to
 * be INSTRUCTIVE, not flattering. The licence is two weeks from expiry, one
 * saved connection has failed its test, and the credential key is in the weak
 * position -- because a demo where everything is green teaches nothing about
 * what the screen looks like when something is wrong, which is the only time
 * anyone reads it carefully.
 * ---------------------------------------------------------------------- */

const now = Date.now() * 1e6;
const day = 86400e9;

export const demoEndpoints: Record<string, any> = {
  "/api/brokers": {
    active_profile: "paper",
    active_adapter: "paper-sim",
    venue_mode: "paper",
    degradations: [],
    open_positions: 3,
    damaged: [],
    restart_required: false,
    credential_storage: {
      level: "weak",
      key_source: "file",
      key_path: "var/broker-secrets.key",
      note: "کلید رمزنگاری در همان پوشه‌ای است که رمزها ذخیره شده‌اند. این جلوی " +
            "کسی را که یک نسخهٔ پشتیبان را بدزدد می‌گیرد، ولی جلوی کسی که به " +
            "خود این سرور دسترسی دارد را نه — چون او هر دو فایل را می‌خواند.",
    },
    connections: [
      {
        id: "sim", display_name: "شبیه‌ساز داخلی", profile: "paper",
        adapter: "paper", origin: "auto", server: "", login: "",
        login_full_length: 0, terminal_path: "", account_currency: "USD",
        exchange_id: "", declared_account_type: "demo", enabled: true,
        created_ns: now - 40 * day, updated_ns: now - 2 * day,
        has_credential: false, notes: "جایی که اشتباه کردن رایگان است.",
        last_probe: {
          connection_id: "sim", profile: "paper", adapter: "paper", ok: true,
          started_ns: now - 2 * day, finished_ns: now - 2 * day,
          duration_sec: 0.4, symbols_total: 8, blocking_failures: [],
          symbol_examples: { EUR_USD: "EUR_USD", USD_JPY: "USD_JPY" },
          mismatches: [], degradations: [], error: "",
          account_id: "•••••001", account_currency: "USD", account_type: "demo",
          checks: [
            { id: "connect", title: "اتصال به بروکر برقرار شد", passed: true,
              severity: "block", detail: "آداپتور «paper» وصل شد." },
            { id: "account", title: "اطلاعات حساب خوانده شد", passed: true,
              severity: "block", detail: "موجودی: 9412.30 USD · پول حساب: 9377.10" },
            { id: "account_type", title: "نوع حساب همان است که اعلام کرده‌اید",
              passed: true, severity: "info",
              detail: "حساب تمرینی است — پولی در خطر نیست." },
            { id: "server_stop", title: "حد ضرر نزد خود بروکر ثبت می‌شود",
              passed: true, severity: "block",
              detail: "اگر برق یا اینترنت این سرور قطع شود، حد ضرر همچنان سر جایش است." },
          ],
        },
      },
      {
        id: "amarkets-demo", display_name: "AMarkets — حساب تمرینی",
        profile: "amarkets", adapter: "mt5", origin: "manual",
        server: "AMarkets-Demo", login: "••••••217", login_full_length: 9,
        terminal_path: "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
        account_currency: "USD", exchange_id: "",
        declared_account_type: "demo", enabled: false,
        created_ns: now - 9 * day, updated_ns: now - 1 * day,
        has_credential: true, notes: "",
        last_probe: {
          connection_id: "amarkets-demo", profile: "amarkets", adapter: "mt5",
          ok: false, started_ns: now - day, finished_ns: now - day,
          duration_sec: 21.8, symbols_total: 0,
          blocking_failures: ["connect"], symbol_examples: {},
          mismatches: [], degradations: [],
          account_id: "", account_currency: "", account_type: "",
          error: "BrokerError: MT5 initialize failed",
          checks: [
            { id: "connect", title: "اتصال به بروکر برقرار نشد", passed: false,
              severity: "block",
              detail: "ترمینال متاتریدر پاسخ نداد. بررسی کنید: ترمینال باز است؟ " +
                      "مسیر آن درست وارد شده؟ نام سرور «AMarkets-Demo» دقیقاً همان " +
                      "چیزی است که در خود ترمینال نوشته شده؟" },
          ],
        },
      },
    ],
    profiles: [
      { name: "alpari", display_name: "Alpari", adapter: "mt5",
        regulator: "Mwali International Services Authority (Comoros)",
        max_leverage: 1000, min_stop_level_points: 0,
        commission_per_lot_round_turn: "0", default_spread_pips: "1.6",
        supports_server_side_stop: true, supports_hedging: true,
        segregated_client_funds: null, negative_balance_protection: null,
        symbol_suffix: "", notes: "",
        verify_before_live: ["یک برداشت کامل انجام دهید و تا رسیدن پول صبر کنید."] },
      { name: "amarkets", display_name: "AMarkets", adapter: "mt5",
        regulator: "Financial Services Authority (Saint Vincent)",
        max_leverage: 1000, min_stop_level_points: 0,
        commission_per_lot_round_turn: "0", default_spread_pips: "1.3",
        supports_server_side_stop: true, supports_hedging: true,
        segregated_client_funds: null, negative_balance_protection: null,
        symbol_suffix: "", notes: "",
        verify_before_live: ["یک برداشت کامل انجام دهید و تا رسیدن پول صبر کنید."] },
      { name: "generic_mt5", display_name: "هر بروکر متاتریدر ۵", adapter: "mt5",
        regulator: "unknown", max_leverage: 500, min_stop_level_points: 0,
        commission_per_lot_round_turn: "0", default_spread_pips: "1.5",
        supports_server_side_stop: true, supports_hedging: true,
        segregated_client_funds: null, negative_balance_protection: null,
        symbol_suffix: "", notes: "هیچ چیزی را فرض نمی‌کند؛ همه را از ترمینال می‌خواند.",
        verify_before_live: [] },
      { name: "paper", display_name: "شبیه‌ساز داخلی", adapter: "paper",
        regulator: "—", max_leverage: 30, min_stop_level_points: 0,
        commission_per_lot_round_turn: "7", default_spread_pips: "0.6",
        supports_server_side_stop: true, supports_hedging: false,
        segregated_client_funds: null, negative_balance_protection: null,
        symbol_suffix: "", notes: "", verify_before_live: [] },
    ],
  },

  "/api/licence": {
    enforced: true, valid: true, reason: "", warnings: [],
    in_grace: false, days_remaining: 12.4, unlicensed_mode: false,
    tier: "live_single", issued_to: "نمونهٔ نمایشی",
    licence_id: "DEMO-0000-0000-0000", subscription_id: "DEMO-0000-0000-0000",
    // Relative to `now` like every other timestamp in this file. Hard-coded
    // dates drifted: the card said "12 days remaining" beside a date that had
    // already passed.
    issued_at: new Date(Date.now() - 80 * 86400e3).toISOString(),
    expires_at: new Date(Date.now() + 12 * 86400e3).toISOString(),
    term_months: 3, term_label: "سه‌ماهه", term_index: 2,
    machine_bound: true, stage: "due",
    headline: "وقت تمدید رسیده",
    advice: "لایسنس شما 12 روز دیگر تمام می‌شود. اگر تمدید نشود، ربات معاملهٔ تازه باز نمی‌کند.",
    live_trading_allowed: true, live_trading_reason: "",
    effective_capabilities: {
      live_trading: true, max_instruments: 8, max_accounts: 1,
      max_equity: null, research_lab: true, llm_news: true,
    },
    clock: { ok: true, rolled_back: false, rollback_seconds: 0,
             previously_expired: false, state_missing: false,
             state_unreadable: false, message: "", checks: 1841,
             first_seen_ns: now - 92 * day },
    integrity: null,
  },

  "/api/licence/fingerprint": {
    fingerprint: { machine_id: "9f3c…", mac: "b41a…", cpu_model: "7e02…",
                   root_fs: "c118…", hostname: "2ad9…" },
    components: {},
    note: "این مقادیر هش‌شده‌اند و چیزی از محتوای سرور شما لو نمی‌دهند.",
  },

  "/api/users": {
    min_password_length: 12,
    roles: [
      { id: "owner", label: "مدیر",
        description: "همه‌کاره. می‌تواند سقف‌های ایمنی را عوض کند، حالت ربات را روی «کاملاً خودکار» بگذارد، کاربر بسازد و توقف اضطراری را آزاد کند. این سطح را فقط به خودتان بدهید." },
      { id: "operator", label: "کاربر",
        description: "می‌تواند معامله‌ای را ببندد، همه را ببندد، ربات را متوقف کند و پیشنهادها را بپذیرد یا رد کند. نمی‌تواند سقف‌های ایمنی یا تنظیمات را عوض کند و نمی‌تواند کاربر بسازد." },
      { id: "viewer", label: "نظاره‌گر",
        description: "فقط می‌بیند. هیچ دکمه‌ای برایش کار نمی‌کند. برای کسی که باید گزارش‌ها را ببیند ولی نباید چیزی را تکان بدهد." },
    ],
    users: [
      { username: "owner", role: "owner", role_label: "مدیر", disabled: false,
        created_ns: now - 92 * day, can_write: true, can_change_risk: true,
        active_sessions: 1, is_last_owner: true },
      { username: "trader", role: "operator", role_label: "کاربر", disabled: false,
        created_ns: now - 40 * day, can_write: true, can_change_risk: false,
        active_sessions: 0, is_last_owner: false },
      { username: "accountant", role: "viewer", role_label: "نظاره‌گر",
        disabled: false, created_ns: now - 12 * day, can_write: false,
        can_change_risk: false, active_sessions: 0, is_last_owner: false },
      { username: "old-laptop", role: "operator", role_label: "کاربر",
        disabled: true, created_ns: now - 120 * day, can_write: false,
        can_change_risk: false, active_sessions: 0, is_last_owner: false },
    ],
  },
};
