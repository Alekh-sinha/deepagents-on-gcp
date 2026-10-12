"""Tools that talk to the person instead of the outside world: the plan gate (submit_plan) and ask_user."""
from __future__ import annotations

from typing import Literal, TypedDict

from langchain_core.tools import tool

from pinboard.agent.gates import gate
from pinboard.core.policy import PolicyDenied

ASSIGNEES = ("assistant", "researcher", "coder", "mailer")
MAX_PLAN_TASKS = 12


class PlanTask(TypedDict):
    title: str
    detail: str
    assignee: Literal["assistant", "researcher", "coder", "mailer"]


def check_plan(summary: str, tasks: list[dict], **_ignored) -> list[dict]:
    """Raises PolicyDenied for an unusable plan (so no human is asked to approve nonsense)."""
    if not (summary or "").strip():
        raise PolicyDenied("plan.invalid", "a plan needs a one-sentence summary")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_PLAN_TASKS:
        raise PolicyDenied("plan.invalid", f"a plan needs 1-{MAX_PLAN_TASKS} tasks")
    out = []
    for t in tasks:
        if not isinstance(t, dict) or not str(t.get("title", "")).strip():
            raise PolicyDenied("plan.invalid", "every task needs a title")
        if t.get("assignee") not in ASSIGNEES:
            raise PolicyDenied("plan.invalid", f"assignee must be one of: {', '.join(ASSIGNEES)}")
        out.append({"title": str(t["title"]).strip(), "detail": str(t.get("detail", "")).strip(), "assignee": t["assignee"]})
    return out


def make_interaction_tools():
    """(tools, interrupt_on). Both tools are gated ALWAYS (independent of APPROVAL_MODE):
    submit_plan -> the person approves/edits/rejects the plan (approval also authorises the delegation it names);
    ask_user    -> the person answers; the answer comes back as the tool result (decision type 'respond')."""
    @tool
    def submit_plan(summary: str, tasks: list[PlanTask]) -> str:
        """Show the user your plan and wait for approval BEFORE doing multi-step work or anything with side effects (email, GitHub, files).
        `summary` is one sentence. `tasks` is an ordered list of {title, detail, assignee}; assignee is who does it:
        "assistant" (you), "researcher" (web research), "coder" (files, commands, GitHub) or "mailer" (email).
        Approval authorises those hand-offs. The user may edit the plan or reject it with feedback; if rejected, revise and submit again."""
        try:
            clean = check_plan(summary, tasks)
        except PolicyDenied as exc:
            return exc.message() + " Fix the plan and submit it again."
        lines = [f"{i}. [{t['assignee']}] {t['title']}" + (f": {t['detail']}" if t["detail"] else "") for i, t in enumerate(clean, 1)]
        return f"Plan approved by the user ({summary.strip()}). Carry it out in this order:\n" + "\n".join(lines)

    @tool
    def ask_user(question: str, options: list[str] | None = None) -> str:
        """Ask the user a question when you cannot continue without their answer (a missing detail, a choice between options). Do not ask what you can decide yourself.
        The user's answer is returned as the result of this tool."""
        return "The user did not answer."

    interrupt_on = {
        **gate({"submit_plan": check_plan}),  # an invalid plan is bounced back to the model without bothering the human
        "ask_user": {"allowed_decisions": ["respond"]},
    }
    return [submit_plan, ask_user], interrupt_on
