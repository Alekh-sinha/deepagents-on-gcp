"""Offline checks of the helper-model prototype: scripted models, no network. Run:  uv run pytest spikes/model_choice -q"""
import json

import pytest
from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from delegating import ModelChoiceSubAgentMiddleware
from pool import ModelPool, PoolError, PoolSelectionMiddleware, RunContext

LOG: list[dict] = []   # one entry per model call: who, which model, the tools offered, the system text
GATE_CALLS: list[str] = []


class Named(BaseChatModel):
    """A scripted model that knows its own name. The orchestrator's first human message is a JSON list of delegations to make;
    a helper's first human message (the description) says what to do: write | risky | loop | anything else = answer."""
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
                msg = AIMessage(content="", tool_calls=[{"name": "delegate", "args": c, "id": f"d{i}", "type": "tool_call"} for i, c in enumerate(calls)])
        elif "loop" in first:
            msg = AIMessage(content="", tool_calls=[{"name": "spin", "args": {}, "id": f"s{len(LOG)}", "type": "tool_call"}])
        elif isinstance(last, ToolMessage):
            msg = AIMessage(content=f"helper finished on {self.name}")
        elif "write" in first:
            msg = AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"file_path": "/out.txt", "content": f"by {self.name}"}, "id": "w1", "type": "tool_call"}])
        elif "risky" in first:
            msg = AIMessage(content="", tool_calls=[{"name": "risky", "args": {}, "id": "r1", "type": "tool_call"}])
        else:
            msg = AIMessage(content=f"helper finished on {self.name}")
        return ChatResult(generations=[ChatGeneration(message=msg)])


@tool
def risky() -> str:
    """A tool a person must approve."""
    GATE_CALLS.append("ran")
    return "risky done"


@tool
def spin() -> str:
    """Never ends."""
    return "again"


def make_agent(ceiling_on=True, max_calls=40):
    pool = ModelPool.load(factory=lambda spec: Named(name=spec.id))
    pool.enforce_ceiling = ceiling_on
    helpers = [{"name": "researcher", "description": "Finds facts.", "system_prompt": "You research.", "tools": [risky, spin],
                "interrupt_on": {"risky": {"allowed_decisions": ["approve", "reject"]}}}]
    mw = ModelChoiceSubAgentMiddleware(pool, helpers, backend=StateBackend(), max_model_calls=max_calls)
    agent = create_deep_agent(model=pool.get(pool.default), tools=[], checkpointer=MemorySaver(), context_schema=RunContext,
                              middleware=[PoolSelectionMiddleware(pool), mw])
    return agent, pool


def go(agent, delegations, ceiling="gemini-2.5-pro", thread="t"):
    LOG.clear()
    GATE_CALLS.clear()
    return agent.invoke({"messages": [HumanMessage(json.dumps(delegations))]}, {"configurable": {"thread_id": thread}, "recursion_limit": 100},
                        context={"model": ceiling})


def helper_models():
    return sorted(e["model"] for e in LOG if e["who"] == "helper")


def tool_results(r):
    return [m.content for m in r["messages"] if isinstance(m, ToolMessage)]


# ── the pool file ────────────────────────────────────────────────────────

def test_the_pool_comes_from_the_config_file_with_ranks_and_locations():
    pool = ModelPool.load()
    assert pool.ids == ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-pro"]
    assert [m.rank for m in pool.models] == [1, 2, 3, 4] and pool.spec("gemini-3.5-flash").location == "global" and pool.default == "gemini-2.5-flash"


def test_a_bad_pool_file_is_refused(tmp_path):
    for text, why in (("models: []\ndefault: x\n", "no models"), ("default: nope\nmodels: [{id: a, provider: g, rank: 1}]\n", "default"),
                      ("default: a\nmodels: [{id: a, provider: g, rank: 1}, {id: a, provider: g, rank: 2}]\n", "duplicate"),
                      ("default: a\nmodels: [{id: a, provider: g, rank: 0}]\n", "rank")):
        p = tmp_path / "m.yaml"
        p.write_text(text)
        with pytest.raises(PoolError, match=why):
            ModelPool.load(p)


# ── one helper graph, a different model on every call ────────────────────

