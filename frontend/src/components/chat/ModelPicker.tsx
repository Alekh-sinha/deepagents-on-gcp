import { useState } from "react";
import type { ModelInfo } from "../../api/types";
import { Icon } from "../ui/Icon";
import { Popover } from "../ui/Popover";

/** Rank as a few filled pips: cheap and quick on the left, strongest and dearest on the right. */
function Pips({ rank, max }: { rank: number; max: number }) {
  const filled = Math.max(1, Math.round((rank / Math.max(max, 1)) * 5));
  return <span className="flex gap-0.5" aria-label={`rank ${rank} of ${max}`}>{[0, 1, 2, 3, 4].map((i) => <span key={i} className={`h-1.5 w-1.5 rounded-full ${i < filled ? "bg-accent" : "bg-lineStrong"}`} />)}</span>;
}

export function ModelPicker({ models, value, onChange, helperChoice, disabled }: { models: ModelInfo[]; value: string; onChange: (id: string) => void; helperChoice: boolean; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const current = models.find((m) => m.id === value);
  const max = Math.max(...models.map((m) => m.rank), 1);
  return (
    <div className="relative">
      <button type="button" className="btn-ghost !px-2" onClick={() => setOpen((o) => !o)} aria-haspopup="dialog" aria-expanded={open} disabled={disabled} title="Choose the model">
        <Icon name="sparkle" size={14} className="text-accentInk" />
        <span className="max-w-[160px] truncate">{current?.label ?? value}</span>
        <Icon name="chevronDown" size={12} />
      </button>
      <Popover open={open} onClose={() => setOpen(false)} above label="Choose a model" className="w-[360px] p-1.5">
        <div className="max-h-[360px] overflow-auto">
          {models.map((m) => (
            <button key={m.id} type="button" role="option" aria-selected={m.id === value} onClick={() => { onChange(m.id); setOpen(false); }}
              className={`flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left hover:bg-paper ${m.id === value ? "bg-accentSoft/60" : ""}`}>
              <span className="mt-1 w-4">{m.id === value && <Icon name="check" size={14} className="text-accentInk" />}</span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2"><span className="text-[13.5px] font-medium">{m.label}</span><Pips rank={m.rank} max={max} /></span>
                {m.use_for && <span className="mt-0.5 block text-[12px] leading-snug text-muted">{m.use_for}</span>}
              </span>
            </button>
          ))}
        </div>
        {helperChoice && <p className="border-t border-line px-3 pb-1.5 pt-2 text-[11.5px] text-faint">Helpers can run on this model or any cheaper one, chosen by the assistant for each task.</p>}
      </Popover>
    </div>
  );
}
