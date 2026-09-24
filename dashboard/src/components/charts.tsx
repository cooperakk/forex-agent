/**
 * Chart kit — hand-drawn SVG.
 *
 * Written by hand rather than pulled from a chart library so that the mark
 * specs are exactly right: 2px strokes, 4px rounded data-ends anchored to the
 * baseline, a 2px surface gap between adjacent fills, recessive grid and axes,
 * and a crosshair-plus-tooltip layer on every plot. Colour is spent only on
 * polarity and identity; everything structural is ink and gray.
 */
import React, { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";

/* ------------------------------------------------------------------ */
/* shared plumbing                                                     */
/* ------------------------------------------------------------------ */

export type Pt = { x: number; y: number; label?: string; meta?: Record<string, unknown> };

const PAD = { t: 12, r: 12, b: 24, l: 46 };

export function useSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState({ w: 640, h: 220 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      if (r.width > 0) setSize((s) => ({ ...s, w: Math.round(r.width) }));
    });
    ro.observe(el);
    const r = el.getBoundingClientRect();
    if (r.width > 0) setSize((s) => ({ ...s, w: Math.round(r.width) }));
    return () => ro.disconnect();
  }, []);
  return { ref, size };
}

type TipState = { x: number; y: number; title: string; rows: [string, string, string?][] } | null;

export function Tooltip({ tip }: { tip: TipState }) {
  if (!tip) return null;
  const style: React.CSSProperties = {
    left: Math.min(tip.x + 14, window.innerWidth - 300),
    top: Math.max(8, tip.y - 12),
  };
  return (
    <div className="tooltip" style={style} role="status">
      <div className="t-title">{tip.title}</div>
      {tip.rows.map(([k, v, color], i) => (
        <div className="t-row" key={i}>
          <span className="muted" style={{ display: "flex", alignItems: "center", gap: 5 }}>
            {color ? <i style={{ width: 8, height: 8, borderRadius: 2, background: color, display: "inline-block" }} /> : null}
            {k}
          </span>
          <span className="num">{v}</span>
        </div>
      ))}
    </div>
  );
}

