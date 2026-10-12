import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import type { AutoApprove, Decision } from "../../api/types";
import { useApp } from "../../state/app";
import { useChat } from "../../state/chat";
import { MenuButton } from "../Sidebar";
import { Icon } from "../ui/Icon";
import { Composer } from "./Composer";
import { TodoPanel } from "./TodoPanel";
import { Transcript } from "./Transcript";

const EXAMPLES = [
  { icon: "globe", title: "Research something", text: "Research the latest results of a company I name, and give me a short summary with sources." },
  { icon: "file", title: "Write a document", text: "Write a one-page brief on a topic I describe and save it as a file." },
  { icon: "terminal", title: "Work with code", text: "Write a small Python script that does something useful, test it, and show me the result." },
  { icon: "mail", title: "Draft and send an email", text: "Draft an email to me summarising what we discussed, and ask me before sending it." },
] as const;

function EmptyState({ name, onPick }: { name: string; onPick: (text: string) => void }) {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col items-center justify-center px-4 pb-6 text-center">
      <div className="mb-5 flex h-12 w-12 items-center justify-center rounded-2xl bg-accent text-white shadow-card"><Icon name="sparkle" size={22} /></div>
      <h1 className="text-[26px] font-semibold tracking-tight">What should we work on{name ? `, ${name}` : ""}?</h1>
      <p className="mt-1.5 max-w-md text-[14.5px] text-muted">privai plans the work, uses its tools, and asks before it does anything that matters.</p>
      <div className="mt-8 grid w-full max-w-xl grid-cols-1 gap-2.5 sm:grid-cols-2">
        {EXAMPLES.map((e) => (
          <button key={e.title} type="button" onClick={() => onPick(e.text)} className="card flex items-start gap-3 p-3.5 text-left transition-colors hover:border-lineStrong hover:bg-paper">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accentSoft text-accentInk"><Icon name={e.icon} size={16} /></span>
            <span><span className="block text-[13.5px] font-medium">{e.title}</span><span className="mt-0.5 block text-[12.5px] leading-snug text-muted">{e.text}</span></span>
          </button>
        ))}
      </div>
    </div>
  );
}

export function ChatView({ sessionId }: { sessionId: string | null }) {
  const navigate = useNavigate();
  const app = useApp();
  const created = useCallback((id: string) => navigate(`/chat/${id}`, { replace: true }), [navigate]);
  const chat = useChat(sessionId, created);
  const { state } = chat;
  const [deciding, setDeciding] = useState(false);
  const [seed, setSeed] = useState<{ text: string; nonce: number } | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  const running = state.active?.status === "running";
  const waiting = state.pending.length > 0;
  const hasContent = state.transcript.items.length > 0;
  const name = app.user.split("@")[0];

  // keep the newest text in view, unless the reader scrolled up to read something
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [state.transcript.items, state.pending, running]);
  useEffect(() => { stick.current = true; }, [sessionId]);

  const decide = async (id: string, decisions: Decision[]) => { setDeciding(true); try { await chat.decide(id, decisions); } finally { setDeciding(false); } };
  const model = state.session?.model ?? (sessionId ? app.defaultModel : app.newChatModel);
  const auto: AutoApprove = state.session ? state.session.auto_approve : app.newChatAuto;
  const warm = () => { if (state.session && app.config.warm_sandbox.mode !== "off" && app.config.features?.sandbox !== false) void api.warm(state.session.id).catch(() => undefined); };
  const title = state.session?.title || (sessionId ? "Conversation" : "New chat");

  const archive = async () => {
    if (!state.session) return;
    await api.updateSession(state.session.id, { archived: true }).catch(() => undefined);
    await app.refreshSessions();
    navigate("/chat");
  };
  const rename = async (t: string) => {
    if (!state.session || !t.trim() || t.trim() === state.session.title) return;
    const s = await api.updateSession(state.session.id, { title: t.trim() }).catch(() => null);
    if (s) app.upsertSession(s);
  };

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col bg-canvas">
      <header className="flex h-12 shrink-0 items-center gap-3 border-b border-line px-4">
        <MenuButton />
        {state.session?.schedule_id && <span className="chip border-accent/30 bg-accentSoft text-accentInk"><Icon name="clock" size={11} />Scheduled</span>}
        <TitleEditor key={state.session?.id ?? "new"} title={title} editable={Boolean(state.session)} onSave={rename} />
        {state.session && <button type="button" className="btn-ghost ml-auto" onClick={archive} title="Archive this conversation"><Icon name="trash" size={14} />Archive</button>}
      </header>

      {state.error && (
        <div role="alert" className="mx-auto mt-3 flex w-full max-w-3xl items-start gap-2 rounded-xl border border-danger/30 bg-dangerSoft px-3.5 py-2.5 text-[13px] text-danger">
          <Icon name="alert" size={15} className="mt-0.5" /><span className="flex-1">{state.error}</span>
          <button type="button" aria-label="Dismiss" onClick={chat.dismissError}><Icon name="x" size={14} /></button>
        </div>
      )}
      {state.notice && <p role="status" className="mx-auto mt-3 text-[12.5px] text-faint">{state.notice}</p>}

      {state.loading ? (
        <div className="flex flex-1 items-center justify-center text-[13px] text-faint" role="status">Loading…</div>
      ) : hasContent ? (
        <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto" onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 140; }}>
          <Transcript transcript={state.transcript} pending={state.pending} working={running} onDecide={decide} deciding={deciding} />
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto"><EmptyState name={name} onPick={(text) => setSeed({ text, nonce: Date.now() })} /></div>
      )}

      <TodoPanel todos={state.transcript.todos} />
      <Composer
        onSend={(t) => void chat.send(t)} onStop={() => void chat.cancel()} onTyping={warm} running={running}
        blockedReason={waiting ? "Answer the request above to continue" : undefined}
        models={app.models} model={model} onModel={(id) => void chat.setModel(id)} helperChoice={app.helperChoice}
        autoApprove={auto} autoOptions={app.config.approvals.auto_approve} onAutoApprove={(v) => void chat.setAutoApprove(v)}
        autoFocus seed={seed} />
    </div>
  );
}

function TitleEditor({ title, editable, onSave }: { title: string; editable: boolean; onSave: (t: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(title);
  useEffect(() => setValue(title), [title]);
  if (editing) {
    return <input autoFocus aria-label="Conversation title" className="input !w-80 !py-1" value={value} maxLength={120} onChange={(e) => setValue(e.target.value)}
      onBlur={() => { setEditing(false); onSave(value); }} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") { setValue(title); setEditing(false); } }} />;
  }
  return <button type="button" disabled={!editable} onClick={() => setEditing(true)} title={editable ? "Rename" : undefined}
    className="min-w-0 truncate rounded-md px-1.5 py-0.5 text-[14px] font-medium hover:bg-paper disabled:cursor-default disabled:hover:bg-transparent">{title}</button>;
}
