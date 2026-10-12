import { useMemo, useState } from "react";
import type { Approval, ApprovalItem, Decision } from "../../api/types";
import type { Item } from "../../state/transcript";
import { Icon } from "../ui/Icon";
import { ItemBody, KIND_ICON, itemSummary } from "./bodies";

type ApprovalRow = Extract<Item, { kind: "approval" }>;

/** What the person has chosen for one item, before anything is sent. */
type Choice = { kind: "approve" } | { kind: "reject"; message: string } | { kind: "respond"; message: string } | { kind: "edit"; args: Record<string, any> } | null;

export function toDecision(item: ApprovalItem, choice: NonNullable<Choice>): Decision {
  switch (choice.kind) {
    case "approve": return { type: "approve" };
    case "reject": return choice.message.trim() ? { type: "reject", message: choice.message.trim() } : { type: "reject" };
    case "respond": return { type: "respond", message: choice.message };
    case "edit": return { type: "edit", edited_action: { name: item.tool, args: choice.args } };
  }
}

const allows = (item: ApprovalItem, d: string) => (item.allowed_decisions ?? []).includes(d as any);

// ── editing ──────────────────────────────────────────────────────────────

function Editor({ item, request, onApply, onCancel }: { item: ApprovalItem; request: Record<string, any>; onApply: (args: Record<string, any>) => void; onCancel: () => void }) {
  const [args, setArgs] = useState<Record<string, any>>(() => structuredClone(request));
  const [json, setJson] = useState(() => JSON.stringify(request, null, 2));
  const [error, setError] = useState("");
  const set = (k: string, v: unknown) => setArgs((a) => ({ ...a, [k]: v }));

  const apply = () => {
    if (item.kind === "shell") return onApply({ ...args, command: String(args.command ?? "") });
    if (item.kind === "email") return onApply(args);
    if (item.kind === "plan") return onApply({ ...args, tasks: (args.tasks ?? []).filter((t: any) => String(t.title ?? "").trim()) });
    try {
      const parsed = JSON.parse(json);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("must be a JSON object");
      onApply(parsed);
    } catch (e) { setError(`Not valid JSON: ${(e as Error).message}`); }
  };

  return (
    <div className="mt-3 space-y-2 rounded-lg border border-accent/30 bg-accentSoft/40 p-3">
      <div className="text-[12px] font-medium text-accentInk">Edit before approving</div>
      {item.kind === "shell" && <textarea aria-label="Command" className="input min-h-[72px] font-mono text-[13px]" value={String(args.command ?? "")} onChange={(e) => set("command", e.target.value)} />}
      {item.kind === "email" && (
        <>
          <input aria-label="Subject" className="input" value={String(args.subject ?? "")} onChange={(e) => set("subject", e.target.value)} />
          <textarea aria-label="Body" className="input min-h-[140px]" value={String(args.body ?? "")} onChange={(e) => set("body", e.target.value)} />
        </>
      )}
      {item.kind === "plan" && (
        <div className="space-y-2">
          <input aria-label="Summary" className="input" value={String(args.summary ?? "")} onChange={(e) => set("summary", e.target.value)} />
          {(args.tasks ?? []).map((t: any, i: number) => (
            <div key={i} className="flex items-start gap-2">
              <div className="min-w-0 flex-1 space-y-1">
                <input aria-label={`Step ${i + 1} title`} className="input" value={String(t.title ?? "")} onChange={(e) => set("tasks", args.tasks.map((x: any, j: number) => (j === i ? { ...x, title: e.target.value } : x)))} />
                <input aria-label={`Step ${i + 1} detail`} className="input text-[13px]" value={String(t.detail ?? "")} onChange={(e) => set("tasks", args.tasks.map((x: any, j: number) => (j === i ? { ...x, detail: e.target.value } : x)))} />
              </div>
              <button type="button" className="btn-ghost mt-1" aria-label={`Remove step ${i + 1}`} onClick={() => set("tasks", args.tasks.filter((_: any, j: number) => j !== i))}><Icon name="trash" /></button>
            </div>
          ))}
        </div>
      )}
      {!["shell", "email", "plan"].includes(item.kind) && (
        <>
          <textarea aria-label="Arguments (JSON)" className="input min-h-[140px] font-mono text-[12.5px]" value={json} onChange={(e) => { setJson(e.target.value); setError(""); }} />
          {error && <p className="text-[12px] text-danger">{error}</p>}
        </>
      )}
      <div className="flex gap-2">
        <button type="button" className="btn-accent" onClick={apply}>Approve with my changes</button>
        <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

// ── one card ─────────────────────────────────────────────────────────────

function ItemCard({ item, request, choice, onChoose, single, onSubmitNow }: {
  item: ApprovalItem; request: Record<string, any>; choice: Choice; onChoose: (c: Choice) => void; single: boolean; onSubmitNow: (c: NonNullable<Choice>) => void;
}) {
  const [mode, setMode] = useState<"idle" | "edit" | "reject">("idle");
  const [text, setText] = useState("");
  const answerKind = item.kind === "question" || item.kind === "interrupt";
  const pick = (c: NonNullable<Choice>) => (single ? onSubmitNow(c) : onChoose(c));
  const options: string[] = Array.isArray(item.preview?.options) ? item.preview.options.map(String) : [];

  return (
    <div className="rounded-xl border border-line bg-panel p-3.5">
      <div className="mb-2.5 flex items-center gap-2">
        <span className="flex h-6 w-6 items-center justify-center rounded-md bg-accentSoft text-accentInk"><Icon name={KIND_ICON[item.kind] ?? "tool"} size={14} /></span>
        <span className="text-[13.5px] font-semibold">{item.title}</span>
        {choice && !single && <span className="chip ml-auto border-accent/30 bg-accentSoft text-accentInk">{choice.kind === "edit" ? "approve with changes" : choice.kind}</span>}
      </div>
      <ItemBody item={item} />

      {answerKind ? (
        <div className="mt-3 space-y-2">
          {options.length > 0 && <div className="flex flex-wrap gap-1.5">{options.map((o) => <button key={o} type="button" className="btn-outline" onClick={() => setText(o)}>{o}</button>)}</div>}
          <textarea aria-label={item.kind === "question" ? "Your answer" : "Your reply"} className="input min-h-[72px]" placeholder={item.kind === "question" ? "Type your answer…" : "Type your reply…"} value={text} onChange={(e) => setText(e.target.value)} />
          <button type="button" className="btn-accent" disabled={!text.trim()} onClick={() => pick({ kind: "respond", message: text.trim() })}>{single ? "Send" : "Use this answer"}</button>
        </div>
      ) : mode === "edit" ? (
        <Editor item={item} request={request} onCancel={() => setMode("idle")} onApply={(args) => { setMode("idle"); pick({ kind: "edit", args }); }} />
      ) : mode === "reject" ? (
        <div className="mt-3 space-y-2">
          <input aria-label="Why not? (optional)" className="input" placeholder="Tell the assistant why (optional)" value={text} onChange={(e) => setText(e.target.value)} autoFocus />
          <div className="flex gap-2"><button type="button" className="btn-danger" onClick={() => pick({ kind: "reject", message: text })}>Reject</button><button type="button" className="btn-ghost" onClick={() => setMode("idle")}>Cancel</button></div>
        </div>
      ) : (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {allows(item, "approve") && <button type="button" className="btn-accent" aria-pressed={choice?.kind === "approve"} onClick={() => pick({ kind: "approve" })}><Icon name="check" size={14} />{single ? "Approve" : "Approve this"}</button>}
          {allows(item, "edit") && <button type="button" className="btn-outline" onClick={() => setMode("edit")}><Icon name="edit" size={14} />Edit</button>}
          {allows(item, "reject") && <button type="button" className="btn-ghost" onClick={() => { setText(""); setMode("reject"); }}>Reject</button>}
        </div>
      )}
    </div>
  );
}

// ── the group ────────────────────────────────────────────────────────────

export function ApprovalGroup({ row, approval, onDecide, busy }: { row: ApprovalRow; approval?: Approval; onDecide: (approvalId: string, decisions: Decision[]) => void; busy?: boolean }) {
  const items = approval?.previews?.length ? approval.previews : row.items;
  const requests = approval?.action_requests ?? [];
  const [choices, setChoices] = useState<Choice[]>(() => items.map(() => null));
  const decidedAll = choices.every(Boolean);
  const canApproveAll = useMemo(() => items.every((i) => allows(i, "approve")), [items]);

  if (row.state !== "pending" || !approval) return <DecidedRow row={row} />;
  const send = (cs: Choice[]) => onDecide(approval.id, cs.map((c, i) => toDecision(items[i], c!)));
  const single = items.length === 1;

  return (
    <section className="rounded-2xl border border-warn/40 bg-warnSoft/50 p-3 shadow-card" aria-label="Waiting for your decision">
      <header className="mb-2.5 flex items-center gap-2 px-1">
        <span className="pulse h-2 w-2 rounded-full bg-warn" />
        <h3 className="text-[13.5px] font-semibold text-ink">{single ? "Waiting for your decision" : `${items.length} things are waiting for your decision`}</h3>
        {!single && canApproveAll && <button type="button" className="btn-outline ml-auto" disabled={busy} onClick={() => send(items.map(() => ({ kind: "approve" as const })))}>Approve all {items.length}</button>}
      </header>
      <div className="space-y-2.5">
        {items.map((item, i) => (
          <ItemCard key={i} item={item} request={requests[i]?.args ?? {}} single={single} choice={choices[i]}
            onChoose={(c) => setChoices((cs) => cs.map((x, j) => (j === i ? c : x)))} onSubmitNow={(c) => send([c])} />
        ))}
      </div>
      {!single && (
        <div className="mt-3 flex items-center gap-3 px-1">
          <button type="button" className="btn-accent" disabled={!decidedAll || busy} onClick={() => send(choices)}>Send my answers</button>
          <span className="text-[12px] text-muted">{choices.filter(Boolean).length} of {items.length} answered</span>
        </div>
      )}
    </section>
  );
}

const STATE_STYLE = {
  approved: { icon: "check", text: "Approved", cls: "text-ok" },
  rejected: { icon: "x", text: "Rejected", cls: "text-danger" },
  expired: { icon: "clock", text: "Expired without an answer", cls: "text-faint" },
  pending: { icon: "clock", text: "Waiting", cls: "text-warn" },
} as const;

function DecidedRow({ row }: { row: ApprovalRow }) {
  const [open, setOpen] = useState(false);
  const style = STATE_STYLE[row.state];
  return (
    <div className="rounded-xl border border-line bg-paper">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <Icon name={style.icon as any} size={14} className={style.cls} />
        <span className={`text-[13px] font-medium ${style.cls}`}>{style.text}</span>
        <span className="min-w-0 flex-1 truncate text-[13px] text-muted">{row.items.map((i) => itemSummary(i) || i.title).join(" · ")}</span>
        <Icon name={open ? "chevronUp" : "chevronDown"} size={14} className="text-faint" />
      </button>
      {open && (
        <div className="space-y-3 border-t border-line px-3 py-3">
          {row.items.map((item, i) => (
            <div key={i}>
              <div className="mb-1.5 text-[12.5px] font-medium text-muted">{item.title}{row.decisions[i] ? ` — ${row.decisions[i].type}${row.decisions[i].message ? `: ${row.decisions[i].message}` : ""}` : ""}</div>
              <ItemBody item={item} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function AutoApprovedRow({ items }: { items: ApprovalItem[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-xl border border-accent/20 bg-accentSoft/40">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <Icon name="bolt" size={14} className="text-accentInk" />
        <span className="text-[13px] font-medium text-accentInk">Approved automatically</span>
        <span className="min-w-0 flex-1 truncate text-[13px] text-muted">{items.map((i) => itemSummary(i) || i.title).join(" · ")}</span>
        <Icon name={open ? "chevronUp" : "chevronDown"} size={14} className="text-faint" />
      </button>
      {open && <div className="space-y-3 border-t border-accent/20 px-3 py-3">{items.map((item, i) => <div key={i}><div className="mb-1.5 text-[12.5px] font-medium text-muted">{item.title}</div><ItemBody item={item} /></div>)}</div>}
    </div>
  );
}
