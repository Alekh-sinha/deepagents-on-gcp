import copy

import pytest

from pinboard.agent import build_agent, resume, run
from pinboard.core.guardrails import CommandGuard
from pinboard.agent.models import ModelRouter
from pinboard.core.policy import PolicyDenied, parse_policy
from pinboard.gcp.sandbox import GCPShellSandbox
from pinboard.agent.tools import make_web_search_tool
from pinboard.gcp.vault import Vault

from helpers import FakeClient, FakeTavily, ScriptedModel, call, make_settings, say
from test_policy import BASE


def policy(**models):
    d = copy.deepcopy(BASE)
    d["models"] = {"default": "m1", "allowed": [{"id": "m1", "provider": "google_genai"}, {"id": "m2", "provider": "google_genai"},
                                                  {"id": "m3", "provider": "google_genai"}], **models}
    return parse_policy(d)


def make(tmp_path, scripts, **settings_over):
    """Agent built the PRODUCTION way (model=None -> policy router); only the model factory is faked."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    settings = make_settings(ws, skills_source=None, **settings_over)
    sb = GCPShellSandbox(FakeClient(ws), "fake", guard=CommandGuard(settings.command_policy, str(ws)), cwd=str(ws))
    models = {mid: ScriptedModel(script=script) for mid, script in scripts.items()}
    built = []

    def factory(spec):
        built.append(spec.id)
        return models[spec.id]

    router = ModelRouter(policy(), factory)
    rt = build_agent(settings, Vault("env"), sandbox=sb, tools=[make_web_search_tool("k", client=FakeTavily())], router=router)
    return rt, models, built


def test_router_validates_caches_and_builds_lazily():
    built = []
    r = ModelRouter(policy(), lambda spec: built.append(spec.id) or object())
    a, b = r.get("m2"), r.get("m2")
    assert a is b and built == ["m2"]
    assert r.get(None) is r.get("m1") and built == ["m2", "m1"]
    with pytest.raises(PolicyDenied):
        r.get("evil-model")
    assert built == ["m2", "m1"]  # nothing was built for the rejected id


def test_default_model_is_used_without_a_selection(tmp_path):
    rt, models, built = make(tmp_path, {"m1": [say("from m1")], "m2": [say("from m2")]})
    assert built == ["m1"]  # only the default is built up front
    assert run(rt, "hello", thread_id="t").output == "from m1"
    assert models["m2"].idx == 0 and built == ["m1"]


def test_per_request_model_switches_the_model_and_builds_it_once(tmp_path):
    rt, models, built = make(tmp_path, {"m1": [say("from m1")], "m2": [say("from m2")]})
    assert run(rt, "hi", thread_id="a", model="m2").output == "from m2"
    assert run(rt, "again", thread_id="b", model="m2").output == "from m2"
    assert built == ["m1", "m2"] and models["m1"].idx == 0
    assert run(rt, "default again", thread_id="c", model="m1").output == "from m1"  # naming the default is fine


def test_switching_model_inside_one_session_keeps_the_conversation(tmp_path):
    rt, models, _ = make(tmp_path, {"m1": [say("one says hi")], "m2": [say("two continues")]})
    assert run(rt, "first question", thread_id="s").output == "one says hi"
    assert run(rt, "second question", thread_id="s", model="m2").output == "two continues"
    seen = [m.content for m in models["m2"].seen[-1] if getattr(m, "type", "") in ("human", "ai")]
    assert "first question" in seen and "one says hi" in seen and "second question" in seen


def test_disallowed_model_is_rejected_before_anything_runs(tmp_path):
    rt, models, built = make(tmp_path, {"m1": [say("x")]})
    with pytest.raises(PolicyDenied) as e:
        run(rt, "hi", thread_id="t", model="gpt-9")
    assert e.value.rule == "model.not_allowed" and models["m1"].idx == 0 and built == ["m1"]


def test_a_fixed_model_runtime_refuses_selection(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    s = make_settings(ws, skills_source=None)
    sb = GCPShellSandbox(FakeClient(ws), "f", cwd=str(ws))
    rt = build_agent(s, Vault("env"), model=ScriptedModel(script=[say("ok")]), sandbox=sb, tools=[])
    assert run(rt, "hi", thread_id="t").output == "ok"
    with pytest.raises(ValueError, match="fixed model"):
        run(rt, "hi", thread_id="t2", model="m2")


def test_the_chosen_model_survives_an_approval_round_trip(tmp_path):
    rt, models, _ = make(tmp_path, {"m1": [say("wrong")], "m2": [call("execute", {"command": "echo hi"}), say("m2 finished")]},
                         approval_mode="execute")
    parked = run(rt, "do it", thread_id="p", model="m2")
    assert parked.status == "awaiting_approval" and parked.pending[0]["args"]["command"] == "echo hi"
    done = resume(rt, "p", [{"type": "approve"}], model="m2")
    assert done.status == "completed" and done.output == "m2 finished" and models["m1"].idx == 0


def test_subagents_follow_the_selected_model(tmp_path):
    """Subagents inherit neither middleware nor context by themselves; build_agent gives the general-purpose one the selector."""
    task = call("task", {"description": "say hi", "subagent_type": "general-purpose"})
    rt, models, _ = make(tmp_path, {"m1": [say("sub on m1")], "m2": [task, say("sub on m2"), say("main done on m2")]})
    out = run(rt, "delegate", thread_id="d", model="m2")
    assert out.status == "completed" and out.output == "main done on m2"
    assert models["m2"].idx == 3 and models["m1"].idx == 0  # main, subagent, main: all on m2, none on the default


def test_subagents_use_the_default_model_when_none_is_selected(tmp_path):
    task = call("task", {"description": "say hi", "subagent_type": "general-purpose"})
    rt, models, built = make(tmp_path, {"m1": [task, say("sub on m1"), say("main done on m1")], "m2": [say("x")]})
    assert run(rt, "delegate", thread_id="d").output == "main done on m1"
    assert models["m1"].idx == 3 and models["m2"].idx == 0 and built == ["m1"]


def test_a_model_is_served_from_its_own_location_when_it_has_one(monkeypatch):
    """gemini-3.5-flash exists only in the global location: the model's location beats the deployment-wide setting."""
    import sys
    import types
    from types import SimpleNamespace

    seen = {}

    class Fake:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setitem(sys.modules, "langchain_google_genai", types.SimpleNamespace(ChatGoogleGenerativeAI=Fake))
    s = SimpleNamespace(google_use_vertex=True, gcp_project="p", model_location="europe-west4", gcp_location="us-central1", model_timeout_seconds=5, model_max_retries=1)
    from pinboard.agent.models import build_chat_model

    build_chat_model(s, None, "google_genai", "gemini-3.5-flash", "global")
    assert seen["location"] == "global"
    build_chat_model(s, None, "google_genai", "gemini-2.5-flash")
    assert seen["location"] == "europe-west4"          # no model location: MODEL_LOCATION
    s.model_location = None
    build_chat_model(s, None, "google_genai", "gemini-2.5-flash")
    assert seen["location"] == "us-central1"           # neither: the deployment region
