import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { McpServer } from "../api/types";

const api = vi.hoisted(() => ({ mcpServers: vi.fn(), mcpImport: vi.fn(), mcpPatch: vi.fn(), mcpTest: vi.fn(), mcpDelete: vi.fn() }));
vi.mock("../api/client", () => ({ api, ApiError: class extends Error {} }));
vi.mock("../state/nav", () => ({ useNav: () => ({ open: false, setOpen: vi.fn() }) }));
const auth = vi.hoisted(() => ({ mcp: undefined as undefined | { enabled: boolean; scope: string; vpc_ranges: string[]; allowed_hosts: string[] } }));
vi.mock("../state/auth", () => ({ useAuth: () => ({ state: { status: "signedIn", user: "me@x.com", config: {}, appConfig: { mcp: auth.mcp } } }) }));

import { ConnectorsPage } from "./ConnectorsPage";

const server = (over: Partial<McpServer> = {}): McpServer => ({
  id: "m1", name: "calc", url: "https://tools.example.com/mcp", transport: "http", header_names: ["Authorization"], enabled: true, requires_approval: true, no_approval_tools: [],
  tools: [{ name: "add", description: "Add two numbers", needs_approval: true }, { name: "shout", description: "", needs_approval: true }], tool_count: 2, tools_checked_at: new Date().toISOString(), last_error: "", ...over,
});
const mount = () => render(<MemoryRouter><ConnectorsPage /></MemoryRouter>);
beforeEach(() => { auth.mcp = undefined; Object.values(api).forEach((f) => f.mockReset()); api.mcpServers.mockResolvedValue({ servers: [] }); });

describe("the list", () => {
  it("invites you to add the first server", async () => {
    mount();
    expect(await screen.findByText("No servers connected")).toBeInTheDocument();
  });

  it("shows a connected server without ever showing a token", async () => {
    api.mcpServers.mockResolvedValue({ servers: [server()] });
    mount();
    expect(await screen.findByRole("heading", { name: "calc" })).toBeInTheDocument();
    expect(screen.getByText(/tools\.example\.com · Authorization set/)).toBeInTheDocument();
    expect(screen.getByText(/2 tools/)).toBeInTheDocument();
    expect(screen.getByLabelText("connected")).toBeInTheDocument();
  });

  it("shows why a server is not reachable", async () => {
    api.mcpServers.mockResolvedValue({ servers: [server({ last_error: "the server answered HTTP 401 (check the token in the headers)", tools: [], tool_count: 0 })] });
    mount();
    expect(await screen.findByText(/HTTP 401/)).toBeInTheDocument();
    expect(screen.getByLabelText("not reachable")).toBeInTheDocument();
  });
});

describe("adding servers", () => {
  it("sends pasted JSON as an object and reports each server", async () => {
    api.mcpImport.mockResolvedValue({ results: [
      { name: "calc", saved: true, reachable: true, tool_count: 4, error: "", warnings: [] },
      { name: "down", saved: true, reachable: false, tool_count: 0, error: "the server did not answer in time", warnings: [] },
      { name: "bad", saved: false, reachable: false, tool_count: 0, error: "only https:// URLs are allowed", warnings: [] }] });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByLabelText("Server configuration"));
    await user.paste('{"mcpServers": {"calc": {"url": "https://x.example.com/mcp"}}}');
    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(api.mcpImport).toHaveBeenCalledWith({ mcpServers: { calc: { url: "https://x.example.com/mcp" } } });
    const list = await screen.findByRole("list", { name: "Import results" });
    expect(within(list).getByText(/connected, 4 tools/)).toBeInTheDocument();
    expect(within(list).getByText(/saved, but not reachable yet \(the server did not answer in time\)/)).toBeInTheDocument();
    expect(within(list).getByText(/not saved \(only https:\/\/ URLs are allowed\)/)).toBeInTheDocument();
  });

  it("passes text that is not JSON through, so the server can explain what is wrong with it", async () => {
    api.mcpImport.mockRejectedValue(Object.assign(new Error("that is not valid JSON (Expecting value)"), {}));
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByLabelText("Server configuration"));
    await user.paste("{nope");
    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(api.mcpImport).toHaveBeenCalledWith("{nope");
    expect(await screen.findByRole("alert")).toHaveTextContent("not valid JSON");
  });

  it("cannot connect with nothing pasted", async () => {
    mount();
    expect(await screen.findByRole("button", { name: "Connect" })).toBeDisabled();
  });
});

