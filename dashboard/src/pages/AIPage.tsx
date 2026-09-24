import React, { useCallback, useEffect, useState } from "react";
import type { Provider } from "../api";
import {
  Banner, Card, Chip, ConfirmWrite, Disclosure, Empty, Field, KV, Switch, ago, dt,
} from "../components/ui";
import type { AIInsights, AIOverview, AIProviderRow } from "../types";

/* AI assistants and news. The first thing on the page is what the AI is NOT
   allowed to do, because "the robot uses ChatGPT" is exactly the sentence that
   makes a newcomer believe a model is picking their trades. */

type Write = (path: string, body: unknown, totp: string) =>
  Promise<{ ok: boolean; detail: string }>;

const PURPOSE_FA: Record<string, { label: string; help: string }> = {
  news: { label: "خواندن اخبار رسمی",
          help: "خبرهای بانک‌های مرکزی را می‌خواند؛ فقط می‌تواند جلوی معامله را بگیرد یا حجم را کم کند." },
  coach: { label: "مربی معامله‌ها",
           help: "بعد از هر معامله بسته‌شده، به زبان ساده توضیح می‌دهد چه شد و چه چیزی را می‌شود آزمایش کرد." },
  brief: { label: "گزارش روزانه بازار",
           help: "خلاصه وضعیت بازار، خبرهای مهم پیش‌رو و وضعیت ربات را به فارسی می‌نویسد." },
};

const CATEGORY_FA: Record<string, string> = {
  working_as_designed: "طبق برنامه بود", bad_luck: "بدشانسی",
  late_entry: "ورود دیر", early_exit: "خروج زود", late_exit: "خروج دیر",
  stop_too_tight: "حد ضرر خیلی نزدیک", stop_too_wide: "حد ضرر خیلی دور",
  against_trend: "خلاف روند", news_shock: "شوک خبری", high_cost: "هزینه زیاد",
  regime_mismatch: "بازار مناسب استراتژی نبود", other: "سایر",
};