function niceTicks(min: number, max: number, count = 4): number[] {
  if (!isFinite(min) || !isFinite(max)) return [0];
  if (min === max) return [min];
  const span = max - min;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const norm = step0 / mag;
  const step = (norm >= 7.5 ? 10 : norm >= 3.5 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
  const out: number[] = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(+v.toFixed(10));
  return out.length ? out : [min, max];
}

export function fmtNum(v: number, digits = 2): string {
  if (!isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (a >= 10000) return (v / 1000).toFixed(1) + "k";
  return v.toFixed(digits);
}

const GRID = "var(--hairline)";
const AXIS_TEXT = "var(--ink-faint)";

function Grid({ w, h, ticks, scaleY, fmt }: {
  w: number; h: number; ticks: number[]; scaleY: (v: number) => number;
  fmt: (v: number) => string;
}) {
  return (
    <g aria-hidden="true">
      {ticks.map((t, i) => (
        <g key={i}>
          <line x1={PAD.l} x2={w - PAD.r} y1={scaleY(t)} y2={scaleY(t)} stroke={GRID} strokeWidth={1} />
          <text x={PAD.l - 8} y={scaleY(t)} dy="0.32em" textAnchor="end"
                fontSize={10} fill={AXIS_TEXT} fontFamily="var(--mono)">{fmt(t)}</text>
        </g>
      ))}
    </g>
  );
}

/* ------------------------------------------------------------------ */
/* line / area                                                         */
/* ------------------------------------------------------------------ */

export type Series = { name: string; color: string; points: Pt[]; dashed?: boolean; area?: boolean };

export function LineChart({
  series, height = 230, yFmt = (v: number) => fmtNum(v, 0), xFmt = (x: number) => String(x),
  zeroBaseline = false, valueFmt, title,
}: {
  series: Series[]; height?: number; yFmt?: (v: number) => string;
  xFmt?: (x: number) => string; zeroBaseline?: boolean;
  valueFmt?: (v: number) => string; title?: string;
}) {
  const { ref, size } = useSize<HTMLDivElement>();
  const [tip, setTip] = useState<TipState>(null);
  const [hoverX, setHoverX] = useState<number | null>(null);
  const w = size.w;
  const h = height;

  const all = series.flatMap((s) => s.points);
  const xs = all.map((p) => p.x);
  const ys = all.map((p) => p.y);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  let yMin = Math.min(...ys), yMax = Math.max(...ys);
  if (zeroBaseline) yMin = Math.min(0, yMin);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const padY = (yMax - yMin) * 0.08;
  yMin -= padY; yMax += padY;

  const sx = (x: number) => PAD.l + ((x - xMin) / (xMax - xMin || 1)) * (w - PAD.l - PAD.r);
  const sy = (y: number) => PAD.t + (1 - (y - yMin) / (yMax - yMin || 1)) * (h - PAD.t - PAD.b);
  const ticks = niceTicks(yMin, yMax, 4);

  const path = (pts: Pt[]) =>
    pts.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(2)},${sy(p.y).toFixed(2)}`).join(" ");
  const areaPath = (pts: Pt[]) => {
    if (!pts.length) return "";
    const base = sy(Math.max(yMin, zeroBaseline ? 0 : yMin));
    return `${path(pts)} L${sx(pts[pts.length - 1].x).toFixed(2)},${base.toFixed(2)} L${sx(pts[0].x).toFixed(2)},${base.toFixed(2)} Z`;
  };

  const onMove = useCallback((e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const xVal = xMin + ((px - PAD.l) / (w - PAD.l - PAD.r || 1)) * (xMax - xMin);
    const base = series[0]?.points ?? [];
    if (!base.length) return;
    let idx = 0, best = Infinity;
    for (let i = 0; i < base.length; i++) {
      const d = Math.abs(base[i].x - xVal);
      if (d < best) { best = d; idx = i; }
    }
    setHoverX(sx(base[idx].x));
    setTip({
      x: e.clientX, y: e.clientY,
      title: base[idx].label ?? xFmt(base[idx].x),
      rows: series.map((s) => {
        const p = s.points[Math.min(idx, s.points.length - 1)];
        return [s.name, p ? (valueFmt ?? yFmt)(p.y) : "—", s.color] as [string, string, string];
      }),
    });
  }, [series, w, xMin, xMax, xFmt, yFmt, valueFmt]);

  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} role="img"
           aria-label={title ?? "نمودار روند"}
           onMouseMove={onMove} onMouseLeave={() => { setTip(null); setHoverX(null); }}>
        <Grid w={w} h={h} ticks={ticks} scaleY={sy} fmt={yFmt} />
        {zeroBaseline && yMin < 0 && yMax > 0 && (
          <line x1={PAD.l} x2={w - PAD.r} y1={sy(0)} y2={sy(0)}
                stroke="var(--hairline-strong)" strokeWidth={1} />
        )}
        {series.map((s, i) => (
          <g key={i}>
            {s.area && <path d={areaPath(s.points)} fill={s.color} opacity={0.1} />}
            <path d={path(s.points)} fill="none" stroke={s.color} strokeWidth={2}
                  strokeLinecap="round" strokeLinejoin="round"
                  strokeDasharray={s.dashed ? "4 4" : undefined} />
          </g>
        ))}
        {hoverX !== null && (
          <g>
            <line x1={hoverX} x2={hoverX} y1={PAD.t} y2={h - PAD.b}
                  stroke="var(--ink-faint)" strokeWidth={1} strokeDasharray="3 3" />
            {series.map((s, i) => {
              const p = s.points.find((pp) => Math.abs(sx(pp.x) - hoverX) < 1e-6)
                ?? s.points.reduce((a, b) => (Math.abs(sx(b.x) - hoverX) < Math.abs(sx(a.x) - hoverX) ? b : a));
              return <circle key={i} cx={sx(p.x)} cy={sy(p.y)} r={4}
                             fill={s.color} stroke="var(--paper)" strokeWidth={2} />;
            })}
          </g>
        )}
        <text x={PAD.l} y={h - 6} fontSize={10} fill={AXIS_TEXT} fontFamily="var(--mono)">
          {xFmt(xMin)}
        </text>
        <text x={w - PAD.r} y={h - 6} fontSize={10} fill={AXIS_TEXT} textAnchor="end"
              fontFamily="var(--mono)">{xFmt(xMax)}</text>
      </svg>
      <Tooltip tip={tip} />
      {series.length > 1 && (
        <div className="legend mt8">
          {series.map((s, i) => (
            <span key={i}><i style={{ background: s.color }} />{s.name}</span>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* bars                                                                */
/* ------------------------------------------------------------------ */

export type BarDatum = { label: string; value: number; color?: string; note?: string };

export function BarsH({ data, height, valueFmt = (v: number) => fmtNum(v, 0), max }: {
  data: BarDatum[]; height?: number; valueFmt?: (v: number) => string; max?: number;
}) {
  const [tip, setTip] = useState<TipState>(null);
  const hi = max ?? Math.max(1, ...data.map((d) => Math.abs(d.value)));
  return (
    <div className="stack gap8" style={{ maxHeight: height, overflowY: height ? "auto" : undefined }}>
      {data.map((d, i) => (
        <div key={i}
             onMouseMove={(e) => setTip({ x: e.clientX, y: e.clientY, title: d.label,
               rows: [["تعداد", valueFmt(d.value)], ...(d.note ? [["توضیح", d.note] as [string, string]] : [])] })}
             onMouseLeave={() => setTip(null)}>
          <div className="row gap8" style={{ justifyContent: "space-between" }}>
            <span className="fs12" style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{d.label}</span>
            <span className="num fs12 muted">{valueFmt(d.value)}</span>
          </div>
          <div className="meter" style={{ marginTop: 3 }}>
            <i style={{ width: `${Math.min(100, (Math.abs(d.value) / hi) * 100)}%`,
                        background: d.color ?? "var(--ink)" }} />
          </div>
        </div>
      ))}
      {!data.length && <div className="empty">هنوز داده‌ای برای نمایش نیست</div>}
      <Tooltip tip={tip} />
    </div>
  );
}

export function BarsV({ data, height = 190, valueFmt = (v: number) => fmtNum(v, 1),
                        zeroLine = true, labelEvery = 1 }: {
  data: BarDatum[]; height?: number; valueFmt?: (v: number) => string;
  zeroLine?: boolean; labelEvery?: number;
}) {
  const { ref, size } = useSize<HTMLDivElement>();
  const [tip, setTip] = useState<TipState>(null);
  const w = size.w, h = height;
  const vals = data.map((d) => d.value);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.1;
  lo -= pad; hi += pad;
  const sy = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * (h - PAD.t - PAD.b);
  const inner = w - PAD.l - PAD.r;
  // 2px surface gap between adjacent fills.
  const bw = Math.max(2, inner / Math.max(1, data.length) - 2);
  const ticks = niceTicks(lo, hi, 3);
  const zero = sy(0);
  const R = 4;

  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} role="img"
           aria-label="نمودار میله‌ای">
        <Grid w={w} h={h} ticks={ticks} scaleY={sy} fmt={valueFmt} />
        {zeroLine && <line x1={PAD.l} x2={w - PAD.r} y1={zero} y2={zero}
                           stroke="var(--hairline-strong)" strokeWidth={1} />}
        {data.map((d, i) => {
          const x = PAD.l + (i * inner) / Math.max(1, data.length) + 1;
          const y = d.value >= 0 ? sy(d.value) : zero;
          const bh = Math.max(1, Math.abs(zero - sy(d.value)));
          const color = d.color ?? (d.value >= 0 ? "var(--pos)" : "var(--neg)");
          // Rounded data-end only; the baseline end stays square.
          const r = Math.min(R, bh, bw / 2);
          const up = d.value >= 0;
          const path = up
            ? `M${x},${y + bh} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + bw - r},${y} Q${x + bw},${y} ${x + bw},${y + r} L${x + bw},${y + bh} Z`
            : `M${x},${y} L${x},${y + bh - r} Q${x},${y + bh} ${x + r},${y + bh} L${x + bw - r},${y + bh} Q${x + bw},${y + bh} ${x + bw},${y + bh - r} L${x + bw},${y} Z`;
          return (
            <path key={i} d={path} fill={color}
                  onMouseMove={(e) => setTip({ x: e.clientX, y: e.clientY, title: d.label,
                    rows: [["مقدار", valueFmt(d.value)]] })}
                  onMouseLeave={() => setTip(null)} />
          );
        })}
        {data.map((d, i) => (i % labelEvery === 0 ? (
          <text key={`l${i}`} x={PAD.l + (i * inner) / Math.max(1, data.length) + bw / 2 + 1}
                y={h - 8} fontSize={9.5} fill={AXIS_TEXT} textAnchor="middle">{d.label}</text>
        ) : null))}
      </svg>
      <Tooltip tip={tip} />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* diverging bars — polarity, two hues + neutral zero                   */
/* ------------------------------------------------------------------ */

export function DivergingBars({ data, valueFmt = (v: number) => fmtNum(v, 2),
                                posLabel = "مثبت", negLabel = "منفی" }: {
  data: BarDatum[]; valueFmt?: (v: number) => string; posLabel?: string; negLabel?: string;
}) {
  const [tip, setTip] = useState<TipState>(null);
  const hi = Math.max(1e-9, ...data.map((d) => Math.abs(d.value)));
  return (
    <div className="stack gap8">
      {data.map((d, i) => {
        const pct = (Math.abs(d.value) / hi) * 50;
        const positive = d.value >= 0;
        return (
          <div key={i}
               onMouseMove={(e) => setTip({ x: e.clientX, y: e.clientY, title: d.label,
                 rows: [["جمع خالص", valueFmt(d.value)], ...(d.note ? [["از این معامله‌ها", d.note] as [string, string]] : [])] })}
               onMouseLeave={() => setTip(null)}>
            <div className="row gap8" style={{ justifyContent: "space-between" }}>
              <span className="fs12 mono">{d.label}</span>
              <span className={`num fs12 ${positive ? "pos" : "neg"}`}>{valueFmt(d.value)}</span>
            </div>
            {/* Physical left/right on purpose: the bar is a number line, and
                positive-right / negative-left is read the same way in every
                locale. The label above it carries the direction in words. */}
            <div style={{ position: "relative", height: 8, marginTop: 3,
                          background: "var(--hairline)", borderRadius: 99,
                          direction: "ltr" }}>
              <div style={{ position: "absolute", left: "50%", top: 0, bottom: 0,
                            width: 1, background: "var(--hairline-strong)" }} />
              <div style={{
                position: "absolute", top: 0, bottom: 0, borderRadius: 99,
                background: positive ? "var(--s1)" : "var(--s8)",
                left: positive ? "50%" : `${50 - pct}%`, width: `${pct}%`,
              }} />
            </div>
          </div>
        );
      })}
      <div className="legend">
        <span><i style={{ background: "var(--s1)" }} />{posLabel}</span>
        <span><i style={{ background: "var(--s8)" }} />{negLabel}</span>
      </div>
      <Tooltip tip={tip} />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* histogram — R multiples, diverging around zero                       */
/* ------------------------------------------------------------------ */

export function Histogram({ values, bins = 17, height = 190, unit = "R" }: {
  values: number[]; bins?: number; height?: number; unit?: string;
}) {
  const data = useMemo(() => {
    if (!values.length) return [] as BarDatum[];
    const lo = Math.min(...values), hi = Math.max(...values);
    const span = hi - lo || 1;
    const step = span / bins;
    const counts = new Array(bins).fill(0);
    values.forEach((v) => {
      const i = Math.min(bins - 1, Math.max(0, Math.floor((v - lo) / step)));
      counts[i] += 1;
    });
    return counts.map((c, i) => {
      const centre = lo + step * (i + 0.5);
      return {
        label: centre.toFixed(1),
        value: c,
        color: centre >= 0 ? "var(--s1)" : "var(--s8)",
      } as BarDatum;
    });
  }, [values, bins]);
  if (!values.length) return <div className="empty">هنوز معامله‌ای بسته نشده است</div>;
  return (
    <>
      <BarsV data={data} height={height} valueFmt={(v) => String(Math.round(v))}
             zeroLine={false} labelEvery={3} />
      <div className="legend mt8">
        <span><i style={{ background: "var(--s8)" }} />معامله‌های زیان‌ده (زیر صفر)</span>
        <span><i style={{ background: "var(--s1)" }} />معامله‌های سودده (بالای صفر)</span>
        <span className="faint">
          عددهای زیر نمودار: نتیجه هر معامله بر حسب چند برابر مبلغ ریسک‌شده ({unit})
        </span>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* scatter — MAE against MFE                                            */
/* ------------------------------------------------------------------ */

export function Scatter({ points, height = 240, xLabel, yLabel }: {
  points: (Pt & { color?: string })[]; height?: number; xLabel: string; yLabel: string;
}) {
  const { ref, size } = useSize<HTMLDivElement>();
  const [tip, setTip] = useState<TipState>(null);
  const w = size.w, h = height;
  if (!points.length) return <div ref={ref} className="empty">هنوز داده‌ای برای نمایش نیست</div>;
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
  const xMin = Math.min(0, ...xs), xMax = Math.max(0.5, ...xs);
  const yMin = Math.min(0, ...ys), yMax = Math.max(0.5, ...ys);
  const sx = (x: number) => PAD.l + ((x - xMin) / (xMax - xMin || 1)) * (w - PAD.l - PAD.r);
  const sy = (y: number) => PAD.t + (1 - (y - yMin) / (yMax - yMin || 1)) * (h - PAD.t - PAD.b);
  const ticks = niceTicks(yMin, yMax, 4);
  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} role="img"
           aria-label={`${xLabel} در برابر ${yLabel}`}>
        <Grid w={w} h={h} ticks={ticks} scaleY={sy} fmt={(v) => v.toFixed(1)} />
        <line x1={sx(0)} x2={sx(0)} y1={PAD.t} y2={h - PAD.b}
              stroke="var(--hairline-strong)" strokeWidth={1} />
        {points.map((p, i) => (
          <circle key={i} cx={sx(p.x)} cy={sy(p.y)} r={4.5}
                  fill={p.color ?? "var(--s1)"} fillOpacity={0.75}
                  stroke="var(--paper)" strokeWidth={2}
                  onMouseMove={(e) => setTip({ x: e.clientX, y: e.clientY,
                    title: p.label ?? "معامله",
                    rows: [[xLabel, p.x.toFixed(2)], [yLabel, p.y.toFixed(2)]] })}
                  onMouseLeave={() => setTip(null)} />
        ))}
      </svg>
      {/* Axis names live in HTML, not in <text>: chart text is forced LTR with
          bidi-override so numerals and dates stay readable, and Persian words
          inside that override would come out reversed. */}
      <div className="legend mt8">
        <span>محور افقی: {xLabel}</span>
        <span>محور عمودی: {yLabel}</span>
      </div>
      <Tooltip tip={tip} />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* heatmap — sequential, one hue                                        */
/* ------------------------------------------------------------------ */

export function Heatmap({ rows, cols, cells, fmt = (v: number) => v.toFixed(1), diverging = true }: {
  rows: string[]; cols: string[]; cells: (number | null)[][];
  fmt?: (v: number) => string; diverging?: boolean;
}) {
  const [tip, setTip] = useState<TipState>(null);
  const flat = cells.flat().filter((v): v is number => v !== null && isFinite(v));
  const absMax = Math.max(1e-9, ...flat.map(Math.abs));
  const max = Math.max(1e-9, ...flat);
  const colour = (v: number) => {
    if (diverging) {
      const t = Math.min(1, Math.abs(v) / absMax);
      const hue = v >= 0 ? "var(--s1)" : "var(--s8)";
      return { background: hue, opacity: 0.12 + 0.78 * t };
    }
    const t = Math.min(1, v / max);
    return { background: "var(--seq-400)", opacity: 0.1 + 0.8 * t };
  };
  return (
    <div className="table-wrap">
      <table className="t" style={{ minWidth: 420 }}>
        <thead>
          <tr><th /> {cols.map((c) => <th key={c} className="n">{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, ri) => (
            <tr key={r}>
              <td className="mono fs12" style={{ fontWeight: 500 }}>{r}</td>
              {cols.map((c, ci) => {
                const v = cells[ri]?.[ci];
                return (
                  <td key={c} className="n" style={{ padding: 3 }}>
                    {v === null || v === undefined || !isFinite(v) ? (
                      <div style={{ height: 26, borderRadius: 6, background: "var(--surface-alt)" }} />
                    ) : (
                      <div
                        style={{ height: 26, borderRadius: 6, display: "flex",
                                 alignItems: "center", justifyContent: "center",
                                 fontSize: 11, color: "var(--ink)", position: "relative" }}
                        onMouseMove={(e) => setTip({ x: e.clientX, y: e.clientY,
                          title: `${r} · ${c}`, rows: [["مقدار", fmt(v)]] })}
                        onMouseLeave={() => setTip(null)}>
                        <span style={{ position: "absolute", inset: 0, borderRadius: 6, ...colour(v) }} />
                        <span style={{ position: "relative" }} className="num">{fmt(v)}</span>
                      </div>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <Tooltip tip={tip} />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* small pieces                                                         */
/* ------------------------------------------------------------------ */

export function Sparkline({ values, color = "var(--ink)", height = 34, fill = false }: {
  values: number[]; color?: string; height?: number; fill?: boolean;
}) {
  const { ref, size } = useSize<HTMLDivElement>();
  const w = size.w || 120;
  if (values.length < 2) return <div ref={ref} style={{ height }} />;
  const lo = Math.min(...values), hi = Math.max(...values);
  const sx = (i: number) => (i / (values.length - 1)) * w;
  const sy = (v: number) => 3 + (1 - (v - lo) / (hi - lo || 1)) * (height - 6);
  const d = values.map((v, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(v).toFixed(1)}`).join(" ");
  return (
    <div ref={ref} style={{ width: "100%" }}>
      <svg width="100%" height={height} viewBox={`0 0 ${w} ${height}`} aria-hidden="true">
        {fill && <path d={`${d} L${w},${height} L0,${height} Z`} fill={color} opacity={0.1} />}
        <path d={d} fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" />
      </svg>
    </div>
  );
}