def test_each_delegation_runs_its_helper_on_the_model_chosen_for_that_call():
    agent, _ = make_agent()
    r = go(agent, [{"description": "look up A", "subagent_type": "researcher", "model": "gemini-2.5-flash-lite"},
                   {"description": "look up B", "subagent_type": "researcher", "model": "gemini-3.5-flash"},
                   {"description": "look up C", "subagent_type": "researcher"}])  # no model: the orchestrator's own (the ceiling)
    assert helper_models() == ["gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-3.5-flash"]
    assert sorted(tool_results(r)) == ["helper finished on gemini-2.5-flash-lite", "helper finished on gemini-2.5-pro", "helper finished on gemini-3.5-flash"]


def test_a_helper_cannot_use_a_model_above_the_one_the_user_selected():
    agent, _ = make_agent()
    r = go(agent, [{"description": "look up", "subagent_type": "researcher", "model": "gemini-2.5-pro"}], ceiling="gemini-2.5-flash")
    assert helper_models() == []  # the helper never ran
    msg = tool_results(r)[0]
    assert "not available" in msg and "gemini-2.5-flash-lite, gemini-2.5-flash" in msg and "gemini-2.5-pro," not in msg


def test_with_the_ceiling_switched_off_any_pool_model_is_allowed():
    agent, _ = make_agent(ceiling_on=False)
    go(agent, [{"description": "look up", "subagent_type": "researcher", "model": "gemini-3.5-flash"}], ceiling="gemini-2.5-flash")
    assert helper_models() == ["gemini-3.5-flash"]


def test_an_unknown_model_never_reaches_the_tool():
    agent, _ = make_agent()
    r = go(agent, [{"description": "look up", "subagent_type": "researcher", "model": "gpt-9"}])
    assert helper_models() == [] and any("gpt-9" in t for t in tool_results(r))  # the schema (an enum) refuses it and says what it got


# ── what the orchestrator sees ───────────────────────────────────────────

def test_the_library_task_tool_is_hidden_and_delegate_is_offered():
    agent, _ = make_agent()
    go(agent, [{"description": "look up", "subagent_type": "researcher"}])
    offered = next(e for e in LOG if e["who"] == "orchestrator")["tools"]
    assert "delegate" in offered and "task" not in offered and "write_file" in offered  # the file tools stay


def test_the_prompt_lists_only_the_models_the_user_may_give_helpers():
    agent, _ = make_agent()
    go(agent, [{"description": "look up", "subagent_type": "researcher"}], ceiling="gemini-2.5-flash")
    system = next(e for e in LOG if e["who"] == "orchestrator")["system"]
    assert "Delegating work" in system and "gemini-2.5-flash-lite (rank 1)" in system and "gemini-2.5-flash (rank 2)" in system
    assert "gemini-3.5-flash (rank 3)" not in system and "gemini-2.5-pro (rank 4)" not in system and "CHEAPEST" in system


# ── the helper is a full agent ───────────────────────────────────────────

def test_files_a_helper_writes_come_back_to_the_orchestrators_state():
    agent, _ = make_agent()
    r = go(agent, [{"description": "write the file", "subagent_type": "researcher", "model": "gemini-2.5-flash-lite"}])
    assert "/out.txt" in r["files"] and "by gemini-2.5-flash-lite" in str(r["files"]["/out.txt"])


def test_a_gated_tool_in_a_helper_pauses_for_a_person_and_resumes_on_the_same_model():
    agent, _ = make_agent()
    r = go(agent, [{"description": "do the risky thing", "subagent_type": "researcher", "model": "gemini-3.5-flash"}], thread="gated")
    assert r["__interrupt__"] and GATE_CALLS == []
    iid = r["__interrupt__"][0].id
    LOG.clear()
    done = agent.invoke(Command(resume={iid: {"decisions": [{"type": "approve"}]}}), {"configurable": {"thread_id": "gated"}, "recursion_limit": 100},
                        context={"model": "gemini-2.5-pro"})
    assert GATE_CALLS == ["ran"] and done["messages"][-1].content == "all done"
    assert {e["model"] for e in LOG if e["who"] == "helper"} == {"gemini-3.5-flash"}  # after the resume the helper is still on the model chosen for it


def test_a_helper_that_never_stops_is_cut_off():
    agent, _ = make_agent(max_calls=5)
    go(agent, [{"description": "loop forever", "subagent_type": "researcher", "model": "gemini-2.5-flash-lite"}])
    assert 1 <= len([e for e in LOG if e["who"] == "helper"]) <= 6
