"""Effect ledger: idempotency and quotas for side-effecting tools.

A resumed turn replays the step that was in flight, so a tool must be able to ask "did I already do this?".
``claim`` is atomic: the first caller for a key gets True, everyone else False. M4 adds a Postgres implementation
(table pb_effects); the in-memory one is for tests and local runs.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Protocol

from pinboard.state import utcnow


class EffectLedger(Protocol):
    def claim(self, key: str, kind: str) -> bool: ...
    def release(self, key: str) -> None: ...
    def count_since(self, kind: str, since: datetime) -> int: ...


class InMemoryLedger:
    def __init__(self, clock=utcnow):
        self._items: dict[str, tuple[str, datetime]] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def claim(self, key: str, kind: str) -> bool:
        with self._lock:
            if key in self._items:
                return False
            self._items[key] = (kind, self._clock())
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def count_since(self, kind: str, since: datetime) -> int:
        with self._lock:
            return sum(1 for k, t in self._items.values() if k == kind and t >= since)
