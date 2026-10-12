import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";

/** On a narrow screen the sidebar is a slide-over menu; this says whether it is open. */
interface NavApi { open: boolean; setOpen: (v: boolean) => void }
const Ctx = createContext<NavApi>({ open: false, setOpen: () => undefined });

export function NavProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  useEffect(() => setOpen(false), [pathname]);   // going somewhere closes the menu
  const value = useMemo(() => ({ open, setOpen }), [open]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export const useNav = () => useContext(Ctx);
