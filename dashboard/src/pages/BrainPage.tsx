import React, { useCallback, useEffect, useState } from "react";
import type { Provider } from "../api";
import {
  Banner, Card, Chip, ConfirmWrite, Disclosure, Empty, Field, KV, NumberField, Switch, Tile, ago,
  fa,
} from "../components/ui";
import type { BrainConfig, BrainView, RSummary } from "../types";
import { VETO_FA } from "./Overview";

/* The brain. The page opens with what it may NOT do, for the same reason the
   AI and TradingView pages do: "the robot learns" is the sentence that makes
   a newcomer expect it to trade bigger after a good week. It cannot. */

type Write = (path: string, body: unknown, totp: string) =>
  Promise<{ ok: boolean; detail: string }>;

const VERDICT_FA: Record<string, { label: string; tone: any; help: string }> = {
  helped: { label: "مفید بود", tone: "pos",
            help: "کارهایی که این قاعده جلویشان را گرفت، به‌طور معنادار ضررده بودند." },
  hurt: { label: "هزینه داشت", tone: "neg",
          help: "کارهایی که این قاعده جلویشان را گرفت، به‌طور معنادار سودده بودند." },
  unclear: { label: "هنوز نامشخص", tone: "flat",
             help: "نتیجه‌ها به هر دو طرف می‌خورد؛ داده بیشتری لازم است." },
  insufficient: { label: "داده کم", tone: "flat",
                  help: "کمتر از ۱۵ نمونه؛ هنوز زود است نتیجه بگیریم." },
};

const LAYER_FA: Record<string, string> = {
  drift: "افت عملکرد (CUSUM)", equity_curve: "منحنی سرمایه زیر میانگین",
  allocation: "شانس سود در این وضعیت بازار", similarity: "موقعیت‌های مشابه گذشته",
  meta_label: "فیلتر دوم (بگیرم یا نه)",
};

const STATUS_FA: Record<string, { label: string; tone: any }> = {
  alive: { label: "سالم", tone: "pos" }, weak: { label: "ضعیف", tone: "warn" },
  dead: { label: "از کار افتاده", tone: "neg" }, insufficient: { label: "داده کم", tone: "flat" },
  no_data: { label: "بدون داده بروکر", tone: "flat" }, error: { label: "خطا", tone: "neg" },
  better: { label: "بهتر", tone: "pos" }, worse: { label: "بدتر", tone: "neg" },
  mixed: { label: "دوگانه", tone: "warn" }, unsupported: { label: "آزمون‌نشدنی", tone: "flat" },
};

const DOW_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یکشنبه"];

function R({ v, digits = 2 }: { v: number | null | undefined; digits?: number }) {
  if (v === null || v === undefined || !isFinite(v)) return <span className="faint">—</span>;
  const tone = v > 0 ? "pos" : v < 0 ? "neg" : "";
  return <span className={`mono ltr ${tone}`}>{v >= 0 ? "+" : ""}{v.toFixed(digits)}R</span>;
}

function CI({ s }: { s: RSummary }) {
  if (s.ci_low === null || s.ci_high === null) return <span className="faint fs11">—</span>;
  return <span className="mono ltr fs11 muted">[{s.ci_low.toFixed(2)} … {s.ci_high.toFixed(2)}]</span>;
}

function CusumBar({ stat, h }: { stat: number; h: number }) {
  const pct = Math.min(100, (stat / h) * 100);
  const tone = stat > h ? "var(--neg)" : stat > h / 2 ? "var(--warn)" : "var(--pos)";
  return (
    <div className="stack gap4" style={{ minWidth: 110 }}>
      <div className="meter" role="img"
           aria-label={`آماره افت ${stat.toFixed(1)} از مرز ${h}`}>
        <i style={{ width: `${pct}%`, background: tone }} />
      </div>
      <span className="fs11 muted"><span className="ltr mono">{stat.toFixed(1)} / {h}</span></span>
    </div>
  );
}

