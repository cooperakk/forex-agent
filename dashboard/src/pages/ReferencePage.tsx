import React, { useCallback, useEffect, useState } from "react";
import type { Provider } from "../api";
import {
  Banner, Card, Chip, ConfirmWrite, Disclosure, Empty, Field, KV, NumberField, Switch, ago,
} from "../components/ui";
import type { ReferenceCheck, ReferenceConfig, ReferenceView, TARating } from "../types";

/* The independent reference price. The page opens with what TradingView is
   NOT allowed to do, for the same reason the AI page does: "the robot reads
   TradingView" is exactly the sentence that makes a newcomer believe the
   ratings pick the trades. */

type Write = (path: string, body: unknown, totp: string) =>
  Promise<{ ok: boolean; detail: string }>;

const STATUS_FA: Record<ReferenceCheck["status"], { label: string; tone: any; help: string }> = {
  ok: { label: "هم‌خوان", tone: "pos",
        help: "قیمت بروکر با قیمت مرجع یکی است؛ هیچ اثری روی معامله ندارد." },
  shrink: { label: "حجم کم شد", tone: "warn",
            help: "اختلاف از حد هشدار بیشتر است؛ حجم معامله تازه کوچک می‌شود." },
  block: { label: "مسدود", tone: "neg",
           help: "اختلاف خیلی زیاد است؛ تا برطرف شدن، معامله تازه روی این نماد باز نمی‌شود." },
  stale: { label: "مرجع قدیمی", tone: "flat",
           help: "قیمت مرجع مدتی تغییر نکرده؛ مقایسه انجام نمی‌شود و اثری ندارد." },
  delayed: { label: "مرجع با تأخیر", tone: "flat",
             help: "TradingView این نماد را با تأخیر می‌دهد؛ برای مقایسه استفاده نمی‌شود." },
  closed: { label: "بازار بسته", tone: "flat", help: "بازار این نماد الان بسته است." },
  unavailable: { label: "در دسترس نیست", tone: "flat",
                 help: "هنوز قیمتی از TradingView نرسیده؛ اثری ندارد." },
  unmapped: { label: "نماد تعریف نشده", tone: "warn",
              help: "برای این نماد، نماد TradingView مشخص نیست." },
  no_broker_quote: { label: "قیمت بروکر نیست", tone: "flat",
                     help: "قیمت تازه‌ای از بروکر نیست؛ موتور ریسک خودش جلوی معامله را می‌گیرد." },
};

const RATING_FA: Record<string, { label: string; tone: any }> = {
  strong_sell: { label: "فروش قوی", tone: "neg" }, sell: { label: "فروش", tone: "neg" },
  neutral: { label: "خنثی", tone: "flat" }, buy: { label: "خرید", tone: "pos" },
  strong_buy: { label: "خرید قوی", tone: "pos" }, unknown: { label: "—", tone: "flat" },
};

const TF_FA: [string, string][] = [["15", "۱۵ دقیقه"], ["60", "۱ ساعت"], ["240", "۴ ساعت"],
  ["1D", "روزانه"], ["1W", "هفتگی"]];

function num(v: number | null | undefined, digits = 5) {
  return v === null || v === undefined ? "—" : Number(v).toFixed(digits);
}

function Spark({ values, limit }: { values: number[]; limit: number | null }) {
  if (!values || values.length < 2) return <span className="fs11 faint">—</span>;
  const w = 120, h = 28;
  const top = Math.max(limit ?? 0, ...values.map(Math.abs), 1);
  const x = (i: number) => (i / (values.length - 1)) * w;
  const y = (v: number) => h / 2 - (v / top) * (h / 2 - 2);
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img"
         aria-label="اختلاف قیمت در بررسی‌های اخیر" style={{ direction: "ltr" }}>
      <line x1={0} x2={w} y1={h / 2} y2={h / 2} stroke="currentColor" strokeOpacity={0.2} />
      {limit ? <>
        <line x1={0} x2={w} y1={y(limit)} y2={y(limit)} stroke="var(--neg)" strokeOpacity={0.4}
              strokeDasharray="3 3" />
        <line x1={0} x2={w} y1={y(-limit)} y2={y(-limit)} stroke="var(--neg)" strokeOpacity={0.4}
              strokeDasharray="3 3" />
      </> : null}
      <path d={d} fill="none" stroke="var(--info)" strokeWidth={1.5} />
    </svg>
  );
}

