import { useState } from "react";
import type { Todo } from "../../api/types";
import { Icon } from "../ui/Icon";

export function TodoPanel({ todos }: { todos: Todo[] | null }) {
  const [open, setOpen] = useState(true);
  if (!todos || todos.length === 0) return null;
  const done = todos.filter((t) => t.status === "completed").length;
  return (
    <div className="mx-auto w-full max-w-3xl px-4"><div className="mb-2 rounded-xl border border-line bg-panel shadow-card">
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open} className="flex w-full items-center gap-2 px-3.5 py-2 text-left">
        <Icon name="list" size={14} className="text-accentInk" />
        <span className="text-[13px] font-medium">Progress</span>
        <span className="text-[12px] text-muted">{done} of {todos.length} done</span>
        <span className="ml-2 h-1.5 flex-1 overflow-hidden rounded-full bg-line"><span className="block h-full rounded-full bg-accent transition-all" style={{ width: `${(done / todos.length) * 100}%` }} /></span>
        <Icon name={open ? "chevronUp" : "chevronDown"} size={14} className="text-faint" />
      </button>
      {open && (
        <ul className="space-y-1 border-t border-line px-3.5 py-2.5">
          {todos.map((t, i) => (
            <li key={i} className="flex items-start gap-2 text-[13px]">
              <span className="mt-0.5">
                {t.status === "completed" ? <Icon name="check" size={14} className="text-ok" label="done" />
                  : t.status === "in_progress" ? <span className="mt-1 block h-2.5 w-2.5 animate-pulse rounded-full bg-accent" aria-label="in progress" />
                  : <span className="mt-1 block h-2.5 w-2.5 rounded-full border border-lineStrong" aria-label="to do" />}
              </span>
              <span className={t.status === "completed" ? "text-faint line-through" : t.status === "in_progress" ? "font-medium" : "text-muted"}>{t.content}</span>
            </li>
          ))}
        </ul>
      )}
    </div></div>
  );
}
