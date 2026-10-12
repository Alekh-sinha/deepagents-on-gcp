import { render, screen, waitFor } from "@testing-library/react";

const auth = vi.hoisted(() => ({ state: { status: "signedOut", config: { mode: "google", client_id: "client-123" } } as any, signInWithGoogle: vi.fn(), retry: vi.fn() }));
vi.mock("../state/auth", () => ({ useAuth: () => auth }));

import { LoginPage } from "./LoginPage";

afterEach(() => { delete (window as any).google; document.querySelectorAll('script[src*="gsi/client"]').forEach((s) => s.remove()); });

it("shows Google's button, configured with this app's client id, once Google's script is there", async () => {
  const initialize = vi.fn();
  const renderButton = vi.fn();
  (window as any).google = { accounts: { id: { initialize, renderButton } } };
  render(<LoginPage />);
  await waitFor(() => expect(renderButton).toHaveBeenCalled());
  expect(initialize.mock.calls[0][0]).toMatchObject({ client_id: "client-123", ux_mode: "popup" });
  initialize.mock.calls[0][0].callback({ credential: "abc" });                     // what Google calls when the person has signed in
  expect(auth.signInWithGoogle).toHaveBeenCalledWith("abc");
});

it("says so plainly when Google's script cannot be loaded", async () => {
  render(<LoginPage />);
  const script = document.querySelector<HTMLScriptElement>('script[src*="gsi/client"]')!;
  script.dispatchEvent(new Event("error"));
  expect(await screen.findByRole("alert")).toHaveTextContent(/could not be loaded/);
});

it("shows why you were sent back here", () => {
  (window as any).google = { accounts: { id: { initialize: vi.fn(), renderButton: vi.fn() } } };
  auth.state = { status: "signedOut", config: { mode: "google", client_id: "c" }, message: "Your session ended. Please sign in again." };
  render(<LoginPage />);
  expect(screen.getByRole("alert")).toHaveTextContent("Your session ended");
});

it("has no Google button when sign-in is handled outside the page, and offers to try again", () => {
  auth.state = { status: "signedOut", config: { mode: "iap" } };
  render(<LoginPage />);
  expect(screen.queryByTestId("google-button")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
});
