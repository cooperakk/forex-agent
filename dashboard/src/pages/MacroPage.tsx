import React, { useCallback, useEffect, useState } from "react";
import type { Provider } from "../api";
import { LineChart } from "../components/charts";
import {
  Banner, Card, Chip, ConfirmWrite, Disclosure, Empty, Field, NumberField, Switch, Tile, ago, fa,
} from "../components/ui";
import type { CotRow, MacroConfig, MacroView } from "../types";

/* The dollar index and CFTC positioning. Like the TradingView and AI pages,
   the page opens with what these inputs may NOT do: "the robot reads the COT"
   is exactly the sentence that makes a newcomer think it trades on it. */

type Write = (path: string, body: unknown, totp: string) =>
  Promise<{ ok: boolean; detail: string }>;

const CCY_FA: Record<string, string> = {
  EUR: "یورو", JPY: "ین ژاپن", GBP: "پوند", CHF: "فرانک سوئیس", CAD: "دلار کانادا",
  AUD: "دلار استرالیا", NZD: "دلار نیوزیلند", MXN: "پزوی مکزیک", USD: "شاخص دلار",
  XAU: "طلا", XAG: "نقره",
};

function momFa(m: number | undefined, threshold: number) {
  if (m === undefined || m === null) return { label: "نامشخص", tone: "flat" as const };
  if (m >= threshold) return { label: "دلار با قدرت بالا می‌رود", tone: "warn" as const };
  if (m <= -threshold) return { label: "دلار با قدرت پایین می‌آید", tone: "warn" as const };
  return { label: "حرکت معناداری ندارد", tone: "flat" as const };
}

/* Where speculators stand on a 0..100 scale, with the crowded zones marked.
   The position is also printed and named in a chip beside it: never colour
   alone. */
function PositionMeter({ index, extreme }: { index?: number; extreme: number }) {
  const w = 150, h = 18;
  const x = (v: number) => 4 + (v / 100) * (w - 8);
  const lo = 100 - extreme;
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" style={{ direction: "ltr" }}
         aria-label={index === undefined ? "نامشخص" : `شاخص ${index.toFixed(0)} از ۱۰۰`}>
      <rect x={4} y={7} width={w - 8} height={4} rx={2} fill="var(--hairline)" />
      <rect x={4} y={7} width={x(lo) - 4} height={4} rx={2} fill="var(--warn-soft)" />
      <rect x={x(extreme)} y={7} width={w - 4 - x(extreme)} height={4} rx={2}
            fill="var(--warn-soft)" />
      <line x1={x(50)} x2={x(50)} y1={4} y2={14} stroke="var(--ink-faint)" strokeWidth={1} />
      {index !== undefined && (
        <circle cx={x(index)} cy={9} r={5} fill="var(--ink)" stroke="var(--paper)"
                strokeWidth={2} />
      )}
    </svg>
  );
}

