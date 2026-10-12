"""The spare-sandbox table: one active spare at a time, an atomic claim, a daily creation cap, sweeping. Memory and Postgres must agree."""
import threading
from datetime import timedelta

from pinboard.state import utcnow

from test_state import store  # noqa: F401  (the `store` fixture runs every test on both backends)

HOUR = timedelta(hours=1)


def begin(store, sid, now=None, *, cap=10):
    now = now or utcnow()
    return store.begin_spare(sid, now, now - timedelta(minutes=10), now - timedelta(hours=24), cap)


def ready(store, sid, name, now=None, ttl=HOUR):
    now = now or utcnow()
    assert begin(store, sid, now) and store.finish_spare(sid, name, now, now + ttl)


def test_only_one_spare_is_active_at_a_time(store):
    now = utcnow()
    assert begin(store, "a", now) is True
    assert begin(store, "b", now) is False  # one is being made
    assert store.finish_spare("a", "sb-1", now, now + HOUR) is True
    assert begin(store, "c", now) is False  # one is ready
    cur = store.current_spare(now - timedelta(minutes=10))
    assert (cur.id, cur.status, cur.name) == ("a", "ready", "sb-1")


def test_two_threads_racing_to_start_a_spare_produce_exactly_one(store):
    wins, now = [], utcnow()
    threads = [threading.Thread(target=lambda i=i: wins.append(begin(store, f"s{i}", now))) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


def test_a_stale_creation_is_replaced_and_a_late_finish_is_refused(store):
    t0 = utcnow() - timedelta(minutes=30)
    assert begin(store, "old", t0)
    assert store.current_spare(utcnow() - timedelta(minutes=10)) is None  # too old to count as "being made"
    assert begin(store, "new", utcnow()) is True  # the stale row is cleared and a fresh creation starts
    assert store.finish_spare("old", "sb-9", utcnow(), utcnow() + HOUR) is False  # its row is gone: the caller must delete that sandbox


def test_the_daily_cap_counts_creations_in_the_last_24_hours_including_claimed_ones(store):
    now = utcnow()
    for i in range(3):
        assert begin(store, f"s{i}", now, cap=3)
        store.finish_spare(f"s{i}", f"sb-{i}", now, now + HOUR)
        assert store.claim_spare("sess", now, 60) is not None  # a claimed row still counts
    assert begin(store, "s3", now, cap=3) is False
    assert begin(store, "s3", now, cap=4) is True


def test_creations_older_than_a_day_do_not_count_against_the_cap(store):
    now = utcnow()
    ready(store, "old", "sb-0", now - timedelta(days=2))
    store.claim_spare("sess", now - timedelta(days=2), 0)
    assert begin(store, "today", now, cap=1) is True  # yesterday's spare is not today's budget
    store.delete_spare("today")
    assert begin(store, "again", now, cap=1) is True  # a failed (deleted) creation frees its place: the cap limits spares kept, bounded by the sweep


def test_a_claim_takes_a_ready_spare_exactly_once(store):
    now = utcnow()
    ready(store, "a", "sb-1", now)
    won = []
    threads = [threading.Thread(target=lambda i=i: won.append(store.claim_spare(f"sess{i}", now, 60))) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    got = [w for w in won if w is not None]
    assert len(got) == 1 and got[0].name == "sb-1" and got[0].status == "claimed" and got[0].claimed_by.startswith("sess")
    assert store.current_spare(now - timedelta(minutes=10)) is None  # nothing active any more: a new spare may be made


def test_a_spare_with_too_little_life_left_is_not_handed_out(store):
    now = utcnow()
    ready(store, "a", "sb-1", now, ttl=timedelta(minutes=20))
    assert store.claim_spare("s", now, 30 * 60) is None  # needs 30 minutes left
    assert store.claim_spare("s", now, 10 * 60).name == "sb-1"


def test_a_spare_still_being_made_cannot_be_claimed(store):
    now = utcnow()
    assert begin(store, "a", now)
    assert store.claim_spare("s", now, 0) is None


def test_the_sweep_finds_old_unclaimed_expired_and_stuck_spares_but_not_claimed_ones(store):
    now = utcnow()
    ready(store, "a", "sb-1", now - timedelta(minutes=20))  # ready 20 minutes ago
    found = store.spares_to_sweep(now, now - timedelta(minutes=15), now - timedelta(minutes=10))
    assert [(x.id, x.name) for x in found] == [("a", "sb-1")]
    assert store.spares_to_sweep(now, now - timedelta(minutes=30), now - timedelta(minutes=10)) == []  # not old enough yet
    assert [x.id for x in store.spares_to_sweep(now + timedelta(hours=2), now - timedelta(days=1), now - timedelta(minutes=10))] == ["a"]  # expired
    store.claim_spare("s", now, 0)
    assert store.spares_to_sweep(now + timedelta(hours=5), now, now) == []  # a claimed spare belongs to its session now
    assert begin(store, "stuck", now - timedelta(minutes=30))
    assert [x.id for x in store.spares_to_sweep(now, now - timedelta(minutes=15), now - timedelta(minutes=10))] == ["stuck"]


def test_old_claimed_rows_are_purged_and_recent_ones_kept(store):
    now = utcnow()
    ready(store, "a", "sb-1", now - timedelta(days=3))
    store.claim_spare("s", now - timedelta(days=3), 0)
    ready(store, "b", "sb-2", now)
    store.claim_spare("s", now, 0)
    assert store.purge_claimed_spares(now - timedelta(days=2)) == 1
    assert store.purge_claimed_spares(now - timedelta(days=2)) == 0
