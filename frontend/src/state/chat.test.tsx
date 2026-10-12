import { act, renderHook, waitFor } from "@testing-library/react";
import { StrictMode, type ReactNode } from "react";
import type { Approval, EngineEvent, Session, SessionView, Transcript, Turn } from "../api/types";

const api = vi.hoisted(() => ({
  session: vi.fn(), transcript: vi.fn(), createSession: vi.fn(), send: vi.fn(), decide: vi.fn(), cancel: vi.fn(), updateSession: vi.fn(),
}));
vi.mock("../api/client", () => ({ api, ApiError: class ApiError extends Error { constructor(public status: number, m: string) { super(m); } } }));

const streams: { turnId: string; after: number; h: any; stopped: boolean }[] = [];
vi.mock("../api/stream", () => ({
  followTurn: (turnId: string, after: number, h: any) => { const s = { turnId, after, h, stopped: false }; streams.push(s); return () => { s.stopped = true; }; },
}));

const app = vi.hoisted(() => ({ newChatModel: "m1", newChatAuto: null as any, setNewChatAuto: vi.fn(), setNewChatModel: vi.fn(), upsertSession: vi.fn(), refreshSessions: vi.fn(), refreshPending: vi.fn() }));
vi.mock("./app", () => ({ useApp: () => app }));

import { useChat } from "./chat";

const session = (over: Partial<Session> = {}): Session => ({ id: "s1", title: "T", model: null, auto_approve: null, schedule_id: "", ...over } as Session);
const turn = (over: Partial<Turn> = {}): Turn => ({ id: "t1", status: "running", session_id: "s1", ...over } as Turn);
const view = (over: Partial<SessionView> = {}): SessionView => ({ session: session(), turns: [], active_turn_id: null, pending_approvals: [], todos: null, ...over });
const transcript = (turns: Transcript["turns"] = []): Transcript => ({ session_id: "s1", turns, has_more: false });
const ev = (seq: number, type: string, payload: Record<string, any> = {}): EngineEvent => ({ seq, type, payload });

beforeEach(() => {
  streams.length = 0;
  Object.values(api).forEach((f) => f.mockReset());
  Object.values(app).forEach((f) => typeof f === "function" && (f as any).mockReset?.());
  app.newChatAuto = null;
  api.session.mockResolvedValue(view());
  api.transcript.mockResolvedValue(transcript());
});

const setup = (id: string | null, onCreated = vi.fn()) => ({ onCreated, ...renderHook(({ id }) => useChat(id, onCreated), { initialProps: { id }, wrapper: ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode> }) });
const items = (r: ReturnType<typeof setup>) => r.result.current.state.transcript.items;

describe("opening a conversation", () => {
  it("loads it and shows its history (also under React's double mounting, which once left it loading forever)", async () => {
    api.transcript.mockResolvedValue(transcript([{ turn: turn({ status: "completed" }), events: [ev(1, "user_message", { text: "hi" }), ev(2, "assistant_message", { message_id: "m1", text: "hello" })], truncated: false, next_seq: 2 }]));
    const r = setup("s1");
    await waitFor(() => expect(r.result.current.state.loading).toBe(false));
    expect(items(r).map((i) => i.kind)).toEqual(["user", "assistant"]);
    expect(r.result.current.state.session?.id).toBe("s1");
  });

  it("follows a turn that is still running, from where the history stopped", async () => {
    api.session.mockResolvedValue(view({ turns: [turn()], active_turn_id: "t1" }));
    api.transcript.mockResolvedValue(transcript([{ turn: turn(), events: [ev(1, "user_message", { text: "hi" }), ev(2, "token", { message_id: "m1", text: "par" })], truncated: false, next_seq: 2 }]));
    const r = setup("s1");
    await waitFor(() => expect(streams.length).toBeGreaterThan(0));
    expect(streams.at(-1)).toMatchObject({ turnId: "t1", after: 2 });
    act(() => streams.at(-1)!.h.onEvent(ev(3, "token", { message_id: "m1", text: "tial" })));
    expect((items(r).at(-1) as any).text).toBe("partial");
  });

  it("does not follow a turn that is waiting for an answer, and keeps the full request for the card", async () => {
    const pending: Approval = { id: "a1", run_id: "t1", status: "pending", expires_at: "", decided_by: null, previews: [], action_requests: [{ name: "execute", args: { command: "x" } }] };
    api.session.mockResolvedValue(view({ turns: [turn({ status: "awaiting_approval" })], active_turn_id: "t1", pending_approvals: [pending] }));
    const r = setup("s1");
    await waitFor(() => expect(r.result.current.state.pending).toHaveLength(1));
    expect(streams).toHaveLength(0);
  });

  it("switches cleanly to another conversation and stops following the old one", async () => {
    api.session.mockResolvedValue(view({ turns: [turn()], active_turn_id: "t1" }));
    const r = setup("s1");
    await waitFor(() => expect(streams.length).toBeGreaterThan(0));
    api.session.mockResolvedValue(view({ session: session({ id: "s2" }) }));
    r.rerender({ id: "s2" });
    await waitFor(() => expect(r.result.current.state.session?.id).toBe("s2"));
    expect(streams.every((s) => s.stopped)).toBe(true);
  });

  it("says what went wrong when a conversation cannot be opened", async () => {
    api.session.mockRejectedValue(new Error("session not found"));
    const r = setup("s9");
    await waitFor(() => expect(r.result.current.state.error).toBe("session not found"));
    expect(r.result.current.state.loading).toBe(false);
  });
});

