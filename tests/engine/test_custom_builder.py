"""Running ANOTHER agent in the engine: ``AGENT_BUILDER=toy_agent:build`` with every optional feature off.

The toy agent (toy_agent.py) is plain LangChain: no deepagents, no sandbox, no `task` / `delegate` tools. Everything the engine promises on any
compiled graph must still work: streaming events, approvals (human-in-the-loop and plain ``interrupt``), cancel, resume after a restart,
housekeeping. And nothing sandbox-shaped may happen: the platform is never contacted (the fake farm records every call and the callbacks raise).
"""
import dataclasses
from datetime import timedelta

import pytest

from pinboard.agent.contract import load_builder
from pinboard.core.policy import Features, PolicyError, parse_policy
from pinboard.engine.errors import NotFound

import toy_agent
from helpers import call, say
from test_policy import BASE
from turn_harness import BACKENDS, Hub, events_of, kinds, make_policy

NO_SANDBOX_POLICY = dataclasses.replace(make_policy(), features=Features(sandbox=False, artifacts=False))


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


@pytest.fixture(autouse=True)
def fresh_toy():
    toy_agent.STATE.update(scripts=[], contexts=[], graph_checkpointer="ctx", **{"return": "handle"})
    yield


def boom(*args, **kwargs):
    raise AssertionError("the sandbox platform was contacted although this agent has no sandbox")


def hub(backend, tmp_path, monkeypatch, scripts, *, policy=NO_SANDBOX_POLICY, **over):
    toy_agent.STATE["scripts"] = scripts
    h = Hub(backend, tmp_path, monkeypatch, [[None]], policy=policy, settings_over={"agent_builder": "toy_agent:build"})
    h.new_process(**{**dict(build=None, sandbox_deleter=boom, sandbox_probe=boom, spare_factory=boom), **over})  # build=None: the real AGENT_BUILDER loader
    return h


def no_platform_contact(h):
    assert h.farm.created == [] and h.farm.deleted == [] and h.farm.create_calls == []


# ── ordinary turns ───────────────────────────────────────────────────────

def test_a_chat_turn_runs_the_other_agent_with_no_sandbox_and_no_helper_blocks(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("hello from the toy")]])
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "hi")
    assert run.status == "completed" and run.output == "hello from the toy"
    evs = events_of(h.svc, run.id)
    assert kinds(evs) == ["user_message", "turn_started", "assistant_message", "turn_done"]
    ctx = toy_agent.STATE["contexts"][0]
    assert ctx.sandbox is None and not ctx.artifacts and ctx.session_id == s.id and ctx.user_id == h.settings.user_id and ctx.checkpointer is h.state.checkpointer
    assert h.svc.store.get_session(s.id).sandbox_name is None and not h.svc.store.get_session(s.id).had_sandbox
    no_platform_contact(h)
    h.close()


def test_tool_calls_stream_as_ordinary_steps_of_the_agent(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("lookup", {"topic": "tea"}), say("tea is nice")]])
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "tell me about tea")
    evs = events_of(h.svc, run.id)
    assert kinds(evs) == ["user_message", "turn_started", "tool_call", "tool_result", "assistant_message", "turn_done"]
    assert [e["payload"]["agent"] for e in evs if e["type"] in ("tool_call", "tool_result", "assistant_message")] == ["assistant"] * 3  # no helper labels
    assert [e["payload"]["name"] for e in evs if e["type"] == "tool_call"] == ["lookup"]
    h.close()


# ── pauses: the approval machinery works on any graph ────────────────────

