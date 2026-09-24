/**
 * Accounts and what each level may do.
 *
 * Three levels, named for what they DO rather than for what they are called in
 * the code: مدیر (owner), کاربر (operator), نظاره‌گر (viewer). The internal
 * names never change, because they are written into the audit chain.
 *
 * The one rule this page enforces visibly is the last-administrator guard. A
 * console where the only owner can demote themselves is a console that can be
 * bricked in one click: after that, nobody can change a risk limit, release
 * the kill switch, take the agent off autonomous, or create another owner —
 * and the only way back is a command line on the server, which the person who
 * just locked themselves out may not have.
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  Banner, Card, Chip, ConfirmWrite, Empty, Field, KV, Modal, Seg, ago,
} from "../components/ui";
import type { Provider } from "../api";
import type { AccountRow, AccountsView } from "../types";

type Props = {
  provider: Provider;
  write: (path: string, body: unknown, totp: string) => Promise<{ ok: boolean; detail: string }>;
  readOnly: boolean;
  canAdminister: boolean;
  me: string;
};

const ROLE_TONE: Record<string, "solid" | "info" | "flat"> = {
  owner: "solid", operator: "info", viewer: "flat",
};

export default function Users({ provider, write, readOnly, canAdminister, me }: Props) {
  const [data, setData] = useState<AccountsView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [enrolment, setEnrolment] = useState<null | { username: string; uri: string; note: string }>(null);
  const [passwordFor, setPasswordFor] = useState<string | null>(null);
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown;
    danger?: boolean; after?: (detail: string) => void;
  }>(null);

  const load = useCallback(async () => {
    try {
      setData(await provider.get<AccountsView>("/api/users"));
      setError(null);
    } catch (e) { setError(String(e)); }
  }, [provider]);

  useEffect(() => { if (canAdminister) void load(); }, [load, canAdminister]);

  if (!canAdminister) {
    return (
      <Card title="کاربران">
        <Banner tone="info" icon="🔐">
          فهرست کاربران فقط برای <strong>مدیر</strong> دیده می‌شود. دیدن اینکه چه
          کسانی می‌توانند وارد شوند، خودش یک اطلاعات حساس است: به کسی که بخواهد
          نفوذ کند می‌گوید کدام نام‌ها را امتحان کند و کدامشان اجازهٔ جابه‌جایی
          پول دارند.
        </Banner>
      </Card>
    );
  }

  if (error && !data) {
    return (
      <div className="stack gap12">
        <Banner tone="neg" icon="✕"><span dir="ltr">{error}</span></Banner>
        <button className="btn outline sm" style={{ alignSelf: "start" }}
                onClick={() => void load()}>تلاش دوباره</button>
      </div>
    );
  }
  if (!data) return <div className="muted fs12">در حال بارگذاری…</div>;

  const owners = data.users.filter((u) => u.role === "owner" && !u.disabled);

  return (
    <div className="stack gap16">
      {error && (
        <Banner tone="neg" icon="✕">
          به‌روزرسانی فهرست انجام نشد: <span dir="ltr">{error}</span>
        </Banner>
      )}

      <Card title="سه سطح دسترسی"
            sub="هر کاربر دقیقاً یکی از این‌هاست">
        <div className="stack gap8">
          {data.roles.map((r) => (
            <div key={r.id} className="kv" style={{ alignItems: "flex-start" }}>
              <span className="k">
                <Chip tone={ROLE_TONE[r.id] ?? "flat"}>{r.label}</Chip>
              </span>
              <span className="v prose fs12">{r.description}</span>
            </div>
          ))}
        </div>
        <div className="mt8">
          <Banner tone="flat" icon="◎">
            حتی «مدیر» هم برای هر تغییری باید یک کد شش‌رقمی تازه از برنامهٔ
            احراز هویتش وارد کند. ورود به داشبورد فقط اجازهٔ <strong>دیدن</strong>
            می‌دهد؛ هیچ نشستی به‌تنهایی اجازهٔ معامله ندارد.
          </Banner>
        </div>
      </Card>

      <Card title="کاربران" sub={`${data.users.length} حساب`}
            actions={
              <button className="btn sm" disabled={readOnly}
                      title={readOnly ? "در این حالت تغییری ممکن نیست" : ""}
                      onClick={() => setCreating(true)}>افزودن کاربر</button>
            }>
        {owners.length === 1 && (
          <div style={{ marginBottom: 12 }}>
            <Banner tone="warn" icon="⚠">
              فقط یک مدیر فعال دارید (<strong>{owners[0].username}</strong>). اگر
              گوشی‌اش گم شود یا گذرواژه‌اش را فراموش کند، هیچ‌کس از داشبورد
              نمی‌تواند سقف‌های ایمنی را عوض کند یا توقف اضطراری را آزاد کند.
              یک مدیر دوم بسازید.
            </Banner>
          </div>
        )}
        {data.users.length === 0 ? <Empty>هیچ حسابی نیست.</Empty> : (
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr>
                  <th>نام کاربری</th>
                  <th>سطح</th>
                  <th>وضعیت</th>
                  <th className="n">نشست باز</th>
                  <th>ساخته شده</th>
                  <th>کارها</th>
                </tr>
              </thead>
              <tbody>
                {data.users.map((u) => (
                  <UserRow key={u.username} user={u} me={me} readOnly={readOnly}
                           roles={data.roles} onAsk={setConfirm}
                           onResetPassword={() => { setPasswordFor(u.username); setNewPassword(""); }} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* ---------- create ---------- */}
      <CreateUser open={creating} onClose={() => setCreating(false)}
                  roles={data.roles} minLength={data.min_password_length}
                  onSubmit={(body) => setConfirm({
                    action: `ساختن کاربر «${body.username}»`,
                    description: (
                      <div className="stack gap8">
                        <KV k="نام کاربری" v={<span className="mono ltr">{body.username}</span>} />
                        <KV k="سطح"
                            v={data.roles.find((r) => r.id === body.role)?.label ?? body.role} />
                        {body.role === "owner" && (
                          <Banner tone="warn" icon="⚠">
                            سطح «مدیر» یعنی این حساب می‌تواند سقف‌های ایمنی را عوض
                            کند و ربات را کاملاً خودکار کند.
                          </Banner>)}
                        <span className="muted fs12">
                          بعد از ساخته شدن، یک آدرس برای ثبت در برنامهٔ احراز هویت
                          نشان داده می‌شود — فقط همان یک بار.
                        </span>
                      </div>),
                    path: "/api/users/create", body,
                    after: (detail) => {
                      setCreating(false);
                      try {
                        const r = JSON.parse(detail);
                        if (r.totp_uri) setEnrolment({
                          username: r.username, uri: r.totp_uri, note: r.note ?? "",
                        });
                      } catch { /* the list refresh is enough */ }
                    },
                  })} />

      {/* ---------- one-time enrolment ---------- */}
      <Modal open={!!enrolment} title="ثبت کد دومرحله‌ای"
             onClose={() => setEnrolment(null)}
             footer={<button className="btn" onClick={() => setEnrolment(null)}>بستم</button>}>
        {enrolment && (
          <div className="stack gap12">
            <Banner tone="warn" icon="⚠">{enrolment.note}</Banner>
            <Field label={`آدرس ثبت برای «${enrolment.username}»`} htmlFor="enrol-uri"
                   help="این را در Google Authenticator، Aegis یا هر برنامهٔ مشابه وارد کنید.">
              <textarea className="input mono ltr" id="enrol-uri" rows={4} dir="ltr" readOnly
                        value={enrolment.uri} style={{ resize: "vertical" }} />
            </Field>
            <button className="btn outline sm"
                    onClick={() => void navigator.clipboard?.writeText(enrolment.uri)}>
              کپی
            </button>
          </div>
        )}
      </Modal>

      {/* ---------- password reset ---------- */}
      <Modal open={passwordFor !== null} title={`گذرواژهٔ تازه برای «${passwordFor}»`}
             onClose={() => setPasswordFor(null)}
             footer={
               <>
                 <button className="btn ghost" onClick={() => setPasswordFor(null)}>انصراف</button>
                 <button className="btn"
                         disabled={newPassword.length < (data.min_password_length ?? 12)}
                         onClick={() => setConfirm({
                           action: `عوض کردن گذرواژهٔ «${passwordFor}»`,
                           description: <>کد دومرحله‌ای این کاربر دست‌نخورده می‌ماند.
                             عوض کردن گذرواژه کار روزمره‌ای است؛ عوض کردن کد
                             دومرحله‌ای نیست، و قاطی کردن این دو به کاربر یاد
                             می‌دهد هر وقت گفتند کد تازه را قبول کند.</>,
                           path: "/api/users/password",
                           body: { username: passwordFor, password: newPassword },
                           after: () => { setPasswordFor(null); setNewPassword(""); },
                         })}>
                   ثبت
                 </button>
               </>
             }>
        <Field label="گذرواژهٔ تازه" htmlFor="reset-password"
               help={`دست‌کم ${data.min_password_length} نویسه. یک عبارت چندکلمه‌ای که فقط خودتان می‌دانید از یک کلمهٔ پیچیدهٔ کوتاه امن‌تر است.`}>
          <input className="input ltr" id="reset-password" type="password"
                 value={newPassword} autoComplete="new-password" maxLength={256}
                 onChange={(e) => setNewPassword(e.target.value)} />
        </Field>
      </Modal>

      <ConfirmWrite
        open={!!confirm} action={confirm?.action ?? ""}
        description={confirm?.description} danger={confirm?.danger}
        onClose={() => setConfirm(null)}
        onConfirm={async (totp) => {
          if (!confirm) return { ok: false, detail: "" };
          const res = await write(confirm.path, confirm.body, totp);
          if (res.ok) {
            confirm.after?.(res.detail);
            await load();
            // Close it. Both dialogs share one z-index, so leaving this open
            // painted it OVER the one-time enrolment dialog -- the operator
            // had to dismiss a confirmation before they could see the second
            // factor they had just created.
            setTimeout(() => setConfirm(null), 700);
          }
          return res;
        }} />
    </div>
  );
}

