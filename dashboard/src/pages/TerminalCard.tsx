import React, { useCallback, useEffect, useState } from "react";
import type { Provider } from "../api";
import {
  Banner, Card, Chip, ConfirmWrite, Field, KV, NumberField, Switch, ago, fa,
} from "../components/ui";
import type { TerminalView } from "../types";

/* The MetaTrader watchdog, on the broker page where a person looks when the
   connection is the question. */

type Write = (path: string, body: unknown, totp: string) =>
  Promise<{ ok: boolean; detail: string }>;

const ACTION_FA: Record<string, string> = {
  reconnected: "دوباره وصل شد", recovered: "خودش برگشت", reconnect_failed: "تلاش ناموفق",
  killed: "برنامهٔ قفل‌شده بسته و دوباره باز شد", kill_skipped: "بستن انجام نشد",
};

export default function TerminalCard({ provider, write, disabled }: {
  provider: Provider; write: Write; disabled: boolean;
}) {
  const [view, setView] = useState<TerminalView | null>(null);
  const [confirm, setConfirm] = useState<null | { body: unknown }>(null);

  const load = useCallback(async () => {
    try {
      setView(await provider.get<TerminalView>("/api/terminal"));
    } catch {
      setView(null);
    }
  }, [provider]);

  useEffect(() => {
    load();
    const t = setInterval(load, 20000);
    return () => clearInterval(t);
  }, [load]);

  if (!view) return null;
  if (!view.available) {
    return (
      <Card title="نگهبان متاتریدر">
        <p className="fs12 muted">این اتصال متاتریدر نیست (یا شبیه‌ساز است)؛ نگهبان لازم نیست.</p>
      </Card>
    );
  }
  const ok = view.state === "ok";
  const cfg = view.config!;
  return (
    <Card title="نگهبان متاتریدر"
          hint="اگر متاتریدر بسته شود، قفل کند، از سرور بروکر قطع شود یا از حساب خارج شود، نگهبان خودش آن را باز می‌کند و دوباره وارد می‌شود. هیچ‌وقت به معامله‌ها دست نمی‌زند."
          actions={<Chip tone={ok ? "pos" : "neg"}>{view.state_fa ?? view.state}</Chip>}>
      <div className="stack gap12">
        {!ok && (
          <Banner tone="neg" icon="!">
            {view.state_fa}: <span className="ltr mono fs12">{view.detail}</span>
            {view.down_since_ns ? <> — از {ago(view.down_since_ns)}</> : null}
            {view.next_attempt_ns ? <> — تلاش بعدی تا {fa(Math.max(0, Math.round(
              (view.next_attempt_ns / 1e6 - Date.now()) / 1000)))} ثانیهٔ دیگر</> : null}
            <br />معامله‌های باز حد ضررشان را نزد بروکر دارند؛ تا برگشتن اتصال معاملهٔ تازه باز نمی‌شود.
          </Banner>
        )}
        {view.algo_trading === false && (
          <Banner tone="warn" icon="⚠">دکمهٔ «Algo Trading» در متاتریدر خاموش است؛ ربات نمی‌تواند
            سفارش بفرستد. در نوار بالای متاتریدر روشنش کنید.</Banner>
        )}
        <div className="kv-grid c3">
          <KV k="تأخیر تا سرور بروکر" v={view.ping_ms ? <span className="num">{view.ping_ms} ms</span> : "—"} />
          <KV k="دفعات برگرداندن اتصال" v={fa(view.recoveries ?? 0)} />
          <KV k="آخرین اقدام" v={view.last_action
            ? <>{ACTION_FA[view.last_action] ?? view.last_action} ({ago(view.last_action_ns ?? 0)})</>
            : "هنوز لازم نشده"} />
          <KV k="ورود خودکار به حساب" v={view.can_sign_in
            ? "بله (با رمز ذخیره‌شده)" : "خیر (به همان حسابی وصل می‌شود که متاتریدر باز می‌کند)"} />
          <KV k="آخرین بررسی" v={view.last_check_ns ? ago(view.last_check_ns) : "—"} />
        </div>
        <Settings cfg={cfg} disabled={disabled} onSave={(patch) => setConfirm({ body: { patch } })} />
      </div>
      <ConfirmWrite open={!!confirm} action="ذخیرهٔ تنظیمات نگهبان متاتریدر"
                    description="از چرخهٔ بعدی ربات اعمال می‌شود. نگهبان هیچ‌وقت معامله‌ای باز، تغییر یا بسته نمی‌کند."
                    onClose={() => setConfirm(null)}
                    onConfirm={async (totp) => {
                      if (!confirm) return { ok: false, detail: "" };
                      const res = await write("/api/terminal/settings", confirm.body, totp);
                      if (res.ok) await load();
                      return res;
                    }} />
    </Card>
  );
}

function Settings({ cfg, disabled, onSave }: {
  cfg: NonNullable<TerminalView["config"]>; disabled: boolean;
  onSave: (patch: Record<string, unknown>) => void;
}) {
  const [c, setC] = useState(cfg);
  return (
    <details>
      <summary className="fs12 muted" style={{ cursor: "pointer" }}>تنظیمات نگهبان</summary>
      <div className="stack gap12" style={{ marginTop: 8 }}>
        <Field label="نگهبان روشن باشد">
          <Switch checked={c.enabled} disabled={disabled} label="نگهبان روشن باشد"
                  onChange={(v) => setC({ ...c, enabled: v })} />
        </Field>
        <Field label="چند بررسی ناموفق پشت سر هم تا اقدام" help="یک خطای تکی معمولاً گذراست. پیش‌فرض ۲.">
          <NumberField value={c.grace_checks} min={1} max={20} step={1} disabled={disabled}
                       onChange={(v) => setC({ ...c, grace_checks: v })} />
        </Field>
        <Field label="بستن متاتریدرِ قفل‌شده بعد از چند تلاش ناموفق"
               help="فقط همان برنامه‌ای که مسیرش را داده‌اید، و فقط روی همین ویندوز. ۰ یعنی هرگز.">
          <NumberField value={c.kill_hung_after_failures} min={0} max={50} step={1}
                       disabled={disabled}
                       onChange={(v) => setC({ ...c, kill_hung_after_failures: v })} />
        </Field>
        <Field label="برگرداندن به حساب ربات اگر کسی حساب را عوض کرد"
               help="فقط وقتی رمز در ربات ذخیره است. در غیر این صورت فقط هشدار می‌دهد.">
          <Switch checked={c.restore_account} disabled={disabled}
                  label="برگرداندن به حساب ربات"
                  onChange={(v) => setC({ ...c, restore_account: v })} />
        </Field>
        <div><button className="btn sm" disabled={disabled} onClick={() => onSave(c)}>ذخیره</button></div>
      </div>
    </details>
  );
}