describe("a server's controls", () => {
  it("switches at once and saves, and refreshes from the server afterwards", async () => {
    api.mcpServers.mockResolvedValue({ servers: [server()] });
    api.mcpPatch.mockResolvedValue(server({ enabled: false }));
    const user = userEvent.setup();
    mount();
    const toggle = await screen.findByRole("switch", { name: "Use calc" });
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(api.mcpPatch).toHaveBeenCalledWith("calc", { enabled: false });
    await waitFor(() => expect(api.mcpServers.mock.calls.length).toBeGreaterThan(1));
  });

  it("lets you mark a tool as safe, keeping the others as they were", async () => {
    api.mcpServers.mockResolvedValue({ servers: [server({ no_approval_tools: ["shout"] })] });
    api.mcpPatch.mockResolvedValue(server());
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "Tools" }));
    expect(screen.getByLabelText("Ask before shout")).not.toBeChecked();
    await user.click(screen.getByLabelText("Ask before add"));
    expect(screen.getByLabelText("Ask before add")).not.toBeChecked();
    expect(api.mcpPatch).toHaveBeenCalledWith("calc", { no_approval_tools: expect.arrayContaining(["shout", "add"]) });
  });

  it("hides the per-tool choices when every call asks anyway... and shows them when it does not", async () => {
    api.mcpServers.mockResolvedValue({ servers: [server({ requires_approval: false })] });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "Tools" }));
    expect(screen.queryByLabelText("Ask before add")).not.toBeInTheDocument();
  });

  it("tests, refreshes and removes only after you confirm", async () => {
    api.mcpServers.mockResolvedValue({ servers: [server()] });
    api.mcpTest.mockResolvedValue(server());
    api.mcpDelete.mockResolvedValue({ deleted: "calc" });
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: /Test and refresh/ }));
    expect(api.mcpTest).toHaveBeenCalledWith("calc");
    vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    await user.click(screen.getByRole("button", { name: /Remove/ }));
    expect(api.mcpDelete).not.toHaveBeenCalled();
    vi.spyOn(window, "confirm").mockReturnValueOnce(true);
    await user.click(screen.getByRole("button", { name: /Remove/ }));
    expect(api.mcpDelete).toHaveBeenCalledWith("calc");
  });
});

describe("the rule about where servers can live", () => {
  it("is not shown when any server may be added", async () => {
    mount();
    await screen.findByText("No servers connected");
    expect(screen.queryByRole("note", { name: /which servers can be added/i })).not.toBeInTheDocument();
  });

  it("names the private network and the approved list, and says anything else is refused", async () => {
    auth.mcp = { enabled: true, scope: "vpc_or_allowlist", vpc_ranges: ["192.168.40.0/24"], allowed_hosts: ["mcp.vendor.example"] };
    mount();
    const note = await screen.findByRole("note", { name: /which servers can be added/i });
    expect(within(note).getByText("192.168.40.0/24")).toBeInTheDocument();
    expect(within(note).getByText("mcp.vendor.example")).toBeInTheDocument();
    expect(note).toHaveTextContent(/any other address is refused/i);
    expect(note).toHaveTextContent(/ask the administrator/i);
  });

  it("says plainly when nothing is on the list yet", async () => {
    auth.mcp = { enabled: true, scope: "vpc_or_allowlist", vpc_ranges: ["192.168.40.0/24"], allowed_hosts: [] };
    mount();
    expect(await screen.findByText("none yet")).toBeInTheDocument();
  });
});
