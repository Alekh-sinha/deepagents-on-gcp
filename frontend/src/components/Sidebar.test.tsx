import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const mocks = vi.hoisted(() => ({ app: {} as any }));
vi.mock("../api/client", () => ({ api: { approvals: vi.fn(), turn: vi.fn() } }));
vi.mock("../state/nav", () => ({ useNav: () => ({ open: false, setOpen: vi.fn() }) }));
vi.mock("../state/auth", () => ({ useAuth: () => ({ signOut: vi.fn() }) }));
vi.mock("../state/app", () => ({ useApp: () => mocks.app }));

import { Sidebar } from "./Sidebar";

const appWith = (features?: { sandbox: boolean; artifacts: boolean }) => ({
  config: { features }, user: "me@example.com", sessions: [], sessionsLoaded: true, pendingCount: 0, setNewChatAuto: vi.fn(),
});
const mount = () => render(<MemoryRouter><Sidebar /></MemoryRouter>);

describe("the Files link follows the engine's artifacts switch", () => {
  it("is shown when artifacts are on", () => {
    mocks.app = appWith({ sandbox: true, artifacts: true });
    mount();
    expect(screen.getByRole("link", { name: "Files" })).toBeInTheDocument();
  });

  it("is shown by an older engine that does not say", () => {
    mocks.app = appWith(undefined);
    mount();
    expect(screen.getByRole("link", { name: "Files" })).toBeInTheDocument();
  });

  it("is hidden when artifacts are off, and the other links stay", () => {
    mocks.app = appWith({ sandbox: false, artifacts: false });
    mount();
    expect(screen.queryByRole("link", { name: "Files" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Scheduled" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connectors" })).toBeInTheDocument();
  });
});
