import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import { ChatView } from "./components/chat/ChatView";
import { Sidebar } from "./components/Sidebar";
import { ConnectorsPage } from "./pages/ConnectorsPage";
import { FilesPage } from "./pages/FilesPage";
import { LoginPage } from "./pages/LoginPage";
import { SchedulesPage } from "./pages/SchedulesPage";
import { SettingsPage } from "./pages/SettingsPage";
import { AppProvider } from "./state/app";
import { AuthProvider, useAuth } from "./state/auth";
import { NavProvider } from "./state/nav";

function Chat() {
  const { sessionId } = useParams();
  return <ChatView sessionId={sessionId ?? null} />;
}

function Shell() {
  return (
    <AppProvider><NavProvider>
      <div className="flex h-full">
        <Sidebar />
        <main className="min-w-0 flex-1">
          <Routes>
            <Route path="/" element={<Navigate to="/chat" replace />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/chat/:sessionId" element={<Chat />} />
            <Route path="/schedules" element={<SchedulesPage />} />
            <Route path="/connectors" element={<ConnectorsPage />} />
            <Route path="/files" element={<FilesPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/chat" replace />} />
          </Routes>
        </main>
      </div>
    </NavProvider></AppProvider>
  );
}

function Gate() {
  const { state, retry } = useAuth();
  if (state.status === "loading") return <div className="flex h-full items-center justify-center text-[13px] text-faint" role="status">Loading…</div>;
  if (state.status === "unreachable") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
        <h1 className="text-lg font-semibold">Cannot reach privai</h1>
        <p className="max-w-md text-[14px] text-muted">{state.message}</p>
        <p className="max-w-md text-[12.5px] text-faint">Start the backend with <code className="rounded bg-chrome px-1">uv run pinboard web</code>, then try again.</p>
        <button type="button" className="btn-primary" onClick={retry}>Try again</button>
      </div>
    );
  }
  if (state.status === "signedOut") return <LoginPage />;
  return <Shell />;
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider><Gate /></AuthProvider>
    </BrowserRouter>
  );
}
