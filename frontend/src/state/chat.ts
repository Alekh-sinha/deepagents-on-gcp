// One conversation on screen: loads it, follows the live turn, sends messages, answers approvals, stops a turn.
import { useCallback, useEffect, useReducer, useRef } from "react";
import { ApiError, api } from "../api/client";
import { followTurn } from "../api/stream";
import type { Approval, AutoApprove, Decision, EngineEvent, Session, SessionView, Transcript, Turn, TurnStatus } from "../api/types";
import { useApp } from "./app";
import { applyEvent, emptyTranscript, fromTurns, lastSeqOf, reconcileApprovals, withLocalMessage, withoutLocalMessages, type TranscriptState } from "./transcript";

export interface ChatState {
  session: Session | null;
  transcript: TranscriptState;
  pending: Approval[];
  active: { id: string; status: TurnStatus } | null;
  loading: boolean;
  error: string | null;
  notice: string | null;
}

type Action =
  | { type: "reset"; loading: boolean }
  | { type: "loaded"; view: SessionView; transcript: TranscriptState }
  | { type: "view"; view: SessionView }
  | { type: "event"; turnId: string; ev: EngineEvent }
  | { type: "local"; text: string; nonce: number }
  | { type: "unlocal" }
  | { type: "session"; session: Session }
  | { type: "active"; turn: Turn | null }
  | { type: "removePending"; id: string }
  | { type: "error"; message: string | null }
  | { type: "notice"; message: string | null };

const initial: ChatState = { session: null, transcript: emptyTranscript(), pending: [], active: null, loading: false, error: null, notice: null };

const activeOf = (view: SessionView): ChatState["active"] => {
  const turn = view.turns.find((t) => t.id === view.active_turn_id);
  return turn ? { id: turn.id, status: turn.status } : null;
};

export function reducer(s: ChatState, a: Action): ChatState {
  switch (a.type) {
    case "reset": return { ...initial, loading: a.loading };
    case "loaded": {
      const ids = new Set(a.view.pending_approvals.map((p) => p.id));
      const transcript = { ...a.transcript, items: reconcileApprovals(a.transcript.items, ids), todos: a.transcript.todos ?? a.view.todos };
      return { ...s, session: a.view.session, transcript, pending: a.view.pending_approvals, active: activeOf(a.view), loading: false, error: null };
    }
    case "view": {
      const ids = new Set(a.view.pending_approvals.map((p) => p.id));
      return { ...s, session: a.view.session, pending: a.view.pending_approvals, active: activeOf(a.view), transcript: { ...s.transcript, items: reconcileApprovals(s.transcript.items, ids) } };
    }
    case "event": return { ...s, transcript: applyEvent(s.transcript, a.turnId, a.ev), notice: null };
    case "local": return { ...s, transcript: withLocalMessage(s.transcript, a.text, a.nonce), error: null };
    case "unlocal": return { ...s, transcript: withoutLocalMessages(s.transcript) };
    case "session": return { ...s, session: a.session };
    case "active": return { ...s, active: a.turn ? { id: a.turn.id, status: a.turn.status } : null };
    case "removePending": return { ...s, pending: s.pending.filter((p) => p.id !== a.id) };
    case "error": return { ...s, error: a.message };
    case "notice": return { ...s, notice: a.message };
  }
}

const messageOf = (e: unknown): string => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Something went wrong.");

