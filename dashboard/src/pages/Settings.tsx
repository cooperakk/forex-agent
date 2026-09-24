import React, { useMemo, useState } from "react";
import {
  Banner, Card, Chip, ConfirmWrite, Disclosure, Field, KV, NumberField, Seg, Switch,
} from "../components/ui";
import type { Snapshot } from "../types";
import { TIMEFRAME_FA } from "./Research";

type Section = "agent" | "risk" | "protect" | "execution" | "news" | "research" | "ops" | "strategies";

const SECTIONS: { value: Section; label: string }[] = [
  { value: "agent", label: "رفتار ربات" },
  { value: "risk", label: "سقف‌های ایمنی" },
  { value: "protect", label: "نگه داشتن سود" },
  { value: "execution", label: "بروکر و اجرا" },
  { value: "news", label: "خبرها" },
  { value: "research", label: "سخت‌گیری آزمون‌ها" },
  { value: "ops", label: "نگهداری و امنیت" },
  { value: "strategies", label: "استراتژی‌ها" },
];

/** Deep-set a dotted path in a plain object clone. */
function setPath(obj: any, path: string, value: unknown) {
  const parts = path.split(".");
  const out = JSON.parse(JSON.stringify(obj));
  let node = out;
  for (let i = 0; i < parts.length - 1; i++) {
    node[parts[i]] = node[parts[i]] ?? {};
    node = node[parts[i]];
  }
  node[parts[parts.length - 1]] = value;
  return out;
}
function getPath(obj: any, path: string) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}
/** Minimal patch object containing only the changed leaves. */
function buildPatch(changes: Record<string, unknown>) {
  let patch: any = {};
  Object.entries(changes).forEach(([p, v]) => { patch = setPath(patch, p, v); });
  return patch;
}

/** Where the orders actually go, in plain words. */
const VENUE_FA: Record<string, string> = {
  paper: "تمرینی — شبیه‌ساز داخلی", demo: "تمرینی — حساب دمو بروکر", live: "پول واقعی",
};

/* Strategy lifecycle stages, in plain words. Kept in step with Research.tsx. */
const LIFECYCLE_FA: Record<string, string> = {
  reference: "فقط برای مقایسه", hypothesis: "هنوز فقط یک ایده",
  experimental: "در حال آزمایش", accepted: "آزمون‌ها را گذرانده",
  suspended: "موقتاً کنار گذاشته شده",
};

const FROZEN = new Set([
  "risk.daily_loss_limit_pct", "risk.weekly_loss_limit_pct", "risk.monthly_loss_limit_pct",
  "risk.max_drawdown_halt_pct", "risk.ladder", "risk.require_broker_side_stop",
  "risk.max_trades_per_day", "risk.max_trades_per_week", "risk.max_trades_per_year",
  "risk.risk_per_trade_pct", "agent.mode", "execution.venue_mode",
]);

