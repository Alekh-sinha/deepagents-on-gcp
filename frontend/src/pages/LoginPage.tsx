import { useEffect, useRef, useState } from "react";
import { useAuth } from "../state/auth";
import { Brand } from "../components/Sidebar";

const GIS = "https://accounts.google.com/gsi/client";

declare global {
  interface Window { google?: { accounts: { id: { initialize: (o: any) => void; renderButton: (el: HTMLElement, o: any) => void } } } }
}

/** Loads Google's sign-in script once. */
function loadGoogle(): Promise<void> {
  if (window.google?.accounts) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${GIS}"]`);
    const s = existing ?? Object.assign(document.createElement("script"), { src: GIS, async: true, defer: true });
    s.addEventListener("load", () => resolve());
    s.addEventListener("error", () => reject(new Error("Google's sign-in script could not be loaded. Check your connection or any content blocker.")));
    if (!existing) document.head.appendChild(s);
  });
}

export function LoginPage() {
  const { state, signInWithGoogle, retry } = useAuth();
  const button = useRef<HTMLDivElement>(null);
  const [problem, setProblem] = useState("");
  const config = state.status === "signedOut" ? state.config : null;
  const clientId = config?.mode === "google" ? config.client_id : "";

  useEffect(() => {
    if (!clientId) return;
    let live = true;
    loadGoogle().then(() => {
      if (!live || !button.current || !window.google) return;
      window.google.accounts.id.initialize({ client_id: clientId, callback: (r: { credential: string }) => void signInWithGoogle(r.credential), ux_mode: "popup" });
      window.google.accounts.id.renderButton(button.current, { theme: "outline", size: "large", shape: "pill", text: "signin_with", width: 280 });
    }).catch((e: Error) => live && setProblem(e.message));
    return () => { live = false; };
  }, [clientId, signInWithGoogle]);

  const message = state.status === "signedOut" ? state.message : undefined;
  return (
    <main className="flex min-h-full items-center justify-center bg-paper px-4">
      <div className="card w-full max-w-sm p-8 text-center">
        <div className="mb-6 flex justify-center"><Brand /></div>
        <h1 className="text-[22px] font-semibold tracking-tight">Welcome back</h1>
        <p className="mt-1.5 text-[14px] text-muted">Sign in to continue to your assistant.</p>
        <div className="mt-7 flex min-h-[44px] justify-center">
          {config?.mode === "google" ? <div ref={button} data-testid="google-button" /> : <p className="text-[13px] text-muted">Sign-in is handled outside this page. Reload to try again.</p>}
        </div>
        {(message || problem) && <p role="alert" className="mt-4 rounded-lg bg-dangerSoft px-3 py-2 text-[13px] text-danger">{problem || message}</p>}
        {config?.mode !== "google" && <button type="button" className="btn-outline mt-4" onClick={retry}>Try again</button>}
        <p className="mt-6 text-[11.5px] text-faint">Only accounts on the allow-list can sign in.</p>
      </div>
    </main>
  );
}
