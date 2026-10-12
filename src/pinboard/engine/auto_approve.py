"""Automatic approval: a session can be switched to approve the agent's actions on the user's behalf, until the user changes it.

The setting lives on the session (``SessionRecord.auto_approve``) and is set through ``update_session`` / ``send_message`` /
``create_session`` (a frontend's "approve all" toggle). The engine applies it itself when a turn pauses (``RunService._auto_continue``);
no client has to be connected.

What it never does, whatever the setting:
  * answer a question (``ask_user``) or any graph interrupt that needs text: only a person can;
  * approve a kind of action the user did not switch on (e-mail, plan approval and unrecognised tools ("other") are OFF in the default "all");
  * approve a tool the policy lists under approvals.never_auto_tools;
  * approve a batch in which ANY action is not covered: the whole batch waits for the person;
  * go past ``max_per_turn`` approvals in one turn;
  * widen policy: the tools check recipients, repositories and branches again when they run.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pinboard.engine.errors import BadRequest

UNSET: Any = object()  # "the caller did not mention auto_approve": leave the session's setting alone

# Approval-card kinds (agent/approvals.py) a person may let the engine approve, in display order.
AUTO_KINDS = ("shell", "github", "other", "email", "plan", "mcp")
DEFAULT_KINDS = ("shell", "github")  # what "approve all" means: no e-mail, no plan approval, and nothing the engine does not recognise ("other")
NEVER_KINDS = ("question", "interrupt")       # answered by a person, always
_KEYS = {"all", "kinds", "max_per_turn"}


def normalise(value: Any, *, default_cap: int, hard_cap: int) -> dict | None:
    """User input -> the stored setting, or None for 'ask me every time'.

    ``false``/``null``/``{}`` = off; ``true`` or ``{"all": true}`` = the default kinds; ``{"kinds": [...]}`` = exactly those;
    ``max_per_turn`` (optional) is clamped to the policy limit."""
    if value is None or value is False or value == {}:
        return None
    if value is True:
        value = {"all": True}
    if not isinstance(value, dict):
        raise BadRequest("auto_approve must be true, false, or an object like {\"kinds\": [\"shell\", \"github\"], \"max_per_turn\": 10}")
    unknown = set(value) - _KEYS
    if unknown:
        raise BadRequest(f"auto_approve: unknown key(s) {', '.join(sorted(unknown))}; use all, kinds, max_per_turn")
    if "kinds" in value:
        kinds = value["kinds"]
        if not (isinstance(kinds, list) and kinds and all(isinstance(k, str) for k in kinds)):
            raise BadRequest("auto_approve.kinds must be a non-empty list of names")
        for k in kinds:
            if k in NEVER_KINDS:
                raise BadRequest(f"auto_approve: '{k}' can only be answered by a person and cannot be automatic")
            if k not in AUTO_KINDS:
                raise BadRequest(f"auto_approve: unknown kind '{k}'; choose from {', '.join(AUTO_KINDS)}")
        chosen = [k for k in AUTO_KINDS if k in kinds]
    elif value.get("all") is True:
        chosen = list(DEFAULT_KINDS)
    elif value.get("all") is False:
        return None
    else:
        raise BadRequest("auto_approve needs \"all\": true or a \"kinds\" list (send false to switch it off)")
    cap = value.get("max_per_turn", default_cap)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise BadRequest("auto_approve.max_per_turn must be a whole number of at least 1")
    return {"kinds": chosen, "max_per_turn": min(cap, hard_cap)}


def held_back(previews: list[dict], never_tools: Iterable[str] = ()) -> list[str]:
    """Tools in this batch that the policy says a person must always approve."""
    never = set(never_tools)
    return sorted({p["tool"] for p in previews if p.get("tool") in never})


def covers(setting: dict | None, previews: list[dict], never_tools: Iterable[str] = ()) -> bool:
    """True only if EVERY pending action is of a switched-on kind, may be approved as it stands, and is not on the policy's never-automatic list."""
    if not setting or not previews or held_back(previews, never_tools):
        return False
    kinds = set(setting.get("kinds") or ())
    return all(p.get("kind") in kinds and "approve" in (p.get("allowed_decisions") or ()) for p in previews)


def describe_options(hard_cap: int, default_cap: int, never_tools: Iterable[str] = ()) -> dict:
    """What a frontend may offer (part of get_config)."""
    return {"kinds": list(AUTO_KINDS), "default_kinds": list(DEFAULT_KINDS), "never": list(NEVER_KINDS), "never_tools": sorted(never_tools),
            "default_max_per_turn": default_cap, "max_per_turn_limit": hard_cap}
