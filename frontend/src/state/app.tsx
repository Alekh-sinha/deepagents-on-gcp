import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { AutoApprove, Config, ModelInfo, Session } from "../api/types";
import { useSignedIn } from "./auth";

const MODEL_KEY = "privai.model";
const read = (k: string): string | null => { try { return localStorage.getItem(k); } catch { return null; } };
const write = (k: string, v: string | null) => { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* private mode: the choice just is not remembered */ } };

interface AppApi {
  config: Config;
  user: string;
  models: ModelInfo[];
  defaultModel: string;
  sessions: Session[];
  sessionsLoaded: boolean;
  refreshSessions: () => Promise<void>;
  upsertSession: (s: Session) => void;
  /** the model a NEW chat starts with (remembered between visits) */
  newChatModel: string;
  setNewChatModel: (id: string) => void;
  /** automatic approval chosen before a chat exists; applied when it is created */
  newChatAuto: AutoApprove;
  setNewChatAuto: (v: AutoApprove) => void;
  modelById: (id: string | null | undefined) => ModelInfo | undefined;
  /** whether the assistant may pick a cheaper model for each helper (policy models.helper_model_choice) */
  helperChoice: boolean;
  /** how many requests wait for an answer anywhere (shown in the sidebar) */
  pendingCount: number;
  refreshPending: () => Promise<void>;
}

const Ctx = createContext<AppApi | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const { appConfig, user } = useSignedIn();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loaded, setLoaded] = useState(false);
  const known = new Set(appConfig.models.map((m) => m.id));
  const [model, setModel] = useState<string>(() => { const saved = read(MODEL_KEY); return saved && known.has(saved) ? saved : appConfig.default_model; });
  const [auto, setAuto] = useState<AutoApprove>(null);
  const [helperChoice, setHelperChoice] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);
  const refreshPending = useCallback(async () => { try { setPendingCount((await api.approvals("pending")).length); } catch { /* keep the old number */ } }, []);
  useEffect(() => { void refreshPending(); const t = setInterval(() => void refreshPending(), 20000); return () => clearInterval(t); }, [refreshPending]);
  useEffect(() => { api.models().then((m) => setHelperChoice(m.helper_model_choice)).catch(() => undefined); }, []);

  const refreshSessions = useCallback(async () => {
    try { setSessions(await api.sessions()); } catch { /* the list is a convenience; a failed refresh keeps the old one */ }
    setLoaded(true);
  }, []);
  useEffect(() => { void refreshSessions(); }, [refreshSessions]);

  const value = useMemo<AppApi>(() => ({
    config: appConfig, user, defaultModel: appConfig.default_model,
    // an engine that has not been updated yet lists models without ranks: fall back to the order it gave them in
    models: appConfig.models.map((m, i) => ({ ...m, rank: m.rank ?? i + 1, use_for: m.use_for ?? "" })).sort((a, b) => a.rank - b.rank),
    sessions, sessionsLoaded: loaded, refreshSessions,
    upsertSession: (s) => setSessions((list) => (list.some((x) => x.id === s.id) ? list.map((x) => (x.id === s.id ? s : x)) : [s, ...list])),
    newChatModel: model, setNewChatModel: (id) => { setModel(id); write(MODEL_KEY, id); },
    newChatAuto: auto, setNewChatAuto: setAuto,
    modelById: (id) => appConfig.models.find((m) => m.id === (id || appConfig.default_model)),
    helperChoice, pendingCount, refreshPending,
  }), [appConfig, user, sessions, loaded, refreshSessions, model, auto, helperChoice, pendingCount, refreshPending]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppApi {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp outside AppProvider");
  return v;
}
