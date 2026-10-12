import { useState } from "react";
import { Icon, type IconName } from "../ui/Icon";
import { toolLabel } from "../../lib/format";
import type { Item } from "../../state/transcript";

type ToolItem = Extract<Item, { kind: "tool" }>;

const ICON: Record<string, IconName> = {
  execute: "terminal", write_file: "file", read_file: "file", edit_file: "edit", ls: "file", glob: "search", grep: "search", web_search: "globe",
  delegate: "users", task: "users", send_email: "mail", list_emails: "mail", read_email: "mail", list_artifacts: "file", restore_artifact: "download",
};
const iconFor = (name: string): IconName => (name.startsWith("mcp__") ? "plug" : name.startsWith("github_") ? "branch" : ICON[name] ?? "tool");

export function StatusDot({ status }: { status: ToolItem["status"] }) {
  if (status === "running") return <span className="flex gap-0.5" aria-label="running"><i className="dot" /><i className="dot" /><i className="dot" /></span>;
  return <Icon name={status === "error" ? "x" : "check"} size={13} className={status === "error" ? "text-danger" : "text-ok"} label={status === "error" ? "failed" : "done"} />;
}

export function ToolRow({ item }: { item: ToolItem }) {
  const [open, setOpen] = useState(false);
  const { title, detail } = toolLabel(item.name, item.args);
  const isHelper = item.name === "delegate" || item.name === "task";
  const model = isHelper && typeof item.args.model === "string" ? item.args.model : "";
  const hasBody = Object.keys(item.args).length > 0 || item.result;
  return (
    <div className="rounded-lg">
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open} disabled={!hasBody}
        className="group flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[13px] text-muted hover:bg-paper disabled:cursor-default">
        <Icon name={iconFor(item.name)} size={14} className="text-faint" />
        <span className="font-medium text-ink/80">{title}</span>
        {model && <span className="chip">{model}</span>}
        {item.edited && <span className="chip border-accent/30 bg-accentSoft text-accentInk">edited</span>}
        <span className="min-w-0 flex-1 truncate text-faint">{detail}</span>
        <StatusDot status={item.status} />
        {hasBody && <Icon name={open ? "chevronUp" : "chevronDown"} size={13} className="text-faint opacity-0 group-hover:opacity-100" />}
      </button>
      {open && (
        <div className="mb-1 ml-6 space-y-2 border-l-2 border-line pl-3">
          {Object.keys(item.args).length > 0 && (
            <div><div className="mb-0.5 text-[11px] font-medium uppercase tracking-wide text-faint">Input</div>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-paper px-2.5 py-1.5 font-mono text-[12px]">{item.name === "execute" ? String(item.args.command ?? "") : JSON.stringify(item.args, null, 2)}</pre></div>
          )}
          {item.result && (
            <div><div className="mb-0.5 text-[11px] font-medium uppercase tracking-wide text-faint">{item.status === "error" ? "Problem" : "Result"}</div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-paper px-2.5 py-1.5 font-mono text-[12px]">{item.result}{item.resultTruncated ? "\n… (shortened)" : ""}</pre></div>
          )}
        </div>
      )}
    </div>
  );
}