def test_a_human_in_the_loop_pause_becomes_a_card_and_the_answer_resumes_the_graph(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("send_note", {"text": "buy milk"}), say("never reached")], [say("note delivered")]])
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "send a note")
    assert run.status == "awaiting_approval" and kinds(events_of(h.svc, run.id))[-1] == "interrupt"
    appr = h.svc.list_approvals(status="pending")[0]
    assert appr.action_requests[0]["name"] == "send_note" and appr.previews[0]["kind"] == "other"  # an unknown tool gets the generic card
    svc2 = h.new_process(build=None, sandbox_deleter=boom, sandbox_probe=boom, spare_factory=boom)  # a restart (postgres loses every in-memory object)
    done = svc2.decide(appr.id, "approve")
    assert done.status == "completed" and done.output == "note delivered"
    results = [e["payload"] for e in events_of(svc2, run.id) if e["type"] == "tool_result"]
    assert results and "note sent: buy milk" in results[0]["preview"]
    no_platform_contact(h)
    h.close()


def test_rejecting_the_pause_stops_the_tool_from_running(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("send_note", {"text": "secret"}), say("x")], [say("understood, not sent")]])
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "send it")
    appr = h.svc.list_approvals(status="pending")[0]
    done = h.svc.decide(appr.id, "reject", message="no")
    assert done.status == "completed"
    assert not any("note sent" in str(e["payload"]) for e in events_of(h.svc, run.id))
    h.close()


def test_a_plain_interrupt_inside_a_tool_is_a_question_and_the_typed_answer_is_the_result(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("ask", {"question": "Which colour?"}), say("x")], [say("blue it is")]])
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "ask me something")
    assert run.status == "awaiting_approval"
    appr = h.svc.list_approvals(status="pending")[0]
    assert appr.previews[0]["kind"] == "interrupt" and "Which colour?" in str(appr.action_requests[0]) and appr.previews[0]["allowed_decisions"] == ["respond"]
    done = h.svc.decide(appr.id, decisions=[{"type": "respond", "message": "blue"}])
    assert done.status == "completed"
    assert any("the person said: blue" in str(e["payload"]) for e in events_of(h.svc, run.id) if e["type"] == "tool_result")
    h.close()


def test_a_parked_turn_can_be_cancelled(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("send_note", {"text": "x"}), say("y")]])
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "send")
    assert h.svc.cancel_turn(run.id).status == "cancelled"
    assert h.svc.list_approvals(status="pending") == []
    h.close()


# ── the rest of the engine's surface answers sensibly with the features off ─

def test_features_that_are_off_answer_as_empty_not_as_errors(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("ok")]])
    s = h.svc.create_session()
    h.svc.send_message(s.id, "hi")
    assert h.svc.list_artifacts(session_id=s.id) == [] and h.svc.list_artifacts() == []
    with pytest.raises(NotFound):
        h.svc.get_artifact("a_nope")
    with pytest.raises(NotFound):
        h.svc.read_artifact("a_nope")
    with pytest.raises(NotFound):
        h.svc.artifact_url("a_nope")
    assert h.svc.warm_sandbox(s.id) == {"status": "off"}
    assert h.svc.get_config()["features"] == {"sandbox": False, "artifacts": False}
    report = h.svc.maintenance()
    assert report["sandboxes_deleted"] == 0 and report["spares_deleted"] == 0
    no_platform_contact(h)
    h.close()


def test_a_legacy_one_shot_run_needs_no_sandbox_either(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("send_note", {"text": "n"}), say("x")], [say("done")]])
    run = h.svc.start_run("legacy")
    assert run.status == "awaiting_approval" and run.sandbox_name is None
    done = h.svc.decide(h.svc.list_approvals(status="pending")[0].id, "approve")
    assert done.status == "completed" and done.outputs == []
    completed = [a for a in h.svc.audit(run.id) if a["event"] == "run.completed"]
    assert completed and completed[0]["detail"] == {"outputs": [], "skipped": []}  # nothing was left behind or quietly failed
    no_platform_contact(h)
    h.close()


def test_a_session_from_before_the_switch_is_not_told_its_sandbox_was_replaced(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("hi")]])
    s = h.svc.create_session()
    h.svc.store.update_session(s.id, had_sandbox=True, sandbox_name="sb-old", sandbox_expires_at=h.clock() + timedelta(hours=1))
    run = h.svc.send_message(s.id, "hi")
    assert run.status == "completed" and not any(e["type"] == "notice" for e in events_of(h.svc, run.id))
    h.close()


