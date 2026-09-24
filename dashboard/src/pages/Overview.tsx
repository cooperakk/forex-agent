import React, { useMemo } from "react";
import { BarsH, LineChart, Sparkline, StackedRatio } from "../components/charts";
import {
  Banner, Card, Chip, Disclosure, Hint, KV, Tile, ago, dt, money, pct,
} from "../components/ui";
import type { Snapshot } from "../types";

const MODE_LABEL: Record<string, string> = {
  observe: "فقط تماشا", advisory: "فقط پیشنهاد",
  semi_auto: "نیمه‌خودکار", autonomous: "کاملاً خودکار",
};
const MODE_NOTE: Record<string, string> = {
  observe: "همه‌چیز را حساب می‌کند ولی هیچ معامله‌ای نمی‌گذارد — حتی روی حساب تمرینی.",
  advisory: "معامله را فقط پیشنهاد می‌دهد؛ تا شما دکمه تأیید را نزنید هیچ سفارشی ارسال نمی‌شود.",
  semi_auto: "اگر معامله داخل محدوده‌ای باشد که از قبل تأیید کرده‌اید خودش اجرا می‌کند؛ بیرون آن محدوده فقط پیشنهاد می‌دهد.",
  autonomous: "خودش معامله می‌کند و از شما اجازه نمی‌گیرد — ولی همچنان زیر همه سقف‌های ایمنی است و هر سقفی را که رد کند، معامله انجام نمی‌شود.",
};
const VENUE_LABEL: Record<string, string> = {
  paper: "تمرینی — شبیه‌ساز داخلی", demo: "تمرینی — حساب دمو بروکر", live: "پول واقعی",
};
/* Market "regime" in words a newcomer can picture. */
const REGIME_LABEL: Record<string, string> = {
  trending: "قیمت یک جهت مشخص دارد", quiet_range: "بازار آرام و بی‌جهت",
  volatile_range: "بی‌جهت ولی پرنوسان", stress: "بازار بحرانی",
  unknown: "نامشخص",
};

