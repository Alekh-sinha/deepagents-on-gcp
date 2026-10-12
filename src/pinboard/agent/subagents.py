"""The team: which helpers exist, what each is for, and which tools each one holds.

Least privilege lives here. Only the mailer can send email. GitHub is changed by ONE party, the orchestrator (the main agent, see
builder.py): it checks the helpers' work and then publishes it, so parallel coders can never push competing branches or pull requests.
Each agent is gated for exactly the tools it holds. To add or change a helper, edit build_subagents().
"""
from __future__ import annotations

from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT
from langchain.agents.middleware import ModelCallLimitMiddleware

from pinboard.agent.models import ModelRouter, ModelSelectionMiddleware
from pinboard.agent.prompts import CODER_PROMPT, MAILER_PROMPT, RESEARCHER_PROMPT
from pinboard.agent.tools.connectors import ConnectorTools


def model_aware(spec: dict, router: ModelRouter | None) -> dict:
    """Subagents do NOT inherit the main agent's middleware or run context (verified), so every subagent spec that
    should follow the user's model choice must carry the selection middleware itself."""
    if router is None:
        return spec
    return {**spec, "middleware": [*spec.get("middleware", []), ModelSelectionMiddleware(router)]}


DEFAULT_HELPER_MODEL_CALLS = 40


def bounded(spec: dict, max_calls: int) -> dict:
    """A helper runs in its own graph, and the step cap the parent runs under (runner.RECURSION_LIMIT) does NOT reach into it (verified: deepagents
    binds a limit of 9,999 on every graph). Without this a confused helper could loop for a very long time. At the limit it stops and reports."""
    return {**spec, "middleware": [*spec.get("middleware", []), ModelCallLimitMiddleware(run_limit=max_calls, exit_behavior="end")]}


def helper_specs(base_tools: list, connectors: ConnectorTools | None, shell_gate: dict | None = None) -> list[dict]:
    """The team as plain specs (no model, no limit): what each helper is for and which tools it holds. Used both by the library's own `task` tool
    (through build_subagents) and by the `delegate` tool (agent/delegation.py), so the two can never disagree about who may do what.

    Least privilege: only the mailer holds email tools; the coder holds GitHub READ tools only; the researcher holds only web search.
    The GitHub write tools belong to the main agent (builder.py). Each agent is gated for exactly the tools it holds
    (a spec's interrupt_on replaces the parent's), so nobody is ever asked to approve a call that could not run."""
    shell_gate = shell_gate or {}
    groups = (connectors.by_group if connectors else {})
    gates = (connectors.gates_by_group if connectors else {})
    specs = [
        {**GENERAL_PURPOSE_SUBAGENT, "tools": list(base_tools), "interrupt_on": dict(shell_gate)},
        {"name": "researcher", "description": "Searches the web and returns a short sourced summary. Cannot email, run commands or use GitHub.",
         "system_prompt": RESEARCHER_PROMPT, "tools": list(base_tools), "interrupt_on": dict(shell_gate)},
        {"name": "coder", "description": "Writes and tests files and runs commands in the sandbox, and can read GitHub repositories. It cannot change GitHub: the assistant "
                                         "publishes the finished files (branch, commit, pull request). Several coders can work in parallel on separate folders.",
         "system_prompt": CODER_PROMPT, "tools": list(groups.get("github_read", [])), "interrupt_on": dict(shell_gate)},
    ]
    if groups.get("email"):
        specs.append({"name": "mailer", "description": "Reads the assistant's inbox and sends email (to allowed recipients only, each send needs approval).",
                      "system_prompt": MAILER_PROMPT, "tools": list(groups["email"]), "interrupt_on": {**shell_gate, **gates.get("email", {})}})
    return specs


def build_subagents(base_tools: list, connectors: ConnectorTools | None, router: ModelRouter | None, shell_gate: dict | None = None,
                    max_model_calls: int = DEFAULT_HELPER_MODEL_CALLS) -> list[dict]:
    """The team in the shape the library's `task` tool wants: each helper follows the user's model and has a model-call limit."""
    return [bounded(model_aware(sp, router), max_model_calls) for sp in helper_specs(base_tools, connectors, shell_gate)]