export function Gauge({ value, max, label, danger, fmt = (v: number) => v.toFixed(1) }: {
  value: number; max: number; label: string; danger?: number; fmt?: (v: number) => string;
}) {
  const pct = Math.max(0, Math.min(1, value / (max || 1)));
  const over = danger !== undefined && value >= danger;
  const R = 34, C = Math.PI * R;
  return (
    <div className="stack" style={{ alignItems: "center", gap: 2 }}>
      <svg width="92" height="54" viewBox="0 0 92 54" role="img" aria-label={label}>
        <path d={`M12,48 A${R},${R} 0 0 1 80,48`} fill="none" stroke="var(--hairline)"
              strokeWidth={7} strokeLinecap="round" />
        <path d={`M12,48 A${R},${R} 0 0 1 80,48`} fill="none"
              stroke={over ? "var(--ember)" : "var(--ink)"} strokeWidth={7} strokeLinecap="round"
              strokeDasharray={`${(C * pct).toFixed(1)} ${C.toFixed(1)}`} />
        <text x="46" y="42" textAnchor="middle" fontSize="15" fontWeight="600"
              fill={over ? "var(--ember)" : "var(--ink)"} fontFamily="var(--mono)">{fmt(value)}</text>
      </svg>
      <div className="tile-label" style={{ textAlign: "center" }}>{label}</div>
      <div className="fs11 faint">سقف مجاز: <span className="num">{fmt(max)}</span></div>
    </div>
  );
}

export function StackedRatio({ parts, height = 10 }: {
  parts: { label: string; value: number; color: string }[]; height?: number;
}) {
  const total = parts.reduce((a, b) => a + b.value, 0) || 1;
  return (
    <div className="stack gap8">
      {/* 2px surface gap between adjacent fills */}
      <div className="row" style={{ gap: 2, height, borderRadius: 99, overflow: "hidden" }}>
        {parts.filter((p) => p.value > 0).map((p, i) => (
          <div key={i} title={`${p.label}: ${p.value}`}
               style={{ width: `${(p.value / total) * 100}%`, background: p.color }} />
        ))}
      </div>
      <div className="legend">
        {parts.map((p, i) => (
          <span key={i}><i style={{ background: p.color }} />{p.label} ({p.value})</span>
        ))}
      </div>
    </div>
  );
}
