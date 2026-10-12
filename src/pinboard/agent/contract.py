"""How to run YOUR agent in this engine instead of Pinboard's: write one function, name it in ``AGENT_BUILDER``.

    AGENT_BUILDER=my_package.my_module:build

    def build(ctx: BuildContext) -> AgentHandle | <a compiled LangGraph graph>:
        graph = ...your graph...compile(checkpointer=ctx.checkpointer)      # the checkpointer is REQUIRED: pauses and resumes live in it
        return AgentHandle(graph)

What the engine gives you and what it expects back
  * ``build`` is called for every segment of a turn (the first run and again after each approval), so keep it cheap and cache heavy objects yourself.
  * The graph must be compiled with ``ctx.checkpointer``. Anything that pauses (``interrupt(...)``, human-in-the-loop middleware) is shown to the
    person as an approval card or a question, and the answer is fed back to the graph; nothing else is needed.
  * Without a sandbox (policy ``features.sandbox: false``) ``ctx.sandbox`` is None and the handle needs no sandbox. With one, ``ctx.sandbox`` says
    which sandbox to attach to / when to create one, and the handle's ``sandbox`` is whatever object your tools run in (it needs ``id``, ``is_open`` and
    ``cwd``; artifacts are read from ``<cwd>/out`` through its ``execute`` / ``download_files``).
  * Everything else (events, approvals, cancel, resume after a restart, schedules, the web app) works on any compiled LangGraph graph.

Not part of the contract: models, tools, prompts, skills, connectors. Those are your agent's business.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable

_SPEC = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")

# What the engine passes to a builder as keyword arguments (see RunService._runtime); the sandbox ones are absent when the sandbox is off.
_SANDBOX_KEYS = ("lazy_sandbox", "on_sandbox_created", "claim_sandbox", "on_sandbox_claimed")


class NoSandbox:
    """The sandbox of an agent that has none: the engine sees a sandbox that was never opened, so it saves and cleans up nothing."""

    id = None
    is_open = False

    def __repr__(self) -> str:
        return "NO_SANDBOX"


NO_SANDBOX = NoSandbox()


@dataclass(frozen=True)
class SandboxRequest:
    """How the engine wants the sandbox handled this turn (only given when the policy has the sandbox on)."""

    adopt: str | None = None                          # name of this conversation's existing sandbox to attach to, if any
    lazy: bool = False                                # create it the first time a tool needs it, not up front
    on_created: Callable[[str], None] | None = None   # call with the new sandbox's name the moment it exists (the engine records it)
    claim: Callable[[], str | None] | None = None     # ask the warm pool for a ready sandbox; returns its name or None
    on_claimed: Callable[[str], None] | None = None


@dataclass(frozen=True)
class BuildContext:
    settings: Any                                     # pinboard.core.config.Settings (environment values; unknown names stay in os.environ)
    vault: Any                                        # secrets by name: vault.get("MY_KEY"); values never reach logs or events
    checkpointer: Any                                 # compile your graph with this
    user_id: str | None = None                        # who is talking to the agent
    session_id: str | None = None                     # the conversation (None for a legacy one-shot run)
    audit: Callable[[str, dict], None] = lambda event, data: None   # write a line to the audit trail of this turn
    on_block: Callable[[str, Any], None] | None = None              # report a refused command to the audit trail
    ledger: Any = None                                # records side effects (e-mails sent...) so a retry never repeats them
    storage_client: Any = None
    artifacts: Any = None                             # the artifact store (falsy when artifacts are off)
    mcp: Any = None                                   # the user's own MCP tools and their approval gates, if they connected any
    sandbox: SandboxRequest | None = None             # None = this agent runs without a sandbox
    extra: dict = field(default_factory=dict)         # keyword arguments this contract does not know yet; ignore what you do not need


@dataclass
class AgentHandle:
    """What ``build`` returns. Only ``agent`` is required."""

    agent: Any                                        # the compiled LangGraph graph
    sandbox: Any = NO_SANDBOX
    router: Any = None                                # a ModelRouter, if the agent supports choosing the model per message; None otherwise
    on_detach: Callable[[], None] | None = None       # the turn is over or parked: keep the sandbox alive
    on_close: Callable[[], None] | None = None        # throwaway (legacy) run: delete the sandbox

    def detach(self) -> str | None:
        if self.on_detach:
            self.on_detach()
        return getattr(self.sandbox, "id", None)

    def close(self) -> None:
        if self.on_close:
            self.on_close()


def context_from_engine_arguments(settings, vault, kwargs: dict) -> BuildContext:
    """Turn the engine's keyword arguments into a BuildContext."""
    kw = dict(kwargs)
    adopt = kw.pop("adopt_sandbox", None)
    sandbox = None
    if adopt is not None or any(k in kw for k in _SANDBOX_KEYS):
        sandbox = SandboxRequest(adopt=adopt, lazy=bool(kw.pop("lazy_sandbox", False)), on_created=kw.pop("on_sandbox_created", None),
                                 claim=kw.pop("claim_sandbox", None), on_claimed=kw.pop("on_sandbox_claimed", None))
    known = {name: kw.pop(name) for name in ("checkpointer", "user_id", "session_id", "audit", "on_block", "ledger", "storage_client", "artifacts", "mcp") if name in kw}
    return BuildContext(settings=settings, vault=vault, sandbox=sandbox, extra=kw, **known)


def as_handle(result: Any, ctx: BuildContext, spec: str) -> AgentHandle:
    """Accept an AgentHandle (or anything with ``.agent``) or a bare compiled graph; refuse anything else with a message that says what to fix."""
    handle = result if hasattr(result, "agent") else AgentHandle(result) if hasattr(result, "stream") and hasattr(result, "get_state") else None
    if handle is None:
        raise TypeError(f"{spec} returned {type(result).__name__}: return an AgentHandle(graph) or a compiled LangGraph graph")
    graph_checkpointer = getattr(handle.agent, "checkpointer", ctx.checkpointer)
    if ctx.checkpointer is not None and graph_checkpointer is not ctx.checkpointer:
        raise TypeError(f"{spec}: the graph was not compiled with ctx.checkpointer, so approvals could not pause and resume; "
                        "use graph.compile(checkpointer=ctx.checkpointer)")
    return handle


def load_builder(spec: str) -> Callable[..., AgentHandle]:
    """Import ``package.module:function`` and return a builder the engine can call like Pinboard's own (``settings, vault, **keywords``).
    Everything wrong with the setting is reported when the service starts, not on the first message."""
    if not _SPEC.match(spec or ""):
        raise ValueError(f"AGENT_BUILDER must look like 'package.module:function', got {spec!r}")
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(f"AGENT_BUILDER={spec}: cannot import {module_name!r} ({exc}); is it installed in the image, or on PYTHONPATH?") from exc
    fn = getattr(module, attr, None)
    if not callable(fn):
        raise ValueError(f"AGENT_BUILDER={spec}: {module_name} has no function called {attr!r}")

    def build(settings, vault, **kwargs) -> AgentHandle:
        ctx = context_from_engine_arguments(settings, vault, kwargs)
        return as_handle(fn(ctx), ctx, spec)

    build.__name__ = f"build[{spec}]"
    return build
