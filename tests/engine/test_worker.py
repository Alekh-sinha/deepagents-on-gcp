import threading
from datetime import timedelta

from pinboard.state import MemoryStore, utcnow
from pinboard.engine.worker import InlineExecutor, LeaseKeeper, ThreadExecutor, TurnContext

from test_state import mkrun
from turn_harness import wait_until


def test_inline_executor_runs_the_job_before_returning():
    done = []
    ex = InlineExecutor()
    assert ex.has_capacity() and ex.try_submit(lambda: done.append(1)) is True
    ex.submit(lambda: done.append(2))
    assert done == [1, 2]


def test_thread_executor_refuses_new_jobs_at_capacity_but_always_runs_admitted_ones():
    ex = ThreadExecutor(2)
    hold, started = threading.Event(), []
    job = lambda: (started.append(1), hold.wait(5))  # noqa: E731
    assert ex.try_submit(job) and ex.try_submit(job)
    wait_until(lambda: len(started) == 2)
    assert ex.active == 2 and not ex.has_capacity() and ex.try_submit(lambda: None) is False
    ran = threading.Event()
    ex.submit(ran.set)  # an already-admitted turn continuing is never refused
    assert ran.wait(5)
    hold.set()
    ex.join(5)
    wait_until(lambda: ex.active == 0)
    assert ex.has_capacity() and ex.try_submit(lambda: None)


def test_thread_executor_survives_a_crashing_job_and_releases_its_slot():
    ex = ThreadExecutor(1)
    assert ex.try_submit(lambda: 1 / 0)
    ex.join(5)
    wait_until(lambda: ex.active == 0)
    ok = threading.Event()
    assert ex.try_submit(ok.set) and ok.wait(5)


def test_the_lease_keeper_renews_on_a_heartbeat_and_notices_when_the_turn_is_taken_over():
    store = MemoryStore()
    r = mkrun()
    store.create_run(r)
    t0 = utcnow()
    assert store.acquire_lease(r.id, "A", t0, 30)
    clock = [t0]
    keeper = LeaseKeeper(store, r.id, "A", 30, 0.01, lambda: clock[0]).start()
    clock[0] = t0 + timedelta(seconds=25)
    wait_until(lambda: store.get_run(r.id).lease_expires_at > t0 + timedelta(seconds=40), what="a renewal")
    assert not keeper.lost.is_set()
    store.acquire_lease(r.id, "B", t0 + timedelta(minutes=10), 30)  # someone else takes the turn
    wait_until(keeper.lost.is_set, what="the keeper to notice")
    keeper.stop()


def test_a_keeper_without_a_thread_renews_on_demand():
    store = MemoryStore()
    r = mkrun()
    store.create_run(r)
    store.acquire_lease(r.id, "A", utcnow(), 30)
    k = LeaseKeeper(store, r.id, "A", 30, 15, utcnow, start_thread=False)
    assert k.renew() is True and not k.lost.is_set()
    store.release_lease(r.id, "A")
    assert k.renew() is False and k.lost.is_set()
    k.stop()


def test_the_turn_context_reports_when_it_has_been_fenced_out():
    store = MemoryStore()
    r = mkrun()
    store.create_run(r)
    store.acquire_lease(r.id, "A", utcnow(), 30)
    ctx = TurnContext(store, r.id, "A", utcnow)
    assert ctx.emit("notice", {"x": 1}) is True and not ctx.lost
    store.acquire_lease(r.id, "B", utcnow() + timedelta(minutes=5), 30)
    assert ctx.emit("notice", {"x": 2}) is False and ctx.lost
    assert [e["payload"]["x"] for e in store.list_events(r.id)] == [1]
