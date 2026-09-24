/**
 * Venue configuration.
 *
 * The page is arranged in the order the work actually happens: see what is
 * connected now, let the machine find what it can, fill in what it cannot,
 * TEST before trusting, and only then switch. Every destructive step sits
 * behind the same second-factor dialog as the rest of the console.
 *
 * Wording rule for this whole file: no term appears that a person who has
 * never traded would have to look up. "Filling mode" becomes "how the order is
 * filled"; "stop level" becomes "کمترین فاصلهٔ مجاز حد ضرر"; anything that
 * genuinely has no plain equivalent gets a Hint beside it rather than a
 * glossary lookup the reader has to go and find.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Banner, Card, Chip, ConfirmWrite, Disclosure, Empty, Field, Hint, KV, Modal,
  Seg, Switch, ago,
} from "../components/ui";
import type { Provider } from "../api";
import type { BrokerOverview, Connection, Discovered, ProbeReport } from "../types";

type Props = {
  provider: Provider;
  write: (path: string, body: unknown, totp: string) => Promise<{ ok: boolean; detail: string }>;
  readOnly: boolean;
  canAdminister: boolean;
};

const BLANK = {
  id: "", display_name: "", profile: "generic_mt5",
  declared_account_type: "demo" as "demo" | "live",
  server: "", login: "", terminal_path: "", account_currency: "USD",
  exchange_id: "", notes: "", secret: "", clearSecret: false,
};

/** Server-side caps, mirrored so the person is stopped here rather than by a
 *  raw validation error from the API. */
const LIMITS = { display_name: 80, server: 120, login: 64, account_currency: 8,
                 terminal_path: 512, notes: 500 };