def test_with_the_sandbox_on_the_other_agent_is_told_how_to_use_it(backend, tmp_path, monkeypatch):
    """The default policy still gives a custom builder the sandbox request; an agent that does not use it simply ignores it."""
    h = hub(backend, tmp_path, monkeypatch, [[say("hi")]], policy=make_policy(), sandbox_deleter=lambda n: None, sandbox_probe=lambda n: True)
    s = h.svc.create_session()
    h.svc.send_message(s.id, "hi")
    req = toy_agent.STATE["contexts"][0].sandbox
    assert req is not None and req.lazy is True and req.adopt is None and callable(req.on_created) and callable(req.claim)
    h.close()


# ── a builder that breaks the contract is explained, not obeyed ──────────

@pytest.mark.parametrize("mode, words", [("junk", "return an AgentHandle"), ("graph_without_checkpointer", "compiled with ctx.checkpointer")])
def test_a_builder_that_breaks_the_contract_fails_the_turn_with_a_message_that_says_what_to_fix(backend, tmp_path, monkeypatch, mode, words):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]])
    if mode == "junk":
        toy_agent.STATE["return"] = "junk"
    else:
        toy_agent.STATE["graph_checkpointer"] = "none"
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "hi")
    assert run.status == "failed" and words in run.error
    h.close()


def test_a_bare_compiled_graph_is_accepted_too(backend, tmp_path, monkeypatch):
    toy_agent.STATE["return"] = "graph"
    h = hub(backend, tmp_path, monkeypatch, [[say("bare graph works")]])
    s = h.svc.create_session()
    assert h.svc.send_message(s.id, "hi").output == "bare graph works"
    h.close()


@pytest.mark.parametrize("spec, words", [
    ("", "package.module:function"), ("no_colon_here", "package.module:function"), ("a:b:c", "package.module:function"),
    ("not_a_module_xyz:build", "cannot import"), ("toy_agent:nope", "no function called"), ("toy_agent:STATE", "no function called")])
def test_a_bad_builder_setting_is_refused_at_start_up(spec, words):
    with pytest.raises(ValueError, match=words):
        load_builder(spec)


def test_the_default_builder_is_unchanged_when_no_builder_is_named(tmp_path, monkeypatch):
    from pinboard.agent import build_agent
    h = Hub("memory", tmp_path, monkeypatch, [[None]])
    from pinboard.engine.service import RunService
    svc = RunService(h.settings, h.vault, *(lambda st: (st.store, st.checkpointer))(__import__("pinboard.state", fromlist=["make_state"]).make_state(h.settings, h.vault)))
    assert svc._build is build_agent
    h.close()


# ── the policy switches ──────────────────────────────────────────────────

def policy_with(**sections):
    d = dict(BASE)
    d.update(sections)
    return parse_policy(d)


def test_both_features_are_on_when_the_policy_says_nothing():
    assert policy_with().features == Features(True, True)


def test_turning_the_sandbox_off_turns_artifacts_off_unless_told_otherwise():
    assert policy_with(features={"sandbox": False}).features == Features(False, False)
    assert policy_with(features={"artifacts": False}).features == Features(True, False)


@pytest.mark.parametrize("section, words", [
    ({"features": {"sandbox": False, "artifacts": True}}, "artifacts needs features.sandbox"),
    ({"features": {"sandbox": False}, "warm_sandbox": {"mode": "on_activity"}}, "warm_sandbox.mode must be off"),
    ({"features": {"sandbox": "no"}}, "must be true or false"),
    ({"features": {"sandboxx": False}}, "unknown features"),
    ({"features": ["sandbox"]}, "must be a mapping")])
def test_an_inconsistent_or_misspelt_feature_setting_is_refused(section, words):
    with pytest.raises(PolicyError, match=words):
        policy_with(**section)
