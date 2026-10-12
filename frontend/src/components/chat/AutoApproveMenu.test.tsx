import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AutoApprove, AutoApproveOptions } from "../../api/types";
import { AutoApproveMenu, summarize } from "./AutoApproveMenu";

const options: AutoApproveOptions = { kinds: ["shell", "github", "mcp", "other", "email", "plan"], default_kinds: ["shell", "github"], never: ["question", "interrupt"], never_tools: ["github_open_pr"], default_max_per_turn: 25, max_per_turn_limit: 30 };

function setup(value: AutoApprove = null) {
  const onChange = vi.fn();
  const user = userEvent.setup();
  render(<AutoApproveMenu value={value} options={options} onChange={onChange} />);
  return { onChange, user };
}
const open = (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole("button", { name: /Ask before acting|Auto-approving/ }));

describe("turning it on and off", () => {
  it("starts from the safe default kinds when switched on", async () => {
    const { onChange, user } = setup();
    await open(user);
    await user.click(screen.getByRole("switch", { name: "Approve automatically" }));
    expect(onChange).toHaveBeenCalledWith({ kinds: ["shell", "github"], max_per_turn: 25 });
  });

  it("switches off with null, which is what the server reads as 'ask every time'", async () => {
    const { onChange, user } = setup({ kinds: ["shell"], max_per_turn: 5 });
    await open(user);
    await user.click(screen.getByRole("switch", { name: "Approve automatically" }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("applies a change to the kinds at once while it is on, and turns itself off when none are left", async () => {
    const { onChange, user } = setup({ kinds: ["shell"], max_per_turn: 5 });
    await open(user);
    await user.click(screen.getByRole("checkbox", { name: /Use connected tools/ }));
    expect(onChange).toHaveBeenLastCalledWith({ kinds: ["shell", "mcp"], max_per_turn: 5 });
    await user.click(screen.getByRole("checkbox", { name: /Run commands/ }));
    await user.click(screen.getByRole("checkbox", { name: /Use connected tools/ }));
    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it("keeps the limit within what the policy allows", async () => {
    const { onChange, user } = setup({ kinds: ["shell"], max_per_turn: 5 });
    await open(user);
    const cap = screen.getByLabelText("Stop and ask after");
    await user.clear(cap);
    await user.type(cap, "999");
    await user.tab();
    expect(onChange).toHaveBeenLastCalledWith({ kinds: ["shell"], max_per_turn: 30 });
  });
});

describe("what it tells you", () => {
  it("names what is automatic in the button", () => {
    expect(summarize(null)).toBe("Ask before acting");
    expect(summarize({ kinds: ["shell", "github", "mcp"], max_per_turn: 5 })).toBe("Auto-approving commands, GitHub, connected tools");
  });

  it("lists the kinds in a sensible order and says what is never automatic", async () => {
    const { user } = setup();
    await open(user);
    expect(screen.getAllByRole("checkbox").map((c) => c.closest("label")!.textContent?.split(/(?=[A-Z][a-z]+ .*\b)/)[0])).toHaveLength(6);
    const labels = screen.getAllByRole("checkbox").map((c) => c.closest("label")!.querySelector("span span")!.textContent);
    expect(labels).toEqual(["Run commands", "Change GitHub", "Use connected tools", "Other tools", "Send email", "Approve plans"]);
    expect(screen.getByText(/Questions are always yours to answer/)).toBeInTheDocument();
    expect(screen.getByText(/Always asks first: github_open_pr/)).toBeInTheDocument();
  });
});
