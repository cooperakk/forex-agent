import React from "react";
import { BarsV, DivergingBars, Gauge, LineChart } from "../components/charts";
import { Banner, Card, Chip, Disclosure, Hint, KV, Tile, fa, money, pct } from "../components/ui";
import type { Snapshot } from "../types";

export default function Risk({ snap }: { snap: Snapshot }) {
  const { risk, performance, status, equity } = snap;
  const eq = Number(status.account.equity ?? 0);
  const lim = risk.limits;
  // The capital table is computed at this risk per trade; say which.
  const riskPct = snap.capitalRiskPct;
  const riskIsConfig = Number(snap.config?.risk?.risk_per_trade_pct) === riskPct;
  // A worked example that always shows the smallest trade overshooting: a
  // 50-pip stop at 0.01 lot risks $5, so the account must be small enough
  // that $5 is more than the intended risk.
  const exampleEquity = riskPct < 1 ? 500 : 200;
  const example = { equity: exampleEquity, actualPct: (5 / exampleEquity) * 100 };

  const ladderPoints = [{ x: 0, y: 1 }, ...risk.ladder.map((l) => ({ x: l.drawdown_pct, y: l.risk_multiplier }))];
  const currentStep = risk.ladder.filter((l) => risk.drawdown_pct >= l.drawdown_pct).slice(-1)[0];

  return (
    <div className="stack gap16">
      {risk.alarms.length > 0 && (
        <Banner tone="neg" icon="⚠">
          <div style={{ marginBottom: 4 }}>
            <strong>یک یا چند سقف ایمنی به حد خودشان نزدیک یا از آن رد شده‌اند:</strong>
          </div>
          <ul style={{ margin: 0, paddingInlineStart: 18 }}>
            {risk.alarms.map((a, i) => <li key={i}>{a.message}</li>)}
          </ul>
        </Banner>
      )}

      <Banner tone="flat" icon="ℹ">
        این صفحه ترمزهای سامانه را نشان می‌دهد. همه این عددها پیش از شروع کار تعیین و قفل
        شده‌اند. هیچ استراتژی و هیچ بخش یادگیرنده‌ای نمی‌تواند آن‌ها را شل کند — تغییرشان فقط
        با دست شما و با کد شش‌رقمی ممکن است.
      </Banner>

      <div className="grid g4">
        <Card>
          <div className="row" style={{ justifyContent: "center" }}>
            <Gauge value={risk.drawdown_pct} max={Number(lim.max_drawdown_halt_pct)}
                   danger={Number(lim.max_drawdown_halt_pct) * 0.8}
                   label="افت حساب از رکوردش (٪)" fmt={(v) => v.toFixed(1)} />
          </div>
          <div className="fs11 muted" style={{ textAlign: "center", marginTop: 6 }}>
            رسیدن به عدد سمت راست، ربات را کاملاً متوقف می‌کند
            <Hint text={<>فاصله پول حساب تا بالاترین رقمی که تا امروز داشته. مثال: از ۱۱٬۰۰۰
              دلار به ۱۰٬۴۵۰ دلار یعنی افت ۵٪.</>} />
          </div>
        </Card>
        <Card>
          <div className="row" style={{ justifyContent: "center" }}>
            <Gauge value={Math.max(0, -risk.day_pnl_pct)} max={Number(lim.daily_loss_limit_pct)}
                   danger={Number(lim.daily_loss_limit_pct) * 0.8}
                   label="ضرر امروز (٪ حساب)" fmt={(v) => v.toFixed(2)} />
          </div>
          <div className="fs11 muted" style={{ textAlign: "center", marginTop: 6 }}>
            با پر شدن این سقف، تا فردا معامله تازه‌ای باز نمی‌شود
            <Hint text={<>مثال: سقف ۲٪ روی حساب ۱۰٬۰۰۰ دلاری یعنی ۲۰۰ دلار. با رسیدن ضرر روز
              به ۲۰۰ دلار، بقیه روز تعطیل است.</>} />
          </div>
        </Card>
        <Card>
          <div className="row" style={{ justifyContent: "center" }}>
            <Gauge value={risk.gross_leverage} max={Number(lim.max_gross_leverage)}
                   danger={Number(lim.max_gross_leverage) * 0.8}
                   label="بزرگی معامله‌ها نسبت به حساب" fmt={(v) => `${v.toFixed(1)}×`} />
          </div>
          <div className="fs11 muted" style={{ textAlign: "center", marginTop: 6 }}>
            به این «اهرم» می‌گویند
            <Hint text={<>۳ برابر یعنی با ۱۰٬۰۰۰ دلار حساب، ۳۰٬۰۰۰ دلار معامله باز دارید. هر
              حرکت ۱ درصدی بازار، ۳ درصد حساب را جابه‌جا می‌کند — در هر دو جهت.</>} />
          </div>
        </Card>
        <Card>
          <div className="row" style={{ justifyContent: "center" }}>
            <Gauge value={risk.trades_today} max={Number(lim.max_trades_per_day)}
                   danger={Number(lim.max_trades_per_day)}
                   label="تعداد معامله‌های امروز" fmt={(v) => String(Math.round(v))} />
          </div>
          <div className="fs11 muted" style={{ textAlign: "center", marginTop: 6 }}>
            تعداد معامله خودش یک سقف ایمنی است
            <Hint text={<>هر معامله هزینه دارد. معامله زیاد یعنی هزینه زیاد، حتی اگر همه
              معامله‌ها درست باشند — به همین دلیل تعدادشان مثل اهرم سقف دارد.</>} />
          </div>
        </Card>
      </div>

      <div className="grid g2">
        <Card title="روی هر ارز چقدر شرط بسته‌ایم"
              hint={<>هر معامله ارزی در واقع دو ارز دارد. اگر چند معامله جدا همگی به یک ارز
                اشاره کنند، روی کاغذ چند معامله است ولی در عمل یک شرط بزرگ.</>}
              sub="چهار خرید که «متفاوت» به نظر می‌رسند می‌توانند در واقع یک شرط واحد علیه دلار باشند">
          <DivergingBars
            data={risk.currency_exposure.map((e) => ({
              label: e.currency, value: Number(e.net_risk),
              note: e.contributors.join("، "),
            }))}
            valueFmt={(v) => money(v, 0)}
            posLabel="روی بالا رفتن این ارز" negLabel="روی پایین آمدن این ارز" />
          <div className="mt16 stack gap6">
            {risk.currency_exposure.map((e) => {
              const over = e.net_risk_pct > Number(lim.max_currency_exposure_pct);
              return (
                <div key={e.currency} className="row gap8 fs12"
                     style={{ justifyContent: "space-between" }}>
                  <span className="mono">{e.currency}</span>
                  <span className="muted grow" style={{ fontSize: 11 }}>
                    {e.contributors.join("، ")}
                  </span>
                  <span className={`num ${over ? "neg" : ""}`}
                        title="درصد فعلی از کل حساب / سقف مجاز">
                    {e.net_risk_pct.toFixed(2)}٪ / {lim.max_currency_exposure_pct}٪
                  </span>
                </div>
              );
            })}
          </div>
          <Disclosure summary="چرا این حساب‌وکتاب مهم است">
            فرض کنید چهار معامله باز دارید: خرید یورو/دلار، خرید پوند/دلار، خرید استرالیا/دلار و
            فروش دلار/ین. این چهار معامله به نظر متنوع می‌آیند، ولی هر چهار تا یک حرف می‌زنند:
            «دلار پایین می‌رود». اگر دلار بالا برود، هر چهار تا با هم ضرر می‌دهند. این جدول
            دقیقاً همین را جمع می‌زند و سقف می‌گذارد. محاسبه‌اش هیچ حدس و تخمینی ندارد — فقط
            تجزیه هر معامله به دو ارزش سازنده‌اش است.
          </Disclosure>
        </Card>

        <Card title="هر چه حساب بیشتر افت کند، معامله‌ها کوچک‌تر می‌شوند"
              hint={<>یک جدول از پیش نوشته‌شده: هر پله افت حساب، اندازه معامله‌های بعدی را
                کم می‌کند تا در نهایت به صفر برسد.</>}
              sub="این جدول پیش از شروع کار قفل شده؛ عوض کردنش بعد از دیدن یک ضرر، یعنی شروع یک آزمایش تازه">
          <LineChart
            series={[{ name: "اندازه معامله نسبت به حالت عادی", color: "var(--ink)",
              points: ladderPoints.map((p) => ({ x: p.x, y: p.y, label: `افت ${p.x}٪` })) }]}
            height={190} zeroBaseline
            yFmt={(v) => `×${v.toFixed(2)}`}
            xFmt={(x) => `${x}٪`}
            valueFmt={(v) => `×${v.toFixed(2)}`} />
          <div className="mt12 stack gap6">
            {risk.ladder.map((l, i) => {
              const active = currentStep && l.drawdown_pct === currentStep.drawdown_pct;
              return (
                <div key={i} className="row gap8 fs12" style={{ justifyContent: "space-between" }}>
                  <span>اگر افت حساب به {l.drawdown_pct}٪ یا بیشتر برسد</span>
                  <span className="row gap6">
                    <span className="num" title="اندازه هر معامله نسبت به حالت عادی">
                      ×{l.risk_multiplier.toFixed(2)}
                    </span>
                    {active && <Chip tone="warn">همین الان اینجاییم</Chip>}
                    {l.risk_multiplier === 0 && <Chip tone="neg">هیچ معامله تازه‌ای</Chip>}
                  </span>
                </div>
              );
            })}
          </div>
          <Banner tone="flat" icon="ℹ">
            دقت کنید چه چیزی کوچک می‌شود: <strong>مبلغی که در هر معامله ریسک می‌کنیم</strong>،
            نه فاصله حد ضرر. اگر به‌جای آن حد ضرر را دورتر می‌گذاشتیم تا حجم کمتر شود، ضرر
            احتمالی همان‌قدر بزرگ می‌ماند و فقط دیرتر اتفاق می‌افتاد.
          </Banner>
        </Card>
      </div>

      <div className="grid g2">
        <Card title="سقف‌هایی که همین حالا فعال‌اند"
              sub="هیچ استراتژی، هیچ مدل هوش مصنوعی و هیچ بخش یادگیرنده‌ای نمی‌تواند این‌ها را شل کند">
          <div className="kv-cols">
            <div className="stack">
              <KV k="در هر معامله چند درصد حساب ریسک می‌شود"
                  hint={<>مثال: ۰٫۵٪ روی حساب ۱۰٬۰۰۰ دلاری یعنی اگر حد ضرر بخورد، ۵۰ دلار از
                    دست می‌رود. همین عدد است که اندازه معامله را تعیین می‌کند، نه برعکس.</>}
                  v={`${lim.risk_per_trade_pct}٪`} />
              <KV k="بیشترین ضرر مجاز در یک روز" v={`${lim.daily_loss_limit_pct}٪`}
                  hint="با رسیدن به این حد، تا فردا هیچ معامله تازه‌ای باز نمی‌شود." />
              <KV k="بیشترین ضرر مجاز در یک هفته" v={`${lim.weekly_loss_limit_pct}٪`} />
              <KV k="بیشترین ضرر مجاز در یک ماه" v={`${lim.monthly_loss_limit_pct}٪`} />
              <KV k="افت حساب که ربات را کاملاً متوقف می‌کند"
                  hint="بعد از این حد، راه‌اندازی دوباره فقط با دست شما ممکن است."
                  v={`${lim.max_drawdown_halt_pct}٪`} />
            </div>
            <div className="stack">
              <KV k="بیشترین تعداد معامله باز هم‌زمان" v={String(lim.max_open_positions)} />
              <KV k="بیشترین تعداد معامله در یک روز" v={String(lim.max_trades_per_day)} />
              <KV k="بیشترین شرط‌بندی روی یک ارز" v={`${lim.max_currency_exposure_pct}٪`}
                  hint="جمع همه معامله‌هایی که در عمل به یک ارز اشاره می‌کنند، نمی‌تواند از این درصدِ حساب بیشتر شود." />
              <KV k="بیشترین ریسک روی معامله‌های شبیه هم" v={`${lim.max_correlated_risk_pct}٪`}
                  hint="جفت‌ارزهایی که تقریباً با هم حرکت می‌کنند، یک معامله شمرده می‌شوند." />
              <KV k="بیشترین بزرگی معامله‌ها نسبت به حساب" v={`${lim.max_gross_leverage}×`}
                  hint="۵ برابر یعنی با ۱۰٬۰۰۰ دلار حساب، حداکثر ۵۰٬۰۰۰ دلار معامله باز." />
            </div>
          </div>
        </Card>

        <Card title="آنچه واقعاً اتفاق افتاده"
              hint={<>این نمره‌ها فقط گذشته را خلاصه می‌کنند. هیچ‌کدام پیش‌بینی آینده نیستند و
                هیچ‌کدام قول سود نمی‌دهند.</>}
              sub="نمره‌های عملکرد بر اساس معامله‌های واقعاً انجام‌شده">
          <div className="kv-cols">
            <div className="stack">
              <KV k="نمره سود نسبت به نوسان" v={performance.sharpe.toFixed(2)}
                  hint={<>به این «شارپ» می‌گویند: سود را بر میزان بالا و پایین شدن حساب تقسیم
                    می‌کند. زیر ۱ یعنی سودی که گرفته شده حتی به اندازه نوسانی که تحمل شده هم
                    نبوده.</>} />
              <KV k="همان نمره، فقط با شمردن نوسان ضررده" v={performance.sortino.toFixed(2)}
                  hint="به این «سورتینو» می‌گویند. بالا رفتن ناگهانی حساب را جریمه نمی‌کند، فقط پایین آمدن را." />
              <KV k="نمره سود نسبت به بدترین افت" v={performance.calmar.toFixed(2)}
                  hint={<>به این «کالمار» می‌گویند: سود سالانه تقسیم بر بزرگ‌ترین افت حساب.
                    ۰٫۶۷ یعنی بابت هر ۱٪ افتی که تحمل شده، ۰٫۶۷٪ سود گرفته شده.</>} />
              <KV k="نمره عمق و طول افت‌ها" v={performance.ulcer_index.toFixed(2)}
                  hint={<>به این «شاخص آلسر» می‌گویند و هر چه کمتر بهتر است. دو حساب ممکن است
                    هر دو ۹٪ افت کرده باشند، ولی یکی دو هفته و دیگری شش ماه پایین مانده
                    باشد — دومی خیلی بدتر است.</>} />
              <KV k="شکل نتایج: بردهای کوچک یا ضررهای بزرگ؟"
                  v={performance.return_skew.toFixed(2)}
                  hint={<>به این «چولگی» می‌گویند. عدد منفی یعنی «برد کوچک زیاد، ضرر بزرگ کم» —
                    الگویی که مدت‌ها امن به نظر می‌رسد و بعد یک‌باره گران تمام می‌شود.</>}
                  tone={performance.return_skew < -0.7 ? "neg" : ""} />
            </div>
            <div className="stack">
              <KV k="بدترین افت حساب تا امروز" v={`${performance.max_drawdown_pct.toFixed(2)}٪`}
                  hint="مثال: ۹٪ یعنی در بدترین نقطه، از هر ۱۰٬۰۰۰ دلار ۹۰۰ دلار زیر رکورد حساب بوده‌اید." />
              <KV k="چقدر طول کشید تا به کف برسد"
                  v={`${performance.max_drawdown_duration_bars} کندل`}
                  hint="«کندل» واحد زمان نمودار است؛ در نمودار یک‌ساعته، هر کندل یک ساعت." />
              <KV k="چقدر طول کشید تا جبران شود"
                  hint="اگر نوشته «هنوز جبران نشده»، یعنی حساب هنوز به رکورد قبلی‌اش برنگشته است."
                  v={performance.time_to_recovery_bars === null ? "هنوز جبران نشده"
                     : `${performance.time_to_recovery_bars} کندل`} />
              <KV k="بیشترین ضرر پشت سر هم"
                  hint="مثال: ۷ باخت پیاپی با ریسک ۰٫۵٪ یعنی حدود ۳٫۵٪ از حساب. این اتفاق طبیعی است، نه نشانه خرابی."
                  v={String(performance.max_consecutive_losses)} />
              <KV k="چقدر نتایج پرت و غیرعادی دارد" v={performance.return_kurtosis.toFixed(2)}
                  hint="به این «کشیدگی» می‌گویند. بالای ۳ یعنی روزهای غافلگیرکننده بیشتر از حالت عادی رخ می‌دهند." />
            </div>
          </div>
          {performance.return_skew < -0.7 && (
            <Banner tone="warn" icon="⚠">
              شکل نتایج نگران‌کننده است: بردهای کوچک و پرتکرار، و ضررهای بزرگ و نادر. با این
              شکل، نمره‌های عملکرد خطر را کمتر از واقع نشان می‌دهند. چنین سامانه‌ای می‌تواند
              ماه‌ها آرام و موفق به نظر برسد و بعد در یک روز، سود چند ماه را پس بدهد.
            </Banner>
          )}
        </Card>
      </div>

      <Card title="برای اینکه فقط سر به سر شوید، چند درصد باید برنده باشید"
            hint={<>هر معامله از همان ابتدا مقداری در ضرر متولد می‌شود (اختلاف قیمت خرید و
              فروش، به‌علاوه کارمزد). هر چه هدف سود کوچک‌تر باشد، این هزینه سهم بزرگ‌تری از
              آن می‌گیرد و باید بیشتر برنده باشید.</>}
            sub="این جدول، پیش از نوشتن حتی یک خط استراتژی، تکلیف معامله‌های خیلی کوتاه را روشن می‌کند">
        <div className="table-wrap">
          <table className="t">
            <thead>
              <tr>
                <th className="n">
                  اندازه هدف و حد ضرر (پیپ)
                  <Hint text={<>پیپ کوچک‌ترین پله حرکت قیمت است. هدف ۱۰ پیپ یعنی معامله با
                    ۱۰ پله سود بسته می‌شود.</>} />
                </th>
                <th className="n">
                  با حساب کم‌هزینه (هزینه ۰٫۴۵ پیپ)
                  <Hint text="حسابی که کارمزد جدا می‌گیرد ولی اختلاف قیمت خرید و فروشش کم است." />
                </th>
                <th className="n">
                  با حساب معمولی (هزینه ۱ پیپ)
                  <Hint text="حسابی که کارمزد جدا ندارد و هزینه را داخل اختلاف قیمت خرید و فروش می‌گذارد." />
                </th>
                <th>نتیجه</th>
              </tr>
            </thead>
            <tbody>
              {snap.costTable.map((row) => (
                <tr key={row.target}>
                  <td className="n">{row.target}</td>
                  <td className={`n ${row.raw >= 60 ? "neg" : ""}`}>{row.raw.toFixed(1)}٪</td>
                  <td className={`n ${row.standard >= 60 ? "neg" : ""}`}>{row.standard.toFixed(1)}٪</td>
                  <td>
                    {row.standard >= 60
                      ? <Chip tone="neg">عملاً غیرممکن</Chip>
                      : row.standard >= 55 ? <Chip tone="warn">خیلی سخت</Chip>
                      : <Chip tone="pos">شدنی</Chip>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Banner tone="flat" icon="ℹ">
          عددهای ستون‌ها یعنی: «باید در این درصد از معامله‌ها برنده باشید تا فقط سر به سر
          شوید». هر چیزی بالای ۶۰٪ عملاً دست‌نیافتنی است. تنها راه فرار از این سد،
          <strong> بزرگ‌تر کردن هدف و صبورتر شدن</strong> است، نه بیشتر معامله کردن — چون هر
          معامله اضافه یک هزینه اضافه است. به همین دلیل در این سامانه تعداد معامله‌ها هم مثل
          اهرم سقف دارد.
          <div className="fs11 faint mt8">
            فرمول برای کسی که کنجکاو است: <span className="mono">p = (S + c) / (T + S)</span> —
            هدف T، حد ضرر S و هزینه رفت‌وبرگشت c، همه بر حسب پیپ.
          </div>
        </Banner>
      </Card>

      <Card title="برای هر فاصله حد ضرر، حداقل چقدر سرمایه لازم است"
            hint={<>کوچک‌ترین معامله ممکن ۰٫۰۱ لات است. اگر حساب خیلی کوچک باشد، همین
              کمینه هم از مبلغی که می‌خواستید ریسک کنید بزرگ‌تر می‌شود و دیگر نمی‌شود ریسک را
              دقیق کنترل کرد.</>}
            sub="ستون‌ها می‌گویند با هر فاصله حد ضرر، چقدر سرمایه لازم است تا ریسک هر معامله دقیق در بیاید">
        <BarsV data={snap.capitalTable.map((c) => ({
          label: `${c.stop}p`, value: c.minEquity, color: "var(--ink)" }))}
          height={170} valueFmt={(v) => money(v, 0)} zeroLine={false} />
        <div className="legend mt8">
          <span>عددهای زیر نمودار: فاصله حد ضرر بر حسب پیپ (مثلاً ۳۰p یعنی ۳۰ پیپ)</span>
          <span>ارتفاع هر ستون: حداقل سرمایه لازم، بر حسب دلار</span>
        </div>
        <Banner tone="flat" icon="ℹ">
          دو خواسته در جهت مخالف هم فشار می‌آورند: حد ضرر دورتر از نظر هزینه بهتر است، ولی
          سرمایه بیشتری می‌خواهد. اگر هر دو با هم حل نشوند، چیزی که اتفاق می‌افتد این است:
          روی حساب {faNum(example.equity)} دلاری می‌خواستید {faNum(riskPct)}٪ ریسک کنید
          ({faNum(example.equity * riskPct / 100)} دلار)، ولی با حد ضرر ۵۰ پیپ کوچک‌ترین حجم
          ممکن (۰٫۰۱ لات) خودش ۵ دلار ریسک دارد؛ یعنی در عمل {faNum(example.actualPct)}٪ ریسک
          می‌کنید — بی‌آنکه جایی نوشته شود.
          <div className="fs11 faint mt8">
            حساب این نمودار با ریسک {faNum(riskPct)}٪ در هر معامله است
            {riskIsConfig ? " (همان عددی که در تنظیمات شماست)" : ""}: حداقل سرمایه = فاصله حد ضرر
            × ۰٫۱ دلار (ارزش هر پیپِ ۰٫۰۱ لات) ÷ (۲۰٪ × درصد ریسک). یعنی کوچک‌ترین حجم ممکن
            حداکثر یک‌پنجم ریسکی باشد که می‌خواهید.
          </div>
        </Banner>
      </Card>
    </div>
  );
}

/** A number in Persian digits with the Persian decimal separator, no trailing zeros. */
function faNum(v: number) {
  return fa(String(+v.toFixed(2))).replace(".", "٫");
}
