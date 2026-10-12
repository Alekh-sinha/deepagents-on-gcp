"""Session turns end to end: events, the per-session sandbox, approvals, restarts, cancellation, lost workers, tailing."""
import pytest

from helpers import call, say
from turn_harness import BACKENDS, Hub, events_of, kinds


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


def hub(backend, tmp_path, monkeypatch, scripts, **kw):
    h = Hub(backend, tmp_path, monkeypatch, scripts, **kw)
    h.new_process()
    return h


def test_a_turn_writes_its_story_as_events_and_the_session_keeps_its_sandbox(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("write_file", {"file_path": "PLACEHOLDER", "content": "hello"}), say("saved it")]])
    h.scripts[0][0] = call("write_file", {"file_path": f"{h.sbdir('sb-1')}/note.txt", "content": "hello"})
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "write a note")
    assert run.status == "completed" and run.output == "saved it" and run.session_id == s.id and run.thread_id == s.thread_id
    evs = events_of(h.svc, run.id)
    assert kinds(evs) == ["user_message", "turn_started", "tool_call", "tool_result", "assistant_message", "turn_done"]
    assert [e["seq"] for e in evs] == [1, 2, 3, 4, 5, 6] and evs[-1]["payload"]["status"] == "completed"
    assert (h.sbdir("sb-1") / "note.txt").read_text() == "hello"
    timing = evs[-1]["payload"]["timing"]  # where the time went: building the agent, waiting for the model, streaming
    assert set(timing) == {"setup_seconds", "probe_seconds", "build_seconds", "prep_seconds", "first_output_seconds", "stream_seconds", "total_seconds"}
    assert timing["total_seconds"] >= timing["setup_seconds"] >= 0 and timing["first_output_seconds"] is not None
    sess = h.svc.store.get_session(s.id)
    assert sess.sandbox_name == "sb-1" and sess.title == "write a note" and sess.sandbox_expires_at > h.clock()
    assert h.farm.deleted == [] and h.svc.store.get_run(run.id).lease_id is None  # the sandbox lives on; the lease is released
    h.close()


def test_a_later_turn_attaches_to_the_same_sandbox_and_sees_its_files(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("write_file", {"file_path": "X", "content": "kept"}), say("wrote")],
                                              [call("read_file", {"file_path": "X"}), say("read it")]])
    h.scripts[0][0] = call("write_file", {"file_path": f"{h.sbdir('sb-1')}/data.txt", "content": "kept"})
    h.scripts[1][0] = call("read_file", {"file_path": f"{h.sbdir('sb-1')}/data.txt"})
    s = h.svc.create_session()
    h.svc.send_message(s.id, "write")
    run2 = h.svc.send_message(s.id, "now read it back")
    assert run2.status == "completed" and h.farm.created == ["sb-1"]
    assert [b["adopt"] for b in h.builds] == [None, "sb-1"]  # the second turn attached instead of creating
    seen = " ".join(str(m.content) for msgs in h.models[1].seen for m in msgs if getattr(m, "type", "") == "tool")
    assert "kept" in seen
    first_user_msgs = [m.content for m in h.models[1].seen[0] if getattr(m, "type", "") == "human"]
    assert "write" in first_user_msgs and "now read it back" in first_user_msgs  # one conversation: the checkpoint carried the first turn over
    assert h.farm.deleted == []
    h.close()


