"""Shared bits for the connector tools."""
from __future__ import annotations

from typing import Any, Callable

from pinboard.core.policy import PolicyDenied

Audit = Callable[[str, dict], None]


def noop_audit(event: str, data: dict) -> None:  # default when nobody is listening
    return None


def effect_key(config: dict | None, tool_call_id: str) -> str:
    """Stable per (conversation thread, tool call): a resumed turn replays the same call id, so this dedupes it."""
    thread = ((config or {}).get("configurable") or {}).get("thread_id") or "no-thread"
    return f"{thread}:{tool_call_id}"


def denied(check: Callable[..., Any], args: dict) -> bool:
    """For the approval ``when`` predicate: True only when policy would refuse these exact arguments, so nothing is worth
    asking a human about. Any OTHER problem (malformed args, bug) returns False, which means 'ask the human'; fail safe."""
    try:
        check(**args)
        return False
    except PolicyDenied:
        return True
    except Exception:  # noqa: BLE001
        return False


def refusal(exc: PolicyDenied) -> str:
    return exc.message() + " Do not retry the same request; tell the user what was refused and why."
