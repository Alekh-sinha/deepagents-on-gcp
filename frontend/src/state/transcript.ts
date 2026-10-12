// Turns the engine's event log into what the chat shows. Pure functions: the same events always give the same transcript, whether they arrive live,
// are replayed after a reconnect, or are loaded all at once. Events at or below a turn's last seen sequence number are ignored (replays are harmless).
import type { ApprovalItem, EngineEvent, Todo, TranscriptTurn } from "../api/types";

export interface DecisionSummary { type: string; message?: string }

export type Item =
  | { kind: "user"; id: string; turnId: string; text: string }
  | { kind: "assistant"; id: string; turnId: string; agent: string; ns: string; text: string; streaming: boolean; truncated: boolean }
  | { kind: "tool"; id: string; turnId: string; agent: string; ns: string; name: string; args: Record<string, any>; status: "running" | "success" | "error"; result: string; resultTruncated: boolean; edited: boolean }
  | { kind: "approval"; id: string; turnId: string; items: ApprovalItem[]; state: "pending" | "approved" | "rejected" | "expired"; decisions: DecisionSummary[]; expiresAt: string | null }
  | { kind: "auto_approval"; id: string; turnId: string; items: ApprovalItem[] }
  | { kind: "artifact"; id: string; turnId: string; artifactId: string; name: string; path: string; size: number; contentType: string }
  | { kind: "notice"; id: string; turnId: string; text: string }
  | { kind: "status"; id: string; turnId: string; status: "completed" | "cancelled" | "failed" | "blocked"; text: string };

export interface Usage { input: number; output: number; calls: number; models: string[] }

export interface TranscriptState {
  items: Item[];
  todos: Todo[] | null;
  usage: Record<string, Usage>;
  lastSeq: Record<string, number>;
  /** the model each helper run actually used, by stream namespace (from the usage events), so a helper block can name it */
  nsModel: Record<string, string>;
}

export const emptyTranscript = (): TranscriptState => ({ items: [], todos: null, usage: {}, lastSeq: {}, nsModel: {} });

export const LOCAL_PREFIX = "local:";
const replace = <T>(list: T[], index: number, value: T): T[] => list.map((x, i) => (i === index ? value : x));
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

