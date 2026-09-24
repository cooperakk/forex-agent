import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  detectBackend, login, makeDemoProvider, makeLiveProvider, storeToken, storedToken,
  type Provider,
} from "./api";
import { Banner, Card, Chip, ConfirmWrite, Hint, Seg, useLocalState } from "./components/ui";
import AgentPage from "./pages/AgentPage";
import AIPage from "./pages/AIPage";
import ReferencePage from "./pages/ReferencePage";
import BrainPage from "./pages/BrainPage";
import NotifyPage from "./pages/NotifyPage";
import MacroPage from "./pages/MacroPage";
import ManualTrade from "./pages/ManualTrade";
import Audit from "./pages/Audit";
import Brokers from "./pages/Brokers";
import Glossary from "./pages/Glossary";
import Journal from "./pages/Journal";
import Licence from "./pages/Licence";
import Overview from "./pages/Overview";
import Positions from "./pages/Positions";
import Research from "./pages/Research";
import Risk from "./pages/Risk";
import Settings from "./pages/Settings";
import Users from "./pages/Users";
import type { Snapshot } from "./types";

type Page = "overview" | "positions" | "manual" | "journal" | "risk" | "research" | "agent"
  | "brain" | "macro" | "ai" | "reference" | "notify" | "settings" | "brokers" | "users"
  | "licence" | "audit" | "glossary";

/* Nav labels are the first words a newcomer reads, so they say what the page
   shows rather than what the subsystem is called. */
const NAV: { id: Page; label: string; icon: string }[] = [
  { id: "overview", label: "نمای کلی", icon: "◧" },
  { id: "positions", label: "معامله‌های باز", icon: "◈" },
  { id: "manual", label: "معامله دستی", icon: "✎" },
  { id: "journal", label: "تاریخچه معامله‌ها", icon: "▤" },
  { id: "risk", label: "سقف‌های ایمنی", icon: "◉" },
  { id: "research", label: "آزمایش و اثبات", icon: "⬡" },
  { id: "agent", label: "تصمیم‌های ربات", icon: "◐" },
  { id: "brain", label: "مغز ربات (یادگیری)", icon: "✺" },
  { id: "macro", label: "دلار و COT (بازار کلان)", icon: "$" },
  { id: "ai", label: "هوش مصنوعی و اخبار", icon: "✦" },
  { id: "reference", label: "قیمت مرجع (TradingView)", icon: "⚖" },
  { id: "notify", label: "اعلان‌ها (تلگرام و بله)", icon: "✉" },
  { id: "settings", label: "تنظیمات", icon: "⚙" },
  { id: "brokers", label: "بروکر و اتصال", icon: "⇄" },
  { id: "users", label: "کاربران", icon: "☰" },
  { id: "licence", label: "لایسنس", icon: "⬚" },
  { id: "audit", label: "دفتر ثبت رویدادها", icon: "⛓" },
  { id: "glossary", label: "واژه‌نامه ساده", icon: "◎" },
];

const API_BASE = (typeof window !== "undefined" && window.location.origin.startsWith("http"))
  ? window.location.origin : "";

