import React, { useMemo, useState } from "react";
import { BarsH, BarsV, Heatmap, Histogram, Scatter } from "../components/charts";
import {
  Banner, Card, Chip, Disclosure, Empty, Hint, KV, Seg, Tile, dt, fa, money,
} from "../components/ui";
import type { Snapshot, Trade } from "../types";

const MONTH_FA = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                  "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"];

/* Why a trade ended, said the way a person would say it out loud. */
const EXIT_FA: Record<string, string> = {
  stop_loss: "به حد ضرر خورد", take_profit: "به هدف سود رسید",
  partial_take: "بخشی از سود برداشته شد",
  trail_stop: "حد ضرر دنبال‌کننده بست", time_stop: "وقتش تمام شد",
  weekend_flat: "قبل از تعطیلی آخر هفته بسته شد",
  margin_stop_out: "بروکر به‌خاطر کمبود پول بست", end_of_backtest: "دوره آزمایش تمام شد",
  opposite_fill: "معامله مخالف آن را خنثی کرد", manual_close: "دستی بسته شد",
  manual_flatten: "با دستور «بستن همه» بسته شد",
};
const REGIME_FA: Record<string, string> = {
  trending: "بازار جهت‌دار", quiet_range: "بازار آرام",
  volatile_range: "بازار پرنوسان", stress: "بازار بحرانی",
};

