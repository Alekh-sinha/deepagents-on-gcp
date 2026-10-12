"""The orchestrator chooses the model of each helper (policy models.helper_model_choice): ceiling by rank, one tool for delegating, gates intact."""
import copy
import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from pinboard.agent import build_agent, resume, run
from pinboard.agent.models import ModelRouter
from pinboard.core.policy import parse_policy
from pinboard.gcp.vault import Vault

from helpers import make_settings
from test_connector_gating import GOOD, make_world
from test_policy import BASE

LOG: list[dict] = []

MODELS = [("m-lite", 1), ("m-mid", 2), ("m-pro", 3), ("m-max", 4)]


class Named(BaseChatModel):
    """Knows its own name. The orchestrator's first human message is a JSON list of delegations; a helper's message says what to do."""
    name: str
    tools_offered: list = []

    @property
    def _llm_type(self):
        return "named"

    def bind_tools(self, tools, **kw):
        return self.model_copy(update={"tools_offered": [t.get("name") if isinstance(t, dict) else getattr(t, "name", "") for t in tools]})

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        first = next((m.content for m in messages if getattr(m, "type", "") == "human"), "")
        last = messages[-1]
        system = next((m.content for m in messages if getattr(m, "type", "") == "system"), "")
        orchestrator = first.startswith("[")
        LOG.append({"who": "orchestrator" if orchestrator else "helper", "model": self.name, "tools": self.tools_offered, "system": str(system), "task": first})
        if orchestrator:
            if isinstance(last, ToolMessage):
                msg = AIMessage(content="all done")
            else:
                calls = json.loads(first)
                msg = AIMessage(content="", tool_calls=[{"name": c.pop("_tool", "delegate"), "args": c, "id": f"d{i}", "type": "tool_call"} for i, c in enumerate(calls)])
        elif isinstance(last, ToolMessage):
            msg = AIMessage(content=f"helper finished on {self.name}")
        elif "mail" in first:
            msg = AIMessage(content="", tool_calls=[{"name": "send_email", "args": {**GOOD, "subject": first}, "id": f"e-{first}", "type": "tool_call"}])
        elif "branch" in first:
            msg = AIMessage(content="", tool_calls=[{"name": "github_create_branch", "args": {"repo": "alekh-sinha/google_translator", "branch": "agent/x"}, "id": "g1", "type": "tool_call"}])
        else:
            msg = AIMessage(content=f"helper finished on {self.name}")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def policy(choice="on"):
    d = copy.deepcopy(BASE)
    d["models"] = {"default": "m-mid", "helper_model_choice": choice,
                   "allowed": [{"id": i, "provider": "google_genai", "rank": r, "use_for": f"use {i} for rank {r} work"} for i, r in MODELS]}
    return parse_policy(d)


@pytest.fixture
def world(tmp_path):
    LOG.clear()
    return make_world(tmp_path, "fixed-model")


def make_rt(world, tmp_path, choice="on", approval_mode="none"):
    ws = tmp_path / "ws-d"
    ws.mkdir(exist_ok=True)
    router = ModelRouter(policy(choice), lambda spec: Named(name=spec.id))
    return build_agent(make_settings(ws, skills_source=None, approval_mode=approval_mode), Vault("env"), sandbox=world.sb, tools=[], connectors=world.conn, router=router)


def go(rt, delegations, model="m-max", thread="t"):
    LOG.clear()
    return run(rt, json.dumps(delegations), thread_id=thread, model=model)


def helper_models():
    return sorted(e["model"] for e in LOG if e["who"] == "helper")


def orchestrator_call():
    return next(e for e in LOG if e["who"] == "orchestrator")


# ── the choice ───────────────────────────────────────────────────────────

def test_each_helper_runs_on_the_model_chosen_for_that_call(world, tmp_path):
    rt = make_rt(world, tmp_path)
    res = go(rt, [{"description": "look up A", "subagent_type": "researcher", "model": "m-lite"},
                  {"description": "look up B", "subagent_type": "researcher", "model": "m-pro"},
                  {"description": "look up C", "subagent_type": "researcher"}])  # no model: the orchestrator's own (the user's model)
    assert res.status == "completed" and helper_models() == ["m-lite", "m-max", "m-pro"]


def test_a_helper_never_runs_above_the_model_the_user_selected(world, tmp_path):
    rt = make_rt(world, tmp_path)
    res = go(rt, [{"description": "look up", "subagent_type": "researcher", "model": "m-pro"}], model="m-mid")
    assert res.status == "completed" and helper_models() == []
    refused = [m.content for m in rt.agent.get_state({"configurable": {"thread_id": "t"}}).values["messages"] if isinstance(m, ToolMessage)]
    assert "not available" in refused[0] and "m-lite, m-mid" in refused[0] and "m-pro," not in refused[0]


def test_a_rank_tie_counts_as_equal_and_lower_ranks_are_always_allowed(world, tmp_path):
    rt = make_rt(world, tmp_path)
    go(rt, [{"description": "x", "subagent_type": "researcher", "model": "m-lite"}, {"description": "y", "subagent_type": "researcher", "model": "m-mid"}], model="m-mid")
    assert helper_models() == ["m-lite", "m-mid"]


