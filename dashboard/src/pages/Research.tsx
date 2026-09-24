import React from "react";
import { BarsV, Histogram } from "../components/charts";
import { Banner, Card, Chip, Disclosure, Hint, KV, Tile } from "../components/ui";
import type { Snapshot } from "../types";

export default function Research({ snap }: { snap: Snapshot }) {
  const { gates, cpcvSharpes, strategies, allocations } = snap;
  const blocking = gates.filter((g) => g.blocking);
  const failed = blocking.filter((g) => !g.passed);
  const accepted = failed.length === 0;
  const positivePaths = cpcvSharpes.filter((s) => s > 0).length / cpcvSharpes.length;

  return (
    <div className="stack gap16">
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
      </Banner>

      <Banner tone="flat" icon="ℹ">
        این صفحه به یک سؤال جواب می‌دهد: <strong>از کجا معلوم که سودِ گذشته شانسی نبوده؟</strong>{" "}
        اگر صد نفر صد استراتژی تصادفی بسازند، چند تایشان روی تاریخ گذشته عالی به نظر می‌رسند —
        بدون اینکه هیچ مزیتی داشته باشند. آزمون‌های این صفحه برای جدا کردن همین شانس از مزیت
        واقعی‌اند. اگر واژه‌ای را نمی‌شناسید، صفحه «واژه‌نامه ساده» همه‌شان را با مثال توضیح داده.
      </Banner>

      <div className="grid g4">
        <Card><Tile label="چند آزمون الزامی عبور کرده"
                    hint={<>آزمون الزامی یعنی آزمونی که رد شدن در آن، جلوی رفتن به پول واقعی
                      را می‌گیرد. تا وقتی همه‌شان عبور نکنند، هیچ‌کدام از بقیه عددها اهمیتی
                      ندارند.</>}
                    value={`${blocking.length - failed.length}/${blocking.length}`}
                    note="از کل آزمون‌های الزامی" /></Card>
        <Card><Tile label="احتمال اینکه این نتیجه فقط خوش‌شانسی باشد" term="PBO"
                    hint={<>اگر ده‌ها نسخه از یک استراتژی را روی همان تاریخ امتحان کنید،
                      بهترینشان تا حدی فقط شانس آورده. این عدد آن شانس را برآورد می‌کند:
                      ۰٫۱۸ یعنی حدود ۱۸٪. هر چه کمتر، بهتر.</>}
                    value="۰٫۱۸" note="بیشتر از ۰٫۲۰ قابل قبول نیست — این یکی عبور کرده" /></Card>
        <Card><Tile label="نمره عملکرد پس از کم کردن اثر شانس" term="DSR"
                    hint={<>نمره شارپ وقتی تعداد نسخه‌های امتحان‌شده هم حساب شود. نزدیک صفر
                      یعنی نتیجه از شانس قابل تفکیک نیست. اینجا ۰٫۳۱ در برابر حداقل لازم ۰٫۹۵
                      است: یعنی این استراتژی اثبات نشده.</>}
                    value="۰٫۳۱"
                    note="حداقل لازم ۰٫۹۵ · ۴۸ نسخه امتحان شده — رد شده" tone="warn" /></Card>
        <Card><Tile label="روی چند درصد برش‌های تاریخ سودده بوده" term="CPCV"
                    hint={<>تاریخ به بلوک‌های زیادی بریده شده و هر ترکیب یک‌بار نقش «آینده
                      ندیده» را بازی کرده. ۶۰٪ یعنی از هر ۱۰ برش مستقل، ۴ برش زیان‌ده بوده
                      است.</>}
                    value={`${(positivePaths * 100).toFixed(0)}٪`}
                    note="حداقل لازم ۷۰٪"
                    tone={positivePaths < 0.7 ? "warn" : undefined} /></Card>
      </div>

      <Card title="فهرست آزمون‌ها و نتیجه هرکدام"
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
                  <span className="fs13" style={{ fontWeight: 500 }}>{g.name}</span>
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
      </Card>

      <div className="grid g2">
        <Card title="نتیجه روی هر برش مستقل از تاریخ چه بوده"
              hint={<>هر میله یک برش مستقل از تاریخ است. میله‌های بالای صفر یعنی آن برش سودده
                بوده و میله‌های زیر صفر یعنی زیان‌ده. اگر فقط یک عدد میانگین گزارش شود، این
                تصویر پنهان می‌ماند.</>}
              sub="یک عدد میانگین چیزی را ثابت نمی‌کند؛ این تصویر می‌گوید در چند درصد حالت‌ها جواب می‌داده">
          <BarsV data={cpcvSharpes.map((s, i) => ({
            label: String(i + 1), value: +s.toFixed(2),
            color: s >= 0 ? "var(--s1)" : "var(--s8)" }))}
            height={200} valueFmt={(v) => v.toFixed(1)} labelEvery={3} />
          <div className="kv-grid c3 mt12">
            <KV k="بدترین حالت‌ها" hint="نمره‌ای که ۱۰٪ برش‌ها از آن هم بدتر بوده‌اند."
                v={cpcvSharpes[Math.floor(cpcvSharpes.length * 0.1)].toFixed(2)} />
            <KV k="حالت میانه" hint="نصف برش‌ها بهتر از این و نصف بدتر از این بوده‌اند."
                v={cpcvSharpes[Math.floor(cpcvSharpes.length / 2)].toFixed(2)} />
            <KV k="بهترین حالت‌ها" hint="نمره‌ای که فقط ۱۰٪ برش‌ها از آن بهتر بوده‌اند."
                v={cpcvSharpes[Math.floor(cpcvSharpes.length * 0.9)].toFixed(2)} />
          </div>
          <Disclosure summary="این روش چه کاری می‌کند که یک آزمایش ساده روی تاریخ نمی‌کند">
            یک آزمایش ساده، استراتژی را یک‌بار روی کل تاریخ اجرا می‌کند و یک عدد می‌دهد. آن
            عدد می‌تواند حاصل دو ماه خوش‌شانسی در وسط دوره باشد. اینجا به‌جای آن، تاریخ به
            بلوک‌های زیاد بریده می‌شود و هر ترکیب از بلوک‌ها یک‌بار نقش «آینده‌ای که ندیده‌ایم»
            را بازی می‌کند. بین بخش تمرین و بخش آزمون هم یک فاصله خالی گذاشته می‌شود تا
            اطلاعات از یکی به دیگری نشت نکند. نتیجه یک عدد نیست، یک تصویر است: سؤال از
            «آیا جواب می‌داد؟» به «<strong>چند درصد مواقع</strong> جواب می‌داد؟» تغییر می‌کند.
          </Disclosure>
        </Card>

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
            نمره واقعی این استراتژی ۰٫۷۴ است، یعنی <strong>پایین‌تر از چیزی که شانس محض
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
