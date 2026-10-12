"""Assemble the deep agent: model + sandbox + skills + guardrails + tools + team + approval gates."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langgraph.checkpoint.memory import MemorySaver

from pinboard.agent.backends import with_local_skills
from pinboard.agent.models import ModelRouter, ModelSelectionMiddleware, RunContext, build_chat_model
from pinboard.agent.prompts import SYSTEM_PROMPT
from pinboard.agent.delegation import ModelChoiceSubAgentMiddleware
from pinboard.agent.subagents import DEFAULT_HELPER_MODEL_CALLS, build_subagents, helper_specs
from pinboard.agent.tools import ConnectorTools, build_connector_tools, make_artifact_tools, make_web_search_tool
from pinboard.core.config import Settings, secret_names
from pinboard.gcp.sandbox import SandboxManager
from pinboard.gcp.skills import SyncReport, local_skills_dir, sync_skills
from pinboard.gcp.vault import Vault
from pinboard.core.guardrails import CommandGuard, PromptGuard
from pinboard.core.policy import Policy, load_policy

log = logging.getLogger(__name__)


@dataclass
class AgentRuntime:
    agent: Any
    sandbox: Any
    settings: Settings
    prompt_guard: PromptGuard
    manager: SandboxManager | None = None
    skills_report: SyncReport | None = None
    router: ModelRouter | None = None  # None when a model was injected (tests): per-request model selection is then unavailable

    @property
    def policy(self) -> Policy | None:
        return self.router.policy if self.router else None

    def close(self) -> None:
        if self.manager:
            self.manager.close()

    def detach(self) -> str | None:
        """Keep the sandbox alive (the run is parked on an approval) and return its name."""
        return self.manager.detach() if self.manager else getattr(self.sandbox, "id", None)


def build_model(settings: Settings, vault: Vault):
    """The model named by settings.model ("provider:name"). Production code uses the policy router instead (build_agent)."""
    provider, _, name = settings.model.partition(":")
    return build_chat_model(settings, vault, provider, name)


def build_agent(
    settings: Settings,
    vault: Vault,
    *,
    model=None,
    sandbox=None,
    tools=None,
    checkpointer=None,
    storage_client=None,
    adopt_sandbox: str | None = None,
    on_block=None,
    policy: Policy | None = None,
    router: ModelRouter | None = None,
    connectors: ConnectorTools | None = None,
    ledger=None,
    audit=None,
    artifacts=None,
    user_id: str | None = None,
    session_id: str | None = None,
    client=None,
    lazy_sandbox: bool = False,
    on_sandbox_created: Callable[[str], None] | None = None,
    claim_sandbox: Callable[[], str | None] | None = None,
    on_sandbox_claimed: Callable[[str], None] | None = None,
    mcp=None,
) -> AgentRuntime:
    # Populate the vault cache up-front so output redaction knows every secret value.
    for name in secret_names():
        vault.get(name)

    command_guard = CommandGuard(settings.command_policy, settings.sandbox_cwd)
    prompt_guard = PromptGuard(settings.max_prompt_chars)

    # Build everything that does not need the sandbox first, so a failure here leaks nothing.
    if model is None:  # production path: the policy decides which models exist; the default is built now, others on first use
        if router is None:
            router = ModelRouter(policy or load_policy(), lambda spec: build_chat_model(settings, vault, spec.provider, spec.id, spec.location))
        model = router.get(None)
    tools_injected = tools is not None
    if tools is None:
        tools = [make_web_search_tool(vault.get("TAVILY_API_KEY", required=True), settings.tavily_max_results)]

    manager = None
    if sandbox is None:
        manager = SandboxManager(settings, guard=command_guard, redact=vault.redact, on_block=on_block, adopt=adopt_sandbox, client=client)
        if lazy_sandbox:
            # Nothing is created now: the sandbox appears the first time a tool needs it (a chat-only turn never pays for one).
            def created(sb, name: str) -> None:
                if settings.skills_source:
                    log.info("skills synced into new sandbox: %s", sync_skills(sb, settings.skills_source, settings.skills_path, storage_client).skills)
                if on_sandbox_created:
                    on_sandbox_created(name)

            sandbox = manager.open_lazy(on_created=created, claim=claim_sandbox, on_claimed=on_sandbox_claimed)
        else:
            sandbox = manager.open()
    try:
        if connectors is None and not tools_injected:  # production path: email/GitHub tools from policy + vault
            connectors = build_connector_tools(settings, vault, policy or (router.policy if router else load_policy()),
                                               read_file=_sandbox_reader(sandbox), ledger=ledger, audit=audit or (lambda e, d: None))
        report = None
        if settings.skills_source and not adopt_sandbox and not lazy_sandbox:  # an adopted sandbox already has its skills; a lazy one gets them when created
            report = sync_skills(sandbox, settings.skills_source, settings.skills_path, storage_client)
            log.info("skills synced: %d files, skills=%s, skipped=%s", report.files, report.skills, report.skipped)
        sb_cwd = getattr(sandbox, "cwd", settings.sandbox_cwd).rstrip("/")
        artifact_tools = make_artifact_tools(artifacts, user_id, sandbox, session_id=session_id, cwd=sb_cwd, skills_path=f"{sb_cwd}/skills") if (artifacts and user_id) else []
        backend = sandbox
        if lazy_sandbox and settings.skills_source:  # list/read skills from a local copy, so doing so never forces a sandbox to exist
            backend = with_local_skills(sandbox, settings.skills_path, local_skills_dir(settings.skills_source, storage_client))
        return _assemble(settings, model, tools, sandbox, prompt_guard, manager, report, checkpointer, router, connectors, artifact_tools, backend, mcp)
    except BaseException:
        if manager:
            manager.close()  # never leak a sandbox we opened
        raise


def _sandbox_reader(sandbox):
    """read_file(path) -> bytes for tools that attach or commit files the agent saved in the sandbox."""
    def read(path: str) -> bytes:
        res = sandbox.download_files([path])[0]
        if res.error or res.content is None:
            raise FileNotFoundError(f"{path}: {res.error or 'not found'}")
        return res.content

    return read


def _assemble(settings, model, tools, sandbox, prompt_guard, manager, report, checkpointer, router=None, connectors=None, artifact_tools=(), backend=None, mcp=None) -> AgentRuntime:
    from pinboard.agent.tools import make_interaction_tools

    cwd, skills = settings.sandbox_cwd.rstrip("/"), settings.skills_path
    # Shell approvals follow APPROVAL_MODE. Connector writes, the plan and questions are ALWAYS gated, whatever the mode.
    shell_gate: dict = {"execute": {"allowed_decisions": ["approve", "edit", "reject"]}} if settings.approval_mode == "execute" else {}
    interaction_tools, interaction_gates = make_interaction_tools()
    github = connectors.by_group.get("github", []) if connectors else []  # the orchestrator alone changes GitHub (helpers can only read it)
    github_gates = connectors.gates_by_group.get("github", {}) if connectors else {}
    mcp_tools = list(mcp.tools) if mcp else []   # the user's own MCP servers: the orchestrator holds them (one approval, in one place)
    mcp_gates = dict(mcp.gates) if mcp else {}
    main_tools = [*tools, *interaction_tools, *artifact_tools, *github, *mcp_tools]  # email lives in the mailer only
    interrupt_on = {**shell_gate, **interaction_gates, **github_gates, **mcp_gates}  # the main agent is gated for the tools IT holds

    helper_calls = router.policy.limits.max_helper_model_calls if router else DEFAULT_HELPER_MODEL_CALLS
    # Policy `models.helper_model_choice: on` gives the orchestrator a `delegate` tool that also takes the helper's model; off builds nothing extra.
    choice = ([ModelChoiceSubAgentMiddleware(router, helper_specs(tools, connectors, shell_gate), backend=backend or sandbox, max_model_calls=helper_calls)]
              if router is not None and router.policy.helper_model_choice else [])

    agent = create_deep_agent(
        model=model,
        tools=main_tools,
        system_prompt=SYSTEM_PROMPT.format(cwd=cwd, skills=skills, out=settings.outputs_path, delegate="delegate" if choice else "task") + (mcp.prompt_section() if mcp else ""),
        backend=backend or sandbox,
        skills=[f"{skills}/"] if settings.skills_source else None,  # listing skills reads the backend: never ask for it when there are none
        interrupt_on=interrupt_on,
        checkpointer=checkpointer or MemorySaver(),
        # deepagents 0.7.x does not add write_todos by default (only some model profiles do), so add it: the todo list is the live progress board.
        middleware=[TodoListMiddleware(), *([ModelSelectionMiddleware(router)] if router else []), *choice],
        subagents=build_subagents(tools, connectors, router, shell_gate, helper_calls),  # the library's own `task`: still built and still gated, even when hidden
        context_schema=RunContext,
    )
    return AgentRuntime(agent, sandbox, settings, prompt_guard, manager, report, router)
