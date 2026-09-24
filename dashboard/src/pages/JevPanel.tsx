import React, { useCallback, useEffect, useState } from "react";
import type { Provider } from "../api";
import { Banner, Card, Chip, ConfirmWrite, Disclosure, Empty, KV, Seg, ago, fa } from "../components/ui";
import type { CalibrationSummary, JevAnswer, JevReport } from "../types";

/* Jev earns authority; it is not granted it. The panel shows, in this order:
   what Jev is currently ALLOWED to do, which version is answering, whether its
   numbers have been shown to mean anything, and the one thing only the owner
   can supply -- the truth about a few headlines. */

type Write = (path: string, body: unknown, totp: string) =>
  Promise<{ ok: boolean; detail: string }>;

const MODE_FA: Record<string, { label: string; help: string; tone: any }> = {
  shadow: { label: "سایه (فقط ثبت)", tone: "flat",
            help: "Jev جواب می‌دهد و جوابش ثبت و نمایش داده می‌شود، ولی هیچ اثری روی معامله ندارد." },
  shrink_only: { label: "فقط کاهش حجم", tone: "warn",
                 help: "اگر Jev بگوید خبر «اصلاحیه» است، حجم معامله نصف می‌شود. نمی‌تواند معامله را متوقف کند." },
  active: { label: "فعال", tone: "info",
            help: "کاهش حجم به‌علاوهٔ توقف تا ۴ ساعت روی ارزی که خبرش متناقض است. فقط با مدرک کافی روی همین نسخه." },
};

const DIR_FA: Record<string, string> = {
  hawkish: "انقباضی", dovish: "انبساطی", neutral: "خنثی", unclear: "نامشخص",
};

type Label = { is_correction: boolean | null; contradicts_prior: boolean | null;
               direction: string | null };

