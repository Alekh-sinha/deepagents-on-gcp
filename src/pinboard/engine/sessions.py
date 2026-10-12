"""Conversations: create / list / rename / archive, the transcript, what a frontend needs to draw itself (config), and the model list."""
from __future__ import annotations

import logging
from typing import Any


from pinboard.core.policy import PolicyDenied
from pinboard.state import (ACTIVE_RUN_STATUSES, SessionRecord, new_session_id)
from pinboard.engine import auto_approve as auto
from pinboard.engine.errors import BadRequest, Conflict, NotFound

log = logging.getLogger(__name__)

TRANSCRIPT_EVENTS_PER_TURN = 400
TRANSCRIPT_MAX_TURNS = 50


class SessionsMixin:
    """The session side of RunService (needs self.store, self.settings, self.policy, self.clock)."""

    # ═════════════════════════ sessions ═════════════════════════
    def _auto_setting(self, value: Any) -> dict | None:
        cap = self.policy.limits.max_auto_approvals_per_turn
        return auto.normalise(value, default_cap=cap, hard_cap=cap)

    def create_session(self, *, user_id: str | None = None, title: str = "", model: str | None = None, auto_approve: Any = auto.UNSET, schedule_id: str = "") -> SessionRecord:
        sid = new_session_id()
        session = SessionRecord(id=sid, user_id=user_id or self.settings.user_id, thread_id=sid, title=(title or "")[:120], model=self._model(model),
                                created_at=self.clock(), last_active_at=self.clock(), schedule_id=schedule_id,
                                auto_approve=None if auto_approve is auto.UNSET else self._auto_setting(auto_approve))
        self.store.create_session(session)
        return session

    def list_sessions(self, *, user_id: str | None = None, limit: int = 50, include_archived: bool = False) -> list[SessionRecord]:
        return self.store.list_sessions(user_id or self.settings.user_id, limit, include_archived)

    def update_session(self, session_id: str, changes: dict, *, user_id: str | None = None) -> SessionRecord:
        """Rename a conversation, change its default model, or archive/restore it. ``changes`` holds only the keys to change;
        ``model`` of None or "" goes back to the policy default. Anything else is refused."""
        session = self._session(session_id, user_id)
        unknown = set(changes) - {"title", "model", "archived", "auto_approve"}
        if unknown or not changes:
            raise BadRequest("send at least one of: title, model, archived, auto_approve" if not unknown else f"cannot change: {', '.join(sorted(unknown))}")
        fields: dict[str, Any] = {}
        if "title" in changes:
            title = changes["title"]
            if not isinstance(title, str) or len(title) > 120:
                raise BadRequest("'title' must be a string of at most 120 characters")
            fields["title"] = title.strip()
        if "model" in changes:
            fields["model"] = self._model(changes["model"] or None)  # validated against the policy's allowed list
        if "auto_approve" in changes:  # takes effect at the next pause, even in a turn that is running now; stays until changed
            fields["auto_approve"] = self._auto_setting(changes["auto_approve"])
        if "archived" in changes:
            if not isinstance(changes["archived"], bool):
                raise BadRequest("'archived' must be true or false")
            if changes["archived"] and self.store.active_run_for_thread(session.thread_id):
                raise Conflict("this session has a turn in progress or awaiting approval; finish or cancel it before archiving")
            fields["archived"] = changes["archived"]
        return self.store.update_session(session.id, **fields)

    def get_config(self, *, user_id: str | None = None) -> dict:
        """What a frontend needs to draw itself and explain approvals: who is signed in, the models, and the rules in force. No secrets."""
        p = self.policy
        return {
            "user": user_id or self.settings.user_id,
            "default_model": p.default_model,
            "models": p.list_models()["models"],
            "limits": {"max_turn_minutes": p.limits.max_turn_minutes, "max_concurrent_turns": p.limits.max_concurrent_turns,
                       "sandbox_idle_minutes": p.limits.sandbox_idle_minutes, "max_message_chars": self.settings.max_prompt_chars},
            "email": {"from_inbox": p.email.from_inbox, "allowed_recipients": list(p.email.allowed_recipients), "max_per_day": p.email.max_per_day,
                      "max_attachment_mb": p.email.max_attachment_mb},
            "github": {"allowed_repos": list(p.github.allowed_repos), "branch_prefix": p.github.branch_prefix},
            "approvals": {"shell_commands": self.settings.approval_mode == "execute", "ttl_seconds": self.settings.approval_ttl_seconds,
                          "auto_approve": auto.describe_options(p.limits.max_auto_approvals_per_turn, p.limits.max_auto_approvals_per_turn, p.never_auto_tools)},
            "warm_sandbox": {"mode": p.warm_sandbox.mode},
            "features": {"sandbox": p.features.sandbox, "artifacts": p.features.artifacts},
        }

    def get_transcript(self, session_id: str, *, user_id: str | None = None, limit: int = 20, before_turn_id: str | None = None) -> dict:
        """The conversation to draw, oldest first: the latest ``limit`` turns (or the ``limit`` before ``before_turn_id``), each with its
        events. One call replaces a get_events call per turn. A turn with more than ``TRANSCRIPT_EVENTS_PER_TURN`` events is cut short:
        ``truncated`` is true and ``next_seq`` is where get_events/stream_turn can continue."""
        session = self._session(session_id, user_id)
        runs = self.store.list_session_runs(session.id)
        if before_turn_id:
            ids = [r.id for r in runs]
            if before_turn_id not in ids:
                raise NotFound("turn not found in this session")
            runs = runs[:ids.index(before_turn_id)]
        limit = max(1, min(int(limit), TRANSCRIPT_MAX_TURNS))
        page = runs[-limit:]
        turns = []
        for r in page:
            if r.status == "running":
                r = self._reap(r)[0]
            events = self.store.list_events(r.id, 0, TRANSCRIPT_EVENTS_PER_TURN + 1)
            truncated = len(events) > TRANSCRIPT_EVENTS_PER_TURN
            events = events[:TRANSCRIPT_EVENTS_PER_TURN]
            turns.append({"turn": r.to_dict(), "events": events, "truncated": truncated, "next_seq": events[-1]["seq"] if events else 0})
        return {"session_id": session.id, "turns": turns, "has_more": len(runs) > len(page)}

    def _session(self, session_id: str, user_id: str | None) -> SessionRecord:
        s = self.store.get_session(session_id)
        if s is None or s.user_id != (user_id or self.settings.user_id):
            raise NotFound("session not found")
        return s

    def get_session(self, session_id: str, *, user_id: str | None = None) -> dict:
        """Everything a client needs to draw a conversation: its turns, what is waiting for the user, and the live todo list."""
        user_id = user_id or self.settings.user_id
        session = self._session(session_id, user_id)
        turns = [self._reap(r)[0] if r.status == "running" else r for r in self.store.list_session_runs(session.id)]
        active = next((r for r in reversed(turns) if r.status in ACTIVE_RUN_STATUSES), None)
        run_ids = {r.id for r in turns}
        pending = [a.to_dict() for a in self.store.list_approvals(user_id, "pending") if a.run_id in run_ids]
        todos = None
        for r in reversed(turns):
            ev = self.store.last_event_of_type(r.id, "todos")
            if ev:
                todos = ev["payload"].get("todos")
                break
        return {"session": session.to_dict(), "turns": [r.to_dict() for r in turns], "active_turn_id": active.id if active else None,
                "pending_approvals": pending, "todos": todos}

    def _model(self, model: str | None) -> str | None:
        if not model:
            return None
        try:
            return self.policy.resolve_model(model).id
        except PolicyDenied as exc:
            raise BadRequest(exc.message()) from exc

    def list_models(self) -> dict:
        return self.policy.list_models()
