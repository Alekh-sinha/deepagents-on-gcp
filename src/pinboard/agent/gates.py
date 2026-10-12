"""Approval gates: which tool calls must wait for a person, and which decisions that person may give.

A gate is an entry of deepagents' interrupt_on. ``gate()`` builds the entries for write tools with a ``when`` predicate that
skips the question only when policy would refuse the exact arguments anyway (any other problem still asks a human: fail safe).
"""
from __future__ import annotations

from typing import Any, Callable

from pinboard.agent.tools.common import denied

ALLOWED_DECISIONS = ["approve", "edit", "reject"]


def shell_gate() -> dict[str, Any]:
    return {"execute": {"allowed_decisions": list(ALLOWED_DECISIONS)}}


def gate(static_checks: dict[str, Callable[..., Any]]) -> dict[str, Any]:
    """interrupt_on entries for write tools. ``when`` returns False (no question) only for policy-denied arguments."""
    return {
        name: {"allowed_decisions": list(ALLOWED_DECISIONS),
               "when": (lambda req, _c=check: not denied(_c, dict(req.tool_call.get("args") or {})))}
        for name, check in static_checks.items()
    }
