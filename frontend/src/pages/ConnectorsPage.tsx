import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import type { McpImportResult, McpServer } from "../api/types";
import { Icon } from "../components/ui/Icon";
import { Empty, ErrorNote, PageShell } from "../components/ui/PageShell";
import { Toggle } from "../components/ui/Toggle";
import { relativeTime } from "../lib/format";
import { useAuth } from "../state/auth";

const messageOf = (e: unknown) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Something went wrong.");
const EXAMPLE = `{
  "mcpServers": {
    "weather": {
      "url": "https://your-server.example.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}`;

/** Says where a server may live when the administrator limited it: inside the private network, or on the approved list. Nothing is shown when anything goes. */
function RuleNote() {
  const auth = useAuth();
  const mcp = auth.state.status === "signedIn" ? auth.state.appConfig.mcp : undefined;
  if (!mcp || mcp.scope !== "vpc_or_allowlist") return null;
  const ranges = mcp.vpc_ranges.length ? mcp.vpc_ranges.join(", ") : "none set";
  const hosts = mcp.allowed_hosts.length ? mcp.allowed_hosts.join(", ") : "none yet";
  return (
    <div className="card mb-6 p-4" role="note" aria-label="Which servers can be added">
      <h2 className="text-[14px] font-semibold">Which servers can be added</h2>
      <p className="mt-1 text-[13.5px] text-muted">
        Only servers <b className="font-medium text-ink">inside our private network</b> (<code className="rounded bg-chrome px-1">{ranges}</code>) are approved automatically,
        or servers on the <b className="font-medium text-ink">approved list</b> (<code className="rounded bg-chrome px-1">{hosts}</code>). Any other address is refused.
        To use another server, ask the administrator.
      </p>
    </div>
  );
}

