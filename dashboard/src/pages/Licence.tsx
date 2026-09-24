/**
 * Licence status, renewal, and an honest account of what the lock does.
 *
 * Two rules shape this page.
 *
 * 1. **The renewal warning has to land before it is urgent.** A quarterly
 *    licence gives the customer four chances a year to be surprised, so the
 *    ladder escalates in WORDING, not only in a number: "۲۹ روز" and "۲ روز"
 *    look equally unalarming in a table.
 *
 * 2. **The page does not oversell the protection.** It says, in the customer's
 *    own language, that no licence running on their own server can stop
 *    someone with full access to that server. A licensing screen that claims
 *    to be unbreakable is a screen that will be disbelieved about everything
 *    else too.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  Banner, Card, Chip, ConfirmWrite, Disclosure, Field, KV, Modal,
} from "../components/ui";
import type { Provider } from "../api";
import type { LicenceView } from "../types";

type Props = {
  provider: Provider;
  write: (path: string, body: unknown, totp: string) => Promise<{ ok: boolean; detail: string }>;
  readOnly: boolean;
  canAdminister: boolean;
};

/* "clock" is the stage the backend sets when the anti-rollback guard finds the
   machine's clock wound back. It was missing from both maps, so it fell to the
   defaults and a TAMPERED, non-trading licence rendered with a neutral banner
   and a GREEN day count. An unknown stage now falls to a warning, not to
   reassurance. */
const STAGE_TONE: Record<string, "pos" | "info" | "warn" | "neg"> = {
  healthy: "pos", perpetual: "pos", approaching: "info",
  due: "warn", critical: "neg", expired: "neg", clock: "neg", none: "neg",
};

const STAGE_BANNER: Record<string, "info" | "warn" | "neg" | "flat"> = {
  healthy: "flat", perpetual: "flat", approaching: "info",
  due: "warn", critical: "neg", expired: "neg", clock: "neg", none: "neg",
};

/** The Persian calendar date, which is the one this audience reads.
 *  The ISO date is kept beside it, because that is what a vendor's invoice and
 *  the licence file itself carry. */
function jalali(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Intl.DateTimeFormat("fa-IR-u-ca-persian",
      { year: "numeric", month: "long", day: "numeric" }).format(new Date(iso));
  } catch {
    return iso.slice(0, 10);
  }
}

const CAPABILITY_FA: Record<string, string> = {
  live_trading: "معاملهٔ واقعی",
  max_instruments: "بیشترین تعداد نماد",
  max_accounts: "بیشترین تعداد حساب",
  max_equity: "سقف موجودی حساب",
  research_lab: "آزمایشگاه پژوهش",
  llm_news: "تحلیل خبر",
};

