/**
 * Figures the live dashboard derives itself, instead of borrowing the demo's.
 *
 * The live provider used to fill the research gates, the CPCV paths, the two
 * reference tables and the monthly table from the bundled demo dataset. A
 * real install therefore showed made-up acceptance numbers and a made-up
 * year of monthly returns as if they were the owner's. Everything here is
 * either computed from a formula whose assumptions are printed beside it, or
 * mapped from what the server actually stored.
 */
import type { Gate, ResearchSummary } from "./types";

/** Break-even win rate for a trade whose stop equals its target:
 *  p = (S + c) / (T + S), with S = T. The two round-trip costs are the
 *  reference accounts the Risk page names in its column headings. */
export function breakEvenTable(targets = [3, 5, 10, 30, 100], rawCost = 0.45,
                               standardCost = 1.0) {
  const p = (t: number, c: number) => Math.round(((t + c) / (2 * t)) * 1000) / 10;
  return targets.map((t) => ({ target: t, raw: p(t, rawCost), standard: p(t, standardCost) }));
}

/** Pip value of the smallest trade (0.01 lot) on a USD-quoted pair, in dollars. */
export const MIN_LOT_PIP_VALUE = 0.1;
/** The risk is "precise" when the smallest trade is at most this share of it. */
export const SIZING_PRECISION = 0.2;

/** Smallest account on which a stop of `stop` pips still lets the risk per
 *  trade be sized to within SIZING_PRECISION, at the configured risk. */
export function capitalTable(riskPct: number, stops = [10, 30, 50, 100]) {
  const r = riskPct > 0 ? riskPct / 100 : 0.005;
  return stops.map((stop) => ({
    stop, minEquity: Math.ceil((stop * MIN_LOT_PIP_VALUE) / SIZING_PRECISION / r),
  }));
}

const num = (v: unknown): number | null =>
  typeof v === "number" && isFinite(v) ? v : null;

/** `/api/research/latest` -> what the Research page needs. */
export function researchFromVerdict(verdict: any): {
  research: ResearchSummary | null; gates: Gate[]; cpcvSharpes: number[];
} {
  if (!verdict || typeof verdict !== "object") {
    return { research: null, gates: [], cpcvSharpes: [] };
  }
  const ev = verdict.evidence ?? {};
  const th = verdict.thresholds ?? {};
  const cpcv = ev.cpcv ?? {};
  const sharpes: number[] = Array.isArray(cpcv.sharpes)
    ? cpcv.sharpes.filter((x: unknown) => typeof x === "number" && isFinite(x as number))
    : [];
  return {
    research: {
      run_id: String(verdict.run_id ?? ""), strategy: String(verdict.strategy ?? ""),
      created_ns: Number(verdict.created_at_ns ?? 0), accepted: verdict.accepted === true,
      data_label: String(verdict.data_label ?? ""), summary: String(verdict.summary ?? ""),
      current_config: typeof verdict.current_config === "boolean" ? verdict.current_config : null,
      effective_trials: num(verdict.effective_trials),
      pbo: num(ev.pbo?.pbo), pbo_max: num(th.pbo_max),
      dsr: num(ev.deflated_sharpe?.dsr), min_dsr: num(th.min_dsr),
      sr: num(ev.deflated_sharpe?.sr), sr_star: num(ev.deflated_sharpe?.sr_star),
      cpcv_positive_fraction: num(cpcv.positive_fraction)
        ?? (sharpes.length ? sharpes.filter((s) => s > 0).length / sharpes.length : null),
      cpcv_min_fraction: num(th.cpcv_positive_path_fraction),
    },
    gates: Array.isArray(verdict.gates) ? verdict.gates : [],
    cpcvSharpes: sharpes,
  };
}
