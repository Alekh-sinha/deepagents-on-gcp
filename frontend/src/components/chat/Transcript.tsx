import { useMemo, useState, type ReactNode } from "react";
import { api } from "../../api/client";
import type { Approval, Decision } from "../../api/types";
import { formatBytes, HIDDEN_TOOLS } from "../../lib/format";
import { toBlocks, type Block } from "../../state/blocks";
import type { Item, TranscriptState, Usage } from "../../state/transcript";
import { ApprovalGroup, AutoApprovedRow } from "../approvals/ApprovalGroup";
import { Markdown } from "../Markdown";
import { Icon } from "../ui/Icon";
import { ToolRow } from "./ToolRow";

function UserBubble({ text }: { text: string }) {
  return <div className="flex justify-end"><div className="max-w-[85%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-chrome px-4 py-2.5 text-[15px]">{text}</div></div>;
}

function AssistantText({ item }: { item: Extract<Item, { kind: "assistant" }> }) {
  return (
    <div className={item.streaming ? "caret" : ""}>
      <Markdown text={item.text} />
      {item.truncated && <p className="mt-1 text-[12px] text-faint">The rest of this answer was cut for display.</p>}
    </div>
  );
}

function HelperBlock({ block, children }: { block: Extract<Block, { type: "helper" }>; children: ReactNode }) {
  const [open, setOpen] = useState(true);
  const running = block.items.some((i) => (i.kind === "tool" && i.status === "running") || (i.kind === "assistant" && i.streaming));
  return (
    <div className="rounded-xl border border-line bg-paper/60">
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open} className="flex w-full items-center gap-2 px-3 py-2 text-left">
        <Icon name="users" size={14} className="text-accentInk" />
        <span className="text-[13px] font-semibold capitalize">{block.agent}</span>
        {block.model && <span className="chip">{block.model}</span>}
        {running && <span className="flex gap-0.5"><i className="dot" /><i className="dot" /><i className="dot" /></span>}
        <span className="ml-auto text-[12px] text-faint">{block.items.length} step{block.items.length === 1 ? "" : "s"}</span>
        <Icon name={open ? "chevronUp" : "chevronDown"} size={14} className="text-faint" />
      </button>
      {open && <div className="space-y-1 border-t border-line px-2 py-2">{children}</div>}
    </div>
  );
}

function ArtifactRow({ items }: { items: Extract<Item, { kind: "artifact" }>[] }) {
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((a) => (
        <a key={a.id} href={api.artifactUrl(a.artifactId)} className="group flex max-w-full items-center gap-2 rounded-xl border border-line bg-panel px-3 py-2 shadow-card hover:border-lineStrong" download>
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accentSoft text-accentInk"><Icon name="file" size={16} /></span>
          <span className="min-w-0"><span className="block truncate text-[13px] font-medium">{a.name}</span><span className="block text-[11.5px] text-faint">{formatBytes(a.size)}{a.path !== a.name ? ` · ${a.path}` : ""}</span></span>
          <Icon name="download" size={14} className="text-faint group-hover:text-ink" />
        </a>
      ))}
    </div>
  );
}

function StatusRow({ item }: { item: Extract<Item, { kind: "status" }> }) {
  if (item.status === "completed") return null;
  const map = {
    cancelled: { cls: "border-line bg-paper text-muted", icon: "stop", text: "You stopped this." },
    failed: { cls: "border-danger/30 bg-dangerSoft text-danger", icon: "alert", text: item.text || "Something went wrong." },
    blocked: { cls: "border-warn/40 bg-warnSoft text-warn", icon: "shield", text: item.text || "That message was blocked." },
  } as const;
  const s = map[item.status];
  return <div role="status" className={`flex items-start gap-2 rounded-xl border px-3 py-2 text-[13px] ${s.cls}`}><Icon name={s.icon as any} size={15} className="mt-0.5" /><span>{s.text}</span></div>;
}

function UsageLine({ usage }: { usage: Usage }) {
  const total = usage.input + usage.output;
  const fmt = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
  return <p className="text-[11.5px] text-faint">{usage.models.length ? `${usage.models.join(" · ")} · ` : ""}{fmt(total)} tokens · {usage.calls} model call{usage.calls === 1 ? "" : "s"}</p>;
}

export function Transcript({ transcript, pending, working, onDecide, deciding }: {
  transcript: TranscriptState; pending: Approval[]; working: boolean; onDecide: (approvalId: string, decisions: Decision[]) => void; deciding: boolean;
}) {
  const byId = useMemo(() => new Map(pending.map((p) => [p.id, p])), [pending]);
  const blocks = useMemo(() => toBlocks(transcript.items, transcript.nsModel), [transcript.items, transcript.nsModel]);
  const lastIsStreaming = transcript.items.at(-1)?.kind === "assistant" && (transcript.items.at(-1) as any).streaming;
  const lastOfTurn = useMemo(() => { const m = new Map<string, number>(); transcript.items.forEach((i, idx) => m.set(i.turnId, idx)); return m; }, [transcript.items]);

  const renderItem = (item: Item, idx: number): ReactNode => {
    switch (item.kind) {
      case "user": return <UserBubble key={item.id} text={item.text} />;
      case "assistant": return <AssistantText key={item.id} item={item} />;
      case "tool": return HIDDEN_TOOLS.has(item.name) ? null : <ToolRow key={item.id} item={item} />;
      case "approval": return <ApprovalGroup key={item.id} row={item} approval={byId.get(item.id)} onDecide={onDecide} busy={deciding} />;
      case "auto_approval": return <AutoApprovedRow key={item.id} items={item.items} />;
      case "notice": return <p key={item.id} className="text-center text-[12.5px] text-faint">{item.text}</p>;
      case "status": return <StatusRow key={item.id} item={item} />;
      case "artifact": {
        const prev = transcript.items[idx - 1];
        if (prev?.kind === "artifact") return null;   // rendered together with the first of its run
        const run: Extract<Item, { kind: "artifact" }>[] = [];
        for (let j = idx; transcript.items[j]?.kind === "artifact"; j++) run.push(transcript.items[j] as any);
        return <ArtifactRow key={item.id} items={run} />;
      }
    }
  };

  return (
    <div className="mx-auto w-full max-w-3xl space-y-4 px-4 py-6">
      {blocks.map((b, bi) => {
        if (b.type === "item") {
          const idx = transcript.items.indexOf(b.item);
          const out = renderItem(b.item, idx);
          const endOfTurn = lastOfTurn.get(b.item.turnId) === idx && transcript.usage[b.item.turnId];
          return out ? <div key={`b${bi}`}>{out}{endOfTurn && b.item.kind !== "user" ? <div className="mt-1.5"><UsageLine usage={transcript.usage[b.item.turnId]} /></div> : null}</div> : null;
        }
        return (
          <HelperBlock key={`h${b.key}`} block={b}>
            {b.items.map((it, k) => (it.kind === "assistant" ? <div key={it.id} className="px-2 py-1 text-[14px]"><AssistantText item={it} /></div> : it.kind === "tool" ? (HIDDEN_TOOLS.has(it.name) ? null : <ToolRow key={it.id} item={it} />) : <span key={k} />))}
          </HelperBlock>
        );
      })}
      {working && !lastIsStreaming && <div className="flex items-center gap-2 px-1 text-[13px] text-muted" role="status"><span className="flex gap-0.5"><i className="dot" /><i className="dot" /><i className="dot" /></span>Working…</div>}
    </div>
  );
}
