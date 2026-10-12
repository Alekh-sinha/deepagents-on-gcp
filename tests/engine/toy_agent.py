"""A second agent, written only against agent/contract.py: plain LangChain/LangGraph, NO deepagents, NO sandbox, no `task` / `delegate` tools.
The engine runs it through ``AGENT_BUILDER=toy_agent:build``. The model is scripted by the test (``STATE["scripts"]``, one script per build)."""
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.tools import tool
from langgraph.types import interrupt

from helpers import ScriptedModel
from pinboard.agent.contract import AgentHandle

STATE: dict = {"scripts": [], "contexts": [], "graph_checkpointer": "ctx", "return": "handle"}


@tool
def lookup(topic: str) -> str:
    """Look something up (harmless, runs without asking)."""
    return f"facts about {topic}"


@tool
def send_note(text: str) -> str:
    """Send a note (a person must approve it first)."""
    return f"note sent: {text}"


@tool
def ask(question: str) -> str:
    """Ask the person something and wait for the answer."""
    return f"the person said: {interrupt(question)}"


def build(ctx):
    STATE["contexts"].append(ctx)
    scripts = STATE["scripts"]
    model = ScriptedModel(script=list(scripts[min(len(STATE["contexts"]) - 1, len(scripts) - 1)]))
    graph = create_agent(model, tools=[lookup, send_note, ask], middleware=[HumanInTheLoopMiddleware(interrupt_on={"send_note": True})],
                         checkpointer=ctx.checkpointer if STATE["graph_checkpointer"] == "ctx" else None)
    return graph if STATE["return"] == "graph" else "not an agent" if STATE["return"] == "junk" else AgentHandle(graph)
