import React, { useCallback, useEffect, useState } from "react";
import type { Provider } from "../api";
import {
  Banner, Card, Chip, ConfirmWrite, Disclosure, Empty, Field, NumberField, Switch, ago, fa,
} from "../components/ui";
import type { NotifyChannel, NotifyView } from "../types";

/* Telegram and Bale. The page leads with the one property that makes phone
   commands safe to offer at all: a phone can STOP the robot, never make it
   take risk. */

type Write = (path: string, body: unknown, totp: string) =>
  Promise<{ ok: boolean; detail: string }>;
type Kind = "telegram" | "bale";

const CATEGORY_FA: Record<string, { label: string; help: string }> = {
  critical: { label: "هشدارهای مهم", help: "توقف ربات، توقف اضطراری، اختلاف با بروکر، استراحت اجباری، تغییر حالت" },
  trades: { label: "باز و بسته شدن معامله‌ها", help: "هر معامله‌ای که باز یا بسته شد، و سفارش‌های ردشده" },
  proposals: { label: "پیشنهادهای معامله", help: "در حالت «فقط پیشنهاد»؛ ممکن است زیاد باشد" },
  learning: { label: "یادگیری و آزمایشگاه", help: "افت عملکرد، نتیجه آزمایشگاه شبانه، گزارش هفتگی" },
  security: { label: "امنیت", help: "چند ورود ناموفق پشت سر هم، تغییرهای ردشده" },
  daily: { label: "گزارش روزانه", help: "یک خلاصه در روز" },
};

const STEPS: Record<Kind, React.ReactNode> = {
  telegram: (
    <ol className="fs12" style={{ lineHeight: 2, paddingInlineStart: 18 }}>
      <li>در تلگرام <span className="ltr mono">@BotFather</span> را جست‌وجو و باز کنید (تیک آبی دارد).</li>
      <li>بنویسید <span className="ltr mono">/newbot</span>، یک اسم و بعد یک نام کاربری که به
        <span className="ltr mono"> bot </span>ختم شود بدهید.</li>
      <li>BotFather یک «توکن» می‌دهد، شبیه <span className="ltr mono">123456789:AAH…</span>. آن را
        کپی کنید، پایین در «توکن ربات» بگذارید و «ذخیره» را بزنید. توکن را به هیچ‌کس ندهید.</li>
      <li>ربات تازه‌تان را در تلگرام باز کنید، <strong>Start</strong> را بزنید و یک پیام بفرستید.</li>
      <li>اینجا «پیدا کردن شناسه گفت‌وگو» را بزنید و گفت‌وگوی خودتان را انتخاب کنید.</li>
      <li>«فعال» را روشن کنید، ذخیره کنید و «پیام آزمایشی» را بزنید.</li>
    </ol>
  ),
  bale: (
    <ol className="fs12" style={{ lineHeight: 2, paddingInlineStart: 18 }}>
      <li>در پیام‌رسان بله، <span className="ltr mono">@botfather</span> (سازنده رسمی «بازو» در
        بله) را باز کنید.</li>
      <li>یک بازوی تازه بسازید (دستور <span className="ltr mono">/newbot</span>) و اسم و نام کاربری بدهید.</li>
      <li>توکن بازو را (شبیه <span className="ltr mono">123456789:AbC…</span>) کپی کنید، پایین در «توکن
        ربات» بگذارید و «ذخیره» را بزنید.</li>
      <li>بازوی خودتان را در بله باز کنید، «شروع» را بزنید و یک پیام بفرستید.</li>
      <li>اینجا «پیدا کردن شناسه گفت‌وگو» را بزنید و گفت‌وگوی خودتان را انتخاب کنید.</li>
      <li>«فعال» را روشن کنید، ذخیره کنید و «پیام آزمایشی» را بزنید.</li>
    </ol>
  ),
};