export function useChat(routeSessionId: string | null, onCreated: (id: string) => void) {
  const app = useApp();
  const [state, dispatch] = useReducer(reducer, initial);
  const stateRef = useRef(state);
  stateRef.current = state;
  const currentId = useRef<string | null>(null);   // the conversation this hook is showing right now
  const stop = useRef<(() => void) | null>(null);
  const nonce = useRef(0);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; stop.current?.(); }; }, []);   // set on mount too: StrictMode mounts, unmounts and mounts again

  const closeStream = () => { stop.current?.(); stop.current = null; };

  const settle = useCallback(async (id: string) => {
    try {
      const view = await api.session(id);
      if (!mounted.current || currentId.current !== id) return;
      dispatch({ type: "view", view });
      app.upsertSession(view.session);
      const active = activeOf(view);
      if (active?.status === "running") follow(active.id, lastSeqOf(stateRef.current.transcript, active.id), id);   // the stream was only recycled: carry on
      else { void app.refreshSessions(); void app.refreshPending(); }
    } catch (e) {
      if (mounted.current && currentId.current === id) dispatch({ type: "error", message: messageOf(e) });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const follow = useCallback((turnId: string, afterSeq: number, sessionId: string) => {
    closeStream();
    stop.current = followTurn(turnId, afterSeq, {
      onEvent: (ev) => dispatch({ type: "event", turnId, ev }),
      onEnd: () => { void settle(sessionId); },
      onError: (info) => { dispatch({ type: "notice", message: "The live connection dropped. Reconnecting…" }); setTimeout(() => { void settle(sessionId); }, 1500); void info; },
    });
  }, [settle]);

  const load = useCallback(async (id: string) => {
    closeStream();
    currentId.current = id;
    dispatch({ type: "reset", loading: true });
    try {
      const [view, tr]: [SessionView, Transcript] = await Promise.all([api.session(id), api.transcript(id, 50)]);
      if (!mounted.current || currentId.current !== id) return;
      const transcript = fromTurns(tr.turns);
      dispatch({ type: "loaded", view, transcript });
      const active = activeOf(view);
      if (active?.status === "running") follow(active.id, lastSeqOf(transcript, active.id), id);
    } catch (e) {
      if (mounted.current && currentId.current === id) { dispatch({ type: "reset", loading: false }); dispatch({ type: "error", message: messageOf(e) }); }
    }
  }, [follow]);

  // the route decides which conversation is shown; one this hook just created itself is already on screen
  useEffect(() => {
    if (routeSessionId === currentId.current) return;
    if (routeSessionId) void load(routeSessionId);
    else { closeStream(); currentId.current = null; dispatch({ type: "reset", loading: false }); }
  }, [routeSessionId, load]);

  const send = useCallback(async (text: string) => {
    const clean = text.trim();
    if (!clean) return;
    dispatch({ type: "local", text: clean, nonce: ++nonce.current });
    try {
      let id = currentId.current;
      if (!id) {
        const created = await api.createSession({ model: app.newChatModel, ...(app.newChatAuto ? { auto_approve: app.newChatAuto } : {}) });
        id = created.id;
        currentId.current = id;
        dispatch({ type: "session", session: created });
        app.upsertSession(created);
        onCreated(id);
      }
      const turn = await api.send(id, clean);
      dispatch({ type: "active", turn });
      follow(turn.id, 0, id);
    } catch (e) {
      dispatch({ type: "unlocal" });
      dispatch({ type: "error", message: messageOf(e) });
      if (currentId.current) void settle(currentId.current);
    }
  }, [app, follow, onCreated, settle]);

  const decide = useCallback(async (approvalId: string, decisions: Decision[]) => {
    const id = currentId.current;
    if (!id) return;
    try {
      const turn = await api.decide(approvalId, decisions);
      dispatch({ type: "removePending", id: approvalId });
      void app.refreshPending();
      dispatch({ type: "active", turn });
      follow(turn.id, lastSeqOf(stateRef.current.transcript, turn.id), id);
    } catch (e) {
      dispatch({ type: "error", message: messageOf(e) });
      void settle(id);   // most likely it was already answered or has expired: show what is true now
    }
  }, [app, follow, settle]);

  const cancel = useCallback(async () => {
    const a = stateRef.current.active;
    if (!a) return;
    try { await api.cancel(a.id); } catch (e) { dispatch({ type: "error", message: messageOf(e) }); }
  }, []);

  const setAutoApprove = useCallback(async (value: AutoApprove) => {
    const id = currentId.current;
    if (!id) { app.setNewChatAuto(value); return; }
    try {
      const session = await api.updateSession(id, { auto_approve: value });
      dispatch({ type: "session", session });
      app.upsertSession(session);
    } catch (e) { dispatch({ type: "error", message: messageOf(e) }); }
  }, [app]);

  const setModel = useCallback(async (model: string) => {
    app.setNewChatModel(model);   // remembered for the next new chat too
    const id = currentId.current;
    if (!id) return;
    try {
      const session = await api.updateSession(id, { model });
      dispatch({ type: "session", session });
      app.upsertSession(session);
    } catch (e) { dispatch({ type: "error", message: messageOf(e) }); }
  }, [app]);

  const dismissError = useCallback(() => dispatch({ type: "error", message: null }), []);

  return { state, send, decide, cancel, setAutoApprove, setModel, dismissError, reload: () => (currentId.current ? load(currentId.current) : Promise.resolve()) };
}