export default function BrainPage({ provider, write, readOnly, canAdminister }: {
  provider: Provider; write: Write; readOnly: boolean; canAdminister: boolean;
}) {
  const [view, setView] = useState<BrainView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown; danger?: boolean;
  }>(null);

  const load = useCallback(async () => {
    try {
      setView(await provider.get<BrainView>("/api/brain"));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [provider]);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  if (error) return <Banner tone="neg" icon="✕">داده‌های این صفحه بارگذاری نشد: {error}</Banner>;
  if (!view) return <Empty>در حال بارگذاری…</Empty>;
  if (!view.available) return <Banner tone="warn" icon="◈">مغز ربات در این نسخه فعال نیست.</Banner>;

  const card = view.scorecard;
  const counts = view.shadow_counts ?? {};
  const recorded = Object.values(counts).reduce((a, b) => a + b, 0);
  const resolved = Object.entries(counts).filter(([k]) => k.endsWith(":resolved"))
    .reduce((a, [, b]) => a + b, 0);
  const cooldowns = Object.entries(view.cooldowns ?? {});
  const active = (view.models ?? []).find((m) => m.status === "active");
  const candidates = (view.models ?? []).filter((m) => m.status === "candidate");
  const lastLab = (view.lab_runs ?? [])[0];
  const disabled = readOnly || !canAdminister;

  return (
    <div className="stack gap16">
      <Banner tone="info" icon="🧠">
        <strong>مغز ربات فقط می‌تواند ربات را محتاط‌تر کند.</strong> از هر سیگنال — چه معامله شده،
        چه رد شده — یاد می‌گیرد و بعداً می‌سنجد «اگر گرفته بودیم چه می‌شد». با این دانسته‌ها
        می‌تواند حجم را کم کند، بعد از چند ضرر پشت سر هم استراحت بدهد، یا معامله‌ای را رد کند؛
        اما <strong>هیچ‌وقت حجم را بزرگ‌تر نمی‌کند و به سقف‌های ایمنی دست نمی‌زند</strong>.
        هر لایه را می‌توانید پایین همین صفحه خاموش کنید، و خود مغز اندازه می‌گیرد که هر لایه
        واقعاً پول نجات داده یا نه.
      </Banner>
      {view.last_error && (
        <Banner tone="warn" icon="!">آخرین خطای مغز (بدون اثر روی ایمنی؛ لایه خطادار اثری
          ندارد): <span className="ltr mono fs12">{view.last_error}</span></Banner>
      )}

      <div className="grid g4">
        <Tile label="سیگنال‌های ثبت‌شده" value={fa(recorded)}
              note={<>{fa(resolved)} مورد نتیجه‌اش معلوم شده</>}
              hint="هر سیگنالی که ربات بررسی کرد، حتی اگر معامله نشد. نتیجه با قیمت‌های واقعی بعدی سنجیده می‌شود." />
        <Tile label="استراحت اجباری" value={cooldowns.length ? fa(cooldowns.length) : "ندارد"}
              tone={cooldowns.length ? "warn" : undefined}
              hint="بعد از چند ضرر پشت سر هم، ربات مدتی معامله تازه باز نمی‌کند — ضد «معامله انتقامی»." />
        <Tile label="فیلتر دوم" value={active ? "فعال" : candidates.length ? "منتظر تأیید" : "ندارد"}
              tone={candidates.length && !active ? "warn" : undefined}
              hint="مدلی که یاد می‌گیرد کدام سیگنال‌ها ارزش گرفتن دارند. فقط با تأیید شما فعال می‌شود." />
        <Tile label="آخرین آزمایشگاه شبانه" value={view.lab_running ? "در حال اجرا…" :
              lastLab ? ago(lastLab.ts_ns) : "هنوز نه"} sub
              hint="هر شب استراتژی‌ها روی قیمت‌های خود بروکر دوباره آزموده می‌شوند." />
      </div>

      <Card title="استراحت اجباری"
            hint="ربات بعد از چند ضرر پشت سر هم برای مدت مشخصی معامله تازه باز نمی‌کند. معامله‌های باز و حد ضررشان دست‌نخورده می‌مانند.">
        {cooldowns.length === 0 ? (
          <Empty>الان استراحتی در کار نیست. ضررهای پشت سر هم حساب: {fa(view.streaks?.account ?? 0)}</Empty>
        ) : (
          <div className="stack gap8">
            {cooldowns.map(([scope, why]) => (
              <div key={scope} className="row gap8 wrap">
                <Chip tone="warn">{scope === "*" ? "کل حساب" : <span className="ltr mono">{scope}</span>}</Chip>
                <span className="fs12 ltr muted grow">{why}</span>
                <button className="btn ghost sm" disabled={disabled}
                        onClick={() => setConfirm({
                          action: "برداشتن زودتر استراحت اجباری",
                          description: <>استراحت برای این هدف برداشته می‌شود و ربات دوباره می‌تواند
                            معامله باز کند. این استراحت برای جلوگیری از «جبران ضرر با عجله» است؛
                            فقط اگر دلیلش را می‌دانید برش دارید. در دفتر رویدادها ثبت می‌شود.</>,
                          path: "/api/brain/cooldown/clear", body: { scope },
                        })}>برداشتن زودتر</button>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="سلامت هر استراتژی"
            hint="نتیجه واقعی هر استراتژی با «حالت عادی» خودش (که آزمایشگاه شبانه اندازه گرفته) مقایسه می‌شود. اگر افت معنادار باشد، حجمش کم می‌شود تا وضعیت روشن شود.">
        {(view.strategies ?? []).length === 0 ? <Empty>استراتژی فعالی نیست</Empty> : (
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr><th>استراتژی</th><th className="n">معامله</th><th className="n">میانگین</th>
                  <th>بازه ۹۵٪</th><th className="n">حالت عادی</th><th>آماره افت</th>
                  <th className="n">ضریب حجم</th><th>چرا</th></tr>
              </thead>
              <tbody>
                {(view.strategies ?? []).map((s) => (
                  <tr key={s.strategy}>
                    <td className="mono fs12">{s.strategy}</td>
                    <td className="n num">{s.live.n}</td>
                    <td className="n"><R v={s.live.mean_r} /></td>
                    <td><CI s={s.live} /></td>
                    <td className="n" title={s.baseline.source === "lab" ? "از آزمایشگاه شبانه"
                      : "پیش‌فرض (آزمایشگاه هنوز اندازه نگرفته)"}>
                      <R v={s.baseline.mean_r} /></td>
                    <td>{s.cusum ? <CusumBar stat={s.cusum.stat} h={s.drift_threshold} />
                      : <span className="faint">—</span>}</td>
                    <td className="n">
                      <Chip tone={s.multiplier < 1 ? "warn" : "pos"}>
                        <span className="ltr mono">×{s.multiplier.toFixed(2)}</span></Chip></td>
                    <td className="fs12">
                      {Object.keys(s.layers).length === 0 ? <span className="muted">عادی</span>
                        : Object.entries(s.layers).map(([k, v]) => (
                          <div key={k}>{LAYER_FA[k] ?? k} <span className="ltr mono">×{v.toFixed(2)}</span></div>))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Disclosure summary="«آماره افت» چیست؟">
          <p className="fs12" style={{ lineHeight: 1.9 }}>
            آزمون CUSUM (پیج، ۱۹۵۴) هر معامله را با میانگین «حالت عادی» استراتژی مقایسه می‌کند و
            کمبودها را جمع می‌زند؛ سودها از جمع کم می‌کنند. اگر جمع از مرز (پیش‌فرض {fa(4)})
            بگذرد، یعنی افت واقعی است نه بدشانسی: با این تنظیم، استراتژی سالم به‌طور میانگین
            فقط هر ~{fa(170)} معامله یک هشدار اشتباه می‌دهد، ولی افتی به اندازه یک انحراف معیار
            در حدود {fa(8)} معامله دیده می‌شود. هشدار وقتی برداشته می‌شود که آماره به نصف مرز برگردد.
          </p>
        </Disclosure>
      </Card>

      <Card title="آیا قاعده‌های ایمنی واقعاً پول نجات دادند؟"
            hint="برای هر سیگنالی که ربات رد کرد، سنجیدیم اگر گرفته بود چه می‌شد. میانگین منفی یعنی آن قاعده ما را از ضرر نجات داده.">
        {!card || card.rules.length === 0 ? (
          <Empty>هنوز سیگنال ردشده‌ای که نتیجه‌اش معلوم باشد نداریم</Empty>
        ) : (
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr><th>قاعده</th><th className="n">دفعات</th><th className="n">میانگین نتیجه</th>
                  <th>بازه ۹۵٪</th><th className="n">درصد برد</th><th>حکم</th></tr>
              </thead>
              <tbody>
                {card.rules.map((r) => {
                  const v = VERDICT_FA[r.verdict] ?? VERDICT_FA.unclear;
                  return (
                    <tr key={r.rule}>
                      <td className="fs12">{VETO_FA[r.rule] ?? r.rule}
                        <div className="fs11 faint ltr mono">{r.rule}</div></td>
                      <td className="n num">{r.n}</td>
                      <td className="n"><R v={r.mean_r} /></td>
                      <td><CI s={r} /></td>
                      <td className="n num">{r.win_rate === null ? "—" : `${Math.round(r.win_rate * 100)}%`}</td>
                      <td><Chip tone={v.tone} title={v.help}>{v.label}</Chip></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {card && card.layers.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <strong className="fs13">اثر لایه‌های مغز روی معامله‌های انجام‌شده</strong>
            <p className="fs12 muted">لایه‌ای که قبل از یک ضرر حجم را کم کرد، پول نجات داده
              (مثبت)؛ اگر قبل از یک سود کم کرد، هزینه داشته (منفی).</p>
            <div className="table-wrap">
              <table className="t">
                <thead><tr><th>لایه</th><th className="n">دفعات</th>
                  <th className="n">جمع R نجات‌یافته</th><th>حکم</th></tr></thead>
                <tbody>
                  {card.layers.map((l) => {
                    const v = VERDICT_FA[l.verdict] ?? VERDICT_FA.unclear;
                    return (
                      <tr key={l.layer}>
                        <td className="fs12">{LAYER_FA[l.layer] ?? l.layer}</td>
                        <td className="n num">{l.n}</td>
                        <td className="n"><R v={l.saved_r} /></td>
                        <td><Chip tone={v.tone} title={v.help}>{v.label}</Chip></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </Card>

      <div className="grid g2">
        <Card title="فیلتر دوم: «این سیگنال را بگیرم یا نه؟»"
              hint="مدلی که روی ۶۰٪ اول تاریخچه آموزش می‌بیند و روی ۴۰٪ آخر که هرگز ندیده امتحان می‌شود. فقط اگر در آن امتحان خوب باشد پیشنهاد می‌شود، و فقط با تأیید شما فعال می‌شود.">
          {active && (
            <div className="stack gap8" style={{ marginBottom: 12 }}>
              <div className="row gap8 wrap">
                <Chip tone="pos">فعال</Chip>
                <span className="ltr mono fs12">{active.id}</span>
                <span className="fs12 muted">تأیید: {active.decided_by}</span>
                <button className="btn ghost sm" disabled={disabled}
                        onClick={() => setConfirm({
                          action: "غیرفعال کردن فیلتر دوم",
                          description: "فیلتر خاموش می‌شود و همه سیگنال‌ها مثل قبل فقط از موتور ریسک عبور می‌کنند.",
                          path: "/api/brain/model/retire", body: {},
                        })}>غیرفعال کن</button>
              </div>
              {view.meta_live && view.meta_live.n > 0 && (
                <span className="fs12 muted">کارکرد واقعی روی {fa(view.meta_live.n)} سیگنال:
                  AUC <span className="ltr mono">{view.meta_live.auc?.toFixed(2) ?? "—"}</span></span>
              )}
            </div>
          )}
          {candidates.length === 0 && !active ? (
            <Empty>هنوز مدلی از امتحان خارج از نمونه قبول نشده است</Empty>
          ) : candidates.map((m) => (
            <div key={m.id} className="stack gap6" style={{ borderTop: "1px solid var(--hairline)", paddingTop: 8 }}>
              <div className="row gap8 wrap">
                <Chip tone="warn">منتظر تأیید</Chip>
                <span className="ltr mono fs12">{m.id}</span>
              </div>
              <div className="kv-grid c3">
                <KV k="AUC روی داده ندیده" v={<span className="ltr mono">{m.report.holdout?.auc?.toFixed(3) ?? "—"}</span>}
                    hint="۰٫۵ یعنی شیر یا خط؛ هر چه بالاتر، تشخیص بهتر. زیر ۰٫۵۵ پیشنهاد نمی‌شود." />
                <KV k="نمونه آموزش / امتحان" v={<span className="ltr mono">{m.report.n_train ?? "—"} / {m.report.n_holdout ?? "—"}</span>} />
                <KV k="نمونه‌های حذف‌شده برای جلوگیری از نشت" v={<span className="ltr mono">{m.report.n_purged ?? 0}</span>}
                    hint="نمونه‌هایی که برچسبشان قیمت‌های دوره امتحان را دیده بود حذف شدند (پاک‌سازی، لوپز د پرادو)." />
              </div>
              <div>
                <button className="btn sm" disabled={disabled}
                        onClick={() => setConfirm({
                          action: "فعال کردن فیلتر دوم",
                          description: <>از این پس هر سیگنال قبل از موتور ریسک از این فیلتر هم رد می‌شود.
                            فیلتر فقط می‌تواند سیگنالی را رد کند یا حجمش را کم کند. عملکرد واقعی‌اش
                            همین‌جا نمایش داده می‌شود و هر وقت بخواهید خاموشش می‌کنید.</>,
                          path: "/api/brain/model/approve", body: { model_id: m.id },
                        })}>تأیید و فعال‌سازی</button>
              </div>
            </div>
          ))}
        </Card>

        <Card title="آزمایشگاه شبانه"
              hint="هر شب، هر استراتژی روی قیمت‌های خود بروکر دوباره آزموده می‌شود — یک بار با هزینه عادی و یک بار با دو برابر هزینه. نتیجه، «حالت عادی» آزمون افت را تنظیم می‌کند."
              actions={<button className="btn ghost sm" disabled={disabled || !!view.lab_running}
                               onClick={() => setConfirm({
                                 action: "اجرای آزمایشگاه همین حالا",
                                 description: "آزمون‌ها در پس‌زمینه اجرا می‌شوند (حداکثر چند دقیقه) و روی معامله‌ها اثری ندارند.",
                                 path: "/api/brain/lab/run", body: {},
                               })}>{view.lab_running ? "در حال اجرا…" : "اجرا کن"}</button>}>
          {!lastLab ? <Empty>هنوز اجرا نشده؛ هر شب ساعت {fa(view.config?.lab_hour_utc ?? 2)} به وقت جهانی اجرا می‌شود</Empty> : (
            <div className="stack gap8">
              <span className="fs12 muted">{ago(lastLab.ts_ns)} — {fa(Math.round(lastLab.seconds ?? 0))} ثانیه</span>
              <div className="table-wrap">
                <table className="t">
                  <thead><tr><th>استراتژی</th><th>وضعیت</th><th className="n">معامله</th>
                    <th className="n">میانگین</th><th className="n">با هزینه ۲ برابر</th></tr></thead>
                  <tbody>
                    {lastLab.strategies.map((s) => {
                      const st = STATUS_FA[s.status] ?? { label: s.status, tone: "flat" };
                      return (
                        <tr key={s.strategy}>
                          <td className="mono fs12">{s.strategy}</td>
                          <td><Chip tone={st.tone} title={s.note ?? s.error ?? ""}>{st.label}</Chip></td>
                          <td className="n num">{s.n ?? "—"}</td>
                          <td className="n"><R v={s.mean_r} /></td>
                          <td className="n"><R v={s.stressed_mean_r} /></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {lastLab.proposals.length > 0 && (
                <div className="fs12">
                  <strong>پیشنهادهای تنظیمات (آزمون الف/ب روی همان داده):</strong>
                  {lastLab.proposals.map((p) => (
                    <div key={p.id} className="row gap6">
                      <span className="ltr mono">{p.path}</span>
                      <Chip tone={(STATUS_FA[p.status] ?? { tone: "flat" }).tone}>
                        {(STATUS_FA[p.status] ?? { label: p.status }).label}</Chip>
                    </div>
                  ))}
                </div>
              )}
              {(lastLab.errors ?? []).length > 0 && (
                <span className="fs11 neg ltr">{(lastLab.errors ?? []).join(" · ")}</span>
              )}
              <p className="fs11 muted">«سالم» یعنی کل بازه اطمینان بالای صفر است و با هزینه دو برابر
                هم سودده می‌ماند. این حکم ارتقا نیست؛ ارتقا به پول واقعی فقط از راه آزمون پذیرش است.</p>
            </div>
          )}
        </Card>
      </div>

      <Card title="گزارش هفتگی"
            hint={`هر ${DOW_FA[view.config?.weekly_report_dow ?? 6]} ساخته و (اگر روشن باشد) به تلگرام/بله فرستاده می‌شود.`}>
        {(view.reports ?? []).length === 0 ? <Empty>هنوز گزارشی ساخته نشده</Empty> : (
          <div className="table-wrap">
            <table className="t">
              <thead><tr><th>هفته منتهی به</th><th className="n">معامله</th>
                <th className="n">جمع</th><th className="n">درصد برد</th><th>به تفکیک استراتژی</th></tr></thead>
              <tbody>
                {(view.reports ?? []).map((r) => (
                  <tr key={r.id}>
                    <td className="mono fs12 ltr">{r.week_ending}</td>
                    <td className="n num">{r.trades.n}</td>
                    <td className="n"><R v={r.trades.sum_r} /></td>
                    <td className="n num">{r.trades.win_rate === null ? "—" : `${Math.round(r.trades.win_rate * 100)}%`}</td>
                    <td className="fs12">{Object.entries(r.by_strategy ?? {}).map(([k, v]) => (
                      <span key={k} className="row gap4 wrap"><span className="ltr mono">{k}</span>
                        <R v={v.sum_r} /></span>))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {view.config && (
        <BrainSettings config={view.config} disabled={disabled}
                       onSave={(patch) => setConfirm({
                         action: "ذخیره تنظیمات مغز ربات",
                         description: <>تنظیمات از چرخه بعدی ربات اعمال می‌شود و در دفتر رویدادها
                           ثبت می‌شود. هیچ‌کدام از این تنظیمات نمی‌تواند حجم را از سقف موتور ریسک
                           بزرگ‌تر کند.</>,
                         path: "/api/brain/settings", body: { patch },
                       })} />
      )}

      <ConfirmWrite open={!!confirm} action={confirm?.action ?? ""}
                    description={confirm?.description} danger={confirm?.danger}
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

function BrainSettings({ config, disabled, onSave }: {
  config: BrainConfig; disabled: boolean; onSave: (patch: Partial<BrainConfig>) => void;
}) {
  const [c, setC] = useState<BrainConfig>(config);
  const [rows, setRows] = useState<[string, number][]>(
    Object.entries(config.stress_scenarios).map(([k, v]) => [k, Math.round(v * 1000) / 10]));
  const set = <K extends keyof BrainConfig>(k: K, v: BrainConfig[K]) => setC({ ...c, [k]: v });
  const sw = (k: keyof BrainConfig, label: string, help: React.ReactNode) => (
    <Field label={label} help={help}>
      <Switch checked={Boolean(c[k])} disabled={disabled} label={label}
              onChange={(v) => set(k, v as any)} />
    </Field>
  );
  const invalid = rows.some(([k, v]) => !/^(\*|[A-Z0-9]{2,8})$/.test(k) || !(v >= 0 && v <= 100));

  return (
    <Card title="تنظیمات مغز ربات" hint="فقط مالک حساب، با کد دومرحله‌ای، می‌تواند این‌ها را تغییر دهد.">
      <div className="stack gap12">
        {sw("enabled", "مغز ربات", "خاموش: همه لایه‌های زیر بی‌اثر می‌شوند و ربات دقیقاً مثل قبل کار می‌کند.")}
        {sw("shadow_book", "ثبت همه سیگنال‌ها (دفتر سایه)", "ماده خام همه یادگیری‌ها. خاموش کردنش یادگیری را متوقف می‌کند.")}

        <strong className="fs13">استراحت بعد از ضرر</strong>
        <Field label="چند ضرر پشت سر هم، کل حساب استراحت کند" help="۰ یعنی خاموش. پیش‌فرض ۳.">
          <NumberField value={c.loss_streak_limit} min={0} max={20} step={1} disabled={disabled}
                       onChange={(v) => set("loss_streak_limit", v)} />
        </Field>
        <Field label="مدت استراحت حساب">
          <NumberField value={c.loss_streak_cooldown_hours} min={0} max={168} step={1}
                       suffix="ساعت" disabled={disabled}
                       onChange={(v) => set("loss_streak_cooldown_hours", v)} />
        </Field>
        <Field label="چند ضرر پشت سر هم، یک استراتژی استراحت کند" help="۰ یعنی خاموش. پیش‌فرض ۴.">
          <NumberField value={c.strategy_loss_streak_limit} min={0} max={20} step={1}
                       disabled={disabled} onChange={(v) => set("strategy_loss_streak_limit", v)} />
        </Field>
        <Field label="مدت استراحت استراتژی">
          <NumberField value={c.strategy_cooldown_hours} min={0} max={720} step={1}
                       suffix="ساعت" disabled={disabled}
                       onChange={(v) => set("strategy_cooldown_hours", v)} />
        </Field>

        <strong className="fs13">لایه‌های کم‌کننده حجم</strong>
        {sw("drift_enabled", "آزمون افت عملکرد (CUSUM)", "وقتی نتیجه واقعی از حالت عادی استراتژی به‌طور معنادار پایین‌تر است، حجم نصف می‌شود.")}
        {sw("equity_filter_enabled", "فیلتر منحنی سرمایه", "وقتی منحنی سود استراتژی زیر میانگین ۲۰ معامله اخیرش است، حجم نصف می‌شود. شواهد علمی درباره‌اش دوگانه است؛ اثرش در جدول بالا سنجیده می‌شود.")}
        {sw("similarity_enabled", "حافظه موقعیت‌های مشابه", "اگر ۲۵ موقعیت شبیه‌تر در گذشته به‌طور معنادار ضررده بودند، حجم نصف می‌شود.")}
        {sw("allocation_enabled", "تخصیص بیزی بر اساس وضعیت بازار", "اگر شانس سودده بودن استراتژی در وضعیت فعلی بازار زیر ۵۰٪ باشد، حجم کم می‌شود (نه کمتر از کف).")}

        <strong className="fs13">آزمون بحران (جهش ناگهانی قیمت)</strong>
        {sw("stress_enabled", "آزمون بحران", <>اگر همه معامله‌های باز به اندازه بدترین جهش ثبت‌شده
          تاریخ (مثل فرانک سوئیس ۲۰۱۵) یک‌باره ضرر کنند، زیان نباید از این درصد حساب بیشتر شود.</>)}
        <Field label="سقف ضرر در بدترین سناریو" help="پیش‌فرض ۲۵٪ حساب. کمتر = امن‌تر و حجم کوچک‌تر.">
          <NumberField value={c.stress_loss_limit_pct} min={1} max={100} step={1} suffix="٪"
                       disabled={disabled} onChange={(v) => set("stress_loss_limit_pct", v)} />
        </Field>
        <Disclosure summary="جدول جهش هر ارز (درصد قیمت)">
          <div className="stack gap6">
            <span className="fs12 muted">«*» برای ارزهایی است که در جدول نیستند. عددها از رویدادهای
              ثبت‌شده آمده‌اند؛ کم کردنشان یعنی فرض کنید تاریخ تکرار نمی‌شود.</span>
            <div className="row gap6 wrap">
              {rows.map(([k, v], i) => (
                <div key={i} className="row gap4">
                  <input className="input ltr mono" style={{ width: 70 }} value={k} disabled={disabled}
                         maxLength={8}
                         onChange={(e) => setRows(rows.map((r, j) => j === i
                           ? [e.target.value.toUpperCase(), r[1]] : r))} />
                  <input className="input num" style={{ width: 70 }} type="number" value={v}
                         disabled={disabled} min={0} max={100} step={0.5}
                         onChange={(e) => setRows(rows.map((r, j) => j === i
                           ? [r[0], Number(e.target.value)] : r))} />
                  <span className="fs11 muted">٪</span>
                </div>
              ))}
            </div>
            <div><button className="btn ghost sm" disabled={disabled || rows.length >= 60}
                         onClick={() => setRows([...rows, ["", 3]])}>افزودن ارز</button></div>
          </div>
        </Disclosure>

        <strong className="fs13">یادگیری شبانه</strong>
        {sw("lab_enabled", "آزمایشگاه شبانه", "هر شب استراتژی‌ها روی قیمت‌های بروکر دوباره آزموده می‌شوند.")}
        <Field label="ساعت اجرا (به وقت جهانی UTC)">
          <NumberField value={c.lab_hour_utc} min={0} max={23} step={1} disabled={disabled}
                       onChange={(v) => set("lab_hour_utc", v)} />
        </Field>
        {sw("meta_auto_train", "آموزش فیلتر دوم", "فیلتر آموزش‌دیده فقط پیشنهاد می‌شود؛ فعال شدنش با تأیید شماست.")}

        {invalid && <Banner tone="warn" icon="!">یک ردیف جدول جهش نامعتبر است: نام ارز مثل
          <span className="ltr"> CHF </span> یا «*» و عدد بین ۰ تا ۱۰۰.</Banner>}
        <div className="row gap8">
          <button className="btn" disabled={disabled || invalid}
                  onClick={() => {
                    const { stress_scenarios: _omit, ...rest } = c;
                    onSave({
                      ...rest,
                      stress_scenarios: Object.fromEntries(
                        rows.filter(([k]) => k).map(([k, v]) => [k, v / 100])),
                    });
                  }}>ذخیره</button>
        </div>
      </div>
    </Card>
  );
}