export default function App() {
  const [page, setPage] = useLocalState<Page>("sentinel.page", "overview");
  const [theme, setTheme] = useLocalState<"light" | "dark" | "system">("sentinel.theme", "system");
  const [provider, setProvider] = useState<Provider | null>(null);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [needLogin, setNeedLogin] = useState(false);
  const [role, setRole] = useState<string>("viewer");
  const [me, setMe] = useState<string>("");
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown; danger?: boolean;
  }>(null);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
  }, [theme]);

  // Boot: try the backend; fall back to the bundled dataset and say so.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const token = storedToken();
      const hasBackend = API_BASE ? await detectBackend(API_BASE) : false;
      if (cancelled) return;
      if (hasBackend && token) {
        const p = makeLiveProvider(API_BASE, token);
        try {
          // ASK the server who this is. A restored session used to be assumed
          // to be an owner, so a viewer reopening the tab saw every write
          // control enabled: each one failed at the server, correctly, but the
          // person was told they could do things they could not, and a
          // disabled-looking console is far easier to reason about than one
          // that argues with you after the fact.
          const [s, who] = await Promise.all([
            p.snapshot(),
            p.get<{ username: string; role: string }>("/api/auth/me"),
          ]);
          if (cancelled) return;
          setProvider(p); setSnap(s);
          setRole(who.role ?? "viewer"); setMe(who.username ?? "");
          return;
        } catch {
          storeToken(null);
        }
      }
      if (hasBackend) { setNeedLogin(true); return; }
      const demo = makeDemoProvider();
      setProvider(demo);
      setSnap(await demo.snapshot());
    })();
    return () => { cancelled = true; };
  }, []);

  const refresh = useCallback(async () => {
    if (!provider) return;
    try {
      setSnap(await provider.snapshot());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [provider]);

  useEffect(() => {
    if (!provider || provider.kind !== "live") return;
    const t = setInterval(refresh, 15000);
    const off = provider.connectStream(() => refresh());
    return () => { clearInterval(t); off(); };
  }, [provider, refresh]);

  const write = useCallback(async (path: string, body: unknown, totp: string) => {
    if (!provider) return { ok: false, detail: "ارائه‌دهنده داده آماده نیست" };
    const res = await provider.write(path, body, totp);
    if (res.ok) await refresh();
    return res;
  }, [provider, refresh]);

  if (needLogin) return <Login onDone={async (token, r, username) => {
    storeToken(token);
    const p = makeLiveProvider(API_BASE, token);
    setProvider(p); setRole(r); setMe(username); setNeedLogin(false);
    setSnap(await p.snapshot());
  }} />;

  if (!snap || !provider) {
    return (
      <div className="row" style={{ height: "100vh", justifyContent: "center" }}>
        <div className="stack gap8" style={{ alignItems: "center" }}>
          <div className="brand-name">Sentinel-FX</div>
          <div className="muted fs12">در حال بارگذاری…</div>
        </div>
      </div>
    );
  }

  const demo = provider.kind === "demo";
  const readOnly = demo || role === "viewer";
  // Owner-only surfaces. In demo mode the pages still RENDER -- the point of
  // the demo is to show what the console looks like -- but every write is
  // refused by the demo provider and says so.
  const canAdminister = demo || role === "owner";
  const s = snap.status;

  return (
    <div className="shell">
      <nav className="sidebar" aria-label="ناوبری اصلی">
        <div className="brand">
          <div className="brand-name">Sentinel-FX</div>
          <div className="brand-sub ltr">AUTONOMOUS TRADING CONSOLE</div>
        </div>
        {NAV.map((n) => (
          <button key={n.id} className="nav-item" aria-current={page === n.id ? "page" : undefined}
                  onClick={() => setPage(n.id)}>
            <span aria-hidden="true" style={{ opacity: .7 }}>{n.icon}</span>
            {n.label}
            {n.id === "positions" && snap.advice.length > 0 &&
              <span className="badge-count">{snap.advice.length}</span>}
            {n.id === "brain" && s.cooldowns && Object.keys(s.cooldowns).length > 0 &&
              <span className="badge-count">{Object.keys(s.cooldowns).length}</span>}
            {n.id === "agent" && snap.proposals.filter((p) => p.status === "pending").length > 0 &&
              <span className="badge-count">
                {snap.proposals.filter((p) => p.status === "pending").length}
              </span>}
          </button>
        ))}
        <div className="grow" />
        <div className="stack gap8" style={{ padding: "12px 8px 0" }}>
          <Seg value={theme} onChange={setTheme}
               options={[{ value: "light", label: "روشن" },
                         { value: "dark", label: "تیره" },
                         { value: "system", label: "سیستم" }]} />
          <div className="fs11 faint ltr" style={{ textAlign: "center" }}>
            <span className="ltr">v{s.api_version ?? "1.0.0"}</span>
            {" · "}نسخه تنظیمات <span className="ltr">#{s.config_version}</span>
          </div>
        </div>
      </nav>

      <div className="main">
        <header className="topbar">
          <h1 style={{ fontSize: 17 }}>{NAV.find((n) => n.id === page)?.label}</h1>
          <div className="row gap8 wrap" style={{ marginInlineStart: "auto" }}>
            <Chip tone={s.halted || s.kill_switch.engaged ? "neg" : "pos"}>
              <i className="dot" />
              {s.halted ? "متوقف شده" : s.kill_switch.engaged ? "توقف اضطراری" : "در حال کار"}
            </Chip>
            <Chip tone="solid">{MODE_FA[s.mode]}</Chip>
            <Chip tone={s.venue_mode === "live" ? "neg" : "info"}>{VENUE_FA[s.venue_mode]}</Chip>
            <span className="row gap6">
              <span className="fs11 muted nowrap">
                پول حساب
                <Hint text={<>پول ته حساب به‌علاوه سود یا زیان معامله‌های بازی که هنوز بسته
                  نشده‌اند. اگر معامله باز داشته باشید این عدد لحظه‌به‌لحظه تکان می‌خورد.</>} />
              </span>
              <span className="num fs13" style={{ fontWeight: 600 }}>
                {Number(s.account.equity ?? 0).toLocaleString("en-US", { minimumFractionDigits: 2 })}
              </span>
            </span>
            <button className="btn ghost sm" onClick={refresh}>تازه‌سازی</button>
            {s.kill_switch.engaged ? (
              /* The API has always accepted a release (owner + second factor);
                 the console had no button for it, so an owner whose robot the
                 watchdog had stopped was told "release it from the dashboard"
                 and found nothing to press. */
              <button className="btn sm" disabled={readOnly || !canAdminister}
                      title={!canAdminister ? "فقط مالک حساب" : undefined}
                      onClick={() => setConfirm({
                        action: "برداشتن توقف اضطراری",
                        description: <>ربات از چرخهٔ بعد دوباره اجازه دارد معاملهٔ تازه باز کند
                          (در حالت «فقط پیشنهاد» فقط پیشنهاد می‌دهد). اول مطمئن شوید علت توقف —
                          که بالای صفحه نوشته شده — برطرف شده است. این کار در دفتر رویدادها ثبت
                          می‌شود.</>,
                        path: "/api/control/kill/release", body: {},
                      })}>برداشتن توقف اضطراری</button>
            ) : (
            <button className="btn danger sm" disabled={readOnly}
                    onClick={() => setConfirm({
                      action: "فعال کردن کلید توقف اضطراری",
                      description: <>ربات از همین لحظه هیچ معامله تازه‌ای باز نمی‌کند.
                        معامله‌های بازی که همین حالا وجود دارند بسته نمی‌شوند؛ حد ضرری که نزد
                        بروکر ثبت شده همچنان از آن‌ها محافظت می‌کند. روشن کردن دوباره فقط با
                        دست خودتان ممکن است — هیچ بخشی از سامانه خودش این کلید را آزاد نمی‌کند.</>,
                      path: "/api/control/kill",
                      body: { reason: "engaged from the console" }, danger: true,
                    })}>توقف فوری همه‌چیز</button>
            )}
          </div>
        </header>

        <main className="content">
          {demo && (
            <div style={{ marginBottom: 16 }}>
              <Banner tone="warn" icon="◈">
                <strong>این فقط یک نمایش است.</strong> این صفحه به هیچ حساب واقعی وصل نیست و
                همه عددها ساختگی‌اند. هیچ دکمه‌ای اینجا چیزی را واقعاً تغییر نمی‌دهد. عددها
                عمداً ناخوشایند انتخاب شده‌اند: نمره عملکرد زیر حد قابل قبول، یک افت واقعی در
                حساب، و حکم نهایی «<em>رد شد</em>» — چون نمایشی که نمودار همیشه‌صعودی و ۹۰٪ برد
                نشان بدهد، چیز غلطی یاد می‌دهد. معنی هر واژه در صفحه «واژه‌نامه ساده» هست.
              </Banner>
            </div>
          )}
          {s.kill_switch.engaged && page !== "overview" && (
            <div style={{ marginBottom: 16 }}>
              <Banner tone="neg" icon="■">
                <strong>توقف اضطراری روشن است؛ معاملهٔ تازه باز نمی‌شود.</strong>{" "}
                دلیل: <span className="ltr mono fs12">{s.kill_switch.reason || "—"}</span>
                {s.kill_switch.engaged_by ? <> (توسط <span className="ltr">{s.kill_switch.engaged_by}</span>)</> : null}.
                {" "}معامله‌های باز حد ضررشان را نزد بروکر دارند. این توقف خودکار برداشته نمی‌شود؛
                وقتی علتش برطرف شد، مالک با دکمهٔ «برداشتن توقف اضطراری» (بالای صفحه) آن را
                برمی‌دارد.
                {(s.kill_switch.reason || "").includes("heartbeat") && <> «no heartbeat» یعنی موتور
                  ربات مدتی جواب نداده بود؛ اگر روی ویندوز نسخهٔ ۱٫۷٫۰ یا ۱٫۸٫۰ نصب بوده، علتش
                  ایرادی بود که در ۱٫۸٫۱ برطرف شده است.</>}
              </Banner>
            </div>
          )}
          {s.entries_permitted && !s.entries_permitted.allowed && (
            <div style={{ marginBottom: 16 }}>
              <Banner tone="neg" icon="⬚">
                <strong>معامله تازه با پول واقعی متوقف است.</strong>{" "}
                {s.entries_permitted.reason} معامله‌های باز همچنان مدیریت و محافظت می‌شوند.
              </Banner>
            </div>
          )}
          {s.terminal && s.terminal.applicable && s.terminal.state !== "ok"
            && s.terminal.state !== "unknown" && (
            <div style={{ marginBottom: 16 }}>
              <Banner tone="neg" icon="⇄">
                <strong>متاتریدر: {s.terminal.state_fa}.</strong> نگهبان در حال برگرداندن اتصال
                است؛ تا آن موقع معاملهٔ تازه باز نمی‌شود. جزئیات در «بروکر و اتصال».
              </Banner>
            </div>
          )}
          {s.cooldowns && Object.keys(s.cooldowns).length > 0 && (
            <div style={{ marginBottom: 16 }}>
              <Banner tone="warn" icon="😮‍💨">
                <strong>استراحت اجباری:</strong>{" "}
                {Object.keys(s.cooldowns).map((k) => k === "*" ? "کل حساب" : k).join("، ")} — بعد
                از چند ضرر پشت سر هم، تا پایان استراحت معامله تازه باز نمی‌شود. جزئیات در صفحه
                «مغز ربات».
              </Banner>
            </div>
          )}
          {s.guard_suspended && Object.keys(s.guard_suspended).length > 0 && (
            <div style={{ marginBottom: 16 }}>
              <Banner tone="warn" icon="⏸">
                <strong>نگهبان عملکرد این استراتژی‌ها را معلق کرده است:</strong>{" "}
                {Object.keys(s.guard_suspended).join("، ")} — به‌خاطر ضرر آماری مداوم، تا
                وقتی شما در صفحه «تصمیم‌های ربات» آزادشان نکنید معامله تازه باز نمی‌کنند.
              </Banner>
            </div>
          )}
          {error && (
            <div style={{ marginBottom: 16 }}>
              <Banner tone="neg" icon="✕">
                داده‌ها به‌روز نشدند (اعدادی که می‌بینید ممکن است قدیمی باشند): {error}
              </Banner>
            </div>
          )}

          {page === "overview" && <Overview snap={snap} />}
          {page === "positions" && <Positions snap={snap} write={write} />}
          {page === "manual" && <ManualTrade snap={snap} provider={provider} write={write}
                                             readOnly={readOnly} />}
          {page === "ai" && <AIPage provider={provider} write={write} readOnly={readOnly}
                                    canAdminister={canAdminister} />}
          {page === "reference" && <ReferencePage provider={provider} write={write}
                                                  readOnly={readOnly}
                                                  canAdminister={canAdminister} />}
          {page === "brain" && <BrainPage provider={provider} write={write} readOnly={readOnly}
                                          canAdminister={canAdminister} />}
          {page === "macro" && <MacroPage provider={provider} write={write} readOnly={readOnly}
                                          canAdminister={canAdminister} />}
          {page === "notify" && <NotifyPage provider={provider} write={write} readOnly={readOnly}
                                            canAdminister={canAdminister} />}
          {page === "journal" && <Journal snap={snap} />}
          {page === "risk" && <Risk snap={snap} />}
          {page === "research" && <Research snap={snap} />}
          {page === "agent" && <AgentPage snap={snap} write={write} />}
          {page === "settings" && <Settings snap={snap} write={write} readOnly={readOnly} />}
          {page === "brokers" && <Brokers provider={provider} write={write}
                                          readOnly={readOnly} canAdminister={canAdminister} />}
          {page === "users" && <Users provider={provider} write={write} me={me}
                                      readOnly={readOnly} canAdminister={canAdminister} />}
          {page === "licence" && <Licence provider={provider} write={write}
                                          readOnly={readOnly} canAdminister={canAdminister} />}
          {page === "audit" && <Audit snap={snap} />}
          {page === "glossary" && <Glossary />}
        </main>
      </div>

      <ConfirmWrite open={!!confirm} action={confirm?.action ?? ""}
                    description={confirm?.description} danger={confirm?.danger}
                    onClose={() => setConfirm(null)}
                    onConfirm={async (totp) =>
                      confirm ? write(confirm.path, confirm.body, totp)
                              : { ok: false, detail: "" }} />
    </div>
  );
}

const MODE_FA: Record<string, string> = {
  observe: "فقط تماشا", advisory: "فقط پیشنهاد",
  semi_auto: "نیمه‌خودکار", autonomous: "کاملاً خودکار",
};
const VENUE_FA: Record<string, string> = {
  paper: "تمرینی — شبیه‌ساز", demo: "تمرینی — حساب دمو", live: "پول واقعی",
};

function Login({ onDone }: {
  onDone: (token: string, role: string, username: string) => void;
}) {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="row" style={{ height: "100vh", justifyContent: "center", padding: 20 }}>
      <Card style={{ width: "min(400px, 100%)" }}>
        <div className="stack gap16">
          <div>
            <div className="brand-name" style={{ fontSize: 18 }}>Sentinel-FX</div>
            <div className="brand-sub ltr">AUTONOMOUS TRADING CONSOLE</div>
          </div>
          <Banner tone="info" icon="🔐">
            ورود فقط اجازه <strong>دیدن</strong> می‌دهد. برای هر تغییری — بستن یک معامله،
            عوض کردن یک تنظیم — جداگانه یک کد شش‌رقمی تازه از برنامه احراز هویت خواسته می‌شود.
          </Banner>
          <div className="field">
            <span className="field-label">نام کاربری</span>
            <input className="input" value={u} autoComplete="username"
                   onChange={(e) => setU(e.target.value)} />
          </div>
          <div className="field">
            <span className="field-label">گذرواژه</span>
            <input className="input" type="password" value={p} autoComplete="current-password"
                   onChange={(e) => setP(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && !busy && submit()} />
          </div>
          {err && <Banner tone="neg" icon="✕">{err}</Banner>}
          <button className="btn" disabled={busy || !u || !p} onClick={submit}>
            {busy ? "در حال ورود…" : "ورود"}
          </button>
        </div>
      </Card>
    </div>
  );

  async function submit() {
    setBusy(true); setErr(null);
    try {
      const r = await login(API_BASE, u, p);
      onDone(r.token, r.role, u);
    } catch (e) {
      setErr("نام کاربری یا گذرواژه نادرست است، یا حساب موقتاً قفل شده است.");
    } finally {
      setBusy(false);
    }
  }
}
