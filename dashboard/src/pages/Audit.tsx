import React, { useMemo, useState } from "react";
import { BarsH } from "../components/charts";
import { Banner, Card, Chip, Empty, KV, Tile, ago, dt } from "../components/ui";
import type { Snapshot } from "../types";

/* Each event said as something that happened, not as a subsystem name. */
const EVENT_FA: Record<string, string> = {
  "system.start": "سامانه روشن شد", "system.stop": "سامانه خاموش شد",
  "system.heartbeat": "علامت زنده بودن", "system.clock_anomaly": "ساعت سامانه مشکوک شد",
  "config.change": "یک تنظیم عوض شد", "config.mode_change": "حالت کار ربات عوض شد",
  "data.stale": "قیمت‌ها کهنه شده بودند", "data.gap": "بخشی از داده بازار نرسید",
  "decision.signal": "یک فرصت معاملاتی دیده شد", "decision.proposal": "یک معامله پیشنهاد شد",
  "decision.proposal_accepted": "پیشنهاد تأیید شد",
  "decision.proposal_rejected": "پیشنهاد رد شد",
  "decision.risk_veto": "قواعد ایمنی جلوی یک معامله را گرفت",
  "order.intent": "تصمیم به ارسال سفارش", "order.sent": "سفارش به بروکر فرستاده شد",
  "order.ack": "بروکر دریافت سفارش را تأیید کرد",
  "order.filled": "سفارش اجرا شد", "order.rejected": "بروکر سفارش را رد کرد",
  "order.unknown": "وضعیت یک سفارش نامعلوم ماند",
  "order.duplicate_blocked": "جلوی ارسال دوباره یک سفارش گرفته شد",
  "position.open": "یک معامله باز شد", "position.modify": "یک معامله تغییر کرد",
  "position.close": "یک معامله بسته شد",
  "risk.limit_breach": "یکی از سقف‌های ایمنی رد شد",
  "risk.ladder_step": "ریسک یک پله کم شد",
  "risk.halt": "ربات متوقف شد", "ops.kill_switch": "کلید توقف اضطراری",
  "ops.deadman": "نگهبان خودکار وارد عمل شد",
  "ops.reconcile": "حساب ما با حساب بروکر مقایسه شد",
  "ops.reconcile_mismatch": "حساب ما با حساب بروکر نخواند",
  "ops.connectivity": "تغییر در وضعیت اتصال",
  "learn.postmortem": "تحلیل یک معامله بسته‌شده", "learn.lesson": "یک درس تازه ثبت شد",
  "learn.param_proposal": "پیشنهاد تغییر یک تنظیم",
  "research.run": "یک اجرای پژوهشی", "research.verdict": "حکم نهایی یک آزمون",
  "sec.auth_ok": "ورود موفق", "sec.auth_fail": "تلاش ناموفق برای ورود",
  "sec.write_action": "یک تغییر انجام شد", "sec.write_denied": "یک تلاش برای تغییر رد شد",
};

const CRITICAL = new Set([
  "risk.halt", "ops.kill_switch", "ops.deadman", "ops.reconcile_mismatch",
  "order.unknown", "sec.auth_fail", "sec.write_denied", "order.duplicate_blocked",
]);