def test_the_orchestrator_is_offered_delegate_instead_of_task_and_told_the_models_it_may_use(world, tmp_path):
    rt = make_rt(world, tmp_path)
    go(rt, [{"description": "look up", "subagent_type": "researcher"}], model="m-mid")
    first = orchestrator_call()
    assert "delegate" in first["tools"] and "task" not in first["tools"] and "write_file" in first["tools"]
    assert "m-lite (rank 1)" in first["system"] and "m-mid (rank 2)" in first["system"] and "m-pro (rank 3)" not in first["system"]
    assert "hand work over with the delegate tool" in first["system"] and "CHEAPEST" in first["system"]


def test_a_task_call_the_model_makes_anyway_is_refused(world, tmp_path):
    rt = make_rt(world, tmp_path)
    res = go(rt, [{"_tool": "task", "description": "do it", "subagent_type": "researcher"}])
    assert res.status == "completed" and helper_models() == []
    results = [m for m in rt.agent.get_state({"configurable": {"thread_id": "t"}}).values["messages"] if isinstance(m, ToolMessage)]
    assert "Use the delegate tool instead" in results[0].content and results[0].status == "error"


# ── switched off ─────────────────────────────────────────────────────────

def test_with_the_switch_off_nothing_is_added_and_the_library_task_tool_is_used(world, tmp_path):
    rt = make_rt(world, tmp_path, choice="off")
    go(rt, [{"_tool": "task", "description": "look up", "subagent_type": "researcher"}], model="m-pro")
    first = orchestrator_call()
    assert "task" in first["tools"] and "delegate" not in first["tools"] and "Delegating work" not in first["system"]
    assert "hand work over with the task tool" in first["system"]
    assert helper_models() == ["m-pro"]  # helpers follow the user's model, as before


def test_a_fixed_model_runtime_has_no_delegation(world, tmp_path):
    from test_connector_gating import ScriptedModel, say  # noqa: F401  (a fixed model has no router, so no pool to choose from)

    rt = build_agent(make_settings(tmp_path / "ws-f", skills_source=None), Vault("env"), model=ScriptedModel(script=[say("hi")]), sandbox=world.sb, tools=[], connectors=world.conn)
    assert "delegate" not in rt.agent.nodes["tools"].bound.tools_by_name and "task" in rt.agent.nodes["tools"].bound.tools_by_name


# ── a helper on another model is still gated and still least-privileged ──

def test_a_mailer_on_a_cheaper_model_still_waits_for_approval_and_sends_on_that_model(world, tmp_path):
    rt = make_rt(world, tmp_path)
    parked = go(rt, [{"description": "mail the report", "subagent_type": "mailer", "model": "m-lite"}])
    assert parked.status == "awaiting_approval" and parked.pending[0]["name"] == "send_email" and world.mail.sent == []
    LOG.clear()
    done = resume(rt, "t", [{"type": "approve"}], model="m-max")
    assert done.status == "completed" and len(world.mail.sent) == 1
    assert {e["model"] for e in LOG if e["who"] == "helper"} == {"m-lite"}  # after the pause the helper is still on the model chosen for it


def test_two_parallel_helpers_on_different_models_pause_together_and_are_answered_by_id(world, tmp_path):
    rt = make_rt(world, tmp_path)
    parked = go(rt, [{"description": "mail one", "subagent_type": "mailer", "model": "m-lite"}, {"description": "mail two", "subagent_type": "mailer", "model": "m-mid"}])
    assert parked.status == "awaiting_approval" and len(parked.pending) == 2 and len({p["interrupt_id"] for p in parked.pending}) == 2
    decisions = [{"type": "approve"} if p["args"]["subject"] == "mail two" else {"type": "reject", "message": "no"} for p in parked.pending]
    assert resume(rt, "t", decisions, model="m-max").status == "completed"
    assert [m["subject"] for m in world.mail.sent] == ["mail two"]


def test_the_coder_still_cannot_change_github_whatever_its_model(world, tmp_path):
    rt = make_rt(world, tmp_path)
    res = go(rt, [{"description": "make a branch", "subagent_type": "coder", "model": "m-lite"}])
    assert res.status == "completed" and res.pending == []
    assert not [c for c in world.http.calls if c["method"] == "POST" and c["path"].endswith("/git/refs")]


def test_the_main_agent_still_publishes_to_github_with_approval(world, tmp_path):
    """Delegation does not touch the orchestrator's own GitHub tools: they stay gated (fixed-model agent on the same connectors)."""
    from helpers import call as tool_call
    from test_connector_gating import ScriptedModel, say

    fixed = build_agent(make_settings(tmp_path / "ws-g", skills_source=None), Vault("env"),
                        model=ScriptedModel(script=[tool_call("github_create_branch", {"repo": "alekh-sinha/google_translator", "branch": "agent/x"}, "c1"), say("ok")]),
                        sandbox=world.sb, tools=[], connectors=world.conn)
    assert run(fixed, "branch", thread_id="g").status == "awaiting_approval"


@pytest.mark.asyncio
async def test_the_async_path_runs_helpers_on_the_chosen_model_too(world, tmp_path):
    rt = make_rt(world, tmp_path)
    LOG.clear()
    out = await rt.agent.ainvoke({"messages": [{"role": "user", "content": json.dumps([{"description": "look up", "subagent_type": "researcher", "model": "m-lite"}])}]},
                                 {"configurable": {"thread_id": "async"}}, context={"model": "m-max"})
    assert out["messages"][-1].content == "all done" and helper_models() == ["m-lite"]
