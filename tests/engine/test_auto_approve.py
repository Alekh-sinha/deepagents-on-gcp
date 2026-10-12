"""Automatic approval ("approve all"): set on the session, applied by the engine itself, stays until the user changes it,
and never answers a question, never covers what was not switched on, never exceeds its limit."""
from langchain_core.messages import AIMessage
import pytest
from fastapi.testclient import TestClient

from pinboard.engine import auto_approve as auto
from pinboard.engine.api import create_app
from pinboard.engine.errors import BadRequest

from helpers import call, say
from turn_harness import BACKENDS, Hub, events_of, kinds

PLAN = {"summary": "Write a file", "tasks": [{"title": "Write", "detail": "", "assignee": "assistant"}]}


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


def hub(backend, tmp_path, monkeypatch, scripts, **kw):
    h = Hub(backend, tmp_path, monkeypatch, scripts, approval_mode="execute", **kw)
    h.new_process()
    return h


def sh(cmd, cid):
    return call("execute", {"command": cmd}, cid)


def autos(h, run_id):
    return [e for e in events_of(h.svc, run_id) if e["type"] == "approval_auto"]


# ── the setting ──────────────────────────────────────────────────────────

def test_the_setting_is_normalised_validated_and_clamped():
    n = lambda v, cap=25: auto.normalise(v, default_cap=cap, hard_cap=cap)  # noqa: E731
    assert n(None) is None and n(False) is None and n({}) is None and n({"all": False}) is None
    assert n(True) == {"kinds": ["shell", "github"], "max_per_turn": 25}  # "approve all" leaves out e-mail, plan and unrecognised tools
    assert n({"all": True}) == n(True)
    assert n({"kinds": ["plan", "shell"], "max_per_turn": 3}) == {"kinds": ["shell", "plan"], "max_per_turn": 3}  # canonical order
    assert n({"kinds": ["shell"], "max_per_turn": 1000}, 25)["max_per_turn"] == 25  # never above the policy limit
    for bad in ("yes", 1, [], {"kinds": []}, {"kinds": "shell"}, {"kinds": ["question"]}, {"kinds": ["interrupt"]}, {"kinds": ["nope"]},
                {"all": True, "extra": 1}, {"kinds": ["shell"], "max_per_turn": 0}, {"kinds": ["shell"], "max_per_turn": True}, {"max_per_turn": 3}):
        with pytest.raises(BadRequest):
            n(bad)


def test_covers_needs_every_action_to_be_switched_on_and_approvable():
    setting = {"kinds": ["shell", "github"], "max_per_turn": 5}
    shell, mail, ask = ({"kind": "shell", "allowed_decisions": ["approve", "reject"]}, {"kind": "email", "allowed_decisions": ["approve"]},
                        {"kind": "question", "allowed_decisions": ["respond"]})
    assert auto.covers(setting, [shell]) and auto.covers(setting, [shell, shell])
    assert not auto.covers(setting, [shell, mail]) and not auto.covers(setting, [ask]) and not auto.covers(None, [shell]) and not auto.covers(setting, [])
    assert not auto.covers(setting, [{"kind": "shell", "allowed_decisions": ["respond"]}])  # approval is not even one of the choices


# ── the engine applies it ────────────────────────────────────────────────

def test_off_by_default_every_command_waits_for_a_person(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[sh("echo a", "c1"), say("done")]])
    s = h.svc.create_session()
    assert h.svc.send_message(s.id, "go").status == "awaiting_approval" and autos(h, h.svc.get_session(s.id)["turns"][0]["id"]) == []
    h.close()


def test_on_the_engine_approves_by_itself_and_the_turn_just_finishes(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[sh("echo a >> f.txt", "c1"), sh("echo b >> f.txt", "c2"), say("all done")]])
    s = h.svc.create_session(auto_approve=True)
    run = h.svc.send_message(s.id, "go")
    assert run.status == "completed" and run.output == "all done"  # nobody was asked
    assert (h.sbdir("sb-1") / "f.txt").read_text() == "a\nb\n"
    evs = autos(h, run.id)
    assert len(evs) == 2 and evs[0]["payload"]["items"][0]["kind"] == "shell" and evs[0]["payload"]["items"][0]["preview"]["command"] == "echo a >> f.txt"
    assert h.svc.list_approvals(status="pending") == []
    recorded = h.state.store.list_approvals(run.user_id, "approved")
    assert len(recorded) == 2 and {a.decided_by for a in recorded} == {"auto"}  # the history shows who approved
    assert [a["event"] for a in h.state.store.list_audit(run.id) if a["event"].startswith("approval.")] == ["approval.auto", "approval.auto"]
    assert kinds(events_of(h.svc, run.id))[-1] == "turn_done"
    h.close()


