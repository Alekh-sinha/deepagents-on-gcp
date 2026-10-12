import type { Session } from "../api/types";

export function relativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.round((now.getTime() - then) / 1000);
  if (s < 0) return futureTime(-s);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)} d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function futureTime(s: number): string {
  if (s < 60) return "in a moment";
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  if (s < 86400) return `in ${Math.round(s / 3600)} h`;
  return `in ${Math.round(s / 86400)} d`;
}

export const timeUntil = (iso: string | null | undefined, now: Date = new Date()): string => (iso ? relativeTime(iso, now) : "never");

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export interface SessionGroup { label: string; sessions: Session[] }

/** The sidebar's conversation list: newest first, grouped like Today / Yesterday / Previous 7 days / Earlier. */
export function groupSessions(sessions: Session[], now: Date = new Date()): SessionGroup[] {
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const today = startOfDay(now);
  const buckets: [string, (t: number) => boolean][] = [
    ["Today", (t) => t >= today],
    ["Yesterday", (t) => t >= today - 86400000],
    ["Previous 7 days", (t) => t >= today - 6 * 86400000],
    ["Earlier", () => true],
  ];
  const sorted = [...sessions].sort((a, b) => b.last_active_at.localeCompare(a.last_active_at));
  const out: SessionGroup[] = buckets.map(([label]) => ({ label, sessions: [] }));
  for (const s of sorted) {
    const t = new Date(s.last_active_at).getTime();
    const at = buckets.findIndex(([, test]) => test(t));
    out[at].sessions.push(s);
  }
  return out.filter((g) => g.sessions.length);
}

/** `execute` -> "Ran a command", `mcp__calc__add` -> "calc · add". Names the model chose are shown, never trusted for layout. */
export function toolLabel(name: string, args: Record<string, any> = {}): { title: string; detail: string } {
  const short = (v: unknown, n = 90) => { const s = String(v ?? "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  if (name.startsWith("mcp__")) { const [, server, ...rest] = name.split("__"); return { title: `${server} · ${rest.join("__")}`, detail: short(JSON.stringify(args)) }; }
  switch (name) {
    case "execute": return { title: "Ran a command", detail: short(args.command) };
    case "write_file": return { title: "Wrote a file", detail: short(args.file_path) };
    case "read_file": return { title: "Read a file", detail: short(args.file_path) };
    case "edit_file": return { title: "Edited a file", detail: short(args.file_path) };
    case "ls": return { title: "Listed files", detail: short(args.path) };
    case "glob": case "grep": return { title: "Searched files", detail: short(args.pattern) };
    case "web_search": return { title: "Searched the web", detail: short(args.query) };
    case "delegate": case "task": return { title: `Asked the ${args.subagent_type ?? "helper"}`, detail: short(args.description) };
    case "list_artifacts": return { title: "Looked at saved files", detail: "" };
    case "restore_artifact": return { title: "Restored a saved file", detail: short(args.to_path) };
    case "github_list_repos": return { title: "Listed repositories", detail: "" };
    case "github_read_file": return { title: "Read from GitHub", detail: short(`${args.repo ?? ""} ${args.path ?? ""}`) };
    case "github_create_branch": return { title: "Created a branch", detail: short(`${args.repo ?? ""} ${args.branch ?? ""}`) };
    case "github_commit_files": return { title: "Committed files", detail: short(`${args.repo ?? ""} ${args.branch ?? ""}`) };
    case "github_open_pr": return { title: "Opened a pull request", detail: short(`${args.repo ?? ""} ${args.title ?? ""}`) };
    case "send_email": return { title: "Sent an email", detail: short(args.subject) };
    case "list_emails": case "read_email": return { title: "Read email", detail: "" };
    default: return { title: name.replace(/_/g, " "), detail: short(JSON.stringify(args)) };
  }
}

/** Tools whose calls are shown as cards or panels of their own, not as plain rows. */
export const HIDDEN_TOOLS = new Set(["write_todos", "ask_user", "submit_plan"]);

export interface CronPreset { label: string; cron: string }
export const CRON_PRESETS: CronPreset[] = [
  { label: "Every day at 9:00", cron: "0 9 * * *" },
  { label: "Weekdays at 9:00", cron: "0 9 * * 1-5" },
  { label: "Every Monday at 9:00", cron: "0 9 * * 1" },
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Every 30 minutes", cron: "*/30 * * * *" },
  { label: "First day of the month, 9:00", cron: "0 9 1 * *" },
];

const DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

/** Plain words for a five-field cron where that is simple, otherwise the expression itself. */
export function describeCron(cron: string): string {
  const preset = CRON_PRESETS.find((p) => p.cron === cron.trim());
  if (preset) return preset.label;
  const [min, hour, dom, mon, dow] = cron.trim().split(/\s+/);
  if (!dow) return cron;
  const time = /^\d+$/.test(min) && /^\d+$/.test(hour) ? `${hour.padStart(2, "0")}:${min.padStart(2, "0")}` : null;
  if (time && dom === "*" && mon === "*") {
    if (dow === "*") return `Every day at ${time}`;
    if (/^[0-7]$/.test(dow)) return `Every ${DOW[Number(dow) % 7]} at ${time}`;
  }
  return cron;
}
