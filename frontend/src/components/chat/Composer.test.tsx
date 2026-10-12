import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AutoApproveOptions, ModelInfo } from "../../api/types";
import { Composer, type ComposerProps } from "./Composer";

const models: ModelInfo[] = [{ id: "m1", provider: "g", label: "Model One", rank: 1, use_for: "quick things", location: "" }, { id: "m2", provider: "g", label: "Model Two", rank: 2, use_for: "hard things", location: "" }];
const options: AutoApproveOptions = { kinds: ["shell", "github"], default_kinds: ["shell", "github"], never: ["question"], never_tools: [], default_max_per_turn: 25, max_per_turn_limit: 25 };

function setup(over: Partial<ComposerProps> = {}) {
  const props: ComposerProps = { onSend: vi.fn(), onStop: vi.fn(), onTyping: vi.fn(), running: false, models, model: "m1", onModel: vi.fn(), helperChoice: true, autoApprove: null, autoOptions: options, onAutoApprove: vi.fn(), ...over };
  const user = userEvent.setup();
  const view = render(<Composer {...props} />);
  return { props, user, ...view };
}

describe("sending", () => {
  it("sends on Enter and clears the box, but not on Shift+Enter", async () => {
    const { props, user } = setup();
    const box = screen.getByLabelText("Message");
    await user.type(box, "hello{Shift>}{Enter}{/Shift}world");
    expect(props.onSend).not.toHaveBeenCalled();
    expect(box).toHaveValue("hello\nworld");
    await user.type(box, "{Enter}");
    expect(props.onSend).toHaveBeenCalledWith("hello\nworld");
    expect(box).toHaveValue("");
  });

  it("does not send an empty or blank message", async () => {
    const { props, user } = setup();
    await user.type(screen.getByLabelText("Message"), "   {Enter}");
    expect(props.onSend).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("sends with the button too", async () => {
    const { props, user } = setup();
    await user.type(screen.getByLabelText("Message"), "go");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(props.onSend).toHaveBeenCalledWith("go");
  });

  it("tells the app the user started typing, once per message", async () => {
    const { props, user } = setup();
    const box = screen.getByLabelText("Message");
    await user.type(box, "abc");
    expect(props.onTyping).toHaveBeenCalledTimes(1);
    await user.type(box, "{Enter}");
    await user.type(box, "d");
    expect(props.onTyping).toHaveBeenCalledTimes(2);
  });
});

describe("states", () => {
  it("shows Stop instead of Send while a turn is running, and does not send then", async () => {
    const { props, user } = setup({ running: true });
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "more{Enter}");
    expect(props.onSend).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(props.onStop).toHaveBeenCalled();
  });

  it("is closed, with the reason, while something waits for an answer", () => {
    setup({ blockedReason: "Answer the request above to continue" });
    const box = screen.getByLabelText("Message");
    expect(box).toBeDisabled();
    expect(box).toHaveAttribute("placeholder", "Answer the request above to continue");
  });

  it("puts an example into the box when seeded", () => {
    const { rerender, props } = setup({ seed: { text: "first example", nonce: 1 } });
    expect(screen.getByLabelText("Message")).toHaveValue("first example");
    rerender(<Composer {...props} seed={{ text: "second", nonce: 2 }} />);
    expect(screen.getByLabelText("Message")).toHaveValue("second");
  });
});

describe("the controls under the box", () => {
  it("shows the chosen model and lets you pick another, cheapest first, with what each is for", async () => {
    const { props, user } = setup({ model: "m2" });
    await user.click(screen.getByRole("button", { name: /Model Two/ }));
    const options = screen.getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual([expect.stringContaining("Model One"), expect.stringContaining("Model Two")]);
    expect(screen.getByText("quick things")).toBeInTheDocument();
    expect(screen.getByText(/Helpers can run on this model or any cheaper one/)).toBeInTheDocument();
    await user.click(options[0]);
    expect(props.onModel).toHaveBeenCalledWith("m1");
  });

  it("does not promise helper choice when the policy has it off", async () => {
    const { user } = setup({ helperChoice: false });
    await user.click(screen.getByRole("button", { name: /Model One/ }));
    expect(screen.queryByText(/Helpers can run on this model/)).not.toBeInTheDocument();
  });

  it("says whether it asks first or approves automatically", () => {
    setup({ autoApprove: null });
    expect(screen.getByRole("button", { name: /Ask before acting/ })).toBeInTheDocument();
  });
});
