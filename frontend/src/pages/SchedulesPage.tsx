import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { Schedule, ScheduleRun } from "../api/types";
import { Icon } from "../components/ui/Icon";
import { Empty, ErrorNote, PageShell } from "../components/ui/PageShell";
import { Toggle } from "../components/ui/Toggle";
import { CRON_PRESETS, describeCron, relativeTime, timeUntil } from "../lib/format";
import { useApp } from "../state/app";

const CUSTOM = "custom";
const zones = (): string[] => {
  try { return (Intl as any).supportedValuesOf("timeZone") as string[]; } catch { return ["UTC", "Europe/London", "Europe/Berlin", "America/New_York", "America/Los_Angeles", "Asia/Kolkata", "Asia/Tokyo", "Australia/Sydney"]; }
};
const localZone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; } };
const messageOf = (e: unknown) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Something went wrong.");

const STATUS: Record<string, { text: string; cls: string }> = {
  started: { text: "Ran", cls: "text-ok" }, skipped: { text: "Skipped", cls: "text-warn" }, failed: { text: "Failed", cls: "text-danger" }, "": { text: "Not run yet", cls: "text-faint" },
};

function ScheduleForm({ initial, onSaved, onCancel }: { initial: Schedule | null; onSaved: () => void; onCancel: () => void }) {
  const app = useApp();
  const [title, setTitle] = useState(initial?.title ?? "");
  const [instructions, setInstructions] = useState(initial?.instructions ?? "");
  const preset = CRON_PRESETS.find((p) => p.cron === initial?.cron)?.cron ?? (initial ? CUSTOM : CRON_PRESETS[1].cron);
  const [choice, setChoice] = useState(preset);
  const [custom, setCustom] = useState(initial?.cron ?? "");
  const [tz, setTz] = useState(initial?.timezone ?? localZone());
  const [model, setModel] = useState(initial?.model ?? "");
  const [unattended, setUnattended] = useState(Boolean(initial?.auto_approve));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const allZones = useMemo(() => Array.from(new Set([tz, "UTC", ...zones()])), [tz]);
  const cron = choice === CUSTOM ? custom : choice;

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      const body = { title, instructions, cron, timezone: tz, model: model || null, auto_approve: unattended ? true : null };
      if (initial) await api.scheduleUpdate(initial.id, body);
      else await api.scheduleCreate({ ...body, auto_approve: unattended ? true : undefined, model: model || undefined });
      onSaved();
    } catch (e) { setError(messageOf(e)); } finally { setBusy(false); }
  };

  return (
    <div className="card mb-6 p-5">
      <h2 className="mb-4 text-[16px] font-semibold">{initial ? "Edit scheduled task" : "New scheduled task"}</h2>
      <ErrorNote message={error} />
      <div className="space-y-4">
        <div><label className="label" htmlFor="s-title">Name</label><input id="s-title" className="input" value={title} maxLength={80} onChange={(e) => setTitle(e.target.value)} placeholder="Morning news brief" /></div>
        <div><label className="label" htmlFor="s-instr">What should it do?</label>
          <textarea id="s-instr" className="input min-h-[110px]" value={instructions} onChange={(e) => setInstructions(e.target.value)} placeholder="Search for the most important AI news from the last day and write a short brief with sources." /></div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div><label className="label" htmlFor="s-when">How often</label>
            <select id="s-when" className="input" value={choice} onChange={(e) => setChoice(e.target.value)}>
              {CRON_PRESETS.map((p) => <option key={p.cron} value={p.cron}>{p.label}</option>)}<option value={CUSTOM}>Custom (cron expression)…</option>
            </select>
            {choice === CUSTOM && <input aria-label="Cron expression" className="input mt-2 font-mono" value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="0 9 * * 1-5" />}
          </div>
          <div><label className="label" htmlFor="s-tz">Time zone</label><select id="s-tz" className="input" value={tz} onChange={(e) => setTz(e.target.value)}>{allZones.map((z) => <option key={z}>{z}</option>)}</select></div>
        </div>
        <div><label className="label" htmlFor="s-model">Model</label>
          <select id="s-model" className="input" value={model} onChange={(e) => setModel(e.target.value)}><option value="">The default ({app.modelById(app.defaultModel)?.label})</option>{app.models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}</select></div>
        <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-line bg-paper p-3.5">
          <Toggle label="Run without asking me" checked={unattended} onChange={setUnattended} />
          <span><span className="block text-[13.5px] font-medium">Allow it to run commands and change GitHub without asking</span>
            <span className="block text-[12.5px] text-muted">Nobody is watching when this runs. Without this, anything that needs your approval waits for you in the conversation. Email, plans and questions always wait.</span></span>
        </label>
      </div>
      <div className="mt-5 flex gap-2"><button type="button" className="btn-accent" onClick={save} disabled={busy || !title.trim() || !instructions.trim() || !cron.trim()}>{initial ? "Save changes" : "Create"}</button><button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button></div>
    </div>
  );
}

