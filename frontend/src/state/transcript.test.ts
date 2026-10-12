import type { EngineEvent, TranscriptTurn } from "../api/types";
import { applyEvent, emptyTranscript, fromTurns, reconcileApprovals, withLocalMessage, withoutLocalMessages, type Item, type TranscriptState } from "./transcript";

let n = 0;
const ev = (type: string, payload: Record<string, any> = {}, seq = ++n): EngineEvent => ({ seq, type, payload });
const run = (events: EngineEvent[], turnId = "t1", start: TranscriptState = emptyTranscript()) => events.reduce((s, e) => applyEvent(s, turnId, e), start);
const kinds = (s: TranscriptState) => s.items.map((i) => i.kind);
const of = <K extends Item["kind"]>(s: TranscriptState, kind: K) => s.items.filter((i) => i.kind === kind) as Extract<Item, { kind: K }>[];

beforeEach(() => { n = 0; });

describe("text", () => {
  it("shows the user's message and grows the assistant's answer from token batches", () => {
    const s = run([ev("user_message", { text: "hi" }), ev("token", { message_id: "m1", text: "Hel", agent: "assistant" }), ev("token", { message_id: "m1", text: "lo", agent: "assistant" })]);
    expect(kinds(s)).toEqual(["user", "assistant"]);
    expect(of(s, "assistant")[0]).toMatchObject({ text: "Hello", streaming: true, agent: "assistant" });
  });

  it("replaces the streamed text with the final message and stops the streaming mark", () => {
    const s = run([ev("token", { message_id: "m1", text: "Hel" }), ev("assistant_message", { message_id: "m1", text: "Hello there", truncated: false })]);
    expect(of(s, "assistant")).toHaveLength(1);
    expect(of(s, "assistant")[0]).toMatchObject({ text: "Hello there", streaming: false });
  });

  it("shows a final message that was never streamed (a short answer, or a reload) and keeps the truncation flag", () => {
    const s = run([ev("assistant_message", { message_id: "m9", text: "Short.", truncated: true, agent: "researcher" })]);
    expect(of(s, "assistant")[0]).toMatchObject({ text: "Short.", agent: "researcher", truncated: true });
  });
});

describe("tools", () => {
  it("starts a tool as running and finishes it with its result", () => {
    const s = run([ev("tool_call", { tool_call_id: "c1", name: "execute", args: { command: "ls" } }), ev("tool_result", { tool_call_id: "c1", status: "success", preview: "a.txt", truncated: false })]);
    expect(of(s, "tool")).toHaveLength(1);
    expect(of(s, "tool")[0]).toMatchObject({ name: "execute", args: { command: "ls" }, status: "success", result: "a.txt" });
  });

  it("marks an error result and keeps an edited call visible as edited", () => {
    const s = run([ev("tool_call", { tool_call_id: "c1", name: "execute", args: { command: "rm" } }), ev("tool_call", { tool_call_id: "c1", name: "execute", args: { command: "ls" }, edited: true }),
      ev("tool_result", { tool_call_id: "c1", status: "error", preview: "denied" })]);
    expect(of(s, "tool")[0]).toMatchObject({ status: "error", edited: true, args: { command: "ls" } });
  });

  it("still shows a result whose call was not seen", () => {
    const s = run([ev("tool_result", { tool_call_id: "zz", name: "read_file", status: "success", preview: "x" })]);
    expect(of(s, "tool")[0]).toMatchObject({ name: "read_file", result: "x" });
  });

  it("labels each step with the helper that took it", () => {
    const s = run([ev("tool_call", { tool_call_id: "c1", name: "web_search", args: {}, agent: "researcher" })]);
    expect(of(s, "tool")[0].agent).toBe("researcher");
  });

  it("keeps the latest todo list, not a history of them", () => {
    const s = run([ev("todos", { todos: [{ content: "a", status: "pending" }] }), ev("todos", { todos: [{ content: "a", status: "completed" }] })]);
    expect(s.todos).toEqual([{ content: "a", status: "completed" }]);
  });
});

describe("approvals", () => {
  const items = [{ tool: "execute", kind: "shell", title: "Run a shell command", allowed_decisions: ["approve", "reject"], preview: { command: "ls" } }];

  it("shows a pause as a pending approval and records the decision", () => {
    let s = run([ev("interrupt", { approval_id: "a1", items, expires_at: "2026-10-11T10:00:00Z" })]);
    expect(of(s, "approval")[0]).toMatchObject({ id: "a1", state: "pending", expiresAt: "2026-10-11T10:00:00Z" });
    s = run([ev("approval_decided", { approval_id: "a1", status: "rejected", decisions: [{ type: "reject", message: "no" }] })], "t1", s);
    expect(of(s, "approval")[0]).toMatchObject({ state: "rejected", decisions: [{ type: "reject", message: "no" }] });
  });

  it("shows automatic approvals as their own rows", () => {
    const s = run([ev("approval_auto", { approval_id: "a2", items })]);
    expect(of(s, "auto_approval")[0].items[0].title).toBe("Run a shell command");
  });

  it("does not duplicate a pause that is replayed, and an unknown decision changes nothing", () => {
    const s = run([ev("interrupt", { approval_id: "a1", items }), ev("interrupt", { approval_id: "a1", items }), ev("approval_decided", { approval_id: "nope", status: "approved" })]);
    expect(of(s, "approval")).toHaveLength(1);
    expect(of(s, "approval")[0].state).toBe("pending");
  });

  it("marks a pause the server no longer lists as pending as expired", () => {
    const s = run([ev("interrupt", { approval_id: "a1", items }), ev("interrupt", { approval_id: "a2", items })]);
    const items2 = reconcileApprovals(s.items, new Set(["a2"]));
    expect(items2.filter((i) => i.kind === "approval").map((i: any) => [i.id, i.state])).toEqual([["a1", "expired"], ["a2", "pending"]]);
  });
});

