"""Running turns in the background: an executor with a capacity limit, and the lease that proves a worker still owns its turn.

A worker takes a LEASE on a turn and renews it on a heartbeat. Every event it writes is fenced by the lease id, so a worker that was
replaced (it went quiet and the reaper handed the turn on) can no longer write anything, even if it wakes up later.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from pinboard.state import StateStore

log = logging.getLogger(__name__)


class InlineExecutor:
    """Runs jobs immediately in the calling thread. Used by the CLI, the legacy synchronous API and most tests."""

    def has_capacity(self) -> bool:
        return True

    def try_submit(self, fn: Callable[[], None]) -> bool:
        fn()
        return True

    def submit(self, fn: Callable[[], None]) -> None:
        fn()


class ThreadExecutor:
    """One daemon thread per job. New turns are refused (not queued) when ``max_concurrent`` jobs are already running;
    resumes of turns that were already admitted always run (``submit``)."""

    def __init__(self, max_concurrent: int):
        self.max_concurrent = max(1, int(max_concurrent))
        self._active = 0
        self._lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def has_capacity(self) -> bool:
        return self.active < self.max_concurrent

    def _start(self, fn: Callable[[], None]) -> None:
        def run() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001  the job reports its own failures; this is the last resort
                log.exception("background turn crashed")
            finally:
                with self._lock:
                    self._active -= 1

        with self._lock:
            self._active += 1
        t = threading.Thread(target=run, daemon=True, name="pinboard-turn")
        self.threads = [x for x in self.threads if x.is_alive()] + [t]
        t.start()

    def try_submit(self, fn: Callable[[], None]) -> bool:
        with self._lock:
            if self._active >= self.max_concurrent:
                return False
        self._start(fn)
        return True

    def submit(self, fn: Callable[[], None]) -> None:
        self._start(fn)

    def join(self, timeout: float = 10.0) -> None:
        for t in list(self.threads):
            t.join(timeout)


class LeaseKeeper:
    """Renews a turn's lease on a heartbeat thread. ``lost`` becomes true if someone else took the turn over."""

    def __init__(self, store: StateStore, run_id: str, lease_id: str, ttl_seconds: float, interval_seconds: float, clock, *, start_thread: bool = True):
        self.store, self.run_id, self.lease_id = store, run_id, lease_id
        self.ttl, self.interval, self.clock = ttl_seconds, interval_seconds, clock
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="pinboard-lease") if start_thread else None

    def renew(self) -> bool:
        ok = False
        try:
            ok = self.store.renew_lease(self.run_id, self.lease_id, self.clock(), self.ttl)
        except Exception:  # noqa: BLE001  a database blip must not kill the turn; the next beat tries again
            log.exception("lease renewal failed for %s", self.run_id)
            return True
        if not ok:
            self.lost.set()
        return ok

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            if not self.renew():
                return

    def start(self) -> "LeaseKeeper":
        if self._thread:
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(2)


class TurnContext:
    """What a running segment of a turn needs: write fenced events, notice when it has lost the turn."""

    def __init__(self, store: StateStore, run_id: str, lease_id: str, clock):
        self.store, self.run_id, self.lease_id, self.clock = store, run_id, lease_id, clock
        self.lost = False
        self.timing: dict = {}  # filled in by the service as the segment progresses (probe_seconds, build_seconds, ...)

    def emit(self, type: str, payload: dict) -> bool:
        """Append an event; False means this worker no longer owns the turn (it must stop and touch nothing else)."""
        if self.store.append_event(self.run_id, self.lease_id, type, payload, self.clock()) is None:
            self.lost = True
        return not self.lost
