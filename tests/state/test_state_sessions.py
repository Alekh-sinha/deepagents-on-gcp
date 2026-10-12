"""Contract tests for sessions, turn leases (fencing), the event log and the effect ledger. MemoryStore and PostgresStore must agree."""
import threading
from datetime import timedelta

import pytest

from pinboard.agent.tools.effects import InMemoryLedger
from pinboard.engine.events import MAX_EVENT_BYTES
from pinboard.state import PostgresLedger, PostgresStore, SessionRecord, new_session_id, utcnow

from test_state import DSN, mkrun, store  # noqa: F401  (the `store` fixture runs every test on both backends)


def mksession(**kw):
    sid = kw.pop("id", new_session_id())
    return SessionRecord(**{"id": sid, "user_id": "owner", "thread_id": sid, **kw})


# ── sessions ─────────────────────────────────────────────────────────────

def test_session_roundtrip_update_and_listing(store):
    now = utcnow()
    a = mksession(title="first", last_active_at=now - timedelta(minutes=5))
    b = mksession(title="second", last_active_at=now)
    other = mksession(user_id="someone-else")
    for x in (a, b, other):
        store.create_session(x)
    assert store.get_session(a.id).title == "first" and store.get_session("nope") is None
    assert [x.id for x in store.list_sessions("owner")] == [b.id, a.id]  # newest activity first, only this user's
    upd = store.update_session(a.id, title="renamed", model="m2", sandbox_name="sb-1", sandbox_expires_at=now + timedelta(hours=1), last_active_at=now + timedelta(seconds=1))
    assert (upd.title, upd.model, upd.sandbox_name) == ("renamed", "m2", "sb-1") and store.list_sessions("owner")[0].id == a.id
    store.update_session(b.id, archived=True)
    assert [x.id for x in store.list_sessions("owner")] == [a.id] and len(store.list_sessions("owner", include_archived=True)) == 2
    with pytest.raises((ValueError, AttributeError)):
        store.update_session(a.id, user_id="hacker")
    assert "sandbox_name" not in upd.to_dict() and upd.to_dict()["sandbox_expires_at"]


def test_idle_sessions_with_a_sandbox_skip_busy_recent_and_sandboxless_ones(store):
    now = utcnow()
    old = now - timedelta(minutes=30)
    idle = mksession(sandbox_name="sb-idle", last_active_at=old)
    busy = mksession(sandbox_name="sb-busy", last_active_at=old)
    recent = mksession(sandbox_name="sb-recent", last_active_at=now)
    nobox = mksession(last_active_at=old)
    for x in (idle, busy, recent, nobox):
        store.create_session(x)
    store.create_run(mkrun(session_id=busy.id, status="awaiting_approval"))  # a parked turn may still need its sandbox
    assert [x.id for x in store.idle_sessions_with_sandbox(now - timedelta(minutes=20))] == [idle.id]


# ── leases ───────────────────────────────────────────────────────────────

def test_only_one_worker_can_hold_a_turn_and_an_expired_lease_can_be_taken_over(store):
    r = mkrun()
    store.create_run(r)
    t0 = utcnow()
    assert store.acquire_lease(r.id, "A", t0, 30) is True
    assert store.acquire_lease(r.id, "B", t0 + timedelta(seconds=10), 30) is False  # A's lease is still valid
    assert store.renew_lease(r.id, "B", t0, 30) is False and store.renew_lease(r.id, "A", t0 + timedelta(seconds=20), 30) is True
    assert store.acquire_lease(r.id, "B", t0 + timedelta(seconds=40), 30) is False  # renewed to t0+50
    assert store.acquire_lease(r.id, "B", t0 + timedelta(seconds=60), 30) is True   # A went quiet: B takes over
    assert store.renew_lease(r.id, "A", t0 + timedelta(seconds=61), 30) is False    # A has lost it
    assert store.release_lease(r.id, "A") is False and store.release_lease(r.id, "B") is True
    assert store.get_run(r.id).lease_id is None


def test_a_lease_needs_a_running_turn(store):
    for status in ("awaiting_approval", "completed", "interrupted", "cancelled"):
        r = mkrun(status=status)
        store.create_run(r)
        assert store.acquire_lease(r.id, "A", utcnow(), 30) is False, status
    assert store.acquire_lease("missing", "A", utcnow(), 30) is False


def test_lease_fields_stay_internal(store):
    r = mkrun(session_id="s1", model="m1")
    store.create_run(r)
    store.acquire_lease(r.id, "secret-lease", utcnow(), 30)
    d = store.get_run(r.id).to_dict()
    assert "lease_id" not in d and "lease_expires_at" not in d and "sandbox_name" not in d
    assert d["session_id"] == "s1" and d["model"] == "m1" and d["cancel_requested"] is False and d["heartbeat_at"]


