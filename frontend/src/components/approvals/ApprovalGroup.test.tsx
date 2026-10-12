import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Approval, ApprovalItem } from "../../api/types";
import type { Item } from "../../state/transcript";
import { ApprovalGroup, AutoApprovedRow, toDecision } from "./ApprovalGroup";

type Row = Extract<Item, { kind: "approval" }>;

const item = (over: Partial<ApprovalItem> & { kind: ApprovalItem["kind"] }): ApprovalItem => ({ tool: "execute", title: "Run a shell command", allowed_decisions: ["approve", "edit", "reject"], preview: {}, ...over });
const shell = (command = "ls -la") => item({ kind: "shell", tool: "execute", preview: { command } });
const email = item({ kind: "email", tool: "send_email", title: "Send email: Report", preview: { to: ["me@example.com"], cc: [], subject: "Report", body: "Here you go.\nBye.", attachments: [] } });
const question = item({ kind: "question", tool: "ask_user", title: "The assistant has a question", allowed_decisions: ["respond"], preview: { question: "Which colour?", options: ["red", "blue"] } });
const plan = item({ kind: "plan", tool: "submit_plan", title: "Approve the plan", preview: { summary: "Do two things", tasks: [{ title: "Write", detail: "a file", assignee: "coder" }, { title: "Mail", detail: "it", assignee: "mailer" }] } });
const mcp = item({ kind: "mcp", tool: "mcp__calc__add", title: "Use add on calc", preview: { server: "calc", tool: "add", args: { a: "1", b: "2" } } });
const generic = item({ kind: "interrupt", tool: "interrupt", title: "The agent is waiting for your input", allowed_decisions: ["respond"], preview: { value: "are you sure?" } });

const row = (items: ApprovalItem[], state: Row["state"] = "pending", decisions: Row["decisions"] = []): Row => ({ kind: "approval", id: "a1", turnId: "t1", items, state, decisions, expiresAt: null });
const approval = (items: ApprovalItem[], requests?: Array<Record<string, any>>): Approval => ({
  id: "a1", run_id: "t1", status: "pending", expires_at: "", decided_by: null, previews: items,
  action_requests: items.map((it, i) => ({ name: it.tool, args: requests?.[i] ?? {} })),
});

function setup(items: ApprovalItem[], requests?: Array<Record<string, any>>) {
  const onDecide = vi.fn();
  const user = userEvent.setup();
  render(<ApprovalGroup row={row(items)} approval={approval(items, requests)} onDecide={onDecide} />);
  return { onDecide, user };
}

