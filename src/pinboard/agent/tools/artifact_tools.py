"""Tools to find the user's earlier files (artifacts) and bring one back into the sandbox.

Both are scoped to ONE user (the one who owns the conversation): another user's files are never visible. Restoring only writes inside
the sandbox's working folder and never into the read-only skills folder.
"""
from __future__ import annotations

import posixpath

from langchain_core.tools import tool

from pinboard.gcp.artifacts import Artifacts
from pinboard.core.guardrails import wrap_untrusted

RESTORE_LIMIT_BYTES = 5 * 1024 * 1024


def make_artifact_tools(artifacts: Artifacts, user_id: str, sandbox, *, session_id: str | None, cwd: str, skills_path: str):
    cwd = cwd.rstrip("/")

    def _target(name: str, to_path: str | None) -> str:
        path = posixpath.normpath(to_path if to_path else f"{cwd}/restored/{name}")
        inside = path.startswith(cwd + "/") and ".." not in path.split("/")
        in_skills = path == skills_path or path.startswith(skills_path.rstrip("/") + "/")
        if not inside or in_skills:
            raise ValueError(f"to_path must be a file path inside {cwd}/ (not the skills folder)")
        return path

    @tool
    def list_artifacts(this_conversation_only: bool = False, limit: int = 20) -> str:
        """List the files you (or earlier conversations) saved for the user: id, name, size, when. Newest first. Use restore_artifact to bring one back."""
        items = artifacts.list(user_id, session_id if this_conversation_only else None, max(1, min(int(limit), 50)))
        if not items:
            return "No saved files yet."
        lines = [f"{a.id} | {a.path} | {a.size} bytes | {a.created_at:%Y-%m-%d %H:%M} UTC" + (" | this conversation" if a.session_id == session_id else "") for a in items]
        return wrap_untrusted("\n".join(lines), "saved files")

    @tool
    def restore_artifact(artifact_id: str, to_path: str | None = None) -> str:
        """Copy one of the user's saved files into your sandbox so you can read or change it. Default location: <working folder>/restored/<name>.
        Files you save again under the output folder are stored as a new version automatically."""
        try:
            rec, data = artifacts.read(user_id, artifact_id, RESTORE_LIMIT_BYTES)
            path = _target(rec.name, to_path)
        except LookupError:
            return "No such saved file (use list_artifacts to see the ids)."
        except ValueError as exc:
            return f"Cannot restore: {exc}"
        (res,) = sandbox.upload_files([(path, data)])
        if res.error:
            return f"Cannot restore: {res.error}"
        return f"Restored {rec.path} ({len(data)} bytes) to {path}."

    return [list_artifacts, restore_artifact]