export default function Licence({ provider, write, readOnly, canAdminister }: Props) {
  const [lic, setLic] = useState<LicenceView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fingerprint, setFingerprint] = useState<Record<string, string> | null>(null);
  const [paste, setPaste] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown;
  }>(null);

  const load = useCallback(async () => {
    try {
      setLic(await provider.get<LicenceView>("/api/licence"));
      setError(null);
    } catch (e) { setError(String(e)); }
  }, [provider]);

  useEffect(() => { void load(); }, [load]);

  if (error && !lic) {
    return (
      <div className="stack gap12">
        <Banner tone="neg" icon="✕"><span dir="ltr">{error}</span></Banner>
        <button className="btn outline sm" style={{ alignSelf: "start" }}
                onClick={() => void load()}>تلاش دوباره</button>
      </div>
    );
  }
  if (!lic) return <div className="muted fs12">در حال بارگذاری…</div>;

  // Two response shapes. When the build has no licence gate at all the
  // endpoint returns only {enforced:false, note}, so `unlicensed_mode` is
  // undefined, the guard below did not fire, and the page threw on
  // `lic.warnings.length` -- which, with no error boundary, unmounted the
  // whole console and left a blank page.
  if (lic.unlicensed_mode || lic.enforced === false || !Array.isArray(lic.warnings)) {
    return (
      <div className="stack gap16">
        <Card title="این نسخه قفل لایسنس ندارد">
          <Banner tone="info" icon="◎">
            {lic.note ?? ("هیچ کلید عمومی فروشنده‌ای در این نسخه نیست، پس لایسنس " +
              "بررسی نمی‌شود. برای نصبی که خودتان صاحبش هستید این درست است؛ " +
              "برای نسخه‌ای که به دیگران داده می‌شود نه.")}
          </Banner>
        </Card>
        <HonestyCard />
      </div>
    );
  }

  const days = lic.days_remaining;
  const tone = STAGE_TONE[lic.stage] ?? (lic.valid ? "warn" : "neg");

  return (
    <div className="stack gap16">
      {/* Previously set and never rendered once the page had loaded once, so
          the fingerprint button simply did nothing when it failed. */}
      {error && (
        <Banner tone="neg" icon="✕">
          یک درخواست ناموفق بود: <span dir="ltr">{error}</span>
        </Banner>
      )}

      {/* ---------- the headline ---------- */}
      <Card title="وضعیت لایسنس">
        <Banner tone={STAGE_BANNER[lic.stage] ?? (lic.valid ? "warn" : "neg")}
                icon={lic.valid ? (lic.stage === "healthy" ? "✓" : "⏳") : "✕"}>
          <div className="stack gap4">
            <strong style={{ fontSize: 14 }}>{lic.headline || (lic.valid ? "فعال" : "غیرفعال")}</strong>
            <span>{lic.advice || lic.reason}</span>
          </div>
        </Banner>

        <div className="kv-grid mt8">
          <KV k="مدت" v={lic.term_label ?? "—"} />
          <KV k="دورهٔ چندم"
              v={lic.term_index ? <span className="num">{lic.term_index}</span> : "—"}
              hint={<>هر بار که تمدید می‌کنید یک شماره جلو می‌رود. دورهٔ ۴ یعنی
                یک سال کامل اشتراک سه‌ماهه.</>} />
          <KV k="تا تاریخ"
              v={<span className="stack gap4" style={{ alignItems: "flex-end" }}>
                <span>{jalali(lic.expires_at)}</span>
                <span className="mono ltr fs11 faint">
                  {(lic.expires_at ?? "—").slice(0, 10)}
                </span>
              </span>} />
          <KV k="روز باقی‌مانده"
              tone={!lic.valid || tone === "neg" ? "neg"
                : tone === "warn" ? "warn" : "pos"}
              v={days === null ? "بی‌پایان"
                : <span className="num">{Math.round(days)}</span>} />
          <KV k="صادر شده برای" v={lic.issued_to ?? "—"} />
          <KV k="بسته به این دستگاه؟"
              v={lic.machine_bound ? "بله" : "نه — روی هر دستگاهی کار می‌کند"}
              hint={<>لایسنس بسته‌شده فقط روی همین سرور کار می‌کند. اگر سخت‌افزار
                را عوض کنید باید از فروشنده بخواهید دوباره صادرش کند.</>} />
        </div>

        {days !== null && days > 0 && (
          <div className="mt8">
            {/* `.meter > i` is what the stylesheet fills; a <span> here rendered
                an empty grey bar that looked like a licence with zero days left. */}
            <div className="meter" role="img"
                 aria-label={`${Math.round(days)} روز از دوره باقی مانده`}>
              <i style={{
                width: `${Math.max(2, Math.min(100, (days / ((lic.term_months || 3) * 30.44)) * 100))}%`,
              }} />
            </div>
            <div className="row fs11 faint" style={{ justifyContent: "space-between" }}>
              <span>امروز</span>
              <span>{jalali(lic.expires_at)}</span>
            </div>
          </div>
        )}

        {lic.warnings.length > 0 && (
          <div className="stack gap8 mt8">
            {lic.warnings.map((w, i) => (
              <Banner key={i} tone="warn" icon="⚠"><span className="fs12">{w}</span></Banner>
            ))}
          </div>
        )}
      </Card>

      {/* ---------- what it permits ---------- */}
      {lic.effective_capabilities && (
        <Card title="این لایسنس چه چیزی را اجازه می‌دهد"
              sub="همهٔ سطح‌ها همان موتور ایمنی و همان آزمون‌های پذیرش را دارند؛ فرق فقط در ظرفیت است">
          <div className="kv-grid">
            {Object.entries(lic.effective_capabilities).map(([k, v]) => (
              <KV key={k} k={CAPABILITY_FA[k] ?? k}
                  v={v === null ? "بدون محدودیت"
                    : v === true ? "دارد" : v === false ? "ندارد"
                      : <span className="num">{String(v)}</span>}
                  tone={v === false ? "muted" : undefined} />
            ))}
          </div>
          {lic.live_trading_allowed === false && lic.live_trading_reason && (
            <div className="mt8">
              <Banner tone="warn" icon="◈">{lic.live_trading_reason}</Banner>
            </div>
          )}
        </Card>
      )}

      {/* ---------- the clock guard ---------- */}
      {lic.clock && (
        <Card title="محافظ ساعت"
              sub="ساده‌ترین راه دور زدن یک لایسنس مدت‌دار، عقب کشیدن ساعت سرور است">
          <div className="kv-grid">
            <KV k="وضعیت"
                v={<Chip tone={lic.clock.ok ? "pos" : "neg"}>
                  {lic.clock.ok ? "سالم" : "مشکل دارد"}</Chip>} />
            <KV k="تعداد بررسی" v={<span className="num">{lic.clock.checks}</span>} />
          </div>
          {!lic.clock.ok && (
            <div className="mt8"><Banner tone="neg" icon="⏱">{lic.clock.message}</Banner></div>
          )}
          {(lic.clock.state_missing || lic.clock.state_unreadable) && lic.clock.ok && (
            <div className="mt8">
              <Banner tone="warn" icon="⚠">
                سابقهٔ زمانی این نصب پیدا نشد یا خوانده نشد. یک نصب تازه، یک
                بازگردانی از پشتیبان و یک دستکاری، هر سه دقیقاً همین شکلی‌اند —
                پس فقط ثبت شده و جلوی کاری گرفته نشده.
              </Banner>
            </div>
          )}
          <div className="fs12 muted mt8">
            سامانه بالاترین زمانی را که تا حالا دیده به‌خاطر می‌سپارد. اگر ساعت
            سرور بیشتر از شش ساعت عقب برود، لایسنس تا وقتی ساعت درست نشود بررسی
            نمی‌شود. اصلاح‌های کوچک ساعت (مثل همگام‌سازی اینترنتی) مشکلی ندارند.
          </div>
        </Card>
      )}

      {/* ---------- renewal / install ---------- */}
      <Card title="تمدید یا نصب لایسنس"
            actions={canAdminister && (
              <button className="btn sm" disabled={readOnly}
                      title={readOnly ? "در این حالت تغییری ممکن نیست" : ""}
                      onClick={() => setPaste("")}>وارد کردن لایسنس</button>
            )}>
        <div className="stack gap12">
          <div className="fs12">
            لایسنس یک فایل متنی کوتاه است که با امضای دیجیتال فروشنده مهر شده.
            برای تمدید، فایل تازه را از فروشنده بگیرید و اینجا بچسبانید.
          </div>
          <Banner tone="flat" icon="◎">
            <strong>تمدید زودهنگام چیزی از شما نمی‌گیرد.</strong> دورهٔ جدید از
            روزی شروع می‌شود که دورهٔ قبلی تمام می‌شد، نه از امروز — پس اگر یک
            هفته زودتر تمدید کنید، آن هفته را دو بار نمی‌پردازید.
          </Banner>

          {canAdminister && (
            <Disclosure summary="فروشنده برای صدور لایسنس چه چیزی از من می‌خواهد؟">
              <div className="stack gap8">
                <div className="fs12">
                  یک «اثر انگشت دستگاه». مقادیر زیر هش‌شده‌اند و چیزی از محتوای
                  سرور شما را لو نمی‌دهند — فقط برای این‌اند که لایسنس به همین
                  دستگاه بسته شود.
                </div>
                <button className="btn outline sm" disabled={readOnly}
                        title={readOnly ? "در این حالت تغییری ممکن نیست" : ""}
                        onClick={async () => {
                          try {
                            const r = await provider.get<{ fingerprint: Record<string, string> }>(
                              "/api/licence/fingerprint");
                            setFingerprint(r.fingerprint);
                          } catch (e) { setError(String(e)); }
                        }}>
                  نشان بده
                </button>
                {fingerprint && (
                  <div className="stack gap4">
                    <pre className="mono fs11 ltr scroll-y"
                         style={{ maxHeight: 160, margin: 0, whiteSpace: "pre-wrap" }}>
                      {JSON.stringify({ fingerprint }, null, 2)}
                    </pre>
                    <button className="btn ghost sm" onClick={() => {
                      void navigator.clipboard?.writeText(
                        JSON.stringify({ fingerprint }, null, 2));
                    }}>کپی</button>
                  </div>
                )}
              </div>
            </Disclosure>
          )}
        </div>
      </Card>

      <HonestyCard />

      <Modal open={paste !== null} title="وارد کردن فایل لایسنس"
             onClose={() => setPaste(null)}
             footer={
               <>
                 <button className="btn ghost" onClick={() => setPaste(null)}>انصراف</button>
                 <button className="btn" disabled={!paste || paste.length < 64}
                         title={!paste || paste.length < 64
                           ? "متن لایسنس هنوز کامل نیست" : ""}
                         onClick={() => setConfirm({
                           action: "نصب لایسنس تازه",
                           description: <>لایسنس اول بررسی می‌شود و فقط اگر امضایش
                             درست بود جایگزین می‌شود. نسخهٔ قبلی کنار گذاشته می‌شود،
                             پاک نمی‌شود.</>,
                           path: "/api/licence/install", body: { document: paste },
                         })}>
                   بررسی و نصب
                 </button>
               </>
             }>
        <div className="stack gap8">
          <Field label="متن کامل فایل لایسنس" htmlFor="lic-paste"
                 help="از خط BEGIN تا خط END، هر دو را هم شامل شود. تا وقتی متن کامل نباشد دکمهٔ «بررسی و نصب» فعال نمی‌شود.">
            <textarea className="input mono ltr" id="lic-paste" rows={10} value={paste ?? ""}
                      dir="ltr" style={{ resize: "vertical" }}
                      onChange={(e) => setPaste(e.target.value)} />
          </Field>
        </div>
      </Modal>

      <ConfirmWrite
        open={!!confirm} action={confirm?.action ?? ""}
        description={confirm?.description}
        onClose={() => setConfirm(null)}
        onConfirm={async (totp) => {
          if (!confirm) return { ok: false, detail: "" };
          const res = await write(confirm.path, confirm.body, totp);
          if (res.ok) {
            setPaste(null);
            await load();
            // Close the dialog on success. Leaving it open stacked it over
            // whatever the page wanted to show next.
            setTimeout(() => setConfirm(null), 900);
          }
          return res;
        }} />
    </div>
  );
}