export default function Overview({ snap }: { snap: Snapshot }) {
  const { status, equity, performance, positions, decisions, risk } = snap;
  const eq = Number(status.account.equity ?? 0);
  const start = equity[0]?.equity ?? eq;

  const equitySeries = useMemo(() => ([{
    name: "پول حساب", color: "var(--ink)", area: true,
    points: equity.map((p) => ({ x: p.ts_ns / 1e6, y: p.equity, label: dt(p.ts_ns) })),
  }]), [equity]);

  const ddSeries = useMemo(() => ([{
    name: "فاصله تا بالاترین حد حساب", color: "var(--s8)", area: true,
    points: equity.map((p) => ({ x: p.ts_ns / 1e6, y: -p.drawdown_pct, label: dt(p.ts_ns) })),
  }]), [equity]);

  const vetoCounts = useMemo(() => {
    const m = new Map<string, number>();
    decisions.forEach((d) => d.vetoes.forEach((v) => m.set(v.rule, (m.get(v.rule) ?? 0) + 1)));
    return [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8)
      .map(([label, value]) => ({ label: VETO_FA[label] ?? label, value }));
  }, [decisions]);

  const actionMix = useMemo(() => {
    const m: Record<string, number> = { executed: 0, queued: 0, vetoed: 0, skipped: 0, proposed: 0 };
    decisions.forEach((d) => { m[d.action] = (m[d.action] ?? 0) + 1; });
    return [
      { label: "انجام شد", value: m.executed, color: "var(--s1)" },
      { label: "منتظر تأیید شما", value: m.queued + m.proposed, color: "var(--s4)" },
      { label: "قواعد ایمنی جلویش را گرفت", value: m.vetoed, color: "var(--s8)" },
      { label: "کنار گذاشته شد", value: m.skipped, color: "var(--ink-faint)" },
    ];
  }, [decisions]);

  const health = status.health as any;
  const dayPnl = Number(risk.day_pnl ?? 0);

  return (
    <div className="stack gap16">
      {status.halted && (
        <Banner tone="neg" icon="⏹">
          <strong>ربات متوقف شده و معامله تازه‌ای باز نمی‌کند.</strong> دلیل: {status.halt_reason} —
          راه‌اندازی دوباره فقط با دست خودتان ممکن است. معامله‌های بازِ فعلی همچنان مدیریت
          می‌شوند و بستن آن‌ها هیچ‌وقت مسدود نمی‌شود.
        </Banner>
      )}
      {status.kill_switch.engaged && (
        <Banner tone="neg" icon="🛑">
          <strong>کلید توقف اضطراری زده شده است.</strong> دلیل: {status.kill_switch.reason} —
          تا وقتی این کلید فعال است هیچ معامله تازه‌ای باز نمی‌شود. برداشتنش فقط با دست
          خودتان است: یا حذف فایل روی سرور، یا از همین صفحه با کد شش‌رقمی.
        </Banner>
      )}
      {status.security_warning && (
        <Banner tone="warn" icon="⚠">{status.security_warning}</Banner>
      )}
      {status.unresolved_orders > 0 && (
        <Banner tone="warn" icon="❓">
          <strong>{status.unresolved_orders} سفارش هست که معلوم نیست ثبت شده یا نه.</strong>{" "}
          پاسخ بروکر نرسیده است. تا روشن شدن وضعیت هیچ معامله تازه‌ای باز نمی‌شود، چون اگر
          سفارش اول ثبت شده باشد و دوباره بفرستیم، حجم معامله دو برابر می‌شود.
        </Banner>
      )}

      <div className="grid g4">
        <Card>
          <Tile label="پول حساب همین الان" term="ارزش خالص"
                hint={<>پول ته حساب، به‌علاوه سود یا زیان معامله‌های بازی که هنوز بسته نشده‌اند.
                  مثال: ۱۰٬۰۰۰ دلار موجودی با ۱۲۰ دلار سود باز، یعنی ۱۰٬۱۲۰ دلار.</>}
                value={<span className="num">{money(eq)}</span>}
                note={<span className={eq >= start ? "pos" : "neg"}>
                  {pct(((eq / start) - 1) * 100)} نسبت به ابتدای این دوره
                </span>} />
          <div className="mt8"><Sparkline values={equity.slice(-90).map((p) => p.equity)} fill /></div>
        </Card>
        <Card>
          <Tile label="سود یا زیان امروز"
                hint={<>جمع سود و زیان امروز بر حسب دلار. درصد زیر آن می‌گوید این مبلغ چند
                  درصد کل حساب است — مثلاً ۲۵ دلار روی حساب ۱۰٬۰۰۰ دلاری یعنی ۰٫۲۵٪.</>}
                value={<span className={`num ${dayPnl >= 0 ? "pos" : "neg"}`}>
                  {dayPnl >= 0 ? "+" : ""}{money(dayPnl)}
                </span>}
                note={`یعنی ${pct(risk.day_pnl_pct)} کل حساب · امروز ${risk.trades_today} معامله از سقف مجاز ${risk.limits.max_trades_per_day} تا`} />
          <div className="mt12 meter">
            <i style={{ width: `${Math.min(100, (risk.trades_today / Number(risk.limits.max_trades_per_day)) * 100)}%` }} />
          </div>
        </Card>
        <Card>
          <Tile label="چقدر از بیشترین مقدار حساب پایین آمده" term="افت از اوج"
                hint={<>فاصله پول حساب تا بالاترین رقمی که تا امروز داشته است. مثال: حساب
                  یک‌بار ۱۱٬۰۰۰ دلار شده و حالا ۱۰٬۴۵۰ دلار است، یعنی افت ۵٪. فقط وقتی صفر
                  می‌شود که حساب دوباره از رکورد خودش رد شود.</>}
                value={<span className="num">{risk.drawdown_pct.toFixed(2)}٪</span>}
                tone={risk.drawdown_pct > 5 ? "warn" : undefined}
                note={`با این افت، اندازه هر معامله ×${risk.risk_multiplier} شده · در افت ${risk.limits.max_drawdown_halt_pct}٪ ربات کاملاً متوقف می‌شود`} />
          <div className="mt12 meter">
            <i style={{
              width: `${Math.min(100, (risk.drawdown_pct / Number(risk.limits.max_drawdown_halt_pct)) * 100)}%`,
              background: risk.drawdown_pct > 5 ? "var(--ember)" : "var(--ink)",
            }} />
          </div>
        </Card>
        <Card>
          <Tile label="حال‌وهوای فعلی بازار" term="رژیم"
                hint={<>برچسبی برای وضعیت بازار: جهت‌دار، آرام، پرنوسان یا بحرانی. هر
                  استراتژی فقط در بعضی از این حالت‌ها جواب می‌دهد، پس ربات در حالت نامناسب
                  معامله نمی‌کند.</>}
                value={status.regime ? REGIME_LABEL[status.regime.regime] ?? status.regime.regime : "—"}
                sub
                note={status.regime?.explanation} />
          <div className="mt8 row gap6 wrap">
            <Chip title="چقدر به این تشخیص مطمئن است">
              اطمینان {status.regime ? (status.regime.confidence * 100).toFixed(0) : "—"}٪
            </Chip>
            <Chip title="هر چه کمتر، یعنی جفت‌ارزها بیشتر شبیه هم حرکت می‌کنند و تنوع کمتر است">
              پراکندگی حرکت‌ها {status.regime ? status.regime.correlation_dispersion.toFixed(2) : "—"}
            </Chip>
          </div>
        </Card>
      </div>

      <div className="grid g-2-1">
        <Card title={<>روند پول حساب<span className="term-tag">منحنی سرمایه</span></>}
              hint={<>هر نقطه، مقدار پول حساب در یک لحظه است. بالا رفتن خط یعنی حساب بزرگ‌تر
                شده و پایین آمدنش یعنی کوچک‌تر. مهم‌تر از نقطه آخر، شکل مسیر است.</>}
              sub={<>
                {equity.length} نقطه ثبت‌شده · نمره عملکرد {performance.sharpe.toFixed(2)}
                {" "}(زیر ۱ یعنی ضعیف) · بدترین افت تا امروز {performance.max_drawdown_pct.toFixed(2)}٪
              </>}>
          <LineChart series={equitySeries} height={300}
                     yFmt={(v) => money(v, 0)}
                     xFmt={(x) => new Date(x).toISOString().slice(0, 10)}
                     valueFmt={(v) => money(v)} title="روند پول حساب" />
          <div className="mt16">
            <div className="card-sub" style={{ marginBottom: 6 }}>
              فاصله حساب تا رکورد خودش، بر حسب درصد
              <Hint text={<>هر چه خط پایین‌تر برود، حساب از بالاترین مقدارش دورتر است. خط روی
                صفر یعنی حساب دقیقاً روی رکورد خودش است. مثال: −۵٪ یعنی از هر ۱۰٬۰۰۰ دلار،
                ۵۰۰ دلار زیر رکورد هستید.</>} />
            </div>
            <LineChart series={ddSeries} height={130} zeroBaseline
                       yFmt={(v) => `${v.toFixed(0)}٪`}
                       xFmt={(x) => new Date(x).toISOString().slice(0, 10)}
                       valueFmt={(v) => `${(-v).toFixed(2)}٪`} />
          </div>
        </Card>

        <div className="stack gap16">
          <Card title="ربات الان در چه حالتی است"
                hint={<>حالت کار ربات تعیین می‌کند چه کسی اجازه اجرای معامله را می‌دهد: خود
                  ربات، یا شما. سقف‌های ایمنی در هر چهار حالت به‌طور کامل برقرارند.</>}>
            <div className="stack gap8">
              <div className="row gap8 wrap">
                <Chip tone="solid">{MODE_LABEL[status.mode]}</Chip>
                <Chip tone={status.venue_mode === "live" ? "neg" : "info"}>
                  {VENUE_LABEL[status.venue_mode]}
                </Chip>
                <Chip tone={status.halted || status.kill_switch.engaged ? "neg" : "pos"}>
                  <i className="dot" />{status.halted ? "متوقف" : "فعال"}
                </Chip>
              </div>
              <p className="fs12 muted" style={{ lineHeight: 1.7 }}>{MODE_NOTE[status.mode]}</p>
              <KV k="چند بار بازار را بررسی کرده" v={money(status.cycles, 0)}
                  hint="ربات هر چند ثانیه یک‌بار همه‌چیز را از نو نگاه می‌کند؛ این عدد تعداد آن دفعات است." />
              <KV k="چند ساعت است روشن مانده" v={`${(status.uptime_sec / 3600).toFixed(1)} ساعت`} />
              <KV k="پول قطعی‌شده / پول قفل‌شده نزد بروکر"
                  hint={<>عدد اول پولی است که معامله‌هایش بسته شده و دیگر تغییر نمی‌کند. عدد
                    دوم بخشی از حساب است که بابت معامله‌های باز نزد بروکر قفل شده و تا بسته
                    شدن آن‌ها قابل استفاده نیست.</>}
                  v={`${money(Number(status.account.balance))} / ${money(Number(status.account.margin_used ?? 0))}`} />
              <KV k="چند برابر پول لازم را داریم"
                  hint={<>نسبت پول حساب به پول قفل‌شده. عدد بزرگ یعنی خیال راحت؛ بروکرها
                    معمولاً زیر ۱۰۰٪ اخطار می‌دهند و زیر ۵۰٪ خودشان معامله‌ها را می‌بندند.</>}
                  v={`${status.account.margin_level_pct ?? "—"}٪`} />
              <KV k="شماره نسخه تنظیمات" v={`#${status.config_version}`}
                  hint="هر بار یک تنظیم عوض شود این شماره یکی بالا می‌رود و تغییر در دفتر ثبت رویدادها می‌ماند." />
            </div>
          </Card>

          <Card title="اتصال و سرعت"
                hint={<>اگر ارتباط با بروکر قطع شود، ربات نه می‌بیند و نه می‌تواند معامله را
                  ببندد. به همین دلیل کیفیت اتصال اینجا به اندازه سود و زیان مهم است.</>}
                sub="قطع شدن اینترنت یک اتفاق نادر نیست؛ باید فرض شود که رخ می‌دهد">
            <div className="stack gap8">
              <KV k="چند درصد ۲۴ ساعت گذشته وصل بوده‌ایم"
                  hint={<>۹۹٪ یعنی حدود ۱۵ دقیقه قطعی در شبانه‌روز؛ ۹۷٫۸٪ یعنی حدود ۳۱ دقیقه.
                    زیر ۹۹٪، استراتژی‌های کوتاه‌مدت فقط به‌خاطر اتصال کنار گذاشته می‌شوند.</>}
                  v={`${(health.uptime_pct ?? 0).toFixed(2)}٪`}
                  tone={(health.uptime_pct ?? 100) < 99 ? "neg" : "pos"} />
              <KV k="سرعت پاسخ بروکر: معمول / کندترین ۵٪"
                  hint={<>عدد اول زمان معمول رفت‌وبرگشت سفارش است و عدد دوم بدترین حالت‌های
                    رایج. مثال: ۹۵ و ۴۱۰ میلی‌ثانیه یعنی از هر ۲۰ سفارش، یکی چهار برابر
                    کندتر پر می‌شود — و در آن فاصله قیمت تکان می‌خورد.</>}
                  v={`${health.median_latency_ms ?? "—"} / ${health.p95_latency_ms ?? "—"} میلی‌ثانیه`} />
              <KV k="اختلاف ساعت ما با ساعت بروکر"
                  hint="اگر ساعت‌ها جور نباشند نمی‌شود گفت کدام اتفاق قبل از کدام رخ داده، و ترتیب رویدادها بی‌اعتبار می‌شود."
                  v={`${health.clock_skew_ms ?? "—"} میلی‌ثانیه`} />
              <KV k="طولانی‌ترین قطعی اخیر"
                  hint="اگر یک معامله کلاً کوتاه‌تر از این مدت زندگی کند، ممکن است کل عمرش در یک قطعی بگذرد."
                  v={`${((health.longest_outage_sec ?? 0) / 60).toFixed(1)} دقیقه`} />
              {(health.warnings ?? []).map((w: string, i: number) => (
                <Banner key={i} tone="warn" icon="⚠">{w}</Banner>
              ))}
            </div>
          </Card>
        </div>
      </div>

      <div className="grid g3">
        <Card title="معامله‌های باز" sub={`${positions.length} معامله باز است`}
              hint={<>معامله‌هایی که هنوز بسته نشده‌اند. سود و زیانشان تا لحظه بسته شدن قطعی
                نیست و می‌تواند برعکس شود.</>}>
          {positions.length === 0 ? (
            <div className="empty">هیچ معامله بازی وجود ندارد</div>
          ) : (
            <div className="stack gap12">
              {positions.map((p) => {
                const r = Number(p.r_multiple ?? 0);
                return (
                  <div key={p.instrument} className="stack gap4">
                    <div className="row gap8" style={{ justifyContent: "space-between" }}>
                      <span className="row gap6">
                        <span className="mono" style={{ fontWeight: 600 }}>{p.instrument}</span>
                        <Chip tone={p.side === "BUY" ? "pos" : "neg"}>
                          {p.side === "BUY" ? "خرید" : "فروش"} {p.lots}
                        </Chip>
                      </span>
                      <span className={`num ${Number(p.unrealised) >= 0 ? "pos" : "neg"}`}
                            title="سود یا زیان این معامله تا همین لحظه — هنوز قطعی نشده">
                        {Number(p.unrealised) >= 0 ? "+" : ""}{money(Number(p.unrealised))}
                      </span>
                    </div>
                    <div className="row gap8 fs11 muted" style={{ justifyContent: "space-between" }}>
                      <span className="mono" title="قیمت ورود ← قیمت فعلی">
                        {p.entry_price} → {p.current_price}
                      </span>
                      <span className="num"
                            title="چند برابر مبلغی که برای این معامله ریسک شده بود: ‎+۱R یعنی به اندازه همان مبلغ سود، ‎−۱R یعنی به اندازه همان مبلغ ضرر">
                        {r >= 0 ? "+" : ""}{r.toFixed(2)}R
                      </span>
                    </div>
                    <div className="meter">
                      <i style={{ width: `${Math.min(100, Math.abs(r) * 40)}%`,
                                  background: r >= 0 ? "var(--pos)" : "var(--neg)" }} />
                    </div>
                    {!p.broker_stop_confirmed && (
                      <Banner tone="neg" icon="⚠">
                        حد ضرر این معامله نزد بروکر ثبت نشده — اگر اینترنت قطع شود، بی‌محافظ است
                      </Banner>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </Card>

        <Card title="ربات با فرصت‌هایی که دید چه کرد"
              sub={`${decisions.length} تصمیم اخیر`}
              hint={<>هر بار که یک استراتژی فرصتی پیدا می‌کند، قواعد ایمنی آن را بررسی
                می‌کنند. این نوار نشان می‌دهد چند تا اجرا شد و چند تا جلویش گرفته شد.</>}>
          <StackedRatio parts={actionMix} height={12} />
          <div className="mt16 card-sub">
            بیشترین دلیل‌هایی که جلوی معامله را گرفته‌اند
            <Hint text={<>هر خط یک قاعده ایمنی است که از قبل نوشته شده. عدد کنارش می‌گوید این
              قاعده چند بار جلوی یک معامله را گرفته است.</>} />
          </div>
          <div className="mt8"><BarsH data={vetoCounts} valueFmt={(v) => String(Math.round(v))} /></div>
          <Disclosure summary="چرا اینقدر زیاد جلوی معامله گرفته می‌شود؟">
            این عمدی است: قواعد ایمنی بیشتر از آنکه اجازه بدهند، جلو می‌گیرند. همه این قاعده‌ها
            پیش از شروع کار نوشته و قفل شده‌اند، نه بعد از دیدن نتیجه. سامانه‌ای که هر فرصتی را
            معامله می‌کند، اصلاً کنترل خطر ندارد — فقط یک استراتژی دارد.
          </Disclosure>
        </Card>

        <Card title="آخرین تصمیم‌ها"
              hint={<>هر ردیف یک فرصت است که ربات دیده، همراه با نتیجه‌اش: انجام شد، منتظر
                تأیید ماند، یا قواعد ایمنی جلویش را گرفتند.</>}>
          <div className="stack gap12 scroll-y" style={{ maxHeight: 360 }}>
            {decisions.slice(0, 8).map((d, i) => (
              <div key={i} className="stack gap4">
                <div className="row gap6 wrap">
                  <Chip tone={d.action === "executed" ? "pos" : d.action === "vetoed" ? "neg" : "warn"}>
                    {d.action === "executed" ? "انجام شد"
                      : d.action === "vetoed" ? "جلویش گرفته شد" : "منتظر تأیید"}
                  </Chip>
                  <span className="mono fs12">{d.instrument}</span>
                  <span className="fs11 muted">{d.strategy}</span>
                  <span className="fs11 faint" style={{ marginInlineStart: "auto" }}>{ago(d.ts_ns)}</span>
                </div>
                <p className="fs12 muted" style={{ lineHeight: 1.7 }}>
                  {d.vetoes.length ? d.vetoes[0].message : d.rationale}
                </p>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}

/* Every reason a trade can be stopped, said as a short sentence a newcomer can
   read without a second lookup. */
export const VETO_FA: Record<string, string> = {
  kill_switch: "کلید توقف اضطراری زده شده", halted: "ربات متوقف است",
  unresolved_orders: "یک سفارش بلاتکلیف داریم", connectivity: "ارتباط با بروکر قطع بود",
  lifecycle: "این استراتژی هنوز اجازه پول واقعی ندارد",
  unknown_instrument: "این جفت‌ارز تعریف نشده",
  no_price: "قیمتی در دست نبود", stale_data: "قیمت‌ها قدیمی بودند",
  data_quality: "کیفیت داده بازار پایین بود",
  clock_skew: "ساعت ما با بروکر جور نبود",
  spread: "فاصله قیمت خرید و فروش خیلی زیاد بود",
  news_blackout: "نزدیک یک خبر مهم اقتصادی بودیم",
  reference_divergence: "قیمت بروکر با قیمت مرجع بازار نمی‌خواند",
  no_stop: "معامله حد ضرر نداشت", stop_too_tight: "حد ضرر خیلی نزدیک بود",
  stop_too_wide: "حد ضرر خیلی دور بود", stop_side: "حد ضرر سمت اشتباه گذاشته شده بود",
  reward_risk: "سود احتمالی به اندازه ریسکش نمی‌ارزید",
  cost_barrier: "هزینه‌ها این معامله را از پیش بازنده می‌کرد",
  max_drawdown: "حساب از سقف افت مجاز رد شده",
  daily_loss: "سقف ضرر امروز پر شده", weekly_loss: "سقف ضرر این هفته پر شده",
  monthly_loss: "سقف ضرر این ماه پر شده",
  profit_lock: "سود امروز به حد کافی رسیده و برای حفظش متوقف شدیم",
  frequency_day: "تعداد معامله‌های امروز پر شده",
  frequency_week: "تعداد معامله‌های این هفته پر شده",
  frequency_year: "تعداد معامله‌های امسال پر شده",
  entry_spacing: "از معامله قبلی خیلی کم گذشته بود",
  annual_cost: "با این آهنگ، هزینه سالانه از سقف می‌زند",
  ladder_halt: "پله کاهش ریسک به توقف رسیده",
  sizing: "حجم قابل محاسبه با ریسک درست نبود",
  max_positions: "تعداد معامله‌های باز پر است",
  per_instrument: "روی همین جفت‌ارز به اندازه کافی معامله باز است",
  leverage: "مجموع معامله‌ها نسبت به حساب زیادی بزرگ می‌شد",
  correlated_risk: "این معامله عملاً تکرار معامله‌های باز بود",
  currency_exposure: "روی این ارز به اندازه کافی شرط بسته‌ایم",
  margin: "پول آزاد حساب کافی نبود",
  broker_stop: "بروکر حد ضرر را ثبت نکرد", broker_reject: "بروکر سفارش را رد کرد",
  caution_multiplier: "درس‌های گذشته اجازه این حجم را نمی‌دادند",
  quarantine: "این جفت‌ارز موقتاً کنار گذاشته شده",
  malformed_intent: "دستور معامله ناقص بود", no_market: "بازار باز نبود",
  stale_proposal: "پیشنهاد کهنه شده بود",
  size_after_scaling: "پس از کم کردن ریسک، حجم به صفر می‌رسید",
  meta_filter: "فیلتر دوم اجازه نداد",
  meta_label: "فیلتر دوم این فرصت را ضعیف ارزیابی کرد",
  loss_streak_cooldown: "بعد از چند ضرر پشت سر هم، ربات در استراحت اجباری است",
  stress_gap: "اگر قیمت مثل بحران‌های تاریخی یک‌باره بپرد، ضرر از سقف مجاز می‌گذشت",
  stress_shrunk: "حجم کم شد تا یک جهش ناگهانی قیمت حساب را نابود نکند",
  stress_unpriced: "ریسک جهش قیمت برای یک معامله باز قابل محاسبه نبود",
  licence: "لایسنس اجازه معامله تازه نمی‌دهد",
  performance_guard: "این استراتژی به‌خاطر ضرر مداوم معلق شده",
  out_of_session: "خارج از ساعت‌های مجاز معامله",
  manual_live_disabled: "معامله دستی با پول واقعی خاموش است",
  malformed_ticket: "اطلاعات معامله ناقص بود",
  venue_stop_distance: "حد ضرر از حداقل فاصله بروکر نزدیک‌تر است",
  rolling_24h_loss: "سقف ضرر ۲۴ ساعت گذشته پر شده",
  unprotected_book: "یک معامله باز بدون حد ضرر ثبت‌شده داریم",
  portfolio_alarm: "یک هشدار ایمنی باز داریم",
  exposure_unconvertible: "ریسک به ارز حساب قابل محاسبه نبود",
  group_total_risk: "جمع ریسک همه حساب‌ها از سقف می‌زند",
  group_currency_exposure: "روی این ارز در همه حساب‌ها زیاد شرط بسته شده",
  group_visibility: "ریسک یکی از حساب‌های دیگر دیده نمی‌شود",
  missing_conversion: "نرخ تبدیل به ارز حساب در دست نبود",
};
