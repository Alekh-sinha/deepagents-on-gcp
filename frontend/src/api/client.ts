// A small typed wrapper around fetch. Every state-changing call carries X-Requested-With (the backend's CSRF check); cookies ride along by themselves.
import type {
  Approval, Artifact, AuthConfig, AutoApprove, Config, Decision, McpImportResult, McpServer, ModelList, Schedule, ScheduleRun, Session, SessionView, Transcript, Turn,
} from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** What the backend says went wrong, as one readable sentence. FastAPI puts it in `detail` (a string, or a list of validation problems). */
export function describeError(status: number, body: unknown): string {
  const detail = (body as any)?.detail;
  if (typeof detail === "string" && detail.startsWith("unknown class_method")) return "This part of privai has not been deployed to your cloud engine yet. Deploy the latest backend to use it.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) return detail.map((d) => `${(d.loc ?? []).slice(1).join(".") || "input"}: ${d.msg}`).join("; ");
  return status === 401 ? "You are signed out." : status === 429 ? "Too many requests right now; try again in a moment." : `The server answered ${status}.`;
}

let onUnauthorized: (() => void) | null = null;
export const setUnauthorizedHandler = (fn: (() => void) | null) => { onUnauthorized = fn; };

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-Requested-With"] = "pinboard";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "Cannot reach the server. Is the privai backend running?");
  }
  const text = await res.text();
  let data: unknown = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    if (res.status === 401 && onUnauthorized) onUnauthorized();
    throw new ApiError(res.status, describeError(res.status, data));
  }
  return data as T;
}

const q = (params: Record<string, string | number | boolean | undefined | null>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") s.set(k, String(v));
  const out = s.toString();
  return out ? `?${out}` : "";
};

export const api = {
  authConfig: () => request<AuthConfig>("GET", "/api/auth/config"),
  signInWithGoogle: (credential: string) => request<{ email: string }>("POST", "/api/auth/google", { credential }),
  signOut: () => request<{ signed_out: boolean }>("POST", "/api/auth/logout"),
  me: () => request<Config>("GET", "/api/me"),
  models: () => request<ModelList>("GET", "/api/models"),

  sessions: (archived = false) => request<Session[]>("GET", `/api/sessions${q({ archived: archived || undefined, limit: 100 })}`),
  createSession: (body: { title?: string; model?: string | null; auto_approve?: AutoApprove | boolean }) => request<Session>("POST", "/api/sessions", body),
  session: (id: string) => request<SessionView>("GET", `/api/sessions/${id}`),
  updateSession: (id: string, changes: { title?: string; model?: string | null; archived?: boolean; auto_approve?: AutoApprove | boolean }) =>
    request<Session>("PATCH", `/api/sessions/${id}`, changes),
  transcript: (id: string, limit = 20, before?: string) => request<Transcript>("GET", `/api/sessions/${id}/transcript${q({ limit, before_turn_id: before })}`),
  warm: (id: string) => request<unknown>("POST", `/api/sessions/${id}/warm`),
  send: (id: string, text: string, extra: { model?: string | null; auto_approve?: AutoApprove | boolean } = {}) =>
    request<Turn>("POST", `/api/sessions/${id}/messages`, { text, ...extra }),

  turn: (id: string) => request<Turn>("GET", `/api/turns/${id}`),
  cancel: (id: string) => request<Turn>("POST", `/api/turns/${id}/cancel`),
  resume: (id: string) => request<Turn>("POST", `/api/turns/${id}/resume`),

  approvals: (status?: string, sessionId?: string) => request<Approval[]>("GET", `/api/approvals${q({ status, session_id: sessionId })}`),
  decide: (approvalId: string, decisions: Decision[]) => request<Turn>("POST", `/api/approvals/${approvalId}/decision`, { decisions }),

  artifacts: (sessionId?: string) => request<Artifact[]>("GET", `/api/artifacts${q({ session_id: sessionId, limit: 200 })}`),
  artifactUrl: (id: string) => `/api/artifacts/${id}/content`,

  mcpServers: () => request<{ servers: McpServer[] }>("GET", "/api/mcp/servers"),
  mcpImport: (config: string | object) => request<{ results: McpImportResult[] }>("POST", "/api/mcp/import", { config }),
  mcpPut: (name: string, body: { url: string; headers?: Record<string, string>; enabled?: boolean; requires_approval?: boolean; transport?: "http" | "sse" }) =>
    request<McpServer>("PUT", `/api/mcp/servers/${name}`, body),
  mcpPatch: (name: string, changes: { enabled?: boolean; requires_approval?: boolean; no_approval_tools?: string[] }) => request<McpServer>("PATCH", `/api/mcp/servers/${name}`, changes),
  mcpTest: (name: string) => request<McpServer>("POST", `/api/mcp/servers/${name}/test`),
  mcpDelete: (name: string) => request<{ deleted: string }>("DELETE", `/api/mcp/servers/${name}`),

  schedules: () => request<{ schedules: Schedule[] }>("GET", "/api/schedules"),
  scheduleCreate: (body: { title: string; instructions: string; cron: string; timezone: string; model?: string | null; auto_approve?: AutoApprove | boolean; enabled?: boolean }) =>
    request<Schedule>("POST", "/api/schedules", body),
  scheduleUpdate: (id: string, changes: Partial<{ title: string; instructions: string; cron: string; timezone: string; model: string | null; auto_approve: AutoApprove | boolean; enabled: boolean }>) =>
    request<Schedule>("PATCH", `/api/schedules/${id}`, changes),
  scheduleDelete: (id: string) => request<{ deleted: string }>("DELETE", `/api/schedules/${id}`),
  scheduleRun: (id: string) => request<ScheduleRun>("POST", `/api/schedules/${id}/run`),
  scheduleRuns: (id: string) => request<{ runs: ScheduleRun[] }>("GET", `/api/schedules/${id}/runs?limit=20`),
};