export default function MacroPage({ provider, write, readOnly, canAdminister }: {
  provider: Provider; write: Write; readOnly: boolean; canAdminister: boolean;
}) {
  const [view, setView] = useState<MacroView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string>("");
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown;
    after?: (detail: string) => void;
  }>(null);

  const load = useCallback(async () => {
    try {
      setView(await provider.get<MacroView>("/api/macro"));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [provider]);

  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);

  if (error) return <Banner tone="neg" icon="✕">داده‌های این صفحه بارگذاری نشد: {error}</Banner>;
  if (!view) return <Empty>در حال بارگذاری…</Empty>;
  if (!view.available) return <Banner tone="warn" icon="◈">این بخش در این نسخه فعال نیست.</Banner>;

  const cfg = view.config!;
  const dxy = view.dxy!;
  const cot = view.cot!;
  const mom = momFa(dxy.state?.dxy_mom, cfg.dxy_headwind_score);
  const disabled = readOnly || !canAdminister;
  const rows = cot.rows ?? [];
  const known = rows.filter((r) => r.index !== undefined);

  return (
    <div className="stack gap16">
      <Banner tone="info" icon="🌐">
        <strong>این صفحه دو نگاه از بیرون نمودار را نشان می‌دهد:</strong> قدرت خود دلار آمریکا
        (شاخص دلار، DXY) و اینکه صندوق‌ها و معامله‌گران بزرگ در بازار آتی ارز کدام طرف ایستاده‌اند
        (گزارش COT). <strong>هیچ‌کدام معامله باز نمی‌کنند و حجم را بزرگ نمی‌کنند.</strong> فقط
        دو کار می‌کنند: (۱) کنار هر سیگنال ثبت می‌شوند تا «حافظهٔ موقعیت‌های مشابه» و «فیلتر دوم»
        از آن‌ها یاد بگیرند؛ (۲) اگر معامله هم‌جهت با یک موقعیت خیلی شلوغ باشد، یا دلار با قدرت
        خلافش حرکت کند، حجمش کمی کوچک‌تر می‌شود. صفحهٔ «مغز ربات» اندازه می‌گیرد این کار واقعاً
        کمک کرده یا نه.
      </Banner>

      <Card title="شاخص دلار آمریکا (DXY)"
            hint="از شش جفت‌ارز خود بروکر با فرمول رسمی ICE ساخته می‌شود؛ همان ساعت و همان داده‌ای که ربات با آن معامله می‌کند.">
        {!dxy.available ? (
          <Banner tone="warn" icon="!">
            شاخص دلار هنوز ساخته نشده: {dxy.error || "هنوز قیمتی نرسیده"}.
            {dxy.components_on_server.length > 0 && <> جفت‌های موجود روی سرور:{" "}
              <span className="ltr mono fs12">{dxy.components_on_server.join(", ")}</span>.</>}
          </Banner>
        ) : (
          <div className="stack gap12">
            <div className="grid g3">
              <Tile label="مقدار فعلی" value={<span className="num">{dxy.last?.toFixed(2)}</span>}
                    note={dxy.complete ? "برابر فرمول رسمی" : "تقریبی (یک جزء روی سرور نیست)"}
                    hint="اگر همهٔ شش جفت روی سرور باشند، عدد با شاخص رسمی یکی است؛ وگرنه فقط جهت و سرعت حرکتش معتبر است." />
              <Tile label="قدرت حرکت اخیر" sub
                    value={<Chip tone={mom.tone}>{mom.label}</Chip>}
                    note={<span className="ltr mono">
                      {dxy.state?.dxy_mom !== undefined ? `${dxy.state.dxy_mom >= 0 ? "+" : ""}${dxy.state.dxy_mom.toFixed(2)}` : "—"}
                    </span>}
                    hint="حرکت ۲۰ کندل اخیر تقسیم بر نوسان عادی دلار. بالای ۲ یا زیر منفی ۲ یعنی حرکت معنادار است، نه نویز." />
              <Tile label="فاصله از میانگین ۵۰ کندل" sub
                    value={<span className="num">{dxy.state?.dxy_z !== undefined
                      ? `${dxy.state.dxy_z >= 0 ? "+" : ""}${dxy.state.dxy_z.toFixed(2)}` : "—"}</span>}
                    note="بر حسب انحراف معیار" />
            </div>
            {dxy.points && dxy.points.length > 2 && (
              <LineChart height={220} title="شاخص دلار"
                         series={[{ name: "DXY", color: "var(--info)",
                                    points: dxy.points.map(([t, v]) => ({ x: t, y: v })) }]}
                         yFmt={(v) => v.toFixed(2)} valueFmt={(v) => v.toFixed(3)}
                         xFmt={(x) => new Date(x).toISOString().slice(5, 16).replace("T", " ")} />
            )}
            <div className="row gap6 wrap fs12">
              <span className="muted">اجزای استفاده‌شده:</span>
              {(dxy.used ?? []).map((u) => <Chip key={u}><span className="ltr mono">{u}</span></Chip>)}
              {(dxy.missing ?? []).map((u) => (
                <Chip key={u} tone="warn" title="این جفت روی سرور بروکر شما نیست">
                  <span className="ltr mono">{u}</span> ندارد</Chip>
              ))}
            </div>
          </div>
        )}
        <Disclosure summary="فرمول و معنی">
          <p className="fs12" style={{ lineHeight: 1.9 }}>
            <span className="ltr mono">DXY = 50.14348112 × EURUSD^-0.576 × USDJPY^0.136 × GBPUSD^-0.119 ×
              USDCAD^0.091 × USDSEK^0.042 × USDCHF^0.036</span>
            <br />یورو بیشترین وزن (۵۷٫۶٪) را دارد. خرید EURUSD یعنی فروش دلار؛ خرید USDJPY یعنی
            خرید دلار؛ خرید طلا یعنی فروش دلار. «باد مخالف دلار» یعنی معامله خلاف جهت یک حرکت
            قوی و معنادار دلار است — آن‌وقت حجم به ضریب {fa(cfg.dxy_headwind_multiplier)} کم می‌شود.
          </p>
        </Disclosure>
      </Card>

      <Card title="موقعیت معامله‌گران بزرگ (گزارش COT)"
            hint="هر جمعه کمیسیون معاملات آتی آمریکا (CFTC) منتشر می‌کند که صندوق‌ها و سفته‌بازان بزرگ روز سه‌شنبه در بازار آتی هر ارز چقدر خرید و فروش داشته‌اند."
            actions={<button className="btn ghost sm" disabled={disabled || cot.running}
                             onClick={() => setConfirm({
                               action: "دریافت تازهٔ گزارش COT",
                               description: "گزارش‌ها از سرور رسمی CFTC خوانده می‌شوند. روی معامله‌های باز اثری ندارد.",
                               path: "/api/macro/cot/refresh", body: {},
                               after: (detail) => {
                                 try {
                                   const r = JSON.parse(detail);
                                   setResult(r.ok ? `دریافت شد؛ تازه‌ترین گزارش ${r.latest ?? "—"}`
                                                  : `ناموفق: ${r.error}`);
                                 } catch { setResult(""); }
                               },
                             })}>{cot.running ? "در حال دریافت…" : "دریافت تازه"}</button>}>
        <div className="row gap12 wrap fs12 muted" style={{ marginBottom: 8 }}>
          <span>تازه‌ترین گزارش: <span className="ltr mono">{cot.latest ?? "—"}</span></span>
          <span>آخرین دریافت: {cot.last_fetch_ns ? ago(cot.last_fetch_ns) : "هنوز نه"}</span>
          <span>ردیف‌های ذخیره‌شده: {fa(cot.stored)}</span>
          {result && <span>{result}</span>}
        </div>
        {cot.error && (
          <Banner tone="warn" icon="!">
            دریافت از CFTC ناموفق بود: <span className="ltr mono fs12">{cot.error}</span>
            <br />اگر سرور به <span className="ltr">publicreporting.cftc.gov</span> دسترسی ندارد، فایل‌های
            سالانه را در مرورگر از سایت CFTC بگیرید و روی سرور وارد کنید:
            <span className="ltr mono fs12"> python scripts/cot_data.py --import deacot2025.zip</span>
          </Banner>
        )}
        {known.length === 0 ? (
          <Empty>هنوز گزارشی نیست (برای شاخص، حداقل {fa(cfg.cot_min_weeks)} هفته سابقه لازم است)</Empty>
        ) : (
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr><th>ارز</th><th>کجا ایستاده‌اند (۰ = بیشترین فروش، ۱۰۰ = بیشترین خرید)</th>
                  <th className="n">شاخص</th><th className="n">تغییر هفته</th>
                  <th className="n">خالص (٪ کل قراردادها)</th><th>وضعیت</th><th>تاریخ</th></tr>
              </thead>
              <tbody>
                {rows.map((r: CotRow) => (
                  <tr key={r.currency}>
                    <td>{CCY_FA[r.currency] ?? r.currency}
                      <span className="ltr mono fs11 faint"> {r.currency}</span></td>
                    <td><PositionMeter index={r.index} extreme={cfg.cot_extreme} /></td>
                    <td className="n num">{r.index === undefined ? "—" : r.index.toFixed(0)}</td>
                    <td className="n num">{r.change === undefined ? "—"
                      : `${r.change >= 0 ? "+" : ""}${r.change.toFixed(1)}`}</td>
                    <td className="n num">{r.net_pct_oi === undefined ? "—"
                      : `${r.net_pct_oi >= 0 ? "+" : ""}${r.net_pct_oi.toFixed(1)}%`}</td>
                    <td>{r.index === undefined
                      ? <Chip title={`${r.weeks} هفته سابقه`}>دادهٔ کم</Chip>
                      : r.crowded_long ? <Chip tone="warn">خریدِ شلوغ</Chip>
                      : r.crowded_short ? <Chip tone="warn">فروشِ شلوغ</Chip>
                      : <Chip>عادی</Chip>}</td>
                    <td className="ltr mono fs11">{r.report_date ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Disclosure summary="این اعداد را چطور بخوانم؟">
          <ul className="fs12" style={{ lineHeight: 1.9 }}>
            <li><strong>شاخص</strong> موقعیت خالص سفته‌بازان را با {fa(cfg.cot_lookback_weeks)} هفتهٔ
              گذشته مقایسه می‌کند: ۱۰۰ یعنی بیشترین خرید در این مدت، ۰ یعنی بیشترین فروش.</li>
            <li><strong>شلوغ</strong> یعنی شاخص بالای {fa(cfg.cot_extreme)} یا زیر {fa(100 - cfg.cot_extreme)}.
              پژوهش‌ها (Brunnermeier و همکاران ۲۰۰۸) نشان می‌دهند موقعیت‌های شلوغ با خطر سقوط ناگهانی
              همراه‌اند؛ برای همین معاملهٔ هم‌جهت با آن‌ها کوچک‌تر گرفته می‌شود.</li>
            <li>COT <strong>جهت را پیش‌بینی نمی‌کند</strong>؛ بیشتر دنبال قیمت می‌رود. ربات با آن
              خرید و فروش نمی‌کند، فقط احتیاط می‌کند.</li>
            <li>گزارش مال سه‌شنبه است ولی جمعه منتشر می‌شود؛ ربات هر گزارش را فقط از جمعه به بعد
              به کار می‌برد، هم در معامله و هم در آزمون‌های گذشته.</li>
          </ul>
        </Disclosure>
      </Card>

      <MacroSettings config={cfg} disabled={disabled}
                     onSave={(patch) => setConfirm({
                       action: "ذخیرهٔ تنظیمات دلار و COT",
                       description: "از چرخهٔ بعدی ربات اعمال می‌شود و در دفتر رویدادها ثبت می‌شود. هیچ‌کدام نمی‌تواند حجم را بزرگ‌تر کند.",
                       path: "/api/macro/settings", body: { patch },
                     })} />

      <ConfirmWrite open={!!confirm} action={confirm?.action ?? ""}
                    description={confirm?.description}
                    onClose={() => setConfirm(null)}
                    onConfirm={async (totp) => {
                      if (!confirm) return { ok: false, detail: "" };
                      const res = await write(confirm.path, confirm.body, totp);
                      if (res.ok) {
                        confirm.after?.(res.detail);
                        await load();
                      }
                      return res;
                    }} />
    </div>
  );
}

function MacroSettings({ config, disabled, onSave }: {
  config: MacroConfig; disabled: boolean; onSave: (patch: Partial<MacroConfig>) => void;
}) {
  const [c, setC] = useState<MacroConfig>(config);
  const set = <K extends keyof MacroConfig>(k: K, v: MacroConfig[K]) => setC({ ...c, [k]: v });
  const sw = (k: keyof MacroConfig, label: string, help: React.ReactNode) => (
    <Field label={label} help={help}>
      <Switch checked={Boolean(c[k])} disabled={disabled} label={label}
              onChange={(v) => set(k, v as any)} />
    </Field>
  );
  return (
    <Card title="تنظیمات" hint="فقط مالک حساب، با کد دومرحله‌ای، می‌تواند این‌ها را تغییر دهد.">
      <div className="stack gap12">
        {sw("enabled", "دلار و COT", "خاموش: هیچ اثری روی ربات ندارد و چیزی ثبت نمی‌شود.")}
        <strong className="fs13">شاخص دلار</strong>
        {sw("dxy_enabled", "ساختن شاخص دلار", "شش جفت‌ارز دلاری از بروکر خوانده می‌شود.")}
        {sw("dxy_headwind_enabled", "کم کردن حجم در «باد مخالف دلار»",
            "وقتی دلار با قدرت خلاف جهت معامله حرکت می‌کند.")}
        <Field label="چه حرکتی «قوی» است" help="حرکت ۲۰ کندل بر حسب نوسان عادی. پیش‌فرض ۲.">
          <NumberField value={c.dxy_headwind_score} min={0.5} max={6} step={0.25}
                       disabled={disabled} onChange={(v) => set("dxy_headwind_score", v)} />
        </Field>
        <Field label="ضریب حجم در باد مخالف" help="۰٫۷۵ یعنی سه‌چهارم حجم عادی.">
          <NumberField value={c.dxy_headwind_multiplier} min={0} max={1} step={0.05}
                       disabled={disabled} onChange={(v) => set("dxy_headwind_multiplier", v)} />
        </Field>
        <strong className="fs13">گزارش COT</strong>
        {sw("cot_enabled", "خواندن گزارش COT", "هر چند ساعت یک بار از سرور رسمی CFTC.")}
        {sw("cot_crowding_enabled", "کم کردن حجم در موقعیت شلوغ",
            "وقتی معامله هم‌جهت با یک موقعیت افراطی سفته‌بازان است.")}
        <Field label="مرز «شلوغ»" help="شاخص بالای این عدد (یا زیر ۱۰۰ منهای آن). پیش‌فرض ۹۰.">
          <NumberField value={c.cot_extreme} min={60} max={100} step={1} disabled={disabled}
                       onChange={(v) => set("cot_extreme", v)} />
        </Field>
        <Field label="ضریب حجم در موقعیت شلوغ">
          <NumberField value={c.cot_crowding_multiplier} min={0} max={1} step={0.05}
                       disabled={disabled} onChange={(v) => set("cot_crowding_multiplier", v)} />
        </Field>
        <Field label="بازهٔ مقایسه" help="۱۵۶ هفته یعنی سه سال.">
          <NumberField value={c.cot_lookback_weeks} min={26} max={520} step={1} suffix="هفته"
                       disabled={disabled} onChange={(v) => set("cot_lookback_weeks", v)} />
        </Field>
        <div className="row gap8">
          <button className="btn" disabled={disabled} onClick={() => onSave(c)}>ذخیره</button>
        </div>
      </div>
    </Card>
  );
}