def test_the_setting_stays_across_turns_until_the_user_changes_it(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[sh("echo 1", "c1"), say("one")], [sh("echo 2", "c2"), say("two")], [sh("echo 3", "c3"), say("three")]])
    s = h.svc.create_session()
    assert h.svc.send_message(s.id, "first", auto_approve=True).status == "completed"      # switched on with this message
    assert h.svc.send_message(s.id, "second").status == "completed"                          # nothing said: still on
    assert h.svc.get_session(s.id)["session"]["auto_approve"]["kinds"] == ["shell", "github"]
    third = h.svc.send_message(s.id, "third", auto_approve=False)                            # switched off
    assert third.status == "awaiting_approval" and h.svc.get_session(s.id)["session"]["auto_approve"] is None
    h.close()


def test_a_questions_is_never_answered_for_the_user(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("ask_user", {"question": "which format?"}, "q1"), say("ok")]])
    s = h.svc.create_session(auto_approve={"kinds": ["shell", "github", "other", "email", "plan"]})  # even with everything switched on
    run = h.svc.send_message(s.id, "go")
    assert run.status == "awaiting_approval" and h.svc.list_approvals(status="pending")[0].previews[0]["kind"] == "question" and autos(h, run.id) == []
    h.close()


def test_plan_approval_is_the_users_unless_plan_was_switched_on(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("submit_plan", PLAN, "p1"), say("planned")], [call("submit_plan", PLAN, "p2"), say("planned")]])
    on = h.svc.create_session(auto_approve=True)
    assert h.svc.send_message(on.id, "plan it").status == "awaiting_approval"  # "all" does not include the plan
    explicit = h.svc.create_session(auto_approve={"kinds": ["plan"]})
    run = h.svc.send_message(explicit.id, "plan it")
    assert run.status == "completed" and [e["payload"]["items"][0]["kind"] for e in autos(h, run.id)] == ["plan"]
    h.close()


def test_one_action_that_is_not_covered_sends_the_whole_batch_to_the_person(backend, tmp_path, monkeypatch):
    both = AIMessage(content="", tool_calls=[{"name": "execute", "args": {"command": "echo hi"}, "id": "a1", "type": "tool_call"},
                                             {"name": "ask_user", "args": {"question": "sure?"}, "id": "a2", "type": "tool_call"}])
    h = hub(backend, tmp_path, monkeypatch, [[both, say("done")]])
    s = h.svc.create_session(auto_approve=True)
    run = h.svc.send_message(s.id, "go")
    assert run.status == "awaiting_approval" and autos(h, run.id) == []
    assert sorted(p["kind"] for p in h.svc.list_approvals(status="pending")[0].previews) == ["question", "shell"]
    h.close()


def test_the_per_turn_limit_hands_control_back_to_the_person(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[sh("echo 1", "c1"), sh("echo 2", "c2"), say("finished")], [say("finished")]])
    s = h.svc.create_session(auto_approve={"kinds": ["shell"], "max_per_turn": 1})
    run = h.svc.send_message(s.id, "go")
    assert run.status == "awaiting_approval" and len(autos(h, run.id)) == 1  # the first was automatic, the second waits
    assert any(e["type"] == "notice" and "Automatic approval paused" in e["payload"]["text"] for e in events_of(h.svc, run.id))
    done = h.svc.decide(h.svc.list_approvals(status="pending")[0].id, "approve")
    assert done.status == "completed"
    h.close()


def test_a_kind_that_was_not_switched_on_waits(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[sh("echo 1", "c1"), say("x")]])
    s = h.svc.create_session(auto_approve={"kinds": ["github"]})  # shell is not covered
    assert h.svc.send_message(s.id, "go").status == "awaiting_approval"
    h.close()


def test_switching_it_off_takes_effect_for_the_next_pause(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[sh("echo 1", "c1"), say("x")]])
    s = h.svc.create_session(auto_approve=True)
    h.svc.update_session(s.id, {"auto_approve": None})
    assert h.svc.send_message(s.id, "go").status == "awaiting_approval"
    h.close()


def test_a_bad_value_is_refused_before_the_turn_starts(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("hi")]])
    s = h.svc.create_session()
    with pytest.raises(BadRequest):
        h.svc.send_message(s.id, "go", auto_approve={"kinds": ["question"]})
    assert h.svc.get_session(s.id)["turns"] == []  # nothing started
    with pytest.raises(BadRequest):
        h.svc.update_session(s.id, {"auto_approve": "always"})
    h.close()


def test_config_tells_a_frontend_what_it_can_offer(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("hi")]])
    opts = h.svc.get_config()["approvals"]["auto_approve"]
    assert opts["default_kinds"] == ["shell", "github"] and opts["never"] == ["question", "interrupt"] and "email" in opts["kinds"]
    h.close()


# ── over HTTP ────────────────────────────────────────────────────────────

