"""Background workers for real (threads): cancellation, a worker that is replaced while still alive, capacity, lost workers, following a live turn."""
import threading

import pytest

from pinboard.engine.errors import Busy, Conflict
from pinboard.engine.worker import ThreadExecutor

from helpers import call, say
from turn_harness import Gate, Hub, events_of, kinds, wait_until


@pytest.fixture(autouse=True)
def _fast_cancel_polling(monkeypatch):
    monkeypatch.setattr("pinboard.engine.service._CANCEL_POLL_SECONDS", 0)


def threaded(tmp_path, monkeypatch, scripts, gate, *, capacity=4, **kw):
    h = Hub("memory", tmp_path, monkeypatch, scripts, tools=[gate.tool()], executor=ThreadExecutor(capacity), **kw)
    h.new_process()
    return h


def status(h, run_id):
    return h.svc.store.get_run(run_id).status


def test_a_new_turn_returns_at_once_and_finishes_in_the_background(tmp_path, monkeypatch):
    gate = Gate()
    h = threaded(tmp_path, monkeypatch, [[call("block", {}), say("finished")]], gate)
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "go")
    assert run.status == "running"  # send_message did not wait for the work
    assert gate.entered.wait(5)
    assert h.svc.store.get_run(run.id).lease_id  # a worker owns it
    gate.release()
    wait_until(lambda: status(h, run.id) == "completed", what="the turn to finish")
    assert h.svc.store.get_run(run.id).output == "finished" and kinds(events_of(h.svc, run.id))[-1] == "turn_done"
    h.close()


def test_cancelling_a_running_turn_stops_it_at_the_next_step_and_frees_the_session(tmp_path, monkeypatch):
    gate = Gate()
    h = threaded(tmp_path, monkeypatch, [[call("block", {}), say("this must never be said")], [say("fresh start")]], gate)
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "go")
    assert gate.entered.wait(5)
    assert h.svc.cancel_turn(run.id).cancel_requested is True and status(h, run.id) == "running"  # a command in flight is not killed mid-way
    gate.release()
    wait_until(lambda: status(h, run.id) == "cancelled", what="the cancel to take effect")
    evs = events_of(h.svc, run.id)
    assert kinds(evs)[-1] == "turn_done" and evs[-1]["payload"]["status"] == "cancelled" and "assistant_message" not in kinds(evs)
    assert h.farm.deleted == [] and h.svc.store.get_run(run.id).lease_id is None
    with pytest.raises(Conflict):
        h.svc.cancel_turn(run.id)  # already over
    nxt = h.svc.send_message(s.id, "start over")  # the session is usable again; the dangling tool call from the cancelled turn is repaired
    wait_until(lambda: status(h, nxt.id) == "completed", what="the next turn")
    assert h.svc.store.get_run(nxt.id).output == "fresh start"
    # the new turn's log tells only the new turn's story: the cut-off call of the cancelled turn is not announced again,
    # and there is no talk of a replaced sandbox (this session never had one)
    assert kinds(events_of(h.svc, nxt.id)) == ["user_message", "turn_started", "assistant_message", "turn_done"]
    h.close()


def test_cancelling_a_parked_turn_rejects_its_approval_and_stops_it_now(tmp_path, monkeypatch):
    h = Hub("memory", tmp_path, monkeypatch, [[call("execute", {"command": "echo x"}), say("x")]], approval_mode="execute")
    h.new_process()
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "go")
    appr = h.svc.list_approvals(status="pending")[0]
    out = h.svc.cancel_turn(run.id)
    assert out.status == "cancelled" and h.svc.store.get_approval(appr.id).status == "rejected"
    assert kinds(events_of(h.svc, run.id))[-1] == "turn_done"
    with pytest.raises(Conflict):
        h.svc.decide(appr.id, "approve")  # the approval is gone
    h.close()