function HonestyCard() {
  return (
    <Card title="این قفل دقیقاً چه کاری می‌کند و چه کاری نمی‌کند"
          sub="این را نوشته‌ایم چون یک بخش لایسنس که بیش از توانش ادعا کند، بدتر از نداشتنش است">
      <div className="stack gap12 fs12">
        <div>
          <strong>چیزهایی که واقعاً تضمین می‌شوند</strong>
          <ul style={{ margin: "6px 0 0", paddingInlineStart: 18 }}>
            <li>هیچ‌کس بدون کلید خصوصی فروشنده نمی‌تواند لایسنس بسازد. این یک
              تضمین ریاضی است، نه یک ترفند برنامه‌نویسی.</li>
            <li>عوض کردن حتی یک نویسه از فایل لایسنس، امضا را باطل می‌کند.</li>
            <li>لایسنس را می‌شود به یک دستگاه بست؛ کپی کردنش روی سرور دیگر کار نمی‌کند.</li>
            <li>عقب کشیدن ساعت سرور برای زنده کردن لایسنس تمام‌شده، گرفته می‌شود.</li>
            <li>دست بردن در فایل‌های حساس برنامه قابل تشخیص است و جلوی معاملهٔ
              واقعی را می‌گیرد.</li>
          </ul>
        </div>
        <div>
          <strong>چیزی که تضمین نمی‌شود</strong>
          <div style={{ marginTop: 6 }}>
            این نرم‌افزار روی سروری اجرا می‌شود که <em>شما</em> صاحبش هستید.
            کسی که دسترسی کامل به آن سرور دارد، می‌تواند خودِ بررسی لایسنس را از
            کد بردارد. این دربارهٔ هر قفل نرم‌افزاری در هر زبانی صادق است؛
            فشرده‌سازی و مبهم‌سازی هزینهٔ این کار را بالا می‌برند، ولی نتیجه را
            عوض نمی‌کنند. هر فروشنده‌ای که بگوید قفلش «غیرقابل شکستن» است، یا
            این را نمی‌داند یا دارد چیزی می‌فروشد.
          </div>
        </div>
        <Banner tone="flat" icon="◎">
          تنها راه واقعی این است که بخش باارزش روی سروری بماند که فروشنده
          کنترلش می‌کند. سامانه برای همین یک قلاب «فعال‌سازی آنلاین» دارد. بقیهٔ
          چیزها قفل خوبی‌اند روی دری که صاحبش خودتان هستید.
        </Banner>
      </div>
    </Card>
  );
}
