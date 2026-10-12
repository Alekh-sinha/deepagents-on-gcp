import { useEffect, useRef, type ReactNode } from "react";

/** A floating panel anchored to its parent (which must be `relative`). Closes on Escape or a click outside. */
export function Popover({ open, onClose, children, className = "", align = "left", above = false, label }: {
  open: boolean; onClose: () => void; children: ReactNode; className?: string; align?: "left" | "right"; above?: boolean; label?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) onClose(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    // a tick later, so the click that opened the panel is not taken for a click outside it
    const t = setTimeout(() => document.addEventListener("mousedown", away), 0);
    document.addEventListener("keydown", esc);
    return () => { clearTimeout(t); document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div ref={ref} role="dialog" aria-label={label}
      className={`absolute z-40 ${above ? "bottom-full mb-2" : "top-full mt-2"} ${align === "right" ? "right-0" : "left-0"} rounded-xl border border-line bg-panel shadow-pop ${className}`}>
      {children}
    </div>
  );
}
