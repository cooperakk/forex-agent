import React, { useState } from "react";
import {
  Banner, Card, Chip, ConfirmWrite, Empty, Hint, KV, ago, dt, money,
} from "../components/ui";
import type { Decision, Snapshot } from "../types";

export default function Positions({ snap, write }: {
  snap: Snapshot;
  write: (path: string, body: unknown, totp: string) => Promise<{ ok: boolean; detail: string }>;
}) {
  const { positions, advice, status } = snap;
  const [confirm, setConfirm] = useState<null | {
    action: string; description: React.ReactNode; path: string; body: unknown; danger?: boolean;
  }>(null);

  return (
    <div className="stack gap16">
      <Card title="معامله‌های باز"
            hint={<>معامله‌هایی که هنوز بسته نشده‌اند. سود و زیانشان تا لحظه بسته شدن قطعی
              نیست و می‌تواند برعکس شود.</>}
            sub="سود و زیان با همان قیمتی حساب شده که واقعاً می‌شود با آن بست، نه با قیمت خوش‌بینانه وسط بازار"
            actions={
              positions.length > 0 && (
                <button className="btn danger sm" onClick={() => setConfirm({
                  action: "بستن همه معامله‌های باز",
                  description: <>هر {positions.length} معامله باز، با قیمت لحظه‌ای بازار
                    بسته می‌شود و سود و زیان شناورشان همان لحظه قطعی می‌شود. بستن معامله
                    هیچ‌وقت توسط قواعد ایمنی مسدود نمی‌شود — این عمدی است: کنترلی که بتواند
                    شما را داخل یک معامله حبس کند، کنترل خطر نیست.</>,
                  path: "/api/control/flatten", body: {}, danger: true,
                })}>بستن همه معامله‌ها</button>
              )
            }>
        {positions.length === 0 ? (
          <Empty>هیچ معامله بازی وجود ندارد</Empty>
        ) : (
          <div className="table-wrap">
            <table className="t">
              <thead>
                <tr>
                  <th>جفت‌ارز</th>
                  <th>خرید یا فروش</th>
                  <th className="n">
                    حجم
                    <Hint text={<>اندازه معامله بر حسب لات. ۰٫۱۰ لات یعنی هر پیپ حرکت قیمت،
                      حدود ۱ دلار سود یا زیان است.</>} />
                  </th>
                  <th className="n">قیمت ورود</th>
                  <th className="n">قیمت فعلی</th>
                  <th className="n">
                    حد ضرر
                    <Hint text={<>قیمتی که اگر بازار خلاف ما برود، معامله را خودکار می‌بندد تا
                      ضرر بزرگ‌تر نشود. تیک سبز یعنی نزد بروکر ثبت شده است.</>} />
                  </th>
                  <th className="n">
                    قیمت هدف
                    <Hint text="قیمتی که اگر بازار به نفع ما برود، معامله را با سود می‌بندد." />
                  </th>
                  <th className="n">
                    چند برابر ریسک
                    <Hint text={<>نتیجه معامله نسبت به مبلغی که رویش ریسک شده بود. ‎+۱ یعنی به
                      اندازه همان مبلغ سود، ‎−۱ یعنی به اندازه همان مبلغ ضرر.</>} />
                  </th>
                  <th className="n">
                    سود/زیان تا این لحظه
                    <Hint text="هنوز قطعی نیست؛ تا وقتی معامله باز است این عدد بالا و پایین می‌رود." />
                  </th>
                  <th>استراتژی</th><th>چند وقت است باز است</th><th />
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => {
                  const r = Number(p.r_multiple ?? 0);
                  const u = Number(p.unrealised ?? 0);
                  return (
                    <tr key={p.instrument}>
                      <td className="mono" style={{ fontWeight: 600 }}>{p.instrument}</td>
                      <td><Chip tone={p.side === "BUY" ? "pos" : "neg"}>
                        {p.side === "BUY" ? "خرید" : "فروش"}</Chip></td>
                      <td className="n">{p.lots}</td>
                      <td className="n">{p.entry_price}</td>
                      <td className="n">{p.current_price ?? "—"}</td>
                      <td className="n">
                        {p.stop_loss ?? "—"}
                        {p.broker_stop_confirmed
                          ? <span title="نزد بروکر ثبت شده: با قطع اینترنت هم کار می‌کند" className="pos"> ✓</span>
                          : <span title="فقط در برنامه ما ثبت شده: اگر ارتباط قطع شود، این معامله بی‌محافظ است" className="neg"> ⚠</span>}
                      </td>
                      <td className="n">{p.take_profit ?? "—"}</td>
                      <td className={`n ${r >= 0 ? "pos" : "neg"}`}>{r >= 0 ? "+" : ""}{r.toFixed(2)}</td>
                      <td className={`n ${u >= 0 ? "pos" : "neg"}`}>{u >= 0 ? "+" : ""}{money(u)}</td>
                      <td className="fs12 muted">{p.strategy}</td>
                      <td className="fs12 muted nowrap">{ago(p.opened_ns)}</td>
                      <td>
                        <button className="btn outline sm" onClick={() => setConfirm({
                          action: `بستن معامله ${p.instrument}`,
                          description: <>معامله {p.side === "BUY" ? "خرید" : "فروش"} به اندازه{" "}
                            {p.lots} لات روی <span className="mono">{p.instrument}</span> با قیمت
                            لحظه‌ای بازار بسته می‌شود. سود/زیان فعلی آن{" "}
                            <span className="num">{money(u)}</span> دلار است و با بستن، همین
                            مبلغ قطعی می‌شود (قیمت تا لحظه اجرا کمی تکان می‌خورد).</>,
                          path: "/api/control/close", body: { instrument: p.instrument },
                        })}>بستن</button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {positions.some((p) => !p.broker_stop_confirmed) && (
          <Banner tone="neg" icon="⚠">
            حد ضرر یک یا چند معامله فقط در برنامه ما ثبت شده، نه نزد بروکر. یعنی اگر همین حالا
            برق یا اینترنت قطع شود، آن معامله‌ها هیچ محافظی ندارند — و قطع شدن ارتباط با یک
            بروکر خارجی اتفاق نادری نیست، اتفاقی است که باید انتظارش را داشت.
          </Banner>
        )}
      </Card>

      <Card title="معامله‌هایی که منتظر تأیید شما هستند"
            hint={<>ربات این معامله‌ها را پیدا کرده ولی اجازه اجرا ندارد. تا وقتی دکمه تأیید
              را نزنید هیچ سفارشی به بروکر نمی‌رود.</>}
            sub={`${advice.length} پیشنهاد منتظر تصمیم شماست`}>
        {advice.length === 0 ? (
          <Empty>هیچ پیشنهادی منتظر تأیید نیست</Empty>
        ) : (
          <div className="stack gap16">
            {advice.map((d: Decision) => (
              <div key={d.client_order_id} className="stack gap8"
                   style={{ padding: 14, borderRadius: 14, background: "var(--surface-alt)" }}>
                <div className="row gap8 wrap">
                  <Chip tone={d.side === "BUY" ? "pos" : "neg"}>
                    {d.side === "BUY" ? "خرید" : "فروش"}
                  </Chip>
                  <span className="mono" style={{ fontWeight: 600 }}>{d.instrument}</span>
                  <span className="num fs12" title="اندازه معامله">{d.lots} لات</span>
                  <Chip title="اگر حد ضرر بخورد، این درصد از کل حساب از دست می‌رود">
                    بیشترین ضرر ممکن {Number(d.risk_pct).toFixed(2)}٪ حساب
                  </Chip>
                  <Chip title="چقدر شرایط با قاعده‌های این استراتژی جور است — این عدد احتمال سود نیست">
                    قوت نشانه {(d.signal_strength * 100).toFixed(0)}٪
                  </Chip>
                  <span className="fs11 faint" style={{ marginInlineStart: "auto" }}>{ago(d.ts_ns)}</span>
                </div>
                <p className="fs12" style={{ lineHeight: 1.8 }}>{d.explanation}</p>
                <div className="kv-grid c3">
                  <KV k="قیمت ورود" v={d.entry} />
                  <KV k="اگر تا اینجا خلاف ما رفت، می‌بندیم" v={d.stop}
                      hint="حد ضرر: بیشترین ضرری که از قبل پذیرفته‌ایم." />
                  <KV k="اگر تا اینجا به نفع ما رفت، می‌بندیم" v={d.target}
                      hint="حد سود: نقطه‌ای که سود برداشته می‌شود." />
                </div>
                {d.warnings.length > 0 && (
                  <Banner tone="warn" icon="⚠">{d.warnings.map((w) => w.message).join("؛ ")}</Banner>
                )}
                <div className="row gap8">
                  <button className="btn sm" onClick={() => setConfirm({
                    action: `تأیید و اجرای معامله ${d.instrument}`,
                    description: <>پیش از ارسال، این پیشنهاد یک‌بار دیگر با همه قواعد ایمنی
                      سنجیده می‌شود. بازار از لحظه ساخته شدن پیشنهاد تکان خورده، پس ممکن است
                      همین حالا رد شود — و این طبیعی است.</>,
                    path: "/api/advice/accept", body: { client_order_id: d.client_order_id },
                  })}>تأیید و اجرا</button>
                  <button className="btn ghost sm" onClick={() => setConfirm({
                    action: `رد پیشنهاد ${d.instrument}`,
                    description: "این پیشنهاد حذف می‌شود و هیچ معامله‌ای انجام نمی‌گیرد. خود این تصمیم هم در دفتر ثبت رویدادها می‌ماند.",
                    path: "/api/advice/reject",
                    body: { client_order_id: d.client_order_id, reason: "rejected from dashboard" },
                  })}>رد</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="سفارش‌ها با چه کیفیتی اجرا می‌شوند"
            hint={<>بین لحظه‌ای که سفارش می‌فرستیم و لحظه‌ای که پر می‌شود، قیمت تکان می‌خورد.
              این تفاوت‌ها کوچک به نظر می‌رسند ولی روی صدها معامله جمع می‌شوند.</>}
            sub="هزینه‌هایی که در قیمت‌ها دیده نمی‌شوند ولی از حساب کم می‌شوند">
        <div className="kv-grid c4">
          <KV k="چند سفارش فرستاده شده" v={snap.execution.n} />
          <KV k="چند درصد سفارش‌ها رد شده"
              hint="یعنی بروکر آن سفارش را اصلاً اجرا نکرده — قیمتی که دیده بودیم دیگر در دسترس نبوده."
              v={`${((snap.execution.reject_rate ?? 0) * 100).toFixed(1)}٪`} />
          <KV k="سرعت معمول پاسخ"
              hint="زمان رفت‌وبرگشت یک سفارش بر حسب هزارم ثانیه. ۱۰۰۰ میلی‌ثانیه یعنی یک ثانیه."
              v={`${snap.execution.median_latency_ms ?? "—"} میلی‌ثانیه`} />
          <KV k="کندترین ۵٪ پاسخ‌ها"
              hint="۹۵ درصد سفارش‌ها سریع‌تر از این پر شده‌اند؛ این عدد بدترین حالت‌های معمول را نشان می‌دهد."
              v={`${snap.execution.p95_latency_ms ?? "—"} میلی‌ثانیه`} />
          <KV k="به‌طور متوسط چقدر بدتر از قیمت درخواستی پر شده"
              hint={<>به آن «لغزش» می‌گویند. ۰٫۴ پیپ روی حجم ۰٫۱۰ لات یعنی حدود ۰٫۴ دلار در
                هر معامله — کوچک، ولی در ۵۰۰ معامله می‌شود ۲۰۰ دلار.</>}
              v={`${snap.execution.mean_slippage_pips ?? "—"} پیپ`} />
          <KV k="چند درصد این تفاوت‌ها به ضرر ما بوده"
              hint="عدد منصفانه حدود ۵۰٪ است؛ بالاتر از آن یعنی قیمت‌ها به‌طور سیستماتیک به ضرر ما پر می‌شوند."
              v={`${((snap.execution.adverse_slippage_share ?? 0) * 100).toFixed(0)}٪`} />
          <KV k="چند درصد ردها دقیقاً وقتی بوده که به نفع ما حرکت می‌کرد"
              hint={<>طرف مقابل حق دارد در آخرین لحظه سفارش را رد کند (به آن last-look می‌گویند).
                اگر این ردها بیشتر وقتی رخ دهد که بازار به نفع ماست، یعنی هزینه پنهان
                می‌پردازیم. عدد منصفانه حدود ۵۰٪ است.</>}
              v={`${((snap.execution.last_look_asymmetry ?? 0) * 100).toFixed(0)}٪`} />
          <KV k="چند سفارش واقعاً پر شده" v={snap.execution.fills ?? "—"} />
        </div>
        {(snap.execution.last_look_asymmetry ?? 0) > 0.6 && (
          <Banner tone="warn" icon="⚠">{snap.execution.last_look_note}</Banner>
        )}
        {(snap.execution.adverse_slippage_share ?? 0) > 0.6 && (
          <Banner tone="flat" icon="ℹ">
            اینکه بیش از نیمی از سفارش‌ها کمی بدتر از قیمت درخواستی پر شوند، برای کسی که با
            قیمت لحظه‌ای بازار معامله می‌کند عادی است. نکته این است که هر آزمایش روی تاریخ
            گذشته هم باید همین را فرض کند؛ آزمایشی که فرض می‌کند سفارش‌ها دقیقاً روی قیمت
            وسط پر می‌شوند، استراتژی‌هایی می‌سازد که در واقعیت ضرر می‌دهند.
          </Banner>
        )}
      </Card>

      <ConfirmWrite open={!!confirm} action={confirm?.action ?? ""}
                    description={confirm?.description} danger={confirm?.danger}
                    onClose={() => setConfirm(null)}
                    onConfirm={async (totp) =>
                      confirm ? write(confirm.path, confirm.body, totp)
                              : { ok: false, detail: "" }} />
    </div>
  );
}