def test_the_setting_travels_through_the_contract_and_rest_routes(tmp_path, monkeypatch):
    import json

    h = Hub("memory", tmp_path, monkeypatch, [[sh("echo 1", "c1"), say("done")]], approval_mode="execute")
    h.new_process()
    c = TestClient(create_app(h.svc, "t" * 32))
    auth = {"Authorization": "Bearer " + "t" * 32}

    def contract(method, inp):
        body = json.dumps({"class_method": method, "input": inp})
        return c.post("/api/reasoning_engine", content=json.dumps(body), headers={**auth, "content-type": "application/json"})

    sid = contract("create_session", {"auto_approve": True}).json()["output"]["id"]
    assert contract("get_session", {"session_id": sid}).json()["output"]["session"]["auto_approve"]["kinds"] == ["shell", "github"]
    assert contract("update_session", {"session_id": sid, "auto_approve": {"kinds": ["shell"], "max_per_turn": 4}}).json()["output"]["auto_approve"]["max_per_turn"] == 4
    assert contract("update_session", {"session_id": sid, "auto_approve": {"kinds": ["question"]}}).status_code == 400
    assert c.patch(f"/v1/sessions/{sid}", json={"auto_approve": False}, headers=auth).json()["auto_approve"] is None
    sent = c.post(f"/v1/sessions/{sid}/messages", json={"text": "go", "auto_approve": True}, headers=auth)
    assert sent.status_code == 200 and sent.json()["status"] in ("completed", "running")
    assert c.get(f"/v1/sessions/{sid}", headers=auth).json()["session"]["auto_approve"] is not None
    h.close()


def test_switching_it_off_while_a_turn_runs_stops_the_next_automatic_approval(backend, tmp_path, monkeypatch):
    from langchain_core.tools import tool

    box = {}

    @tool
    def switch_off() -> str:
        """The user flips the toggle while the agent is working."""
        box["h"].svc.update_session(box["sid"], {"auto_approve": None})
        return "switched"

    h = hub(backend, tmp_path, monkeypatch, [[sh("echo 1", "c1"), call("switch_off", {}, "f1"), sh("echo 2", "c2"), say("end")]], tools=[switch_off])
    box["h"] = h
    s = h.svc.create_session(auto_approve=True)
    box["sid"] = s.id
    run = h.svc.send_message(s.id, "go")
    assert run.status == "awaiting_approval" and len(autos(h, run.id)) == 1  # the first was automatic; after the toggle the second waits for the user
    assert h.svc.list_approvals(status="pending")[0].action_requests[0]["args"]["command"] == "echo 2"
    h.close()


# ── tools nobody classified, and tools the policy always wants a person for ───────────────

def test_a_tool_the_engine_does_not_recognise_is_not_covered_by_approve_all():
    """A new agent's own tools show up as kind 'other': a person must opt in to those explicitly."""
    unknown = {"kind": "other", "tool": "drop_table", "allowed_decisions": ["approve", "reject"]}
    assert not auto.covers({"kinds": list(auto.DEFAULT_KINDS), "max_per_turn": 5}, [unknown])
    assert auto.covers({"kinds": ["other"], "max_per_turn": 5}, [unknown])  # ...but they can say yes to the kind


def test_a_tool_on_the_policy_list_is_never_automatic_even_when_its_kind_is_on():
    shell = {"kind": "shell", "tool": "execute", "allowed_decisions": ["approve", "reject"]}
    setting = {"kinds": ["shell"], "max_per_turn": 5}
    assert auto.covers(setting, [shell]) and not auto.covers(setting, [shell], ["execute"])
    assert auto.held_back([shell, {"kind": "github", "tool": "github_open_pr"}], ["execute", "x"]) == ["execute"]


def policy_with(never):
    import copy

    from pinboard.core.policy import parse_policy
    from test_policy import BASE

    d = copy.deepcopy(BASE)
    d["limits"] = {"max_turn_minutes": 10, "max_concurrent_turns": 4, "max_auto_resumes": 1, "sandbox_idle_minutes": 20}
    d["approvals"] = {"never_auto_tools": never}
    return parse_policy(d)


def test_the_engine_stops_at_a_tool_the_policy_always_wants_a_person_for_and_says_why(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[sh("echo 1", "c1"), say("x")]], policy=policy_with(["execute"]))
    s = h.svc.create_session(auto_approve=True)  # shell is switched on...
    run = h.svc.send_message(s.id, "go")
    assert run.status == "awaiting_approval" and autos(h, run.id) == []  # ...but the policy overrides it
    notes = [e["payload"]["text"] for e in events_of(h.svc, run.id) if e["type"] == "notice"]
    assert any("execute always needs your approval" in n for n in notes)
    assert h.svc.get_config()["approvals"]["auto_approve"]["never_tools"] == ["execute"]
    h.close()


def test_the_policy_list_is_validated():
    import copy

    import pytest as _pytest

    from pinboard.core.policy import PolicyError, parse_policy
    from test_policy import BASE

    for bad in ("execute", [1], ["has space"], {"a": 1}):
        d = copy.deepcopy(BASE)
        d["approvals"] = {"never_auto_tools": bad}
        with _pytest.raises(PolicyError):
            parse_policy(d)
    d = copy.deepcopy(BASE)
    assert parse_policy(d).never_auto_tools == ()
    d["approvals"] = {"never_auto_tools": ["a", "b", "a"]}
    assert parse_policy(d).never_auto_tools == ("a", "b")