function ImportBox({ onDone }: { onDone: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [results, setResults] = useState<McpImportResult[] | null>(null);

  const run = async () => {
    setBusy(true); setError(""); setResults(null);
    let parsed: string | object = text;
    try { parsed = JSON.parse(text); } catch { /* the server explains what is wrong with text that is not JSON */ }
    try {
      const out = await api.mcpImport(parsed);
      setResults(out.results);
      if (out.results.some((r) => r.saved)) { setText(""); onDone(); }
    } catch (e) { setError(messageOf(e)); } finally { setBusy(false); }
  };

  return (
    <div className="card mb-6 p-5">
      <h2 className="text-[15px] font-semibold">Add servers</h2>
      <p className="mt-1 text-[13.5px] text-muted">Paste the standard <code className="rounded bg-chrome px-1">mcpServers</code> JSON for servers that are already running and reachable by URL. Tokens are stored encrypted and are never shown again.</p>
      <textarea aria-label="Server configuration" className="input mt-3 min-h-[150px] font-mono text-[12.5px]" value={text} onChange={(e) => setText(e.target.value)} placeholder={EXAMPLE} spellCheck={false} />
      <ErrorNote message={error} />
      {results && (
        <ul className="mt-3 space-y-1.5" aria-label="Import results">
          {results.map((r) => (
            <li key={r.name} className={`flex items-start gap-2 rounded-lg px-3 py-2 text-[13px] ${r.reachable ? "bg-okSoft text-ok" : r.saved ? "bg-warnSoft text-warn" : "bg-dangerSoft text-danger"}`}>
              <Icon name={r.reachable ? "check" : "alert"} size={14} className="mt-0.5" />
              <span><b className="font-medium">{r.name}</b>: {r.reachable ? `connected, ${r.tool_count} tool${r.tool_count === 1 ? "" : "s"}` : r.saved ? `saved, but not reachable yet (${r.error})` : `not saved (${r.error})`}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-3"><button type="button" className="btn-accent" onClick={run} disabled={busy || !text.trim()}>{busy ? "Connecting…" : "Connect"}</button></div>
    </div>
  );
}

function ServerCard({ s, onChange, onError }: { s: McpServer; onChange: () => void; onError: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const host = (() => { try { return new URL(s.url).host; } catch { return s.url; } })();
  // what the person just chose shows at once; the server's answer (or a failure) then settles it
  const [enabled, setEnabled] = useState(s.enabled);
  const [asks, setAsks] = useState(s.requires_approval);
  const [free, setFree] = useState(() => new Set(s.no_approval_tools));
  useEffect(() => { setEnabled(s.enabled); setAsks(s.requires_approval); setFree(new Set(s.no_approval_tools)); }, [s]);
  const act = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn(); onChange(); } catch (e) { onError(messageOf(e)); onChange(); } finally { setBusy(false); } };
  const ok = !s.last_error && s.tool_count > 0;

  return (
    <li className="card overflow-hidden">
      <div className="flex items-start gap-3 p-4">
        <span className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${ok ? "bg-ok" : s.last_error ? "bg-danger" : "bg-faint"}`} aria-label={ok ? "connected" : s.last_error ? "not reachable" : "not tested"} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2"><h3 className="text-[15px] font-semibold">{s.name}</h3>{!enabled && <span className="chip">off</span>}{s.transport === "sse" && <span className="chip">sse</span>}</div>
          <p className="truncate text-[12.5px] text-faint">{host}{s.header_names.length ? ` · ${s.header_names.join(", ")} set` : ""}</p>
          {s.last_error ? <p className="mt-1.5 text-[13px] text-danger">{s.last_error}</p>
            : <p className="mt-1.5 text-[13px] text-muted">{s.tool_count} tool{s.tool_count === 1 ? "" : "s"}{s.tools_checked_at ? ` · checked ${relativeTime(s.tools_checked_at)}` : ""}</p>}
        </div>
        <Toggle label={`Use ${s.name}`} checked={enabled} disabled={busy} onChange={(v) => { setEnabled(v); void act(() => api.mcpPatch(s.name, { enabled: v })); }} />
      </div>
      <div className="flex flex-wrap items-center gap-1.5 border-t border-line bg-paper/60 px-3 py-2">
        <button type="button" className="btn-ghost" disabled={busy} onClick={() => act(() => api.mcpTest(s.name))}><Icon name="refresh" size={13} />Test and refresh</button>
        {s.tool_count > 0 && <button type="button" className="btn-ghost" onClick={() => setOpen((o) => !o)} aria-expanded={open}><Icon name="tool" size={13} />Tools</button>}
        <label className="ml-2 flex items-center gap-2 text-[12.5px] text-muted"><Toggle label={`Ask before every ${s.name} tool`} checked={asks} disabled={busy} onChange={(v) => { setAsks(v); void act(() => api.mcpPatch(s.name, { requires_approval: v })); }} />Ask before each tool call</label>
        <button type="button" className="btn-ghost ml-auto text-danger" onClick={() => { if (window.confirm(`Remove ${s.name}?`)) void act(() => api.mcpDelete(s.name)); }}><Icon name="trash" size={13} />Remove</button>
      </div>
      {open && (
        <ul className="divide-y divide-line border-t border-line">
          {s.tools.map((t) => (
            <li key={t.name} className="flex items-start gap-3 px-4 py-2.5">
              <div className="min-w-0 flex-1"><code className="font-mono text-[12.5px] font-medium">{t.name}</code>{t.description && <p className="mt-0.5 text-[12.5px] text-muted">{t.description}</p>}</div>
              {asks && (
                <label className="flex shrink-0 items-center gap-2 text-[12.5px] text-muted">
                  <input type="checkbox" className="accent-[var(--accent)]" checked={!free.has(t.name)} disabled={busy}
                    onChange={(e) => { const next = new Set(free); if (e.target.checked) next.delete(t.name); else next.add(t.name); setFree(next); void act(() => api.mcpPatch(s.name, { no_approval_tools: [...next] })); }} aria-label={`Ask before ${t.name}`} />Ask first
                </label>
              )}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function ConnectorsPage() {
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => api.mcpServers().then((r) => setServers(r.servers)).catch((e) => setError(messageOf(e))), []);
  useEffect(() => { void load(); }, [load]);
  return (
    <PageShell title="Connectors" subtitle="Bring your own tools. Connect an MCP server and privai can use its tools in any conversation, asking you first unless you say otherwise.">
      <RuleNote />
      <ImportBox onDone={() => void load()} />
      <ErrorNote message={error} onDismiss={() => setError("")} />
      {servers === null && !error && <p className="text-[13px] text-faint" role="status">Loading…</p>}
      {servers?.length === 0 && <Empty title="No servers connected">Paste a configuration above to connect your first one.</Empty>}
      <ul className="space-y-3">{servers?.map((s) => <ServerCard key={s.name} s={s} onChange={() => void load()} onError={setError} />)}</ul>
    </PageShell>
  );
}
