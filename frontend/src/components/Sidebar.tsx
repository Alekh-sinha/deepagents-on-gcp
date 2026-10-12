import { useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { groupSessions } from "../lib/format";
import { useApp } from "../state/app";
import { useNav } from "../state/nav";
import { useAuth } from "../state/auth";
import { Icon, type IconName } from "./ui/Icon";
import { Popover } from "./ui/Popover";

const NAV: { to: string; label: string; icon: IconName }[] = [
  { to: "/schedules", label: "Scheduled", icon: "clock" },
  { to: "/connectors", label: "Connectors", icon: "plug" },
  { to: "/files", label: "Files", icon: "file" },
];

export function Brand() {
  return (
    <div className="flex items-center gap-2 px-1">
      <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent text-white"><svg width="16" height="16" viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M10 22V10h6a4 4 0 010 8h-6" /></svg></span>
      <span className="text-[17px] font-semibold tracking-tight">privai</span>
    </div>
  );
}

export function Sidebar() {
  const app = useApp();
  const auth = useAuth();
  const navigate = useNavigate();
  const [menu, setMenu] = useState(false);
  const nav = useNav();
  const groups = groupSessions(app.sessions);

  const openWaiting = async () => {
    const [first] = await api.approvals("pending").catch(() => []);
    if (!first) return;
    const turn = await api.turn(first.run_id).catch(() => null);
    if (turn?.session_id) navigate(`/chat/${turn.session_id}`);
  };

  return (
    <>
    {nav.open && <div className="fixed inset-0 z-40 bg-ink/30 md:hidden" onClick={() => nav.setOpen(false)} aria-hidden />}
    <aside className={`${nav.open ? "fixed inset-y-0 left-0 z-50 flex shadow-pop" : "hidden"} h-full w-[264px] shrink-0 flex-col border-r border-line bg-chrome md:relative md:z-auto md:flex md:shadow-none`} aria-label="Navigation">
      <div className="px-3 pb-2 pt-3.5"><Brand /></div>
      <div className="px-3 pb-2">
        <NavLink to="/chat" end className="btn-outline w-full justify-start !rounded-xl !py-2" onClick={() => app.setNewChatAuto(null)}><Icon name="plus" size={15} />New chat</NavLink>
      </div>

      {app.pendingCount > 0 && (
        <button type="button" onClick={openWaiting} className="mx-3 mb-2 flex items-center gap-2 rounded-xl border border-warn/40 bg-warnSoft px-3 py-2 text-left text-[12.5px] font-medium text-warn hover:bg-warnSoft/70">
          <span className="pulse h-2 w-2 rounded-full bg-warn" />{app.pendingCount} waiting for your answer
        </button>
      )}

      <nav className="space-y-0.5 px-2 pb-2">
        {NAV.filter((n) => n.to !== "/files" || app.config.features?.artifacts !== false).map((n) => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => `flex items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-[13.5px] ${isActive ? "bg-chromeHover font-medium text-ink" : "text-muted hover:bg-chromeHover hover:text-ink"}`}>
            <Icon name={n.icon} size={15} />{n.label}
          </NavLink>
        ))}
      </nav>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2" aria-label="Conversations">
        {groups.map((g) => (
          <div key={g.label} className="mb-2">
            <div className="px-2.5 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wide text-faint">{g.label}</div>
            {g.sessions.map((s) => (
              <NavLink key={s.id} to={`/chat/${s.id}`} className={({ isActive }) => `flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13.5px] ${isActive ? "bg-chromeHover font-medium text-ink" : "text-ink/80 hover:bg-chromeHover"}`}>
                {s.schedule_id && <Icon name="clock" size={12} className="text-accentInk" label="scheduled" />}
                <span className="min-w-0 flex-1 truncate">{s.title || "New chat"}</span>
              </NavLink>
            ))}
          </div>
        ))}
        {app.sessionsLoaded && groups.length === 0 && <p className="px-3 py-4 text-[12.5px] text-faint">Your conversations will appear here.</p>}
      </div>

      <div className="relative border-t border-line p-2">
        <button type="button" onClick={() => setMenu((o) => !o)} aria-haspopup="menu" aria-expanded={menu} className="flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-left hover:bg-chromeHover">
          <span className="flex h-7 w-7 items-center justify-center rounded-full bg-accentSoft text-[12px] font-semibold uppercase text-accentInk">{app.user[0]}</span>
          <span className="min-w-0 flex-1 truncate text-[13px]">{app.user}</span>
          <Icon name="chevronUp" size={13} className="text-faint" />
        </button>
        <Popover open={menu} onClose={() => setMenu(false)} above label="Account" className="w-[240px] p-1.5">
          <NavLink to="/settings" onClick={() => setMenu(false)} className="flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[13px] hover:bg-paper"><Icon name="settings" size={15} />Settings</NavLink>
          <button type="button" onClick={() => void auth.signOut()} className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] hover:bg-paper"><Icon name="logout" size={15} />Sign out</button>
        </Popover>
      </div>
    </aside>
    </>
  );
}

/** The button that opens the menu on a narrow screen. */
export function MenuButton() {
  const nav = useNav();
  return <button type="button" className="btn-ghost -ml-2 !px-2 md:hidden" onClick={() => nav.setOpen(true)} aria-label="Open the menu"><Icon name="panel" size={18} /></button>;
}