export default function Brokers({ provider, write, readOnly, canAdminister }: Props) {
  const [data, setData] = useState<BrokerOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [found, setFound] = useState<Discovered[] | null>(null);
  // `editing` is captured when the form opens. Deriving it from whether the
  // typed id matches an existing connection meant that typing "s","i","m" into
  // a NEW connection disabled the id field mid-keystroke on the third
  // character, retitled the dialog «ویرایش», and silently turned the save into
  // an overwrite of the existing record.
  const [form, setForm] = useState<FormState | null>(null);
  const [probe, setProbe] = useState<ProbeReport | null>(null);
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown;
    danger?: boolean; after?: (detail: string) => void;
  }>(null);

  const load = useCallback(async () => {
    try {
      setData(await provider.get<BrokerOverview>("/api/brokers"));
      setError(null);
    } catch (e) { setError(String(e)); }
  }, [provider]);

  useEffect(() => { void load(); }, [load]);

  if (error && !data) {
    return (
      <div className="stack gap12">
        <Banner tone="neg" icon="✕">{error}</Banner>
        <button className="btn outline sm" style={{ alignSelf: "start" }}
                onClick={() => void load()}>تلاش دوباره</button>
      </div>
    );
  }
  if (!data) return <div className="muted fs12">در حال بارگذاری…</div>;

  const profileNames = data.profiles.map((p) => ({ value: p.name, label: p.display_name }));

  return (
    <div className="stack gap16">
      {/* An error AFTER the first successful load used to be stored and never
          rendered: the page kept showing stale rows and the operator had no
          way to tell that the last refresh had failed. */}
      {error && (
        <Banner tone="neg" icon="✕">
          به‌روزرسانی انجام نشد (چیزی که می‌بینید ممکن است قدیمی باشد):{" "}
          <span dir="ltr">{error}</span>
        </Banner>
      )}

      {/* ---------- what is connected right now ---------- */}
      <Card title="بروکر فعلی"
            sub="جایی که سفارش‌های ربات همین حالا به آن فرستاده می‌شود">
        <div className="kv-grid">
          <KV k="پروفایل فعال" v={<span className="mono">{data.active_profile}</span>} />
          <KV k="نوع حساب"
              v={<Chip tone={data.venue_mode === "live" ? "neg" : "info"}>
                {data.venue_mode === "live" ? "پول واقعی"
                  : data.venue_mode === "demo" ? "تمرینی — حساب دمو" : "تمرینی — شبیه‌ساز"}
              </Chip>} />
          <KV k="معاملهٔ باز" v={<span className="num">{data.open_positions}</span>}
              hint={<>تا وقتی معاملهٔ باز دارید، عوض کردن بروکر ممکن نیست. آن
                معامله‌ها را همین بروکر نگه داشته؛ بروکر بعدی آن‌ها را نمی‌شناسد.</>} />
        </div>

        {data.degradations.length > 0 && (
          <div className="mt8">
            <Banner tone="warn" icon="◈">
              <strong>چیزهایی که این بروکر نمی‌تواند تضمین کند:</strong>
              <ul style={{ margin: "6px 0 0", paddingInlineStart: 18 }}>
                {data.degradations.map((d, i) => <li key={i} className="fs12">{d}</li>)}
              </ul>
            </Banner>
          </div>
        )}

        {data.restart_required && (
          <div className="mt8">
            <Banner tone="info" icon="↻">
              یک بروکر تازه انتخاب شده ولی هنوز به کار نیفتاده است. برای اعمال شدن،
              سرویس باید یک بار راه‌اندازی دوباره شود. این عمدی است: عوض کردن بروکر
              وسط کار، معامله‌های باز را از دست سامانه خارج می‌کند.
            </Banner>
          </div>
        )}
      </Card>

      {/* ---------- credential storage, honestly ---------- */}
      <Card title="رمزهای بروکر کجا نگه داشته می‌شوند"
            sub="این را یک بار بخوانید؛ روی جایی که سرور را می‌گذارید اثر دارد">
        {/* "fair" (key kept apart from the data) is BETTER than "weak" (key
            beside it). Colouring anything that is not good-or-weak as an error
            told a customer who had just moved their key off the data
            directory -- the recommended step -- that they had made it worse. */}
        <Banner tone={data.credential_storage.level === "good" ? "info"
                      : data.credential_storage.level === "unavailable" ? "neg" : "warn"}
                icon={data.credential_storage.level === "good" ? "🔒" : "⚠"}>
          {data.credential_storage.note}
        </Banner>
        <div className="mt8 fs12 muted">
          رمز بروکر با روش AES-256-GCM قفل می‌شود و در فایلی جدا از تنظیمات
          می‌ماند. هیچ‌جای این صفحه — و هیچ پاسخی از سرور — رمز را برنمی‌گرداند؛
          فقط می‌توانید آن را عوض کنید یا پاک کنید.
        </div>
      </Card>

      {/* ---------- automatic discovery ---------- */}
      <Card title="پیدا کردن خودکار"
            sub="اگر ترمینال متاتریدر روی همین سرور باز و وارد شده باشد، خودش پیدا می‌شود"
            actions={
              <button className="btn outline sm"
                      disabled={readOnly || !canAdminister || busy !== null}
                      title={!canAdminister ? "فقط مدیر می‌تواند این کار را بکند" : ""}
                      onClick={() => setConfirm({
                        action: "جست‌وجوی خودکار بروکر",
                        description: <>سامانه فقط می‌خواند: به ترمینالی که روی این
                          سرور باز است نگاه می‌کند و مشخصات حساب را برمی‌دارد.
                          هیچ سفارشی فرستاده نمی‌شود و هیچ تنظیمی عوض نمی‌شود.</>,
                        path: "/api/brokers/discover", body: {},
                        after: (detail) => {
                          try { setFound(JSON.parse(detail).found ?? []); }
                          catch { setFound([]); }
                        },
                      })}>
                {busy === "/api/brokers/discover" ? "در حال جست‌وجو…" : "جست‌وجو کن"}
              </button>
            }>
        {found === null ? (
          <div className="fs12 muted">
            دکمهٔ «جست‌وجو کن» را بزنید. اگر ترمینال باز باشد، نام بروکر، شمارهٔ
            حساب، ارز حساب و واقعی یا تمرینی بودنش را خودش می‌خواند — و شما فقط
            تأیید می‌کنید.
          </div>
        ) : found.length === 0 ? (
          <Empty>چیزی پیدا نشد. اگر ترمینال متاتریدر روی همین سرور باز است،
            مطمئن شوید وارد حساب شده باشد. در غیر این صورت، پایین‌تر دستی وارد کنید.</Empty>
        ) : (
          <div className="stack gap8">
            {found.map((f, i) => (
              <div key={i} className="kv" style={{ alignItems: "flex-start" }}>
                <span className="k">
                  {f.display_name}
                  <Chip tone={f.declared_account_type === "live" ? "neg" : "info"}>
                    {f.declared_account_type === "live" ? "واقعی" : "تمرینی"}
                  </Chip>
                </span>
                <span className="v prose">
                  <div className="fs12 muted">{f.note}</div>
                  <button className="btn ghost sm mt8" disabled={readOnly || !canAdminister}
                          onClick={() => setForm({
                            ...BLANK, editing: false,
                            id: suggestId(f, data.connections.map((c) => c.id)),
                            display_name: f.display_name,
                            profile: f.profile, server: f.server, login: f.login,
                            terminal_path: f.terminal_path ?? "",
                            account_currency: f.account_currency || "USD",
                            declared_account_type: f.declared_account_type,
                          })}>
                    از این بساز
                  </button>
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* ---------- the saved connections ---------- */}
      <Card title="بروکرهای ذخیره‌شده"
            sub="هر کدام را می‌توانید بدون خطر آزمایش کنید — آزمایش هیچ سفارشی نمی‌فرستد"
            actions={
              <button className="btn sm" disabled={readOnly || !canAdminister}
                      onClick={() => setForm({ ...BLANK, editing: false })}>
                افزودن دستی
              </button>
            }>
        {data.damaged.length > 0 && (
          <div style={{ marginBottom: 12 }}>
            <Banner tone="neg" icon="✕">
              این رکوردها خوانده نشدند و نادیده گرفته شده‌اند:{" "}
              <span className="mono">{data.damaged.join("، ")}</span>. دوباره واردشان کنید.
            </Banner>
          </div>
        )}
        {data.connections.length === 0 ? (
          <Empty>هنوز هیچ بروکری ذخیره نشده است.</Empty>
        ) : (
          <div className="stack gap12">
            {data.connections.map((c) => (
              <ConnectionRow
                key={c.id} conn={c} readOnly={readOnly} canAdminister={canAdminister}
                openPositions={data.open_positions}
                activeConnectionId={data.connections.find((x) => x.enabled)?.id ?? ""}
                onProbe={() => setProbe(c.last_probe)}
                onEdit={() => setForm({
                  ...BLANK, editing: true, id: c.id, display_name: c.display_name,
                  profile: c.profile, server: c.server, login: "",  // masked; blank means "keep"
                  terminal_path: c.terminal_path,
                  account_currency: c.account_currency,
                  exchange_id: c.exchange_id, notes: c.notes,
                  declared_account_type: c.declared_account_type, secret: "",
                })}
                onAsk={setConfirm}
              />
            ))}
          </div>
        )}
      </Card>

      {/* ---------- profile reference ---------- */}
      <Card title="بروکرهایی که سامانه از قبل می‌شناسد"
            sub="«پروفایل» یعنی مجموعه‌ای از عادت‌های یک بروکر که از قبل نوشته شده است">
        <Banner tone="flat" icon="◎">
          پروفایل فقط یک <strong>حدس اولیه</strong> است. هر چیزی که خودِ بروکر
          گزارش کند بر پروفایل مقدم است و اختلاف‌ها ثبت می‌شوند. اگر بروکر شما
          اینجا نیست، «هر بروکر متاتریدر ۵» را انتخاب کنید — آن پروفایل هیچ چیزی
          را فرض نمی‌کند و همه‌چیز را از خود ترمینال می‌خواند.
        </Banner>
        <div className="table-wrap mt8">
          <table className="t">
            <thead>
              <tr>
                <th>بروکر</th>
                <th>ناظر قانونی</th>
                <th className="n">
                  بیشترین اهرم
                  <Hint text={<>اهرم یعنی بروکر اجازه می‌دهد با پول کم، معاملهٔ
                    بزرگ‌تر باز کنید. ۱:۳۰ یعنی با ۱۰۰ دلار می‌توانید تا ۳٬۰۰۰
                    دلار معامله کنید. هرچه این عدد بزرگ‌تر باشد، یک حرکت کوچک
                    قیمت اثر بزرگ‌تری روی حساب شما دارد — در هر دو جهت.</>} />
                </th>
                <th className="n">
                  فاصلهٔ لازم حد ضرر
                  <Hint text={<>کمترین فاصله‌ای که بروکر اجازه می‌دهد بین قیمت
                    فعلی و حد ضرر بگذارید. اگر نزدیک‌تر بگذارید، سفارش رد می‌شود.
                    صفر یعنی محدودیتی ندارد.</>} />
                </th>
                <th>پول مشتری جدا نگه‌داری می‌شود؟</th>
              </tr>
            </thead>
            <tbody>
              {data.profiles.map((p) => (
                <tr key={p.name}>
                  <td>
                    <div>{p.display_name}</div>
                    <div className="fs11 faint mono ltr">{p.name}</div>
                  </td>
                  <td className="fs12">{p.regulator === "unknown" ? "نامشخص" : p.regulator}</td>
                  {/* One digit system per table. The rest of the console uses
                      Latin digits, so "۱:1000" was two alphabets in one cell. */}
                  <td className="n">1:{p.max_leverage}</td>
                  <td className="n">{p.min_stop_level_points || 0}</td>
                  <td className="fs12">
                    {p.segregated_client_funds === null
                      ? <span className="muted">تأیید نشده</span>
                      : p.segregated_client_funds ? "بله" : "خیر"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <Disclosure summary="پیش از اینکه با پول واقعی کار کنید، این کارها را بکنید">
          <div className="stack gap8 fs12">
            {data.profiles.filter((p) => p.verify_before_live.length > 0).map((p) => (
              <div key={p.name}>
                <strong>{p.display_name}</strong>
                <ul style={{ margin: "4px 0 0", paddingInlineStart: 18 }}>
                  {p.verify_before_live.map((v, i) => <li key={i}>{v}</li>)}
                </ul>
              </div>
            ))}
            <Banner tone="warn" icon="⚠">
              هیچ آزمایشی در این صفحه به شما نمی‌گوید که بروکر <em>درست‌کار</em> است.
              آزمایش فقط می‌گوید این نرم‌افزار می‌تواند درست با آن حساب حرف بزند.
              اینکه پولتان را پس می‌دهند یا نه، سؤال دیگری است — و تنها راه
              جوابش این است که یک برداشت کامل انجام دهید و ببینید پول می‌رسد.
            </Banner>
          </div>
        </Disclosure>
      </Card>

      {form && (
        <ConnectionForm
          form={form} setForm={setForm} profiles={profileNames}
          credentialAvailable={data.credential_storage.level !== "unavailable"}
          existing={form.editing}
          onSubmit={(payload) => setConfirm({
            action: "ذخیرهٔ تنظیمات بروکر",
            description: (
              <div className="stack gap8">
                <span>این اتصال ذخیره می‌شود:</span>
                <KV k="نام" v={String(payload.display_name)} />
                <KV k="بروکر" v={<span className="mono">{String(payload.profile)}</span>} />
                <KV k="نوع حساب"
                    v={payload.declared_account_type === "live"
                      ? <strong className="neg">واقعی — پول واقعی</strong> : "تمرینی"} />
                <span className="muted fs12">
                  ذخیره کردن هیچ بروکری را فعال نمی‌کند. بعد از ذخیره باید آزمایش
                  کنید و بعد فعال.
                </span>
              </div>),
            path: "/api/brokers/save", body: payload,
            after: () => setForm(null),
          })}
        />
      )}

      <Modal open={!!probe} title="نتیجهٔ آزمایش اتصال" onClose={() => setProbe(null)}>
        {probe && <ProbeDetail report={probe} />}
      </Modal>

      <ConfirmWrite
        open={!!confirm} action={confirm?.action ?? ""}
        description={confirm?.description} danger={confirm?.danger}
        onClose={() => setConfirm(null)}
        onConfirm={async (totp) => {
          if (!confirm) return { ok: false, detail: "" };
          setBusy(confirm.path);
          const res = await write(confirm.path, confirm.body, totp);
          setBusy(null);
          if (res.ok) {
            confirm.after?.(res.detail);
            await load();
            setTimeout(() => setConfirm(null), 700);
          }
          return res;
        }} />
    </div>
  );
}

/* --------------------------------------------------------------------- */

function suggestId(f: Discovered, taken: string[]): string {
  const base = `${f.profile}-${f.declared_account_type}`
    .toLowerCase().replace(/[^a-z0-9_-]+/g, "-").slice(0, 36);
  // A suggestion that collides with an existing connection would turn an
  // "add" into an overwrite the moment it was saved.
  if (!taken.includes(base)) return base;
  for (let n = 2; n < 50; n++) {
    const candidate = `${base}-${n}`;
    if (!taken.includes(candidate)) return candidate;
  }
  return `${base}-${Date.now() % 1000}`;
}

const PROBE_MAX_AGE_MS = 24 * 3600 * 1000;

function ConnectionRow({ conn, readOnly, canAdminister, openPositions,
                         activeConnectionId, onProbe, onEdit, onAsk }: {
  conn: Connection; readOnly: boolean; canAdminister: boolean;
  openPositions: number; activeConnectionId: string;
  onProbe: () => void; onEdit: () => void; onAsk: (c: any) => void;
}) {
  const probe = conn.last_probe;
  const tested = !!probe;
  const ageMs = probe ? Date.now() - probe.finished_ns / 1e6 : Infinity;
  // The server ages a probe out at 24 hours (activation_blockers,
  // max_probe_age_sec). The row used to show a green "tested" chip for ever
  // and enable the button the server would then refuse.
  const stale = tested && ageMs > PROBE_MAX_AGE_MS;
  const green = tested && probe!.ok && !stale;
  // Compare CONNECTION IDS, exactly as the server does. Comparing profile
  // names meant two connections sharing one profile enabled a button that
  // came back 409, and a genuine re-activation of the live venue was blocked
  // for no reason.
  const blocked = openPositions > 0 && conn.id !== activeConnectionId;

  return (
    <div className="card" style={{ padding: 14 }}>
      <div className="row gap8 wrap" style={{ alignItems: "flex-start" }}>
        <div className="stack gap4 grow">
          <div className="row gap6 wrap">
            <strong>{conn.display_name}</strong>
            {conn.enabled && <Chip tone="solid">فعال</Chip>}
            <Chip tone={conn.declared_account_type === "live" ? "neg" : "info"}>
              {conn.declared_account_type === "live" ? "پول واقعی" : "تمرینی"}
            </Chip>
            <Chip tone={!tested ? "warn" : green ? "pos" : stale ? "warn" : "neg"}>
              {!tested ? "آزمایش نشده"
                : stale ? "آزمایش قدیمی شده"
                  : green ? "آزمایش موفق" : "آزمایش ناموفق"}
            </Chip>
          </div>
          <div className="fs12 muted">
            <span className="mono ltr">{conn.profile}</span>
            {conn.server && <> · سرور <span className="mono ltr">{conn.server}</span></>}
            {conn.login && <> · حساب <span className="mono ltr">{conn.login}</span></>}
            {conn.has_credential ? <> · رمز ذخیره شده</> : <> · بدون رمز ذخیره‌شده</>}
            {tested && (
              <> · آزمایش {ago(conn.last_probe!.finished_ns)}</>
            )}
          </div>
          {tested && !green && probe!.checks.filter((c) => c.passed === false).slice(0, 1)
            .map((c) => (
              <div key={c.id} className="fs12 neg">{c.title}</div>
            ))}
        </div>

        <div className="row gap6 wrap">
          {tested && (
            <button className="btn ghost sm" onClick={onProbe}>گزارش آزمایش</button>
          )}
          <button className="btn outline sm"
                  disabled={readOnly || !canAdminister}
                  title={!canAdminister ? "فقط مدیر می‌تواند اتصال را آزمایش کند" : ""}
                  onClick={() => onAsk({
                    action: `آزمایش اتصال «${conn.display_name}»`,
                    description: <>سامانه وصل می‌شود، مشخصات حساب و فهرست نمادها را
                      می‌خواند و قطع می‌کند. <strong>هیچ سفارشی فرستاده نمی‌شود</strong> —
                      این محدودیت در خود کد اعمال شده، نه فقط یک قول.</>,
                    path: "/api/brokers/test", body: { id: conn.id },
                  })}>
            آزمایش اتصال
          </button>
          <button className="btn ghost sm" disabled={readOnly || !canAdminister}
                  onClick={onEdit}>ویرایش</button>
          {!conn.enabled && (
            <button className="btn sm" disabled={readOnly || !canAdminister || !green || blocked}
                    title={blocked ? "اول معامله‌های باز را ببندید"
                      : stale ? "آزمایش بیش از ۲۴ ساعت قدیمی است؛ دوباره آزمایش کنید"
                        : !green ? "اول آزمایش موفق لازم است" : ""}
                    onClick={() => onAsk({
                      action: `فعال کردن «${conn.display_name}»`,
                      danger: conn.declared_account_type === "live",
                      description: (
                        <div className="stack gap8">
                          <span>از این پس ربات سفارش‌ها را به این بروکر می‌فرستد.</span>
                          {conn.declared_account_type === "live" && (
                            <Banner tone="neg" icon="⚠">
                              این یک حساب <strong>واقعی</strong> است. از این لحظه
                              زیان‌ها واقعی‌اند.
                            </Banner>)}
                          {conn.declared_account_type === "live" && (
                            <span className="muted fs12">
                              برای حساب واقعی، سامانه علاوه بر این بررسی می‌کند که
                              لایسنس اجازهٔ معاملهٔ واقعی بدهد و دست‌کم یک استراتژی
                              حکم «پذیرفته شد» گرفته باشد. اگر هرکدام نباشد،
                              فعال‌سازی با توضیح رد می‌شود.
                            </span>)}
                          <span className="muted fs12">
                            تغییر ذخیره می‌شود ولی تا راه‌اندازی دوبارهٔ سرویس به
                            کار نمی‌افتد.
                          </span>
                        </div>),
                      path: "/api/brokers/activate", body: { id: conn.id },
                    })}>
              فعال کن
            </button>
          )}
          <button className="btn danger sm" disabled={readOnly || !canAdminister || conn.enabled}
                  title={conn.enabled ? "بروکر فعال را نمی‌شود پاک کرد" : ""}
                  onClick={() => onAsk({
                    action: `حذف «${conn.display_name}»`, danger: true,
                    description: <>این اتصال و رمز ذخیره‌شده‌اش پاک می‌شوند.
                      تاریخچهٔ معامله‌ها دست نمی‌خورد.</>,
                    path: "/api/brokers/delete", body: { id: conn.id },
                  })}>
            حذف
          </button>
        </div>
      </div>
    </div>
  );
}

type FormState = typeof BLANK & { editing: boolean };

function ConnectionForm({ form, setForm, profiles, existing, credentialAvailable, onSubmit }: {
  form: FormState; setForm: (f: FormState | null) => void;
  profiles: { value: string; label: string }[]; existing: boolean;
  credentialAvailable: boolean;
  onSubmit: (payload: Record<string, unknown>) => void;
}) {
  const set = (k: keyof typeof BLANK, v: string) => setForm({ ...form, [k]: v });

  const idProblem = /^[a-z0-9][a-z0-9_-]{0,38}[a-z0-9]$/.test(form.id)
    ? null : "شناسه: ۲ تا ۴۰ نویسه، فقط حرف انگلیسی کوچک، رقم، «-» یا «_».";
  const ready = !idProblem && form.display_name.trim().length > 0;

  return (
    <Modal open title={existing ? "ویرایش بروکر" : "افزودن بروکر"}
           onClose={() => setForm(null)}
           footer={
             <>
               <button className="btn ghost" onClick={() => setForm(null)}>انصراف</button>
               <button className="btn" disabled={!ready} onClick={() => {
                 const payload: Record<string, unknown> = {
                   id: form.id, display_name: form.display_name.trim(),
                   profile: form.profile,
                   declared_account_type: form.declared_account_type,
                   server: form.server.trim(),
                   terminal_path: form.terminal_path.trim(),
                   account_currency: form.account_currency.trim().toUpperCase(),
                   exchange_id: form.exchange_id.trim(), notes: form.notes.trim(),
                   origin: "manual",
                 };
                 // undefined => "leave the stored value alone". The login is
                 // shown MASKED, so submitting the form as displayed used to
                 // send an empty account number -- which the server reads as a
                 // change of identity, so it cleared the passed test AND
                 // switched the connection off. Renaming a connection took the
                 // live venue out of service.
                 if (form.login !== "") payload.login = form.login.trim();
                 if (form.secret !== "") payload.secret = form.secret;
                 if (form.clearSecret) payload.secret = "";
                 onSubmit(payload);
               }}>ذخیره</button>
             </>
           }>
      <div className="stack gap12">
        <Field label="شناسه" htmlFor="conn-id" help={idProblem ?? "یک نام کوتاه انگلیسی برای خود سامانه."}>
          <input className="input mono ltr" id="conn-id" value={form.id}
                 disabled={existing} maxLength={40}
                 onChange={(e) => set("id", e.target.value.toLowerCase())} />
        </Field>
        <Field label="نامی که در داشبورد می‌بینید" htmlFor="conn-name">
          <input className="input" value={form.display_name}
                 maxLength={LIMITS.display_name} id="conn-name"
                 onChange={(e) => set("display_name", e.target.value)} />
        </Field>
        <Field label="بروکر" htmlFor="conn-profile"
               help="اگر بروکرتان در فهرست نیست، «هر بروکر متاتریدر ۵» را بگذارید.">
          <select className="input" id="conn-profile" value={form.profile}
                  onChange={(e) => set("profile", e.target.value)}>
            {profiles.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
        </Field>
        <Field label="این حساب واقعی است یا تمرینی؟"
               help="این را خودتان اعلام می‌کنید و سامانه موقع آزمایش با خودِ بروکر مقایسه می‌کند. اگر نخواند، اجازهٔ فعال‌سازی داده نمی‌شود.">
          <Seg value={form.declared_account_type}
               onChange={(v) => set("declared_account_type", v)}
               options={[{ value: "demo", label: "تمرینی" },
                         { value: "live", label: "واقعی" }]} />
        </Field>

        <Field label="نام سرور" htmlFor="conn-server"
               help="دقیقاً همان رشته‌ای که در خود ترمینال متاتریدر نوشته شده، مثلاً AMarkets-Demo.">
          <input className="input mono ltr" value={form.server}
                 maxLength={LIMITS.server} id="conn-server"
                 onChange={(e) => set("server", e.target.value)} />
        </Field>
        <Field label="شمارهٔ حساب" htmlFor="conn-login">
          <input className="input mono ltr" value={form.login}
                 maxLength={LIMITS.login} id="conn-login"
                 onChange={(e) => set("login", e.target.value)} />
        </Field>
        <Field label="رمز حساب" htmlFor="conn-secret"
               help={credentialAvailable
                 ? (existing ? "خالی بگذارید تا رمز فعلی دست‌نخورده بماند."
                   : "قفل‌شده ذخیره می‌شود و هرگز دوباره نمایش داده نمی‌شود.")
                 : "ذخیرهٔ رمز در دسترس نیست چون کلید رمزنگاری پیدا نشد."}>
          <input className="input ltr" id="conn-secret" type="password"
                 value={form.secret} maxLength={512}
                 disabled={!credentialAvailable || form.clearSecret}
                 autoComplete="new-password"
                 onChange={(e) => set("secret", e.target.value)} />
        </Field>
        {existing && credentialAvailable && (
          <Field label="پاک کردن رمز ذخیره‌شده"
                 help="رمز از حافظه حذف می‌شود و سرویس دیگر خودش وارد حساب نمی‌شود.">
            <Switch checked={form.clearSecret} label="رمز ذخیره‌شده پاک شود"
                    onChange={(v) => setForm({ ...form, clearSecret: v,
                                               secret: v ? "" : form.secret })} />
          </Field>
        )}
        <Banner tone="flat" icon="◎">
          اگر ترمینال متاتریدر را خودتان باز کرده و وارد حساب شده‌اید، نیازی به
          وارد کردن رمز نیست — سامانه به همان ترمینال وصل می‌شود. رمز فقط وقتی
          لازم است که سرویس باید خودش وارد حساب شود.
        </Banner>
        <Field label="مسیر برنامهٔ متاتریدر" htmlFor="conn-path"
               help={"اختیاری. اگر خالی باشد، ترمینالی که همین حالا باز است " +
                     "استفاده می‌شود. اگر پر می‌کنید، باید مسیر کامل تا خود فایل " +
                     "برنامه باشد (مثلاً …\\terminal64.exe)، نه تا پوشه."}>
          <input className="input mono ltr" value={form.terminal_path}
                 maxLength={LIMITS.terminal_path} id="conn-path"
                 onChange={(e) => set("terminal_path", e.target.value)} />
        </Field>
        <Field label="ارز حساب" htmlFor="conn-ccy">
          <input className="input mono ltr" value={form.account_currency}
                 maxLength={LIMITS.account_currency} id="conn-ccy"
                 onChange={(e) => set("account_currency", e.target.value)} />
        </Field>
        <Field label="یادداشت" htmlFor="conn-notes" help="اختیاری — برای خودتان.">
          <input className="input" value={form.notes}
                 maxLength={LIMITS.notes} id="conn-notes"
                 onChange={(e) => set("notes", e.target.value)} />
        </Field>
      </div>
    </Modal>
  );
}

function ProbeDetail({ report }: { report: ProbeReport }) {
  const groups = useMemo(() => ({
    bad: report.checks.filter((c) => c.passed === false),
    unknown: report.checks.filter((c) => c.passed === null),
    good: report.checks.filter((c) => c.passed === true),
  }), [report]);

  return (
    <div className="stack gap12">
      <Banner tone={report.ok ? "info" : "neg"} icon={report.ok ? "✓" : "✕"}>
        {report.ok
          ? <>همه‌چیز درست است. این نرم‌افزار می‌تواند با این حساب کار کند —
              که با «این بروکر قابل اعتماد است» یکی نیست.</>
          : <>{groups.bad.length} ایراد پیدا شد. تا وقتی ایرادهای جدی برطرف نشوند،
              فعال‌سازی ممکن نیست.</>}
      </Banner>

      <div className="kv-grid">
        <KV k="مدت آزمایش"
            v={<><span className="num">{report.duration_sec}</span> ثانیه</>} />
        <KV k="تعداد نمادها" v={<span className="num">{report.symbols_total}</span>} />
        {report.account_id !== undefined && (
          <KV k="به کدام حساب وصل شد"
              v={<span className="mono ltr">{report.account_id || "—"}</span>}
              hint={<>شمارهٔ حساب عمداً ناقص نشان داده می‌شود. همین سه رقم آخر
                برای تشخیص اینکه آزمایش روی حساب درستی انجام شده کافی است.</>} />
        )}
        {report.account_currency && (
          <KV k="ارز حساب" v={<span className="mono ltr">{report.account_currency}</span>} />
        )}
        {report.finished_ns > 0 && (
          <KV k="زمان آزمایش" v={ago(report.finished_ns)} />
        )}
      </div>

      {[["bad", "ایرادها"], ["unknown", "نامشخص"], ["good", "درست"]].map(([key, title]) => {
        const items = (groups as any)[key] as ProbeReport["checks"];
        if (!items.length) return null;
        return (
          <div key={key} className="stack gap8">
            <div className="fs12 muted">{title}</div>
            {items.map((c) => (
              <div key={c.id} className="kv" style={{ alignItems: "flex-start" }}>
                <span className="k">
                  {c.passed === false ? "✕" : c.passed === null ? "—" : "✓"} {c.title}
                  {c.severity === "block" && c.passed === false &&
                    <Chip tone="neg">جدی</Chip>}
                </span>
                <span className="v prose fs12">{c.detail}</span>
              </div>
            ))}
          </div>
        );
      })}

      {report.mismatches.length > 0 && (
        <Disclosure summary={`${report.mismatches.length} اختلاف با پروفایل`}>
          <div className="stack gap4 fs11 mono ltr">
            {report.mismatches.map((m, i) => <div key={i}>{m}</div>)}
          </div>
        </Disclosure>
      )}
      {Object.keys(report.symbol_examples).length > 0 && (
        <Disclosure summary="این بروکر نمادها را چطور می‌نویسد">
          <div className="stack gap4 fs12 mono ltr">
            {Object.entries(report.symbol_examples).map(([k, v]) => (
              <div key={k}>{k} → {v}</div>
            ))}
          </div>
        </Disclosure>
      )}
    </div>
  );
}