/* --------------------------------------------------------------------- */

function UserRow({ user, me, readOnly, roles, onAsk, onResetPassword }: {
  user: AccountRow; me: string; readOnly: boolean;
  roles: { id: string; label: string }[];
  onAsk: (c: any) => void; onResetPassword: () => void;
}) {
  const isMe = user.username === me;
  const locked = user.is_last_owner;

  return (
    <tr>
      <td>
        <span className="mono ltr">{user.username}</span>
        {isMe && <Chip tone="flat">خودتان</Chip>}
      </td>
      <td>
        <Chip tone={ROLE_TONE[user.role] ?? "flat"}>{user.role_label}</Chip>
        {locked && <Chip tone="warn" title="تنها مدیر فعال">تنها مدیر</Chip>}
      </td>
      <td>
        {user.disabled
          ? <span className="muted fs12">غیرفعال</span>
          : <span className="pos fs12">فعال</span>}
      </td>
      <td className="n">{user.active_sessions}</td>
      <td className="fs12 muted">{ago(user.created_ns)}</td>
      <td>
        <div className="row gap6 wrap">
          <select className="input sm" value={user.role} disabled={readOnly || locked}
                  aria-label={`سطح دسترسی ${user.username}`}
                  style={{ width: "auto", minWidth: 96 }}
                  title={locked ? "تنها مدیر فعال را نمی‌شود پایین آورد" : ""}
                  onChange={(e) => {
                    const role = e.target.value;
                    if (role === user.role) return;
                    onAsk({
                      action: `تغییر سطح «${user.username}»`,
                      danger: role === "owner",
                      description: (
                        <div className="stack gap8">
                          <span>
                            سطح از «{user.role_label}» به «
                            {roles.find((r) => r.id === role)?.label ?? role}» عوض می‌شود.
                          </span>
                          {isMe && role !== "owner" && (
                            <Banner tone="warn" icon="⚠">
                              دارید سطح <strong>خودتان</strong> را پایین می‌آورید.
                              بعد از این نمی‌توانید همین کار را برگردانید.
                            </Banner>)}
                          <span className="muted fs12">
                            نشست‌های باز این کاربر بلافاصله سطح تازه را می‌گیرند؛
                            منتظر خروج و ورود دوباره نمی‌ماند.
                          </span>
                        </div>),
                      path: "/api/users/role", body: { username: user.username, role },
                    });
                  }}>
            {roles.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
          </select>

          <button className="btn ghost sm" disabled={readOnly}
                  title={readOnly ? "در این حالت تغییری ممکن نیست"
                    : `گذرواژهٔ تازه برای ${user.username}`}
                  onClick={onResetPassword}>گذرواژه</button>

          <button className="btn ghost sm" disabled={readOnly}
                  onClick={() => onAsk({
                    action: `ساختن کد دومرحله‌ای تازه برای «${user.username}»`,
                    danger: true,
                    description: (
                      <div className="stack gap8">
                        <span>کد قبلی از کار می‌افتد و همهٔ نشست‌های باز این کاربر
                          بسته می‌شود.</span>
                        <Banner tone="warn" icon="⚠">
                          این کار را فقط وقتی بکنید که گوشی کاربر گم شده باشد.
                          هر کسی که این کار را انجام دهد می‌تواند کد تازه را در
                          برنامهٔ خودش ثبت کند.
                        </Banner>
                      </div>),
                    path: "/api/users/totp", body: { username: user.username },
                  })}>
            کد دومرحله‌ای
          </button>

          <button className={`btn ${user.disabled ? "outline" : "ghost"} sm`}
                  disabled={readOnly || (locked && !user.disabled)}
                  title={locked && !user.disabled ? "تنها مدیر فعال را نمی‌شود غیرفعال کرد" : ""}
                  onClick={() => onAsk({
                    action: user.disabled
                      ? `فعال کردن «${user.username}»` : `غیرفعال کردن «${user.username}»`,
                    danger: !user.disabled,
                    description: user.disabled
                      ? <>این حساب دوباره می‌تواند وارد شود.</>
                      : <>این حساب دیگر نمی‌تواند وارد شود و نشست‌های بازش همین
                        حالا بسته می‌شوند. تاریخچه‌اش در دفتر رویدادها می‌ماند —
                        برای همین غیرفعال کردن از حذف کردن بهتر است.</>,
                    path: "/api/users/disable",
                    body: { username: user.username, disabled: !user.disabled },
                  })}>
            {user.disabled ? "فعال کن" : "غیرفعال کن"}
          </button>

          <button className="btn danger sm" disabled={readOnly || locked || isMe}
                  title={isMe ? "حساب خودتان را پاک نکنید"
                    : locked ? "تنها مدیر فعال را نمی‌شود حذف کرد" : ""}
                  onClick={() => onAsk({
                    action: `حذف کامل «${user.username}»`, danger: true,
                    description: <>معمولاً «غیرفعال کردن» جواب بهتری است: نام این
                      کاربر در دفتر رویدادها هست و بعد از حذف، آن رکوردها به نامی
                      اشاره می‌کنند که دیگر وجود ندارد. حذف برای حسابی است که
                      اشتباهی ساخته شده.</>,
                    path: "/api/users/delete", body: { username: user.username },
                  })}>
            حذف
          </button>
        </div>
      </td>
    </tr>
  );
}

function CreateUser({ open, onClose, roles, minLength, onSubmit }: {
  open: boolean; onClose: () => void;
  roles: { id: string; label: string; description: string }[];
  minLength: number;
  onSubmit: (body: { username: string; password: string; role: string }) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("viewer");

  useEffect(() => {
    if (open) { setUsername(""); setPassword(""); setRole("viewer"); }
  }, [open]);

  const nameOk = /^[A-Za-z0-9._-]{3,64}$/.test(username);
  const ready = nameOk && password.length >= minLength;

  return (
    <Modal open={open} title="افزودن کاربر" onClose={onClose}
           footer={
             <>
               <button className="btn ghost" onClick={onClose}>انصراف</button>
               <button className="btn" disabled={!ready}
                       onClick={() => onSubmit({ username, password, role })}>
                 بساز
               </button>
             </>
           }>
      <div className="stack gap12">
        <Field label="نام کاربری" htmlFor="new-username"
               help="۳ تا ۶۴ نویسه: حرف انگلیسی، رقم، نقطه، خط تیره یا زیرخط.">
          <input className="input mono ltr" id="new-username" value={username}
                 autoComplete="off" maxLength={64}
                 onChange={(e) => setUsername(e.target.value)} />
        </Field>
        <Field label="گذرواژه" htmlFor="new-password"
               help={`دست‌کم ${minLength} نویسه. گذرواژه‌های خیلی رایج — حتی بلندشان — پذیرفته نمی‌شوند؛ اگر رد شد، دلیلش نوشته می‌شود.`}>
          <input className="input ltr" id="new-password" type="password"
                 value={password} autoComplete="new-password" maxLength={256}
                 onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="سطح دسترسی"
               help="اگر شک دارید «نظاره‌گر» را انتخاب کنید — بالا بردنش همیشه ممکن است.">
          <Seg value={role} onChange={setRole}
               options={roles.map((r) => ({ value: r.id, label: r.label }))} />
        </Field>
        <Banner tone="flat" icon="◎">
          {roles.find((r) => r.id === role)?.description}
        </Banner>
      </div>
    </Modal>
  );
}
