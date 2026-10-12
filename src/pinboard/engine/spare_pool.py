"""The warm pool: one spare sandbox kept ready so the first file or shell use does not wait 30-130 s. See SparePoolMixin."""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import timedelta
from typing import Callable


from pinboard.state import (SessionRecord)

log = logging.getLogger(__name__)

SPARE_STALE_SECONDS = 600  # a spare that has been "being created" this long is given up on
SPARE_JOIN_SECONDS = 90.0  # a turn that needs a sandbox while a spare is still being made waits this long for it, then makes its own
SPARE_POLL_SECONDS = 2.0


def _in_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="pinboard-spare").start()


class SparePoolMixin:
    """The spare-sandbox side of RunService (needs self.store, self.policy, self.sandboxes, self.settings, self.clock, self._spare_runner,
    self._spare_factory, self._delete_sandbox, self._sleep, self._mono)."""

    # ═════════════════════════ spare sandboxes (the warm pool) ═════════════════════════
    def warm_sandbox(self, session_id: str, *, user_id: str | None = None) -> dict:
        """Called when the user starts typing: make sure a sandbox will be ready by the time one is needed. Cheap and safe to repeat.
        Returns {"status": off | have_sandbox | ready | warming | limit_reached}."""
        session = self._session(session_id, user_id)
        if self.policy.warm_sandbox.mode == "off":
            return {"status": "off"}
        if self.sandboxes.adoption(session):
            return {"status": "have_sandbox"}  # this conversation already has a live sandbox
        spare = self.store.current_spare(self._spare_stale())
        if spare is None and self._start_spare():
            return {"status": "warming"}
        spare = spare or self.store.current_spare(self._spare_stale())  # someone else may have just started one
        if spare is None:
            return {"status": "limit_reached"}  # today's cap on spares is used up
        return {"status": "ready" if spare.status == "ready" else "warming"}

    def _spare_stale(self):
        return self.clock() - timedelta(seconds=SPARE_STALE_SECONDS)

    def _start_spare(self) -> bool:
        """Begin making ONE spare in the background. False if one already exists / is being made, or today's cap is reached."""
        cfg, now = self.policy.warm_sandbox, self.clock()
        spare_id = "sp_" + uuid.uuid4().hex[:14]
        if not self.store.begin_spare(spare_id, now, self._spare_stale(), now - timedelta(hours=24), cfg.max_spares_per_day):
            return False
        self._spare_runner(lambda: self._make_spare(spare_id, now))
        return True

    def _make_spare(self, spare_id: str, started) -> None:
        try:
            name = self._spare_factory()
        except Exception:  # noqa: BLE001  nothing was lost: the next trigger tries again (the daily cap still counts this attempt)
            log.exception("could not create a spare sandbox")
            self.store.delete_spare(spare_id)
            return
        # the sandbox's own lifetime started when we began making it, so count from there
        if not self.store.finish_spare(spare_id, name, self.clock(), started + timedelta(seconds=self.settings.sandbox_ttl_seconds)):
            log.warning("spare %s was swept while being made; deleting the sandbox %s", spare_id, name)
            self._delete_sandbox(name)

    def _claim_spare(self, session: SessionRecord) -> str | None:
        """The hook a turn calls the moment a tool first needs a sandbox: take the ready spare (waiting a little for one being made)."""
        if self.policy.warm_sandbox.mode == "off":
            return None
        deadline = self._mono() + SPARE_JOIN_SECONDS
        while True:
            spare = self.store.claim_spare(session.id, self.clock(), self.settings.sandbox_ttl_seconds / 2)  # not one that is about to expire
            if spare is not None:
                self.sandboxes.record_claimed(session.id, spare.name, spare.expires_at)
                self._start_spare()  # the next session (or this one, later) will find another ready, within the daily cap
                return spare.name
            pending = self.store.current_spare(self._spare_stale())
            if pending is None or pending.status != "creating" or self._mono() >= deadline:
                return None
            self._sleep(SPARE_POLL_SECONDS)

    def _sweep_spares(self) -> int:
        cfg, now = self.policy.warm_sandbox, self.clock()
        n = 0
        for spare in self.store.spares_to_sweep(now, now - timedelta(minutes=cfg.spare_max_minutes), self._spare_stale()):
            if spare.name:
                self._delete_sandbox(spare.name)
            self.store.delete_spare(spare.id)
            n += 1
        self.store.purge_claimed_spares(now - timedelta(days=2))
        if cfg.mode == "always":
            self._start_spare()
        return n
