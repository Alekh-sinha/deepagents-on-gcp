"""Turn a raw pending tool call into something a person can review: a kind, a title and a compact preview.

``kind`` drives which card the UI shows: shell | email | github | plan | question | interrupt | mcp | other.
Previews never contain more than a reviewer needs (long text is clipped) and never include secrets (tools do not hold any in their args).
"""
from __future__ import annotations

from typing import Any

_CLIP = 4000


def _clip(text: Any, n: int = _CLIP) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= n else s[:n] + f"\n[... {len(s) - n} more characters]"


def describe(action: dict) -> dict:
    """action = one entry of the interrupt's action_requests (name, args, description) plus optional allowed_decisions."""
    name, a = action.get("name", ""), dict(action.get("args") or {})
    allowed = list(action.get("allowed_decisions") or ["approve", "edit", "reject"])
    base = {"tool": name, "allowed_decisions": allowed}
    if name == "execute":
        return {**base, "kind": "shell", "title": "Run a shell command", "preview": {"command": _clip(a.get("command"))}}
    if name == "send_email":
        return {**base, "kind": "email", "title": f"Send email: {_clip(a.get('subject'), 80)}",
                "preview": {"to": a.get("to"), "cc": a.get("cc") or [], "subject": a.get("subject"), "body": _clip(a.get("body")),
                            "attachments": a.get("attachments") or []}}
    if name.startswith("github_"):
        files = [{"path": f.get("path"), "source": f.get("from_sandbox") or "inline",
                  "chars": len(f["content"]) if isinstance(f.get("content"), str) else None,
                  "content": _clip(f.get("content"), 1500)} for f in (a.get("files") or []) if isinstance(f, dict)]
        titles = {"github_create_branch": "Create a branch", "github_commit_files": "Commit files", "github_open_pr": "Open a pull request"}
        return {**base, "kind": "github", "title": f"{titles.get(name, name)} in {a.get('repo')}",
                "preview": {"repo": a.get("repo"), "branch": a.get("branch") or a.get("head"), "base": a.get("base") or a.get("from_branch"),
                            "message": a.get("message") or a.get("title"), "body": _clip(a.get("body")), "files": files}}
    if name == "submit_plan":
        return {**base, "kind": "plan", "title": "Approve the plan",
                "preview": {"summary": a.get("summary"), "tasks": [t for t in (a.get("tasks") or []) if isinstance(t, dict)]}}
    if name == "ask_user":
        return {**base, "kind": "question", "title": "The assistant has a question",
                "preview": {"question": _clip(a.get("question"), 1000), "options": a.get("options") or []}}
    if name.startswith("mcp__"):  # a tool of a server the user connected: mcp__<server>__<tool>
        _, _, rest = name.partition("mcp__")
        server, _, tool = rest.partition("__")
        return {**base, "kind": "mcp", "title": f"Use {tool or name} on {server or 'an MCP server'}",
                "preview": {"server": server, "tool": tool, "args": {k: _clip(v, 1500) for k, v in a.items()}}}
    if name == "interrupt":  # a graph paused with its own interrupt(value): show the value, take a free-text answer
        return {**base, "kind": "interrupt", "title": "The agent is waiting for your input", "preview": {"value": a.get("value")}}
    return {**base, "kind": "other", "title": f"Approve {name}", "preview": {"args": {k: _clip(v, 500) for k, v in a.items()}}}


def describe_all(action_requests: list[dict]) -> list[dict]:
    return [describe(a) for a in action_requests]