export default function NotifyPage({ provider, write, readOnly, canAdminister }: {
  provider: Provider; write: Write; readOnly: boolean; canAdminister: boolean;
}) {
  const [view, setView] = useState<NotifyView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, React.ReactNode>>({});
  const [chats, setChats] = useState<Record<string, { id: string; type: string; name: string }[]>>({});
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown;
    after?: (detail: string) => void;
  }>(null);

  const load = useCallback(async () => {
    try {
      setView(await provider.get<NotifyView>("/api/notify"));
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [provider]);

  useEffect(() => { load(); }, [load]);

  if (error) return <Banner tone="neg" icon="✕">داده‌های این صفحه بارگذاری نشد: {error}</Banner>;
  if (!view) return <Empty>در حال بارگذاری…</Empty>;
  if (!view.available || !view.channels) {
    return <Banner tone="warn" icon="◈">اعلان‌ها در این نسخه فعال نیست.</Banner>;
  }
  const disabled = readOnly || !canAdminister;

  return (
    <div className="stack gap16">
      <Banner tone="info" icon="🔔">
        <strong>ربات هر اتفاق مهم را به تلگرام یا بله شما می‌فرستد</strong> — توقف، باز و بسته
        شدن معامله، استراحت اجباری، نتیجه یادگیری شبانه و یک گزارش روزانه. پیام‌ها هیچ رمز یا
        شماره حسابی ندارند.
      </Banner>
      <Banner tone="warn" icon="🔐">
        اگر «دستورها» را روشن کنید، از گوشی فقط دو کار ممکن است: <strong>«وضعیت»</strong> و
        <strong> «توقف»</strong> (توقف اضطراری: معامله تازه باز نمی‌شود). هیچ دستوری برای باز کردن
        معامله یا <strong>برداشتن</strong> توقف وجود ندارد؛ آن فقط از همین داشبورد و با کد
        دومرحله‌ای ممکن است. پس اگر گوشی گم شود، بدترین اتفاق توقف ربات است، نه ضرر. دستورها فقط از
        همان گفت‌وگویی پذیرفته می‌شوند که اینجا ثبت کرده‌اید، و دستور قدیمی‌تر از ۵ دقیقه نادیده
        گرفته می‌شود.
      </Banner>
      {view.token_storage !== "ok" && (
        <Banner tone="neg" icon="✕">محل امن نگهداری توکن در دسترس نیست؛ توکن ذخیره نمی‌شود.
          اسکریپت عیب‌یابی (Diagnose) را اجرا کنید.</Banner>
      )}

      <div className="grid g2">
        {(["telegram", "bale"] as Kind[]).map((kind) => (
          <ChannelCard key={kind} kind={kind} ch={view.channels![kind]}
                       categories={view.categories ?? Object.keys(CATEGORY_FA)}
                       disabled={disabled} result={result[kind]} chats={chats[kind]}
                       picked={picked[kind]}
                       onPick={(id) => setPicked({ ...picked, [kind]: id })}
                       onSave={(body) => setConfirm({
                         action: `ذخیره تنظیمات ${view.channels![kind].label_fa}`,
                         description: <>تنظیمات و (اگر وارد کرده‌اید) توکن ذخیره می‌شود. توکن
                           رمزگذاری‌شده نگهداری می‌شود و دیگر نمایش داده نمی‌شود.</>,
                         path: "/api/notify/channel", body: { channel: kind, ...body },
                         after: () => setResult({ ...result, [kind]: <Chip tone="pos">ذخیره شد</Chip> }),
                       })}
                       onDiscover={() => setConfirm({
                         action: "پیدا کردن شناسه گفت‌وگو",
                         description: "از ربات می‌پرسیم اخیراً چه کسانی به آن پیام داده‌اند. قبلش باید خودتان به ربات یک پیام داده باشید.",
                         path: "/api/notify/discover", body: { channel: kind },
                         after: (detail) => {
                           try {
                             const r = JSON.parse(detail);
                             if (!r.ok) {
                               setResult({ ...result, [kind]: <span className="neg fs12">{r.error}</span> });
                               return;
                             }
                             setChats({ ...chats, [kind]: r.chats });
                             setResult({ ...result, [kind]: r.chats.length
                               ? <span className="fs12">ربات <span className="ltr mono">@{r.bot}</span> — یک گفت‌وگو انتخاب کنید</span>
                               : <span className="fs12 warn">پیامی پیدا نشد. اول در {view.channels![kind].label_fa} به ربات پیام بدهید.</span> });
                           } catch { /* the modal shows the raw detail */ }
                         },
                       })}
                       onTest={() => setConfirm({
                         action: "ارسال پیام آزمایشی",
                         description: "یک پیام کوتاه به گفت‌وگوی ثبت‌شده فرستاده می‌شود.",
                         path: "/api/notify/test", body: { channel: kind },
                         after: (detail) => {
                           try {
                             const r = JSON.parse(detail);
                             setResult({ ...result, [kind]: r.ok
                               ? <Chip tone="pos">پیام رفت؛ گوشی را ببینید</Chip>
                               : <span className="neg fs12 ltr">{r.error}</span> });
                           } catch { /* ignore */ }
                         },
                       })} />
        ))}
      </div>

      <DailyHour hour={view.daily_hour_utc ?? 17} disabled={disabled}
                 onSave={(h) => {
                   const ch = view.channels!.telegram;
                   setConfirm({
                     action: "ذخیره ساعت گزارش روزانه",
                     description: "ساعت گزارش روزانه برای هر دو پیام‌رسان تغییر می‌کند.",
                     path: "/api/notify/channel",
                     // chat id and categories omitted: the server keeps them.
                     body: { channel: "telegram", enabled: ch.enabled, commands: ch.commands,
                             daily_hour_utc: h },
                   });
                 }} />

      <ConfirmWrite open={!!confirm} action={confirm?.action ?? ""}
                    description={confirm?.description}
                    onClose={() => setConfirm(null)}
                    onConfirm={async (totp) => {
                      if (!confirm) return { ok: false, detail: "" };
                      const res = await write(confirm.path, confirm.body, totp);
                      if (res.ok) {
                        confirm.after?.(res.detail);
                        await load();
                      }
                      return res;
                    }} />
    </div>
  );
}

function ChannelCard({ kind, ch, categories, disabled, result, chats, picked, onPick, onSave,
                       onDiscover, onTest }: {
  kind: Kind; ch: NotifyChannel; categories: string[]; disabled: boolean;
  result?: React.ReactNode; chats?: { id: string; type: string; name: string }[];
  picked?: string; onPick: (id: string) => void;
  onSave: (body: Record<string, unknown>) => void; onDiscover: () => void; onTest: () => void;
}) {
  const [enabled, setEnabled] = useState(ch.enabled);
  const [token, setToken] = useState("");
  const [chat, setChat] = useState(ch.chat_id === "***" ? "" : ch.chat_id);
  const [cats, setCats] = useState<string[]>(ch.categories);
  const [commands, setCommands] = useState(ch.commands);
  const hiddenChat = ch.chat_id === "***";
  const effectiveChat = picked || chat;
  const configured = ch.token_stored && (ch.chat_id !== "");

  useEffect(() => { if (picked) setChat(picked); }, [picked]);

  const tokenOk = !token || /^\d{3,15}:[A-Za-z0-9_-]{16,80}$/.test(token.trim());
  const chatOk = !effectiveChat || /^(-?\d{1,20}|@[A-Za-z0-9_]{5,32})$/.test(effectiveChat);
  const needsChat = enabled && !effectiveChat && !hiddenChat;

  return (
    <Card title={ch.label_fa}
          actions={<Chip tone={ch.enabled ? "pos" : "flat"}>{ch.enabled ? "فعال" : "خاموش"}</Chip>}>
      <div className="stack gap12">
        <div className="row gap6 wrap fs12">
          <Chip tone={ch.token_stored ? "pos" : "warn"}>{ch.token_stored ? "توکن ذخیره شده" : "بدون توکن"}</Chip>
          <Chip>ارسال‌شده: {fa(ch.sent)}</Chip>
          {ch.failed > 0 && <Chip tone="warn">ناموفق: {fa(ch.failed)}</Chip>}
          {ch.last_ok_ns > 0 && <span className="muted">آخرین ارسال {ago(ch.last_ok_ns)}</span>}
        </div>
        {ch.last_error && <Banner tone="warn" icon="!">آخرین خطا: <span className="ltr mono fs12">{ch.last_error}</span>
          {ch.last_error.includes("401") && <> — توکن اشتباه است یا باطل شده.</>}
          {ch.last_error.includes("403") && <> — ربات را در گفت‌وگو Start نکرده‌اید یا مسدودش کرده‌اید.</>}
          {ch.last_error.includes("400") && ch.last_error.toLowerCase().includes("chat") &&
            <> — شناسه گفت‌وگو اشتباه است.</>}
        </Banner>}

        <Disclosure summary={configured ? "راهنمای راه‌اندازی" : "راهنمای راه‌اندازی (از اینجا شروع کنید)"}>
          {STEPS[kind]}
          {kind === "telegram" && (
            <p className="fs11 muted">اگر سرور به <span className="ltr">api.telegram.org</span> دسترسی
              ندارد (فیلتر یا فایروال)، از بله استفاده کنید؛ بله از داخل ایران در دسترس است.</p>
          )}
        </Disclosure>

        <Field label="توکن ربات" help={ch.token_stored ? "خالی بگذارید تا توکن فعلی بماند."
                                                      : "از BotFather می‌گیرید."}>
          <input className="input ltr mono" type="password" autoComplete="off" value={token}
                 placeholder={ch.token_stored ? "••••••••" : "123456789:AAH…"} disabled={disabled}
                 maxLength={120} onChange={(e) => setToken(e.target.value)} />
        </Field>
        <Field label="شناسه گفت‌وگو" help={hiddenChat ? "ثبت شده (برای امنیت نمایش داده نمی‌شود)."
                                               : "عدد؛ با دکمه زیر پیدا می‌شود."}>
          <input className="input ltr mono" value={chat} disabled={disabled} maxLength={40}
                 placeholder={hiddenChat ? "***" : "123456789"}
                 onChange={(e) => setChat(e.target.value.trim())} />
        </Field>
        <div className="row gap6 wrap">
          <button className="btn ghost sm" disabled={disabled || !ch.token_stored}
                  title={ch.token_stored ? "" : "اول توکن را ذخیره کنید"}
                  onClick={onDiscover}>پیدا کردن شناسه گفت‌وگو</button>
          <button className="btn ghost sm" disabled={disabled || !configured}
                  onClick={onTest}>پیام آزمایشی</button>
          {result}
        </div>
        {chats && chats.length > 0 && (
          <div className="row gap6 wrap">
            {chats.map((c) => (
              <button key={c.id} className={`btn sm ${picked === c.id ? "" : "ghost"}`}
                      disabled={disabled} onClick={() => onPick(c.id)}>
                {c.name || "بی‌نام"} <span className="ltr mono fs11">({c.id})</span></button>
            ))}
          </div>
        )}

        <Field label="چه چیزهایی فرستاده شود">
          <div className="row gap6 wrap">
            {categories.map((c) => {
              const on = cats.includes(c);
              return (
                <button key={c} type="button" className={`btn sm ${on ? "" : "ghost"}`}
                        aria-pressed={on} disabled={disabled} title={CATEGORY_FA[c]?.help}
                        onClick={() => setCats(on ? cats.filter((x) => x !== c) : [...cats, c])}>
                  {CATEGORY_FA[c]?.label ?? c}</button>
              );
            })}
          </div>
        </Field>
        <Field label="دستورها از گوشی" help="فقط «وضعیت» و «توقف». پیش‌فرض خاموش.">
          <Switch checked={commands} onChange={setCommands} disabled={disabled} label="دستورها از گوشی" />
        </Field>
        <Field label="فعال">
          <Switch checked={enabled} onChange={setEnabled} disabled={disabled} label="فعال" />
        </Field>
        {(!tokenOk || !chatOk || needsChat) && (
          <Banner tone="warn" icon="!">
            {!tokenOk && <>توکن شبیه توکن ربات نیست (عدد، دونقطه، حروف). </>}
            {!chatOk && <>شناسه گفت‌وگو باید عدد باشد. </>}
            {needsChat && <>برای فعال کردن، شناسه گفت‌وگو لازم است.</>}
          </Banner>
        )}
        <div className="row gap8">
          <button className="btn" disabled={disabled || !tokenOk || !chatOk || needsChat}
                  onClick={() => onSave({
                    enabled, categories: cats, commands,
                    ...(effectiveChat ? { chat_id: effectiveChat } : {}),
                    ...(token.trim() ? { token: token.trim() } : {}),
                  })}>ذخیره</button>
          {ch.token_stored && (
            <button className="btn ghost sm" disabled={disabled}
                    onClick={() => onSave({ enabled: false, categories: cats, commands: false,
                                            token: "" })}>حذف توکن</button>
          )}
        </div>
      </div>
    </Card>
  );
}

function DailyHour({ hour, disabled, onSave }: {
  hour: number; disabled: boolean; onSave: (h: number) => void;
}) {
  const [h, setH] = useState(hour);
  const tehran = (h * 60 + 210) % 1440;
  return (
    <Card title="گزارش روزانه">
      <Field label="ساعت ارسال (به وقت جهانی UTC)"
             help={<>یعنی حدود ساعت {fa(`${Math.floor(tehran / 60)}:${String(tehran % 60).padStart(2, "0")}`)} به وقت تهران.</>}>
        <div className="row gap8">
          <NumberField value={h} min={0} max={23} step={1} disabled={disabled} onChange={setH} />
          <button className="btn ghost sm" disabled={disabled || h === hour}
                  onClick={() => onSave(h)}>ذخیره</button>
        </div>
      </Field>
    </Card>
  );
}
