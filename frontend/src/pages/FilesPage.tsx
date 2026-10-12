import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Artifact } from "../api/types";
import { Empty, ErrorNote, PageShell } from "../components/ui/PageShell";
import { Icon } from "../components/ui/Icon";
import { formatBytes, relativeTime } from "../lib/format";

export function FilesPage() {
  const [files, setFiles] = useState<Artifact[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { api.artifacts().then(setFiles).catch((e) => setError(e.message)); }, []);
  return (
    <PageShell title="Files" subtitle="Everything privai has saved for you, from every conversation. Files stay here after the workspace they were made in is gone.">
      <ErrorNote message={error} />
      {files === null && !error && <p className="text-[13px] text-faint" role="status">Loading…</p>}
      {files?.length === 0 && <Empty title="No files yet">When privai writes a report or a script, it appears here and in the conversation.</Empty>}
      {files && files.length > 0 && (
        <ul className="card divide-y divide-line">
          {files.map((f) => (
            <li key={f.id} className="flex items-center gap-3 px-4 py-3">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-accentSoft text-accentInk"><Icon name="file" size={17} /></span>
              <div className="min-w-0 flex-1"><div className="truncate text-[14px] font-medium">{f.name}</div><div className="truncate text-[12.5px] text-faint">{f.path} · {formatBytes(f.size)} · {relativeTime(f.created_at)}</div></div>
              <Link to={`/chat/${f.session_id}`} className="btn-ghost">Conversation</Link>
              <a href={api.artifactUrl(f.id)} download className="btn-outline"><Icon name="download" size={14} />Download</a>
            </li>
          ))}
        </ul>
      )}
    </PageShell>
  );
}