def test_a_worker_that_was_replaced_while_still_alive_can_no_longer_write(tmp_path, monkeypatch):
    gate = Gate()
    h = threaded(tmp_path, monkeypatch, [[call("block", {}), say("stale ending")]], gate)
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "go")
    assert gate.entered.wait(5)
    a_lease = h.svc.store.get_run(run.id).lease_id
    h.clock.advance(minutes=10)  # worker A went quiet: its lease is long expired
    assert h.svc.store.acquire_lease(run.id, "worker-B", h.clock(), 45) is True  # B takes the turn over
    before = len(events_of(h.svc, run.id))
    gate.release()  # A wakes up
    wait_until(lambda: h.executor.active == 0, what="worker A to stop")
    after = events_of(h.svc, run.id)
    assert len(after) == before and "turn_done" not in kinds(after)  # A wrote nothing
    r = h.svc.store.get_run(run.id)
    assert r.status == "running" and r.lease_id == "worker-B" != a_lease  # and did not finish or fail the turn that is now B's
    assert h.farm.deleted == []  # nor delete the sandbox
    h.close()


def test_a_lost_worker_is_found_interrupted_and_resumed_once_from_the_checkpoint(tmp_path, monkeypatch):
    gate = Gate(first_call_only=True)  # the first run of the tool hangs forever (a stuck worker); the replay returns at once
    h = threaded(tmp_path, monkeypatch, [[call("block", {}), say("never")], [say("recovered")]], gate)
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "go")
    assert gate.entered.wait(5)
    h.clock.advance(minutes=5)  # the lease (no heartbeat in this test) has expired
    report = h.svc.maintenance()
    assert report["turns_resumed"] == 1 and report["turns_interrupted"] == 0
    wait_until(lambda: status(h, run.id) == "completed", what="the resumed turn")
    r = h.svc.store.get_run(run.id)
    assert r.output == "recovered" and r.resume_count == 1
    evs = events_of(h.svc, run.id)
    assert "turn_error" in kinds(evs) and any(e["payload"].get("text", "").startswith("Resuming") for e in evs if e["type"] == "notice")
    assert gate.calls == 2  # the step that was in flight was repeated, as documented
    gate.release()  # the zombie wakes up late ...
    wait_until(lambda: h.executor.active == 0, what="the zombie to stop")
    final = events_of(h.svc, run.id)
    assert kinds(final).count("turn_done") == 1 and h.svc.store.get_run(run.id).status == "completed"  # ... and changes nothing
    h.close()


def test_after_the_automatic_resume_is_used_up_the_turn_waits_for_a_person(tmp_path, monkeypatch):
    gate = Gate(first_call_only=True)
    h = threaded(tmp_path, monkeypatch, [[call("block", {}), say("never")], [say("manual recovery")]], gate, policy=None)
    h.policy = __import__("turn_harness").make_policy(max_auto_resumes=0)
    h.new_process()
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "go")
    assert gate.entered.wait(5)
    h.clock.advance(minutes=5)
    assert h.svc.maintenance()["turns_interrupted"] == 1
    r = h.svc.get_turn(run.id)
    assert r.status == "interrupted" and r.resume_count == 0 and kinds(events_of(h.svc, run.id))[-1] == "turn_error"
    assert h.svc.send_message(s.id, "another message").status == "running"  # an interrupted turn does not block the session
    gate.release()
    wait_until(lambda: h.executor.active == 0, what="workers to settle")
    h.close()


def test_resuming_by_hand_works_for_an_interrupted_turn_only(tmp_path, monkeypatch):
    gate = Gate(first_call_only=True)
    h = threaded(tmp_path, monkeypatch, [[call("block", {}), say("never")], [say("manual recovery")]], gate)
    h.policy = __import__("turn_harness").make_policy(max_auto_resumes=0)
    h.new_process()
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "go")
    assert gate.entered.wait(5)
    with pytest.raises(Conflict):
        h.svc.resume_turn(run.id)  # still running
    h.clock.advance(minutes=5)
    h.svc.maintenance()
    h.svc.resume_turn(run.id)
    wait_until(lambda: status(h, run.id) == "completed", what="the manual resume")
    assert h.svc.store.get_run(run.id).output == "manual recovery" and h.svc.store.get_run(run.id).resume_count == 0  # manual resumes are not counted
    gate.release()
    h.close()


