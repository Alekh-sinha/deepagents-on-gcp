import { useEffect, useRef, useState } from "react";
import type { AutoApprove, AutoApproveOptions, ModelInfo } from "../../api/types";
import { Icon } from "../ui/Icon";
import { AutoApproveMenu } from "./AutoApproveMenu";
import { ModelPicker } from "./ModelPicker";

export interface ComposerProps {
  onSend: (text: string) => void;
  onStop: () => void;
  onTyping?: () => void;
  running: boolean;
  /** why sending is not possible right now (shown instead of the placeholder) */
  blockedReason?: string;
  models: ModelInfo[];
  model: string;
  onModel: (id: string) => void;
  helperChoice: boolean;
  autoApprove: AutoApprove;
  autoOptions: AutoApproveOptions;
  onAutoApprove: (v: AutoApprove) => void;
  placeholder?: string;
  autoFocus?: boolean;
  /** text to put in the box (an example the user picked); a new nonce puts it there again */
  seed?: { text: string; nonce: number } | null;
}

export function Composer(p: ComposerProps) {
  const [text, setText] = useState("");
  const box = useRef<HTMLTextAreaElement>(null);
  const warned = useRef(false);

  useEffect(() => { if (p.autoFocus) box.current?.focus(); }, [p.autoFocus]);
  useEffect(() => { if (p.seed) { setText(p.seed.text); box.current?.focus(); } }, [p.seed?.nonce]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {   // grow with the text, up to a limit
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [text]);

  const blocked = Boolean(p.blockedReason);
  const submit = () => {
    const clean = text.trim();
    if (!clean || blocked || p.running) return;
    p.onSend(clean);
    setText("");
    warned.current = false;
  };

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-4">
      <div className={`rounded-2xl border bg-panel shadow-card transition-colors focus-within:border-accent/60 ${blocked ? "border-line" : "border-lineStrong"}`}>
        <textarea ref={box} rows={1} value={text} aria-label="Message"
          placeholder={p.blockedReason ?? p.placeholder ?? "Ask privai to do something…"} disabled={blocked}
          className="no-ring block max-h-[220px] w-full resize-none bg-transparent px-4 pb-1 pt-3.5 text-[15px] outline-none placeholder:text-faint disabled:cursor-not-allowed"
          onChange={(e) => { setText(e.target.value); if (!warned.current && e.target.value) { warned.current = true; p.onTyping?.(); } }}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }} />
        <div className="flex items-center gap-1 px-2 pb-2">
          <ModelPicker models={p.models} value={p.model} onChange={p.onModel} helperChoice={p.helperChoice} />
          <AutoApproveMenu value={p.autoApprove} options={p.autoOptions} onChange={p.onAutoApprove} />
          <div className="ml-auto">
            {p.running ? (
              <button type="button" className="flex h-8 w-8 items-center justify-center rounded-full bg-ink text-white hover:bg-ink/85" onClick={p.onStop} aria-label="Stop"><Icon name="stop" size={13} /></button>
            ) : (
              <button type="button" className="flex h-8 w-8 items-center justify-center rounded-full bg-accent text-white transition-opacity hover:bg-accent/90 disabled:opacity-30" onClick={submit} disabled={!text.trim() || blocked} aria-label="Send"><Icon name="send" size={16} /></button>
            )}
          </div>
        </div>
      </div>
      <p className="mt-1.5 text-center text-[11.5px] text-faint">privai can make mistakes. It asks before it sends, commits or runs anything that matters.</p>
    </div>
  );
}