export default function Settings({ snap, write, readOnly }: {
  snap: Snapshot;
  write: (path: string, body: unknown, totp: string) => Promise<{ ok: boolean; detail: string }>;
  readOnly: boolean;
}) {
  const [section, setSection] = useState<Section>("agent");
  const [changes, setChanges] = useState<Record<string, unknown>>({});
  const [confirmOpen, setConfirmOpen] = useState(false);
  const cfg = snap.config;

  const val = (path: string) => (path in changes ? changes[path] : getPath(cfg, path));
  const set = (path: string, v: unknown) => setChanges((c) => ({ ...c, [path]: v }));
  const dirty = Object.keys(changes).length;

  const num = (path: string, opts: { min?: number; max?: number; step?: number; suffix?: string } = {}) => (
    <NumberField value={Number(val(path) ?? 0)} onChange={(v) => set(path, String(v))}
                 disabled={readOnly} {...opts} />
  );
  const int = (path: string, opts: { min?: number; max?: number; suffix?: string } = {}) => (
    <NumberField value={Number(val(path) ?? 0)} onChange={(v) => set(path, Math.round(v))}
                 step={1} disabled={readOnly} {...opts} />
  );
  const bool = (path: string) => (
    <Switch checked={Boolean(val(path))} onChange={(v) => set(path, v)}
            disabled={readOnly} label={path} />
  );

  return (
    <div className="stack gap16">
      {readOnly && (
        <Banner tone="info" icon="👁">
          این صفحه فعلاً فقط برای <strong>دیدن</strong> است و هیچ تغییری ذخیره نمی‌شود. برای
          تغییر دادن تنظیمات باید با حساب مالک وارد شوید و برای هر ذخیره، یک کد شش‌رقمی تازه
          از برنامه احراز هویت بزنید.
        </Banner>
      )}

      <Banner tone="flat" icon="ℹ">
        کنار هر تنظیم یک ⓘ هست که با یک جمله ساده می‌گوید آن عدد چه کار می‌کند. تنظیم‌هایی که
        علامت 🔒 دارند قفل‌اند: نه ربات و نه هیچ بخش یادگیرنده‌ای نمی‌تواند عوضشان کند، و
        تغییرشان با دست شما هم در دفتر ثبت رویدادها باقی می‌ماند.
      </Banner>

      <div className="row gap8 wrap">
        {SECTIONS.map((s) => (
          <button key={s.value} className={`btn ${section === s.value ? "" : "ghost"} sm`}
                  onClick={() => setSection(s.value)}>{s.label}</button>
        ))}
        <div className="grow" />
        {dirty > 0 && (
          <>
            <Chip tone="warn">{dirty} تغییر ذخیره‌نشده</Chip>
            <button className="btn ghost sm" onClick={() => setChanges({})}>لغو تغییرها</button>
            <button className="btn sm" onClick={() => setConfirmOpen(true)}>ذخیره تغییرات</button>
          </>
        )}
      </div>

      {section === "agent" && (
        <Card title="ربات چقدر اختیار دارد"
              sub="این بخش تعیین می‌کند چه کسی اجازه اجرای معامله را می‌دهد: ربات یا شما">
          <Field label="حالت کار ربات"
                 hint={<>سقف‌های ایمنی در هر چهار حالت به‌طور کامل برقرارند. این انتخاب فقط
                   می‌گوید چه کسی دکمه نهایی را می‌زند.</>}
                 help="«فقط تماشا» هیچ معامله‌ای نمی‌گذارد · «فقط پیشنهاد» منتظر تأیید شما می‌ماند · «نیمه‌خودکار» داخل محدوده‌ای که از قبل تأیید کرده‌اید خودش اجرا می‌کند · «کاملاً خودکار» بدون پرسیدن اجرا می‌کند.">
            <Seg value={String(val("agent.mode"))}
                 onChange={(v) => set("agent.mode", v)}
                 options={[{ value: "observe", label: "تماشا" },
                           { value: "advisory", label: "پیشنهاد" },
                           { value: "semi_auto", label: "نیمه" },
                           { value: "autonomous", label: "خودکار" }]} />
          </Field>
          <Field label="هر چند ثانیه بازار را از نو بررسی کند"
                 hint={<>در هر بررسی این کارها پشت سر هم انجام می‌شود: کلید توقف، سلامت
                   اتصال، مقایسه با حساب بروکر، گرفتن قیمت‌ها، تشخیص حال‌وهوای بازار، مدیریت
                   معامله‌های باز، جست‌وجوی فرصت، سنجش قواعد ایمنی، و در آخر اقدام.</>}
                 help="عدد کوچک‌تر یعنی واکنش سریع‌تر ولی فشار بیشتر روی اتصال.">
            {int("agent.decision_interval_sec", { min: 5, max: 3600, suffix: "ثانیه" })}
          </Field>
          <Field label="در چه ساعت‌هایی اجازه معامله دارد (به وقت گرینویچ)"
                 hint={<>در ساعت‌های کم‌رمق، اختلاف قیمت خرید و فروش ۲ تا ۴ برابر می‌شود، یعنی
                   همان معامله چند برابر گران‌تر تمام می‌شود.</>}
                 help="محدود کردن ساعت‌های معامله، ارزان‌ترین راه کم کردن هزینه است.">
            <input className="input num" disabled={readOnly}
                   value={JSON.stringify(val("agent.session_windows_utc"))}
                   onChange={(e) => {
                     try { set("agent.session_windows_utc", JSON.parse(e.target.value)); }
                     catch { /* keep typing */ }
                   }} />
          </Field>
          <Field label="از معامله‌های گذشته درس بگیرد"
                 hint={<>ربات هر معامله بسته‌شده را تحلیل می‌کند و اگر الگویی را بارها ببیند،
                   یک درس ثبت می‌کند. هیچ درسی بدون تأیید شما رفتار ربات را عوض نمی‌کند.</>}
                 help="یادگیری فقط می‌تواند ربات را محتاط‌تر کند، نه جسورتر.">
            {bool("agent.learning_enabled")}
          </Field>
          <Field label="حداقل چند معامله لازم است تا اجازه پیشنهاد داشته باشد"
                 hint={<>الگویی که فقط در ۱۰ معامله دیده شده، احتمالاً تصادفی است. هر چه این
                   عدد بزرگ‌تر باشد، پیشنهادها قابل اتکاتر و کمیاب‌ترند.</>}
                 help="زیر این تعداد، ربات حق ندارد تغییر هیچ تنظیمی را پیشنهاد بدهد.">
            {int("agent.proposal_min_sample", { min: 10, max: 10000, suffix: "معامله" })}
          </Field>
          <Field label="هر پیشنهاد باید به تأیید شما برسد"
                 hint={<>این گزینه عمداً قفل است و خاموش نمی‌شود.</>}
                 help="سامانه‌ای که خودش از نتایج اخیرش تنظیماتش را عوض کند، در عمل فقط خودش را روی صد معامله آخر «تنظیم» می‌کند — و همین کار است که باعث می‌شود در بازار واقعی شکست بخورد.">
            <Switch checked={true} onChange={() => undefined} disabled label="قفل" />
          </Field>
          <Field label="حال‌وهوای بازار را تشخیص بدهد"
                 hint={<>بازار را برچسب می‌زند: جهت‌دار، آرام، پرنوسان یا بحرانی. در حالت
                   بحرانی خودبه‌خود محتاط‌تر می‌شود.</>}
                 help="هر استراتژی فقط در بعضی حالت‌ها جواب می‌دهد؛ این تشخیص جلوی معامله در حالت نامناسب را می‌گیرد.">
            {bool("agent.regime_detection_enabled")}
          </Field>
        </Card>
      )}

      {section === "risk" && (
        <>
          <Banner tone="warn" icon="🔒">
            تنظیم‌هایی که علامت 🔒 دارند از دسترس بخش یادگیرنده خارج‌اند: ربات حتی نمی‌تواند
            پیشنهاد تغییرشان را بدهد. فقط شما، با حساب مالک و یک کد شش‌رقمی تازه، می‌توانید
            عوضشان کنید — و هر تغییر در دفتر ثبت رویدادها می‌ماند.
          </Banner>
          <Card title="در هر معامله چقدر پول در خطر باشد">
            <Field label="در هر معامله چند درصد حساب ریسک شود 🔒"
                   hint={<>مثال: ۰٫۵٪ روی حساب ۱۰٬۰۰۰ دلاری یعنی اگر حد ضرر بخورد، ۵۰ دلار
                     از دست می‌رود. اندازه معامله از روی همین عدد و فاصله حد ضرر حساب
                     می‌شود.</>}
                   help="این عدد حجم معامله را تعیین می‌کند، نه برعکس.">
              {num("risk.risk_per_trade_pct", { min: 0.01, max: 2, step: 0.05, suffix: "٪" })}
            </Field>
            <Field label="بیشترین ضرر مجاز در یک روز 🔒"
                   hint="مثال: ۲٪ روی حساب ۱۰٬۰۰۰ دلاری یعنی ۲۰۰ دلار."
                   help="با رسیدن به این حد، تا فردا هیچ معامله تازه‌ای باز نمی‌شود.">
              {num("risk.daily_loss_limit_pct", { min: 0.1, max: 20, step: 0.1, suffix: "٪" })}
            </Field>
            <Field label="بیشترین ضرر مجاز در یک هفته 🔒"
                   help="با رسیدن به این حد، تا هفته بعد معامله تازه‌ای باز نمی‌شود.">
              {num("risk.weekly_loss_limit_pct", { min: 0.1, max: 40, step: 0.1, suffix: "٪" })}
            </Field>
            <Field label="بیشترین ضرر مجاز در یک ماه 🔒"
                   help="با رسیدن به این حد، تا ماه بعد معامله تازه‌ای باز نمی‌شود.">
              {num("risk.monthly_loss_limit_pct", { min: 0.1, max: 60, step: 0.1, suffix: "٪" })}
            </Field>
            <Field label="افت حساب که ربات را کاملاً متوقف می‌کند 🔒"
                   hint={<>فاصله حساب از بالاترین رقمی که تا امروز داشته. مثال: ۱۰٪ یعنی از
                     ۱۱٬۰۰۰ دلار به ۹٬۹۰۰ دلار.</>}
                   help="بعد از این حد، راه‌اندازی دوباره فقط با دست شما ممکن است — سامانه خودش برنمی‌گردد.">
              {num("risk.max_drawdown_halt_pct", { min: 1, max: 50, step: 0.5, suffix: "٪" })}
            </Field>
          </Card>
          <Card title="شکل هر معامله">
            <Field label="کمترین فاصله مجاز تا حد ضرر"
                   hint={<>پیپ کوچک‌ترین پله حرکت قیمت است. حد ضرر خیلی نزدیک یعنی هزینه
                     معامله سهم بزرگی از سود احتمالی می‌شود و باید در بیش از ۶۰٪ مواقع
                     برنده باشید تا فقط سر به سر شوید.</>}
                   help="این یک محدودیت ریاضی است، نه سلیقه: زیر این فاصله، حتی یک استراتژی خوب هم پس از هزینه‌ها ضرر می‌دهد.">
              {num("risk.min_stop_pips", { min: 1, max: 100, step: 1, suffix: "پیپ" })}
            </Field>
            <Field label="بیشترین فاصله مجاز تا حد ضرر"
                   help="حد ضرر خیلی دور، حجم معامله را آن‌قدر کوچک می‌کند که دیگر قابل اجرا نیست.">
              {num("risk.max_stop_pips", { min: 10, max: 500, step: 5, suffix: "پیپ" })}
            </Field>
            <Field label="سود احتمالی دست‌کم چند برابر ریسک باشد"
                   hint={<>مثال: عدد ۲ یعنی معامله‌ای پذیرفته می‌شود که هدفش دست‌کم دو برابر
                     فاصله حد ضررش باشد — ۵۰ دلار ریسک برای ۱۰۰ دلار سود احتمالی.</>}
                   help="هدف کوچک در برابر هزینه ثابت، در بلندمدت همیشه ضرر می‌دهد — حتی اگر بیشتر معامله‌ها برنده باشند.">
              {num("risk.min_reward_risk", { min: 0.3, max: 5, step: 0.1, suffix: "برابر" })}
            </Field>
            <Field label="حد ضرر حتماً نزد بروکر ثبت شود 🔒"
                   hint={<>اگر حد ضرر فقط در برنامه ما باشد، با قطع برق یا اینترنت عملاً وجود
                     ندارد و معامله بی‌محافظ می‌ماند.</>}
                   help="قطع شدن ارتباط با یک بروکر خارجی اتفاق نادری نیست؛ باید فرض شود که رخ می‌دهد.">
              <Switch checked={Boolean(val("risk.require_broker_side_stop"))}
                      onChange={(v) => set("risk.require_broker_side_stop", v)}
                      disabled={readOnly} label="broker stop" />
            </Field>
          </Card>
          <Card title="سقف‌های کل حساب">
            <Field label="بیشترین تعداد معامله باز هم‌زمان"
                   help="هر معامله باز، بخشی از حساب را در خطر می‌گذارد؛ این عدد جمع آن‌ها را محدود می‌کند.">
              {int("risk.max_open_positions", { min: 0, max: 50 })}
            </Field>
            <Field label="بیشترین تعداد معامله باز روی یک جفت‌ارز"
                   help="مانع از این می‌شود که چند معامله روی یک جفت‌ارز، عملاً یک معامله چند برابری بسازند.">
              {int("risk.max_positions_per_instrument", { min: 0, max: 10 })}
            </Field>
            <Field label="بیشترین بزرگی معامله‌ها نسبت به حساب"
                   hint={<>به این «اهرم» می‌گویند. ۵ برابر یعنی با ۱۰٬۰۰۰ دلار حساب، حداکثر
                     ۵۰٬۰۰۰ دلار معامله باز. هر حرکت ۱ درصدی بازار، ۵ درصد حساب را جابه‌جا
                     می‌کند — در هر دو جهت.</>}>
              {num("risk.max_gross_leverage", { min: 0.1, max: 100, step: 0.5, suffix: "×" })}
            </Field>
            <Field label="بیشترین شرط‌بندی روی یک ارز (٪ حساب)"
                   hint={<>چهار معامله جدا که همگی یعنی «دلار پایین می‌رود»، در عمل یک شرط
                     بزرگ روی دلارند. این عدد جمع آن‌ها را سقف می‌زند.</>}
                   help="مثال: ۱٫۲۵٪ روی حساب ۱۰٬۰۰۰ دلاری یعنی حداکثر ۱۲۵ دلار ریسک خالص روی هر ارز.">
              {num("risk.max_currency_exposure_pct", { min: 0.1, max: 20, step: 0.05, suffix: "٪" })}
            </Field>
            <Field label="بیشترین ریسک روی جفت‌ارزهایی که شبیه هم حرکت می‌کنند (٪ حساب)"
                   hint={<>یورو/دلار و پوند/دلار معمولاً تقریباً با هم بالا و پایین می‌روند؛
                     خرید هر دو یعنی یک معامله دوبرابری، نه دو معامله جدا.</>}
                   help="گروهی از جفت‌ارزها که بیشتر از حد زیر با هم حرکت می‌کنند، یک معامله شمرده می‌شوند.">
              {num("risk.max_correlated_risk_pct", { min: 0.1, max: 20, step: 0.05, suffix: "٪" })}
            </Field>
            <Field label="از چه حدی به بالا، دو جفت‌ارز «شبیه هم» شمرده شوند"
                   hint={<>عددی بین ۰ و ۱. مثال: ۰٫۸ یعنی جفت‌ارزهایی که بیش از ۸۰٪ مواقع
                     هم‌جهت حرکت می‌کنند، یک معامله حساب می‌شوند.</>}>
              <NumberField value={Number(val("risk.correlation_threshold"))} step={0.05} min={0} max={1}
                           suffix="از ۱" disabled={readOnly}
                           onChange={(v) => set("risk.correlation_threshold", v)} />
            </Field>
          </Card>
          <Card title="چند معامله مجاز است — این هم یک سقف ایمنی است">
            <Banner tone="flat" icon="ℹ">
              هر معامله هزینه دارد، چه سودده باشد چه زیان‌ده. سامانه‌ای که روزی ۱۰ معامله با
              حجم بالا می‌زند، در یک سال چیزی نزدیک به <strong>سه برابر کل سرمایه‌اش</strong>{" "}
              را فقط بابت هزینه معامله می‌پردازد. به همین دلیل تعداد معامله‌ها اینجا دقیقاً
              مثل اهرم سقف دارد.
            </Banner>
            <Field label="بیشترین تعداد معامله در یک روز 🔒">
              {int("risk.max_trades_per_day", { min: 0, max: 500 })}
            </Field>
            <Field label="بیشترین تعداد معامله در یک هفته 🔒">
              {int("risk.max_trades_per_week", { min: 0, max: 2000 })}
            </Field>
            <Field label="بیشترین تعداد معامله در یک سال 🔒">
              {int("risk.max_trades_per_year", { min: 0, max: 100000 })}
            </Field>
            <Field label="دست‌کم چقدر بین دو معامله فاصله بیفتد"
                   help="جلوی باز کردن چند معامله پشت سر هم روی یک حرکت واحد را می‌گیرد.">
              {int("risk.min_seconds_between_entries", { min: 0, max: 86400, suffix: "ثانیه" })}
            </Field>
            <Field label="بیشترین هزینه معاملاتی مجاز در یک سال (٪ حساب)"
                   hint={<>با همین آهنگ فعلی، هزینه یک سال چقدر می‌شود. مثال: ۲۰٪ روی حساب
                     ۱۰٬۰۰۰ دلاری یعنی ۲٬۰۰۰ دلار هزینه در سال.</>}
                   help="بالاتر از این، معامله تازه رد می‌شود حتی اگر فرصت عالی باشد.">
              {num("risk.max_annual_cost_pct_of_equity", { min: 1, max: 200, step: 1, suffix: "٪" })}
            </Field>
          </Card>
          <Card title="شرایطی که معامله را متوقف می‌کنند">
            <Field label="اگر قیمت‌ها از این قدیمی‌تر بودند، معامله نکن"
                   help="معامله بر پایه قیمتی که دو دقیقه پیش گرفته شده، یعنی معامله با قیمتی که دیگر وجود ندارد.">
              {int("risk.max_data_staleness_sec", { min: 1, max: 3600, suffix: "ثانیه" })}
            </Field>
            <Field label="بیشترین اختلاف مجاز ساعت ما با ساعت بروکر"
                   hint="۱۰۰۰ میلی‌ثانیه یعنی یک ثانیه."
                   help="سامانه‌ای که نتواند بگوید هر اتفاق کِی افتاده، نمی‌تواند صادقانه بگوید چه می‌دانسته و چه کرده.">
              {int("risk.max_clock_skew_ms", { min: 10, max: 60000, suffix: "میلی‌ثانیه" })}
            </Field>
            <Field label="اگر اختلاف قیمت خرید و فروش از حالت عادی این‌قدر بیشتر شد، معامله نکن"
                   hint={<>عدد ۳ یعنی: اگر این اختلاف سه برابر حالت عادیِ همان جفت‌ارز شد،
                     معامله انجام نمی‌شود. در ساعت‌های کم‌رمق این اتفاق عادی است.</>}>
              {num("risk.max_spread_pips_multiple", { min: 1.1, max: 10, step: 0.1, suffix: "×" })}
            </Field>
            <Field label="چند دقیقه قبل از یک خبر مهم اقتصادی، معامله نکن"
                   help="در لحظه اعلام، قیمت‌ها غیرقابل پیش‌بینی می‌پرند و حد ضرر ممکن است خیلی بدتر پر شود.">
              {int("risk.block_minutes_before_high_impact", { min: 0, max: 600, suffix: "دقیقه" })}
            </Field>
            <Field label="چند دقیقه بعد از یک خبر مهم اقتصادی، معامله نکن">
              {int("risk.block_minutes_after_high_impact", { min: 0, max: 600, suffix: "دقیقه" })}
            </Field>
            <Field label="معامله‌ها را پیش از تعطیلی آخر هفته ببند"
                   hint={<>بازار آخر هفته بسته است ولی خبرها ادامه دارند؛ دوشنبه قیمت می‌تواند
                     با یک پرش باز شود که حد ضرر را کاملاً رد کند.</>}
                   help="نگه داشتن معامله در آخر هفته، ضرری را که سقف داشت بی‌سقف می‌کند.">
              {bool("risk.weekend_flat")}
            </Field>
            <Field label="بعد از چند ثانیه بی‌اتصالی، همه‌چیز را قفل کن"
                   help="وقتی ارتباط نیست، ربات نه می‌بیند و نه می‌تواند ببندد؛ امن‌ترین کار، متوقف ماندن است.">
              {int("risk.max_offline_seconds_before_freeze", { min: 5, max: 7200, suffix: "ثانیه" })}
            </Field>
          </Card>
        </>
      )}

      {section === "protect" && (
        <Card title="چطور سودِ به‌دست‌آمده حفظ شود"
              sub="پیدا کردن یک معامله خوب یک مسئله است؛ نگه داشتن سودی که به دست آمده، مسئله‌ای کاملاً جداست">
          <Banner tone="flat" icon="ℹ">
            همه عددهای این بخش بر حسب <strong>R</strong> هستند: یعنی «چند برابر مبلغی که روی
            آن معامله ریسک شده بود». اگر در یک معامله ۵۰ دلار ریسک کرده باشید، ۱R یعنی ۵۰ دلار
            سود و ۲R یعنی ۱۰۰ دلار سود.
          </Banner>
          <Field label="با چقدر سود، حد ضرر به نقطه ورود منتقل شود"
                 hint={<>بعد از این جابه‌جایی، بدترین حالت آن معامله دیگر ضرر نیست، بلکه صفر
                   است. مثال: عدد ۱ یعنی به‌محض اینکه سود به ۵۰ دلار (یک برابر مبلغ
                   ریسک‌شده) رسید، حد ضرر روی قیمت ورود می‌رود.</>}
                 help="صفر یعنی این قابلیت خاموش است.">
            {num("risk.breakeven_trigger_r", { min: 0, max: 3, step: 0.1, suffix: "R" })}
          </Field>
          <Field label="با چقدر سود، بخشی از معامله بسته شود"
                 hint={<>مثال: عدد ۱ یعنی وقتی سود به یک برابر مبلغ ریسک‌شده رسید، بخشی از
                   حجم بسته می‌شود و همان مقدار سود قطعی می‌شود.</>}
                 help="صفر یعنی این قابلیت خاموش است.">
            {num("risk.partial_take_r", { min: 0, max: 5, step: 0.1, suffix: "R" })}
          </Field>
          <Field label="چه کسری از معامله در آن نقطه بسته شود"
                 hint={<>۰٫۵ یعنی نصف. مثال: از ۰٫۱۲ لات، ۰٫۰۶ لات بسته می‌شود و بقیه ادامه
                   می‌دهد.</>}
                 help="سامانه هرگز باقیمانده‌ای کوچک‌تر از کمینه قابل معامله نمی‌سازد؛ یک معامله خرد که نشود بستش، از نبودِ این قابلیت بدتر است.">
            {num("risk.partial_take_fraction", { min: 0, max: 0.8, step: 0.05, suffix: "از کل حجم" })}
          </Field>
          <Field label="حد ضرر دنبال‌کننده چقدر از قیمت فاصله بگیرد"
                 hint={<>فاصله بر حسب «نوسان معمول همان جفت‌ارز» حساب می‌شود (به آن ATR
                   می‌گویند). عدد بزرگ‌تر یعنی فاصله بیشتر و جا دادن بیشتر به نوسان‌های
                   عادی.</>}
                 help="حد ضرر دنبال‌کننده همراه سود بالا می‌آید و هیچ‌وقت به عقب برنمی‌گردد.">
            {num("risk.trail_atr_multiple", { min: 0, max: 6, step: 0.1, suffix: "×نوسان معمول" })}
          </Field>
          <Field label="از چه مقدار سودی به بعد، حد ضرر دنبال‌کننده روشن شود"
                 help="پیش از این نقطه، حد ضرر سر جای اولیه‌اش می‌ماند.">
            {num("risk.trail_activate_r", { min: 0, max: 3, step: 0.1, suffix: "R" })}
          </Field>
          <Field label="اگر سود امروز به این حد رسید، دیگر معامله نکن (٪ حساب)"
                 hint={<>مثال: ۳٪ روی حساب ۱۰٬۰۰۰ دلاری یعنی ۳۰۰ دلار. بعد از آن، بقیه روز
                   معامله تازه‌ای باز نمی‌شود.</>}
                 help="هدفش این است که یک روز خوب، در چند ساعت بعد پس داده نشود.">
            {num("risk.daily_profit_lock_pct", { min: 0, max: 20, step: 0.25, suffix: "٪" })}
          </Field>
          <Banner tone="flat" icon="ℹ">
            حد ضرر در این سامانه فقط می‌تواند به سمت امن‌تر حرکت کند، هیچ‌وقت به سمت دورتر.
            دور کردن حد ضرر وسط معامله («بگذار کمی بیشتر فرصت بدهم») رایج‌ترین راهی است که یک
            ضرر کوچکِ کنترل‌شده به یک ضرر بزرگِ کنترل‌نشده تبدیل می‌شود. اینجا این کار در سطح
            نرم‌افزار رد می‌شود، نه اینکه به خویشتن‌داری کاربر سپرده شود.
          </Banner>
        </Card>
      )}

      {section === "execution" && (
        <Card title="بروکر و نحوه ارسال سفارش">
          <Field label="روی کدام حساب کار می‌کند 🔒"
                 hint={<>«شبیه‌ساز» یعنی کل بازار داخل برنامه ما ساخته می‌شود · «دمو» یعنی
                   حساب تمرینی بروکر با پول غیرواقعی · «پول واقعی» یعنی همان چیزی که به نظر
                   می‌رسد.</>}
                 help="رفتن به پول واقعی فقط وقتی ممکن است که همه استراتژی‌های فعال، همه آزمون‌های صفحه «آزمایش و اثبات» را گذرانده باشند.">
            <Chip tone={val("execution.venue_mode") === "live" ? "neg" : "info"}>
              {VENUE_FA[String(val("execution.venue_mode"))] ?? String(val("execution.venue_mode"))}
            </Chip>
          </Field>
          <Field label="کدام بروکر 🔒"><Chip>{String(val("execution.broker"))}</Chip></Field>
          <Field label="حساب با چه ارزی سنجیده می‌شود"
                 hint="همه سود و زیان‌ها به این ارز تبدیل و گزارش می‌شوند.">
            <input className="input num" disabled={readOnly}
                   value={String(val("execution.account_currency"))}
                   onChange={(e) => set("execution.account_currency", e.target.value.toUpperCase())} />
          </Field>
          <Field label="اگر قیمت بیش از این با درخواست ما فرق داشت، سفارش را نپذیر"
                 hint={<>بین لحظه ارسال و لحظه اجرا، قیمت تکان می‌خورد. به این تفاوت «لغزش»
                   می‌گویند. مثال: ۰٫۸ پیپ روی حجم ۰٫۱۰ لات یعنی حدود ۰٫۸ دلار.</>}>
            {num("execution.max_slippage_pips", { min: 0, max: 20, step: 0.1, suffix: "پیپ" })}
          </Field>
          <Field label="چقدر منتظر پاسخ بروکر بماند"
                 hint="۵٬۰۰۰ میلی‌ثانیه یعنی ۵ ثانیه. بعد از آن، سفارش «بلاتکلیف» شمرده می‌شود.">
            {int("execution.submit_timeout_ms", { min: 250, max: 60000, suffix: "میلی‌ثانیه" })}
          </Field>
          <Field label="چند بار اجازه تلاش دوباره دارد"
                 hint={<>تلاش دوباره همیشه با همان شماره یکتای سفارش انجام می‌شود، تا اگر
                   سفارش اول ثبت شده باشد، بروکر دومی را رد کند و حجم دو برابر نشود.</>}
                 help="سفارشی که وضعیتش نامعلوم است هیچ‌وقت با ارسال دوباره حل نمی‌شود — فقط با پرسیدن از بروکر.">
            {int("execution.max_submit_retries", { min: 0, max: 5 })}
          </Field>
          <Field label="هر چند ثانیه حساب ما با حساب بروکر مقایسه شود"
                 hint="اگر آنچه ما می‌بینیم با آنچه بروکر می‌گوید یکی نباشد، چیزی جدی اشتباه است و سامانه متوقف می‌شود.">
            {int("execution.reconcile_interval_sec", { min: 5, max: 3600, suffix: "ثانیه" })}
          </Field>
          <Field label="اگر وضعیت یک جفت‌ارز نامعلوم شد، موقتاً کنارش بگذار"
                 help="تا روشن شدن وضعیت، روی آن جفت‌ارز معامله‌ای انجام نمی‌شود؛ بقیه عادی کار می‌کنند.">
            {bool("execution.quarantine_on_unknown_state")}
          </Field>
          <Disclosure summary="بروکر فعلی چه چیزهایی را پشتیبانی می‌کند">
            <div className="stack gap6">
              <KV k="شماره یکتا برای هر سفارش"
                  hint="بدون آن، اگر پاسخ بروکر گم شود نمی‌شود با اطمینان فهمید سفارش ثبت شده یا نه."
                  v={snap.status.broker.supports_client_order_id ? "پشتیبانی می‌شود" : "پشتیبانی نمی‌شود"}
                  tone={snap.status.broker.supports_client_order_id ? "pos" : "neg"} />
              <KV k="ثبت حد ضرر روی سرور خود بروکر"
                  hint="بدون آن، حد ضرر فقط تا وقتی کار می‌کند که برنامه ما روشن و متصل باشد."
                  v={snap.status.broker.supports_server_side_stop ? "پشتیبانی می‌شود" : "پشتیبانی نمی‌شود"}
                  tone={snap.status.broker.supports_server_side_stop ? "pos" : "neg"} />
              <KV k="اعلام لحظه‌ای رویدادها به ترتیب"
                  hint="بدون آن باید مرتب از بروکر پرسید چه خبر است، و بین دو پرسش ممکن است چیزی از قلم بیفتد."
                  v={snap.status.broker.supports_transaction_stream ? "پشتیبانی می‌شود" : "پشتیبانی نمی‌شود"}
                  tone={snap.status.broker.supports_transaction_stream ? "pos" : "neg"} />
              {snap.status.broker.degradations.map((d, i) => (
                <Banner key={i} tone="warn" icon="⚠">{d}</Banner>
              ))}
            </div>
          </Disclosure>
        </Card>
      )}

      {section === "news" && (
        <Card title="خبرهای اقتصادی"
              sub="خبر فقط می‌تواند جلوی معامله را بگیرد یا حجم را کم کند — هیچ‌وقت خودش معامله‌ای را شروع نمی‌کند">
          <Field label="خبرها را در نظر بگیرد"
                 help="خاموش کردنش یعنی ربات نمی‌داند کی اعلام‌های مهم اقتصادی است.">
            {bool("news.enabled")}
          </Field>
          <Field label="خبر چه نقشی داشته باشد"
                 hint={<>«فقط جلوگیری» یعنی خبر فقط می‌تواند نگذارد معامله‌ای باز شود. دو حالت
                   دیگر به خبر اجازه اثر بیشتری می‌دهند و هنوز اجازه استفاده ندارند.</>}
                 help="ارتقا از «فقط جلوگیری» به حالت‌های دیگر، نیازمند گذراندن آزمون‌هایی است که ثابت کنند خبر واقعاً اطلاعات اضافه می‌دهد و صرفاً آینده را لو نمی‌دهد.">
            <Seg value={String(val("news.role"))} onChange={(v) => set("news.role", v)}
                 options={[{ value: "risk_filter", label: "فقط جلوگیری" },
                           { value: "meta_label", label: "تعدیل حجم" },
                           { value: "signal", label: "شروع معامله" }]} />
          </Field>
          <Field label="متن خبرها با هوش مصنوعی خوانده شود"
                 hint={<>مدل زبانی فقط می‌گوید خبر درباره چیست و به کدام ارز مربوط است. جهت
                   معامله را تعیین نمی‌کند و حجم را بزرگ‌تر نمی‌کند.</>}>
            {bool("news.llm_enabled")}
          </Field>
          <Field label="مدل هوش مصنوعی تا چه تاریخی را می‌داند"
                 hint={<>مدل، متن‌های تا این تاریخ را در آموزشش دیده است. اگر آزمایشی روی
                   خبرهای قدیمی‌تر از این تاریخ انجام شود، مدل عملاً جواب را از قبل می‌داند و
                   نتیجه بی‌اعتبار است.</>}
                 help="چنین آزمایشی فقط برای کنجکاوی است، نه برای تصمیم‌گیری.">
            <input className="input num" disabled={readOnly}
                   value={String(val("news.llm_training_cutoff"))}
                   onChange={(e) => set("news.llm_training_cutoff", e.target.value)} />
          </Field>
          <Field label="فقط خبرهای بعد از آن تاریخ استفاده شوند"
                 help="تنها راه اطمینان از اینکه مدل جواب را از قبل نمی‌داند.">
            {bool("news.require_post_cutoff_only")}
          </Field>
          <Field label="آزمون «آیا مدل جواب را از قبل می‌دانست» الزامی باشد"
                 hint={<>آزمونی که بررسی می‌کند نتیجه خوب، از تحلیل واقعی آمده یا از حافظه
                   مدل درباره اتفاق‌هایی که واقعاً رخ داده‌اند.</>}>
            {bool("news.lap_test_required")}
          </Field>
          <Banner tone="flat" icon="⏱">
            چرا خبر اینجا اجازه شروع معامله ندارد: خواندن یک خبر با هوش مصنوعی چند صد
            میلی‌ثانیه طول می‌کشد، ولی بازار یک اعلام زمان‌بندی‌شده را در کمتر از یک ثانیه
            هضم می‌کند. یعنی تا ما بفهمیم خبر چه بود، قیمت جدید جا افتاده است. این بخش عمداً
            یک مسابقه سرعت نیست.
          </Banner>
        </Card>
      )}

      {section === "research" && (
        <Card title="یک استراتژی چقدر باید خودش را اثبات کند"
              sub="این حدها پیش از اولین آزمایش تعیین و قفل می‌شوند و همراه نتیجه هر آزمون ثبت می‌گردند">
          <Banner tone="flat" icon="ℹ">
            این عددها تعیین می‌کنند چقدر سخت‌گیر باشیم. سخت‌گیری بیشتر یعنی استراتژی‌های
            کمتری قبول می‌شوند — و همین هدف است. معنی هر واژه در صفحه «واژه‌نامه ساده» با
            مثال آمده.
          </Banner>
          <Field label="چقدر سخت‌گیر باشیم" term="α"
                 hint={<>عدد کوچک‌تر یعنی سخت‌گیرتر. ۰٫۰۵ یعنی «می‌پذیرم که ۵٪ مواقع اشتباه
                   کنم»، ۰٫۰۱ یعنی ۱٪.</>}
                 help="با ۰٫۰۵ و این فرض واقع‌بینانه که فقط ۳٪ ایده‌های معاملاتی درست‌اند، یک نتیجه «معنادار» بیشتر احتمال دارد غلط باشد تا درست. به همین دلیل اینجا ۰٫۰۱ استفاده می‌شود.">
            <NumberField value={Number(val("research.alpha"))} step={0.001} min={0.0001} max={0.05}
                         disabled={readOnly} onChange={(v) => set("research.alpha", v)} />
          </Field>
          <Field label="پیش از آزمایش، چقدر احتمال می‌دهیم ایده درست باشد" term="π"
                 hint={<>عددی بین ۰ و ۱. ۰٫۰۳ یعنی ۳٪: از هر ۱۰۰ ایده معاملاتی، انتظار داریم
                   فقط ۳ تا واقعاً درست باشند.</>}
                 help="بدون اعلام این عدد، هیچ نتیجه مثبتی قابل تفسیر نیست — چون معلوم نیست از چه تعداد ایده بیرون آمده.">
            <NumberField value={Number(val("research.declared_prior"))} step={0.005} min={0.001} max={0.5}
                         disabled={readOnly} onChange={(v) => set("research.declared_prior", v)} />
          </Field>
          <Field label="بیشترین احتمال قابل قبول برای اینکه نتیجه فقط خوش‌شانسی باشد" term="PBO"
                 hint={<>۰٫۲۰ یعنی: اگر بیش از ۲۰٪ احتمال داشته باشد که نتیجه صرفاً از امتحان
                   کردن نسخه‌های زیاد آمده باشد، رد می‌شود.</>}>
            <NumberField value={Number(val("research.pbo_max"))} step={0.05} min={0.01} max={1}
                         disabled={readOnly} onChange={(v) => set("research.pbo_max", v)} />
          </Field>
          <Field label="کمترین نمره عملکرد قابل قبول پس از کم کردن اثر شانس" term="DSR"
                 hint={<>نمره شارپ وقتی تعداد نسخه‌های امتحان‌شده هم حساب شود. نزدیک صفر یعنی
                   نتیجه از شانس قابل تفکیک نیست.</>}>
            <NumberField value={Number(val("research.min_dsr"))} step={0.01} min={0.5} max={1}
                         disabled={readOnly} onChange={(v) => set("research.min_dsr", v)} />
          </Field>
          <Field label="دست‌کم چند درصد برش‌های تاریخ باید سودده باشند" term="CPCV"
                 hint={<>۰٫۷۰ یعنی ۷۰٪. اگر استراتژی فقط در نصف برش‌های مستقل تاریخ سودده
                   باشد، نتیجه‌اش از شیر یا خط بهتر نیست.</>}>
            <NumberField value={Number(val("research.cpcv_positive_path_fraction"))} step={0.05}
                         min={0.5} max={1} disabled={readOnly}
                         onChange={(v) => set("research.cpcv_positive_path_fraction", v)} />
          </Field>
          <Field label="در آزمون، هزینه‌ها را چند برابر فرض کن"
                 hint={<>عدد ۲ یعنی همه‌چیز با فرض دو برابر هزینه واقعی دوباره حساب شود.</>}
                 help="استراتژی‌ای که فقط با خوش‌بینانه‌ترین فرض هزینه سودده است، در واقعیت سودده نخواهد بود.">
            <NumberField value={Number(val("research.cost_stress_multiple"))} step={0.5} min={1} max={10}
                         suffix="برابر" disabled={readOnly}
                         onChange={(v) => set("research.cost_stress_multiple", v)} />
          </Field>
          <Field label="در آزمون، کندی اجرا را چند برابر فرض کن"
                 help="همان منطق، این بار برای تأخیر: اگر سود فقط با فرض اجرای فوری وجود داشته باشد، سود واقعی نیست.">
            <NumberField value={Number(val("research.latency_stress_multiple"))} step={0.5} min={1} max={10}
                         suffix="برابر" disabled={readOnly}
                         onChange={(v) => set("research.latency_stress_multiple", v)} />
          </Field>
          <Field label="باید ثابت کند از حدس ساده «فردا مثل امروز» بهتر است"
                 hint={<>اگر مدل نتواند ساده‌ترین حدس ممکن را شکست بدهد، هیچ اطلاعاتی اضافه
                   نکرده — فقط پیچیده‌تر است.</>}>
            {bool("research.require_random_walk_beat")}
          </Field>
          <Field label="باید ثابت کند سودش از عوامل عمومی بازار نیامده"
                 hint={<>مثال: اگر یک استراتژی فقط وقتی سود می‌دهد که دلار ضعیف شود، آن سود
                   مال استراتژی نیست، مال یک روند عمومی است که هر کسی می‌توانست بگیرد.</>}>
            {bool("research.require_factor_alpha")}
          </Field>
        </Card>
      )}

      {section === "ops" && (
        <>
          <Card title="نگهبانی و توقف اضطراری">
            <Field label="هر چند ثانیه علامت زنده بودن بفرستد"
                   hint="یک نبض ساده که به برنامه نگهبان می‌گوید سامانه اصلی هنوز کار می‌کند.">
              {int("ops.heartbeat_interval_sec", { min: 1, max: 120, suffix: "ثانیه" })}
            </Field>
            <Field label="بعد از چند ثانیه سکوت، نگهبان وارد عمل شود"
                   hint={<>برنامه نگهبان جداگانه اجرا می‌شود. اگر این مدت نبضی نرسد، خودش
                     تصمیم می‌گیرد — بدون اینکه از سامانه اصلی اجازه بگیرد.</>}
                   help="یک سامانه از کار افتاده با معامله باز، خطرناک‌ترین حالت ممکن است.">
              {int("ops.deadman_timeout_sec", { min: 5, max: 3600, suffix: "ثانیه" })}
            </Field>
            <Field label="نگهبان در آن لحظه چه کند"
                   hint={<>«فقط هشدار» یعنی خبر می‌دهد · «فقط اجازه بستن» یعنی معامله جدید نه،
                     بستن آری · «همه را ببند» یعنی همه معامله‌های باز را می‌بندد.</>}>
              <Seg value={String(val("ops.deadman_action"))}
                   onChange={(v) => set("ops.deadman_action", v)}
                   options={[{ value: "alert", label: "فقط هشدار" },
                             { value: "close_only", label: "فقط اجازه بستن" },
                             { value: "flatten", label: "همه را ببند" }]} />
            </Field>
            <Field label="مسیر فایلی که با ساختنش همه‌چیز متوقف می‌شود"
                   hint={<>یک فایل ساده روی دیسک. کافی است آن را بسازید تا ظرف یک چرخه،
                     ربات هیچ معامله تازه‌ای باز نکند.</>}
                   help="هیچ بخشی از سامانه معاملاتی اجازه حذف این فایل را ندارد — فقط شما.">
              <input className="input mono" disabled value={String(val("ops.killswitch_file"))} />
            </Field>
          </Card>
          <Card title="امنیت">
            <Field label="این صفحه به‌صورت پیش‌فرض فقط اجازه دیدن می‌دهد"
                   hint="ورود به سامانه به‌تنهایی هیچ اجازه تغییری نمی‌دهد."
                   help="این قفل عمدی است و خاموش نمی‌شود.">
              <Switch checked disabled onChange={() => undefined} label="read only" />
            </Field>
            <Field label="هر تغییر، یک کد شش‌رقمی تازه می‌خواهد"
                   hint={<>یعنی اگر کسی هم به نشست شما دسترسی پیدا کند، بدون گوشی شما
                     نمی‌تواند هیچ معامله‌ای بگذارد یا تنظیمی را عوض کند.</>}
                   help="این هم عمدی است و خاموش نمی‌شود.">
              <Switch checked disabled onChange={() => undefined} label="totp" />
            </Field>
            <Field label="بعد از چند دقیقه بی‌کاری، دوباره ورود لازم باشد">
              {int("security.session_ttl_minutes", { min: 5, max: 480, suffix: "دقیقه" })}
            </Field>
            <Field label="این صفحه روی چه آدرسی در دسترس است"
                   hint={<>مقدار پیش‌فرض یعنی «فقط روی همین کامپیوتر». باز کردن این صفحه روی
                     اینترنت عمومی، عملاً یعنی در اختیار گذاشتن حساب.</>}>
              <input className="input mono" disabled value={String(val("security.bind_host"))} />
            </Field>
            <Field label="بیشترین تعداد درخواست خواندن در دقیقه"
                   help="جلوی فشار بیش از حد به سامانه را می‌گیرد.">
              {int("security.api_rate_limit_per_minute", { min: 10, max: 10000 })}
            </Field>
            <Field label="بیشترین تعداد تغییر در دقیقه"
                   help="حتی با کد درست، بیش از این تعداد تغییر در دقیقه پذیرفته نمی‌شود.">
              {int("security.write_rate_limit_per_minute", { min: 1, max: 600 })}
            </Field>
            <Banner tone="info" icon="🔐">
              دو توصیه برای کسی که این سامانه را نصب می‌کند: برنامه معامله‌گر و این صفحه باید
              با دو کاربر سیستمی جدا اجرا شوند، و کلید دسترسی به بروکر فقط باید در متغیر
              محیطی سرور نگه داشته شود — نه داخل کد، نه در گیت، نه در فایل تنظیمات.
            </Banner>
          </Card>
        </>
      )}

      {section === "strategies" && (
        <Card title="کدام استراتژی روشن است و با چه وزنی"
              hint={<>«وزن» یعنی چه سهمی از مبلغی که در مجموع ریسک می‌شود به این استراتژی
                می‌رسد. وزن ۰٫۵ یعنی نصف سهم یک استراتژی با وزن ۱.</>}>
          <div className="stack gap12">
            {Object.values(snap.allocations).map((a: any) => (
              <div key={a.name} className="row gap12 wrap"
                   style={{ padding: 12, borderRadius: 14, background: "var(--surface-alt)" }}>
                <Switch checked={Boolean(a.enabled)} disabled={readOnly}
                        onChange={() => undefined} label={a.name} />
                <span className="mono" style={{ fontWeight: 600 }}>{a.name}</span>
                <Chip title="هر کندل نمودار چقدر زمان را نشان می‌دهد">
                  {TIMEFRAME_FA[a.timeframe] ?? a.timeframe}
                </Chip>
                <Chip tone={a.lifecycle === "accepted" ? "pos" : "warn"}
                      title="مرحله عمر این استراتژی">
                  {LIFECYCLE_FA[a.lifecycle] ?? a.lifecycle}
                </Chip>
                <span className="fs12 muted grow">{(a.instruments ?? []).join("، ")}</span>
                <span className="num fs12" title="سهم این استراتژی از مبلغی که در مجموع ریسک می‌شود">
                  وزن {a.weight}
                </span>
              </div>
            ))}
          </div>
          <Banner tone="flat" icon="ℹ">
            روشن کردن یک استراتژی برای پول واقعی فقط از یک راه ممکن است: گذراندن همه
            آزمون‌های صفحه «آزمایش و اثبات». استراتژی‌ای که هنوز «فقط یک ایده» یا «در حال
            آزمایش» است می‌تواند روی حساب تمرینی معامله کند، ولی روی پول واقعی سفارشش رد
            می‌شود — حتی اگر اینجا روشن باشد.
          </Banner>
        </Card>
      )}

      <ConfirmWrite open={confirmOpen} action="ذخیره تغییرات تنظیمات"
                    description={
                      <div className="stack gap8">
                        <span>{dirty} تنظیم عوض می‌شود:</span>
                        <div className="stack gap4 scroll-y" style={{ maxHeight: 180 }}>
                          {Object.entries(changes).map(([p, v]) => (
                            <KV key={p} k={<span className="mono fs11">{p}</span>}
                                v={<>{String(getPath(cfg, p))} → <strong>{String(v)}</strong></>} />
                          ))}
                        </div>
                        <span className="muted fs12">
                          تنظیمات پیش از اعمال بررسی می‌شوند؛ اگر حتی یک مقدار با بقیه
                          ناسازگار باشد، هیچ‌کدام از تغییرها اعمال نمی‌شود. نسخه جدید و فهرست
                          دقیق تفاوت‌ها در دفتر ثبت رویدادها می‌ماند.
                        </span>
                      </div>
                    }
                    onClose={() => setConfirmOpen(false)}
                    onConfirm={async (totp) => {
                      const res = await write("/api/config", { patch: buildPatch(changes) }, totp);
                      if (res.ok) setChanges({});
                      return res;
                    }} />
    </div>
  );
}
