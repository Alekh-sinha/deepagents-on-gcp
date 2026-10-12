"""``delegate``: hand a task to a helper and say WHICH model the helper runs on.

The library's ``task`` tool starts a helper that was compiled with one fixed model, so it cannot take a model argument. When the policy switches
``models.helper_model_choice`` on, this middleware adds a tool of our own with the same job plus a ``model`` argument. Nothing in the library is changed:

* helpers are compiled ONCE with the library's public ``create_sub_agent`` and the model-selection middleware, so one compiled graph serves every model;
* ``delegate`` runs a helper with ``graph.invoke(state, config, context={"model": chosen})``: the context picks the model for that call only;
* the middleware adds a prompt section (the models the orchestrator may choose, cheapest first), hides the library's ``task`` tool from the model and
  refuses a ``task`` call it makes anyway, so there is exactly one way to delegate;
* a helper may never use a model ranked above the one the user selected (the ceiling), checked here, in code, not in the prompt.

It inherits ``AgentMiddleware`` and not ``SubAgentMiddleware``: that class's constructor always builds and compiles the library's ``task`` tool, and its
``private_state_keys`` setter rebuilds it, so inheriting would compile every helper twice and could quietly put ``task`` back.
When the switch is off none of this is built and the library's ``task`` works as before.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, Optional, Sequence

from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.subagents import create_sub_agent
from deepagents.middleware.summarization import create_summarization_middleware
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field

from pinboard.agent.models import ModelRouter, ModelSelectionMiddleware, _context_model
from pinboard.core.policy import PolicyDenied

log = logging.getLogger(__name__)

LIBRARY_TASK_TOOL = "task"
DELEGATE_TOOL = "delegate"
STATE_PASSED_TO_HELPERS = ("files",)  # what a helper sees of the orchestrator's state; its conversation is NOT passed, only the description
DEFAULT_HELPER_MODEL_CALLS = 40


def _tool_name(tool: Any) -> str | None:
    return tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)


def _append_text(message: SystemMessage | None, text: str) -> SystemMessage:
    if message is None:
        return SystemMessage(content=text)
    content = message.content
    if isinstance(content, str):
        return SystemMessage(content=f"{content}\n\n{text}" if content else text)
    return SystemMessage(content=[*content, {"type": "text", "text": f"\n\n{text}"}])


def _last_text(messages: Sequence[Any]) -> str:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            text = (m.text or "").rstrip() if hasattr(m, "text") else str(m.content)
            if text:
                return text
    return ""


class ModelChoiceSubAgentMiddleware(AgentMiddleware):
    def __init__(self, router: ModelRouter, helpers: Sequence[dict], *, backend: Any, max_model_calls: int = DEFAULT_HELPER_MODEL_CALLS):
        """``helpers``: the library's SubAgent shape without ``model`` (name, description, system_prompt, tools, optional interrupt_on / middleware)."""
        super().__init__()
        if not helpers:
            raise ValueError("at least one helper is required")
        self.router, self.policy = router, router.policy
        self._helpers = {h["name"]: h for h in helpers}
        base_model = router.get(None)
        self._graphs = {}
        for h in helpers:
            spec = {
                **{k: v for k, v in h.items() if k != "model"},
                "model": base_model,  # only the starting point: the run context chooses the model for every call
                "middleware": [
                    FilesystemMiddleware(backend=backend),
                    create_summarization_middleware(base_model, backend),
                    PatchToolCallsMiddleware(),
                    *h.get("middleware", []),
                    ModelSelectionMiddleware(router),
                    ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="end"),
                ],
            }
            self._graphs[h["name"]] = create_sub_agent(spec)
        self.tools = [self._build_tool()]

    # ── the tool ─────────────────────────────────────────────────────────────
    def _build_tool(self) -> StructuredTool:
        names = tuple(self._helpers)
        ids = tuple(m.id for m in self.policy.models)

        class DelegateArgs(BaseModel):
            description: str = Field(description="Everything the helper needs, as the helper sees NOTHING else: paths, goals, the exact output you want back.")
            subagent_type: Literal[names] = Field(description="Which helper to use.")  # type: ignore[valid-type]
            model: Optional[Literal[ids]] = Field(default=None, description="Which model the helper runs on. Leave out to use your own model.")  # type: ignore[valid-type]

        helper_lines = "\n".join(f"- {n}: {h['description']}" for n, h in self._helpers.items())
        return StructuredTool.from_function(
            func=self._run, coroutine=self._arun, name=DELEGATE_TOOL, args_schema=DelegateArgs, infer_schema=False,
            description=("Hand a task to a helper agent and get back its final report. The helper starts with a blank conversation (only your description). "
                         "Start several helpers in the same step when their work is independent. You can choose the model each helper runs on "
                         "(see the model list in your instructions).\n\nAvailable helpers:\n" + helper_lines))

    def _prepare(self, subagent_type: str, model: str | None, runtime: ToolRuntime):
        """Returns (error_text, graph, state, chosen_model_id). Exactly one of error_text / graph is set."""
        graph = self._graphs.get(subagent_type)
        if graph is None:
            return f"There is no helper called {subagent_type!r}; use one of: {', '.join(self._graphs)}.", None, None, None
        ceiling = _context_model(runtime) or self.policy.default_model
        try:
            allowed = self.policy.allowed_for(ceiling)
        except PolicyDenied as exc:
            return exc.message(), None, None, None
        chosen = model or ceiling
        if chosen not in {m.id for m in allowed}:
            return (f"Model {chosen!r} is not available for helpers in this conversation. Choose one of: {', '.join(m.id for m in allowed)} "
                    f"(or leave `model` out to use {ceiling}). Call delegate again."), None, None, None
        if not runtime.tool_call_id:
            raise ValueError("Tool call ID is required for delegation")
        state = {k: runtime.state[k] for k in STATE_PASSED_TO_HELPERS if k in runtime.state}
        return None, graph, state, chosen

    def _result(self, result: dict, runtime: ToolRuntime) -> Command:
        update = {k: result[k] for k in STATE_PASSED_TO_HELPERS if k in result}
        return Command(update={**update, "messages": [ToolMessage(_last_text(result.get("messages", [])), tool_call_id=runtime.tool_call_id)]})

    _CONFIG = {"configurable": {"ls_agent_type": "subagent"}}  # the parent's callbacks, checkpoint namespace and tags are inherited from the ambient config

    def _run(self, description: str, subagent_type: str, runtime: ToolRuntime, model: str | None = None):
        error, graph, state, chosen = self._prepare(subagent_type, model, runtime)
        if error:
            return error
        state["messages"] = [HumanMessage(content=description)]
        return self._result(graph.invoke(state, self._CONFIG, context={"model": chosen}), runtime)

    async def _arun(self, description: str, subagent_type: str, runtime: ToolRuntime, model: str | None = None):
        error, graph, state, chosen = self._prepare(subagent_type, model, runtime)
        if error:
            return error
        state["messages"] = [HumanMessage(content=description)]
        return self._result(await graph.ainvoke(state, self._CONFIG, context={"model": chosen}), runtime)

    # ── what the orchestrator is told ────────────────────────────────────────
    def prompt_section(self, ceiling: str | None) -> str:
        allowed = self.policy.allowed_for(ceiling)
        lines = "\n".join(f"- {m.id} (rank {m.rank}): {m.use_for or m.label}" for m in allowed)
        return ("## Delegating work\n"
                "Use the `delegate` tool to hand work to a helper. You may run each helper on a different model. Pick the CHEAPEST model that can do the "
                "job well: the lowest rank for simple lookups and formatting, higher ranks only for hard reasoning or careful analysis. "
                f"If you leave `model` out, the helper runs on {ceiling or self.policy.default_model}, your own model.\n"
                f"Models you may choose (rank 1 is the cheapest):\n{lines}")

    def _shape(self, request):
        try:
            ceiling = _context_model(request.runtime) or self.policy.default_model
            section = self.prompt_section(ceiling)
        except PolicyDenied:  # an unusable selection is refused elsewhere; do not fail the model call over the prompt
            return request
        tools = [t for t in request.tools if _tool_name(t) != LIBRARY_TASK_TOOL]  # one way to delegate: the library's own tool stays out of sight
        return request.override(tools=tools, system_message=_append_text(request.system_message, section))

    def wrap_model_call(self, request, handler):
        return handler(self._shape(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._shape(request))

    # ── a `task` call the model makes anyway (it is not offered, but a model can still name it) ──
    def _refuse_task(self, request):
        call = request.tool_call
        if call.get("name") != LIBRARY_TASK_TOOL:
            return None
        log.warning("model called the hidden task tool; refused")
        return ToolMessage(content="The task tool is not available. Use the delegate tool instead.", tool_call_id=call["id"], name=LIBRARY_TASK_TOOL, status="error")

    def wrap_tool_call(self, request, handler):
        return self._refuse_task(request) or handler(request)

    async def awrap_tool_call(self, request, handler):
        return self._refuse_task(request) or await handler(request)