export default function Audit({ snap }: { snap: Snapshot }) {
  const { audit } = snap;
  const [filter, setFilter] = useState("");

  const counts = useMemo(() => {
    const m = new Map<string, number>();
    audit.forEach((r) => m.set(r.event, (m.get(r.event) ?? 0) + 1));
    return [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10)
      .map(([k, v]) => ({ label: EVENT_FA[k] ?? k, value: v }));
  }, [audit]);

  const shown = useMemo(
    () => audit.filter((r) => !filter || r.event.includes(filter)
      || (EVENT_FA[r.event] ?? "").includes(filter) || r.actor.includes(filter)),
    [audit, filter]);

  return (
    <div className="stack gap16">
      <Banner tone="info" icon="🔗">
        <strong>این دفتر قابل دستکاری نیست.</strong> هر خط، یک اثرانگشت از خط قبلی خودش را
        در دل دارد. اگر کسی — یا یک خطای نرم‌افزاری — یک خط را عوض یا حذف کند، این رشته
        پاره می‌شود و دقیقاً معلوم می‌شود کجا. دفتری که بشود بی‌سروصدا ویرایشش کرد، نمی‌تواند
        به این سؤال جواب بدهد: «سامانه چه می‌دانست و چه کرد؟»
      </Banner>

      <div className="grid g4">
        <Card><Tile label="چند رویداد ثبت شده" value={audit.length.toLocaleString("en-US")} /></Card>
        <Card><Tile label="آیا دفتر دست‌نخورده است" value="بله، دست‌نخورده" tone="pos"
                    hint={<>اثرانگشت همه رکوردها از نو محاسبه و با آنچه ذخیره شده مقایسه
                      شد. اگر حتی یک حرف در یک خط عوض شده بود، اینجا نوشته می‌شد که کجا.</>}
                    note="اثرانگشت همه خط‌ها دوباره محاسبه و مطابقت داده شد" /></Card>
        <Card><Tile label="چند رویداد جدی رخ داده"
                    hint={<>رویداد جدی یعنی: توقف ربات، کلید اضطراری، نخواندن حساب ما با
                      حساب بروکر، یا سفارشی که وضعیتش معلوم نشد.</>}
                    value={audit.filter((r) => CRITICAL.has(r.event)).length}
                    note="توقف، توقف اضطراری، مغایرت حساب، سفارش بلاتکلیف" /></Card>
        <Card><Tile label="چند کار را یک آدم انجام داده"
                    hint="هر تغییری که یک انسان انجام می‌دهد، با نام کاربری‌اش ثبت می‌شود."
                    value={audit.filter((r) => r.actor !== "system").length}
                    note="بقیه رویدادها را خود سامانه ثبت کرده است" /></Card>
      </div>

      <div className="grid g-1-2">
        <Card title="بیشتر چه چیزهایی ثبت شده"
              hint="عدد کنار هر خط می‌گوید آن نوع رویداد چند بار در این دفتر آمده است.">
          <BarsH data={counts} valueFmt={(v) => String(Math.round(v))} />
        </Card>
        <Card title="فهرست کامل رویدادها"
              actions={
                <input className="input" placeholder="جست‌وجو در رویدادها…" style={{ width: 200 }}
                       value={filter} onChange={(e) => setFilter(e.target.value)} />
              }>
          {shown.length === 0 ? <Empty>هیچ رویدادی با این عبارت پیدا نشد</Empty> : (
            <div className="table-wrap" style={{ maxHeight: 520, overflowY: "auto" }}>
              <table className="t">
                <thead>
                  <tr><th className="n">ردیف</th><th>چه اتفاقی افتاد</th>
                      <th>چه کسی انجامش داد</th>
                      <th>زمان</th><th>اثرانگشت</th></tr>
                </thead>
                <tbody>
                  {shown.map((r) => (
                    <tr key={r.seq}>
                      <td className="n faint">{r.seq}</td>
                      <td>
                        <span className="row gap6">
                          {CRITICAL.has(r.event) && <span className="dot" style={{ background: "var(--ember)" }} />}
                          <span className="fs12">{EVENT_FA[r.event] ?? r.event}</span>
                        </span>
                        <div className="mono fs11 faint">{r.event}</div>
                      </td>
                      <td className="fs12">
                        {r.actor === "system" ? <span className="muted">سامانه</span>
                          : <Chip tone="info">{r.actor}</Chip>}
                      </td>
                      <td className="fs11 muted nowrap">{dt(r.ts_ns)}<div className="faint">{ago(r.ts_ns)}</div></td>
                      <td className="mono fs11 faint">{r.hash.slice(0, 12)}…</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