export default function Journal({ snap }: { snap: Snapshot }) {
  const { trades, performance, monthly, monthlyInfo } = snap;
  const [filter, setFilter] = useState<"all" | "win" | "loss">("all");
  const [strategy, setStrategy] = useState<string>("all");
  const [selected, setSelected] = useState<Trade | null>(null);

  const strategies = useMemo(
    () => ["all", ...Array.from(new Set(trades.map((t) => t.strategy)))], [trades]);

  const filtered = useMemo(() => trades.filter((t) => {
    const r = Number(t.r_multiple);
    if (filter === "win" && r <= 0) return false;
    if (filter === "loss" && r > 0) return false;
    if (strategy !== "all" && t.strategy !== strategy) return false;
    return true;
  }).slice().reverse(), [trades, filter, strategy]);

  const rs = filtered.map((t) => Number(t.r_multiple));
  const exitData = useMemo(() => Object.entries(performance.exit_breakdown)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => ({ label: EXIT_FA[k] ?? k, value: v })), [performance.exit_breakdown]);

  const instData = useMemo(() => Object.entries(performance.by_instrument)
    .sort((a, b) => b[1].pnl - a[1].pnl)
    .map(([k, v]) => ({ label: k, value: v.pnl,
      color: v.pnl >= 0 ? "var(--pos)" : "var(--neg)",
      note: `${v.n} معامله · ${(v.win_rate * 100).toFixed(0)}٪ برنده` })),
    [performance.by_instrument]);

  const scatter = useMemo(() => filtered.slice(0, 220).map((t) => ({
    x: Math.abs(Number(t.max_adverse_r)), y: Number(t.max_favourable_r),
    label: `${t.instrument} · ${Number(t.r_multiple) >= 0 ? "+" : ""}${Number(t.r_multiple).toFixed(2)}R`,
    color: Number(t.r_multiple) >= 0 ? "var(--s1)" : "var(--s8)",
  })), [filtered]);

  const cumR = useMemo(() => {
    let acc = 0;
    return trades.map((t, i) => {
      acc += Number(t.r_multiple);
      return { x: i, y: acc };
    });
  }, [trades]);

  return (
    <div className="stack gap16">
      <div className="grid g4">
        <Card><Tile label="چند معامله بسته شده" value={performance.n_trades}
                    hint={<>تعداد ردیف‌های این صفحه. عدد زیر آن می‌گوید چندتای این‌ها واقعاً
                      مستقل بوده‌اند: معامله‌هایی که هم‌زمان و هم‌جهت باز می‌شوند، در عمل یک
                      آزمایش‌اند نه چند تا.</>}
                    note={`از این‌ها حدود ${performance.effective_n} تا واقعاً مستقل بوده‌اند`} /></Card>
        <Card><Tile label="چند درصد معامله‌ها برنده بوده" term="نرخ برد"
                    hint={<>این عدد به‌تنهایی هیچ چیزی درباره سودآوری نمی‌گوید. ۴۰٪ برد با
                      بردهای دو برابر باخت‌ها سودده است؛ ۷۰٪ برد با بردهای خیلی کوچک
                      زیان‌ده.</>}
                    value={`${(performance.win_rate * 100).toFixed(1)}٪`}
                    note={`بردها به‌طور متوسط ${performance.payoff_ratio.toFixed(2)} برابر باخت‌ها بوده‌اند`} /></Card>
        <Card><Tile label="به‌طور متوسط هر معامله چقدر داده" term="امید ریاضی"
                    hint={<>بر حسب «چند برابر مبلغ ریسک‌شده». ‎+۰٫۰۸R یعنی هر معامله به‌طور
                      میانگین ۸٪ مبلغ ریسک‌شده سود داده — با ریسک ۵۰ دلار، یعنی ۴ دلار در هر
                      معامله. منفی بودنش یعنی هر معامله اضافه، پول کم می‌کند.</>}
                    value={<span className={performance.expectancy_r >= 0 ? "pos" : "neg"}>
                      {performance.expectancy_r >= 0 ? "+" : ""}{performance.expectancy_r.toFixed(3)}R</span>}
                    note={`به ازای هر ۱ دلار ضرر، ${performance.profit_factor.toFixed(2)} دلار سود`} /></Card>
        <Card><Tile label="چقدر بابت هزینه معامله پرداخته‌ایم"
                    hint={<>جمع اختلاف قیمت خرید و فروش، کارمزد و بهره شبانه. این پول از حساب
                      رفته، چه معامله‌ها سودده بوده باشند چه نه.</>}
                    value={money(performance.total_cost)}
                    note={`یعنی ${performance.cost_drag_pct.toFixed(2)}٪ از کل حساب`} /></Card>
      </div>

      {performance.notes.length > 0 && (
        <Banner tone="warn" icon="⚠">
          <div style={{ marginBottom: 4 }}>
            <strong>نکته‌هایی که باید کنار عددهای بالا خوانده شوند:</strong>
          </div>
          <ul style={{ margin: 0, paddingInlineStart: 18 }}>
            {performance.notes.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        </Banner>
      )}

      <div className="grid g2">
        <Card title="نتیجه معامله‌ها چطور پخش شده"
              hint={<>هر ستون می‌گوید چند معامله نتیجه‌ای در آن حدود داشته‌اند. محور پایین بر
                حسب «چند برابر مبلغ ریسک‌شده» است: ۰ یعنی سر به سر، ‎−۱ یعنی یک واحد ریسک
                از دست رفته، ‎+۲ یعنی دو برابرش سود.</>}
              sub="به دنباله سمت چپ نگاه کنید: ضررهای بزرگ و نادر همان چیزی‌اند که نمره‌های عملکرد پنهانشان می‌کنند">
          <Histogram values={rs} />
          <div className="kv-grid c4 mt16">
            <KV k="میانگین بردها" v={`${performance.avg_win_r.toFixed(2)}R`}
                hint="یعنی یک معامله برنده به‌طور متوسط چند برابر مبلغ ریسک‌شده سود داده." />
            <KV k="میانگین باخت‌ها" v={`${performance.avg_loss_r.toFixed(2)}R`}
                hint="‎−۱ یعنی دقیقاً به اندازه مبلغی که ریسک شده بود. بدتر از ‎−۱ یعنی قیمت از حد ضرر پریده." />
            <KV k="در ۹۵٪ مواقع بدتر نبوده" v={`${performance.var_95_r.toFixed(2)}R`}
                hint="به این «VaR ۹۵٪» می‌گویند: در ۹۵ مورد از ۱۰۰ مورد، ضرر از این عدد بیشتر نشده." />
            <KV k="میانگین آن ۵٪ بدتر" v={`${performance.cvar_95_r.toFixed(2)}R`}
                hint="به این «CVaR ۹۵٪» می‌گویند و همیشه بدتر از عدد کناری است: وقتی اوضاع بد می‌شود، چقدر بد می‌شود." />
          </div>
        </Card>

        <Card title="هر معامله وسط راه چقدر ضرر و چقدر سود نشان داد"
              hint={<>هر نقطه یک معامله است. محور افقی: بیشترین ضرری که وسط راه دید. محور
                عمودی: بیشترین سودی که وسط راه دید. نقطه‌های بالا و چپ یعنی سود خوبی روی میز
                بوده که گرفته نشده.</>}
              sub="نقطه‌های قرمزِ بالا دردناک‌ترین‌اند: معامله‌هایی که در سود بودند و با ضرر بسته شدند">
          <Scatter points={scatter} height={250}
                   xLabel="بیشترین ضرر وسط راه (R)"
                   yLabel="بیشترین سود وسط راه (R)" />
          <div className="kv-grid c3 mt12">
            <KV k="میانگین بیشترین ضرر وسط راه"
                hint="اگر این عدد به ‎−۱ نزدیک باشد، یعنی معامله‌ها معمولاً تا نزدیکی حد ضرر می‌روند و بعد برمی‌گردند."
                v={`${performance.avg_mae_r.toFixed(2)}R`} />
            <KV k="میانگین بیشترین سود وسط راه"
                hint="این عدد می‌گوید چقدر سود در دسترس بوده، فارغ از اینکه در نهایت چقدرش گرفته شده."
                v={`${performance.avg_mfe_r.toFixed(2)}R`} />
            <KV k="چند درصد آن سود گرفته شد"
                hint={<>مثال: اگر به‌طور متوسط ۱٫۱R سود روی میز بوده و در نهایت ۰٫۳R گرفته
                  شده، این عدد حدود ۲۷٪ است.</>}
                v={`${(performance.edge_efficiency * 100).toFixed(0)}٪`} />
          </div>
          <Disclosure summary="این عدد آخر چه چیزی را لو می‌دهد">
            اگر معامله‌ها در مجموع سودده باشند ولی این درصد پایین باشد، مشکل در <strong>زمان
            خروج</strong> است نه در انتخاب ورود: ربات فرصت‌های درستی پیدا می‌کند ولی خیلی زود
            یا خیلی دیر بیرون می‌آید. این دقیقاً همان چیزی است که بخش یادگیرنده دنبالش می‌گردد.
          </Disclosure>
        </Card>
      </div>

      <div className="grid g3">
        <Card title="معامله‌ها چطور تمام شده‌اند"
              hint={<>هر خط یک دلیل بسته شدن است و عدد کنارش تعداد دفعات. اگر «به حد ضرر خورد»
                خیلی بیشتر از بقیه باشد، یعنی یا ورودها زود هستند یا حد ضررها خیلی نزدیک.</>}>
          <BarsH data={exitData} valueFmt={(v) => String(Math.round(v))} />
        </Card>
        <Card title="روی هر جفت‌ارز چقدر سود یا ضرر شده"
              hint="عددها بر حسب دلار هستند؛ نوار به سمت راست یعنی سود و به سمت چپ یعنی ضرر.">
          <BarsH data={instData} valueFmt={(v) => money(v, 0)} />
        </Card>
        <Card title="هر ماه چند درصد سود یا ضرر داشته"
              hint={<>هر خانه یک ماه است. ۱٫۲ یعنی آن ماه ۱٫۲٪ به حساب اضافه شده و ‎−۰٫۸ یعنی
                ۰٫۸٪ کم شده. خانه خالی یعنی آن ماه داده‌ای نداریم.</>}
              sub="بر حسب درصد، به تفکیک ماه‌های سال شمسی">
          {monthly.length === 0 ? (
            <Empty>
              {monthlyInfo.source === "unavailable"
                ? "این جدول از سرور خوانده نشد؛ صفحه را تازه کنید."
                : "هنوز داده‌ای ثبت نشده. ربات از این نسخه به بعد پول حساب را روزبه‌روز ثبت می‌کند و این جدول ماه‌به‌ماه پر می‌شود."}
            </Empty>
          ) : (
            <Heatmap
              rows={monthly.map((m) => String(m.year))}
              cols={["فرو", "ارد", "خرد", "تیر", "مرد", "شهر", "مهر", "آبا", "آذر", "دی", "بهم", "اسف"]}
              cells={monthly.map((m) => m.months)}
              fmt={(v) => v.toFixed(1)} />
          )}
          {monthlyInfo.source === "ledger" && monthly.length > 0 && (
            <div className="fs11 faint mt8" style={{ lineHeight: 1.8 }}>
              از پول حساب در پایان هر ماه، نسبت به پایان ماه قبل. ثبت از{" "}
              {monthlyInfo.since_ns ? dt(monthlyInfo.since_ns) : "—"}.
              {monthlyInfo.partial.length > 0 && <>
                {monthlyInfo.partial.length === 1 ? " ماه " : " ماه‌های "}
                {monthlyInfo.partial.map((m) => `${MONTH_FA[m.month - 1]} ${fa(m.year)}`).join("، ")}
                {monthlyInfo.partial.length === 1
                  ? " کامل نیست: از اولین روزِ ثبت‌شده (یا از روز عوض شدن حساب) حساب شده است."
                  : " کامل نیستند: از اولین روزِ ثبت‌شده (یا از روز عوض شدن حساب) حساب شده‌اند."}</>}
              {" "}واریز و برداشت از این عددها جدا نشده است.
            </div>
          )}
        </Card>
      </div>

      <Card title="فهرست کامل معامله‌ها"
            hint="روی هر ردیف بزنید تا تحلیل کامل آن معامله باز شود."
            sub={`${filtered.length} معامله · روی هر ردیف بزنید تا جزئیاتش را ببینید`}
            actions={
              <div className="row gap8 wrap">
                <select className="select" style={{ width: 170 }} value={strategy}
                        onChange={(e) => setStrategy(e.target.value)}>
                  {strategies.map((s) => (
                    <option key={s} value={s}>{s === "all" ? "همه استراتژی‌ها" : s}</option>
                  ))}
                </select>
                <Seg value={filter} onChange={setFilter}
                     options={[{ value: "all", label: "همه" },
                               { value: "win", label: "فقط سودده" },
                               { value: "loss", label: "فقط زیان‌ده" }]} />
              </div>
            }>
        {filtered.length === 0 ? <Empty>هیچ معامله‌ای با این فیلتر پیدا نشد</Empty> : (
          <div className="table-wrap" style={{ maxHeight: 460, overflowY: "auto" }}>
            <table className="t">
              <thead>
                <tr>
                  <th>شماره</th><th>جفت‌ارز</th><th>خرید یا فروش</th>
                  <th className="n">حجم (لات)</th>
                  <th className="n">قیمت ورود</th><th className="n">قیمت خروج</th>
                  <th className="n">
                    حرکت (پیپ)
                    <Hint text="چند پله قیمت به نفع یا ضرر ما حرکت کرد. پیپ کوچک‌ترین پله حرکت قیمت است." />
                  </th>
                  <th className="n">
                    چند برابر ریسک
                    <Hint text={<>نتیجه نسبت به مبلغی که ریسک شده بود. ‎+۲ یعنی دو برابر آن
                      مبلغ سود، ‎−۱ یعنی همان مبلغ ضرر.</>} />
                  </th>
                  <th className="n">سود/زیان (دلار)</th><th>چطور تمام شد</th>
                  <th>حال بازار</th><th className="n">چقدر باز بود</th><th>تاریخ بسته شدن</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((t) => {
                  const r = Number(t.r_multiple);
                  return (
                    <tr key={t.trade_id} onClick={() => setSelected(t)} style={{ cursor: "pointer" }}>
                      <td className="mono fs11 faint">{t.trade_id}</td>
                      <td className="mono">{t.instrument}</td>
                      <td><Chip tone={t.side === "BUY" ? "pos" : "neg"}>
                        {t.side === "BUY" ? "خرید" : "فروش"}</Chip></td>
                      <td className="n">{t.lots}</td>
                      <td className="n">{t.entry_price}</td>
                      <td className="n">{t.exit_price}</td>
                      <td className={`n ${Number(t.pnl_pips) >= 0 ? "pos" : "neg"}`}>{t.pnl_pips}</td>
                      <td className={`n ${r >= 0 ? "pos" : "neg"}`}>{r >= 0 ? "+" : ""}{r.toFixed(2)}</td>
                      <td className={`n ${Number(t.pnl) >= 0 ? "pos" : "neg"}`}>{money(Number(t.pnl))}</td>
                      <td className="fs12">{EXIT_FA[t.exit_reason] ?? t.exit_reason}</td>
                      <td className="fs12 muted">{REGIME_FA[t.regime] ?? t.regime}</td>
                      <td className="n">{(t.duration_sec / 3600).toFixed(1)}h</td>
                      <td className="fs11 muted nowrap">{dt(t.closed_ns)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {selected && (
        <Card title={`تحلیل معامله ${selected.trade_id}`}
              sub={`${selected.instrument} · ${selected.strategy}`}
              actions={<button className="btn ghost sm" onClick={() => setSelected(null)}>بستن</button>}>
          <Autopsy trade={selected} />
        </Card>
      )}
    </div>
  );
}

function Autopsy({ trade }: { trade: Trade }) {
  const r = Number(trade.r_multiple);
  const mae = Number(trade.max_adverse_r);
  const mfe = Number(trade.max_favourable_r);
  const capture = mfe > 0 ? r / mfe : r >= 0 ? 1 : 0;

  let mode = "", note = "";
  if (trade.exit_reason === "stop_loss" && r < -1.15) {
    mode = "ضرر بیشتر از حد ضرر";
    note = "ضرر از چیزی که برنامه‌ریزی شده بود بیشتر شد، چون قیمت از روی حد ضرر پرید. حد ضرر یک درخواست است، نه تضمین مبلغ — در بازار پرتلاطم می‌تواند بدتر پر شود.";
  } else if (trade.exit_reason === "stop_loss" && mfe >= 1.0) {
    mode = "سودی که پس داده شد";
    note = "این معامله یک‌بار بیش از یک برابر مبلغ ریسک‌شده در سود بود و در نهایت با ضرر بسته شد. اگر حد ضرر زودتر به نقطه ورود منتقل می‌شد، این معامله در بدترین حالت سر به سر تمام می‌شد.";
  } else if (trade.exit_reason === "stop_loss" && mae <= -0.95 && mfe >= 0.5) {
    mode = "حد ضرر خورد، بعد بازار برگشت";
    note = "قیمت دقیقاً بعد از بسته شدن معامله، به جهت درست حرکت کرد. حد ضرر دورتر (با حجم کمتر، تا مبلغ ریسک همان بماند) این یکی را نجات می‌داد — ولی یک معامله هیچ‌وقت قاعده نمی‌سازد.";
  } else if (r > 0 && mae > -0.45) {
    mode = "برد تمیز";
    note = "بدون اینکه وسط راه ضرر عمیقی نشان بدهد، به هدف رسید.";
  } else if (mfe >= 0.75 && r <= 0) {
    mode = "هدف بیش از حد دور بود";
    note = "بیشتر راه تا هدف طی شد و بعد برگشت. اگر بخشی از سود در میانه راه برداشته می‌شد، دست‌کم آن بخش قطعی می‌شد.";
  } else if (r > 0) {
    mode = "برد، ولی با دلهره";
    note = "در نهایت سودده شد، اما وسط راه ضرر قابل‌توجهی نشان داد.";
  } else {
    mode = "ضرر ساده";
    note = "از همان ابتدا خلاف جهت ما حرکت کرد و به حد ضرر رسید. این حالت طبیعی‌ترین شکل ضرر است.";
  }

  return (
    <div className="stack gap16">
      <div className="kv-grid c4">
        <KV k="نتیجه نهایی" v={`${r >= 0 ? "+" : ""}${r.toFixed(2)}R`} tone={r >= 0 ? "pos" : "neg"}
            hint="بر حسب چند برابر مبلغی که روی این معامله ریسک شده بود." />
        <KV k="بیشترین ضرری که وسط راه دید" v={`${mae.toFixed(2)}R`}
            hint="نزدیک بودن این عدد به ‎−۱ یعنی معامله تا لبه حد ضرر رفته بود." />
        <KV k="بیشترین سودی که وسط راه دید" v={`${mfe.toFixed(2)}R`}
            hint="یعنی حداکثر چقدر سود روی میز بود، فارغ از نتیجه نهایی." />
        <KV k="چند درصد آن سود گرفته شد" v={`${(capture * 100).toFixed(0)}٪`}
            hint="مثال: اگر سود در دسترس ۲R بوده و در نهایت ۰٫۵R گرفته شده، این عدد ۲۵٪ است." />
      </div>
      <div>
        <Chip tone={r >= 0 ? "pos" : "neg"}>{mode}</Chip>
        <p className="fs13 mt8" style={{ lineHeight: 1.9 }}>{note}</p>
      </div>
      <div className="kv-grid c3">
        <KV k="کارمزد بروکر" v={money(Number(trade.commission))}
            hint="مبلغ ثابتی که بروکر بابت باز و بسته کردن این معامله گرفته." />
        <KV k="بهره شبانه" v={money(Number(trade.financing))}
            hint="بابت هر شبی که معامله باز مانده، مبلغی کم یا اضافه می‌شود. روی معامله‌های چندروزه این عدد جمع می‌شود." />
        <KV k="چند ساعت باز بود" v={`${(trade.duration_sec / 3600).toFixed(1)} ساعت`} />
      </div>
      <Banner tone="flat" icon="ℹ">
        یک معامله هیچ‌وقت به‌تنهایی قاعده نمی‌سازد. راه درست این است: اول یک الگو در ده‌ها
        معامله تکرار شود، بعد با آزمون آماری سنجیده شود، بعد به یک <strong>پیشنهاد تغییر</strong>
        تبدیل شود، بعد در یک اجرای آزمایشی سنجیده شود، و آخر سر <strong>شما</strong> آن را
        تأیید کنید. هیچ میان‌بری در این مسیر وجود ندارد.
      </Banner>
    </div>
  );
}
