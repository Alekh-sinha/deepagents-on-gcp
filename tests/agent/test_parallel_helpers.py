"""Parallel helpers: the orchestrator starts several helpers in one step, each pauses for its own approval, and the answers go back by interrupt id."""
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from pinboard.agent import build_agent, resume, run
from pinboard.gcp.vault import Vault

from helpers import make_settings
from test_connector_gating import make_world


class Router(BaseChatModel):
    """Stateless, so it is safe when several helpers call it at the same time. The orchestrator is the conversation that starts with 'go';
    a helper is one that starts with 'send:'."""

    @property
    def _llm_type(self):
        return "router"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        humans = [m for m in messages if getattr(m, "type", "") == "human"]
        first, last = (humans[0].content if humans else ""), messages[-1]
        if first.startswith("send:"):  # a mailer
            if getattr(last, "type", "") == "tool":
                msg = AIMessage(content="sent")
            else:
                subject = first.split(":", 1)[1].strip()
                msg = AIMessage(content="", tool_calls=[{"name": "send_email", "args": {"to": ["me@example.com"], "subject": subject, "body": "hi"}, "id": f"e-{subject}", "type": "tool_call"}])
        elif getattr(last, "type", "") == "tool":
            msg = AIMessage(content="both mails handled")
        else:
            msg = AIMessage(content="", tool_calls=[{"name": "task", "args": {"description": f"send: {s}", "subagent_type": "mailer"}, "id": f"t-{s}", "type": "tool_call"} for s in ("one", "two")])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def build(world, tmp_path):
    ws = tmp_path / "ws2"
    ws.mkdir()
    world.build(["unused"])  # makes the connectors' sandbox and fakes; we need only those
    return build_agent(make_settings(ws, skills_source=None), Vault("env"), model=Router(), tools=[], connectors=world.conn, sandbox=world.sb)


def test_two_parallel_mailers_both_pause_and_each_answer_reaches_its_own_email(tmp_path):
    world = make_world(tmp_path, "fixed-model")
    rt = build(world, tmp_path)
    parked = run(rt, "go", thread_id="t")
    assert parked.status == "awaiting_approval" and len(parked.pending) == 2
    assert len({p["interrupt_id"] for p in parked.pending}) == 2 and world.mail.sent == []
    decisions = [{"type": "approve"} if p["args"]["subject"] == "one" else {"type": "reject", "message": "not this one"} for p in parked.pending]
    done = resume(rt, "t", decisions)
    assert done.status == "completed" and done.output == "both mails handled"
    assert [m["subject"] for m in world.mail.sent] == ["one"]  # the approval for "one" did not leak onto "two"


class Looper(BaseChatModel):
    """The orchestrator hands one task to the coder; the coder never stops calling a tool."""
    calls: list = []

    @property
    def _llm_type(self):
        return "looper"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        first = next((m.content for m in messages if getattr(m, "type", "") == "human"), "")
        if first == "go":
            msg = (AIMessage(content="orchestrator done") if getattr(messages[-1], "type", "") == "tool" else
                   AIMessage(content="", tool_calls=[{"name": "task", "args": {"description": "loop", "subagent_type": "coder"}, "id": "t1", "type": "tool_call"}]))
        else:
            self.calls.append(1)
            msg = AIMessage(content="", tool_calls=[{"name": "github_list_repos", "args": {}, "id": f"l{len(self.calls)}", "type": "tool_call"}])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def test_a_helper_that_never_stops_is_cut_off_and_the_orchestrator_carries_on(tmp_path):
    """The parent's step cap does not reach inside a helper (verified), so each helper has its own model-call limit."""
    from pinboard.agent.subagents import DEFAULT_HELPER_MODEL_CALLS

    world = make_world(tmp_path, "fixed-model")
    world.build(["unused"])
    ws = tmp_path / "ws3"
    ws.mkdir()
    model = Looper(calls=[])
    rt = build_agent(make_settings(ws, skills_source=None), Vault("env"), model=model, tools=[], connectors=world.conn, sandbox=world.sb)
    res = run(rt, "go", thread_id="t")
    assert res.status == "completed" and res.output == "orchestrator done"
    assert len(model.calls) <= DEFAULT_HELPER_MODEL_CALLS + 1


def test_the_limit_comes_from_the_policy_when_there_is_one():
    from pinboard.agent.subagents import build_subagents

    mws = [type(m).__name__ for sp in build_subagents([], None, None, max_model_calls=3) for m in sp["middleware"]]
    assert mws.count("ModelCallLimitMiddleware") == 3  # general-purpose, researcher, coder (no mailer without email)
