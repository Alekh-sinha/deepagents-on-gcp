import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiError, api, setUnauthorizedHandler } from "../api/client";
import type { AuthConfig, Config } from "../api/types";

type AuthState =
  | { status: "loading" }
  | { status: "unreachable"; message: string }
  | { status: "signedOut"; config: AuthConfig; message?: string }
  | { status: "signedIn"; config: AuthConfig; user: string; appConfig: Config };

interface AuthApi { state: AuthState; signInWithGoogle: (credential: string) => Promise<void>; signOut: () => Promise<void>; refreshConfig: () => Promise<void>; retry: () => void }

const Ctx = createContext<AuthApi | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  const boot = useCallback(async () => {
    setState({ status: "loading" });
    let config: AuthConfig;
    try {
      config = await api.authConfig();
    } catch (e) {
      setState({ status: "unreachable", message: e instanceof ApiError ? e.message : "Cannot reach the server." });
      return;
    }
    try {
      const appConfig = await api.me();
      setState({ status: "signedIn", config, user: appConfig.user, appConfig });
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) setState({ status: "signedOut", config, message: e.status === 403 ? e.message : undefined });
      else setState({ status: "unreachable", message: e instanceof ApiError ? e.message : "Something went wrong." });
    }
  }, []);

  useEffect(() => { void boot(); }, [boot, attempt]);

  useEffect(() => {   // any call that finds the session gone sends the user back to the sign-in page
    setUnauthorizedHandler(() => setState((s) => (s.status === "signedIn" ? { status: "signedOut", config: s.config, message: "Your session ended. Please sign in again." } : s)));
    return () => setUnauthorizedHandler(null);
  }, []);

  const value = useMemo<AuthApi>(() => ({
    state,
    retry: () => setAttempt((n) => n + 1),
    signInWithGoogle: async (credential) => {
      try {
        await api.signInWithGoogle(credential);
      } catch (e) {
        setState((s) => (s.status === "signedOut" ? { ...s, message: e instanceof ApiError ? e.message : "Sign-in failed." } : s));
        return;
      }
      await boot();
    },
    signOut: async () => {
      try { await api.signOut(); } catch { /* signing out of a session that is already gone is fine */ }
      await boot();
    },
    refreshConfig: async () => {
      const appConfig = await api.me();
      setState((s) => (s.status === "signedIn" ? { ...s, appConfig } : s));
    },
  }), [state, boot]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthApi {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}

export function useSignedIn() {
  const { state } = useAuth();
  if (state.status !== "signedIn") throw new Error("not signed in");
  return state;
}
