// What each kind of approval shows (read-only). The full content is shown, not a summary: the person is deciding on exactly this.
import { Icon, type IconName } from "../ui/Icon";
import { formatBytes } from "../../lib/format";
import type { ApprovalItem, ApprovalKind } from "../../api/types";

export const KIND_ICON: Record<ApprovalKind, IconName> = { shell: "terminal", email: "mail", github: "branch", plan: "list", question: "question", interrupt: "question", mcp: "plug", other: "tool" };

const Row = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div className="flex gap-3 text-[13px]"><span className="w-16 shrink-0 text-faint">{label}</span><span className="min-w-0 flex-1 break-words text-ink">{children}</span></div>
);

const Pre = ({ children, max = "max-h-56" }: { children: React.ReactNode; max?: string }) => (
  <pre className={`${max} overflow-auto whitespace-pre-wrap break-words rounded-lg border border-line bg-paper px-3 py-2 font-mono text-[12.5px] leading-relaxed text-ink`}>{children}</pre>
);

const list = (v: unknown): string => (Array.isArray(v) ? v.join(", ") : v ? String(v) : "");

export function ItemBody({ item }: { item: ApprovalItem }) {
  const p = item.preview ?? {};
  switch (item.kind) {
    case "shell":
      return <Pre max="max-h-40">{String(p.command ?? "")}</Pre>;

    case "email":
      return (
        <div className="space-y-1.5">
          <Row label="To">{list(p.to)}</Row>
          {list(p.cc) && <Row label="Cc">{list(p.cc)}</Row>}
          <Row label="Subject"><span className="font-medium">{String(p.subject ?? "")}</span></Row>
          {Array.isArray(p.attachments) && p.attachments.length > 0 && <Row label="Files">{p.attachments.map((a: any) => String(a?.path ?? a)).join(", ")}</Row>}
          <Pre>{String(p.body ?? "")}</Pre>
        </div>
      );

    case "github":
      return (
        <div className="space-y-1.5">
          <Row label="Repo">{String(p.repo ?? "")}</Row>
          {p.branch && <Row label="Branch"><code className="font-mono text-[12.5px]">{String(p.branch)}</code></Row>}
          {p.base && <Row label="From">{String(p.base)}</Row>}
          {p.message && <Row label="Message"><span className="font-medium">{String(p.message)}</span></Row>}
          {p.body && <Pre max="max-h-32">{String(p.body)}</Pre>}
          {Array.isArray(p.files) && p.files.length > 0 && (
            <div className="space-y-1.5 pt-1">
              {p.files.map((f: any, i: number) => (
                <details key={i} className="rounded-lg border border-line bg-paper px-3 py-1.5">
                  <summary className="cursor-pointer text-[13px]"><code className="font-mono text-[12.5px]">{String(f.path)}</code>
                    <span className="ml-2 text-faint">{f.source && f.source !== "inline" ? `from ${f.source}` : f.chars != null ? `${f.chars} characters` : ""}</span></summary>
                  {f.content ? <Pre max="max-h-48">{String(f.content)}</Pre> : <p className="py-1 text-[12.5px] text-faint">The file is read from the sandbox when it is committed.</p>}
                </details>
              ))}
            </div>
          )}
        </div>
      );

    case "plan":
      return (
        <div className="space-y-2">
          <p className="text-[14px] font-medium">{String(p.summary ?? "")}</p>
          <ol className="space-y-1.5">
            {(Array.isArray(p.tasks) ? p.tasks : []).map((t: any, i: number) => (
              <li key={i} className="flex gap-2.5 rounded-lg border border-line bg-paper px-3 py-2">
                <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accentSoft text-[11px] font-semibold text-accentInk">{i + 1}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2"><span className="text-[13.5px] font-medium">{String(t.title ?? "")}</span><span className="chip">{String(t.assignee ?? "")}</span></div>
                  {t.detail && <p className="mt-0.5 text-[12.5px] text-muted">{String(t.detail)}</p>}
                </div>
              </li>
            ))}
          </ol>
        </div>
      );

    case "question":
      return <p className="whitespace-pre-wrap text-[14px]">{String(p.question ?? "")}</p>;

    case "interrupt":
      return <Pre>{typeof p.value === "string" ? p.value : JSON.stringify(p.value, null, 2)}</Pre>;

    case "mcp":
      return (
        <div className="space-y-1.5">
          <Row label="Server">{String(p.server ?? "")}</Row>
          <Row label="Tool"><code className="font-mono text-[12.5px]">{String(p.tool ?? "")}</code></Row>
          <Pre max="max-h-48">{JSON.stringify(p.args ?? {}, null, 2)}</Pre>
        </div>
      );

    default:
      return <Pre max="max-h-48">{JSON.stringify(p.args ?? p, null, 2)}</Pre>;
  }
}

/** A one-line description of an already-decided item (for the collapsed history rows). */
export function itemSummary(item: ApprovalItem): string {
  const p = item.preview ?? {};
  switch (item.kind) {
    case "shell": return String(p.command ?? "").split("\n")[0].slice(0, 100);
    case "email": return `${list(p.to)} · ${String(p.subject ?? "")}`;
    case "github": return `${String(p.repo ?? "")}${p.branch ? ` · ${p.branch}` : ""}`;
    case "plan": return String(p.summary ?? "");
    case "mcp": return `${String(p.server ?? "")} · ${String(p.tool ?? "")}`;
    case "question": return String(p.question ?? "").slice(0, 100);
    default: return item.title;
  }
}

export const attachmentSize = formatBytes;
export { Icon };
