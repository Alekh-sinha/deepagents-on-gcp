"""MemoryStore: the in-memory state store (tests and local use). Everything is lost on restart."""
from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta
from pinboard.state.records import (ACTIVE_RUN_STATUSES, ApprovalRecord, ArtifactRecord, McpServerRecord, RunRecord, ScheduleRecord, ScheduleRunRecord, SessionRecord,
                                    SpareRecord, _MCP_UPDATABLE, _SCHEDULE_UPDATABLE, _SESSION_UPDATABLE, _UPDATABLE, utcnow)


class MemoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, RunRecord] = {}
        self._approvals: dict[str, ApprovalRecord] = {}
        self._audit: list[dict] = []
        self._sessions: dict[str, SessionRecord] = {}
        self._events: dict[str, list[dict]] = {}
        self._artifacts: dict[str, ArtifactRecord] = {}
        self._spares: dict[str, SpareRecord] = {}
        self._mcp: dict[tuple[str, str], McpServerRecord] = {}
        self._schedules: dict[str, ScheduleRecord] = {}
        self._schedule_runs: list[ScheduleRunRecord] = []

    def setup(self) -> None:
        pass

    # ── spare sandboxes ──────────────────────────────────────────────────
    def begin_spare(self, spare_id, now, stale_before, since, max_since):
        with self._lock:
            for k in [k for k, x in self._spares.items() if x.status == "creating" and x.created_at <= stale_before]:
                del self._spares[k]  # a creation that never finished
            if any(x.status in ("creating", "ready") for x in self._spares.values()):
                return False
            if sum(1 for x in self._spares.values() if x.created_at > since) >= max_since:
                return False
            self._spares[spare_id] = SpareRecord(id=spare_id, status="creating", created_at=now)
            return True

    def finish_spare(self, spare_id, name, now, expires_at):
        with self._lock:
            x = self._spares.get(spare_id)
            if x is None or x.status != "creating":
                return False
            x.status, x.name, x.ready_at, x.expires_at = "ready", name, now, expires_at
            return True

    def delete_spare(self, spare_id):
        with self._lock:
            self._spares.pop(spare_id, None)

    def current_spare(self, stale_before):
        with self._lock:
            for x in self._spares.values():
                if x.status == "ready" or (x.status == "creating" and x.created_at > stale_before):
                    return replace(x)
            return None

    def claim_spare(self, session_id, now, min_remaining_seconds):
        with self._lock:
            ready = sorted((x for x in self._spares.values() if x.status == "ready" and x.expires_at - now > timedelta(seconds=min_remaining_seconds)),
                           key=lambda x: x.created_at)
            if not ready:
                return None
            x = ready[0]
            x.status, x.claimed_by, x.claimed_at = "claimed", session_id, now
            return replace(x)

    def spares_to_sweep(self, now, unclaimed_before, stale_before):
        with self._lock:
            return [replace(x) for x in self._spares.values()
                    if (x.status == "ready" and (x.ready_at < unclaimed_before or x.expires_at <= now)) or (x.status == "creating" and x.created_at <= stale_before)]

    def purge_claimed_spares(self, before):
        with self._lock:
            old = [k for k, x in self._spares.items() if x.status == "claimed" and x.claimed_at < before]
            for k in old:
                del self._spares[k]
            return len(old)

    def create_run(self, run):
        with self._lock:
            self._runs[run.id] = replace(run)

    def get_run(self, run_id):
        with self._lock:
            r = self._runs.get(run_id)
            return replace(r) if r else None

    def update_run(self, run_id, **fields):
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        with self._lock:
            r = self._runs[run_id]
            for k, v in fields.items():
                setattr(r, k, v)
            r.updated_at = utcnow()
            return replace(r)

    def claim_run(self, run_id, from_status, to_status, now):
        with self._lock:
            r = self._runs.get(run_id)
            if r is None or r.status != from_status:
                return False
            r.status, r.updated_at = to_status, now
            return True

    def active_run_for_thread(self, thread_id):
        with self._lock:
            for r in self._runs.values():
                if r.thread_id == thread_id and r.status in ACTIVE_RUN_STATUSES:
                    return replace(r)
            return None

    def create_approval(self, approval):
        with self._lock:
            self._approvals[approval.id] = replace(approval)

    def get_approval(self, approval_id):
        with self._lock:
            a = self._approvals.get(approval_id)
            return replace(a) if a else None

    def list_approvals(self, user_id, status=None, limit=50):
        with self._lock:
            items = [a for a in self._approvals.values() if a.user_id == user_id and (status is None or a.status == status)]
            items.sort(key=lambda a: a.created_at, reverse=True)
            return [replace(a) for a in items[:limit]]

    def decide_approval(self, approval_id, status, decision, decided_by, now):
        with self._lock:
            a = self._approvals.get(approval_id)
            if a is None or a.status != "pending" or a.expires_at <= now:
                return None
            a.status, a.decision, a.decided_by, a.decided_at = status, decision, decided_by, now
            return replace(a)

    def expire_approvals(self, now):
        with self._lock:
            out = []
            for a in self._approvals.values():
                if a.status == "pending" and a.expires_at <= now:
                    a.status, a.decided_at = "expired", now
                    out.append(replace(a))
            return out

    def add_audit(self, user_id, run_id, event, detail, now=None):
        with self._lock:
            self._audit.append({"ts": (now or utcnow()).isoformat(), "user_id": user_id, "run_id": run_id, "event": event, "detail": detail})

    def list_audit(self, run_id=None, limit=200):
        with self._lock:
            rows = [a for a in self._audit if run_id is None or a["run_id"] == run_id]
            return rows[-limit:]

    # ── sessions ──
    def create_session(self, session):
        with self._lock:
            self._sessions[session.id] = replace(session)

    def get_session(self, session_id):
        with self._lock:
            x = self._sessions.get(session_id)
            return replace(x) if x else None

    def update_session(self, session_id, **fields):
        bad = set(fields) - _SESSION_UPDATABLE
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        with self._lock:
            x = self._sessions[session_id]
            for k, v in fields.items():
                setattr(x, k, v)
            return replace(x)

    def list_sessions(self, user_id, limit=50, include_archived=False):
        with self._lock:
            items = [x for x in self._sessions.values() if x.user_id == user_id and (include_archived or not x.archived)]
            items.sort(key=lambda x: x.last_active_at, reverse=True)
            return [replace(x) for x in items[:limit]]

    def idle_sessions_with_sandbox(self, before):
        with self._lock:
            busy = {r.session_id for r in self._runs.values() if r.status in ACTIVE_RUN_STATUSES and r.session_id}
            return [replace(x) for x in self._sessions.values() if x.sandbox_name and x.last_active_at <= before and x.id not in busy]

    # ── leases ──
    def acquire_lease(self, run_id, lease_id, now, ttl_seconds):
        with self._lock:
            r = self._runs.get(run_id)
            if r is None or r.status != "running" or (r.lease_id and r.lease_expires_at and r.lease_expires_at > now):
                return False
            r.lease_id, r.lease_expires_at, r.heartbeat_at, r.updated_at = lease_id, now + timedelta(seconds=ttl_seconds), now, now
            return True

    def renew_lease(self, run_id, lease_id, now, ttl_seconds):
        with self._lock:
            r = self._runs.get(run_id)
            if r is None or r.lease_id != lease_id:
                return False
            r.lease_expires_at, r.heartbeat_at = now + timedelta(seconds=ttl_seconds), now
            return True

    def release_lease(self, run_id, lease_id):
        with self._lock:
            r = self._runs.get(run_id)
            if r is None or r.lease_id != lease_id:
                return False
            r.lease_id = r.lease_expires_at = None
            return True

    def _is_stale(self, r, now, grace_seconds):
        if r.status != "running":
            return False
        if r.lease_expires_at is not None:
            return r.lease_expires_at <= now
        return r.updated_at <= now - timedelta(seconds=grace_seconds)  # never got a worker

    def stale_runs(self, now, grace_seconds=120):
        with self._lock:
            return [replace(r) for r in self._runs.values() if self._is_stale(r, now, grace_seconds)]

    def mark_interrupted(self, run_id, now, grace_seconds=120):
        with self._lock:
            r = self._runs.get(run_id)
            if r is None or not self._is_stale(r, now, grace_seconds):
                return False
            r.status, r.lease_id, r.lease_expires_at, r.updated_at = "interrupted", None, None, now
            return True

    def request_cancel(self, run_id):
        with self._lock:
            r = self._runs[run_id]
            r.cancel_requested, r.updated_at = True, utcnow()
            return replace(r)

    # ── events ──
    def append_event(self, run_id, lease_id, type, payload, now=None):
        from pinboard.engine.events import cap_payload

        with self._lock:
            r = self._runs.get(run_id)
            if r is None or (lease_id is not None and r.lease_id != lease_id):
                return None  # fenced: this writer no longer owns the turn
            r.event_seq += 1
            self._events.setdefault(run_id, []).append({"seq": r.event_seq, "type": type, "payload": cap_payload(payload), "ts": (now or utcnow()).isoformat()})
            return r.event_seq

    def list_events(self, run_id, after_seq=0, limit=500):
        with self._lock:
            return [dict(e) for e in self._events.get(run_id, []) if e["seq"] > after_seq][:limit]

    def last_event_of_type(self, run_id, type):
        with self._lock:
            for e in reversed(self._events.get(run_id, [])):
                if e["type"] == type:
                    return dict(e)
            return None

    def list_session_runs(self, session_id, limit=50):
        with self._lock:
            items = sorted((r for r in self._runs.values() if r.session_id == session_id), key=lambda r: r.created_at)
            return [replace(r) for r in items[-limit:]]

    def create_artifact(self, artifact):
        with self._lock:
            self._artifacts[artifact.id] = replace(artifact)

    def get_artifact(self, artifact_id):
        with self._lock:
            a = self._artifacts.get(artifact_id)
            return replace(a) if a else None

    def list_artifacts(self, user_id, session_id=None, limit=50):
        with self._lock:
            items = [a for a in self._artifacts.values() if a.user_id == user_id and (session_id is None or a.session_id == session_id)]
            items.sort(key=lambda a: (a.created_at, a.id), reverse=True)
            return [replace(a) for a in items[:limit]]

    # ── bring-your-own MCP servers ──
    def put_mcp_server(self, server):
        with self._lock:
            old = self._mcp.get((server.user_id, server.name))
            rec = replace(server, id=old.id if old else server.id, created_at=old.created_at if old else server.created_at, updated_at=utcnow())
            self._mcp[(server.user_id, server.name)] = rec
            return replace(rec)

    def get_mcp_server(self, user_id, name):
        with self._lock:
            x = self._mcp.get((user_id, name))
            return replace(x) if x else None

    def list_mcp_servers(self, user_id):
        with self._lock:
            return [replace(x) for x in sorted((x for (u, _), x in self._mcp.items() if u == user_id), key=lambda x: x.name)]

    def update_mcp_server(self, user_id, name, **fields):
        bad = set(fields) - _MCP_UPDATABLE
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        with self._lock:
            x = self._mcp.get((user_id, name))
            if x is None:
                return None
            for k, v in fields.items():
                setattr(x, k, v)
            x.updated_at = utcnow()
            return replace(x)

    def delete_mcp_server(self, user_id, name):
        with self._lock:
            return self._mcp.pop((user_id, name), None) is not None

    # ── scheduled tasks ──
    def create_schedule(self, schedule):
        with self._lock:
            self._schedules[schedule.id] = replace(schedule)

    def get_schedule(self, schedule_id):
        with self._lock:
            x = self._schedules.get(schedule_id)
            return replace(x) if x else None

    def list_schedules(self, user_id):
        with self._lock:
            return [replace(x) for x in sorted((x for x in self._schedules.values() if x.user_id == user_id), key=lambda x: (x.created_at, x.id))]

    def update_schedule(self, schedule_id, **fields):
        bad = set(fields) - _SCHEDULE_UPDATABLE
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        with self._lock:
            x = self._schedules.get(schedule_id)
            if x is None:
                return None
            for k, v in fields.items():
                setattr(x, k, v)
            x.updated_at = utcnow()
            return replace(x)

    def delete_schedule(self, schedule_id):
        with self._lock:
            self._schedule_runs = [r for r in self._schedule_runs if r.schedule_id != schedule_id]
            return self._schedules.pop(schedule_id, None) is not None

    def due_schedules(self, now, limit=50):
        with self._lock:
            due = sorted((x for x in self._schedules.values() if x.enabled and x.next_run_at is not None and x.next_run_at <= now), key=lambda x: (x.next_run_at, x.id))
            return [replace(x) for x in due[:limit]]

    def claim_schedule(self, schedule_id, expected_next_run, new_next_run, now):
        """Atomically move a due schedule to its next firing time. Only the caller that sees ``expected_next_run`` still in place wins, so two
        instances ticking at once fire a schedule once."""
        with self._lock:
            x = self._schedules.get(schedule_id)
            if x is None or not x.enabled or x.next_run_at != expected_next_run:
                return False
            x.next_run_at, x.updated_at = new_next_run, now
            return True

    def add_schedule_run(self, run):
        with self._lock:
            self._schedule_runs.append(replace(run))

    def update_schedule_run(self, run_id, **fields):
        with self._lock:
            for r in self._schedule_runs:
                if r.id == run_id:
                    for k, v in fields.items():
                        setattr(r, k, v)

    def list_schedule_runs(self, schedule_id, limit=20):
        with self._lock:
            return [replace(r) for r in sorted((r for r in self._schedule_runs if r.schedule_id == schedule_id), key=lambda r: (r.created_at, r.id), reverse=True)[:limit]]

    def count_schedule_runs_since(self, user_id, since):
        with self._lock:
            return sum(1 for r in self._schedule_runs if r.user_id == user_id and r.created_at >= since and r.status == "started")

    def latest_artifact(self, user_id, session_id, path):
        with self._lock:
            items = [a for a in self._artifacts.values() if a.user_id == user_id and a.session_id == session_id and a.path == path]
            return replace(max(items, key=lambda a: (a.created_at, a.id))) if items else None

    def delete_token_events(self, run_id, message_id):
        with self._lock:
            keep = [e for e in self._events.get(run_id, []) if not (e["type"] == "token" and e["payload"].get("message_id") == message_id)]
            n = len(self._events.get(run_id, [])) - len(keep)
            self._events[run_id] = keep
            return n
