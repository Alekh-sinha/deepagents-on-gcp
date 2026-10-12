"""Driving a built agent: start a run, answer an approval, resume. (The graph itself is assembled in builder.py.)"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

from langgraph.types import Command

from pinboard.agent.models import RunContext
from pinboard.engine.events import Emit, EventMapper
from pinboard.core.guardrails import wrap_untrusted

if TYPE_CHECKING:
    from pinboard.agent.builder import AgentRuntime

RECURSION_LIMIT = 100  # hard cap on agent steps per run (cost / runaway protection)


@dataclass
class RunResult:
    status: str  # completed | awaiting_approval | blocked
    thread_id: str
    output: str = ""
    pending: list[dict] = field(default_factory=list)


Approver = Callable[[list[dict]], list[dict]]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def _jsonable(value: Any) -> Any:
    """A copy of ``value`` that can be stored as JSON (anything odd becomes its text)."""
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


GENERIC_INTERRUPT = "interrupt"  # the action name of an interrupt that is not the human-in-the-loop middleware's


def _pending(result: dict) -> list[dict]:
    """Everything the graph is waiting on, one entry per question. Each carries the ``interrupt_id`` it must be answered under.

    * the human-in-the-loop middleware's payload (``action_requests`` + ``review_configs``) gives one entry per tool call, annotated with the
      decisions the reviewer may use;
    * ANY OTHER ``interrupt(value)`` anywhere in the graph gives one generic entry (name ``interrupt``, the value in ``args``) that is
      answered with free text (``respond``). Nothing a graph pauses on is ever silently dropped.
    """
    out: list[dict] = []
    for intr in result.get("__interrupt__", []) or []:
        value = getattr(intr, "value", intr)
        iid = getattr(intr, "id", None)
        if isinstance(value, dict) and isinstance(value.get("action_requests"), list):
            cfgs = value.get("review_configs") or []
            for i, req in enumerate(value["action_requests"]):
                req = dict(req)
                if i < len(cfgs) and isinstance(cfgs[i], dict) and cfgs[i].get("allowed_decisions"):
                    req["allowed_decisions"] = list(cfgs[i]["allowed_decisions"])
                if iid:
                    req["interrupt_id"] = iid
                out.append(req)
        else:
            entry = {"name": GENERIC_INTERRUPT, "args": {"value": _jsonable(value)}, "allowed_decisions": ["respond"]}
            if iid:
                entry["interrupt_id"] = iid
            out.append(entry)
    # One fixed order wherever the list is built from (the run result, the stream, the saved checkpoint): decisions are positional, so two
    # readers must never disagree about which answer belongs to which question. Python's sort is stable: requests of one interrupt keep their order.
    return sorted(out, key=lambda r: r.get("interrupt_id") or "")


def resume_value(pending: list[dict], decisions: list[dict]) -> Any:
    """What to hand to ``Command(resume=...)``: the decisions, grouped under the interrupt each one answers.

    LangGraph accepts a plain value only while ONE interrupt is pending; parallel helpers can each pause, and then every answer must be
    addressed by interrupt id. Entries recorded before ids existed (no ``interrupt_id``) fall back to the plain single-interrupt form."""
    if len(pending) != len(decisions):
        raise ValueError(f"{len(pending)} pending action(s) but {len(decisions)} decision(s)")
    if not pending or any(not p.get("interrupt_id") for p in pending):
        return {"decisions": list(decisions)}
    grouped: dict[str, list[dict]] = {}
    for p, d in zip(pending, decisions):
        grouped.setdefault(p["interrupt_id"], []).append(d)
    out: dict[str, Any] = {}
    for p in pending:
        iid = p["interrupt_id"]
        if iid in out:
            continue
        if p.get("name") == GENERIC_INTERRUPT:
            d = grouped[iid][0]
            out[iid] = d.get("message", "") if d.get("type") == "respond" else d.get("message", "rejected")
        else:
            out[iid] = {"decisions": grouped[iid]}
    return out


pending_actions = _pending  # public name for the service layer


def _context(rt: AgentRuntime, model: str | None) -> RunContext | None:
    """Validate the requested model against policy (PolicyDenied if not allowed) and build the run context."""
    if not model:
        return None
    if rt.router is None:
        raise ValueError("this runtime was built with a fixed model; per-request model selection is unavailable")
    return {"model": rt.router.resolve(model).id}


def drive(rt: AgentRuntime, payload: Any, thread_id: str, approver: Approver | None, model: str | None = None) -> RunResult:
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    ctx = _context(rt, model)
    while True:
        result = rt.agent.invoke(payload, config=cfg, context=ctx)
        pending = _pending(result)
        if not pending:
            last = result["messages"][-1] if result.get("messages") else None
            return RunResult("completed", thread_id, _text(last.content) if last else "")
        if approver is None:
            return RunResult("awaiting_approval", thread_id, pending=pending)
        payload = Command(resume=resume_value(pending, approver(pending)))


def run(rt: AgentRuntime, prompt: str, *, thread_id: str, approver: Approver | None = None, source: str = "user",
        model: str | None = None) -> RunResult:
    _context(rt, model)  # reject a disallowed model before anything else happens
    verdict = rt.prompt_guard.check(prompt)
    if not verdict.ok:
        return RunResult("blocked", thread_id, verdict.message())
    if source != "user":
        prompt = wrap_untrusted(prompt, source)
    return drive(rt, {"messages": [{"role": "user", "content": prompt}]}, thread_id, approver, model)


def resume(rt: AgentRuntime, thread_id: str, decisions: list[dict], approver: Approver | None = None, model: str | None = None) -> RunResult:
    """Continue a run that parked on an approval. Pass the same model the run started with."""
    state = rt.agent.get_state({"configurable": {"thread_id": thread_id}})
    pending = _pending({"__interrupt__": list(getattr(state, "interrupts", ()) or ())})
    return drive(rt, Command(resume=resume_value(pending, decisions) if pending else {"decisions": decisions}), thread_id, approver, model)


def stream_segment(rt: "AgentRuntime", payload: Any, thread_id: str, *, model: str | None = None, mapper: EventMapper | None = None) -> Iterator[Emit]:
    """Run one segment of a turn (a new message, a resume after an approval, or a continue from the checkpoint) and yield small events.
    The stream ends when the agent finishes or pauses on an approval (a ``_interrupt`` Emit comes last)."""
    mapper = mapper or EventMapper()
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    ctx = _context(rt, model)
    # "checkpoints" marks the END of a step: every parallel tool call of that step has finished and the conversation state is saved.
    # That is the only safe place to stop for a cancel (results are never half-recorded, the next message starts from a clean state).
    for ns, mode, data in rt.agent.stream(payload, config=cfg, context=ctx, stream_mode=["updates", "messages", "checkpoints"], subgraphs=True):
        yield from mapper.feed(ns, mode, data)
        if mode == "checkpoints":
            yield Emit("_boundary", {})
    yield from mapper.flush()


def final_text(rt: "AgentRuntime", thread_id: str) -> str:
    """The assistant's last message in the conversation, read from the checkpoint (complete, never clipped)."""
    state = rt.agent.get_state({"configurable": {"thread_id": thread_id}})
    msgs = (state.values or {}).get("messages") or []
    return _text(msgs[-1].content) if msgs and getattr(msgs[-1], "type", "") == "ai" else ""


def existing_messages(rt: "AgentRuntime", thread_id: str) -> list:
    """The conversation so far (from the checkpoint). Empty for a brand-new thread."""
    state = rt.agent.get_state({"configurable": {"thread_id": thread_id}})
    return list((state.values or {}).get("messages") or [])
