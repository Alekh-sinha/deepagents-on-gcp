"""PostgresStore and PostgresLedger: durable state in Postgres (production), with the table definitions."""
from __future__ import annotations

from datetime import datetime, timedelta
from pinboard.state.records import (ACTIVE_RUN_STATUSES, ApprovalRecord, ArtifactRecord, McpServerRecord, RunRecord, ScheduleRecord, ScheduleRunRecord, SessionRecord,
                                    SpareRecord, _MCP_UPDATABLE, _SCHEDULE_UPDATABLE, _SESSION_UPDATABLE, _UPDATABLE, utcnow)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pb_runs (
    id            text PRIMARY KEY,
    thread_id     text NOT NULL,
    user_id       text NOT NULL,
    status        text NOT NULL,
    source        text NOT NULL DEFAULT 'user',
    prompt        text NOT NULL DEFAULT '',
    output        text NOT NULL DEFAULT '',
    error         text NOT NULL DEFAULT '',
    sandbox_name  text,
    outputs       jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS pb_runs_thread ON pb_runs (thread_id, status);
CREATE TABLE IF NOT EXISTS pb_approvals (
    id              text PRIMARY KEY,
    run_id          text NOT NULL REFERENCES pb_runs(id),
    user_id         text NOT NULL,
    status          text NOT NULL,
    action_requests jsonb NOT NULL,
    decision        jsonb,
    decided_by      text,
    expires_at      timestamptz NOT NULL,
    created_at      timestamptz NOT NULL,
    decided_at      timestamptz
);
ALTER TABLE pb_approvals ADD COLUMN IF NOT EXISTS previews jsonb NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS pb_approvals_user_status ON pb_approvals (user_id, status, created_at DESC);
CREATE TABLE IF NOT EXISTS pb_audit (
    id       bigserial PRIMARY KEY,
    ts       timestamptz NOT NULL,
    user_id  text NOT NULL,
    run_id   text,
    event    text NOT NULL,
    detail   jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS pb_audit_run ON pb_audit (run_id, id);
CREATE TABLE IF NOT EXISTS pb_sessions (
    id                 text PRIMARY KEY,
    user_id            text NOT NULL,
    thread_id          text NOT NULL,
    title              text NOT NULL DEFAULT '',
    model              text,
    sandbox_name       text,
    sandbox_expires_at timestamptz,
    created_at         timestamptz NOT NULL,
    last_active_at     timestamptz NOT NULL,
    archived           boolean NOT NULL DEFAULT false
);
ALTER TABLE pb_sessions ADD COLUMN IF NOT EXISTS had_sandbox boolean NOT NULL DEFAULT false;
ALTER TABLE pb_sessions ADD COLUMN IF NOT EXISTS auto_approve jsonb;
ALTER TABLE pb_sessions ADD COLUMN IF NOT EXISTS schedule_id text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS pb_sessions_user ON pb_sessions (user_id, last_active_at DESC);
ALTER TABLE pb_runs ADD COLUMN IF NOT EXISTS session_id text;
ALTER TABLE pb_runs ADD COLUMN IF NOT EXISTS model text;
ALTER TABLE pb_runs ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;
ALTER TABLE pb_runs ADD COLUMN IF NOT EXISTS lease_id text;
ALTER TABLE pb_runs ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE pb_runs ADD COLUMN IF NOT EXISTS resume_count integer NOT NULL DEFAULT 0;
ALTER TABLE pb_runs ADD COLUMN IF NOT EXISTS cancel_requested boolean NOT NULL DEFAULT false;
ALTER TABLE pb_runs ADD COLUMN IF NOT EXISTS event_seq integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS pb_runs_session ON pb_runs (session_id, created_at DESC);
CREATE TABLE IF NOT EXISTS pb_events (
    run_id  text NOT NULL REFERENCES pb_runs(id),
    seq     integer NOT NULL,
    type    text NOT NULL,
    payload jsonb NOT NULL,
    ts      timestamptz NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS pb_effects (
    key        text PRIMARY KEY,
    kind       text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS pb_effects_kind ON pb_effects (kind, created_at);
CREATE TABLE IF NOT EXISTS pb_artifacts (
    id           text PRIMARY KEY,
    user_id      text NOT NULL,
    session_id   text NOT NULL,
    run_id       text NOT NULL,
    name         text NOT NULL,
    path         text NOT NULL,
    size         bigint NOT NULL,
    content_type text NOT NULL,
    sha256       text NOT NULL,
    blob_key     text NOT NULL,
    created_at   timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS pb_artifacts_user ON pb_artifacts (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS pb_artifacts_path ON pb_artifacts (user_id, session_id, path, created_at DESC);
CREATE TABLE IF NOT EXISTS pb_mcp_servers (
    id                text PRIMARY KEY,
    user_id           text NOT NULL,
    name              text NOT NULL,
    url               text NOT NULL,
    transport         text NOT NULL DEFAULT 'http',
    headers_enc       text NOT NULL DEFAULT '',
    header_names      jsonb NOT NULL DEFAULT '[]'::jsonb,
    enabled           boolean NOT NULL DEFAULT true,
    requires_approval boolean NOT NULL DEFAULT true,
    no_approval_tools jsonb NOT NULL DEFAULT '[]'::jsonb,
    tools             jsonb NOT NULL DEFAULT '[]'::jsonb,
    tools_checked_at  timestamptz,
    last_error        text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    UNIQUE (user_id, name)
);
CREATE TABLE IF NOT EXISTS pb_schedules (
    id              text PRIMARY KEY,
    user_id         text NOT NULL,
    title           text NOT NULL,
    instructions    text NOT NULL,
    cron            text NOT NULL,
    timezone        text NOT NULL DEFAULT 'UTC',
    model           text,
    auto_approve    jsonb,
    enabled         boolean NOT NULL DEFAULT true,
    next_run_at     timestamptz,
    last_run_at     timestamptz,
    last_status     text NOT NULL DEFAULT '',
    last_session_id text NOT NULL DEFAULT '',
    run_count       integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS pb_schedules_user ON pb_schedules (user_id, created_at);
CREATE INDEX IF NOT EXISTS pb_schedules_due ON pb_schedules (next_run_at) WHERE enabled;
CREATE TABLE IF NOT EXISTS pb_schedule_runs (
    id          text PRIMARY KEY,
    schedule_id text NOT NULL,
    user_id     text NOT NULL,
    trigger     text NOT NULL,
    status      text NOT NULL,
    session_id  text NOT NULL DEFAULT '',
    turn_id     text NOT NULL DEFAULT '',
    note        text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS pb_schedule_runs_sched ON pb_schedule_runs (schedule_id, created_at DESC);
CREATE INDEX IF NOT EXISTS pb_schedule_runs_user ON pb_schedule_runs (user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS pb_spares (
    id         text PRIMARY KEY,
    status     text NOT NULL,
    created_at timestamptz NOT NULL,
    name       text,
    ready_at   timestamptz,
    expires_at timestamptz,
    claimed_by text,
    claimed_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS pb_spares_one_active ON pb_spares ((true)) WHERE status IN ('creating', 'ready');
"""


_RUN_COLS = ("id", "thread_id", "user_id", "status", "source", "prompt", "output", "error", "sandbox_name", "outputs", "created_at", "updated_at",
             "session_id", "model", "heartbeat_at", "lease_id", "lease_expires_at", "resume_count", "cancel_requested", "event_seq")


_SESSION_COLS = ("id", "user_id", "thread_id", "title", "model", "sandbox_name", "sandbox_expires_at", "created_at", "last_active_at", "archived", "had_sandbox", "schedule_id", "auto_approve")


def make_pool(dsn: str, max_size: int = 5):
    """Connection pool in the shape both PostgresStore and langgraph's PostgresSaver need."""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    return ConnectionPool(dsn, min_size=1, max_size=max_size, kwargs={"autocommit": True, "row_factory": dict_row}, open=True)


def _run_from_row(r: dict) -> RunRecord:
    return RunRecord(**{k: r[k] for k in _RUN_COLS})


def _session_from_row(r: dict) -> SessionRecord:
    return SessionRecord(**{k: r[k] for k in _SESSION_COLS})


_ARTIFACT_COLS = ("id", "user_id", "session_id", "run_id", "name", "path", "size", "content_type", "sha256", "blob_key", "created_at")


_MCP_COLS = ("id", "user_id", "name", "url", "transport", "headers_enc", "header_names", "enabled", "requires_approval", "no_approval_tools", "tools", "tools_checked_at", "last_error",
             "created_at", "updated_at")
_SCHEDULE_COLS = ("id", "user_id", "title", "instructions", "cron", "timezone", "model", "auto_approve", "enabled", "next_run_at", "last_run_at", "last_status", "last_session_id",
                  "run_count", "created_at", "updated_at")
_SCHEDULE_RUN_COLS = ("id", "schedule_id", "user_id", "trigger", "status", "session_id", "turn_id", "note", "created_at")


def _mcp_from_row(r: dict) -> McpServerRecord:
    return McpServerRecord(**{k: r[k] for k in _MCP_COLS})


def _schedule_from_row(r: dict) -> ScheduleRecord:
    return ScheduleRecord(**{k: r[k] for k in _SCHEDULE_COLS})


def _artifact_from_row(r: dict) -> ArtifactRecord:
    return ArtifactRecord(**{k: r[k] for k in _ARTIFACT_COLS})


_SPARE_COLS = ("id", "status", "created_at", "name", "ready_at", "expires_at", "claimed_by", "claimed_at")


def _spare_from_row(r: dict) -> SpareRecord:
    return SpareRecord(**{k: r[k] for k in _SPARE_COLS})


def _approval_from_row(r: dict) -> ApprovalRecord:
    return ApprovalRecord(
        id=r["id"], run_id=r["run_id"], user_id=r["user_id"], status=r["status"],
        action_requests=r["action_requests"], decision=r["decision"], decided_by=r["decided_by"],
        expires_at=r["expires_at"], created_at=r["created_at"], decided_at=r["decided_at"], previews=r.get("previews") or [],
    )


class PostgresStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    def setup(self) -> None:
        with self.pool.connection() as conn:
            conn.execute(_SCHEMA)

    def _q(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.pool.connection() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def create_run(self, run):
        from psycopg.types.json import Jsonb

        self._q(
            "INSERT INTO pb_runs (id, thread_id, user_id, status, source, prompt, output, error, sandbox_name, outputs, created_at, updated_at, session_id, model) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (run.id, run.thread_id, run.user_id, run.status, run.source, run.prompt, run.output, run.error,
             run.sandbox_name, Jsonb(run.outputs), run.created_at, run.updated_at, run.session_id, run.model),
        )

    def get_run(self, run_id):
        rows = self._q("SELECT * FROM pb_runs WHERE id=%s", (run_id,))
        return _run_from_row(rows[0]) if rows else None

    def update_run(self, run_id, **fields):
        from psycopg.types.json import Jsonb

        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        if "outputs" in fields:
            fields["outputs"] = Jsonb(fields["outputs"])
        sets = ", ".join(f"{k}=%s" for k in fields) + (", " if fields else "") + "updated_at=%s"
        rows = self._q(f"UPDATE pb_runs SET {sets} WHERE id=%s RETURNING *", (*fields.values(), utcnow(), run_id))
        if not rows:
            raise KeyError(run_id)
        return _run_from_row(rows[0])

    def claim_run(self, run_id, from_status, to_status, now):
        rows = self._q("UPDATE pb_runs SET status=%s, updated_at=%s WHERE id=%s AND status=%s RETURNING id", (to_status, now, run_id, from_status))
        return bool(rows)

    def active_run_for_thread(self, thread_id):
        rows = self._q("SELECT * FROM pb_runs WHERE thread_id=%s AND status = ANY(%s) LIMIT 1", (thread_id, list(ACTIVE_RUN_STATUSES)))
        return _run_from_row(rows[0]) if rows else None

    def create_approval(self, a):
        from psycopg.types.json import Jsonb

        self._q(
            "INSERT INTO pb_approvals (id, run_id, user_id, status, action_requests, decision, decided_by, expires_at, created_at, decided_at, previews) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (a.id, a.run_id, a.user_id, a.status, Jsonb(a.action_requests), Jsonb(a.decision) if a.decision else None,
             a.decided_by, a.expires_at, a.created_at, a.decided_at, Jsonb(a.previews)),
        )

    def get_approval(self, approval_id):
        rows = self._q("SELECT * FROM pb_approvals WHERE id=%s", (approval_id,))
        return _approval_from_row(rows[0]) if rows else None

    def list_approvals(self, user_id, status=None, limit=50):
        if status:
            rows = self._q("SELECT * FROM pb_approvals WHERE user_id=%s AND status=%s ORDER BY created_at DESC LIMIT %s", (user_id, status, limit))
        else:
            rows = self._q("SELECT * FROM pb_approvals WHERE user_id=%s ORDER BY created_at DESC LIMIT %s", (user_id, limit))
        return [_approval_from_row(r) for r in rows]

    def decide_approval(self, approval_id, status, decision, decided_by, now):
        from psycopg.types.json import Jsonb

        rows = self._q(
            "UPDATE pb_approvals SET status=%s, decision=%s, decided_by=%s, decided_at=%s "
            "WHERE id=%s AND status='pending' AND expires_at > %s RETURNING *",
            (status, Jsonb(decision), decided_by, now, approval_id, now),
        )
        return _approval_from_row(rows[0]) if rows else None

    def expire_approvals(self, now):
        rows = self._q("UPDATE pb_approvals SET status='expired', decided_at=%s WHERE status='pending' AND expires_at <= %s RETURNING *", (now, now))
        return [_approval_from_row(r) for r in rows]

    def add_audit(self, user_id, run_id, event, detail, now=None):
        from psycopg.types.json import Jsonb

        self._q("INSERT INTO pb_audit (ts, user_id, run_id, event, detail) VALUES (%s,%s,%s,%s,%s)", (now or utcnow(), user_id, run_id, event, Jsonb(detail)))

    def list_audit(self, run_id=None, limit=200):
        if run_id:
            rows = self._q("SELECT ts, user_id, run_id, event, detail FROM pb_audit WHERE run_id=%s ORDER BY id DESC LIMIT %s", (run_id, limit))
        else:
            rows = self._q("SELECT ts, user_id, run_id, event, detail FROM pb_audit ORDER BY id DESC LIMIT %s", (limit,))
        return [{**r, "ts": r["ts"].isoformat()} for r in reversed(rows)]

    # ── sessions ──
    def create_session(self, x):
        from psycopg.types.json import Jsonb

        self._q("INSERT INTO pb_sessions (id, user_id, thread_id, title, model, sandbox_name, sandbox_expires_at, created_at, last_active_at, archived, had_sandbox, schedule_id, auto_approve) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (x.id, x.user_id, x.thread_id, x.title, x.model, x.sandbox_name, x.sandbox_expires_at, x.created_at, x.last_active_at, x.archived, x.had_sandbox, x.schedule_id,
                 Jsonb(x.auto_approve) if x.auto_approve else None))

    def get_session(self, session_id):
        rows = self._q("SELECT * FROM pb_sessions WHERE id=%s", (session_id,))
        return _session_from_row(rows[0]) if rows else None

    def update_session(self, session_id, **fields):
        bad = set(fields) - _SESSION_UPDATABLE
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        if not fields:
            return self.get_session(session_id)
        if "auto_approve" in fields:
            from psycopg.types.json import Jsonb

            fields["auto_approve"] = Jsonb(fields["auto_approve"]) if fields["auto_approve"] else None
        rows = self._q(f"UPDATE pb_sessions SET {', '.join(f'{k}=%s' for k in fields)} WHERE id=%s RETURNING *", (*fields.values(), session_id))
        if not rows:
            raise KeyError(session_id)
        return _session_from_row(rows[0])

    def list_sessions(self, user_id, limit=50, include_archived=False):
        rows = self._q("SELECT * FROM pb_sessions WHERE user_id=%s AND (%s OR NOT archived) ORDER BY last_active_at DESC LIMIT %s", (user_id, include_archived, limit))
        return [_session_from_row(r) for r in rows]

    def idle_sessions_with_sandbox(self, before):
        rows = self._q("SELECT s.* FROM pb_sessions s WHERE s.sandbox_name IS NOT NULL AND s.last_active_at <= %s AND NOT EXISTS "
                       "(SELECT 1 FROM pb_runs r WHERE r.session_id = s.id AND r.status = ANY(%s))", (before, list(ACTIVE_RUN_STATUSES)))
        return [_session_from_row(r) for r in rows]

    # ── leases ──
    def acquire_lease(self, run_id, lease_id, now, ttl_seconds):
        rows = self._q("UPDATE pb_runs SET lease_id=%s, lease_expires_at=%s, heartbeat_at=%s, updated_at=%s "
                       "WHERE id=%s AND status='running' AND (lease_id IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= %s) RETURNING id",
                       (lease_id, now + timedelta(seconds=ttl_seconds), now, now, run_id, now))
        return bool(rows)

    def renew_lease(self, run_id, lease_id, now, ttl_seconds):
        rows = self._q("UPDATE pb_runs SET lease_expires_at=%s, heartbeat_at=%s WHERE id=%s AND lease_id=%s RETURNING id",
                       (now + timedelta(seconds=ttl_seconds), now, run_id, lease_id))
        return bool(rows)

    def release_lease(self, run_id, lease_id):
        rows = self._q("UPDATE pb_runs SET lease_id=NULL, lease_expires_at=NULL WHERE id=%s AND lease_id=%s RETURNING id", (run_id, lease_id))
        return bool(rows)

    def stale_runs(self, now, grace_seconds=120):
        rows = self._q("SELECT * FROM pb_runs WHERE status='running' AND ((lease_expires_at IS NOT NULL AND lease_expires_at <= %s) "
                       "OR (lease_expires_at IS NULL AND updated_at <= %s))", (now, now - timedelta(seconds=grace_seconds)))
        return [_run_from_row(r) for r in rows]

    def mark_interrupted(self, run_id, now, grace_seconds=120):
        rows = self._q("UPDATE pb_runs SET status='interrupted', lease_id=NULL, lease_expires_at=NULL, updated_at=%s WHERE id=%s AND status='running' "
                       "AND ((lease_expires_at IS NOT NULL AND lease_expires_at <= %s) OR (lease_expires_at IS NULL AND updated_at <= %s)) RETURNING id",
                       (now, run_id, now, now - timedelta(seconds=grace_seconds)))
        return bool(rows)

    def request_cancel(self, run_id):
        rows = self._q("UPDATE pb_runs SET cancel_requested=true, updated_at=%s WHERE id=%s RETURNING *", (utcnow(), run_id))
        if not rows:
            raise KeyError(run_id)
        return _run_from_row(rows[0])

    # ── events ──
    def append_event(self, run_id, lease_id, type, payload, now=None):
        from psycopg.types.json import Jsonb

        from pinboard.engine.events import cap_payload

        payload = cap_payload(payload)
        with self.pool.connection() as conn:
            with conn.transaction():  # the row lock on pb_runs serialises writers, so seq is gapless and ordered
                if lease_id is None:
                    cur = conn.execute("UPDATE pb_runs SET event_seq = event_seq + 1 WHERE id=%s RETURNING event_seq", (run_id,))
                else:
                    cur = conn.execute("UPDATE pb_runs SET event_seq = event_seq + 1 WHERE id=%s AND lease_id=%s RETURNING event_seq", (run_id, lease_id))
                row = cur.fetchone()
                if row is None:
                    return None  # fenced: this writer no longer owns the turn
                conn.execute("INSERT INTO pb_events (run_id, seq, type, payload, ts) VALUES (%s,%s,%s,%s,%s)",
                             (run_id, row["event_seq"], type, Jsonb(payload), now or utcnow()))
                return row["event_seq"]

    def list_events(self, run_id, after_seq=0, limit=500):
        rows = self._q("SELECT seq, type, payload, ts FROM pb_events WHERE run_id=%s AND seq > %s ORDER BY seq LIMIT %s", (run_id, after_seq, limit))
        return [{**r, "ts": r["ts"].isoformat()} for r in rows]

    def last_event_of_type(self, run_id, type):
        rows = self._q("SELECT seq, type, payload, ts FROM pb_events WHERE run_id=%s AND type=%s ORDER BY seq DESC LIMIT 1", (run_id, type))
        return {**rows[0], "ts": rows[0]["ts"].isoformat()} if rows else None

    def list_session_runs(self, session_id, limit=50):
        rows = self._q("SELECT * FROM (SELECT * FROM pb_runs WHERE session_id=%s ORDER BY created_at DESC LIMIT %s) t ORDER BY created_at", (session_id, limit))
        return [_run_from_row(r) for r in rows]

    def create_artifact(self, a):
        self._q("INSERT INTO pb_artifacts (id, user_id, session_id, run_id, name, path, size, content_type, sha256, blob_key, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (a.id, a.user_id, a.session_id, a.run_id, a.name, a.path, a.size, a.content_type, a.sha256, a.blob_key, a.created_at))

    def get_artifact(self, artifact_id):
        rows = self._q("SELECT * FROM pb_artifacts WHERE id=%s", (artifact_id,))
        return _artifact_from_row(rows[0]) if rows else None

    def list_artifacts(self, user_id, session_id=None, limit=50):
        if session_id is None:
            rows = self._q("SELECT * FROM pb_artifacts WHERE user_id=%s ORDER BY created_at DESC, id DESC LIMIT %s", (user_id, limit))
        else:
            rows = self._q("SELECT * FROM pb_artifacts WHERE user_id=%s AND session_id=%s ORDER BY created_at DESC, id DESC LIMIT %s", (user_id, session_id, limit))
        return [_artifact_from_row(r) for r in rows]

    # ── bring-your-own MCP servers ──
    def put_mcp_server(self, s):
        from psycopg.types.json import Jsonb

        rows = self._q("INSERT INTO pb_mcp_servers (id, user_id, name, url, transport, headers_enc, header_names, enabled, requires_approval, no_approval_tools, tools, tools_checked_at, last_error, created_at, updated_at) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                       "ON CONFLICT (user_id, name) DO UPDATE SET url=EXCLUDED.url, transport=EXCLUDED.transport, headers_enc=EXCLUDED.headers_enc, header_names=EXCLUDED.header_names, enabled=EXCLUDED.enabled, "
                       "requires_approval=EXCLUDED.requires_approval, no_approval_tools=EXCLUDED.no_approval_tools, tools=EXCLUDED.tools, tools_checked_at=EXCLUDED.tools_checked_at, "
                       "last_error=EXCLUDED.last_error, updated_at=EXCLUDED.updated_at RETURNING *",
                       (s.id, s.user_id, s.name, s.url, s.transport, s.headers_enc, Jsonb(s.header_names), s.enabled, s.requires_approval, Jsonb(s.no_approval_tools), Jsonb(s.tools),
                        s.tools_checked_at, s.last_error, s.created_at, utcnow()))
        return _mcp_from_row(rows[0])

    def get_mcp_server(self, user_id, name):
        rows = self._q("SELECT * FROM pb_mcp_servers WHERE user_id=%s AND name=%s", (user_id, name))
        return _mcp_from_row(rows[0]) if rows else None

    def list_mcp_servers(self, user_id):
        return [_mcp_from_row(r) for r in self._q("SELECT * FROM pb_mcp_servers WHERE user_id=%s ORDER BY name", (user_id,))]

    def update_mcp_server(self, user_id, name, **fields):
        from psycopg.types.json import Jsonb

        bad = set(fields) - _MCP_UPDATABLE
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        if not fields:
            return self.get_mcp_server(user_id, name)
        for k in ("header_names", "no_approval_tools", "tools"):
            if k in fields:
                fields[k] = Jsonb(fields[k])
        rows = self._q(f"UPDATE pb_mcp_servers SET {', '.join(f'{k}=%s' for k in fields)}, updated_at=%s WHERE user_id=%s AND name=%s RETURNING *",
                       (*fields.values(), utcnow(), user_id, name))
        return _mcp_from_row(rows[0]) if rows else None

    def delete_mcp_server(self, user_id, name):
        return bool(self._q("DELETE FROM pb_mcp_servers WHERE user_id=%s AND name=%s RETURNING id", (user_id, name)))

    # ── scheduled tasks ──
    def create_schedule(self, x):
        from psycopg.types.json import Jsonb

        self._q("INSERT INTO pb_schedules (id, user_id, title, instructions, cron, timezone, model, auto_approve, enabled, next_run_at, last_run_at, last_status, last_session_id, run_count, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (x.id, x.user_id, x.title, x.instructions, x.cron, x.timezone, x.model, Jsonb(x.auto_approve) if x.auto_approve else None, x.enabled, x.next_run_at, x.last_run_at,
                 x.last_status, x.last_session_id, x.run_count, x.created_at, x.updated_at))

    def get_schedule(self, schedule_id):
        rows = self._q("SELECT * FROM pb_schedules WHERE id=%s", (schedule_id,))
        return _schedule_from_row(rows[0]) if rows else None

    def list_schedules(self, user_id):
        return [_schedule_from_row(r) for r in self._q("SELECT * FROM pb_schedules WHERE user_id=%s ORDER BY created_at, id", (user_id,))]

    def update_schedule(self, schedule_id, **fields):
        from psycopg.types.json import Jsonb

        bad = set(fields) - _SCHEDULE_UPDATABLE
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        if not fields:
            return self.get_schedule(schedule_id)
        if "auto_approve" in fields:
            fields["auto_approve"] = Jsonb(fields["auto_approve"]) if fields["auto_approve"] else None
        rows = self._q(f"UPDATE pb_schedules SET {', '.join(f'{k}=%s' for k in fields)}, updated_at=%s WHERE id=%s RETURNING *", (*fields.values(), utcnow(), schedule_id))
        return _schedule_from_row(rows[0]) if rows else None

    def delete_schedule(self, schedule_id):
        self._q("DELETE FROM pb_schedule_runs WHERE schedule_id=%s", (schedule_id,))
        return bool(self._q("DELETE FROM pb_schedules WHERE id=%s RETURNING id", (schedule_id,)))

    def due_schedules(self, now, limit=50):
        return [_schedule_from_row(r) for r in self._q("SELECT * FROM pb_schedules WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= %s ORDER BY next_run_at, id LIMIT %s", (now, limit))]

    def claim_schedule(self, schedule_id, expected_next_run, new_next_run, now):
        return bool(self._q("UPDATE pb_schedules SET next_run_at=%s, updated_at=%s WHERE id=%s AND enabled AND next_run_at=%s RETURNING id",
                            (new_next_run, now, schedule_id, expected_next_run)))

    def add_schedule_run(self, r):
        self._q("INSERT INTO pb_schedule_runs (id, schedule_id, user_id, trigger, status, session_id, turn_id, note, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (r.id, r.schedule_id, r.user_id, r.trigger, r.status, r.session_id, r.turn_id, r.note, r.created_at))

    def update_schedule_run(self, run_id, **fields):
        allowed = {"status", "session_id", "turn_id", "note"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        if fields:
            self._q(f"UPDATE pb_schedule_runs SET {', '.join(f'{k}=%s' for k in fields)} WHERE id=%s", (*fields.values(), run_id))

    def list_schedule_runs(self, schedule_id, limit=20):
        return [ScheduleRunRecord(**{k: r[k] for k in _SCHEDULE_RUN_COLS}) for r in
                self._q("SELECT * FROM pb_schedule_runs WHERE schedule_id=%s ORDER BY created_at DESC, id DESC LIMIT %s", (schedule_id, limit))]

    def count_schedule_runs_since(self, user_id, since):
        rows = self._q("SELECT count(*) AS n FROM pb_schedule_runs WHERE user_id=%s AND created_at >= %s AND status='started'", (user_id, since))
        return int(rows[0]["n"])

    def latest_artifact(self, user_id, session_id, path):
        rows = self._q("SELECT * FROM pb_artifacts WHERE user_id=%s AND session_id=%s AND path=%s ORDER BY created_at DESC, id DESC LIMIT 1", (user_id, session_id, path))
        return _artifact_from_row(rows[0]) if rows else None

    def delete_token_events(self, run_id, message_id):
        rows = self._q("DELETE FROM pb_events WHERE run_id=%s AND type='token' AND payload->>'message_id' IS NOT DISTINCT FROM %s RETURNING seq", (run_id, message_id))
        return len(rows)

    # ── spare sandboxes ──────────────────────────────────────────────────
    def begin_spare(self, spare_id, now, stale_before, since, max_since):
        self._q("DELETE FROM pb_spares WHERE status='creating' AND created_at <= %s", (stale_before,))  # a creation that never finished
        rows = self._q(
            "INSERT INTO pb_spares (id, status, created_at) "
            "SELECT %s, 'creating', %s WHERE (SELECT count(*) FROM pb_spares WHERE created_at > %s) < %s "
            "ON CONFLICT DO NOTHING RETURNING id",  # the unique partial index allows ONE creating/ready row: a second begin loses the race cleanly
            (spare_id, now, since, max_since))
        return bool(rows)

    def finish_spare(self, spare_id, name, now, expires_at):
        rows = self._q("UPDATE pb_spares SET status='ready', name=%s, ready_at=%s, expires_at=%s WHERE id=%s AND status='creating' RETURNING id",
                       (name, now, expires_at, spare_id))
        return bool(rows)

    def delete_spare(self, spare_id):
        self._q("DELETE FROM pb_spares WHERE id=%s", (spare_id,))

    def current_spare(self, stale_before):
        rows = self._q("SELECT * FROM pb_spares WHERE status='ready' OR (status='creating' AND created_at > %s) ORDER BY created_at LIMIT 1", (stale_before,))
        return _spare_from_row(rows[0]) if rows else None

    def claim_spare(self, session_id, now, min_remaining_seconds):
        rows = self._q(
            "UPDATE pb_spares SET status='claimed', claimed_by=%s, claimed_at=%s WHERE id = ("
            " SELECT id FROM pb_spares WHERE status='ready' AND expires_at > %s ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *",
            (session_id, now, now + timedelta(seconds=min_remaining_seconds)))
        return _spare_from_row(rows[0]) if rows else None

    def spares_to_sweep(self, now, unclaimed_before, stale_before):
        rows = self._q("SELECT * FROM pb_spares WHERE (status='ready' AND (ready_at < %s OR expires_at <= %s)) OR (status='creating' AND created_at <= %s)",
                       (unclaimed_before, now, stale_before))
        return [_spare_from_row(r) for r in rows]

    def purge_claimed_spares(self, before):
        return len(self._q("DELETE FROM pb_spares WHERE status='claimed' AND claimed_at < %s RETURNING id", (before,)))


class PostgresLedger:
    """EffectLedger on Postgres: idempotency keys and daily quotas for side-effecting tools (see agent/tools/effects.py)."""

    def __init__(self, pool, clock=utcnow):
        self.pool, self._clock = pool, clock

    def claim(self, key: str, kind: str) -> bool:
        with self.pool.connection() as conn:
            cur = conn.execute("INSERT INTO pb_effects (key, kind, created_at) VALUES (%s,%s,%s) ON CONFLICT (key) DO NOTHING RETURNING key", (key, kind, self._clock()))
            return cur.fetchone() is not None

    def release(self, key: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM pb_effects WHERE key=%s", (key,))

    def count_since(self, kind: str, since: datetime) -> int:
        with self.pool.connection() as conn:
            return conn.execute("SELECT count(*) AS n FROM pb_effects WHERE kind=%s AND created_at >= %s", (kind, since)).fetchone()["n"]
