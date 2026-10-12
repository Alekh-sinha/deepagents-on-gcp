"""The warm pool: one spare sandbox is made when the user starts typing, taken by the first turn that needs a sandbox, replaced within a
daily cap, and swept when unused. The REAL creation / claim / adoption code runs against the fake platform."""
import dataclasses
import itertools
import types
from datetime import timedelta

import pytest

from pinboard.gcp.sessions import REPLACED_NOTE
from pinboard.core.policy import WarmSandbox, parse_policy
from pinboard.engine.errors import NotFound

from helpers import say
from test_policy import BASE
from test_turns_sessions import human_texts, touch
from turn_harness import BACKENDS, Hub, make_policy

ME = "me@example.com"


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


def warm_policy(mode="on_activity", **warm):
    return dataclasses.replace(make_policy(), warm_sandbox=WarmSandbox(mode, **warm))


def hub(backend, tmp_path, monkeypatch, scripts, *, policy=None, **over):
    h = Hub(backend, tmp_path, monkeypatch, scripts, policy=policy or warm_policy())
    h.new_process(**over)
    return h


def spare(h):
    return h.svc.store.current_spare(h.clock() - timedelta(minutes=10))


# ── the whole flow ───────────────────────────────────────────────────────

def test_typing_makes_a_spare_the_first_tool_use_takes_it_and_another_is_made(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[None]])
    h.scripts[0] = [touch(h, "sb-1"), say("saved")]
    s = h.svc.create_session(user_id=ME)
    assert h.svc.warm_sandbox(s.id, user_id=ME) == {"status": "warming"} and h.farm.created == ["sb-1"]
    assert h.svc.warm_sandbox(s.id, user_id=ME) == {"status": "ready"} and h.farm.created == ["sb-1"]  # repeating is free

    run = h.svc.send_message(s.id, "make a file", user_id=ME)
    assert run.status == "completed" and (h.sbdir("sb-1") / "f.txt").exists()  # the work happened in the spare
    session = h.svc.store.get_session(s.id)
    assert session.sandbox_name == "sb-1" and session.had_sandbox and session.sandbox_expires_at
    assert h.farm.created == ["sb-1", "sb-2"]  # the claim started a replacement
    nxt = spare(h)
    assert nxt.name == "sb-2" and nxt.status == "ready"
    assert not any(REPLACED_NOTE in t for t in human_texts(h.models[-1]))  # a spare is a first sandbox, not a replacement
    h.close()


def test_a_chat_only_turn_leaves_the_spare_alone(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("hello")]])
    s = h.svc.create_session(user_id=ME)
    h.svc.warm_sandbox(s.id, user_id=ME)
    assert h.svc.send_message(s.id, "hi", user_id=ME).status == "completed"
    assert h.farm.created == ["sb-1"] and spare(h).status == "ready" and h.svc.store.get_session(s.id).sandbox_name is None
    h.close()


def test_a_session_that_already_has_a_sandbox_does_not_warm_another(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[None]])
    h.scripts[0] = [touch(h, "sb-1"), say("ok")]
    s = h.svc.create_session(user_id=ME)
    h.svc.send_message(s.id, "make a file", user_id=ME)  # no spare existed: the turn made its own sandbox
    assert h.farm.created == ["sb-1"] and h.svc.warm_sandbox(s.id, user_id=ME) == {"status": "have_sandbox"} and h.farm.created == ["sb-1"]
    h.close()


def test_mode_off_never_creates_or_claims_anything(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[None]], policy=warm_policy("off"))
    h.scripts[0] = [touch(h, "sb-1"), say("ok")]
    s = h.svc.create_session(user_id=ME)
    assert h.svc.warm_sandbox(s.id, user_id=ME) == {"status": "off"} and h.farm.created == []
    h.svc.send_message(s.id, "make a file", user_id=ME)
    assert h.farm.created == ["sb-1"] and spare(h) is None  # its own sandbox, created lazily as before
    h.close()


def test_someone_elses_session_cannot_be_used_to_trigger_warming(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]])
    s = h.svc.create_session(user_id=ME)
    with pytest.raises(NotFound):
        h.svc.warm_sandbox(s.id, user_id="other@example.com")
    assert h.farm.created == []
    h.close()


# ── a spare that is still being made ─────────────────────────────────────