export default function ReferencePage({ provider, write, readOnly, canAdminister }: {
  provider: Provider; write: Write; readOnly: boolean; canAdminister: boolean;
}) {
  const [view, setView] = useState<ReferenceView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown;
  }>(null);

  const load = useCallback(async () => {
    try {
      setView(await provider.get<ReferenceView>("/api/reference"));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [provider]);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  if (error) return <Banner tone="neg" icon="✕">داده‌های این صفحه بارگذاری نشد: {error}</Banner>;
  if (!view) return <Empty>در حال بارگذاری…</Empty>;
  if (!view.available) {
    return <Banner tone="warn" icon="◈">قیمت مرجع در این نسخه فعال نیست. {view.reason}</Banner>;
  }
  const stream = view.stream;
  const checks = view.checks ?? [];
  const blocked = checks.filter((c) => c.status === "block");

  return (
    <div className="stack gap16">
      <Banner tone="info" icon="⚖">
        <strong>TradingView اینجا «نظر دوم» درباره قیمت است، نه منبع معامله.</strong> ربات
        همیشه با قیمت خود بروکر معامله می‌کند. این بخش فقط قیمت بروکر را با یک منبع مستقل
        مقایسه می‌کند تا اگر قیمت بروکر خراب بود (قیمت یخ‌زده، تیک اشتباه، نماد اشتباه)
        جلوی معامله تازه را بگیرد یا حجمش را کم کند. هیچ‌وقت معامله باز نمی‌کند، حجم را بزرگ
        نمی‌کند و به معامله‌های باز دست نمی‌زند. اگر TradingView در دسترس نباشد، ربات دقیقاً
        مثل قبل کار می‌کند.
      </Banner>
      <Banner tone="warn" icon="!">
        این اتصال <strong>غیررسمی</strong> است (همان روشی که کتابخانه متن‌باز TradingView-API
        استفاده می‌کند). شرایط استفاده TradingView دسترسی خودکار را محدود می‌کند و ممکن است
        هر زمان بدون اطلاع از کار بیفتد؛ به همین دلیل به‌طور پیش‌فرض خاموش است و بدون حساب
        کاربری کار می‌کند. تصمیم روشن کردنش با شماست.
      </Banner>

      {blocked.length > 0 && (
        <Banner tone="neg" icon="⛔">
          قیمت بروکر برای {blocked.map((c) => c.instrument).join("، ")} با بازار نمی‌خواند؛
          معامله تازه روی {blocked.length > 1 ? "این نمادها" : "این نماد"} باز نمی‌شود تا
          اختلاف برطرف شود. اگر این اختلاف همیشگی است، احتمالاً نماد مرجع درست انتخاب نشده.
        </Banner>
      )}

      <Card title="وضعیت اتصال"
            hint="اتصال فقط‌خواندنی به سرور داده TradingView، بدون نام کاربری و رمز."
            actions={
              <button className="btn ghost sm" disabled={readOnly || !view.enabled}
                      onClick={() => setConfirm({
                        action: "به‌روزرسانی قیمت مرجع",
                        description: "فهرست نمادها دوباره بررسی و امتیازهای تحلیل تکنیکال تازه خوانده می‌شوند.",
                        path: "/api/reference/refresh", body: {},
                      })}>به‌روزرسانی</button>
            }>
        <div className="kv-grid c3">
          <KV k="وضعیت" v={view.enabled
            ? <Chip tone={stream?.connected ? "pos" : "warn"}>
                {stream?.connected ? "روشن و وصل" : "روشن، در حال اتصال"}</Chip>
            : <Chip>خاموش</Chip>} />
          <KV k="آخرین داده" v={stream?.last_message_ns ? ago(stream.last_message_ns) : "هنوز نه"} />
          <KV k="نمادهای دنبال‌شده" v={stream?.symbols?.length ?? 0} />
          <KV k="تعداد اتصال دوباره" v={stream?.reconnects ?? 0} />
          <KV k="بسته‌های نامعتبر ردشده" v={stream?.dropped_packets ?? 0} />
          <KV k="امتیازهای تکنیکال" v={view.ta_last_ns ? ago(view.ta_last_ns) : "هنوز نه"} />
        </div>
        {stream?.last_error && (
          <div style={{ marginTop: 12 }}>
            <Banner tone="warn" icon="⚠">
              آخرین خطا ({stream.last_error_ns ? ago(stream.last_error_ns) : ""}):{" "}
              <span className="ltr mono fs12">{stream.last_error}</span>
              {stream.last_error.includes("403") &&
                <> — دسترسی سرور به <span className="ltr">data.tradingview.com</span> بسته است
                  (فایروال یا پروکسی).</>}
            </Banner>
          </div>
        )}
        {view.ta_error && (
          <p className="fs12 muted" style={{ marginTop: 8 }}>
            امتیازهای تکنیکال خوانده نشد: <span className="ltr">{view.ta_error}</span>
          </p>
        )}
      </Card>

      <Card title="مقایسه قیمت بروکر با بازار"
            hint={<>اختلاف بر حسب «واحد پایه» (bp، یک‌صدم درصد) اندازه گرفته می‌شود تا برای
              یورو/دلار، ین و طلا یکسان کار کند. خط‌چین قرمز در نمودار کوچک، مرز مسدود شدن است.</>}>
        {checks.length === 0 ? (
          <Empty>{view.enabled
            ? "هنوز بررسی‌ای انجام نشده؛ در چرخه بعدی ربات انجام می‌شود"
            : "قیمت مرجع خاموش است"}</Empty>
        ) : (
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr><th>نماد</th><th>مرجع</th><th className="n">قیمت بروکر</th>
                  <th className="n">قیمت مرجع</th><th className="n">اختلاف (bp)</th>
                  <th className="n">اختلاف (پیپ)</th><th className="n">مرز مسدود</th>
                  <th>وضعیت</th><th>روند اختلاف</th><th className="n">میانه اختلاف</th></tr>
              </thead>
              <tbody>
                {checks.map((c) => {
                  const st = STATUS_FA[c.status] ?? STATUS_FA.unavailable;
                  return (
                    <tr key={c.instrument}>
                      <td className="mono fs12">{c.instrument}</td>
                      <td className="mono fs11 ltr">{c.symbol || "—"}</td>
                      <td className="n mono">{num(c.broker_mid)}</td>
                      <td className="n mono">{num(c.reference_mid)}</td>
                      <td className="n mono">{c.divergence_bp === null ? "—" : c.divergence_bp.toFixed(1)}</td>
                      <td className="n mono">{c.divergence_pips === null ? "—" : c.divergence_pips.toFixed(1)}</td>
                      <td className="n mono">{c.block_at_bp === null ? "—" : c.block_at_bp.toFixed(1)}</td>
                      <td><Chip tone={st.tone} title={`${st.help}\n${c.reason}`}>{st.label}</Chip></td>
                      <td><Spark values={c.recent_gap_bp} limit={c.block_at_bp} /></td>
                      <td className="n mono" title="اختلاف ثابت (نه نوسانی) معمولاً یعنی نماد مرجع با قرارداد بروکر فرق دارد">
                        {c.median_gap_bp === null ? "—" : c.median_gap_bp.toFixed(1)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <Disclosure summary="این اعداد را چطور بخوانم؟">
          <ul className="fs12" style={{ lineHeight: 1.9 }}>
            <li><strong>هم‌خوان:</strong> همه چیز عادی است. دو منبع مستقل فارکس معمولاً کمتر از
              یک پیپ با هم فرق دارند.</li>
            <li><strong>حجم کم شد:</strong> اختلاف از مرز هشدار گذشته؛ معامله تازه با نصف حجم
              (قابل تنظیم) باز می‌شود.</li>
            <li><strong>مسدود:</strong> اختلاف آن‌قدر زیاد است که احتمالاً قیمت بروکر خراب است؛
              معامله تازه باز نمی‌شود. معامله‌های باز و حد ضررشان دست‌نخورده می‌مانند.</li>
            <li><strong>میانه اختلاف:</strong> اگر همیشه یک عدد ثابت غیرصفر است (مثلاً طلا
              روی قرارداد آتی)، نماد مرجع دیگری انتخاب کنید؛ مرزها را بی‌دلیل بزرگ نکنید.</li>
          </ul>
        </Disclosure>
      </Card>

      <Card title="امتیاز تحلیل تکنیکال TradingView"
            hint={<>خلاصه ۲۶ اندیکاتور کلاسیک (میانگین‌های متحرک و نوسان‌نماها) از خود TradingView.
              <strong> فقط برای اطلاع شماست؛ ربات بر اساس آن معامله نمی‌کند</strong>، چون هیچ
              مدرکی از سودآوری‌اش ندارد و هر سیگنالی فقط از راه آزمون پذیرش به پول واقعی می‌رسد.</>}>
        {!view.ta || Object.keys(view.ta).length === 0 ? (
          <Empty>{view.config?.ta_ratings === false ? "این بخش خاموش است"
            : "هنوز امتیازی خوانده نشده"}</Empty>
        ) : (
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr><th>نماد</th>{TF_FA.map(([k, label]) => <th key={k}>{label}</th>)}</tr>
              </thead>
              <tbody>
                {Object.entries(view.ta).map(([inst, per]) => (
                  <tr key={inst}>
                    <td className="mono fs12">{inst}</td>
                    {TF_FA.map(([k]) => {
                      const r: TARating | undefined = per[k];
                      const lab = RATING_FA[r?.label ?? "unknown"] ?? RATING_FA.unknown;
                      return (
                        <td key={k}>
                          <Chip tone={lab.tone}
                                title={r ? `کل: ${r.all ?? "—"} · میانگین‌ها: ${r.ma ?? "—"} · نوسان‌نماها: ${r.other ?? "—"}` : ""}>
                            {lab.label}</Chip>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {view.config && (
        <SettingsCard config={view.config} mapping={view.mapping ?? {}} provider={provider}
                      disabled={readOnly || !canAdminister}
                      onSave={(body) => setConfirm({
                        action: "ذخیره تنظیمات قیمت مرجع",
                        description: <>تنظیمات مقایسه قیمت ذخیره می‌شود و از چرخه بعدی ربات اعمال
                          می‌شود. این تغییر در دفتر رویدادها ثبت می‌شود.
                          {(body as any).enabled ? <> روشن کردن این بخش یعنی سرور به
                            <span className="ltr"> data.tradingview.com </span> وصل می‌شود.</> : null}</>,
                        path: "/api/reference/settings", body,
                      })} />
      )}

      <ConfirmWrite open={!!confirm} action={confirm?.action ?? ""}
                    description={confirm?.description}
                    onClose={() => setConfirm(null)}
                    onConfirm={async (totp) => {
                      if (!confirm) return { ok: false, detail: "" };
                      const res = await write(confirm.path, confirm.body, totp);
                      if (res.ok) await load();
                      return res;
                    }} />
    </div>
  );
}

function SettingsCard({ config, mapping, provider, disabled, onSave }: {
  config: ReferenceConfig; mapping: Record<string, string>; provider: Provider;
  disabled: boolean; onSave: (body: Record<string, unknown>) => void;
}) {
  const [enabled, setEnabled] = useState(config.enabled);
  const [exchange, setExchange] = useState(config.exchange);
  const [shrink, setShrink] = useState(config.shrink_bp);
  const [block, setBlock] = useState(config.block_bp);
  const [mult, setMult] = useState(config.shrink_multiplier);
  const [maxAge, setMaxAge] = useState(config.max_age_sec);
  const [ta, setTa] = useState(config.ta_ratings);
  const [rows, setRows] = useState<[string, string][]>(Object.entries(config.symbol_map));
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<{ id: string; description: string }[] | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);

  const search = async () => {
    setSearchError(null);
    try {
      const r = await provider.get<{ results: { id: string; description: string }[] }>(
        `/api/reference/search?q=${encodeURIComponent(query)}&kind=`);
      setResults(r.results);
    } catch (e) {
      setSearchError(String(e));
    }
  };

  // The search is a read: explorable in the demo, owner-only on a live server
  // (the API enforces that; this only mirrors it).
  const canSearch = !disabled || provider.kind === "demo";

  const invalid = block < shrink || !/^[A-Z0-9_]{1,24}$/.test(exchange)
    || rows.some(([i, s]) => !/^[A-Z0-9]{2,12}(_[A-Z0-9]{2,12})?$/.test(i)
                 || !/^[A-Z0-9_]{1,24}:[A-Z0-9._!&-]{1,40}$/.test(s));

  return (
    <Card title="تنظیمات"
          hint="فقط مالک حساب، با کد دومرحله‌ای، می‌تواند این‌ها را تغییر دهد.">
      <div className="stack gap12">
        <Field label="مقایسه قیمت با TradingView"
               help="خاموش: هیچ اتصالی برقرار نمی‌شود و هیچ اثری روی معامله‌ها ندارد.">
          <Switch checked={enabled} onChange={setEnabled} disabled={disabled}
                  label="مقایسه قیمت با TradingView" />
        </Field>
        <Field label="صرافی پیش‌فرض برای نمادها"
               help={<>برای نمادهایی که جدول زیر ندارند: <span className="ltr">EUR_USD → {exchange}:EURUSD</span>.
                 OANDA و FX_IDC برای فارکس رایگان و بی‌تأخیرند.</>}>
          <input className="input ltr mono" value={exchange} disabled={disabled} maxLength={24}
                 onChange={(e) => setExchange(e.target.value.toUpperCase())} />
        </Field>
        <Field label="مرز کم کردن حجم" help="اختلاف بیشتر از این (یا دو برابر اسپرد بروکر، هر کدام بزرگ‌تر)، حجم را کم می‌کند.">
          <NumberField value={shrink} onChange={setShrink} min={0.5} max={500} step={0.5}
                       suffix="bp" disabled={disabled} />
        </Field>
        <Field label="مرز مسدود کردن" help="اختلاف بیشتر از این (یا چهار برابر اسپرد)، معامله تازه را مسدود می‌کند.">
          <NumberField value={block} onChange={setBlock} min={1} max={1000} step={0.5}
                       suffix="bp" disabled={disabled} />
        </Field>
        <Field label="ضریب حجم هنگام هشدار" help="۰٫۵ یعنی نصف حجم عادی.">
          <NumberField value={mult} onChange={setMult} min={0} max={1} step={0.05}
                       disabled={disabled} />
        </Field>
        <Field label="حداکثر عمر قیمت مرجع" help="قیمت مرجعی که بیشتر از این تغییر نکرده، مقایسه نمی‌شود.">
          <NumberField value={maxAge} onChange={setMaxAge} min={5} max={3600} step={5}
                       suffix="ثانیه" disabled={disabled} />
        </Field>
        <Field label="امتیاز تحلیل تکنیکال" help="فقط نمایشی؛ هر ۱۵ دقیقه خوانده می‌شود.">
          <Switch checked={ta} onChange={setTa} disabled={disabled} label="امتیاز تحلیل تکنیکال" />
        </Field>

        <div className="stack gap8">
          <strong className="fs13">نماد مرجع دلخواه برای هر نماد بروکر</strong>
          <span className="fs12 muted">مثلاً اگر طلای بروکر شما روی قرارداد آتی است، یک نماد
            آتی انتخاب کنید. نمادهایی که الان دنبال می‌شوند:</span>
          <div className="row gap6 wrap">
            {Object.entries(mapping).map(([i, s]) =>
              <Chip key={i}><span className="ltr mono fs11">{i} → {s}</span></Chip>)}
          </div>
          {rows.map(([inst, sym], idx) => (
            <div key={idx} className="row gap6">
              <input className="input ltr mono" placeholder="XAU_USD" value={inst} disabled={disabled}
                     onChange={(e) => setRows(rows.map((r, i) => i === idx
                       ? [e.target.value.toUpperCase(), r[1]] : r))} />
              <input className="input ltr mono" placeholder="OANDA:XAUUSD" value={sym} disabled={disabled}
                     onChange={(e) => setRows(rows.map((r, i) => i === idx
                       ? [r[0], e.target.value.toUpperCase()] : r))} />
              <button className="btn ghost sm" disabled={disabled}
                      onClick={() => setRows(rows.filter((_, i) => i !== idx))}>حذف</button>
            </div>
          ))}
          <div className="row gap6">
            <button className="btn ghost sm" disabled={disabled || rows.length >= 60}
                    onClick={() => setRows([...rows, ["", ""]])}>افزودن ردیف</button>
          </div>
          <div className="row gap6 wrap">
            <input className="input ltr" placeholder="EURUSD, XAUUSD, OANDA:GBPJPY…" value={query}
                   maxLength={40} disabled={!canSearch}
                   onChange={(e) => setQuery(e.target.value)} />
            <button className="btn ghost sm" disabled={!canSearch || !query.trim()}
                    onClick={search}>جست‌وجوی نماد در TradingView</button>
          </div>
          {searchError && <span className="fs12 neg">{searchError}</span>}
          {results && (results.length === 0 ? <span className="fs12 muted">نتیجه‌ای نبود</span> : (
            <div className="row gap6 wrap">
              {results.slice(0, 12).map((r) => (
                <button key={r.id} className="btn ghost sm" disabled={disabled}
                        title={r.description}
                        onClick={() => setRows([...rows, ["", r.id]])}>
                  <span className="ltr mono fs11">{r.id}</span></button>
              ))}
            </div>
          ))}
        </div>

        {invalid && (
          <Banner tone="warn" icon="!">
            یک مقدار نامعتبر است: مرز مسدود باید از مرز هشدار بزرگ‌تر باشد، نماد بروکر مثل
            <span className="ltr"> EUR_USD </span> و نماد مرجع مثل
            <span className="ltr"> OANDA:EURUSD </span> نوشته شود.
          </Banner>
        )}
        <div className="row gap8">
          <button className="btn" disabled={disabled || invalid}
                  onClick={() => onSave({
                    enabled, exchange, shrink_bp: shrink, block_bp: block,
                    shrink_multiplier: mult, max_age_sec: maxAge, ta_ratings: ta,
                    symbol_map: Object.fromEntries(rows.filter(([i, s]) => i && s)),
                  })}>ذخیره</button>
        </div>
      </div>
    </Card>
  );
}
