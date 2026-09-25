/**
 * Data access.
 *
 * Two providers behind one interface:
 *   LiveProvider — talks to the FastAPI service, with a bearer session and a
 *                  TOTP header on every write.
 *   DemoProvider — serves the bundled dataset so the page is explorable with no
 *                  backend. It refuses every write and says why.
 *
 * The page starts in demo mode when it cannot reach an API, and the banner says
 * so plainly rather than showing fabricated data as if it were live.
 */
import { demoEndpoints, demoSnapshot } from "./demo";
import { breakEvenTable, capitalTable, researchFromVerdict } from "./derived";
import type { Decision, Snapshot } from "./types";

export type Provider = {
  kind: "live" | "demo";
  snapshot(): Promise<Snapshot>;
  /** One endpoint, on demand. Used by the pages whose data changes rarely and
   *  is owner-only in places -- the account list and the machine fingerprint
   *  have no business being on the wire every 15 seconds for a page nobody has
   *  opened. */
  get<T>(path: string): Promise<T>;
  write(path: string, body: unknown, totp: string): Promise<{ ok: boolean; detail: string }>;
  connectStream(onMessage: (m: any) => void): () => void;
};

const TOKEN_KEY = "sentinel.token";

export function storedToken(): string | null {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function storeToken(t: string | null) {
  try { t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY); }
  catch { /* private mode: the session simply does not persist a reload */ }
}

async function j<T>(url: string, token: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`,
               ...(init?.headers ?? {}) },
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export function makeLiveProvider(base: string, token: string): Provider {
  return {
    kind: "live",
    async snapshot() {
      // Optional reads: a failure here must not blank the whole console, and
      // must not be mistaken for "nothing stored" either -- `undefined` means
      // "could not ask", `null` means "asked; there is none".
      const optional = <T,>(path: string) =>
        j<T>(`${base}${path}`, token).catch(() => undefined);
      const [status, positions, trades, equity, decisions, risk, performance,
             lessons, proposals, audit, strategies, execution, config, advice, health,
             latest, monthlyView] =
        await Promise.all([
          j<any>(`${base}/api/status`, token),
          j<any>(`${base}/api/positions`, token),
          j<any>(`${base}/api/trades?limit=500`, token),
          j<any>(`${base}/api/equity?limit=3000`, token),
          j<any>(`${base}/api/decisions?limit=300`, token),
          j<any>(`${base}/api/risk`, token),
          j<any>(`${base}/api/performance`, token),
          j<any>(`${base}/api/lessons`, token),
          j<any>(`${base}/api/proposals`, token),
          j<any>(`${base}/api/audit?limit=200`, token),
          j<any>(`${base}/api/strategies`, token),
          j<any>(`${base}/api/execution`, token),
          j<any>(`${base}/api/config`, token),
          j<any>(`${base}/api/advice`, token),
          j<any>(`${base}/api/health`, token),
          optional<{ verdict: any }>("/api/research/latest"),
          optional<any>("/api/performance/monthly"),
        ]);
      const research = latest === undefined
        ? { research: undefined, gates: [], cpcvSharpes: [] }
        : researchFromVerdict(latest.verdict);
      const riskPct = Number(config?.risk?.risk_per_trade_pct ?? 0.5);
      return {
        status, positions: positions.positions ?? [], trades: trades.trades ?? [],
        equity: equity.points ?? [], decisions: decisions.decisions ?? [],
        risk, performance, lessons: lessons.lessons ?? [],
        proposals: proposals.proposals ?? [], audit: audit.records ?? [],
        strategies: strategies.available ?? [], allocations: strategies.allocations ?? {},
        execution, config, advice: advice.pending ?? [], health,
        // Never the demo's numbers. These came from the bundled dataset until
        // 1.8.2, so a real install showed a made-up acceptance verdict and a
        // made-up year of monthly returns as the owner's own.
        gates: research.gates, cpcvSharpes: research.cpcvSharpes,
        research: research.research,
        costTable: breakEvenTable(),
        capitalTable: capitalTable(riskPct), capitalRiskPct: riskPct,
        monthly: monthlyView?.rows ?? [],
        monthlyInfo: monthlyView === undefined
          ? { source: "unavailable", since_ns: null, partial: [], days: 0 }
          : { source: "ledger", since_ns: monthlyView.since_ns ?? null,
              partial: monthlyView.partial ?? [], days: monthlyView.days ?? 0 },
      } as Snapshot;
    },
    async get<T>(path: string) {
      return j<T>(`${base}${path}`, token);
    },
    async write(path, body, totp) {
      try {
        const res = await fetch(`${base}${path}`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`,
                     "X-TOTP": totp },
          body: JSON.stringify(body),
        });
        const text = await res.text();
        return { ok: res.ok, detail: res.ok ? text : `${res.status}: ${text}` };
      } catch (e) {
        return { ok: false, detail: String(e) };
      }
    },
    connectStream(onMessage) {
      let closed = false;
      // The token rides in the WebSocket subprotocol list, never the URL: a
      // query string is written to access logs and proxies, and a session
      // token in a log is a session for whoever reads it.
      const url = `${base.replace(/^http/, "ws")}/ws`;
      let ws: WebSocket | null = null;
      let retry = 0;
      const open = () => {
        if (closed) return;
        try { ws = new WebSocket(url, ["sentinel-v1", `auth.${token}`]); } catch { return; }
        ws.onmessage = (ev) => { try { onMessage(JSON.parse(ev.data)); } catch { /* ignore */ } };
        ws.onclose = () => {
          if (closed) return;
          retry = Math.min(30000, 1000 * 2 ** Math.min(5, retry / 1000 + 1));
          setTimeout(open, retry);
        };
      };
      open();
      return () => { closed = true; ws?.close(); };
    },
  };
}