def test_new_turns_are_refused_when_the_server_is_full_but_admitted_turns_still_continue(tmp_path, monkeypatch):
    gate = Gate()
    h = Hub("memory", tmp_path, monkeypatch, [[call("execute", {"command": "echo parked"}), say("x")], [call("block", {}), say("busy turn done")], [say("resumed fine")]],
            approval_mode="execute", tools=[gate.tool()], executor=ThreadExecutor(1))
    h.new_process()
    a, b, c = h.svc.create_session(), h.svc.create_session(), h.svc.create_session()
    ra = h.svc.send_message(a.id, "park me")
    wait_until(lambda: status(h, ra.id) == "awaiting_approval", what="turn A to park")  # parked turns hold no capacity
    rb = h.svc.send_message(b.id, "occupy the only slot")
    assert gate.entered.wait(5)
    with pytest.raises(Busy):
        h.svc.send_message(c.id, "one too many")
    assert h.svc.store.list_session_runs(c.id) == []  # nothing was created for the refused message
    with pytest.raises(Conflict):
        h.svc.send_message(b.id, "second message while the first is running")
    appr = h.svc.list_approvals(status="pending")[0]
    h.svc.decide(appr.id, "approve")  # an admitted turn is always allowed to continue, even over capacity
    wait_until(lambda: status(h, ra.id) == "completed", what="the resumed turn A")
    assert status(h, rb.id) == "running"
    gate.release()
    wait_until(lambda: status(h, rb.id) == "completed", what="turn B")
    assert h.svc.send_message(c.id, "now there is room").status in ("running", "completed")
    h.close()


def test_following_a_live_turn_delivers_every_event_in_order_and_stops_when_it_ends(tmp_path, monkeypatch):
    gate = Gate()
    h = threaded(tmp_path, monkeypatch, [[call("block", {}), say("the end")]], gate)
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "go")
    assert gate.entered.wait(5)
    got = []
    t = threading.Thread(target=lambda: got.extend(h.svc.tail_turn(run.id, poll_seconds=0.01, heartbeat_seconds=0.05)))
    t.start()
    wait_until(lambda: any(e.get("type") == "heartbeat" for e in got), what="a heartbeat during the quiet stretch")
    gate.release()
    t.join(10)
    assert not t.is_alive()
    real = [e for e in got if e["type"] not in ("heartbeat",)]
    assert [e["seq"] for e in real] == list(range(1, len(real) + 1)) and real[-1]["type"] == "turn_done" and real[0]["type"] == "user_message"
    assert [e["type"] for e in real] == kinds(events_of(h.svc, run.id))
    h.close()


def test_cancelling_during_parallel_tool_calls_still_records_every_result_of_that_step(tmp_path, monkeypatch):
    """Found by a real run: the model wrote 5 files in one step; all 5 happened, but the log showed one result because the
    cancel check ran between the results of the same step."""
    from langchain_core.messages import AIMessage

    gate = Gate(first_call_only=True)
    h = Hub("memory", tmp_path, monkeypatch, [[AIMessage(content="", tool_calls=[
        {"name": "block", "args": {}, "id": "p1", "type": "tool_call"},
        {"name": "ls", "args": {"path": str(tmp_path)}, "id": "p2", "type": "tool_call"},
        {"name": "ls", "args": {"path": str(tmp_path / "ws")}, "id": "p3", "type": "tool_call"}]), say("never")]],
        tools=[gate.tool()], executor=ThreadExecutor(2))
    h.new_process()
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "do three things at once")
    assert gate.entered.wait(5)
    h.svc.cancel_turn(run.id)  # requested while the step is still running
    gate.release()
    wait_until(lambda: status(h, run.id) == "cancelled", what="the cancel to take effect")
    evs = events_of(h.svc, run.id)
    assert sorted(e["payload"]["tool_call_id"] for e in evs if e["type"] == "tool_result") == ["p1", "p2", "p3"]  # nothing of that step was lost
    assert kinds(evs)[-1] == "turn_done" and "assistant_message" not in kinds(evs)
    h.close()