def test_a_turn_that_needs_a_sandbox_waits_for_the_spare_being_made(backend, tmp_path, monkeypatch):
    pending = []
    h = hub(backend, tmp_path, monkeypatch, [[None]], spare_runner=pending.append, sleep=lambda s: pending.pop(0)() if pending else None)
    h.scripts[0] = [touch(h, "sb-1"), say("saved")]
    s = h.svc.create_session(user_id=ME)
    assert h.svc.warm_sandbox(s.id, user_id=ME) == {"status": "warming"} and h.farm.created == [] and spare(h).status == "creating"
    assert h.svc.warm_sandbox(s.id, user_id=ME) == {"status": "warming"}  # still being made: nothing more is started
    run = h.svc.send_message(s.id, "make a file", user_id=ME)  # the tool needs a sandbox now: it waits for the one being made
    assert run.status == "completed" and h.farm.created == ["sb-1"] and (h.sbdir("sb-1") / "f.txt").exists()
    h.close()


def test_a_turn_stops_waiting_after_a_while_and_makes_its_own_sandbox(backend, tmp_path, monkeypatch):
    clock = itertools.count(0, 100)  # every look at the clock is 100 s later: the 90 s wait is over at the first check
    h = hub(backend, tmp_path, monkeypatch, [[None]], spare_runner=lambda fn: None, monotonic=lambda: next(clock))
    h.scripts[0] = [touch(h, "sb-1"), say("saved")]
    s = h.svc.create_session(user_id=ME)
    h.svc.warm_sandbox(s.id, user_id=ME)
    run = h.svc.send_message(s.id, "make a file", user_id=ME)
    assert run.status == "completed" and h.farm.created == ["sb-1"]  # created by the turn itself
    assert h.svc.store.get_session(s.id).sandbox_name == "sb-1"
    h.close()


# ── limits and clean-up ──────────────────────────────────────────────────

def test_the_daily_cap_stops_new_spares(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[None], [say("x")]], policy=warm_policy(max_spares_per_day=1))
    h.scripts[0] = [touch(h, "sb-1"), say("ok")]
    s = h.svc.create_session(user_id=ME)
    h.svc.warm_sandbox(s.id, user_id=ME)
    h.svc.send_message(s.id, "make a file", user_id=ME)
    assert h.farm.created == ["sb-1"] and spare(h) is None  # claimed, and today's one spare is used up: no replacement
    other = h.svc.create_session(user_id=ME)
    assert h.svc.warm_sandbox(other.id, user_id=ME) == {"status": "limit_reached"} and h.farm.created == ["sb-1"]
    h.clock.advance(hours=25)
    assert h.svc.warm_sandbox(other.id, user_id=ME) == {"status": "warming"} and h.farm.created == ["sb-1", "sb-2"]  # a new day
    h.close()


def test_an_unused_spare_is_deleted_after_its_time(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]], policy=warm_policy(spare_max_minutes=15))
    s = h.svc.create_session(user_id=ME)
    h.svc.warm_sandbox(s.id, user_id=ME)
    h.clock.advance(minutes=14)
    assert h.svc.maintenance()["spares_deleted"] == 0 and spare(h) is not None
    h.clock.advance(minutes=2)
    assert h.svc.maintenance()["spares_deleted"] == 1 and h.farm.deleted == ["sb-1"] and spare(h) is None
    h.close()


def test_a_creation_that_never_finishes_is_given_up_and_a_late_finish_does_not_leak_a_sandbox(backend, tmp_path, monkeypatch):
    pending = []
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]], spare_runner=pending.append)
    s = h.svc.create_session(user_id=ME)
    h.svc.warm_sandbox(s.id, user_id=ME)
    h.clock.advance(minutes=11)
    assert h.svc.maintenance()["spares_deleted"] == 1 and spare(h) is None  # the stuck row is cleared
    pending.pop(0)()  # ...and now the slow creation finally completes
    assert h.farm.created == ["sb-1"] and h.farm.deleted == ["sb-1"]  # nobody owns that sandbox any more, so it is deleted at once
    h.close()


