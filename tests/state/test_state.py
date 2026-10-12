"""Contract tests: MemoryStore and PostgresStore must behave identically.

The Postgres variant runs when PINBOARD_TEST_DATABASE_URL is set, e.g.
    docker run -d -p 5433:5432 -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=pinboard_test postgres:16
    export PINBOARD_TEST_DATABASE_URL=postgresql://postgres@localhost:5433/pinboard_test
"""
import os
import threading
from datetime import timedelta

import pytest

from pinboard.state import ApprovalRecord, MemoryStore, PostgresStore, RunRecord, make_pool, new_approval_id, new_run_id, utcnow

DSN = os.environ.get("PINBOARD_TEST_DATABASE_URL")
PARAMS = ["memory"] + (["postgres"] if DSN else [pytest.param("postgres", marks=pytest.mark.skip(reason="PINBOARD_TEST_DATABASE_URL not set"))])


@pytest.fixture(params=PARAMS)
def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return
    pool = make_pool(DSN)
    with pool.connection() as c:
        c.execute("DROP TABLE IF EXISTS pb_schedule_runs, pb_schedules, pb_mcp_servers, pb_spares, pb_artifacts, pb_events, pb_effects, pb_sessions, pb_audit, pb_approvals, pb_runs CASCADE")
    s = PostgresStore(pool)
    s.setup()
    yield s
    pool.close()


def mkrun(**kw):
    rid = kw.pop("id", new_run_id())
    base = dict(id=rid, thread_id=rid, user_id="owner", status="running", prompt="p")
    base.update(kw)
    return RunRecord(**base)


def mkapproval(run_id, minutes=10, **kw):
    base = dict(id=new_approval_id(), run_id=run_id, user_id="owner",
                action_requests=[{"name": "execute", "args": {"command": "ls"}}], expires_at=utcnow() + timedelta(minutes=minutes))
    base.update(kw)
    return ApprovalRecord(**base)


def test_run_roundtrip_and_update(store):
    r = mkrun(outputs=["a.md"])
    store.create_run(r)
    got = store.get_run(r.id)
    assert got.status == "running" and got.outputs == ["a.md"] and got.thread_id == r.id
    upd = store.update_run(r.id, status="completed", output="done", outputs=["a.md", "b.md"], sandbox_name=None)
    assert upd.status == "completed" and upd.outputs == ["a.md", "b.md"] and upd.updated_at >= got.updated_at
    assert store.get_run("missing") is None


def test_update_rejects_unknown_fields(store):
    r = mkrun()
    store.create_run(r)
    with pytest.raises((ValueError, AttributeError)):
        store.update_run(r.id, user_id="attacker")


def test_claim_run_is_exclusive(store):
    r = mkrun(status="awaiting_approval")
    store.create_run(r)
    assert store.claim_run(r.id, "awaiting_approval", "running", utcnow()) is True
    assert store.claim_run(r.id, "awaiting_approval", "running", utcnow()) is False
    assert store.get_run(r.id).status == "running"


def test_active_run_for_thread(store):
    r = mkrun(thread_id="t-1")
    store.create_run(r)
    assert store.active_run_for_thread("t-1").id == r.id
    store.update_run(r.id, status="completed")
    assert store.active_run_for_thread("t-1") is None


def test_approval_decision_is_exactly_once(store):
    r = mkrun(status="awaiting_approval")
    store.create_run(r)
    a = mkapproval(r.id)
    store.create_approval(a)
    first = store.decide_approval(a.id, "approved", {"decisions": [{"type": "approve"}]}, "owner", utcnow())
    second = store.decide_approval(a.id, "rejected", {"decisions": [{"type": "reject"}]}, "owner", utcnow())
    assert first is not None and first.status == "approved" and first.decided_by == "owner"
    assert second is None  # a repeat / double click can never decide again
    assert store.get_approval(a.id).status == "approved"


def test_concurrent_decisions_only_one_wins(store):
    r = mkrun(status="awaiting_approval")
    store.create_run(r)
    a = mkapproval(r.id)
    store.create_approval(a)
    results, barrier = [], threading.Barrier(6)

    def go(i):
        barrier.wait()
        results.append(store.decide_approval(a.id, "approved", {"n": i}, "owner", utcnow()))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(x is not None for x in results) == 1


def test_expired_approval_cannot_be_decided_and_gets_expired(store):
    r = mkrun(status="awaiting_approval")
    store.create_run(r)
    a = mkapproval(r.id, minutes=-1)  # already past its deadline
    store.create_approval(a)
    assert store.decide_approval(a.id, "approved", {}, "owner", utcnow()) is None
    expired = store.expire_approvals(utcnow())
    assert [x.id for x in expired] == [a.id] and store.get_approval(a.id).status == "expired"
    assert store.expire_approvals(utcnow()) == []  # idempotent


def test_list_approvals_filters_by_user_and_status(store):
    r = mkrun()
    store.create_run(r)
    mine, theirs, done = mkapproval(r.id), mkapproval(r.id, user_id="someone-else"), mkapproval(r.id)
    for a in (mine, theirs, done):
        store.create_approval(a)
    store.decide_approval(done.id, "rejected", {}, "owner", utcnow())
    pend = store.list_approvals("owner", status="pending")
    assert [a.id for a in pend] == [mine.id]
    assert {a.id for a in store.list_approvals("owner")} == {mine.id, done.id}


def test_action_requests_survive_roundtrip_exactly(store):
    r = mkrun()
    store.create_run(r)
    reqs = [{"name": "execute", "args": {"command": "echo 'it''s' && ls", "n": 3, "nested": {"x": [1, 2]}}, "description": "d"}]
    a = mkapproval(r.id, action_requests=reqs)
    store.create_approval(a)
    assert store.get_approval(a.id).action_requests == reqs


def test_audit_trail_in_order(store):
    r = mkrun()
    store.create_run(r)
    store.add_audit("owner", r.id, "run.started", {"a": 1})
    store.add_audit("owner", r.id, "command.blocked", {"cmd": "sudo id"})
    store.add_audit("owner", "other", "run.started", {})
    ev = store.list_audit(r.id)
    assert [e["event"] for e in ev] == ["run.started", "command.blocked"] and ev[1]["detail"] == {"cmd": "sudo id"}


def test_run_to_dict_hides_sandbox_name():
    d = mkrun(sandbox_name="projects/x/sandboxEnvironments/1").to_dict()
    assert "sandbox_name" not in d and isinstance(d["created_at"], str)
