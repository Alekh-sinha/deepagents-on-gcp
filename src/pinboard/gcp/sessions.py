"""The sandbox belongs to the SESSION, not to a turn: later turns attach to it (files survive), and it is replaced after it expires.

This class only does the bookkeeping on the session record and the clean-up; opening and attaching is done by the agent builder
(``adopt_sandbox``) and deleting by ``gcp.sandbox.delete_sandbox``. A sandbox is deleted only when its session has been idle for
``limits.sandbox_idle_minutes`` and has no turn running or waiting on an approval.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from pinboard.state import SessionRecord, StateStore

log = logging.getLogger(__name__)

EXPIRY_MARGIN = timedelta(seconds=120)  # do not attach to a sandbox that is about to expire under us

REPLACED_NOTE = (
    "[System note: your sandbox was replaced since the last message, so files you created earlier in /workspace are gone. "
    "Files you saved in /workspace/out earlier are kept as artifacts: use list_artifacts and restore_artifact to bring back what you need, "
    "or recreate it from the conversation.]\n\n"
)


class SessionSandboxes:
    def __init__(self, store: StateStore, ttl_seconds: int, deleter: Callable[[str], None], clock: Callable[[], datetime]):
        self.store, self.ttl, self._delete, self.clock = store, ttl_seconds, deleter, clock

    def adoption(self, session: SessionRecord) -> str | None:
        """Name of the sandbox this session can still attach to, or None (create a new one)."""
        if session.sandbox_name and session.sandbox_expires_at and session.sandbox_expires_at - self.clock() > EXPIRY_MARGIN:
            return session.sandbox_name
        return None

    def was_replaced(self, session: SessionRecord, adopted: str | None) -> bool:
        """A new sandbox is only a REPLACEMENT if this session had one before (the stored name is cleared by the idle sweep, so the
        session remembers separately that it ever had one: earlier chat-only turns lost nothing)."""
        return (session.had_sandbox or bool(session.sandbox_name)) and adopted is None

    def build_options(self, session: SessionRecord, claim: Callable[[], str | None]) -> dict:
        """What the agent builder is told about the sandbox: create it lazily (the first time a tool needs it), record it on the session
        at that moment, and offer the warm-pool claim."""
        return {"lazy_sandbox": True, "on_sandbox_created": lambda name: self.record(session.id, name, attached=False), "claim_sandbox": claim}

    def forget(self, session_id: str) -> None:
        """The recorded sandbox could not be attached (it is gone): drop it so the next open creates a new one."""
        self.store.update_session(session_id, sandbox_name=None, sandbox_expires_at=None)

    def record(self, session_id: str, sandbox_name: str, *, attached: bool) -> None:
        """Remember the sandbox a turn is using. A freshly created one gets its expiry stamped; an attached one keeps its old expiry."""
        if not attached:
            self.store.update_session(session_id, sandbox_name=sandbox_name, sandbox_expires_at=self.clock() + timedelta(seconds=self.ttl), had_sandbox=True)

    def record_claimed(self, session_id: str, sandbox_name: str, expires_at) -> None:
        """The session took over a ready spare: it keeps the spare's own expiry (the sandbox's lifetime started when it was made)."""
        self.store.update_session(session_id, sandbox_name=sandbox_name, sandbox_expires_at=expires_at, had_sandbox=True)

    def sweep(self, idle_minutes: int) -> int:
        """Delete the sandboxes of sessions idle for ``idle_minutes``. Returns how many were deleted."""
        n = 0
        for s in self.store.idle_sessions_with_sandbox(self.clock() - timedelta(minutes=idle_minutes)):
            try:
                self._delete(s.sandbox_name)
            except Exception:  # noqa: BLE001  a failed delete is retried on the next sweep; the platform TTL is the backstop
                log.exception("could not delete idle sandbox %s", s.sandbox_name)
                continue
            self.forget(s.id)
            n += 1
        return n


class NoSessionSandboxes:
    """Stands in for SessionSandboxes when the agent has no sandbox (policy ``features.sandbox: false``): same methods, nothing to look after.
    The engine calls these exactly as it calls the real ones, so it needs no checks of its own."""

    def adoption(self, session: SessionRecord) -> None:
        return None

    def was_replaced(self, session: SessionRecord, adopted: str | None) -> bool:
        return False

    def build_options(self, session: SessionRecord, claim: Callable[[], str | None]) -> dict:
        return {}

    def forget(self, session_id: str) -> None:
        pass

    def record(self, session_id: str, sandbox_name: str, *, attached: bool) -> None:
        pass

    def record_claimed(self, session_id: str, sandbox_name: str, expires_at) -> None:
        pass

    def sweep(self, idle_minutes: int) -> int:
        return 0