def test_a_failed_creation_is_forgotten_and_tried_again_next_time(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]])
    s = h.svc.create_session(user_id=ME)
    h.farm.fail_create = RuntimeError("quota exceeded")
    h.svc.warm_sandbox(s.id, user_id=ME)
    assert spare(h) is None and h.farm.created == []
    h.farm.fail_create = None
    assert h.svc.warm_sandbox(s.id, user_id=ME) == {"status": "warming"} and h.farm.created == ["sb-1"]
    h.close()


def test_mode_always_keeps_one_ready_from_the_maintenance_tick(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]], policy=warm_policy("always"))
    assert spare(h) is None
    h.svc.maintenance()
    assert spare(h).status == "ready" and h.farm.created == ["sb-1"]
    h.svc.maintenance()
    assert h.farm.created == ["sb-1"]  # one is enough
    h.close()


# ── a spare that cannot be used ──────────────────────────────────────────

def test_a_spare_that_died_is_dropped_and_the_turn_makes_a_new_sandbox(backend, tmp_path, monkeypatch):
    fake_time = types.SimpleNamespace(monotonic=itertools.count(0, 20).__next__, sleep=lambda s: None)
    monkeypatch.setattr("pinboard.gcp.sandbox.time", fake_time)  # the 30 s readiness wait must not take 30 real seconds
    h = hub(backend, tmp_path, monkeypatch, [[None]])
    h.scripts[0] = [touch(h, "sb-2"), say("saved")]
    s = h.svc.create_session(user_id=ME)
    h.svc.warm_sandbox(s.id, user_id=ME)
    h.farm.alive.discard("sb-1")  # the platform lost the spare
    run = h.svc.send_message(s.id, "make a file", user_id=ME)
    assert run.status == "completed" and "sb-1" in h.farm.deleted  # the dead spare was deleted...
    assert h.svc.store.get_session(s.id).sandbox_name != "sb-1"  # ...and the session got a sandbox of its own
    h.close()


def test_a_spare_about_to_expire_is_not_handed_out(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[None]], policy=warm_policy(spare_max_minutes=45))
    h.scripts[0] = [touch(h, "sb-2"), say("saved")]
    s = h.svc.create_session(user_id=ME)
    h.svc.warm_sandbox(s.id, user_id=ME)
    h.clock.advance(minutes=35)  # the sandbox's hour is more than half gone
    run = h.svc.send_message(s.id, "make a file", user_id=ME)
    assert run.status == "completed" and h.svc.store.get_session(s.id).sandbox_name == "sb-2"  # it made its own; sb-1 was not used
    h.close()


# ── policy ───────────────────────────────────────────────────────────────

def test_the_policy_block_is_validated_and_defaults_to_off():
    import copy

    d = copy.deepcopy(BASE)
    assert parse_policy(d).warm_sandbox == WarmSandbox("off", 15, 10)
    d["warm_sandbox"] = {"mode": "on_activity", "spare_max_minutes": 20, "max_spares_per_day": 3}
    assert parse_policy(d).warm_sandbox == WarmSandbox("on_activity", 20, 3)
    for bad, msg in (({"mode": "sometimes"}, "mode"), ({"spare_max_minutes": 0}, "spare_max_minutes"), ({"spare_max_minutes": 60}, "at most 45"),
                     ({"max_spares_per_day": -1}, "max_spares_per_day"), ({"max_spares_per_day": True}, "max_spares_per_day")):
        d["warm_sandbox"] = {"mode": "on_activity", **bad}
        with pytest.raises(Exception, match=msg):
            parse_policy(d)


def test_old_claimed_rows_are_cleaned_up_by_maintenance(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[None]], policy=warm_policy(max_spares_per_day=1))
    h.scripts[0] = [touch(h, "sb-1"), say("ok")]
    s = h.svc.create_session(user_id=ME)
    h.svc.warm_sandbox(s.id, user_id=ME)
    h.svc.send_message(s.id, "make a file", user_id=ME)  # claimed: the row stays for a while (it counts toward today's cap)
    assert h.svc.warm_sandbox(h.svc.create_session(user_id=ME).id, user_id=ME) == {"status": "limit_reached"}
    h.clock.advance(days=3)
    h.svc.maintenance()
    assert h.svc.store.purge_claimed_spares(h.clock() - timedelta(days=2)) == 0  # maintenance already removed the old claimed row
    assert h.svc.warm_sandbox(h.svc.create_session(user_id=ME).id, user_id=ME) == {"status": "warming"}  # and a new day began
    h.close()
