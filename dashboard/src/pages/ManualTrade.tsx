import React, { useMemo, useState } from "react";
import type { Provider } from "../api";
import {
  Banner, Card, Chip, ConfirmWrite, Disclosure, Empty, Field, Hint, KV, Seg, money,
} from "../components/ui";
import type { Decision, Snapshot } from "../types";
import { VETO_FA } from "./Overview";

/* A human's own trade. The page says, before anything else, that the robot's
   safety rules apply to it exactly as they apply to the robot: a manual ticket
   is not a way around a limit, it is another way in through the same door. */

const MAJORS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CHF", "USD_CAD",
                "NZD_USD", "EUR_JPY"];

const MODE_FA: Record<string, string> = {
  observe: "فقط تماشا", advisory: "فقط پیشنهاد",
  semi_auto: "نیمه‌خودکار", autonomous: "کاملاً خودکار",
};

export default function ManualTrade({ snap, provider, write, readOnly }: {
  snap: Snapshot; provider: Provider; readOnly: boolean;
  write: (path: string, body: unknown, totp: string) => Promise<{ ok: boolean; detail: string }>;
}) {
  const instruments = useMemo(() => {
    const set = new Set<string>(MAJORS);
    (snap.config?.strategies ?? []).forEach((a: any) =>
      (a.instruments ?? []).forEach((i: string) => set.add(i)));
    (snap.config?.agent?.semi_auto_envelope?.instruments ?? []).forEach((i: string) => set.add(i));
    snap.positions.forEach((p) => set.add(p.instrument));
    return [...set].sort();
  }, [snap]);

  const [instrument, setInstrument] = useState(instruments[0] ?? "EUR_USD");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [stop, setStop] = useState("");
  const [target, setTarget] = useState("");
  const [risk, setRisk] = useState("");
  const [preview, setPreview] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);

  const s = snap.status;
  const live = s.venue_mode === "live";
  const liveAllowed = !!snap.config?.agent?.manual_trading_live;
  const budget = Number(snap.config?.risk?.risk_per_trade_pct ?? 0.5);
  const decimal = /^[0-9]+(\.[0-9]+)?$/;
  const valid = decimal.test(stop) && (!target || decimal.test(target))
    && (!risk || decimal.test(risk));

  const query = () => {
    const q = new URLSearchParams({ instrument, side, stop_loss: stop });
    if (target) q.set("take_profit", target);
    if (risk) q.set("risk_pct", risk);
    return q.toString();
  };

  async function runPreview() {
    setBusy(true); setError(null); setPreview(null);
    try {
      setPreview(await provider.get<Decision>(`/api/trade/preview?${query()}`));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const approved = preview?.action === "preview";

  return (
    <div className="stack gap16">
      <Banner tone="info" icon="🛡">
        <strong>قواعد ایمنی روی معامله دستی شما هم اجرا می‌شوند.</strong> حد ضرر اجباری
        است، حجم را خود سامانه از روی درصد ریسک حساب می‌کند، و همه سقف‌ها — ضرر روزانه،
        افت حساب، نزدیکی خبر مهم، تعداد معامله، اندازه کل ریسک — دقیقاً مثل معامله‌های ربات
        بررسی می‌شوند. هیچ دکمه‌ای برای دور زدن آن‌ها وجود ندارد. بعد از باز شدن، ربات این
        معامله را هم مثل بقیه محافظت می‌کند (انتقال حد ضرر به نقطه سربه‌سر، حد ضرر متحرک،
        بستن قبل از تعطیلی آخر هفته).
      </Banner>

      {live && !liveAllowed && (
        <Banner tone="warn" icon="🔒">
          حساب روی <strong>پول واقعی</strong> است و معامله دستی با پول واقعی خاموش است.
          فقط مدیر سامانه می‌تواند آن را در تنظیمات (<span className="mono">agent.manual_trading_live</span>)
          روشن کند. پیش‌نمایش همچنان کار می‌کند.
        </Banner>
      )}

      <div className="grid g-2-1">
        <Card title="ثبت یک معامله دستی"
              hint={<>شما فقط تصمیم می‌گیرید چه جفت‌ارزی، خرید یا فروش، و حد ضرر کجا باشد.
                اندازه معامله را سامانه طوری حساب می‌کند که اگر حد ضرر خورد، فقط همان درصد
                تعیین‌شده از حساب از دست برود.</>}
              sub="اول پیش‌نمایش بگیرید؛ هیچ سفارشی بدون تأیید دومرحله‌ای شما فرستاده نمی‌شود">
          <div className="stack gap12">
            <Field label="جفت‌ارز">
              <select className="input" value={instrument}
                      onChange={(e) => { setInstrument(e.target.value); setPreview(null); }}>
                {instruments.map((i) => <option key={i} value={i}>{i}</option>)}
              </select>
            </Field>
            <Field label="جهت معامله"
                   hint="خرید یعنی امید دارید قیمت بالا برود؛ فروش یعنی امید دارید پایین بیاید.">
              <Seg value={side} onChange={(v) => { setSide(v); setPreview(null); }}
                   options={[{ value: "BUY", label: "خرید" }, { value: "SELL", label: "فروش" }]} />
            </Field>
            <Field label="حد ضرر (قیمت)" term="Stop Loss"
                   hint={<>قیمتی که اگر بازار به آن رسید، معامله خودکار بسته می‌شود. برای خرید
                     باید پایین‌تر از قیمت فعلی باشد و برای فروش بالاتر. اجباری است.</>}>
              <input className="input num ltr" inputMode="decimal" value={stop}
                     placeholder="مثلاً 1.08000"
                     onChange={(e) => { setStop(e.target.value.trim()); setPreview(null); }} />
            </Field>
            <Field label="قیمت هدف (اختیاری)" term="Take Profit"
                   hint="قیمتی که اگر بازار به آن رسید، معامله با سود بسته می‌شود.">
              <input className="input num ltr" inputMode="decimal" value={target}
                     placeholder="مثلاً 1.09500"
                     onChange={(e) => { setTarget(e.target.value.trim()); setPreview(null); }} />
            </Field>
            <Field label={`ریسک این معامله (٪ حساب، حداکثر ${budget})`}
                   hint={<>اگر خالی بماند، همان درصد ریسک عادی سامانه ({budget}٪) استفاده
                     می‌شود. می‌توانید کمتر بگذارید، ولی بیشتر از سقف پذیرفته نمی‌شود.</>}>
              <input className="input num ltr" inputMode="decimal" value={risk}
                     placeholder={String(budget)}
                     onChange={(e) => { setRisk(e.target.value.trim()); setPreview(null); }} />
            </Field>
            {error && <Banner tone="neg" icon="✕">{error}</Banner>}
            <div className="row gap8">
              {/* The preview is a read. In the demo it runs against sample data so
                  the page can be explored; a live viewer is refused by the server. */}
              <button className="btn ghost"
                      disabled={!valid || busy || (readOnly && provider.kind !== "demo")}
                      onClick={runPreview}>
                {busy ? "در حال بررسی…" : "پیش‌نمایش (هیچ سفارشی فرستاده نمی‌شود)"}
              </button>
              <button className="btn" disabled={!approved || readOnly || (live && !liveAllowed)}
                      onClick={() => setConfirm(true)}>ثبت معامله</button>
            </div>
            {readOnly && (
              <span className="fs12 muted">این حساب کاربری فقط اجازه دیدن دارد.</span>
            )}
          </div>
        </Card>

        <Card title="نتیجه بررسی"
              hint="همان بررسی‌ای که برای معامله‌های ربات انجام می‌شود، روی معامله شما.">
          {!preview ? (
            <Empty>برای دیدن حجم، ریسک و اینکه آیا قواعد ایمنی اجازه می‌دهند، پیش‌نمایش بگیرید</Empty>
          ) : (
            <div className="stack gap12">
              <Chip tone={approved ? "pos" : "neg"}>
                {approved ? "قواعد ایمنی اجازه می‌دهند" : "قواعد ایمنی اجازه نمی‌دهند"}
              </Chip>
              {approved && (
                <div className="kv-grid c2">
                  <KV k="حجم معامله (لات)" v={preview.lots ?? "—"} />
                  <KV k="مبلغ در معرض ریسک" v={money(preview.risk_amount ?? 0)} />
                  <KV k="درصد حساب در معرض ریسک" v={`${Number(preview.risk_pct ?? 0).toFixed(2)}٪`} />
                  <KV k="قیمت ورود تقریبی" v={preview.entry ?? "—"} />
                  <KV k="سود احتمالی چند برابر ریسک"
                      v={String(preview.diagnostics?.reward_risk ?? "—")} />
                  <KV k="درصد برد لازم برای سربه‌سر"
                      hint="با احتساب هزینه‌ها؛ هر چه کمتر، بهتر."
                      v={preview.diagnostics?.break_even_win_rate
                        ? `${(Number(preview.diagnostics.break_even_win_rate) * 100).toFixed(1)}٪`
                        : "—"} />
                </div>
              )}
              {preview.vetoes.length > 0 && (
                <div className="stack gap6">
                  {preview.vetoes.map((v, i) => (
                    <Banner key={i} tone="neg" icon="✕">
                      <strong>{VETO_FA[v.rule] ?? MANUAL_VETO_FA[v.rule] ?? v.rule}</strong>
                      <div className="fs12 muted ltr" style={{ textAlign: "start" }}>{v.message}</div>
                    </Banner>
                  ))}
                </div>
              )}
              {preview.lessons?.length > 0 && (
                <Disclosure summary="چرا حجم کوچک‌تر شد">
                  <ul className="fs12">{preview.lessons.map((l, i) => <li key={i}>{l}</li>)}</ul>
                </Disclosure>
              )}
            </div>
          )}
        </Card>
      </div>

      <Card title="بخش خودکار"
            hint="حالت ربات را فقط مدیر می‌تواند در صفحه تنظیمات عوض کند (با کد دومرحله‌ای).">
        <div className="kv-grid c3">
          <KV k="حالت فعلی ربات" v={MODE_FA[s.mode] ?? s.mode} />
          <KV k="پیشنهادهای منتظر تأیید شما" v={s.advisory_pending} />
          <KV k="اجازه معامله تازه از نظر لایسنس"
              v={s.entries_permitted?.allowed === false ? "ندارد" : "دارد"}
              tone={s.entries_permitted?.allowed === false ? "neg" : undefined} />
        </div>
        <p className="fs12 muted" style={{ lineHeight: 1.9, marginTop: 8 }}>
          در حالت «فقط پیشنهاد»، ربات فرصت‌ها را در صفحه «معامله‌های باز» به شما پیشنهاد می‌دهد
          و شما تأیید یا رد می‌کنید. در «نیمه‌خودکار» فقط معامله‌های کوچک و روی جفت‌ارزهای
          مجاز را خودش انجام می‌دهد. در «کاملاً خودکار» همه را خودش انجام می‌دهد — ولی همیشه
          زیر همین قواعد ایمنی.
        </p>
      </Card>

      <ConfirmWrite open={confirm} danger={live}
                    action={`${side === "BUY" ? "خرید" : "فروش"} ${instrument}`}
                    description={<>
                      سفارش {side === "BUY" ? "خرید" : "فروش"} {instrument} به حجم{" "}
                      <strong className="num">{preview?.lots}</strong> لات با حد ضرر{" "}
                      <span className="num">{stop}</span>
                      {target && <> و هدف <span className="num">{target}</span></>} فرستاده
                      می‌شود. سامانه درست قبل از ارسال، همه قواعد ایمنی را با قیمت همان لحظه
                      دوباره بررسی می‌کند.
                      {live && <strong> این معامله با پول واقعی است.</strong>}
                    </>}
                    onClose={() => { setConfirm(false); setPreview(null); }}
                    onConfirm={(totp) => write("/api/trade/manual", {
                      instrument, side, stop_loss: stop,
                      take_profit: target || null, risk_pct: risk || null,
                    }, totp)} />
    </div>
  );
}

const MANUAL_VETO_FA: Record<string, string> = {
  manual_live_disabled: "معامله دستی با پول واقعی خاموش است",
  licence: "لایسنس اجازه معامله تازه نمی‌دهد",
  out_of_session: "الان خارج از ساعت‌های مجاز معامله است",
  malformed_ticket: "اطلاعات معامله کامل یا درست نیست",
  venue_stop_distance: "حد ضرر از حداقل فاصله بروکر نزدیک‌تر است",
  performance_guard: "این استراتژی به‌خاطر ضرر مداوم معلق شده",
  meta_label: "فیلتر دوم این فرصت را ضعیف ارزیابی کرد",
  rolling_24h_loss: "سقف ضرر ۲۴ ساعت گذشته پر شده",
  exposure_unconvertible: "ریسک به ارز حساب قابل محاسبه نبود",
  group_total_risk: "جمع ریسک همه حساب‌های شما از سقف می‌زند",
  group_currency_exposure: "روی این ارز در همه حساب‌ها روی هم زیاد شرط بسته‌اید",
  group_visibility: "ریسک یکی از حساب‌های دیگرتان دیده نمی‌شود",
  portfolio_alarm: "یک هشدار ایمنی باز داریم",
  unprotected_book: "یک معامله باز بدون حد ضرر ثبت‌شده داریم",
};
export { MANUAL_VETO_FA };
