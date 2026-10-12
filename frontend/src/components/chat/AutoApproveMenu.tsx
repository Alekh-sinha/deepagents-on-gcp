import { useEffect, useState } from "react";
import type { AutoApprove, AutoApproveOptions } from "../../api/types";
import { Icon } from "../ui/Icon";
import { Popover } from "../ui/Popover";
import { Toggle } from "../ui/Toggle";

const KIND_LABEL: Record<string, { label: string; hint: string }> = {
  shell: { label: "Run commands", hint: "Shell commands in the sandbox" },
  github: { label: "Change GitHub", hint: "Branches, commits and pull requests" },
  mcp: { label: "Use connected tools", hint: "Tools from the MCP servers you added" },
  other: { label: "Other tools", hint: "Tools privai does not recognise" },
  email: { label: "Send email", hint: "Off unless you turn it on" },
  plan: { label: "Approve plans", hint: "Off unless you turn it on" },
};

/** the order people think of them in: what runs, what changes things elsewhere, then the rest */
const ORDER = ["shell", "github", "mcp", "other", "email", "plan"];

export function summarize(value: AutoApprove): string {
  if (!value) return "Ask before acting";
  const names = value.kinds.map((k) => KIND_LABEL[k]?.label.replace(/^(Run|Change|Use|Send|Approve) /, "") ?? k);
  return `Auto-approving ${names.join(", ")}`;
}

export function AutoApproveMenu({ value, options, onChange, disabled }: { value: AutoApprove; options: AutoApproveOptions; onChange: (v: AutoApprove) => void; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [kinds, setKinds] = useState<string[]>(value?.kinds ?? options.default_kinds);
  const [cap, setCap] = useState<number>(value?.max_per_turn ?? options.default_max_per_turn);
  useEffect(() => { setKinds(value?.kinds ?? options.default_kinds); setCap(value?.max_per_turn ?? options.default_max_per_turn); }, [value, options]);

  const on = Boolean(value);
  const save = (nextKinds: string[], nextCap: number) => nextKinds.length ? onChange({ kinds: nextKinds, max_per_turn: Math.min(Math.max(1, nextCap), options.max_per_turn_limit) }) : onChange(null);
  const toggleKind = (k: string) => { const next = kinds.includes(k) ? kinds.filter((x) => x !== k) : [...kinds, k]; setKinds(next); if (on) save(next, cap); };

  return (
    <div className="relative">
      <button type="button" onClick={() => setOpen((o) => !o)} disabled={disabled} aria-haspopup="dialog" aria-expanded={open} title="Automatic approval"
        className={`btn !px-2 ${on ? "bg-accentSoft text-accentInk hover:bg-accentSoft/70" : "text-muted hover:bg-chromeHover hover:text-ink"}`}>
        <Icon name={on ? "bolt" : "shield"} size={14} />
        <span className="max-w-[220px] truncate">{summarize(value)}</span>
      </button>
      <Popover open={open} onClose={() => setOpen(false)} above label="Automatic approval" className="w-[340px] p-3">
        <div className="flex items-center gap-3">
          <div className="min-w-0 flex-1"><div className="text-[13.5px] font-semibold">Approve automatically</div><div className="text-[12px] text-muted">Let privai go ahead without asking, for the kinds you choose.</div></div>
          <Toggle label="Approve automatically" checked={on} onChange={(v) => (v ? save(kinds.length ? kinds : options.default_kinds, cap) : onChange(null))} />
        </div>
        <ul className="mt-3 space-y-1.5">
          {[...options.kinds].sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b)).map((k) => (
            <li key={k}>
              <label className="flex cursor-pointer items-start gap-2.5 rounded-lg px-1.5 py-1 hover:bg-paper">
                <input type="checkbox" className="mt-1 accent-[var(--accent)]" checked={kinds.includes(k)} onChange={() => toggleKind(k)} />
                <span><span className="block text-[13px] font-medium">{KIND_LABEL[k]?.label ?? k}</span><span className="block text-[11.5px] text-faint">{KIND_LABEL[k]?.hint ?? ""}</span></span>
              </label>
            </li>
          ))}
        </ul>
        <div className="mt-3 flex items-center gap-2 border-t border-line pt-3">
          <label htmlFor="auto-cap" className="text-[12.5px] text-muted">Stop and ask after</label>
          <input id="auto-cap" type="number" min={1} max={options.max_per_turn_limit} className="input !w-16 !py-1 text-center" value={cap}
            onChange={(e) => setCap(Number(e.target.value) || 1)} onBlur={() => on && save(kinds, cap)} />
          <span className="text-[12.5px] text-muted">approvals in one turn</span>
        </div>
        <p className="mt-3 rounded-lg bg-paper px-2.5 py-2 text-[11.5px] leading-snug text-muted">
          Questions are always yours to answer.{options.never_tools.length > 0 && <> Always asks first: {options.never_tools.join(", ")}.</>} This stays on for this conversation until you turn it off.
        </p>
      </Popover>
    </div>
  );
}
