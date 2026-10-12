import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const api = vi.hoisted(() => ({ authConfig: vi.fn(), me: vi.fn(), signInWithGoogle: vi.fn(), signOut: vi.fn() }));
let unauthorized: (() => void) | null = null;
vi.mock("../api/client", () => ({
  api,
  ApiError: class ApiError extends Error { constructor(public status: number, m: string) { super(m); } },
  setUnauthorizedHandler: (fn: (() => void) | null) => { unauthorized = fn; },
}));

import { ApiError } from "../api/client";
import { AuthProvider, useAuth } from "./auth";

const config = { user: "me@example.com", models: [], default_model: "m" } as any;
function Probe() {
  const { state, signInWithGoogle, signOut, retry } = useAuth();
  return (
    <div>
      <p data-testid="status">{state.status}</p>
      {state.status === "signedIn" && <p>user:{state.user}</p>}
      {state.status === "signedOut" && <p>message:{state.message ?? ""} mode:{state.config.mode}</p>}
      {state.status === "unreachable" && <p>why:{state.message}</p>}
      <button onClick={() => void signInWithGoogle("cred")}>sign in</button>
      <button onClick={() => void signOut()}>sign out</button>
      <button onClick={retry}>retry</button>
    </div>
  );
}
const mount = () => render(<AuthProvider><Probe /></AuthProvider>);
const status = () => screen.getByTestId("status").textContent;

beforeEach(() => { Object.values(api).forEach((f) => f.mockReset()); unauthorized = null; api.authConfig.mockResolvedValue({ mode: "google", client_id: "cid" }); });

describe("finding out who you are", () => {
  it("is signed in when the server knows you", async () => {
    api.me.mockResolvedValue(config);
    mount();
    expect(status()).toBe("loading");
    await waitFor(() => expect(status()).toBe("signedIn"));
    expect(screen.getByText("user:me@example.com")).toBeInTheDocument();
  });

  it("asks you to sign in when it does not", async () => {
    api.me.mockRejectedValue(new ApiError(401, "not signed in"));
    mount();
    await waitFor(() => expect(status()).toBe("signedOut"));
    expect(screen.getByText("message: mode:google")).toBeInTheDocument();
  });

  it("explains a refusal for an account that is not allowed", async () => {
    api.me.mockRejectedValue(new ApiError(403, "this Google account is not allowed to use privai"));
    mount();
    await waitFor(() => expect(status()).toBe("signedOut"));
    expect(screen.getByText(/message:this Google account is not allowed/)).toBeInTheDocument();
  });

  it("says the server cannot be reached, and can try again", async () => {
    api.authConfig.mockRejectedValueOnce(new ApiError(0, "Cannot reach the server. Is the privai backend running?"));
    api.me.mockResolvedValue(config);
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(status()).toBe("unreachable"));
    expect(screen.getByText(/Cannot reach the server/)).toBeInTheDocument();
    await user.click(screen.getByText("retry"));
    await waitFor(() => expect(status()).toBe("signedIn"));
  });

  it("treats an unexpected server error as unreachable, not as signed out", async () => {
    api.me.mockRejectedValue(new ApiError(502, "The server answered 502."));
    mount();
    await waitFor(() => expect(status()).toBe("unreachable"));
  });
});

describe("signing in and out", () => {
  it("sends the Google credential and then loads who you are", async () => {
    api.me.mockRejectedValueOnce(new ApiError(401, "x")).mockResolvedValue(config);
    api.signInWithGoogle.mockResolvedValue({ email: "me@example.com" });
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(status()).toBe("signedOut"));
    await user.click(screen.getByText("sign in"));
    expect(api.signInWithGoogle).toHaveBeenCalledWith("cred");
    await waitFor(() => expect(status()).toBe("signedIn"));
  });

  it("keeps you on the sign-in page, with the reason, when the server refuses the credential", async () => {
    api.me.mockRejectedValue(new ApiError(401, "x"));
    api.signInWithGoogle.mockRejectedValue(new ApiError(403, "this Google account is not allowed to use privai"));
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(status()).toBe("signedOut"));
    await user.click(screen.getByText("sign in"));
    await waitFor(() => expect(screen.getByText(/message:this Google account is not allowed/)).toBeInTheDocument());
    expect(status()).toBe("signedOut");
  });

  it("signs out even if the server already forgot the session", async () => {
    api.me.mockResolvedValueOnce(config).mockRejectedValue(new ApiError(401, "x"));
    api.signOut.mockRejectedValue(new ApiError(401, "gone"));
    const user = userEvent.setup();
    mount();
    await waitFor(() => expect(status()).toBe("signedIn"));
    await user.click(screen.getByText("sign out"));
    await waitFor(() => expect(status()).toBe("signedOut"));
  });

  it("sends you back to sign in when any call finds the session gone", async () => {
    api.me.mockResolvedValue(config);
    mount();
    await waitFor(() => expect(status()).toBe("signedIn"));
    act(() => unauthorized?.());
    await waitFor(() => expect(status()).toBe("signedOut"));
    expect(screen.getByText(/message:Your session ended/)).toBeInTheDocument();
  });
});
