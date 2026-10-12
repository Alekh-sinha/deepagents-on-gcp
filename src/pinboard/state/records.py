"""The records the state layer stores (runs, approvals, sessions, spare sandboxes, artifacts) and their ids."""
from __future__ import annotations

import secrets
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


RUN_STATUSES = ("running", "awaiting_approval", "completed", "failed", "blocked", "expired", "interrupted", "cancelled")


ACTIVE_RUN_STATUSES = ("running", "awaiting_approval")


_UPDATABLE = {"status", "output", "error", "sandbox_name", "outputs", "prompt", "model", "resume_count"}  # never user_id/thread_id/source/lease/event_seq


_SESSION_UPDATABLE = {"title", "model", "sandbox_name", "sandbox_expires_at", "last_active_at", "archived", "had_sandbox", "auto_approve"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


def new_approval_id() -> str:
    return secrets.token_urlsafe(24)  # unguessable: it will appear in approval links later


@dataclass
class RunRecord:
    id: str
    thread_id: str
    user_id: str
    status: str
    source: str = "user"
    prompt: str = ""
    output: str = ""
    error: str = ""
    sandbox_name: str | None = None
    outputs: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    # ── turn bookkeeping (a "run" is one turn of a session) ──
    session_id: str | None = None
    model: str | None = None
    heartbeat_at: datetime | None = None
    lease_id: str | None = None            # fencing token of the worker that owns this turn right now
    lease_expires_at: datetime | None = None
    resume_count: int = 0                  # automatic resumes used (policy limits.max_auto_resumes)
    cancel_requested: bool = False
    event_seq: int = 0                     # sequence number of the last event written for this turn

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"], d["updated_at"] = self.created_at.isoformat(), self.updated_at.isoformat()
        d["heartbeat_at"] = self.heartbeat_at.isoformat() if self.heartbeat_at else None
        for internal in ("sandbox_name", "lease_id", "lease_expires_at"):  # internal details, not for API clients
            d.pop(internal)
        return d


@dataclass
class SessionRecord:
    """A conversation. ``thread_id`` is the LangGraph thread; the sandbox belongs to the session, not to a turn."""

    id: str
    user_id: str
    thread_id: str
    title: str = ""
    model: str | None = None
    sandbox_name: str | None = None
    sandbox_expires_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    last_active_at: datetime = field(default_factory=utcnow)
    archived: bool = False
    had_sandbox: bool = False  # a sandbox was created for this session at some point (never cleared: the stored name is, by the idle sweep)
    schedule_id: str = ""  # set when a schedule opened this session (the UI marks it as scheduled)
    auto_approve: dict | None = None  # {"kinds": [...], "max_per_turn": n} while the user has switched approvals to automatic; None = ask every time (see engine/auto_approve.py)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("created_at", "last_active_at", "sandbox_expires_at"):
            d[k] = d[k].isoformat() if d[k] else None
        d.pop("sandbox_name")
        d.pop("had_sandbox")
        return d


def new_session_id() -> str:
    return "s_" + uuid.uuid4().hex[:14]


@dataclass
class SpareRecord:
    """A pre-created, never-used sandbox waiting for the next session that needs one. At most ONE is active (creating or ready) at a time.
    Claimed rows stay for a while: they are how the daily creation limit is counted."""

    id: str
    status: str  # creating | ready | claimed
    created_at: datetime
    name: str | None = None
    ready_at: datetime | None = None
    expires_at: datetime | None = None  # when the platform deletes the sandbox (its own TTL)
    claimed_by: str | None = None
    claimed_at: datetime | None = None


@dataclass
class ArtifactRecord:
    """A file the agent produced, kept for the USER (not the session or the sandbox) so it can be found days later."""

    id: str
    user_id: str
    session_id: str
    run_id: str
    name: str             # file name, e.g. report.md
    path: str             # path under the output folder, e.g. reports/report.md
    size: int
    content_type: str
    sha256: str
    blob_key: str         # where the bytes live in the blob store
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d.pop("blob_key")  # internal location, not for clients
        return d


def new_artifact_id() -> str:
    return "a_" + uuid.uuid4().hex[:14]


@dataclass
class McpServerRecord:
    """A remote MCP server a user brought along (a deployed server reached over HTTP). The header values (tokens) are stored encrypted and never leave the
    engine: clients see only the header NAMES. ``tools`` is the list the server offered the last time it was tested, so the agent can be built without
    connecting (a server that is down must not break a turn)."""

    id: str
    user_id: str
    name: str                                   # unique per user; used in tool names (mcp__<name>__<tool>)
    url: str
    transport: str = "http"                     # http (streamable HTTP) | sse
    headers_enc: str = ""                       # encrypted JSON {header: value}
    header_names: list[str] = field(default_factory=list)
    enabled: bool = True
    requires_approval: bool = True              # every tool call of this server waits for a person ...
    no_approval_tools: list[str] = field(default_factory=list)   # ... except these (the user marked them safe)
    tools: list[dict] = field(default_factory=list)              # [{name, description, input_schema}]
    tools_checked_at: datetime | None = None
    last_error: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("headers_enc")
        for k in ("tools_checked_at", "created_at", "updated_at"):
            d[k] = d[k].isoformat() if d[k] else None
        d["tool_count"] = len(self.tools)
        d["tools"] = [{"name": t.get("name"), "description": t.get("description", ""), "needs_approval": self.requires_approval and t.get("name") not in self.no_approval_tools}
                      for t in self.tools]
        return d


def new_mcp_id() -> str:
    return "m_" + uuid.uuid4().hex[:14]


@dataclass
class ScheduleRecord:
    """A saved instruction that runs on a cron schedule, with nobody watching. Each firing opens its own session."""

    id: str
    user_id: str
    title: str
    instructions: str
    cron: str                                   # five-field cron, evaluated in ``timezone``
    timezone: str = "UTC"
    model: str | None = None
    auto_approve: dict | None = None            # the same setting a session has; None = anything that needs approval waits for the user
    enabled: bool = True
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str = ""                        # started | skipped | failed
    last_session_id: str = ""
    run_count: int = 0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("next_run_at", "last_run_at", "created_at", "updated_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclass
class ScheduleRunRecord:
    id: str
    schedule_id: str
    user_id: str
    trigger: str                                # schedule | manual | catchup
    status: str                                 # started | skipped | failed
    session_id: str = ""
    turn_id: str = ""
    note: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


def new_schedule_id() -> str:
    return "sc_" + uuid.uuid4().hex[:12]


def new_schedule_run_id() -> str:
    return "sr_" + uuid.uuid4().hex[:14]


_SCHEDULE_UPDATABLE = {"title", "instructions", "cron", "timezone", "model", "auto_approve", "enabled", "next_run_at", "last_run_at", "last_status",
                       "last_session_id", "run_count"}
_MCP_UPDATABLE = {"url", "transport", "headers_enc", "header_names", "enabled", "requires_approval", "no_approval_tools", "tools", "tools_checked_at", "last_error"}


@dataclass
class ApprovalRecord:
    id: str
    run_id: str
    user_id: str
    action_requests: list[dict]
    expires_at: datetime
    status: str = "pending"  # pending | approved | rejected | expired
    decision: dict | None = None
    decided_by: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    decided_at: datetime | None = None
    previews: list[dict] = field(default_factory=list)  # one per action request: kind, title, preview, allowed_decisions (see approvals.py)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("expires_at", "created_at", "decided_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d