export function makeDemoProvider(): Provider {
  return {
    kind: "demo",
    async snapshot() {
      return JSON.parse(JSON.stringify(demoSnapshot)) as Snapshot;
    },
    async get<T>(path: string) {
      const key = path.split("?")[0];
      if (!(key in demoEndpoints)) {
        throw new Error(`در حالت نمایشی، «${key}» داده‌ای ندارد.`);
      }
      return JSON.parse(JSON.stringify(demoEndpoints[key])) as T;
    },
    async write() {
      return {
        ok: false,
        detail: "این یک نمای نمایشی است و به هیچ حسابی وصل نیست. هر عمل نوشتنی نیاز به " +
          "یک نشست احراز هویت‌شده روی سرویس واقعی و یک کد دومرحله‌ای تازه دارد.",
      };
    },
    connectStream() { return () => undefined; },
  };
}

export async function login(base: string, username: string, password: string) {
  const res = await fetch(`${base}/api/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) throw new Error((await res.text()) || "ورود ناموفق");
  return res.json() as Promise<{ token: string; role: string; expires_in_sec: number }>;
}

/** Probe for a backend on the same origin. Silent failure → demo mode.
 *
 * A 200 is NOT sufficient evidence: any static host with an SPA fallback
 * answers /api/status with index.html and a 200, which would strand the page
 * on a login screen it can never satisfy. So a 200 must also be JSON that
 * looks like our status document. A 401 is the cleanest positive signal —
 * something is there and it wants a session. */
export async function detectBackend(base: string): Promise<boolean> {
  try {
    const res = await fetch(`${base}/api/status`, {
      method: "GET", headers: { Accept: "application/json" },
    });
    if (res.status === 401 || res.status === 403) return true;
    if (!res.ok) return false;
    const type = res.headers.get("content-type") ?? "";
    if (!type.includes("application/json")) return false;
    const body = await res.json().catch(() => null);
    return !!body && typeof body === "object" && "mode" in body;
  } catch {
    return false;
  }
}