describe("the end of a turn", () => {
  it("adds a status row and stops anything still streaming or running", () => {
    const s = run([ev("token", { message_id: "m1", text: "partial" }), ev("tool_call", { tool_call_id: "c1", name: "execute", args: {} }), ev("turn_done", { status: "cancelled" })]);
    expect(of(s, "assistant")[0].streaming).toBe(false);
    expect(of(s, "tool")[0]).toMatchObject({ status: "error", result: "Stopped before it finished." });
    expect(of(s, "status")[0].status).toBe("cancelled");
  });

  it("reports a failure with its reason and a blocked message as blocked", () => {
    expect(of(run([ev("turn_error", { status: "failed", error: "boom" })]), "status")[0]).toMatchObject({ status: "failed", text: "boom" });
    expect(of(run([ev("turn_error", { status: "blocked", error: "refused" })]), "status")[0].status).toBe("blocked");
  });

  it("only settles its own turn", () => {
    let s = run([ev("token", { message_id: "m1", text: "a" })], "t1");
    s = run([ev("turn_done", { status: "completed" }, 1)], "t2", s);
    expect(of(s, "assistant")[0].streaming).toBe(true);
  });
});

describe("the rest", () => {
  it("lists a saved file once, even if the event is replayed under another sequence number", () => {
    const s = run([ev("artifact", { artifact_id: "f1", name: "r.md", path: "r.md", size: 10, content_type: "text/markdown" }), ev("artifact", { artifact_id: "f1", name: "r.md" })]);
    expect(of(s, "artifact")).toHaveLength(1);
  });

  it("adds up token usage and the models that were used", () => {
    const s = run([ev("usage", { input_tokens: 100, output_tokens: 10, model: "m-a" }), ev("usage", { input_tokens: 50, output_tokens: 5, model: "m-b" }), ev("usage", { input_tokens: 1, output_tokens: 1, model: "m-a" })]);
    expect(s.usage.t1).toEqual({ input: 151, output: 16, calls: 3, models: ["m-a", "m-b"] });
  });

  it("ignores events it has already seen and events it does not know", () => {
    const first = run([ev("notice", { text: "one" }, 5)]);
    const again = run([ev("notice", { text: "one" }, 5), ev("notice", { text: "old" }, 3), ev("heartbeat", {}, 6), ev("brand_new_kind", {}, 7)], "t1", first);
    expect(of(again, "notice")).toHaveLength(1);
    expect(again.lastSeq.t1).toBe(7);
  });

  it("builds a whole conversation from loaded turns in order", () => {
    const turns: TranscriptTurn[] = [
      { turn: { id: "t1" } as any, events: [ev("user_message", { text: "one" }, 1), ev("assistant_message", { message_id: "m1", text: "ok one" }, 2), ev("turn_done", { status: "completed" }, 3)], truncated: false, next_seq: 3 },
      { turn: { id: "t2" } as any, events: [ev("user_message", { text: "two" }, 1), ev("assistant_message", { message_id: "m2", text: "ok two" }, 2)], truncated: false, next_seq: 2 },
    ];
    const s = fromTurns(turns);
    expect(kinds(s)).toEqual(["user", "assistant", "status", "user", "assistant"]);
    expect(s.lastSeq).toEqual({ t1: 3, t2: 2 });
  });
});


describe("the message you just typed", () => {
  it("appears at once and is replaced, not duplicated, when the server confirms it", () => {
    let s = withLocalMessage(emptyTranscript(), "hello", 1);
    expect(of(s, "user")).toHaveLength(1);
    s = run([ev("user_message", { text: "hello" })], "t1", s);
    expect(of(s, "user")).toHaveLength(1);
    expect(of(s, "user")[0].id).toBe("t1:" + s.lastSeq.t1);
  });

  it("can be taken back when sending failed", () => {
    const s = withoutLocalMessages(withLocalMessage(emptyTranscript(), "hello", 1));
    expect(s.items).toEqual([]);
  });

  it("only replaces a bubble with the same text", () => {
    let s = withLocalMessage(emptyTranscript(), "one", 1);
    s = run([ev("user_message", { text: "something else" })], "t1", s);
    expect(of(s, "user").map((u) => u.text)).toEqual(["one", "something else"]);
  });
});