# ── events and fencing ───────────────────────────────────────────────────

def test_events_get_gapless_increasing_sequence_numbers_and_can_be_read_from_a_position(store):
    r = mkrun()
    store.create_run(r)
    store.acquire_lease(r.id, "A", utcnow(), 30)
    seqs = [store.append_event(r.id, "A", "token", {"text": f"t{i}"}) for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5] and store.get_run(r.id).event_seq == 5
    assert [e["seq"] for e in store.list_events(r.id)] == [1, 2, 3, 4, 5]
    assert [e["payload"]["text"] for e in store.list_events(r.id, after_seq=3)] == ["t3", "t4"]
    assert len(store.list_events(r.id, limit=2)) == 2 and store.list_events(r.id, after_seq=99) == []
    assert store.append_event(r.id, None, "notice", {"x": 1}) == 6  # unfenced writer (the service itself)
    assert store.list_events("other-run") == []


def test_a_writer_that_lost_its_lease_cannot_write(store):
    r = mkrun()
    store.create_run(r)
    t0 = utcnow()
    store.acquire_lease(r.id, "A", t0, 30)
    assert store.append_event(r.id, "A", "token", {"text": "a1"}) == 1
    store.acquire_lease(r.id, "B", t0 + timedelta(seconds=60), 30)  # takes over after A's lease expired
    assert store.append_event(r.id, "A", "token", {"text": "stale"}) is None  # fenced
    assert store.append_event(r.id, "B", "token", {"text": "b1"}) == 2
    assert [e["payload"]["text"] for e in store.list_events(r.id)] == ["a1", "b1"]
    assert store.append_event("missing", "A", "token", {}) is None


def test_concurrent_writers_never_duplicate_or_skip_a_sequence_number(store):
    r = mkrun()
    store.create_run(r)
    store.acquire_lease(r.id, "A", utcnow(), 300)
    got, errors = [], []

    def writer(n):
        try:
            for i in range(25):
                got.append(store.append_event(r.id, "A", "token", {"w": n, "i": i}))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and sorted(got) == list(range(1, 201))
    assert [e["seq"] for e in store.list_events(r.id, limit=1000)] == list(range(1, 201))


def test_oversized_payloads_are_capped_on_write(store):
    r = mkrun()
    store.create_run(r)
    store.append_event(r.id, None, "tool_result", {"preview": "x" * 500_000, "name": "read_file"})
    ev = store.list_events(r.id)[0]["payload"]
    assert ev["_capped"] is True and ev["name"] == "read_file" and len(ev["preview"]) < MAX_EVENT_BYTES


def test_finished_message_replaces_its_token_batches(store):
    r = mkrun()
    store.create_run(r)
    for text, mid in (("Hel", "m1"), ("lo", "m1"), ("other", "m2")):
        store.append_event(r.id, None, "token", {"message_id": mid, "text": text})
    store.append_event(r.id, None, "tool_call", {"message_id": "m1"})  # not a token: must survive
    assert store.delete_token_events(r.id, "m1") == 2
    left = store.list_events(r.id)
    assert [(e["type"], e["payload"].get("message_id")) for e in left] == [("token", "m2"), ("tool_call", "m1")]
    assert store.delete_token_events(r.id, "m1") == 0


# ── stale turns, interruption, cancel ────────────────────────────────────

def test_a_turn_whose_worker_went_quiet_is_found_and_interrupted_exactly_once(store):
    t0 = utcnow()
    dead, alive, parked, orphan, fresh = mkrun(), mkrun(), mkrun(status="awaiting_approval"), mkrun(), mkrun()
    orphan.updated_at = t0 - timedelta(minutes=10)  # created long ago, no worker ever took it
    for r in (dead, alive, parked, orphan, fresh):
        store.create_run(r)
    store.acquire_lease(dead.id, "D", t0 - timedelta(minutes=5), 30)
    store.acquire_lease(alive.id, "L", t0, 30)
    stale = {r.id for r in store.stale_runs(t0 + timedelta(seconds=5))}
    assert stale == {dead.id, orphan.id}  # not the live one, not the parked one, not the brand-new one
    assert store.mark_interrupted(dead.id, t0 + timedelta(seconds=5)) is True
    assert store.mark_interrupted(dead.id, t0 + timedelta(seconds=5)) is False  # already handled
    assert store.mark_interrupted(alive.id, t0 + timedelta(seconds=5)) is False
    r = store.get_run(dead.id)
    assert r.status == "interrupted" and r.lease_id is None
    assert store.claim_run(dead.id, "interrupted", "running", t0) is True  # the resume path
    assert store.acquire_lease(dead.id, "NEW", t0, 30) is True