function pct(v: number | null | undefined, digits = 0) {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}٪`;
}

function Tri({ value, onChange, disabled }: {
  value: boolean | null; onChange: (v: boolean | null) => void; disabled: boolean;
}) {
  return (
    <select className="input" style={{ minWidth: 72 }} disabled={disabled}
            value={value === null ? "" : value ? "yes" : "no"}
            onChange={(e) => onChange(e.target.value === "" ? null : e.target.value === "yes")}>
      <option value="">؟</option><option value="yes">بله</option><option value="no">خیر</option>
    </select>
  );
}

function Reliability({ title, s }: { title: string; s: CalibrationSummary }) {
  return (
    <div className="stack gap6">
      <strong className="fs12">{title}</strong>
      <div className="kv-grid c3">
        <KV k="نمونهٔ برچسب‌خورده" v={s.n} />
        <KV k="درستی تصمیم" v={pct(s.decision.accuracy)}
            hint={`با آستانهٔ ${s.decision.threshold}: ${s.decision.false_positive} هشدار اشتباه، ${s.decision.false_negative} مورد از قلم افتاده`} />
        <KV k="مهارت نسبت به حدس ساده" v={s.skill === null ? "—" : s.skill.toFixed(2)}
            hint="بالای صفر یعنی Jev از «همیشه نرخ پایه را بگو» بهتر است. صفر یعنی چیزی نمی‌داند، حتی اگر اعدادش مرتب باشند." />
      </div>
      <div className="table-wrap">
        <table className="t">
          <thead><tr><th>بازهٔ احتمال Jev</th><th className="n">تعداد</th>
            <th className="n">میانگین احتمال</th><th className="n">واقعاً درست بود</th></tr></thead>
          <tbody>
            {s.reliability.map((b) => (
              <tr key={b.lo}>
                <td className="mono fs12 ltr">{b.lo.toFixed(1)}–{b.hi.toFixed(1)}</td>
                <td className="n">{b.n}</td>
                <td className="n mono">{b.mean_p === null ? "—" : b.mean_p.toFixed(2)}</td>
                <td className="n mono">{b.observed === null ? "—" : b.observed.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function JevPanel({ provider, write, readOnly, canAdminister }: {
  provider: Provider; write: Write; readOnly: boolean; canAdminister: boolean;
}) {
  const [report, setReport] = useState<JevReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<string>("shadow");
  const [labels, setLabels] = useState<Record<string, Label>>({});
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown;
  }>(null);
  const disabled = readOnly || !canAdminister;

  const load = useCallback(async () => {
    try {
      const r = await provider.get<JevReport>("/api/ai/jev");
      setReport(r); setMode(r.mode); setError(null); setLabels({});
    } catch (e) {
      setError(String(e));
    }
  }, [provider]);

  useEffect(() => { load(); }, [load]);

  if (error) return null;           // the AI page already reports an unreachable API
  if (!report) return <Empty>در حال بارگذاری وضعیت Jev…</Empty>;

  const current = MODE_FA[report.mode] ?? MODE_FA.shadow;
  const labelOf = (a: JevAnswer): Label => labels[a.article_id] ?? {
    is_correction: a.label_correction, contradicts_prior: a.label_contradiction,
    direction: a.label_direction,
  };
  const setLabel = (id: string, patch: Partial<Label>, a: JevAnswer) =>
    setLabels((l) => ({ ...l, [id]: { ...labelOf(a), ...patch } }));
  const changed = Object.entries(labels).map(([article_id, l]) => ({ article_id, ...l }));
  const agreement = report.agreement_with_text_model;

  return (
    <Card title="جِو (Jev): اختیار، نسخه و میزان اعتماد"
          hint={<>Jev تا وقتی ثابت نکرده که احتمال‌هایش روی خبرهای واقعی درست است، فقط
            «سایه» است: جواب می‌دهد، ثبت می‌شود و اثری ندارد. با برچسب زدن به چند خبر
            (درست بود یا نه)، معلوم می‌شود می‌شود به آن اعتماد کرد یا نه.</>}>
      <div className="stack gap16">
        <div className="row gap8 wrap">
          <span className="fs13">حالت فعلی:</span>
          <Chip tone={current.tone}>{current.label}</Chip>
          {report.breaker?.open && (
            <Chip tone="warn">مکث به‌خاطر محدودیت درخواست: {report.breaker.seconds_left} ثانیه</Chip>
          )}
        </div>
        <p className="fs12 muted" style={{ lineHeight: 1.8 }}>{current.help}</p>

        {report.pending_version && (
          <Banner tone="neg" icon="⚠">
            <strong>نسخهٔ تازه‌ای از Jev جواب می‌دهد</strong> (<span className="ltr mono">
            {report.pending_version}</span> به‌جای <span className="ltr mono">{report.known_version}</span>،
            {" "}{ago(report.pending_since_ns)}). آستانه‌ها و برچسب‌ها برای نسخهٔ قبلی بودند،
            پس Jev خودکار به حالت «سایه» برگشت. بعد از بررسی، نسخهٔ تازه را بپذیرید تا
            شمارش مدرک برایش از صفر شروع شود.
            <div style={{ marginTop: 8 }}>
              <button className="btn sm" disabled={disabled}
                      onClick={() => setConfirm({
                        action: "پذیرفتن نسخهٔ تازهٔ Jev",
                        description: "نسخهٔ تازه به‌عنوان نسخهٔ شناخته‌شده ثبت می‌شود. Jev در حالت سایه می‌ماند تا روی همین نسخه مدرک جمع شود.",
                        path: "/api/ai/jev/accept-version", body: {},
                      })}>پذیرفتن نسخهٔ تازه</button>
            </div>
          </Banner>
        )}

        <div className="kv-grid c3">
          <KV k="نسخهٔ در حال پاسخ" v={<span className="ltr mono fs12">{report.known_version || "هنوز جوابی نیامده"}</span>} />
          <KV k="جواب‌های این نسخه" v={report.answers} />
          <KV k="برچسب‌خورده" v={<span dir="rtl">{fa(report.labelled)} از {fa(report.gate.min_labels)}</span>} />
        </div>
        {report.floating_alias && (
          <p className="fs12 muted">نام مدل «<span className="ltr mono">jev-latest</span>» یعنی
            «آخرین نسخه، هر چه باشد». اگر TypeSafe نسخه‌ها را با نام مشخص منتشر می‌کند، در
            بخش سرویس‌ها نام دقیق را بنویسید. در هر حال، تغییر نسخه خودکار شناسایی می‌شود.</p>
        )}

        <div className="stack gap8">
          <strong className="fs13">تغییر حالت</strong>
          <Seg value={mode as any} onChange={(v) => setMode(v)}
               options={report.modes.map((m) => ({ value: m, label: MODE_FA[m]?.label ?? m }))} />
          {mode === "active" && !report.gate.passed && (
            <Banner tone="warn" icon="!">
              برای حالت «فعال» هنوز مدرک کافی نیست: {report.gate.missing.join("؛ ")}
            </Banner>
          )}
          <div>
            <button className="btn sm"
                    disabled={disabled || mode === report.mode || (mode === "active" && !report.gate.passed)
                              || (mode !== "shadow" && !!report.pending_version)}
                    onClick={() => setConfirm({
                      action: `تغییر حالت Jev به «${MODE_FA[mode]?.label ?? mode}»`,
                      description: MODE_FA[mode]?.help,
                      path: "/api/ai/jev/mode", body: { mode },
                    })}>ذخیرهٔ حالت</button>
          </div>
        </div>

        <Disclosure summary="میزان اعتماد (کالیبراسیون) روی همین نسخه">
          <div className="stack gap16" style={{ marginTop: 8 }}>
            <Reliability title="«این خبر با حرف قبلی بانک تناقض دارد» (می‌تواند معامله را متوقف کند)"
                         s={report.contradiction} />
            <Reliability title="«این خبر اصلاحیه است» (حجم را نصف می‌کند)" s={report.correction} />
            <div className="kv-grid c3">
              <KV k="درستی جهت سیاست پولی" v={pct(report.direction.accuracy)}
                  hint={`${report.direction.n} نمونه؛ میانگین اطمینان ادعاشده ${pct(report.direction.mean_confidence)}`} />
              <KV k="هم‌نظری با مدل متنی" v={agreement.n ? `${agreement.n} خبر` : "—"}
                  hint="فقط در حالت سایه، وقتی یک مدل متنی هم پیکربندی شده، هر دو نظر می‌دهند و مقایسه می‌شوند." />
              <KV k="هم‌نظری: تناقض / اصلاحیه / جهت"
                  v={`${pct(agreement.contradiction)} / ${pct(agreement.correction)} / ${pct(agreement.direction)}`} />
            </div>
          </div>
        </Disclosure>

        <div className="stack gap8">
          <strong className="fs13">برچسب‌گذاری: Jev درست گفت؟</strong>
          <span className="fs12 muted">برای چند خبر اخیر بگویید واقعاً «اصلاحیه» یا «متناقض»
            بود یا نه. لازم نیست همه را پر کنید. همه با یک کد تأیید ذخیره می‌شوند.</span>
          {report.recent.length === 0 ? <Empty>هنوز Jev به خبری جواب نداده است</Empty> : (
            <div className="table-wrap">
              <table className="t">
                <thead><tr><th>خبر</th><th className="n">Jev: اصلاحیه</th>
                  <th className="n">Jev: تناقض</th><th>Jev: جهت</th>
                  <th>واقعاً اصلاحیه؟</th><th>واقعاً متناقض؟</th><th>جهت واقعی</th></tr></thead>
                <tbody>
                  {report.recent.slice(0, 25).map((a) => {
                    const l = labelOf(a);
                    return (
                      <tr key={a.article_id}>
                        <td className="fs12 ltr" style={{ textAlign: "start", maxWidth: 360 }}>
                          {a.headline}
                          {a.version !== report.known_version &&
                            <span className="fs11 faint"> ({a.version})</span>}
                        </td>
                        <td className="n mono">{pct(a.p_correction)}</td>
                        <td className="n mono">{pct(a.p_contradiction)}</td>
                        <td className="fs12">{DIR_FA[a.direction] ?? a.direction}
                          {!a.confidence_known && <span className="fs11 faint"> (بی‌عدد)</span>}</td>
                        <td><Tri value={l.is_correction} disabled={disabled}
                                 onChange={(v) => setLabel(a.article_id, { is_correction: v }, a)} /></td>
                        <td><Tri value={l.contradicts_prior} disabled={disabled}
                                 onChange={(v) => setLabel(a.article_id, { contradicts_prior: v }, a)} /></td>
                        <td>
                          <select className="input" disabled={disabled} value={l.direction ?? ""}
                                  onChange={(e) => setLabel(a.article_id,
                                    { direction: e.target.value || null }, a)}>
                            <option value="">؟</option>
                            {Object.entries(DIR_FA).map(([k, v]) =>
                              <option key={k} value={k}>{v}</option>)}
                          </select>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <div>
            <button className="btn sm" disabled={disabled || changed.length === 0}
                    onClick={() => setConfirm({
                      action: `ذخیرهٔ ${changed.length} برچسب`,
                      description: "برچسب‌ها فقط برای سنجش Jev استفاده می‌شوند و روی معامله اثر مستقیم ندارند.",
                      path: "/api/ai/jev/labels", body: { labels: changed.slice(0, 100) },
                    })}>ذخیرهٔ برچسب‌ها ({changed.length})</button>
          </div>
        </div>
      </div>

      <ConfirmWrite open={!!confirm} action={confirm?.action ?? ""}
                    description={confirm?.description}
                    onClose={() => setConfirm(null)}
                    onConfirm={async (totp) => {
                      if (!confirm) return { ok: false, detail: "" };
                      const res = await write(confirm.path, confirm.body, totp);
                      if (res.ok) await load();
                      return res;
                    }} />
    </Card>
  );
}
