import React, { useMemo, useState } from "react";
import { BarsH, StackedRatio } from "../components/charts";
import {
  Banner, Card, Chip, ConfirmWrite, Disclosure, Empty, Hint, KV, Seg, ago, dt,
} from "../components/ui";
import type { Snapshot } from "../types";
import { VETO_FA } from "./Overview";

export default function AgentPage({ snap, write }: {
  snap: Snapshot;
  write: (path: string, body: unknown, totp: string) => Promise<{ ok: boolean; detail: string }>;
}) {
  const { decisions, lessons, proposals } = snap;
  const [filter, setFilter] = useState<"all" | "executed" | "vetoed" | "queued">("all");
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown;
  }>(null);

  const shown = useMemo(
    () => decisions.filter((d) => filter === "all" || d.action === filter).slice(0, 60),
    [decisions, filter]);

  const vetoMix = useMemo(() => {
    const m = new Map<string, number>();
    decisions.forEach((d) => d.vetoes.forEach((v) => m.set(v.rule, (m.get(v.rule) ?? 0) + 1)));
    return [...m.entries()].sort((a, b) => b[1] - a[1])
      .map(([k, v]) => ({ label: VETO_FA[k] ?? k, value: v }));
  }, [decisions]);

  return (
    <div className="stack gap16">
      <div className="grid g-2-1">
        <Card title="ربات چه دید و چه کرد"
              hint={<>هر کارت یک فرصت است که ربات پیدا کرده، همراه با دلیل کامل تصمیمش.
                فرصت‌هایی که جلویشان گرفته شده هم اینجا هستند، نه فقط معامله‌های انجام‌شده.</>}
              sub="هر تصمیم — چه انجام شده و چه نشده — با دلیل کاملش ثبت می‌شود"
              actions={
                <Seg value={filter} onChange={setFilter}
                     options={[{ value: "all", label: "همه" },
                               { value: "executed", label: "انجام‌شده" },
                               { value: "vetoed", label: "جلوگیری‌شده" },
                               { value: "queued", label: "منتظر تأیید" }]} />
              }>
          <div className="stack gap12 scroll-y" style={{ maxHeight: 620 }}>
            {shown.length === 0 && <Empty>هیچ تصمیمی با این فیلتر پیدا نشد</Empty>}
            {shown.map((d, i) => (
              <div key={i} className="stack gap6"
                   style={{ padding: 12, borderRadius: 14,
                            background: "var(--surface-alt)",
                            borderInlineStart: `3px solid ${
                              d.action === "executed" ? "var(--pos)"
                              : d.action === "vetoed" ? "var(--neg)" : "var(--warn)"}` }}>
                <div className="row gap6 wrap">
                  <Chip tone={d.action === "executed" ? "pos"
                              : d.action === "vetoed" ? "neg" : "warn"}>
                    {d.action === "executed" ? "انجام شد"
                      : d.action === "vetoed" ? "جلویش گرفته شد" : "منتظر تأیید"}
                  </Chip>
                  <span className="mono fs12" style={{ fontWeight: 600 }}>{d.instrument}</span>
                  {d.side && <Chip>{d.side === "BUY" ? "خرید" : "فروش"}</Chip>}
                  <span className="fs11 muted">{d.strategy}</span>
                  {d.regime && <Chip title="حال‌وهوای بازار در آن لحظه">{REGIME_FA[d.regime] ?? d.regime}</Chip>}
                  <span className="fs11 faint" style={{ marginInlineStart: "auto" }}>{ago(d.ts_ns)}</span>
                </div>
                <p className="fs12" style={{ lineHeight: 1.85 }}>{d.explanation}</p>
                {d.vetoes.length > 0 && (
                  <div className="row gap6 wrap">
                    {d.vetoes.map((v, j) => (
                      <Chip key={j} tone="neg" title={v.message}>
                        {VETO_FA[v.rule] ?? v.rule}
                        {v.observed ? ` · ${v.observed}` : ""}
                        {v.limit ? ` / ${v.limit}` : ""}
                      </Chip>
                    ))}
                  </div>
                )}
                <Disclosure summary="عددهایی که این تصمیم روی آن‌ها گرفته شد">
                  <div className="kv-grid c3">
                    {Object.entries(d.diagnostics).map(([k, v]) => (
                      <KV key={k} k={DIAG_FA[k] ?? k}
                          v={typeof v === "object" ? JSON.stringify(v) : String(v)} />
                    ))}
                  </div>
                </Disclosure>
              </div>
            ))}
          </div>
        </Card>

        <div className="stack gap16">
          <Card title="بیشتر به چه دلیلی جلوی معامله گرفته شده"
                hint={<>هر خط یک قاعده ایمنی است و عدد کنارش می‌گوید چند بار جلوی یک معامله
                  را گرفته. این فهرست، تصویر واقعی رفتار ربات است.</>}
                sub="هر خط یک قاعده ایمنی است، همراه با تعداد دفعاتی که فعال شده">
            <BarsH data={vetoMix} valueFmt={(v) => String(Math.round(v))} height={260} />
          </Card>
          <Card title="چیزهایی که ربات از گذشته یاد گرفته"
                hint={<>هر درس فقط دو کار می‌تواند بکند: در توضیح تصمیم‌ها ظاهر شود، و اندازه
                  معامله را کوچک‌تر کند. هیچ درسی نمی‌تواند هیچ سقف ایمنی را شل کند.</>}
                sub="یک درس فقط می‌تواند محتاط‌تر کند، هرگز نمی‌تواند جسورتر کند">
            {lessons.length === 0 ? (
              <Empty>هنوز هیچ الگویی به اندازه کافی تکرار نشده که بشود اسمش را درس گذاشت</Empty>
            ) : (
              <div className="stack gap12">
                {lessons.map((l) => (
                  <div key={l.id} className="stack gap4">
                    <div className="row gap6 wrap">
                      <Chip tone="info">{l.scope === "global" ? "سراسری"
                        : l.scope === "strategy" ? l.strategy : l.instrument ?? l.regime}</Chip>
                      <Chip title="این الگو در چند معامله دیده شده — هر چه بیشتر، قابل اتکاتر">
                        در {l.sample_size} معامله دیده شده
                      </Chip>
                      <Chip title="احتمال اینکه این الگو صرفاً تصادفی باشد؛ هر چه کمتر، بهتر">
                        احتمال تصادفی بودن {l.p_value.toFixed(4)}
                      </Chip>
                      <Chip tone="warn" title="در این شرایط، اندازه معامله را این‌قدر کوچک می‌کند">
                        اندازه معامله ×{(l.effective_caution ?? l.caution).toFixed(2)}
                      </Chip>
                      {l.age_days !== undefined && l.age_days > (l.half_life_days ?? 90) && (
                        <Chip title="شواهد تازه‌ای این درس را تأیید نکرده؛ اثرش رو به صفر می‌رود">
                          {l.age_days.toFixed(0)} روز بدون تأیید تازه
                        </Chip>
                      )}
                    </div>
                    <p className="fs12" style={{ lineHeight: 1.8 }}>{l.statement}</p>
                    <div className="meter"><i style={{ width: `${l.confidence * 100}%` }} /></div>
                    <span className="fs11 faint">
                      چقدر به این درس مطمئن است: {(l.confidence * 100).toFixed(0)}٪ · {ago(l.created_ns)}
                    </span>
                  </div>
                ))}
              </div>
            )}
            <Disclosure summary="چرا اجازه یادگیری عمداً محدود است">
              آنچه ربات یاد می‌گیرد فقط سه کار می‌تواند بکند: در توضیح یک تصمیم ظاهر شود،
              اندازه معامله را <strong>کوچک‌تر</strong> کند، و به بخش پیشنهاددهنده خوراک بدهد.
              همین و بس. دلیلش این است: سامانه‌ای که از موفقیت‌های اخیرش یاد بگیرد و قواعد
              ایمنی خودش را شل کند، دقیقاً درست قبل از بزرگ‌ترین ضررش، بیشترین اعتمادبه‌نفس
              را خواهد داشت — چون اعتمادبه‌نفسش را از همان دوره آرامی گرفته که تمام شده است.
            </Disclosure>
          </Card>
        </div>
      </div>

      <Card title="تغییرهایی که ربات پیشنهاد داده"
            hint={<>ربات می‌تواند پیشنهاد بدهد که یک تنظیم عوض شود، ولی خودش اجازه عوض کردنش
              را ندارد. مسیر: پیشنهاد ← تأیید شما ← یک اجرای آزمایشی ← اعمال.</>}
            sub="ربات فقط پیشنهاد می‌دهد؛ اجازه اعمال هیچ تغییری را ندارد">
        {proposals.length === 0 ? <Empty>هیچ پیشنهادی وجود ندارد</Empty> : (
          <div className="stack gap16">
            {proposals.map((p) => (
              <div key={p.id} className="stack gap8"
                   style={{ padding: 14, borderRadius: 14, background: "var(--surface-alt)" }}>
                <div className="row gap8 wrap">
                  <span className="mono fs12 faint">{p.id}</span>
                  <span className="mono fs13" style={{ fontWeight: 600 }}>{p.path}</span>
                  <Chip>
                    <span className="num">{String(p.current_value)}</span>
                    {" → "}
                    <span className="num">{String(p.proposed_value)}</span>
                  </Chip>
                  <Chip tone={STATUS_TONE[p.status]}>{PROPOSAL_FA[p.status] ?? p.status}</Chip>
                  <span className="fs11 faint" style={{ marginInlineStart: "auto" }}>{ago(p.created_ns)}</span>
                </div>
                <p className="fs12" style={{ lineHeight: 1.8 }}>{p.rationale}</p>
                <div className="kv-grid c4">
                  <KV k="بر پایه چند معامله" v={p.sample_size}
                      hint="هر چه این عدد بزرگ‌تر باشد، پیشنهاد قابل اتکاتر است." />
                  <KV k="انتظار می‌رود چقدر بهتر کند"
                      hint={<>بر حسب چند برابر مبلغ ریسک‌شده در هر معامله. ‎+۰٫۴۳R یعنی هر
                        معامله به‌طور میانگین ۴۳٪ مبلغ ریسک‌شده بهتر شود — البته اگر این
                        تخمین درست باشد.</>}
                      v={`${p.expected_effect_r >= 0 ? "+" : ""}${p.expected_effect_r.toFixed(2)}R`} />
                  <KV k="بازه‌ای که اثر واقعی احتمالاً در آن است"
                      hint={<>با ۹۵٪ اطمینان، اثر واقعی جایی بین این دو عدد است. اگر این بازه
                        شامل صفر باشد، یعنی ممکن است اصلاً اثری در کار نباشد.</>}
                      v={`[${p.effect_ci_low.toFixed(2)}, ${p.effect_ci_high.toFixed(2)}]`} />
                  <KV k="احتمال اینکه این اثر فقط تصادفی باشد" v={p.p_value.toFixed(5)}
                      hint="هر چه کمتر، احتمال تصادفی بودن کمتر. حد قبولی در این سامانه ۰٫۰۱ است." />
                </div>
                {p.validation_run_id && (
                  <KV k="شماره اجرای آزمایشی" v={p.validation_run_id} />
                )}
                {p.status === "pending" && (
                  <div className="row gap8">
                    <button className="btn sm" onClick={() => setConfirm({
                      action: `تأیید پیشنهاد ${p.id}`,
                      description: <>با این تأیید، هیچ چیزی همین حالا عوض نمی‌شود. پیشنهاد
                        فقط وارد صف <strong>آزمایش</strong> می‌شود. اگر از آزمایش سربلند
                        بیرون بیاید، ابتدا روی حساب تمرینی اعمال می‌شود. سقف‌های ایمنی از این
                        مسیر اصلاً قابل تغییر نیستند.</>,
                      path: "/api/proposals/review",
                      body: { proposal_id: p.id, approve: true },
                    })}>بفرست برای آزمایش</button>
                    <button className="btn ghost sm" onClick={() => setConfirm({
                      action: `رد پیشنهاد ${p.id}`,
                      description: "پیشنهاد کنار گذاشته می‌شود. خود این تصمیم هم در دفتر ثبت رویدادها می‌ماند.",
                      path: "/api/proposals/review",
                      body: { proposal_id: p.id, approve: false },
                    })}>رد</button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
        <Banner tone="flat" icon="🔒">
          <strong>چیزهایی که ربات با هیچ درجه‌ای از اطمینان اجازه پیشنهاد تغییرشان را
          ندارد:</strong> سقف ضرر روزانه، هفتگی و ماهانه؛ افتی که ربات را کاملاً متوقف می‌کند؛
          جدول کم کردن ریسک هنگام افت؛ سقف تعداد معامله‌ها؛ الزام ثبت حد ضرر نزد بروکر؛ درصد
          ریسک هر معامله؛ حالت کار ربات؛ و حد قبولی آزمون‌های آماری.
        </Banner>
      </Card>

      <ConfirmWrite open={!!confirm} action={confirm?.action ?? ""}
                    description={confirm?.description}
                    onClose={() => setConfirm(null)}
                    onConfirm={async (totp) =>
                      confirm ? write(confirm.path, confirm.body, totp)
                              : { ok: false, detail: "" }} />
    </div>
  );
}

const PROPOSAL_FA: Record<string, string> = {
  pending: "منتظر تصمیم شما", approved: "تأیید شده، در صف آزمایش",
  rejected: "رد شده", validated: "از آزمایش سربلند بیرون آمد",
  applied: "اعمال شده", expired: "کهنه شد و منقضی گشت",
};
const REGIME_FA: Record<string, string> = {
  trending: "بازار جهت‌دار", quiet_range: "بازار آرام",
  volatile_range: "بازار پرنوسان", stress: "بازار بحرانی",
};
const STATUS_TONE: Record<string, any> = {
  pending: "warn", approved: "info", validated: "pos", applied: "pos",
  rejected: "neg", expired: undefined,
};
/* Diagnostic keys, said in words instead of trading shorthand. */
const DIAG_FA: Record<string, string> = {
  spread_pips: "اختلاف قیمت خرید و فروش (پیپ)",
  stop_pips: "فاصله تا حد ضرر (پیپ)",
  reward_risk: "سود احتمالی چند برابر ریسک است",
  round_trip_cost_pips: "هزینه باز و بسته کردن (پیپ)",
  break_even_win_rate: "چند درصد باید برنده باشیم تا سر به سر شویم",
  drawdown_pct: "افت حساب از رکوردش (٪)",
  risk_multiplier: "اندازه معامله نسبت به حالت عادی",
  gross_leverage: "بزرگی معامله‌ها نسبت به حساب",
  max_currency_exposure: "بیشترین شرط‌بندی روی یک ارز",
  clusters: "گروه‌های جفت‌ارزی که شبیه هم حرکت می‌کنند",
  total_risk_pct_incl_pending: "جمع ریسک باز، با احتساب معامله‌های در صف (٪)",
  projected_annual_cost_pct: "هزینه پیش‌بینی‌شده یک سال (٪ حساب)",
  projected_trades_per_year: "تعداد معامله پیش‌بینی‌شده در یک سال",
};
