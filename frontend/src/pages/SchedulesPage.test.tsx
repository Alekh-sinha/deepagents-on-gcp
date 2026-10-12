import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { Schedule } from "../api/types";

const api = vi.hoisted(() => ({ schedules: vi.fn(), scheduleCreate: vi.fn(), scheduleUpdate: vi.fn(), scheduleDelete: vi.fn(), scheduleRun: vi.fn(), scheduleRuns: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));
vi.mock("../state/nav", () => ({ useNav: () => ({ open: false, setOpen: vi.fn() }) }));
vi.mock("../state/app", () => ({ useApp: () => ({ models: [{ id: "m1", label: "Model One", rank: 1 }, { id: "m2", label: "Model Two", rank: 2 }], defaultModel: "m1", modelById: () => ({ label: "Model One" }) }) }));

import { SchedulesPage } from "./SchedulesPage";

const sched = (over: Partial<Schedule> = {}): Schedule => ({
  id: "sc_1", title: "Morning brief", instructions: "Summarise the news.", cron: "0 9 * * 1-5", timezone: "UTC", model: null, auto_approve: null, enabled: true,
  next_run_at: new Date(Date.now() + 3 * 3600_000).toISOString(), last_run_at: null, last_status: "", last_session_id: "", run_count: 0, ...over,
});
const mount = () => render(<MemoryRouter><SchedulesPage /></MemoryRouter>);
beforeEach(() => { Object.values(api).forEach((f) => f.mockReset()); api.schedules.mockResolvedValue({ schedules: [] }); });

describe("the list", () => {
  it("explains what scheduling is when there is nothing yet", async () => {
    mount();
    expect(await screen.findByText("Nothing scheduled")).toBeInTheDocument();
  });

  it("shows when each task runs, in words, and how it went last time", async () => {
    api.schedules.mockResolvedValue({ schedules: [sched({ last_status: "started", last_run_at: new Date(Date.now() - 600_000).toISOString(), run_count: 3 }), sched({ id: "sc_2", title: "Paused", enabled: false, cron: "30 18 * * *", auto_approve: { kinds: ["shell"], max_per_turn: 5 } })] });
    mount();
    expect(await screen.findByText("Weekdays at 9:00 · UTC")).toBeInTheDocument();
    expect(screen.getByText(/Next run/)).toBeInTheDocument();
    expect(screen.getByText(/Ran/)).toBeInTheDocument();
    expect(screen.getByText("3 runs", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Every day at 18:30 · UTC")).toBeInTheDocument();
    expect(screen.getByText("paused")).toBeInTheDocument();
    expect(screen.getByText("unattended")).toBeInTheDocument();
  });
});

describe("creating", () => {
  it("needs a name, instructions and a schedule before it can be created, then sends exactly what was chosen", async () => {
    api.scheduleCreate.mockResolvedValue(sched());
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: /New task/ }));
    const create = screen.getByRole("button", { name: "Create" });
    expect(create).toBeDisabled();
    await user.type(screen.getByLabelText("Name"), "Brief");
    await user.type(screen.getByLabelText("What should it do?"), "Say hi.");
    expect(create).toBeEnabled();
    await user.selectOptions(screen.getByLabelText("How often"), "0 9 * * 1");
    await user.selectOptions(screen.getByLabelText("Model"), "m2");
    await user.click(screen.getByRole("switch", { name: "Run without asking me" }));
    await user.click(create);
    expect(api.scheduleCreate).toHaveBeenCalledWith(expect.objectContaining({ title: "Brief", instructions: "Say hi.", cron: "0 9 * * 1", model: "m2", auto_approve: true, timezone: expect.any(String) }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Create" })).not.toBeInTheDocument());
  });

  it("uses a custom cron expression when asked", async () => {
    api.scheduleCreate.mockResolvedValue(sched());
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: /New task/ }));
    await user.type(screen.getByLabelText("Name"), "Brief");
    await user.type(screen.getByLabelText("What should it do?"), "Say hi.");
    await user.selectOptions(screen.getByLabelText("How often"), "custom");
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();       // a custom schedule needs an expression
    await user.type(screen.getByLabelText("Cron expression"), "15 7 * * *");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(api.scheduleCreate).toHaveBeenCalledWith(expect.objectContaining({ cron: "15 7 * * *", auto_approve: undefined, model: undefined }));
  });

  it("shows the server's reason and keeps what was typed", async () => {
    api.scheduleCreate.mockRejectedValue(Object.assign(new Error("that schedule can fire every 5 minute(s); the minimum interval is 15 minutes"), {}));
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: /New task/ }));
    await user.type(screen.getByLabelText("Name"), "Often");
    await user.type(screen.getByLabelText("What should it do?"), "Anything.");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("minimum interval is 15 minutes");
    expect(screen.getByLabelText("Name")).toHaveValue("Often");
  });
});

describe("a task's actions", () => {
  beforeEach(() => { api.schedules.mockResolvedValue({ schedules: [sched()] }); });

  it("pauses at once and saves", async () => {
    api.scheduleUpdate.mockImplementation(async () => { api.schedules.mockResolvedValue({ schedules: [sched({ enabled: false })] }); return sched({ enabled: false }); });   // what the server will say afterwards
    const user = userEvent.setup();
    mount();
    const toggle = await screen.findByRole("switch", { name: "Enable Morning brief" });
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(api.scheduleUpdate).toHaveBeenCalledWith("sc_1", { enabled: false });
  });

  it("runs now, and shows the history with a way into each run", async () => {
    api.scheduleRun.mockResolvedValue({});
    api.scheduleRuns.mockResolvedValue({ runs: [{ id: "r1", schedule_id: "sc_1", trigger: "manual", status: "started", session_id: "s_9", turn_id: "t", note: "", created_at: new Date().toISOString() },
      { id: "r2", schedule_id: "sc_1", trigger: "schedule", status: "skipped", session_id: "", turn_id: "", note: "the previous run is still working or waiting for your approval", created_at: new Date().toISOString() }] });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: /Run now/ }));
    expect(api.scheduleRun).toHaveBeenCalledWith("sc_1");
    await user.click(screen.getByRole("button", { name: /History/ }));
    expect(await screen.findByText("by hand")).toBeInTheDocument();
    expect(screen.getByText(/previous run is still working/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open" })).toHaveAttribute("href", "/chat/s_9");
  });

  it("deletes only after you confirm", async () => {
    api.scheduleDelete.mockResolvedValue({ deleted: "sc_1" });
    const user = userEvent.setup();
    mount();
    vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    await user.click(await screen.findByRole("button", { name: /Delete/ }));
    expect(api.scheduleDelete).not.toHaveBeenCalled();
    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    await user.click(screen.getByRole("button", { name: /Delete/ }));
    expect(api.scheduleDelete).toHaveBeenCalledWith("sc_1");
  });

  it("opens the same form to edit, filled in", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: /Edit/ }));
    expect(screen.getByLabelText("Name")).toHaveValue("Morning brief");
    expect(screen.getByLabelText("What should it do?")).toHaveValue("Summarise the news.");
    expect(screen.getByLabelText("How often")).toHaveValue("0 9 * * 1-5");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeInTheDocument();
  });
});