def test_cancel_is_a_flag_the_worker_reads(store):
    r = mkrun()
    store.create_run(r)
    assert store.get_run(r.id).cancel_requested is False
    assert store.request_cancel(r.id).cancel_requested is True and store.get_run(r.id).cancel_requested is True
    with pytest.raises(KeyError):
        store.request_cancel("missing")


def test_resume_count_and_model_are_updatable_but_the_lease_is_not(store):
    r = mkrun()
    store.create_run(r)
    assert store.update_run(r.id, resume_count=1, model="m2").resume_count == 1
    with pytest.raises((ValueError, AttributeError)):
        store.update_run(r.id, lease_id="forged")
    with pytest.raises((ValueError, AttributeError)):
        store.update_run(r.id, event_seq=99)


# ── the effect ledger (idempotency / quotas) ─────────────────────────────

@pytest.fixture
def ledger(store):
    if isinstance(store, PostgresStore):
        return PostgresLedger(store.pool)
    return InMemoryLedger()


def test_ledger_claims_once_releases_and_counts(ledger):
    assert ledger.claim("T:c1", "email") is True and ledger.claim("T:c1", "email") is False
    assert ledger.claim("T:c2", "email") is True and ledger.claim("T:c3", "github.commit") is True
    since = utcnow() - timedelta(hours=1)
    assert ledger.count_since("email", since) == 2 and ledger.count_since("github.commit", since) == 1
    assert ledger.count_since("email", utcnow() + timedelta(hours=1)) == 0
    ledger.release("T:c1")
    assert ledger.claim("T:c1", "email") is True
    ledger.release("never-claimed")  # harmless