def test_sessions_never_share_a_sandbox(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")], [say("y")]])
    h.scripts[:] = [[call("write_file", {"file_path": f"{h.sbdir('sb-1')}/a.txt", "content": "A"}), say("one")],
                    [call("write_file", {"file_path": f"{h.sbdir('sb-2')}/b.txt", "content": "B"}), say("two")]]
    a, b = h.svc.create_session(), h.svc.create_session()
    h.svc.send_message(a.id, "hi")
    h.svc.send_message(b.id, "hi")
    assert h.farm.created == ["sb-1", "sb-2"]
    assert h.svc.store.get_session(a.id).sandbox_name == "sb-1" and h.svc.store.get_session(b.id).sandbox_name == "sb-2"
    assert (h.sbdir("sb-1") / "a.txt").exists() and not (h.sbdir("sb-1") / "b.txt").exists()
    h.close()


def test_parking_on_an_approval_then_deciding_continues_the_same_turn(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("execute", {"command": "echo ran >> proof.txt"}), say("never reached")], [say("all done")]],
            approval_mode="execute")
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "run it")
    assert run.status == "awaiting_approval" and run.sandbox_name is None  # a session turn keeps nothing on the run: the session owns the sandbox
    appr = h.svc.list_approvals(status="pending")[0]
    assert appr.previews[0]["kind"] == "shell" and h.farm.created == []  # the command has not run, so no sandbox was even created yet
    evs = events_of(h.svc, run.id)
    assert kinds(evs)[-1] == "interrupt" and evs[-1]["payload"]["approval_id"] == appr.id and evs[-1]["payload"]["items"][0]["kind"] == "shell"
    assert h.farm.deleted == []

    done = h.svc.decide(appr.id, "approve")
    assert done.status == "completed" and done.output == "all done" and (h.sbdir("sb-1") / "proof.txt").read_text() == "ran\n"
    assert h.farm.created == ["sb-1"] and h.svc.store.get_session(s.id).sandbox_name == "sb-1"  # created the moment the approved command needed it
    evs = events_of(h.svc, run.id)
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))  # one gapless story across both segments
    assert kinds(evs).count("tool_call") == 1 and kinds(evs).count("user_message") == 1  # the approved call was not announced twice
    assert "approval_decided" in kinds(evs) and kinds(evs)[-1] == "turn_done"
    assert [b["adopt"] for b in h.builds] == [None, None]  # there was nothing to attach to when the turn resumed
    h.close()


def test_a_plan_and_a_question_flow_through_the_service(backend, tmp_path, monkeypatch):
    plan = {"summary": "Write it", "tasks": [{"title": "Write", "detail": "", "assignee": "assistant"}]}
    h = hub(backend, tmp_path, monkeypatch, [[call("submit_plan", plan), say("x")], [call("ask_user", {"question": "Format?"}), say("y")],
                                              [say("final")]])
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "do the thing")
    appr = h.svc.list_approvals(status="pending")[0]
    assert run.status == "awaiting_approval" and appr.previews[0]["kind"] == "plan"
    assert h.svc.decide(appr.id, "approve").status == "awaiting_approval"  # the next gate: the model now asks a question
    q = h.svc.list_approvals(status="pending")[0]
    assert q.previews[0]["kind"] == "question" and q.previews[0]["allowed_decisions"] == ["respond"]
    done = h.svc.decide(q.id, decisions=[{"type": "respond", "message": "markdown please"}])
    assert done.status == "completed" and done.output == "final"
    seen = [m.content for msgs in h.models[2].seen for m in msgs if getattr(m, "type", "") == "tool"]
    assert "markdown please" in seen
    assert kinds(events_of(h.svc, run.id)).count("interrupt") == 2
    h.close()


def test_a_subagent_waiting_for_approval_survives_a_restart(backend, tmp_path, monkeypatch):
    task = call("task", {"description": "run the command", "subagent_type": "coder"}, "c1")
    h = hub(backend, tmp_path, monkeypatch, [[task, call("execute", {"command": "echo from coder >> coder.txt"}, "c2"), say("coder done"), say("main done")],
                                              [say("coder done"), say("main done")]], approval_mode="execute")
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "delegate it")
    assert run.status == "awaiting_approval"
    evs = events_of(h.svc, run.id)
    assert [e["payload"]["agent"] for e in evs if e["type"] == "tool_call"] == ["assistant", "coder"]  # the helper's step is visible
    appr = h.svc.list_approvals(status="pending")[0]
    assert appr.action_requests[0]["name"] == "execute" and not (h.sbdir("sb-1") / "coder.txt").exists()

    svc2 = h.new_process()  # the process restarts: postgres loses every in-memory object
    assert svc2.get_run(run.id).status == "awaiting_approval"
    done = svc2.decide(appr.id, "approve")
    assert done.status == "completed" and done.output == "main done" and (h.sbdir("sb-1") / "coder.txt").read_text() == "from coder\n"
    said = [(e["payload"]["agent"], e["payload"]["text"]) for e in events_of(svc2, run.id) if e["type"] == "assistant_message"]
    assert said == [("coder", "coder done"), ("assistant", "main done")]  # after the restart the helper is still called "coder", not "helper"
    assert kinds(events_of(svc2, run.id))[-1] == "turn_done"
    h.close()
