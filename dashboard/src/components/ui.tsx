import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

export const fa = (n: number | string) =>
  String(n).replace(/[0-9]/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)]);

export function pct(v: number, digits = 2) {
  if (!isFinite(v)) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}٪`;
}
export function money(v: number | string, digits = 2) {
  const n = Number(v);
  if (!isFinite(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
export function ago(ts_ns: number) {
  const s = (Date.now() - ts_ns / 1e6) / 1000;
  if (s < 60) return `${Math.max(0, Math.round(s))} ثانیه پیش`;
  if (s < 3600) return `${Math.round(s / 60)} دقیقه پیش`;
  if (s < 86400) return `${Math.round(s / 3600)} ساعت پیش`;
  return `${Math.round(s / 86400)} روز پیش`;
}
export function dt(ts_ns: number) {
  const d = new Date(ts_ns / 1e6);
  return d.toISOString().slice(0, 16).replace("T", " ");
}

/**
 * Quiet help affordance.
 *
 * A small ⓘ that reveals one or two plain-Persian sentences. It answers to
 * hover on a desktop and to a tap on a phone, because the console is read on
 * both. The bubble is position:fixed and re-measured after it mounts, so a card
 * with its own scroll container can never clip it.
 */
export function Hint({ text, label = "توضیح ساده" }: {
  text: React.ReactNode; label?: string;
}) {
  const btn = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLSpanElement>(null);
  const [stuck, setStuck] = useState(false);
  const [hover, setHover] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number; width: number } | null>(null);
  const open = stuck || hover;

  const place = useCallback(() => {
    const b = btn.current?.getBoundingClientRect();
    if (!b) return;
    const width = Math.min(300, window.innerWidth - 24);
    const left = Math.max(12, Math.min(b.left + b.width / 2 - width / 2,
                                      window.innerWidth - width - 12));
    const h = pop.current?.offsetHeight ?? 0;
    const below = b.bottom + 8;
    const top = h && below + h > window.innerHeight - 8
      ? Math.max(8, b.top - h - 8) : below;
    setPos((prev) => (prev && prev.top === top && prev.left === left && prev.width === width)
      ? prev : { top, left, width });
  }, []);

  useLayoutEffect(() => {
    if (!open) { setPos(null); return; }
    place();
    const id = requestAnimationFrame(place);
    return () => cancelAnimationFrame(id);
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const close = () => { setStuck(false); setHover(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    const onDown = (e: Event) => {
      if (!btn.current?.contains(e.target as Node)) close();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("pointerdown", onDown, true);
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, place]);

  return (
    <>
      <button ref={btn} type="button" className={`hint ${open ? "on" : ""}`}
              aria-label={label} aria-expanded={open}
              onClick={(e) => { e.stopPropagation(); e.preventDefault(); setStuck((v) => !v); }}
              onMouseEnter={() => setHover(true)}
              onMouseLeave={() => setHover(false)}
              onFocus={() => setHover(true)}
              onBlur={() => setHover(false)}>ⓘ</button>
      {open && (
        <span ref={pop} role="tooltip" className="hint-pop"
              style={{ top: pos?.top ?? 0, left: pos?.left ?? 0,
                       width: pos?.width ?? 300,
                       visibility: pos ? "visible" : "hidden" }}>
          {text}
        </span>
      )}
    </>
  );
}

export function Card({ title, sub, actions, children, className = "", style, hint }: {
  title?: React.ReactNode; sub?: React.ReactNode; actions?: React.ReactNode;
  children: React.ReactNode; className?: string; style?: React.CSSProperties;
  hint?: React.ReactNode;
}) {
  return (
    <section className={`card ${className}`} style={style}>
      {(title || actions) && (
        <header className="card-head">
          <div>
            {title && (
              <div className="card-title">{title}{hint && <Hint text={hint} />}</div>
            )}
            {sub && <div className="card-sub">{sub}</div>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Tile({ label, value, note, tone, sub, hint, term }: {
  label: React.ReactNode; value: React.ReactNode; note?: React.ReactNode;
  tone?: "pos" | "neg" | "warn"; sub?: boolean;
  hint?: React.ReactNode; term?: string;
}) {
  return (
    <div className="tile">
      <div className="tile-label">
        {label}
        {term && <span className="term-tag">{term}</span>}
        {hint && <Hint text={hint} />}
      </div>
      <div className={`tile-value ${sub ? "sm" : ""} ${tone ?? ""}`}>{value}</div>
      {note && <div className="tile-note">{note}</div>}
    </div>
  );
}

export function Chip({ tone, children, title }: {
  tone?: "pos" | "neg" | "warn" | "info" | "solid" | "flat";
  children: React.ReactNode; title?: string;
}) {
  // "flat" is the default outline chip. Naming it lets a caller say "neutral
  // on purpose" rather than omitting the prop, which reads as an oversight.
  const cls = !tone || tone === "flat" ? "" : tone;
  return <span className={`chip ${cls}`} title={title}>{children}</span>;
}

export function Switch({ checked, onChange, disabled, label }: {
  checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string;
}) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label}
            className="switch" disabled={disabled}
            onClick={() => !disabled && onChange(!checked)} />
  );
}

export function Seg<T extends string>({ value, options, onChange }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void;
}) {
  return (
    <div className="seg" role="group">
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={value === o.value}
                onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

export function Field({ label, help, children, hint, term, htmlFor }: {
  label: React.ReactNode; help?: React.ReactNode; children: React.ReactNode;
  hint?: React.ReactNode; term?: string; htmlFor?: string;
}) {
  /* A real <label>. This was a <span>, so NO input rendered through Field had
     an accessible name -- including the broker password, the new-user
     password and the licence paste box. Wrapping the control in the label
     also names it when no id is supplied, which is every existing caller. */
  const inner = (
    <>
      <span className="field-label">
        {label}
        {term && <span className="term-tag">{term}</span>}
        {hint && <Hint text={hint} />}
      </span>
      {help && <span className="field-help">{help}</span>}
    </>
  );
  return (
    <div className="field-row">
      {htmlFor
        ? <label className="field" htmlFor={htmlFor}>{inner}</label>
        : <div className="field">{inner}</div>}
      <div className="control">{children}</div>
    </div>
  );
}

export function NumberField({ value, onChange, min, max, step = 0.01, suffix, disabled }: {
  value: number; onChange: (v: number) => void; min?: number; max?: number;
  step?: number; suffix?: string; disabled?: boolean;
}) {
  return (
    <div className="row gap6">
      <input className="input num" type="number" value={value} min={min} max={max} step={step}
             disabled={disabled}
             onChange={(e) => onChange(Number(e.target.value))} />
      {suffix && <span className="fs12 muted nowrap">{suffix}</span>}
    </div>
  );
}

export function Banner({ tone = "flat", children, icon }: {
  tone?: "warn" | "neg" | "info" | "flat"; children: React.ReactNode; icon?: string;
}) {
  return (
    <div className={`banner ${tone}`}>
      {icon && <span aria-hidden="true" style={{ flex: "0 0 auto" }}>{icon}</span>}
      <div>{children}</div>
    </div>
  );
}

export function KV({ k, v, tone, hint }: {
  k: React.ReactNode; v: React.ReactNode; tone?: string; hint?: React.ReactNode;
}) {
  return (
    <div className="kv">
      <span className="k">{k}{hint && <Hint text={hint} />}</span>
      <span className={`v ${tone ?? ""}`}>{v}</span>
    </div>
  );
}

export function Modal({ open, title, onClose, children, footer }: {
  open: boolean; title: string; onClose: () => void;
  children: React.ReactNode; footer?: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!open) return;
    // Remember what had focus, so closing the dialog puts the keyboard back
    // where the person left it rather than at the top of the document.
    opener.current = document.activeElement as HTMLElement | null;
    const FOCUSABLE =
      'a[href],button:not([disabled]),textarea:not([disabled]),' +
      'input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { onClose(); return; }
      if (e.key !== "Tab" || !ref.current) return;
      // TRAP TAB. Without this a keyboard user tabs straight out of the
      // confirmation dialog -- the one holding the second-factor field -- into
      // the page behind it, which is still fully interactive.
      const items = Array.from(
        ref.current.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter((el) => el.offsetParent !== null);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === ref.current)) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault(); first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    // Focus the first control rather than the dialog box, so the person can
    // start typing the code immediately.
    const firstControl = ref.current?.querySelector<HTMLElement>(
      'input:not([disabled]),textarea:not([disabled]),select:not([disabled])');
    (firstControl ?? ref.current)?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
      opener.current?.focus?.();
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="modal-back" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}
           tabIndex={-1} ref={ref}>
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
          <h2>{title}</h2>
          <button className="btn ghost sm" onClick={onClose}>بستن</button>
        </div>
        {children}
        {footer && <div className="row gap8 mt16" style={{ justifyContent: "flex-end" }}>{footer}</div>}
      </div>
    </div>
  );
}

/** Every write goes through this: it is the place the second factor is demanded. */
export function ConfirmWrite({ open, action, description, danger, onClose, onConfirm }: {
  open: boolean; action: string; description: React.ReactNode; danger?: boolean;
  onClose: () => void; onConfirm: (totp: string) => Promise<{ ok: boolean; detail: string }>;
}) {
  const [totp, setTotp] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; detail: string } | null>(null);
  useEffect(() => { if (open) { setTotp(""); setResult(null); setBusy(false); } }, [open]);
  return (
    <Modal open={open} title={action} onClose={onClose}
           footer={
             <>
               <button className="btn ghost" onClick={onClose}>انصراف</button>
               <button className={`btn ${danger ? "danger" : ""}`} disabled={busy || totp.length !== 6}
                       onClick={async () => {
                         setBusy(true);
                         setResult(await onConfirm(totp));
                         setBusy(false);
                       }}>
                 {busy ? "در حال اجرا…" : "تأیید و اجرا"}
               </button>
             </>
           }>
      <div className="stack gap12">
        <div className="fs13" style={{ lineHeight: 1.8 }}>{description}</div>
        <Banner tone="info" icon="🔐">
          هر عمل نوشتنی به یک کد دومرحله‌ای تازه نیاز دارد. نشست به‌تنهایی اجازه تغییر وضعیت
          حساب را نمی‌دهد — یک توکن دزدیده‌شده نمی‌تواند سفارش بگذارد.
        </Banner>
        <div className="field">
          <span className="field-label">کد شش‌رقمی احراز هویت دومرحله‌ای</span>
          <input className="input num" inputMode="numeric" maxLength={6} value={totp}
                 placeholder="------"
                 onChange={(e) => setTotp(e.target.value.replace(/\D/g, "").slice(0, 6))} />
        </div>
        {result && (
          <Banner tone={result.ok ? "info" : "neg"} icon={result.ok ? "✓" : "✕"}>
            {result.ok
              /* NEVER the raw body on success. `detail` is the endpoint's JSON
                 response, and for "create user" that document contains the
                 one-time TOTP enrolment URI -- the second factor itself,
                 printed into a banner, while the page's own carefully written
                 enrolment dialog waited behind this one. */
              ? <>انجام شد.</>
              : <span dir="ltr" style={{ display: "block", textAlign: "start",
                                         wordBreak: "break-word" }}>
                  {result.detail || "انجام نشد."}
                </span>}
          </Banner>
        )}
      </div>
    </Modal>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Disclosure({ summary, children }: {
  summary: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <details className="disc">
      <summary>{summary}</summary>
      <div>{children}</div>
    </details>
  );
}

export function useLocalState<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : initial;
    } catch { return initial; }
  });
  const set = (next: T) => {
    setV(next);
    try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* ignore */ }
  };
  return [v, set];
}