def test_ledger_claim_is_atomic_across_threads(ledger):
    wins = []
    threads = [threading.Thread(target=lambda: wins.append(ledger.claim("T:race", "email"))) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1 and wins.count(False) == 19


# ── migration (Postgres only) ────────────────────────────────────────────

@pytest.mark.skipif(not DSN, reason="PINBOARD_TEST_DATABASE_URL not set")
def test_setup_upgrades_tables_created_by_an_older_version_and_is_repeatable():
    from pinboard.state import make_pool

    pool = make_pool(DSN)
    with pool.connection() as c:
        c.execute("DROP TABLE IF EXISTS pb_schedule_runs, pb_schedules, pb_mcp_servers, pb_spares, pb_artifacts, pb_events, pb_effects, pb_sessions, pb_audit, pb_approvals, pb_runs CASCADE")
        c.execute("CREATE TABLE pb_runs (id text PRIMARY KEY, thread_id text NOT NULL, user_id text NOT NULL, status text NOT NULL, source text NOT NULL DEFAULT 'user', "
                  "prompt text NOT NULL DEFAULT '', output text NOT NULL DEFAULT '', error text NOT NULL DEFAULT '', sandbox_name text, outputs jsonb NOT NULL DEFAULT '[]'::jsonb, "
                  "created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL)")
        c.execute("INSERT INTO pb_runs (id, thread_id, user_id, status, created_at, updated_at) VALUES ('old', 'old', 'owner', 'completed', now(), now())")
    s = PostgresStore(pool)
    s.setup()
    s.setup()
    old = s.get_run("old")
    assert old.status == "completed" and old.session_id is None and old.resume_count == 0 and old.cancel_requested is False and old.event_seq == 0
    assert s.append_event("old", None, "notice", {"ok": True}) == 1
    pool.close()


# ── artifacts ────────────────────────────────────────────────────────────

def mkartifact(**kw):
    from pinboard.state import ArtifactRecord, new_artifact_id

    base = dict(id=new_artifact_id(), user_id="owner", session_id="s1", run_id="r1", name="report.md", path="reports/report.md", size=10,
                content_type="text/markdown", sha256="a" * 64, blob_key="users/x/artifacts/s1/r1/reports/report.md")
    base.update(kw)
    return ArtifactRecord(**base)


def test_artifacts_are_listed_newest_first_per_user_and_session(store):
    now = utcnow()
    a = mkartifact(path="a.md", created_at=now - timedelta(minutes=2))
    b = mkartifact(path="b.md", created_at=now - timedelta(minutes=1))
    c = mkartifact(path="c.md", session_id="s2", created_at=now)
    other = mkartifact(path="x.md", user_id="someone-else", created_at=now)
    for x in (a, b, c, other):
        store.create_artifact(x)
    assert [x.path for x in store.list_artifacts("owner")] == ["c.md", "b.md", "a.md"]
    assert [x.path for x in store.list_artifacts("owner", "s1")] == ["b.md", "a.md"]
    assert len(store.list_artifacts("owner", limit=2)) == 2 and store.list_artifacts("nobody") == []
    assert store.get_artifact(a.id).blob_key == a.blob_key and store.get_artifact("missing") is None


def test_the_latest_version_of_a_path_is_found_per_session(store):
    now = utcnow()
    old = mkartifact(path="r.md", sha256="1" * 64, created_at=now - timedelta(hours=1))
    new = mkartifact(path="r.md", sha256="2" * 64, created_at=now)
    elsewhere = mkartifact(path="r.md", session_id="s2", sha256="3" * 64, created_at=now + timedelta(hours=1))
    for x in (old, new, elsewhere):
        store.create_artifact(x)
    assert store.latest_artifact("owner", "s1", "r.md").sha256 == "2" * 64
    assert store.latest_artifact("owner", "s2", "r.md").sha256 == "3" * 64
    assert store.latest_artifact("owner", "s1", "nope.md") is None and store.latest_artifact("other", "s1", "r.md") is None


def test_artifact_dicts_do_not_reveal_where_the_bytes_live(store):
    a = mkartifact()
    store.create_artifact(a)
    d = store.get_artifact(a.id).to_dict()
    assert "blob_key" not in d and d["name"] == "report.md" and d["created_at"]


def test_a_session_remembers_that_it_once_had_a_sandbox_even_after_the_name_is_cleared(store):
    sess = mksession()
    store.create_session(sess)
    assert store.get_session(sess.id).had_sandbox is False
    store.update_session(sess.id, sandbox_name="sb-1", sandbox_expires_at=utcnow(), had_sandbox=True)
    store.update_session(sess.id, sandbox_name=None, sandbox_expires_at=None)  # what the idle sweep does
    got = store.get_session(sess.id)
    assert got.sandbox_name is None and got.had_sandbox is True and "had_sandbox" not in got.to_dict()


# ── a checkpoint table that has gone missing (seen in production) ────────

@pytest.mark.skipif(not DSN, reason="PINBOARD_TEST_DATABASE_URL not set")
def test_a_dropped_checkpoint_table_is_rebuilt_at_start_up_instead_of_failing_every_turn():

    from langgraph.checkpoint.postgres import PostgresSaver

    from pinboard.state import make_pool, repair_checkpoint_tables

    pool = make_pool(DSN)
    with pool.connection() as c:
        c.execute("DROP TABLE IF EXISTS pb_schedule_runs, pb_schedules, pb_mcp_servers, pb_spares, pb_artifacts, pb_events, pb_effects, pb_sessions, pb_audit, pb_approvals, pb_runs, "
                  "checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations CASCADE")
    saver = PostgresSaver(pool)
    saver.setup()
    assert repair_checkpoint_tables(pool, saver) == []  # healthy: nothing to do, nothing touched
    cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    assert saver.get_tuple(cfg) is None
    with pool.connection() as c:
        c.execute("DROP TABLE checkpoints")  # exactly what happened in production: the marker says "migrated", the table is gone
    with pytest.raises(Exception, match="does not exist"):
        saver.get_tuple(cfg)
    PostgresSaver(pool).setup()  # plain setup() does NOT notice: this is the bug
    with pytest.raises(Exception, match="does not exist"):
        saver.get_tuple(cfg)
    assert repair_checkpoint_tables(pool, saver) == ["checkpoints"]
    assert saver.get_tuple(cfg) is None  # works again
    assert repair_checkpoint_tables(pool, saver) == []
    pool.close()


@pytest.mark.skipif(not DSN, reason="PINBOARD_TEST_DATABASE_URL not set")
def test_starting_the_service_repairs_a_dropped_checkpoint_table(monkeypatch):
    from helpers import make_settings
    from pinboard.gcp.vault import Vault
    from pinboard.state import make_pool, make_state

    monkeypatch.setenv("DATABASE_URL", DSN)
    settings = make_settings("/tmp", state_backend="postgres")
    make_state(settings, Vault("env")).close()
    pool = make_pool(DSN)
    with pool.connection() as c:
        c.execute("DROP TABLE checkpoint_writes")
    pool.close()
    state = make_state(settings, Vault("env"))  # a restart
    try:
        state.checkpointer.put_writes({"configurable": {"thread_id": "t", "checkpoint_ns": "", "checkpoint_id": "c"}}, [("ch", "v")], "task")  # uses the rebuilt table
    finally:
        state.close()
