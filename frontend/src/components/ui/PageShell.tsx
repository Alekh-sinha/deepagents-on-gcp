import type { ReactNode } from "react";
import { MenuButton } from "../Sidebar";

export function PageShell({ title, subtitle, actions, children }: { title: string; subtitle?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="h-full overflow-y-auto bg-canvas">
      <div className="mx-auto w-full max-w-4xl px-4 py-6 md:px-6 md:py-8">
        <div className="mb-3 md:hidden"><MenuButton /></div>
        <header className="mb-6 flex items-start gap-4">
          <div className="min-w-0 flex-1"><h1 className="text-[22px] font-semibold tracking-tight">{title}</h1>{subtitle && <p className="mt-1 max-w-2xl text-[14px] text-muted">{subtitle}</p>}</div>
          {actions}
        </header>
        {children}
      </div>
    </div>
  );
}

export function ErrorNote({ message, onDismiss }: { message: string; onDismiss?: () => void }) {
  if (!message) return null;
  return <div role="alert" className="mb-4 flex items-start gap-2 rounded-xl border border-danger/30 bg-dangerSoft px-3.5 py-2.5 text-[13px] text-danger"><span className="flex-1">{message}</span>{onDismiss && <button type="button" aria-label="Dismiss" onClick={onDismiss}>×</button>}</div>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="rounded-2xl border border-dashed border-lineStrong bg-paper px-6 py-12 text-center"><p className="text-[15px] font-medium">{title}</p>{children && <div className="mx-auto mt-1.5 max-w-md text-[13.5px] text-muted">{children}</div>}</div>;
}