describe("sending", () => {
  it("starts a conversation on the first message, shows it at once, then follows the answer", async () => {
    api.createSession.mockResolvedValue(session({ id: "new" }));
    api.send.mockResolvedValue(turn({ id: "t9", session_id: "new" }));
    const r = setup(null);
    await act(async () => { await r.result.current.send("  hello  "); });
    expect(api.createSession).toHaveBeenCalledWith({ model: "m1" });
    expect(api.send).toHaveBeenCalledWith("new", "hello");
    expect(r.onCreated).toHaveBeenCalledWith("new");
    expect(app.upsertSession).toHaveBeenCalled();
    expect(items(r)).toMatchObject([{ kind: "user", text: "hello" }]);
    expect(streams.at(-1)).toMatchObject({ turnId: "t9", after: 0 });
    act(() => streams.at(-1)!.h.onEvent(ev(1, "user_message", { text: "hello" })));
    expect(items(r)).toHaveLength(1);                                    // the typed message was replaced, not repeated
  });

  it("passes the automatic-approval choice made before the conversation existed", async () => {
    app.newChatAuto = { kinds: ["shell"], max_per_turn: 3 };
    api.createSession.mockResolvedValue(session({ id: "new" }));
    api.send.mockResolvedValue(turn());
    const r = setup(null);
    await act(async () => { await r.result.current.send("go"); });
    expect(api.createSession).toHaveBeenCalledWith({ model: "m1", auto_approve: { kinds: ["shell"], max_per_turn: 3 } });
  });

  it("takes the message back and says why when it could not be sent", async () => {
    api.send.mockRejectedValue(new Error("this session already has a turn in progress"));
    const r = setup("s1");
    await waitFor(() => expect(r.result.current.state.loading).toBe(false));
    await act(async () => { await r.result.current.send("hi"); });
    expect(items(r)).toEqual([]);
    expect(r.result.current.state.error).toBe("this session already has a turn in progress");
    act(() => r.result.current.dismissError());
    expect(r.result.current.state.error).toBeNull();
  });

  it("ignores a blank message", async () => {
    const r = setup("s1");
    await waitFor(() => expect(r.result.current.state.loading).toBe(false));
    await act(async () => { await r.result.current.send("   "); });
    expect(api.send).not.toHaveBeenCalled();
  });

  it("when the live stream ends it reads the conversation again (it may be waiting for an answer now)", async () => {
    api.send.mockResolvedValue(turn());
    const r = setup("s1");
    await waitFor(() => expect(r.result.current.state.loading).toBe(false));
    await act(async () => { await r.result.current.send("hi"); });
    const pending: Approval = { id: "a1", run_id: "t1", status: "pending", expires_at: "", decided_by: null, previews: [], action_requests: [] };
    api.session.mockResolvedValue(view({ turns: [turn({ status: "awaiting_approval" })], active_turn_id: "t1", pending_approvals: [pending] }));
    await act(async () => { streams.at(-1)!.h.onEnd({ after_seq: 5, reason: "finished" }); });
    await waitFor(() => expect(r.result.current.state.pending).toHaveLength(1));
    expect(app.refreshPending).toHaveBeenCalled();
  });

  it("carries on following when the server only recycled the connection", async () => {
    api.send.mockResolvedValue(turn());
    const r = setup("s1");
    await waitFor(() => expect(r.result.current.state.loading).toBe(false));
    await act(async () => { await r.result.current.send("hi"); });
    act(() => streams.at(-1)!.h.onEvent(ev(1, "user_message", { text: "hi" })));
    act(() => streams.at(-1)!.h.onEvent(ev(2, "token", { message_id: "m", text: "x" })));
    api.session.mockResolvedValue(view({ turns: [turn()], active_turn_id: "t1" }));
    const before = streams.length;
    await act(async () => { streams.at(-1)!.h.onEnd({ after_seq: 2, reason: "time" }); });
    await waitFor(() => expect(streams.length).toBe(before + 1));
    expect(streams.at(-1)!.after).toBe(2);
  });
});

