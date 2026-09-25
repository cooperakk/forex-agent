import React from "react";
import { BarsV, Histogram } from "../components/charts";
import { Banner, Card, Chip, Disclosure, Empty, Hint, KV, Tile, dt, fa } from "../components/ui";
import type { Gate, ResearchSummary, Snapshot } from "../types";

export default function Research({ snap }: { snap: Snapshot }) {
  const { gates, cpcvSharpes, strategies, allocations, research } = snap;
  const blocking = gates.filter((g) => g.blocking);
  const failed = blocking.filter((g) => !g.passed);
  // The stored verdict decides, not a count of the gates that happen to be
  // present: an empty gate list would otherwise read "all passed".
  const accepted = !!research && research.accepted && failed.length === 0;

  return (
    <div className="stack gap16">
      {!research ? (
        <Banner tone="warn" icon="⏳">
          <strong>
            {research === undefined
              ? "نتیجهٔ آزمون پذیرش از سرور خوانده نشد."
              : "هنوز هیچ آزمون پذیرشی اجرا نشده است."}
          </strong>{" "}
          {research === undefined
            ? "صفحه را تازه کنید؛ اگر ماند، سرور را بررسی کنید (Diagnose). تا وقتی نتیجه‌ای خوانده نشود، این صفحه هیچ عددی را به‌عنوان نتیجهٔ شما نشان نمی‌دهد."
            : <>بنابراین هیچ استراتژی‌ای اجازهٔ معامله با پول واقعی ندارد؛ استراتژی‌ها فقط روی
              حساب تمرینی و دمو کار می‌کنند. آزمون پذیرش بعد از چند هفته کار روی دمو و با
              داده‌های واقعی بروکر اجرا می‌شود (فعلاً از خط فرمان:{" "}
              <span className="mono ltr">scripts/run_acceptance.py</span>). نتیجه‌اش همین‌جا
              نمایش داده می‌شود.</>}
        </Banner>
      ) : (
        <Banner tone={accepted ? "info" : "warn"} icon={accepted ? "✓" : "⏳"}>
          <strong>
            {accepted
              ? "همه آزمون‌ها عبور کردند"
              : `این استراتژی هنوز اثبات نشده است — ${failed.length} آزمون از ${blocking.length} آزمون الزامی رد شده‌اند`}
          </strong>
          {" "}
          {accepted
            ? "این یعنی اجازه رفتن به مرحله بعد، نه قول سود. هیچ آزمونی نمی‌تواند آینده را تضمین کند."
            : "تا وقتی این آزمون‌ها عبور نکنند، این استراتژی اجازه معامله با پول واقعی را ندارد. پایین‌تر، هر آزمون با دلیل رد شدنش آمده است."}
          <div className="fs11 faint mt8">
            آخرین اجرا: <span className="mono ltr">{research.strategy}</span>
            {research.created_ns ? <> · {dt(research.created_ns)}</> : null}
            {" "}· داده: {DATA_FA[research.data_label] ?? research.data_label}
          </div>
        </Banner>
      )}
      {research && research.current_config === false && (
        <Banner tone="warn" icon="↻">
          این نتیجه مال تنظیمات یا نسخه‌ای از برنامه است که دیگر اجرا نمی‌شود. برای تنظیمات
          فعلی، آزمون باید دوباره اجرا شود.
        </Banner>
      )}

      <Banner tone="flat" icon="ℹ">
        این صفحه به یک سؤال جواب می‌دهد: <strong>از کجا معلوم که سودِ گذشته شانسی نبوده؟</strong>{" "}
        اگر صد نفر صد استراتژی تصادفی بسازند، چند تایشان روی تاریخ گذشته عالی به نظر می‌رسند —
        بدون اینکه هیچ مزیتی داشته باشند. آزمون‌های این صفحه برای جدا کردن همین شانس از مزیت
        واقعی‌اند. اگر واژه‌ای را نمی‌شناسید، صفحه «واژه‌نامه ساده» همه‌شان را با مثال توضیح داده.
      </Banner>

      {research && <ResearchTiles research={research} blocking={blocking.length}
                                  failed={failed.length} />}

      {research && <Card title="فهرست آزمون‌ها و نتیجه هرکدام"
            hint={<>هر خط یک آزمون است. «عبور» یعنی آن شرط برآورده شده و «رد» یعنی نشده. حد
              لازم هر آزمون پیش از شروع تعیین شده، نه بعد از دیدن نتیجه.</>}
            sub="حد قبولی همه این آزمون‌ها پیش از اجرا نوشته و قفل شده — تا کسی نتواند بعد از دیدن نتیجه، حد قبولی را جابه‌جا کند">
        <div className="stack gap12">
          {gates.map((g) => (
            <div key={g.id} className="row gap12"
                 style={{ alignItems: "flex-start", padding: "10px 0",
                          borderBottom: "1px solid var(--hairline)" }}>
              <span style={{ flex: "0 0 60px" }}>
                <Chip tone={g.passed ? "pos" : g.blocking ? "neg" : "warn"}>
                  {g.passed ? "عبور" : "رد"}
                </Chip>
              </span>
              <span className="mono fs12 faint" style={{ flex: "0 0 44px" }}>{g.id}</span>
              <div className="grow stack gap4">
                <div className="row gap8 wrap" style={{ justifyContent: "space-between" }}>
                  <span className="fs13" style={{ fontWeight: 500 }}>{gateName(g)}</span>
                  <span className="fs12 muted nowrap">
                    <span className="faint">نتیجه: </span>
                    <span className="num">{g.observed}</span>
                    <span className="faint"> · حد لازم: </span>
                    <span className="num">{g.threshold}</span>
                  </span>
                </div>
                <p className="fs12 muted" style={{ lineHeight: 1.7 }}>{g.detail}</p>
                {!g.blocking && (
                  <span className="fs11 faint">
                    رد شدن در این آزمون به‌تنهایی جلوی کار را نمی‌گیرد
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      </Card>}

      <div className={research ? "grid g2" : "stack gap16"}>
        {research && <Card title="نتیجه روی هر برش مستقل از تاریخ چه بوده"
              hint={<>هر میله یک برش مستقل از تاریخ است. میله‌های بالای صفر یعنی آن برش سودده
                بوده و میله‌های زیر صفر یعنی زیان‌ده. اگر فقط یک عدد میانگین گزارش شود، این
                تصویر پنهان می‌ماند.</>}
              sub="یک عدد میانگین چیزی را ثابت نمی‌کند؛ این تصویر می‌گوید در چند درصد حالت‌ها جواب می‌داده">
          {cpcvSharpes.length === 0 ? (
            <Empty>این اجرا تاریخ را برش نزده بود (آزمون CPCV انجام نشد)</Empty>
          ) : (
            <>
              <BarsV data={cpcvSharpes.map((s, i) => ({
                label: String(i + 1), value: +s.toFixed(2),
                color: s >= 0 ? "var(--s1)" : "var(--s8)" }))}
                height={200} valueFmt={(v) => v.toFixed(1)} labelEvery={3} />
              <div className="kv-grid c3 mt12">
                <KV k="بدترین حالت‌ها" hint="نمره‌ای که ۱۰٪ برش‌ها از آن هم بدتر بوده‌اند."
                    v={quantile(cpcvSharpes, 0.1).toFixed(2)} />
                <KV k="حالت میانه" hint="نصف برش‌ها بهتر از این و نصف بدتر از این بوده‌اند."
                    v={quantile(cpcvSharpes, 0.5).toFixed(2)} />
                <KV k="بهترین حالت‌ها" hint="نمره‌ای که فقط ۱۰٪ برش‌ها از آن بهتر بوده‌اند."
                    v={quantile(cpcvSharpes, 0.9).toFixed(2)} />
              </div>
            </>
          )}
          <Disclosure summary="این روش چه کاری می‌کند که یک آزمایش ساده روی تاریخ نمی‌کند">
            یک آزمایش ساده، استراتژی را یک‌بار روی کل تاریخ اجرا می‌کند و یک عدد می‌دهد. آن
            عدد می‌تواند حاصل دو ماه خوش‌شانسی در وسط دوره باشد. اینجا به‌جای آن، تاریخ به
            بلوک‌های زیاد بریده می‌شود و هر ترکیب از بلوک‌ها یک‌بار نقش «آینده‌ای که ندیده‌ایم»
            را بازی می‌کند. بین بخش تمرین و بخش آزمون هم یک فاصله خالی گذاشته می‌شود تا
            اطلاعات از یکی به دیگری نشت نکند. نتیجه یک عدد نیست، یک تصویر است: سؤال از
            «آیا جواب می‌داد؟» به «<strong>چند درصد مواقع</strong> جواب می‌داد؟» تغییر می‌کند.
          </Disclosure>
        </Card>}

        <Card title="وقتی می‌گوییم نتیجه «معنادار» است، چقدر احتمال دارد درست باشد"
              hint={<>سخت‌گیری آماری یعنی چقدر شواهد لازم است تا یک نتیجه را بپذیریم. هر چه
                سخت‌گیرانه‌تر، احتمال اینکه نتیجه پذیرفته‌شده واقعاً درست باشد بیشتر.</>}
              sub="این جدول توضیح می‌دهد چرا در این سامانه سخت‌گیری از استاندارد رایج بیشتر است">
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr><th className="n">
                      سطح سخت‌گیری
                      <Hint text={<>به آن α می‌گویند. عدد کوچک‌تر یعنی سخت‌گیرتر. ۰٫۰۵ یعنی
                        «قبول می‌کنم که ۵٪ مواقع اشتباه کنم».</>} />
                    </th>
                    <th className="n">
                      چند درصد نتیجه‌های «معنادار» واقعاً درست‌اند
                      <Hint text={<>با این فرض واقع‌بینانه که پیش از آزمایش، فقط ۳٪ ایده‌های
                        معاملاتی درست‌اند.</>} />
                    </th>
                    <th>یعنی چه</th></tr>
              </thead>
              <tbody>
                <tr><td className="n">۰٫۰۵</td><td className="n neg">۲۳٫۶٪</td>
                    <td className="fs12">بیشتر احتمال دارد غلط باشد تا درست</td></tr>
                <tr><td className="n">۰٫۰۱</td><td className="n warn">۶۰٫۷٪</td>
                    <td className="fs12">مرزی — همین حد در این سامانه استفاده می‌شود</td></tr>
                <tr><td className="n">۰٫۰۰۱</td><td className="n pos">۹۳٫۹٪</td>
                    <td className="fs12">قابل اتکا</td></tr>
              </tbody>
            </table>
          </div>
          <Banner tone="flat" icon="ℹ">
            چرا ستون وسط اینقدر پایین است؟ چون بیشتر ایده‌های معاملاتی از اول غلط‌اند. اگر
            فقط ۳ ایده از هر ۱۰۰ ایده درست باشد، آنگاه با سخت‌گیری معمولِ ۰٫۰۵، از هر ۱۰۰
            نتیجه‌ای که «معنادار» اعلام می‌شود، حدود ۷۶ تا در واقع غلط‌اند. به همین دلیل اینجا
            سخت‌گیری ۰٫۰۱ است، و به همین دلیل تعداد نسخه‌هایی که امتحان شده‌اند باید از قبل
            <em> اعلام</em> شود.
          </Banner>
          <Disclosure summary="یک مثال عددی که همه‌چیز را روشن می‌کند">
            فرض کنید ۴۸ نسخه مختلف از یک استراتژی را امتحان می‌کنید، ولی هیچ‌کدام هیچ مزیت
            واقعی ندارند و همه‌شان فقط نویز تصادفی‌اند. حتی در این حالت، انتظار می‌رود
            <strong> بهترینِ آن ۴۸ نسخه</strong> نمره‌ای حدود ۱٫۱۹ بگیرد — فقط به‌خاطر شانس.
            اگر نمره واقعی استراتژی ۰٫۷۴ باشد، یعنی <strong>پایین‌تر از چیزی که شانس محض
            تولید می‌کند</strong>. گزارش کردن «بهترین نسخه از ۴۸ تا» بدون این مقایسه، یک
            نتیجه نیست.
          </Disclosure>
        </Card>
      </div>

      <Card title="استراتژی‌ها و مرحله‌ای که در آن هستند"
            hint={<>هر استراتژی از «فرضیه» شروع می‌شود، به «آزمایشی» می‌رسد و فقط بعد از عبور
              از همه آزمون‌های بالا «پذیرفته‌شده» می‌شود.</>}
            sub="هیچ استراتژی‌ای پیش از پذیرفته شدن، اجازه معامله با پول واقعی ندارد">
        <div className="stack gap16">
          {strategies.map((s) => {
            const alloc = allocations[s.name];
            const life = alloc?.lifecycle ?? s.lifecycle;
            return (
              <div key={s.name} className="stack gap8"
                   style={{ padding: 14, borderRadius: 14, background: "var(--surface-alt)" }}>
                <div className="row gap8 wrap">
                  <span className="mono" style={{ fontWeight: 600 }}>{s.name}</span>
                  <span className="fs11 faint">v{s.version}</span>
                  <Chip tone={life === "accepted" ? "pos" : life === "reference" ? "info"
                              : life === "suspended" ? "neg" : "warn"}>
                    {LIFECYCLE_FA[life] ?? life}
                  </Chip>
                  <Chip title="هر کندل نمودار چقدر زمان را نشان می‌دهد">
                    {TIMEFRAME_FA[s.timeframe] ?? s.timeframe}
                  </Chip>
                  <Chip title="معامله‌ها معمولاً چقدر باز می‌مانند">
                    نگهداری حدود {s.horizon_bars} کندل
                  </Chip>
                  {alloc?.enabled && <Chip tone="solid">روشن</Chip>}
                </div>
                <p className="fs12 muted">{s.description}</p>
                {s.hypothesis && (
                  <div className="fs12" style={{ lineHeight: 1.8 }}>
                    <span className="muted">
                      چرا فکر می‌کنیم این باید جواب بدهد:{" "}
                      <Hint text="این جمله پیش از هر آزمایشی نوشته شده. استراتژی‌ای که دلیلش را از قبل ننوشته باشد، بعداً می‌تواند هر نتیجه‌ای را به نفع خودش تفسیر کند." />
                    </span>{" "}
                    {s.hypothesis}
                  </div>
                )}
                {s.failure_conditions.length > 0 && (
                  <Disclosure summary={`چه چیزی این استراتژی را باطل می‌کند (${s.failure_conditions.length} شرط)`}>
                    <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                      {s.failure_conditions.map((f, i) => <li key={i}>{f}</li>)}
                    </ul>
                    <p className="mt8 muted">
                      این شرط‌ها پیش از شروع نوشته شده‌اند. دلیلش ساده است: اگر از قبل معلوم
                      نباشد چه چیزی یک ایده را باطل می‌کند، هر نتیجه بدی را می‌شود با یک
                      تنظیم کوچک توجیه کرد و ایده هیچ‌وقت <em>رد</em> نمی‌شود.
                    </p>
                  </Disclosure>
                )}
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}

/** Chart timeframes, said as a length of time. */
export const TIMEFRAME_FA: Record<string, string> = {
  M1: "کندل ۱ دقیقه‌ای", M5: "کندل ۵ دقیقه‌ای", M15: "کندل ۱۵ دقیقه‌ای",
  M30: "کندل ۳۰ دقیقه‌ای", H1: "کندل ۱ ساعته", H4: "کندل ۴ ساعته",
  D1: "کندل روزانه", W1: "کندل هفتگی",
};

const LIFECYCLE_FA: Record<string, string> = {
  reference: "فقط برای مقایسه", hypothesis: "هنوز فقط یک ایده",
  experimental: "در حال آزمایش", accepted: "آزمون‌ها را گذرانده",
  suspended: "موقتاً کنار گذاشته شده",
};

/** Four headline numbers, each read from the stored verdict. A gate the run
 *  did not evaluate says so instead of showing a number. */
function ResearchTiles({ research: r, blocking, failed }: {
  research: ResearchSummary; blocking: number; failed: number;
}) {
  const f2 = (v: number) => fa(v.toFixed(2)).replace(".", "٫");
  const pboPassed = r.pbo !== null && r.pbo_max !== null && r.pbo <= r.pbo_max;
  const dsrPassed = r.dsr !== null && r.min_dsr !== null && r.dsr >= r.min_dsr;
  const cpcvPassed = r.cpcv_positive_fraction !== null && r.cpcv_min_fraction !== null
    && r.cpcv_positive_fraction >= r.cpcv_min_fraction;
  const verdictWord = (passed: boolean) => (passed ? "عبور کرده" : "رد شده");
  return (
    <div className="grid g4">
      <Card><Tile label="چند آزمون الزامی عبور کرده"
                  hint={<>آزمون الزامی یعنی آزمونی که رد شدن در آن، جلوی رفتن به پول واقعی
                    را می‌گیرد. تا وقتی همه‌شان عبور نکنند، هیچ‌کدام از بقیه عددها اهمیتی
                    ندارند.</>}
                  value={`${blocking - failed}/${blocking}`}
                  note="از کل آزمون‌های الزامی" /></Card>
      <Card><Tile label="احتمال اینکه این نتیجه فقط خوش‌شانسی باشد" term="PBO"
                  hint={<>اگر ده‌ها نسخه از یک استراتژی را روی همان تاریخ امتحان کنید،
                    بهترینشان تا حدی فقط شانس آورده. این عدد آن شانس را برآورد می‌کند:
                    ۰٫۱۸ یعنی حدود ۱۸٪. هر چه کمتر، بهتر.</>}
                  value={r.pbo === null ? "—" : f2(r.pbo)}
                  note={r.pbo === null || r.pbo_max === null
                    ? "این آزمون در این اجرا انجام نشد"
                    : `بیشتر از ${f2(r.pbo_max)} قابل قبول نیست — این یکی ${verdictWord(pboPassed)}`}
                  tone={r.pbo !== null && !pboPassed ? "warn" : undefined} /></Card>
      <Card><Tile label="نمره عملکرد پس از کم کردن اثر شانس" term="DSR"
                  hint={<>نمره شارپ وقتی تعداد نسخه‌های امتحان‌شده هم حساب شود. نزدیک صفر
                    یعنی نتیجه از شانس قابل تفکیک نیست.
                    {r.dsr !== null && r.min_dsr !== null && <> اینجا {f2(r.dsr)} در برابر
                      حداقل لازم {f2(r.min_dsr)} است: یعنی {dsrPassed
                        ? "این آزمون عبور کرده." : "این استراتژی اثبات نشده."}</>}</>}
                  value={r.dsr === null ? "—" : f2(r.dsr)}
                  note={r.dsr === null || r.min_dsr === null
                    ? "این آزمون در این اجرا انجام نشد"
                    : `حداقل لازم ${f2(r.min_dsr)}`
                      + (r.effective_trials ? ` · ${fa(r.effective_trials)} نسخه امتحان شده` : "")
                      + ` — ${verdictWord(dsrPassed)}`}
                  tone={r.dsr !== null && !dsrPassed ? "warn" : undefined} /></Card>
      <Card><Tile label="روی چند درصد برش‌های تاریخ سودده بوده" term="CPCV"
                  hint={<>تاریخ به بلوک‌های زیادی بریده شده و هر ترکیب یک‌بار نقش «آینده
                    ندیده» را بازی کرده. ۶۰٪ یعنی از هر ۱۰ برش مستقل، ۴ برش زیان‌ده بوده
                    است.</>}
                  value={r.cpcv_positive_fraction === null ? "—"
                    : `${(r.cpcv_positive_fraction * 100).toFixed(0)}٪`}
                  note={r.cpcv_positive_fraction === null || r.cpcv_min_fraction === null
                    ? "این آزمون در این اجرا انجام نشد"
                    : `حداقل لازم ${fa((r.cpcv_min_fraction * 100).toFixed(0))}٪`}
                  tone={r.cpcv_positive_fraction !== null && !cpcvPassed ? "warn" : undefined} /></Card>
    </div>
  );
}

/** A percentile of an unsorted list, without reordering it (the bars keep the
 *  run's own path order). */
function quantile(values: number[], q: number) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
}

/** Gate names in Persian when the server stored them in English. */
const GATE_FA: Record<string, string> = {
  "L0.1": "آیا هزینه‌ها اجازه سود می‌دهند؟",
  "L0.2": "آیا سرمایه برای اندازه‌گیری درست ریسک کافی است؟",
  "L0.3": "آیا هر سفارش شماره یکتا می‌گیرد؟",
  "L0.4": "آیا حد ضرر نزد خود بروکر ثبت می‌شود؟",
  "L0.5": "آیا اتصال به اندازه کافی پایدار است؟",
  "L0.6": "آیا پول واقعاً وارد و خارج می‌شود؟",
  L1: "آیا از ساده‌ترین جایگزین‌ها بهتر است؟",
  L2: "آیا از حدس «فردا مثل امروز» بهتر است؟",
  L3: "آیا سودش از عوامل عمومی بازار نیامده؟",
  L4: "آیا نتیجه فقط حاصل امتحان کردن نسخه‌های زیاد است؟",
  "L5.1": "نمره عملکرد پس از کم کردن اثر شانس",
  "L5.2": "آیا بهترین نسخه واقعاً بهتر است یا شانسی؟",
  L6: "آیا روی برش‌های مختلف تاریخ پایدار است؟",
  L7: "آیا با هزینه و کندی بیشتر هم سودده می‌ماند؟",
  L8: "آیا بدترین افت حساب قابل تحمل بوده؟",
  L9: "آیا اصلاً به اندازه کافی داده داریم؟",
  L10: "آیا داده‌ها و هزینه‌ها واقعی و تأییدشده‌اند؟",
  L11: "آیا با همان موتور واقعی ربات هم همین نتیجه را می‌دهد؟",
  L12: "آیا روی حساب، بعد از آزمون هم جواب داده؟",
  L13: "آیا نتیجه‌ها سالم و بی‌نقص تولید شده‌اند؟",
};

function gateName(g: Gate) {
  // eslint-disable-next-line no-control-regex
  return /^[\x00-\x7F]*$/.test(g.name) && GATE_FA[g.id] ? GATE_FA[g.id] : g.name;
}

const DATA_FA: Record<string, string> = {
  synthetic: "داده ساختگی", "live-quality": "داده واقعی بروکر", unknown: "نامشخص",
};