function History({ id }: { id: string }) {
  const [runs, setRuns] = useState<ScheduleRun[] | null>(null);
  useEffect(() => { api.scheduleRuns(id).then((r) => setRuns(r.runs)).catch(() => setRuns([])); }, [id]);
  if (runs === null) return <p className="px-4 py-3 text-[12.5px] text-faint">Loading…</p>;
  if (runs.length === 0) return <p className="px-4 py-3 text-[12.5px] text-faint">It has not run yet.</p>;
  return (
    <ul className="divide-y divide-line border-t border-line">
      {runs.map((r) => (
        <li key={r.id} className="flex items-center gap-3 px-4 py-2 text-[13px]">
          <span className={`w-16 font-medium ${(STATUS[r.status] ?? STATUS[""]).cls}`}>{(STATUS[r.status] ?? STATUS[""]).text}</span>
          <span className="w-20 text-faint">{r.trigger === "manual" ? "by hand" : r.trigger === "catchup" ? "catch-up" : "on schedule"}</span>
          <span className="w-28 text-faint">{relativeTime(r.created_at)}</span>
          <span className="min-w-0 flex-1 truncate text-muted">{r.note}</span>
          {r.session_id && <Link to={`/chat/${r.session_id}`} className="btn-ghost !py-0.5">Open</Link>}
        </li>
      ))}
    </ul>
  );
}

export function SchedulesPage() {
  const [items, setItems] = useState<Schedule[] | null>(null);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<Schedule | "new" | null>(null);
  const [history, setHistory] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(() => api.schedules().then((r) => setItems(r.schedules)).catch((e) => setError(messageOf(e))), []);
  useEffect(() => { void load(); }, [load]);

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id); setError("");
    try { await fn(); await load(); } catch (e) { setError(messageOf(e)); } finally { setBusy(null); }
  };

  return (
    <PageShell title="Scheduled" subtitle="Tasks privai runs by itself on a schedule. Each run is its own conversation, so you can open it and see exactly what happened."
      actions={<button type="button" className="btn-primary" onClick={() => setEditing("new")}><Icon name="plus" size={14} />New task</button>}>
      <ErrorNote message={error} onDismiss={() => setError("")} />
      {editing && <ScheduleForm initial={editing === "new" ? null : editing} onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); void load(); }} />}
      {items === null && !error && <p className="text-[13px] text-faint" role="status">Loading…</p>}
      {items?.length === 0 && !editing && <Empty title="Nothing scheduled">Set up a morning brief, a weekly report, or a watch on something. It runs while you are away, and anything that needs your approval waits for you.</Empty>}
      <ul className="space-y-3">
        {items?.map((s) => {
          const st = STATUS[s.last_status] ?? STATUS[""];
          return (
            <li key={s.id} className="card overflow-hidden">
              <div className="flex items-start gap-3 p-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2"><h3 className="truncate text-[15px] font-semibold">{s.title}</h3>{!s.enabled && <span className="chip">paused</span>}{s.auto_approve && <span className="chip border-accent/30 bg-accentSoft text-accentInk">unattended</span>}</div>
                  <p className="mt-0.5 text-[13px] text-muted">{describeCron(s.cron)} · {s.timezone}</p>
                  <p className="mt-1.5 line-clamp-2 text-[13px] text-muted">{s.instructions}</p>
                  <p className="mt-2 text-[12.5px] text-faint">{s.enabled ? <>Next run <b className="font-medium text-muted">{timeUntil(s.next_run_at)}</b> · </> : null}<span className={st.cls}>{st.text}</span>{s.last_run_at ? ` ${relativeTime(s.last_run_at)}` : ""} · {s.run_count} run{s.run_count === 1 ? "" : "s"}</p>
                </div>
                <Toggle label={`Enable ${s.title}`} checked={s.enabled} disabled={busy === s.id}
                  onChange={(v) => { setItems((list) => list && list.map((x) => (x.id === s.id ? { ...x, enabled: v } : x))); void act(s.id, () => api.scheduleUpdate(s.id, { enabled: v })); }} />
              </div>
              <div className="flex flex-wrap gap-1.5 border-t border-line bg-paper/60 px-3 py-2">
                <button type="button" className="btn-ghost" disabled={busy === s.id} onClick={() => act(s.id, () => api.scheduleRun(s.id))}><Icon name="play" size={13} />Run now</button>
                <button type="button" className="btn-ghost" onClick={() => setHistory(history === s.id ? null : s.id)} aria-expanded={history === s.id}><Icon name="clock" size={13} />History</button>
                <button type="button" className="btn-ghost" onClick={() => setEditing(s)}><Icon name="edit" size={13} />Edit</button>
                <button type="button" className="btn-ghost ml-auto text-danger" onClick={() => { if (window.confirm(`Delete "${s.title}"? Its past conversations stay.`)) void act(s.id, () => api.scheduleDelete(s.id)); }}><Icon name="trash" size={13} />Delete</button>
              </div>
              {history === s.id && <History id={s.id} />}
            </li>
          );
        })}
      </ul>
    </PageShell>
  );
}
