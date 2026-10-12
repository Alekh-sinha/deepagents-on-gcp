"""Persistent state: runs, approvals and an audit trail, plus the LangGraph checkpointer.

Two interchangeable stores with identical semantics:
  * MemoryStore   - tests and local dev (lost on restart)
  * PostgresStore - durable; any Postgres works (local, Cloud SQL, Neon, ...)

The approval decision is a single atomic UPDATE ... WHERE status='pending', so a
double click or a retried request can never execute an action twice.

Layout: records.py (the records), store.py (the interface), memory.py, postgres.py; this file builds the configured state."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from pinboard.state.memory import MemoryStore
from pinboard.state.postgres import PostgresLedger, PostgresStore, make_pool
from pinboard.state.records import (ACTIVE_RUN_STATUSES, RUN_STATUSES, ApprovalRecord, ArtifactRecord, McpServerRecord, RunRecord, ScheduleRecord, ScheduleRunRecord,
                                    SessionRecord, SpareRecord, new_approval_id, new_artifact_id, new_mcp_id, new_run_id, new_schedule_id, new_schedule_run_id,
                                    new_session_id, utcnow)
from pinboard.state.store import StateStore

log = logging.getLogger(__name__)

__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ApprovalRecord",
    "ArtifactRecord",
    "CHECKPOINT_TABLES",
    "McpServerRecord",
    "MemoryStore",
    "PostgresLedger",
    "PostgresStore",
    "RUN_STATUSES",
    "RunRecord",
    "ScheduleRecord",
    "ScheduleRunRecord",
    "SessionRecord",
    "SpareRecord",
    "State",
    "StateStore",
    "make_pool",
    "make_state",
    "new_approval_id",
    "new_artifact_id",
    "new_mcp_id",
    "new_run_id",
    "new_schedule_id",
    "new_schedule_run_id",
    "new_session_id",
    "repair_checkpoint_tables",
    "utcnow",
]


@dataclass
class State:
    store: StateStore
    checkpointer: Any
    close: Callable[[], None]
    ledger: Any = None  # EffectLedger shared by the connector tools


CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def repair_checkpoint_tables(pool, saver) -> list[str]:
    """LangGraph's ``setup()`` trusts its ``checkpoint_migrations`` marker and never looks at the tables. If a table was dropped (seen in
    production: ``checkpoints`` vanished while the marker stayed) every turn fails with 'relation does not exist'. Check, and if any are
    missing clear the marker and run the (idempotent) migrations again. Returns the tables that were missing; their old rows are gone."""
    with pool.connection() as conn:
        missing = [t for t in CHECKPOINT_TABLES if not conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (t,)).fetchone()["ok"]]
        if missing:
            log.warning("checkpoint tables missing (%s): rebuilding them; conversations that used them lose their saved agent state", ", ".join(missing))
            conn.execute("DELETE FROM checkpoint_migrations")
    if missing:
        saver.setup()
    return missing


def make_state(settings, vault) -> State:
    """Build the store + LangGraph checkpointer for the configured backend."""
    if settings.state_backend == "postgres":
        from langgraph.checkpoint.postgres import PostgresSaver

        pool = make_pool(vault.get("DATABASE_URL", required=True))
        store, saver = PostgresStore(pool), PostgresSaver(pool)
        store.setup()
        saver.setup()
        repair_checkpoint_tables(pool, saver)
        return State(store, saver, pool.close, PostgresLedger(pool))
    from langgraph.checkpoint.memory import MemorySaver

    from pinboard.agent.tools.effects import InMemoryLedger

    return State(MemoryStore(), MemorySaver(), lambda: None, InMemoryLedger())