export function applyEvent(state: TranscriptState, turnId: string, ev: EngineEvent): TranscriptState {
  if (ev.seq <= (state.lastSeq[turnId] ?? 0)) return state;
  const next: TranscriptState = { ...state, lastSeq: { ...state.lastSeq, [turnId]: ev.seq } };
  const p = ev.payload ?? {};
  const key = `${turnId}:${ev.seq}`;
  const agent = String(p.agent ?? "assistant");
  const ns = String(p.ns ?? "");

  switch (ev.type) {
    case "user_message": {
      const text = String(p.text ?? "");
      const local = state.items.findIndex((i) => i.kind === "user" && i.id.startsWith(LOCAL_PREFIX) && i.text === text);
      const item: Item = { kind: "user", id: key, turnId, text };
      next.items = local >= 0 ? replace(state.items, local, item) : [...state.items, item];   // the message the user just typed, now confirmed by the server
      return next;
    }

    case "token": {
      const mid = String(p.message_id ?? key);
      const at = state.items.findIndex((i) => i.kind === "assistant" && i.id === mid);
      const text = String(p.text ?? "");
      if (at >= 0) {
        const cur = state.items[at] as Extract<Item, { kind: "assistant" }>;
        next.items = replace(state.items, at, { ...cur, text: cur.text + text, streaming: true });
      } else {
        next.items = [...state.items, { kind: "assistant", id: mid, turnId, agent, ns, text, streaming: true, truncated: false }];
      }
      return next;
    }

    case "assistant_message": {
      const mid = String(p.message_id ?? key);
      const item: Item = { kind: "assistant", id: mid, turnId, agent, ns, text: String(p.text ?? ""), streaming: false, truncated: Boolean(p.truncated) };
      const at = state.items.findIndex((i) => i.kind === "assistant" && i.id === mid);
      next.items = at >= 0 ? replace(state.items, at, item) : [...state.items, item];
      return next;
    }

    case "tool_call": {
      const id = String(p.tool_call_id ?? key);
      const at = state.items.findIndex((i) => i.kind === "tool" && i.id === id);
      const args = (p.args ?? {}) as Record<string, any>;
      if (at >= 0) {
        const cur = state.items[at] as Extract<Item, { kind: "tool" }>;
        next.items = replace(state.items, at, { ...cur, args, edited: Boolean(p.edited) || cur.edited });
      } else {
        next.items = [...state.items, { kind: "tool", id, turnId, agent, ns, name: String(p.name ?? "tool"), args, status: "running", result: "", resultTruncated: false, edited: Boolean(p.edited) }];
      }
      return next;
    }

    case "tool_result": {
      const id = String(p.tool_call_id ?? "");
      const at = state.items.findIndex((i) => i.kind === "tool" && i.id === id);
      const status = p.status === "error" ? "error" : "success";
      if (at >= 0) {
        const cur = state.items[at] as Extract<Item, { kind: "tool" }>;
        next.items = replace(state.items, at, { ...cur, status, result: String(p.preview ?? ""), resultTruncated: Boolean(p.truncated) });
      } else {  // a result whose call was not seen (replay from the middle): still show it
        next.items = [...state.items, { kind: "tool", id: id || key, turnId, agent, ns, name: String(p.name ?? "tool"), args: {}, status, result: String(p.preview ?? ""), resultTruncated: Boolean(p.truncated), edited: false }];
      }
      return next;
    }

    case "todos":
      next.todos = Array.isArray(p.todos) ? (p.todos as Todo[]) : null;
      return next;

    case "interrupt": {
      const id = String(p.approval_id ?? key);
      if (state.items.some((i) => i.kind === "approval" && i.id === id)) return next;
      next.items = [...state.items, { kind: "approval", id, turnId, items: (p.items ?? []) as ApprovalItem[], state: "pending", decisions: [], expiresAt: p.expires_at ? String(p.expires_at) : null }];
      return next;
    }

    case "approval_decided": {
      const id = String(p.approval_id ?? "");
      const at = state.items.findIndex((i) => i.kind === "approval" && i.id === id);
      if (at < 0) return next;
      const cur = state.items[at] as Extract<Item, { kind: "approval" }>;
      next.items = replace(state.items, at, { ...cur, state: p.status === "rejected" ? "rejected" : "approved", decisions: (p.decisions ?? []) as DecisionSummary[] });
      return next;
    }

    case "approval_auto":
      next.items = [...state.items, { kind: "auto_approval", id: String(p.approval_id ?? key), turnId, items: (p.items ?? []) as ApprovalItem[] }];
      return next;

    case "artifact": {
      const id = String(p.artifact_id ?? key);
      if (state.items.some((i) => i.kind === "artifact" && i.artifactId === id)) return next;
      next.items = [...state.items, { kind: "artifact", id: key, turnId, artifactId: id, name: String(p.name ?? "file"), path: String(p.path ?? ""), size: num(p.size), contentType: String(p.content_type ?? "") }];
      return next;
    }

    case "notice":
      next.items = [...state.items, { kind: "notice", id: key, turnId, text: String(p.text ?? "") }];
      return next;

    case "usage": {
      const cur = state.usage[turnId] ?? { input: 0, output: 0, calls: 0, models: [] };
      const model = p.model ? String(p.model) : "";
      if (ns && model) next.nsModel = { ...state.nsModel, [ns]: model };
      next.usage = { ...state.usage, [turnId]: { input: cur.input + num(p.input_tokens), output: cur.output + num(p.output_tokens), calls: cur.calls + 1, models: model && !cur.models.includes(model) ? [...cur.models, model] : cur.models } };
      return next;
    }

    case "turn_done":
      next.items = [...settle(state.items, turnId), { kind: "status", id: key, turnId, status: p.status === "cancelled" ? "cancelled" : "completed", text: "" }];
      return next;

    case "turn_error":
      next.items = [...settle(state.items, turnId), { kind: "status", id: key, turnId, status: p.status === "blocked" ? "blocked" : "failed", text: String(p.error ?? "") }];
      return next;

    default:  // turn_started, heartbeat and anything newer: nothing to draw
      return next;
  }
}

/** When a turn ends, nothing of it is still "streaming" or "running" (a cancelled step never reports a result). */
function settle(items: Item[], turnId: string): Item[] {
  return items.map((i) => {
    if (i.turnId !== turnId) return i;
    if (i.kind === "assistant" && i.streaming) return { ...i, streaming: false };
    if (i.kind === "tool" && i.status === "running") return { ...i, status: "error", result: i.result || "Stopped before it finished." };
    return i;
  });
}

export function applyTurn(state: TranscriptState, t: TranscriptTurn): TranscriptState {
  return t.events.reduce((s, e) => applyEvent(s, t.turn.id, e), state);
}

export function fromTurns(turns: TranscriptTurn[]): TranscriptState {
  return turns.reduce(applyTurn, emptyTranscript());
}

/** An approval that the transcript still shows as pending but the server no longer lists as pending has expired (or was answered elsewhere). */
export function reconcileApprovals(items: Item[], pendingIds: Set<string>): Item[] {
  return items.map((i) => (i.kind === "approval" && i.state === "pending" && !pendingIds.has(i.id) ? { ...i, state: "expired" as const } : i));
}

export const lastSeqOf = (state: TranscriptState, turnId: string): number => state.lastSeq[turnId] ?? 0;

/** The user's own message shown the moment they press send, before the server has confirmed it. */
export function withLocalMessage(state: TranscriptState, text: string, nonce: number): TranscriptState {
  return { ...state, items: [...state.items, { kind: "user", id: `${LOCAL_PREFIX}${nonce}`, turnId: "", text }] };
}

export function withoutLocalMessages(state: TranscriptState): TranscriptState {
  return { ...state, items: state.items.filter((i) => !(i.kind === "user" && i.id.startsWith(LOCAL_PREFIX))) };
}