describe("one thing to approve", () => {
  it("shows exactly what will run and approves with one click", async () => {
    const { onDecide, user } = setup([shell("rm -rf build && make")]);
    expect(screen.getByText("rm -rf build && make")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /approve/i }));
    expect(onDecide).toHaveBeenCalledWith("a1", [{ type: "approve" }]);
  });

  it("rejects with or without a reason", async () => {
    const a = setup([shell()]);
    await a.user.click(screen.getByRole("button", { name: "Reject" }));
    await a.user.click(screen.getByRole("button", { name: "Reject" }));
    expect(a.onDecide).toHaveBeenLastCalledWith("a1", [{ type: "reject" }]);
  });

  it("sends the reason when there is one", async () => {
    const { onDecide, user } = setup([shell()]);
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await user.type(screen.getByLabelText("Why not? (optional)"), "too risky");
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecide).toHaveBeenCalledWith("a1", [{ type: "reject", message: "too risky" }]);
  });

  it("lets you change a command before approving it", async () => {
    const { onDecide, user } = setup([shell("ls")], [{ command: "ls" }]);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByLabelText("Command");
    await user.clear(box);
    await user.type(box, "ls -l");
    await user.click(screen.getByRole("button", { name: "Approve with my changes" }));
    expect(onDecide).toHaveBeenCalledWith("a1", [{ type: "edit", edited_action: { name: "execute", args: { command: "ls -l" } } }]);
  });

  it("shows an email in full and lets you edit the subject and body", async () => {
    const { onDecide, user } = setup([email], [{ to: ["me@example.com"], subject: "Report", body: "Here you go.\nBye." }]);
    expect(screen.getByText("me@example.com")).toBeInTheDocument();
    expect(screen.getByText(/Here you go\./)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const subject = screen.getByLabelText("Subject");
    await user.clear(subject);
    await user.type(subject, "Weekly report");
    await user.click(screen.getByRole("button", { name: "Approve with my changes" }));
    const sent = onDecide.mock.calls[0][1][0];
    expect(sent.edited_action).toEqual({ name: "send_email", args: { to: ["me@example.com"], subject: "Weekly report", body: "Here you go.\nBye." } });
  });

  it("answers a question, with suggested answers as shortcuts, and cannot be approved or edited", async () => {
    const { onDecide, user } = setup([question]);
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "blue" }));
    expect(screen.getByLabelText("Your answer")).toHaveValue("blue");
    await user.click(send);
    expect(onDecide).toHaveBeenCalledWith("a1", [{ type: "respond", message: "blue" }]);
  });

  it("takes a free-text reply to a pause the agent made on its own", async () => {
    const { onDecide, user } = setup([generic]);
    expect(screen.getByText("are you sure?")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Your reply"), "yes go on");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(onDecide).toHaveBeenCalledWith("a1", [{ type: "respond", message: "yes go on" }]);
  });

  it("shows a connected tool's server, tool and arguments, and edits them as JSON", async () => {
    const { onDecide, user } = setup([mcp], [{ a: "1", b: "2" }]);
    expect(screen.getByText("calc")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByLabelText("Arguments (JSON)");
    await user.clear(box);
    await user.type(box, "{{not json");
    await user.click(screen.getByRole("button", { name: "Approve with my changes" }));
    expect(screen.getByText(/Not valid JSON/)).toBeInTheDocument();
    expect(onDecide).not.toHaveBeenCalled();
    await user.clear(box);
    await user.type(box, '{{"a": 5, "b": 6}');
    await user.click(screen.getByRole("button", { name: "Approve with my changes" }));
    expect(onDecide).toHaveBeenCalledWith("a1", [{ type: "edit", edited_action: { name: "mcp__calc__add", args: { a: 5, b: 6 } } }]);
  });

  it("edits a plan: rename a step, drop another, approve the result", async () => {
    const tasks = [{ title: "Write", detail: "a file", assignee: "coder" }, { title: "Mail", detail: "it", assignee: "mailer" }];
    const { onDecide, user } = setup([plan], [{ summary: "Do two things", tasks }]);
    expect(screen.getByText("Do two things")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const t1 = screen.getByLabelText("Step 1 title");
    await user.clear(t1);
    await user.type(t1, "Write the report");
    await user.click(screen.getByRole("button", { name: "Remove step 2" }));
    await user.click(screen.getByRole("button", { name: "Approve with my changes" }));
    expect(onDecide).toHaveBeenCalledWith("a1", [{ type: "edit", edited_action: { name: "submit_plan", args: { summary: "Do two things", tasks: [{ title: "Write the report", detail: "a file", assignee: "coder" }] } } }]);
  });

  it("only offers the decisions the agent allows for this action", () => {
    setup([item({ kind: "shell", allowed_decisions: ["approve", "reject"], preview: { command: "x" } })]);
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });
});

describe("several things at once (helpers working in parallel)", () => {
  it("approves them all with one click", async () => {
    const { onDecide, user } = setup([shell("echo one"), shell("echo two")]);
    expect(screen.getByText("2 things are waiting for your decision")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve all 2" }));
    expect(onDecide).toHaveBeenCalledWith("a1", [{ type: "approve" }, { type: "approve" }]);
  });

  it("collects an answer for each before sending, in the order shown", async () => {
    const { onDecide, user } = setup([shell("echo one"), shell("echo two")]);
    const send = screen.getByRole("button", { name: "Send my answers" });
    expect(send).toBeDisabled();
    const cards = screen.getAllByText(/Run a shell command/).map((el) => el.closest("div.rounded-xl") as HTMLElement);
    await user.click(within(cards[1]).getByRole("button", { name: "Approve this" }));
    expect(send).toBeDisabled();
    expect(screen.getByText("1 of 2 answered")).toBeInTheDocument();
    await user.click(within(cards[0]).getByRole("button", { name: "Reject" }));
    await user.click(within(cards[0]).getByRole("button", { name: "Reject" }));
    await user.click(send);
    expect(onDecide).toHaveBeenCalledWith("a1", [{ type: "reject" }, { type: "approve" }]);
  });

  it("does not offer 'approve all' when one of them is a question", () => {
    setup([shell(), question]);
    expect(screen.queryByRole("button", { name: /Approve all/ })).not.toBeInTheDocument();
  });
});

describe("after the decision", () => {
  it("shows a short row that opens to the full content", async () => {
    const user = userEvent.setup();
    render(<ApprovalGroup row={row([shell("make")], "approved", [{ type: "approve" }])} onDecide={vi.fn()} />);
    expect(screen.getByText("Approved")).toBeInTheDocument();
    expect(screen.queryByText(/Run a shell command/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText(/Run a shell command — approve/)).toBeInTheDocument();
    expect(screen.getByText("make", { selector: "pre" })).toBeInTheDocument();
  });

  it("says when it was rejected, and when nobody answered in time", () => {
    const { rerender } = render(<ApprovalGroup row={row([shell()], "rejected", [{ type: "reject", message: "no" }])} onDecide={vi.fn()} />);
    expect(screen.getByText("Rejected")).toBeInTheDocument();
    rerender(<ApprovalGroup row={row([shell()], "expired")} onDecide={vi.fn()} />);
    expect(screen.getByText("Expired without an answer")).toBeInTheDocument();
  });

  it("shows approvals the assistant was allowed to make itself, clearly marked", async () => {
    const user = userEvent.setup();
    render(<AutoApprovedRow items={[shell("echo hi")]} />);
    expect(screen.getByText("Approved automatically")).toBeInTheDocument();
    await user.click(screen.getByRole("button"));
    expect(screen.getByText("echo hi", { selector: "pre" })).toBeInTheDocument();
  });
});

describe("the decision sent to the server", () => {
  it("is built from the choice", () => {
    expect(toDecision(shell(), { kind: "approve" })).toEqual({ type: "approve" });
    expect(toDecision(shell(), { kind: "reject", message: "  " })).toEqual({ type: "reject" });
    expect(toDecision(shell(), { kind: "reject", message: " no " })).toEqual({ type: "reject", message: "no" });
    expect(toDecision(question, { kind: "respond", message: "blue" })).toEqual({ type: "respond", message: "blue" });
    expect(toDecision(shell(), { kind: "edit", args: { command: "x" } })).toEqual({ type: "edit", edited_action: { name: "execute", args: { command: "x" } } });
  });
});