export default function AIPage({ provider, write, readOnly, canAdminister }: {
  provider: Provider; write: Write; readOnly: boolean; canAdminister: boolean;
}) {
  const [overview, setOverview] = useState<AIOverview | null>(null);
  const [insights, setInsights] = useState<AIInsights | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown;
    onDone?: (detail: string) => void;
  }>(null);
  const [testResult, setTestResult] = useState<Record<string, any>>({});

  const load = useCallback(async () => {
    try {
      const [o, i] = await Promise.all([
        provider.get<AIOverview>("/api/ai"),
        provider.get<AIInsights>("/api/ai/insights"),
      ]);
      setOverview(o); setInsights(i); setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [provider]);

  useEffect(() => { load(); }, [load]);

  const run: Write = async (path, body, totp) => {
    const res = await write(path, body, totp);
    if (res.ok) {
      confirm?.onDone?.(res.detail);
      await load();
    }
    return res;
  };

  if (error) return <Banner tone="neg" icon="✕">داده‌های این صفحه بارگذاری نشد: {error}</Banner>;
  if (!overview) return <Empty>در حال بارگذاری…</Empty>;
  if (!overview.enabled) {
    return <Banner tone="warn" icon="◈">دستیارهای هوش مصنوعی در این نسخه فعال نیستند.</Banner>;
  }

  return (
    <div className="stack gap16">
      <Banner tone="info" icon="🧠">
        <strong>هوش مصنوعی اینجا دستیار است، نه تصمیم‌گیرنده.</strong> هیچ مدل زبانی
        نمی‌تواند معامله باز کند، حجم را بزرگ کند، حد ضرر را جابه‌جا کند یا یک قاعده ایمنی
        را خاموش کند. کارهایش فقط این‌هاست: خواندن خبرهای رسمی (که فقط می‌تواند جلوی معامله
        را بگیرد یا حجم را کم کند)، توضیح معامله‌های بسته‌شده به زبان ساده، و نوشتن گزارش
        روزانه. اگر هیچ کلیدی وارد نکنید، ربات دقیقاً مثل قبل کار می‌کند.
      </Banner>

      <Card title="سرویس‌های هوش مصنوعی"
            hint={<>کلید API را از سایت هر سرویس بگیرید و اینجا وارد کنید. کلیدها رمزنگاری‌شده
              (AES-256) روی سرور نگه داشته می‌شوند و هیچ‌وقت دوباره نشان داده نمی‌شوند — فقط ۴
              رقم آخرشان.</>}
            sub="می‌توانید چند سرویس را فعال کنید؛ اگر اولی جواب نداد، سراغ بعدی می‌رود">
        <div className="stack gap12">
          {(overview.providers ?? []).map((p) => (
            <ProviderRowCard key={p.id} row={p} disabled={readOnly || !canAdminister}
                             isPrimary={overview.primary === p.id}
                             test={testResult[p.id]}
                             onSave={(body) => setConfirm({
                               action: `ذخیره تنظیمات ${p.label_fa}`,
                               description: <>تنظیمات این سرویس ذخیره می‌شود
                                 {body.api_key ? <> و کلید تازه رمزنگاری‌شده نگه داشته می‌شود</> : null}.
                                 هر استفاده از این سرویس در دفتر رویدادها ثبت می‌شود (بدون متن پیام‌ها).</>,
                               path: "/api/ai/provider", body,
                             })}
                             onTest={() => setConfirm({
                               action: `آزمایش ${p.label_fa}`,
                               description: <>یک پیام کوتاه آزمایشی به این سرویس فرستاده می‌شود و فهرست
                                 مدل‌های در دسترس خوانده می‌شود. این کار هزینه بسیار کمی دارد.</>,
                               path: "/api/ai/test", body: { provider: p.id },
                               onDone: (detail) => {
                                 try { setTestResult((t) => ({ ...t, [p.id]: JSON.parse(detail) })); }
                                 catch { /* ignore */ }
                               },
                             })} />
          ))}
          {overview.key_storage !== "ok" && (
            <Banner tone="warn" icon="🔑">
              ذخیره امن کلیدها در دسترس نیست: {overview.key_storage}
            </Banner>
          )}
        </div>
      </Card>

      <SettingsCard overview={overview} disabled={readOnly || !canAdminister}
                    onSave={(body) => setConfirm({
                      action: "ذخیره تنظیمات هوش مصنوعی",
                      description: "ترتیب سرویس‌ها، کاربردها و سقف تعداد درخواست‌ها ذخیره می‌شود.",
                      path: "/api/ai/settings", body,
                    })} />

      <div className="grid g-2-1">
        <Card title="گزارش روزانه بازار"
              hint="خلاصه‌ای به فارسی ساده از شرایط بازار، خبرهای مهم پیش‌رو و وضعیت ربات. پیش‌بینی قیمت نمی‌کند."
              actions={
                <button className="btn sm" disabled={readOnly}
                        onClick={() => setConfirm({
                          action: "ساخت گزارش تازه",
                          description: "یک گزارش تازه با اطلاعات همین لحظه ساخته می‌شود.",
                          path: "/api/ai/brief", body: {},
                        })}>ساخت گزارش تازه</button>
              }>
          {!insights?.brief ? <Empty>هنوز گزارشی ساخته نشده</Empty> : (
            <div className="stack gap8">
              <h3 style={{ fontSize: 15 }}>{insights.brief.payload.headline_fa}</h3>
              <p className="fs13" style={{ lineHeight: 1.9 }}>{insights.brief.payload.market_fa}</p>
              {insights.brief.payload.risks_fa.length > 0 && (
                <div className="stack gap4">
                  <strong className="fs12">ریسک‌های پیش‌رو</strong>
                  <ul className="fs12" style={{ lineHeight: 1.9 }}>
                    {insights.brief.payload.risks_fa.map((r, i) => <li key={i}>{r}</li>)}
                  </ul>
                </div>
              )}
              <p className="fs12 muted" style={{ lineHeight: 1.8 }}>{insights.brief.payload.agent_state_fa}</p>
              <span className="fs11 faint">
                {ago(insights.brief.ts_ns)} · {insights.brief.provider} / {insights.brief.model}
              </span>
            </div>
          )}
        </Card>

        <Card title="مربی: الگوهای تکراری"
              hint="شمارش ساده دسته‌بندی‌های مربی. تا وقتی حلقه آماری ربات تأیید نکند، مدرک به حساب نمی‌آید.">
          {!insights?.themes || insights.themes.reviewed === 0 ? (
            <Empty>هنوز معامله‌ای بررسی نشده</Empty>
          ) : (
            <div className="stack gap8">
              <KV k="معامله‌های بررسی‌شده" v={insights.themes.reviewed} />
              <KV k="قابل‌اجتناب از نظر مربی" v={insights.themes.avoidable} />
              <div className="row gap6 wrap">
                {insights.themes.categories.map((c) => (
                  <Chip key={c.category}>{CATEGORY_FA[c.category] ?? c.category} · {c.count}</Chip>
                ))}
              </div>
              <span className="fs11 muted">{insights.themes.note_fa}</span>
            </div>
          )}
        </Card>
      </div>

      <Card title="مربی: بررسی معامله‌های بسته‌شده"
            hint={<>بعد از هر معامله ضررده (و نمونه‌ای از سودده‌ها)، مربی به زبان ساده توضیح
              می‌دهد چه اتفاقی افتاد. پیشنهادهایش فقط «ایده برای آزمایش» است و هیچ‌وقت خودکار
              اعمال نمی‌شود.</>}>
        {!insights || insights.reviews.length === 0 ? (
          <Empty>هنوز بررسی‌ای وجود ندارد — یا هیچ معامله‌ای بسته نشده، یا مربی خاموش است</Empty>
        ) : (
          <div className="stack gap12 scroll-y" style={{ maxHeight: 520 }}>
            {insights.reviews.map((r) => (
              <div key={r.trade_id} className="stack gap6"
                   style={{ padding: 12, borderRadius: 14, background: "var(--surface-alt)" }}>
                <div className="row gap6 wrap">
                  <span className="mono fs12" style={{ fontWeight: 600 }}>{r.instrument}</span>
                  <Chip tone={r.r_multiple > 0 ? "pos" : "neg"}>
                    {r.r_multiple > 0 ? "+" : ""}{r.r_multiple.toFixed(2)}R
                  </Chip>
                  <Chip>{CATEGORY_FA[r.payload.category] ?? r.payload.category}</Chip>
                  {r.payload.avoidable && <Chip tone="warn">قابل اجتناب</Chip>}
                  <span className="fs11 muted">{r.strategy}</span>
                  <span className="fs11 faint" style={{ marginInlineStart: "auto" }}>{ago(r.ts_ns)}</span>
                </div>
                <p className="fs12" style={{ lineHeight: 1.85 }}>{r.payload.summary_fa}</p>
                {r.payload.what_went_wrong_fa && (
                  <p className="fs12 neg" style={{ lineHeight: 1.8 }}>✕ {r.payload.what_went_wrong_fa}</p>
                )}
                {r.payload.what_went_right_fa && (
                  <p className="fs12 pos" style={{ lineHeight: 1.8 }}>✓ {r.payload.what_went_right_fa}</p>
                )}
                {r.payload.suggestion_fa && (
                  <p className="fs12 muted" style={{ lineHeight: 1.8 }}>
                    ایده برای آزمایش: {r.payload.suggestion_fa}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="اخبار رسمی و تقویم اقتصادی"
            hint={<>فقط منابع رسمی: فدرال رزرو، بانک مرکزی اروپا، بانک انگلستان، بانک ژاپن،
              استرالیا، کانادا و اداره آمار کار آمریکا. زمان دقیق خبرهای مهم از تقویم اقتصادی
              گرفته می‌شود تا ربات حوالی انتشارشان معامله تازه باز نکند.</>}
            actions={
              <button className="btn ghost sm" disabled={readOnly}
                      onClick={() => setConfirm({
                        action: "به‌روزرسانی اخبار",
                        description: "تقویم اقتصادی و خبرهای رسمی همین حالا دوباره خوانده می‌شوند.",
                        path: "/api/news/refresh", body: {},
                      })}>به‌روزرسانی</button>
            }>
        {insights?.desk && (
          <div className="kv-grid c3" style={{ marginBottom: 12 }}>
            <KV k="آخرین به‌روزرسانی تقویم"
                v={insights.desk.calendar_last_ns ? ago(insights.desk.calendar_last_ns) : "هنوز نه"} />
            <KV k="خبرهای خوانده‌شده" v={insights.desk.articles_seen ?? 0} />
            <KV k="تحلیل‌شده با هوش مصنوعی" v={insights.desk.extracted ?? 0} />
          </div>
        )}
        {insights?.desk?.feed_errors && Object.keys(insights.desk.feed_errors).length > 0 && (
          <Disclosure summary="منابعی که در دسترس نبودند">
            <ul className="fs12 ltr">
              {Object.entries(insights.desk.feed_errors).map(([k, v]) => (
                <li key={k}>{k}: {String(v)}</li>
              ))}
            </ul>
          </Disclosure>
        )}
        {!insights || insights.headlines.length === 0 ? (
          <Empty>هنوز خبری خوانده نشده (سرور باید به اینترنت دسترسی داشته باشد)</Empty>
        ) : (
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr><th>منبع</th><th>عنوان</th><th>ارز</th><th>تحلیل</th><th>زمان</th></tr>
              </thead>
              <tbody>
                {insights.headlines.slice(0, 30).map((h) => (
                  <tr key={h.article_id}>
                    <td className="fs12">{h.source}</td>
                    <td className="fs12 ltr" style={{ textAlign: "start" }}>{h.title}</td>
                    <td className="mono fs12">{h.currencies.join(",")}</td>
                    <td>
                      {h.extraction ? (
                        <div className="row gap4 wrap">
                          {h.extraction.contradicts_prior && <Chip tone="neg">متناقض</Chip>}
                          {h.extraction.is_correction && <Chip tone="warn">اصلاحیه</Chip>}
                          <Chip>{h.extraction.direction_claim}</Chip>
                        </div>
                      ) : <span className="fs11 faint">—</span>}
                    </td>
                    <td className="fs11 faint nowrap">{h.published_ns ? dt(h.published_ns) : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {insights && insights.background_errors.length > 0 && (
          <Disclosure summary="خطاهای اخیر دستیارها">
            <ul className="fs12 ltr">{insights.background_errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
          </Disclosure>
        )}
      </Card>

      {overview.usage && (
        <Card title="مصرف ۲۴ ساعت گذشته"
              hint="تعداد درخواست‌ها به هر سرویس. سقف ساعتی و روزانه جلوی هزینه ناخواسته را می‌گیرد.">
          {overview.usage.last_24h.length === 0 ? <Empty>هیچ درخواستی ثبت نشده</Empty> : (
            <div className="table-wrap">
              <table className="t">
                <thead><tr><th>سرویس</th><th>کاربرد</th><th className="n">تعداد</th>
                  <th className="n">موفق</th><th className="n">توکن ورودی</th>
                  <th className="n">توکن خروجی</th><th className="n">زمان پاسخ (ms)</th></tr></thead>
                <tbody>
                  {overview.usage.last_24h.map((u: any, i: number) => (
                    <tr key={i}>
                      <td>{u.provider}</td><td>{PURPOSE_FA[u.purpose]?.label ?? u.purpose}</td>
                      <td className="n">{u.n}</td><td className="n">{u.ok}</td>
                      <td className="n">{u.tin ?? 0}</td><td className="n">{u.tout ?? 0}</td>
                      <td className="n">{u.lat ? Math.round(u.lat) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      <ConfirmWrite open={!!confirm} action={confirm?.action ?? ""}
                    description={confirm?.description}
                    onClose={() => setConfirm(null)}
                    onConfirm={async (totp) =>
                      confirm ? run(confirm.path, confirm.body, totp)
                              : { ok: false, detail: "" }} />
    </div>
  );
}

function ProviderRowCard({ row, disabled, isPrimary, test, onSave, onTest }: {
  row: AIProviderRow; disabled: boolean; isPrimary: boolean; test?: any;
  onSave: (body: Record<string, unknown>) => void; onTest: () => void;
}) {
  const [enabled, setEnabled] = useState(row.enabled);
  const [model, setModel] = useState(row.model);
  const [key, setKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(row.base_url ?? "");
  const custom = row.id === "custom";
  return (
    <div className="stack gap8" style={{ padding: 12, borderRadius: 14, background: "var(--surface-alt)" }}>
      <div className="row gap8 wrap">
        <strong>{row.label_fa}</strong>
        <span className="fs11 faint ltr">{row.label}</span>
        {isPrimary && <Chip tone="solid">سرویس اصلی</Chip>}
        <Chip tone={row.key_stored ? "pos" : undefined}>
          {row.key_stored ? `کلید ذخیره شده${row.key_last4 ? ` (…${row.key_last4})` : ""}` : "بدون کلید"}
        </Chip>
        <span className="row gap6" style={{ marginInlineStart: "auto" }}>
          <span className="fs12 muted">فعال</span>
          <Switch checked={enabled} onChange={setEnabled} disabled={disabled} label="فعال" />
        </span>
      </div>
      {row.notes_fa && <span className="fs12 muted">{row.notes_fa}</span>}
      <div className="grid g-2">
        <Field label="مدل" hint={`پیش‌فرض: ${row.default_model || "—"}. بعد از «آزمایش»، فهرست مدل‌های در دسترس نشان داده می‌شود.`}>
          <input className="input ltr" value={model} disabled={disabled}
                 list={`models-${row.id}`} onChange={(e) => setModel(e.target.value.trim())} />
          {test?.models && (
            <datalist id={`models-${row.id}`}>
              {test.models.map((m: string) => <option key={m} value={m} />)}
            </datalist>
          )}
        </Field>
        <Field label="کلید API" hint={<>کلید را از <span className="ltr">{row.console_url || "سایت سرویس"}</span> بگیرید.
          خالی بگذارید تا کلید قبلی بماند.</>}>
          <input className="input ltr" type="password" autoComplete="off" value={key}
                 disabled={disabled} placeholder={row.key_prefix_hint ? `${row.key_prefix_hint}…` : ""}
                 onChange={(e) => setKey(e.target.value.trim())} />
        </Field>
      </div>
      {(custom || row.id === "kimi") && (
        <Field label="نشانی سرویس (https)"
               hint="برای سرویس سفارشی الزامی است؛ فقط https (یا http روی همین سرور برای مدل محلی).">
          <input className="input ltr" value={baseUrl} disabled={disabled}
                 placeholder="https://api.example.com/v1"
                 onChange={(e) => setBaseUrl(e.target.value.trim())} />
        </Field>
      )}
      <div className="row gap8 wrap">
        <button className="btn sm" disabled={disabled} onClick={() => onSave({
          provider: row.id, enabled, model, base_url: baseUrl,
          ...(key ? { api_key: key } : {}),
        })}>ذخیره</button>
        <button className="btn ghost sm" disabled={disabled || !row.key_stored && !custom}
                onClick={onTest}>آزمایش اتصال</button>
        {row.key_stored && (
          <button className="btn ghost sm" disabled={disabled}
                  onClick={() => onSave({ provider: row.id, enabled: false, model,
                                          base_url: baseUrl, api_key: "" })}>حذف کلید</button>
        )}
        {test && (
          <Chip tone={test.ok ? "pos" : "neg"}>
            {test.ok ? `وصل شد · ${Math.round(test.latency_ms)}ms` : `خطا: ${String(test.error).slice(0, 80)}`}
          </Chip>
        )}
      </div>
    </div>
  );
}

function SettingsCard({ overview, disabled, onSave }: {
  overview: AIOverview; disabled: boolean; onSave: (body: Record<string, unknown>) => void;
}) {
  const providers = overview.providers ?? [];
  const [primary, setPrimary] = useState(overview.primary ?? "");
  const [fallbacks, setFallbacks] = useState<string[]>(overview.fallbacks ?? []);
  const [purposes, setPurposes] = useState<Record<string, boolean>>(overview.purposes ?? {});
  const [hour, setHour] = useState(overview.max_calls_per_hour ?? 60);
  const [day, setDay] = useState(overview.max_calls_per_day ?? 400);
  return (
    <Card title="ترتیب و کاربردها"
          hint="سرویس اصلی اول امتحان می‌شود؛ اگر جواب نداد، سرویس‌های پشتیبان به ترتیب.">
      <div className="stack gap12">
        <Field label="سرویس اصلی">
          <select className="input" value={primary} disabled={disabled}
                  onChange={(e) => setPrimary(e.target.value)}>
            <option value="">— انتخاب کنید —</option>
            {providers.map((p) => <option key={p.id} value={p.id}>{p.label_fa}</option>)}
          </select>
        </Field>
        <Field label="سرویس‌های پشتیبان">
          <div className="row gap12 wrap">
            {providers.filter((p) => p.id !== primary).map((p) => (
              <label key={p.id} className="row gap6 fs12">
                <input type="checkbox" disabled={disabled} checked={fallbacks.includes(p.id)}
                       onChange={(e) => setFallbacks(e.target.checked
                         ? [...fallbacks, p.id] : fallbacks.filter((f) => f !== p.id))} />
                {p.label_fa}
              </label>
            ))}
          </div>
        </Field>
        {Object.entries(PURPOSE_FA).map(([id, meta]) => (
          <Field key={id} label={meta.label} help={meta.help}>
            <div className="row gap8">
              <Switch checked={!!purposes[id]} disabled={disabled} label={meta.label}
                      onChange={(v) => setPurposes({ ...purposes, [id]: v })} />
              {overview.available?.[id] && !overview.available[id].ok && (
                <span className="fs11 muted">{overview.available[id].reason}</span>
              )}
            </div>
          </Field>
        ))}
        <div className="grid g-2">
          <Field label="سقف درخواست در ساعت">
            <input className="input num" type="number" min={0} value={hour} disabled={disabled}
                   onChange={(e) => setHour(Number(e.target.value))} />
          </Field>
          <Field label="سقف درخواست در روز">
            <input className="input num" type="number" min={0} value={day} disabled={disabled}
                   onChange={(e) => setDay(Number(e.target.value))} />
          </Field>
        </div>
        <div>
          <button className="btn sm" disabled={disabled} onClick={() => onSave({
            primary, fallbacks: fallbacks.filter((f) => f !== primary), purposes,
            max_calls_per_hour: hour, max_calls_per_day: day,
          })}>ذخیره تنظیمات</button>
        </div>
      </div>
    </Card>
  );
}
