"""Email and GitHub tools assembled for the agent: which tools exist, grouped per helper, with their approval gates.

Write tools are ALWAYS gated by a human approval, independent of APPROVAL_MODE (which only governs the shell).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from pinboard.agent.gates import gate
from pinboard.agent.tools.common import Audit, noop_audit
from pinboard.agent.tools.effects import EffectLedger, InMemoryLedger
from pinboard.agent.tools.email_tools import make_email_tools
from pinboard.agent.tools.github_tools import make_github_tools
from pinboard.integrations.agentmail import AgentMailGateway
from pinboard.integrations.github import GitHubApp
from pinboard.core.policy import Policy

log = logging.getLogger(__name__)


@dataclass
class ConnectorTools:
    tools: list = field(default_factory=list)
    interrupt_on: dict[str, Any] = field(default_factory=dict)
    gated: set[str] = field(default_factory=set)
    skipped: dict[str, str] = field(default_factory=dict)  # connector -> why it was not enabled
    by_group: dict[str, list] = field(default_factory=dict)  # "email" / "github" (all) / "github_read" / "github_write" -> its tools
    gates_by_group: dict[str, dict] = field(default_factory=dict)  # the approval gates for exactly those tools

    def merge(self, other: "ConnectorTools") -> "ConnectorTools":
        return ConnectorTools([*self.tools, *other.tools], {**self.interrupt_on, **other.interrupt_on},
                              self.gated | other.gated, {**self.skipped, **other.skipped}, {**self.by_group, **other.by_group}, {**self.gates_by_group, **other.gates_by_group})


def email_connector(gateway: AgentMailGateway, policy: Policy, inbox_id: str, **kw) -> ConnectorTools:
    tools, checks = make_email_tools(gateway, policy, inbox_id, **kw)
    gates = gate(checks)
    return ConnectorTools(tools, gates, set(checks), by_group={"email": tools}, gates_by_group={"email": gates})


def github_connector(app: GitHubApp, policy: Policy, **kw) -> ConnectorTools:
    tools, checks = make_github_tools(app, policy, **kw)
    gates = gate(checks)
    reads = [t for t in tools if t.name not in checks]  # list/read: no approval, safe for any helper
    writes = [t for t in tools if t.name in checks]     # branch / commit / pull request: gated, held by the orchestrator only
    return ConnectorTools(tools, gates, set(checks), by_group={"github": tools, "github_read": reads, "github_write": writes},
                          gates_by_group={"github": gates, "github_read": {}, "github_write": gates})


def build_connector_tools(settings, vault, policy: Policy, *, read_file: Callable[[str], bytes] | None = None,
                          ledger: EffectLedger | None = None, audit: Audit = noop_audit) -> ConnectorTools:
    """Production wiring: enable each connector only if its credentials exist; say why not otherwise."""
    ledger = ledger or InMemoryLedger()
    common = dict(outputs_path=settings.outputs_path, read_file=read_file, ledger=ledger, audit=audit)
    result = ConnectorTools()

    key = vault.get("AGENTMAIL_API_KEY")
    if key:
        inbox = settings.agentmail_inbox_id or policy.email.from_inbox
        result = result.merge(email_connector(AgentMailGateway(key), policy, inbox, **common))
    else:
        result.skipped["email"] = "AGENTMAIL_API_KEY is not set"

    pem = vault.get("GITHUB_APP_PRIVATE_KEY")
    if settings.github_app_id and pem:
        app = GitHubApp(settings.github_app_id, pem, settings.github_installation_id)
        result = result.merge(github_connector(app, policy, **common))
    else:
        result.skipped["github"] = "GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY is not set"
    for name, why in result.skipped.items():
        log.warning("connector %s disabled: %s", name, why)
    return result