describe("answering", () => {
  const pending: Approval = { id: "a1", run_id: "t1", status: "pending", expires_at: "", decided_by: null, previews: [], action_requests: [{ name: "execute", args: {} }] };

  async function waiting() {
    api.session.mockResolvedValue(view({ turns: [turn({ status: "awaiting_approval" })], active_turn_id: "t1", pending_approvals: [pending] }));
    api.transcript.mockResolvedValue(transcript([{ turn: turn({ status: "awaiting_approval" }), events: [ev(1, "user_message", { text: "x" }), ev(2, "interrupt", { approval_id: "a1", items: [] })], truncated: false, next_seq: 2 }]));
    const r = setup("s1");
    await waitFor(() => expect(r.result.current.state.pending).toHaveLength(1));
    return r;
  }

  it("sends the decisions and follows the turn on from where it paused", async () => {
    const r = await waiting();
    api.decide.mockResolvedValue(turn({ status: "running" }));
    await act(async () => { await r.result.current.decide("a1", [{ type: "approve" }]); });
    expect(api.decide).toHaveBeenCalledWith("a1", [{ type: "approve" }]);
    expect(r.result.current.state.pending).toEqual([]);
    expect(streams.at(-1)).toMatchObject({ turnId: "t1", after: 2 });
  });

  it("shows what is true now when the request was already answered or has expired", async () => {
    const r = await waiting();
    api.decide.mockRejectedValue(new Error("approval is no longer pending (already decided or expired)"));
    api.session.mockResolvedValue(view({ turns: [turn({ status: "completed" })] }));
    await act(async () => { await r.result.current.decide("a1", [{ type: "approve" }]); });
    expect(r.result.current.state.error).toMatch(/no longer pending/);
    await waitFor(() => expect(r.result.current.state.pending).toEqual([]));
  });

  it("stops the running turn", async () => {
    api.session.mockResolvedValue(view({ turns: [turn()], active_turn_id: "t1" }));
    const r = setup("s1");
    await waitFor(() => expect(r.result.current.state.active?.id).toBe("t1"));
    await act(async () => { await r.result.current.cancel(); });
    expect(api.cancel).toHaveBeenCalledWith("t1");
  });
});

describe("settings of the conversation", () => {
  it("keeps the automatic-approval choice for the next new chat when there is no conversation yet", async () => {
    const r = setup(null);
    await act(async () => { await r.result.current.setAutoApprove({ kinds: ["shell"], max_per_turn: 5 }); });
    expect(app.setNewChatAuto).toHaveBeenCalledWith({ kinds: ["shell"], max_per_turn: 5 });
    expect(api.updateSession).not.toHaveBeenCalled();
  });

  it("saves it on the conversation, and the model too, once there is one", async () => {
    const r = setup("s1");
    await waitFor(() => expect(r.result.current.state.loading).toBe(false));
    api.updateSession.mockResolvedValue(session({ auto_approve: { kinds: ["shell"], max_per_turn: 5 } }));
    await act(async () => { await r.result.current.setAutoApprove({ kinds: ["shell"], max_per_turn: 5 }); });
    expect(api.updateSession).toHaveBeenCalledWith("s1", { auto_approve: { kinds: ["shell"], max_per_turn: 5 } });
    expect(r.result.current.state.session?.auto_approve).toEqual({ kinds: ["shell"], max_per_turn: 5 });
    api.updateSession.mockResolvedValue(session({ model: "m2" }));
    await act(async () => { await r.result.current.setModel("m2"); });
    expect(api.updateSession).toHaveBeenLastCalledWith("s1", { model: "m2" });
    expect(app.setNewChatModel).toHaveBeenCalledWith("m2");
  });
});
