"""RunService: turns, sessions and the park-and-resume orchestration.

A TURN (a ``RunRecord``) is one user message and the agent's work on it. It runs in a background WORKER that
  1. takes a lease on the turn and renews it on a heartbeat (every write is fenced by the lease, so a replaced worker is harmless),
  2. streams the agent and appends small, capped events to the event log (clients follow the log with ``tail_turn``),
  3. ends by finishing, by PARKING on a human approval (exact tool + arguments saved as an approval), or by failing / being cancelled.
``decide()`` (possibly in another process after a restart) atomically records the decision and resumes the agent from the durable checkpoint.
A worker that disappears is found by the reaper (``maintenance()``), marked ``interrupted`` and resumed once automatically.

A SESSION is a conversation (one LangGraph thread). Its sandbox is reused by later turns and deleted after it has been idle for a while.
The legacy synchronous API (``start_run`` / ``decide`` on a run without a session) uses the same code with a throwaway sandbox.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Iterator

from langgraph.types import Command

from pinboard.agent import build_agent
from pinboard.agent.contract import load_builder
from pinboard.agent.approvals import describe_all
from pinboard.agent.runner import existing_messages, final_text, pending_actions, resume_value, stream_segment
from pinboard.engine import auto_approve as auto
from pinboard.engine.errors import BadRequest, Busy, Conflict, Forbidden, NotFound
from pinboard.engine.mcp_servers import DEFAULT_RESOLVER, McpMixin
from pinboard.engine.schedules import SchedulesMixin
from pinboard.mcp import McpClient
from pinboard.engine.events import EventMapper
from pinboard.engine.sessions import SessionsMixin
from pinboard.engine.spare_pool import SparePoolMixin, _in_thread
from pinboard.gcp.artifacts import Artifacts, NoArtifacts, make_blob_store
from pinboard.gcp.outputs import harvest
from pinboard.gcp.sandbox import delete_sandbox, probe_sandbox
from pinboard.gcp.sessions import REPLACED_NOTE, NoSessionSandboxes, SessionSandboxes
from pinboard.gcp.spares import create_spare_sandbox
from pinboard.core.guardrails import PromptGuard, wrap_untrusted
from pinboard.core.policy import Features, Policy, PolicyDenied, PolicyError, load_policy
from pinboard.state import (ApprovalRecord, RunRecord, SessionRecord, StateStore, new_approval_id, new_run_id,
                            utcnow)
from pinboard.engine.worker import InlineExecutor, LeaseKeeper, TurnContext

log = logging.getLogger(__name__)

_DECISION_TYPES = ("approve", "reject", "edit", "respond")


_CANCEL_POLL_SECONDS = 0.5




@dataclass
class Outcome:
    kind: str  # completed | approval | cancelled | lost
    text: str = ""
    pending: list[dict] = field(default_factory=list)
    timing: dict = field(default_factory=dict)  # where this segment's time went: setup, wait for the model's first output, streaming


class RunService(SessionsMixin, SparePoolMixin, McpMixin, SchedulesMixin):
    def __init__(
        self,
        settings,
        vault,
        store: StateStore,
        checkpointer,
        *,
        build: Callable[..., Any] | None = None,
        storage_client=None,
        sandbox_deleter: Callable[[str], None] | None = None,
        clock: Callable[[], Any] = utcnow,
        policy: Policy | None = None,
        executor=None,
        ledger=None,
        artifacts: Artifacts | None = None,
        sandbox_probe: Callable[[str], bool] | None = None,
        spare_factory: Callable[[], str] | None = None,
        spare_runner: Callable[[Callable[[], None]], None] | None = None,
        lease_ttl: float = 45.0,
        lease_interval: float = 15.0,
        lease_thread: bool = True,
        stale_grace: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        mcp_client=None,
        mcp_resolver=None,
    ) -> None:
        self.settings, self.vault, self.store, self.checkpointer = settings, vault, store, checkpointer
        self.storage_client, self.clock, self.ledger = storage_client, clock, ledger
        self.prompt_guard = PromptGuard(settings.max_prompt_chars)
        self._build = build or (load_builder(settings.agent_builder) if settings.agent_builder else build_agent)  # AGENT_BUILDER: run another agent (agent/contract.py)
        self._delete_sandbox = sandbox_deleter or (lambda name: delete_sandbox(settings, name))
        self._policy = policy
        self.executor = executor or InlineExecutor()
        self.lease_ttl, self.lease_interval, self.lease_thread, self.stale_grace = lease_ttl, lease_interval, lease_thread, stale_grace
        self._sleep, self._mono = sleep, monotonic
        self._probe = sandbox_probe or (lambda name: probe_sandbox(settings, name))
        self._spare_factory = spare_factory or (lambda: create_spare_sandbox(settings, storage_client=storage_client))
        self._spare_runner = spare_runner or _in_thread  # tests run it inline
        # The ONE place that looks at the policy's feature switches (both on by default). A switched-off feature gets a stand-in with the same methods
        # that does nothing, so the rest of the service never asks whether a feature exists.
        features = policy.features if policy is not None else Features()
        self.artifacts = artifacts if artifacts is not None else (
            Artifacts(store, make_blob_store(settings.artifacts_uri, storage_client), lambda: self.clock()) if features.artifacts else NoArtifacts())
        self.sandboxes = (SessionSandboxes(store, settings.sandbox_ttl_seconds, self._delete_sandbox, lambda: self.clock())
                          if features.sandbox else NoSessionSandboxes())
        self._harvest = harvest if features.sandbox else (lambda *args, **kwargs: ([], []))  # legacy one-shot runs copy their output folder out of the sandbox
        self.mcp_client = mcp_client or McpClient()           # injectable: tests use a fake or a local server
        self._mcp_resolver = mcp_resolver or DEFAULT_RESOLVER  # how a server's host name is resolved before it is allowed

    def authorize_user(self, user_id: str | None) -> str:
        """Who is acting. No user (or the configured owner) is the bearer-token owner; any other user, such as the e-mail the frontend
        passes from its sign-in, must be on the policy's ``allowed_users`` list."""
        if not user_id or user_id == self.settings.user_id:
            return self.settings.user_id
        try:
            self.policy.check_user(user_id)
        except PolicyDenied as exc:
            raise Forbidden(exc.message()) from exc
        except PolicyError as exc:
            raise Forbidden("no usable policy file, so only the owner account is allowed") from exc
        return user_id.strip().lower()

    @property
    def policy(self) -> Policy:
        if self._policy is None:
            self._policy = load_policy()
        return self._policy



    # ═════════════════════════ turns: starting ═════════════════════════
    def send_message(self, session_id: str, text: str, *, user_id: str | None = None, model: str | None = None, source: str = "user",
                     auto_approve: Any = auto.UNSET) -> RunRecord:
        """Start a turn in a session. Returns at once with the turn (status 'running'); follow it with ``tail_turn``.
        ``auto_approve`` (when given) replaces the session's automatic-approval setting before the turn starts; see engine/auto_approve.py."""
        user_id = user_id or self.settings.user_id
        session = self._session(session_id, user_id)
        new_auto = auto.UNSET if auto_approve is auto.UNSET else self._auto_setting(auto_approve)  # a bad value is refused before anything happens
        eff_model = self._model(model) or session.model
        run_id, now = new_run_id(), self.clock()

        verdict = self.prompt_guard.check(text)
        if not verdict.ok:  # refuse before spending anything on a sandbox
            run = RunRecord(id=run_id, thread_id=session.thread_id, user_id=user_id, status="blocked", source=source, prompt=(text or "")[:500],
                            output=verdict.message(), session_id=session.id, model=eff_model)
            self.store.create_run(run)
            self._emit(run.id, "user_message", {"text": (text or "")[:2000], "turn_id": run.id})
            self._emit(run.id, "turn_error", {"status": "blocked", "error": verdict.message()})
            self.store.add_audit(user_id, run_id, "run.blocked", {"rule": verdict.rule, "reason": verdict.reason}, now)
            return run

        if self.store.active_run_for_thread(session.thread_id):
            raise Conflict("this session already has a turn in progress or awaiting approval")
        if not self.executor.has_capacity():
            raise Busy("too many turns are running right now; try again in a moment")

        if new_auto is not auto.UNSET:
            self.store.update_session(session.id, auto_approve=new_auto)
        run = RunRecord(id=run_id, thread_id=session.thread_id, user_id=user_id, status="running", source=source, prompt=text,
                        session_id=session.id, model=eff_model)
        self.store.create_run(run)
        self._emit(run.id, "user_message", {"text": text, "turn_id": run.id})
        self.store.add_audit(user_id, run_id, "run.started", {"source": source, "thread_id": session.thread_id, "session_id": session.id}, now)
        self.store.update_session(session.id, last_active_at=now, **({} if session.title else {"title": text.strip().replace("\n", " ")[:60]}))
        if not self.executor.try_submit(lambda: self._run_turn(run_id, "start", {"text": text})):
            self.store.update_run(run_id, status="cancelled", error="server busy")
            self._emit(run_id, "turn_error", {"status": "cancelled", "error": "server busy"})
            raise Busy("too many turns are running right now; try again in a moment")
        return self.store.get_run(run_id)

    def start_run(self, prompt: str, *, user_id: str | None = None, source: str = "user", thread_id: str | None = None) -> RunRecord:
        """Legacy synchronous API: one run with its own throwaway sandbox, finished (or parked on an approval) when this returns."""
        user_id = user_id or self.settings.user_id
        run_id = new_run_id()
        thread = thread_id or run_id

        verdict = self.prompt_guard.check(prompt)
        if not verdict.ok:
            run = RunRecord(id=run_id, thread_id=thread, user_id=user_id, status="blocked", source=source,
                            prompt=(prompt or "")[:500], output=verdict.message())
            self.store.create_run(run)
            self.store.add_audit(user_id, run_id, "run.blocked", {"rule": verdict.rule, "reason": verdict.reason}, self.clock())
            return run

        if thread_id and self.store.active_run_for_thread(thread_id):
            raise Conflict("this thread already has a run in progress or awaiting approval")

        run = RunRecord(id=run_id, thread_id=thread, user_id=user_id, status="running", source=source, prompt=prompt)
        self.store.create_run(run)
        self._emit(run.id, "user_message", {"text": prompt, "turn_id": run.id})
        self.store.add_audit(user_id, run_id, "run.started", {"source": source, "thread_id": thread}, self.clock())
        self._run_turn(run_id, "start", {"text": prompt})
        return self.store.get_run(run_id)

    # ═════════════════════════ turns: deciding, cancelling, resuming ═════════════════════════
    def decide(self, approval_id: str, decision: str | None = None, *, user_id: str | None = None,
               message: str | None = None, decisions: list[dict] | None = None) -> RunRecord:
        """Record the human decision and resume the turn. Session turns resume in the background (follow with ``tail_turn``);
        legacy runs resume synchronously."""
        user_id = user_id or self.settings.user_id
        appr = self.store.get_approval(approval_id)
        if appr is None or appr.user_id != user_id:
            raise NotFound("approval not found")
        dec_list = self._build_decisions(appr, decision, message, decisions)
        status = "rejected" if all(d["type"] == "reject" for d in dec_list) else "approved"

        now = self.clock()
        decided = self.store.decide_approval(approval_id, status, {"decisions": dec_list}, user_id, now)
        if decided is None:
            raise Conflict("approval is no longer pending (already decided or expired)")
        run = self.store.get_run(appr.run_id)
        resume = resume_value(appr.action_requests, dec_list)
        self.store.add_audit(user_id, run.id, f"approval.{status}", {"approval_id": approval_id, "decisions": dec_list}, now)
        if not self.store.claim_run(run.id, "awaiting_approval", "running", now):
            raise Conflict("run is not awaiting approval")
        self._emit(run.id, "approval_decided", {"approval_id": approval_id, "status": status,
                                                "decisions": [{"type": d["type"], **({"message": str(d["message"])[:500]} if d.get("message") else {})} for d in dec_list]})
        if run.session_id:
            self.store.update_session(run.session_id, last_active_at=now)
            self.executor.submit(lambda: self._run_turn(run.id, "resume", resume))  # an admitted turn is always allowed to continue
        else:
            self._run_turn(run.id, "resume", resume)
        return self.store.get_run(run.id)

    def _build_decisions(self, appr: ApprovalRecord, decision: str | None, message: str | None, decisions: list[dict] | None) -> list[dict]:
        n = len(appr.action_requests)

        def allowed_for(i: int) -> list[str]:
            return list(appr.action_requests[i].get("allowed_decisions") or ["approve", "edit", "reject"])

        def check(i: int, d: dict) -> None:
            if d["type"] not in allowed_for(i):
                raise BadRequest(f"'{d['type']}' is not allowed for {appr.action_requests[i].get('name')}; allowed: {', '.join(allowed_for(i))}")
            if d["type"] == "respond" and not (isinstance(d.get("message"), str) and d["message"].strip()):
                raise BadRequest("a respond decision needs a non-empty message (the answer)")

        if decisions is not None:
            if len(decisions) != n:
                raise BadRequest(f"expected {n} decision(s), got {len(decisions)}")
            for i, (d, req) in enumerate(zip(decisions, appr.action_requests)):
                if not isinstance(d, dict) or d.get("type") not in _DECISION_TYPES:
                    raise BadRequest(f"each decision needs a type in {_DECISION_TYPES}")
                if d["type"] == "edit":
                    edited = d.get("edited_action")
                    if not (isinstance(edited, dict) and isinstance(edited.get("args"), dict)):
                        raise BadRequest("an edit decision needs edited_action with an args object")
                    if edited.get("name") != req["name"]:
                        raise BadRequest("an edit may change the arguments but not which tool is called")
                check(i, d)
            return decisions
        if decision not in ("approve", "reject"):
            raise BadRequest("decision must be 'approve' or 'reject' (answer a question with decisions=[{\"type\": \"respond\", \"message\": ...}])")
        one: dict = {"type": decision}
        if decision == "reject" and message:
            one["message"] = message
        out = [dict(one) for _ in range(n)]
        for i, d in enumerate(out):
            check(i, d)
        return out

    def cancel_turn(self, run_id: str, *, user_id: str | None = None) -> RunRecord:
        """Stop a turn. A running turn stops at its next step (a command already executing finishes first); a waiting or interrupted turn stops now."""
        run = self.get_turn(run_id, user_id=user_id)
        now = self.clock()
        if run.status == "running":
            self.store.request_cancel(run.id)
            self.store.add_audit(run.user_id, run.id, "run.cancel_requested", {}, now)
            return self.store.get_run(run.id)
        if run.status in ("awaiting_approval", "interrupted"):
            for a in self.store.list_approvals(run.user_id, "pending"):
                if a.run_id == run.id:
                    self.store.decide_approval(a.id, "rejected", {"cancelled": True}, run.user_id, now)
            if not self.store.claim_run(run.id, run.status, "cancelled", now):
                raise Conflict("the turn changed state; try again")
            self._emit(run.id, "turn_done", {"status": "cancelled"})
            self.store.add_audit(run.user_id, run.id, "run.cancelled", {}, now)
            if not run.session_id and run.sandbox_name:
                self._delete_sandbox(run.sandbox_name)
                self.store.update_run(run.id, sandbox_name=None)
            return self.store.get_run(run.id)
        raise Conflict(f"a {run.status} turn cannot be cancelled")

    def resume_turn(self, run_id: str, *, user_id: str | None = None) -> RunRecord:
        """Resume an interrupted turn from its last checkpoint (the step that was in flight is repeated; side-effecting tools are idempotent)."""
        run = self.get_turn(run_id, user_id=user_id)
        if run.status != "interrupted":
            raise Conflict("only an interrupted turn can be resumed")
        return self._relaunch(run, auto=False)

    def _relaunch(self, run: RunRecord, *, auto: bool) -> RunRecord:
        if auto:
            self.store.update_run(run.id, resume_count=run.resume_count + 1)
        if not self.store.claim_run(run.id, "interrupted", "running", self.clock()):
            raise Conflict("the turn is no longer interrupted")
        self.store.add_audit(run.user_id, run.id, "run.resumed", {"auto": auto}, self.clock())
        self._emit(run.id, "notice", {"text": "Resuming after an interruption." if auto else "Resumed by the user."})
        if run.session_id:
            self.executor.submit(lambda: self._run_turn(run.id, "continue", None))
        else:
            self._run_turn(run.id, "continue", None)
        return self.store.get_run(run.id)

    # ═════════════════════════ turns: the worker ═════════════════════════
    def _emit(self, run_id: str, type: str, payload: dict) -> None:
        """Append an event from the service itself (not from a worker): unfenced."""
        self.store.append_event(run_id, None, type, payload, self.clock())

    def _runtime(self, run: RunRecord, adopt: str | None, **sandbox_options):
        def on_block(command: str, verdict) -> None:
            self.store.add_audit(run.user_id, run.id, "command.blocked",
                                 {"rule": verdict.rule, "reason": verdict.reason, "command": command[:500]}, self.clock())

        def audit(event: str, data: dict) -> None:
            self.store.add_audit(run.user_id, run.id, event, data, self.clock())

        return self._build(self.settings, self.vault, checkpointer=self.checkpointer, adopt_sandbox=adopt, on_block=on_block,
                           storage_client=self.storage_client, ledger=self.ledger, audit=audit, artifacts=self.artifacts,
                           user_id=run.user_id, session_id=run.session_id, mcp=self.mcp_tools_for(run.user_id, audit), **sandbox_options)

    def _open(self, run: RunRecord, session: SessionRecord | None, ctx: TurnContext):
        """Build the agent. Returns (runtime, replaced): replaced means this session had earlier work whose sandbox is gone."""
        if session is None:  # legacy run: attach to the sandbox it parked on, else create a throwaway one
            return self._runtime(run, run.sandbox_name), False
        adopt = self.sandboxes.adoption(session)
        t_probe = self._mono()
        alive = adopt is None or self._probe(adopt)
        ctx.timing["probe_seconds"] = round(self._mono() - t_probe, 2)  # asking the platform whether the recorded sandbox still answers
        if adopt is not None and not alive:  # the platform may have lost it before its recorded expiry
            log.warning("sandbox %s of session %s is gone; a new one will be created when it is first needed", adopt, session.id)
            self.sandboxes.forget(session.id)
            adopt = None
        replaced = self.sandboxes.was_replaced(session, adopt)  # only if this session had a sandbox before (earlier chat-only turns lost nothing)
        # The sandbox is created lazily, the first time a tool needs it; the session records it at that moment.
        t_build = self._mono()
        rt = self._runtime(run, adopt, **self.sandboxes.build_options(session, lambda: self._claim_spare(session)))
        ctx.timing["build_seconds"] = round(self._mono() - t_build, 2)  # secrets, tools, the agent graph and the model client
        if replaced:
            ctx.emit("notice", {"text": "The sandbox was replaced, so files from earlier turns are gone."})
        return rt, replaced

    def _payload(self, run: RunRecord, kind: str, payload: Any, replaced: bool):
        if kind == "start":
            text = payload["text"]
            if run.source != "user":
                text = wrap_untrusted(text, run.source)
            if replaced:
                text = REPLACED_NOTE + text
            return {"messages": [{"role": "user", "content": text}]}
        if kind == "resume":
            return Command(resume=payload)  # already addressed by interrupt id (see runner.resume_value)
        return None  # continue from the last checkpoint

    def _run_turn(self, run_id: str, kind: str, payload: Any = None) -> None:
        """Body of the background worker: one segment of a turn (``start`` a message, ``resume`` after a decision, ``continue`` after an interruption)."""
        run = self.store.get_run(run_id)
        if run is None or run.status != "running":
            return
        lease_id = uuid.uuid4().hex
        if not self.store.acquire_lease(run.id, lease_id, self.clock(), self.lease_ttl):
            return  # another worker owns this turn
        ctx = TurnContext(self.store, run.id, lease_id, self.clock)
        keeper = LeaseKeeper(self.store, run.id, lease_id, self.lease_ttl, self.lease_interval, self.clock, start_thread=self.lease_thread).start()
        session = self.store.get_session(run.session_id) if run.session_id else None
        rt = None
        try:
            ctx.emit("turn_started", {"kind": kind, "model": run.model, "resume_count": run.resume_count})
            t_start = self._mono()
            rt, replaced = self._open(run, session, ctx)
            setup = round(self._mono() - t_start, 2)  # building the agent: secrets, tools, graph, model client
            out = self._drive(run, rt, ctx, keeper, kind, payload, replaced)
            out = self._auto_continue(run, session, rt, ctx, keeper, out)
            out.timing = {"setup_seconds": setup, **ctx.timing, **out.timing, "total_seconds": round(self._mono() - t_start, 2)}
            self._settle(run, session, rt, ctx, out)
        except Exception as exc:  # noqa: BLE001
            log.exception("turn %s failed", run.id)
            self._settle_failure(run, session, rt, ctx, exc)
        finally:
            keeper.stop()
            self.store.release_lease(run.id, lease_id)

    def _auto_continue(self, run: RunRecord, session: SessionRecord | None, rt, ctx: TurnContext, keeper: LeaseKeeper, out: Outcome) -> Outcome:
        """While the session is on automatic approval and the turn paused only on things it covers, approve them here and carry on in the same
        turn (same worker, same lease, same sandbox). Anything else, or the per-turn limit, parks the turn for a person as usual."""
        approved = 0
        while out.kind == "approval" and session is not None:
            fresh = self.store.get_session(session.id)  # read every time: the user may have switched it off while the turn runs
            setting = fresh.auto_approve if fresh else None
            previews = describe_all(out.pending)
            never = self.policy.never_auto_tools
            if not auto.covers(setting, previews, never):
                if auto.covers(setting, previews):  # it would have been automatic but for the policy's never-automatic list: say why the turn stopped
                    ctx.emit("notice", {"text": f"{', '.join(auto.held_back(previews, never))} always needs your approval (policy), so automatic approval stopped here."})
                break
            if approved + len(out.pending) > setting["max_per_turn"]:
                ctx.emit("notice", {"text": f"Automatic approval paused after {approved} approvals in this turn (limit {setting['max_per_turn']}). Please review."})
                break
            current = self.store.get_run(run.id)
            if current is not None and current.cancel_requested:
                return Outcome("cancelled", timing=out.timing)
            now = self.clock()
            decisions = [{"type": "approve"} for _ in out.pending]
            record = ApprovalRecord(id=new_approval_id(), run_id=run.id, user_id=run.user_id, action_requests=out.pending, status="approved",
                                    decision={"decisions": decisions}, decided_by="auto", previews=previews, expires_at=now, decided_at=now)
            self.store.create_approval(record)
            self.store.add_audit(run.user_id, run.id, "approval.auto", {"approval_id": record.id, "actions": out.pending, "setting": setting}, now)
            if not ctx.emit("approval_auto", {"approval_id": record.id, "items": previews}):
                return Outcome("lost", timing=out.timing)
            approved += len(decisions)
            nxt = self._drive(run, rt, ctx, keeper, "resume", resume_value(out.pending, decisions), False)
            nxt.timing = {**out.timing, **nxt.timing, "auto_approved": approved}
            out = nxt
        return out

    def _drive(self, run: RunRecord, rt, ctx: TurnContext, keeper: LeaseKeeper, kind: str, payload: Any, replaced: bool) -> Outcome:
        t0 = self._mono()
        # Nothing that already happened may be announced again: not what an earlier segment of this turn showed, and not the earlier
        # turns of the conversation (the agent's start-up steps report the whole message list again).
        mapper = EventMapper().prime_messages(existing_messages(rt, run.thread_id))
        if kind != "start":
            mapper.prime(self.store.list_events(run.id, 0, limit=100_000))
        pending: list[dict] = []
        t_stream = last_poll = self._mono()
        first_output: list[float] = []

        def timing() -> dict:
            end = self._mono()
            return {"prep_seconds": round(t_stream - t0, 2), "first_output_seconds": first_output[0] if first_output else None,
                    "stream_seconds": round(end - t_stream, 2)}

        for em in stream_segment(rt, self._payload(run, kind, payload, replaced), run.thread_id, model=run.model, mapper=mapper):
            if em.type == "_boundary":  # between stream items: every result of a step has been recorded, so stopping here loses none
                now = self._mono()
                if now - last_poll >= _CANCEL_POLL_SECONDS:
                    last_poll = now
                    fresh = self.store.get_run(run.id)
                    if fresh is not None and fresh.cancel_requested:
                        return Outcome("cancelled", timing=timing())
                continue
            if not first_output:
                first_output.append(round(self._mono() - t_stream, 2))  # how long the model (and the graph before it) took to produce anything
            if ctx.lost or keeper.lost.is_set():
                return Outcome("lost", timing=timing())
            if em.type == "_compact":
                self.store.delete_token_events(run.id, em.payload["message_id"])
            elif em.type == "_interrupt":
                pending += pending_actions({"__interrupt__": em.payload["raw"]})
            elif not ctx.emit(em.type, em.payload):
                return Outcome("lost", timing=timing())
        if ctx.lost or keeper.lost.is_set():
            return Outcome("lost", timing=timing())
        if pending:
            return Outcome("approval", pending=pending, timing=timing())
        return Outcome("completed", text=final_text(rt, run.thread_id), timing=timing())

    def _capture(self, run: RunRecord, session: SessionRecord | None, rt, ctx: TurnContext) -> list:
        """Save the files in the output folder for the user (session turns only; never fails a turn). Returns the new artifacts."""
        sb = rt.sandbox
        if session is None or self.artifacts is None or not getattr(sb, "is_open", True):
            return []  # a sandbox that was never used has nothing to save (and must not be created just to look)
        try:
            root = getattr(sb, "cwd", self.settings.sandbox_cwd).rstrip("/") + "/out"
            created, skipped = self.artifacts.capture(sb, root, user_id=run.user_id, session_id=session.id, run_id=run.id)
            for a in created:
                ctx.emit("artifact", {"artifact_id": a.id, **{k: v for k, v in a.to_dict().items() if k in ("name", "path", "size", "content_type")}})
            if skipped:
                ctx.emit("notice", {"text": "Some files were not saved: " + "; ".join(skipped[:5])})
            if created:
                self.store.add_audit(run.user_id, run.id, "artifacts.saved", {"files": [a.path for a in created]}, self.clock())
            return created
        except Exception:  # noqa: BLE001
            log.exception("saving artifacts failed for turn %s", run.id)
            return []

    def _release(self, rt, session: SessionRecord | None) -> None:
        """A session keeps its sandbox between turns; a legacy run deletes its throwaway one."""
        if session is not None:
            rt.detach()
        else:
            rt.close()

    def _settle(self, run: RunRecord, session: SessionRecord | None, rt, ctx: TurnContext, out: Outcome) -> None:
        now = self.clock()
        if out.kind == "lost" or ctx.lost:  # someone else owns the turn now: touch nothing, and never delete a sandbox we no longer own
            rt.detach()
            return
        if session is not None:
            self.store.update_session(session.id, last_active_at=now)

        captured: list = []
        if out.kind in ("approval", "cancelled", "completed"):
            captured = self._capture(run, session, rt, ctx)  # before the terminal event, so a client sees the files appear first

        if out.kind == "approval":
            for old in self.store.list_approvals(run.user_id, "pending"):  # a re-raised pause supersedes an older one for this turn
                if old.run_id == run.id:
                    self.store.decide_approval(old.id, "expired", {"superseded": True}, "system", now)
            approval = ApprovalRecord(id=new_approval_id(), run_id=run.id, user_id=run.user_id, action_requests=out.pending,
                                      previews=describe_all(out.pending), expires_at=now + timedelta(seconds=self.settings.approval_ttl_seconds))
            self.store.create_approval(approval)
            if not ctx.emit("interrupt", {"approval_id": approval.id, "expires_at": approval.expires_at.isoformat(), "items": approval.previews, "timing": out.timing}):
                rt.detach()
                return
            self.store.update_run(run.id, status="awaiting_approval", output="", sandbox_name=None if session else rt.sandbox.id)
            self.store.add_audit(run.user_id, run.id, "approval.requested",
                                 {"approval_id": approval.id, "actions": out.pending, "expires_at": approval.expires_at.isoformat()}, now)
            rt.detach()  # only after the DB writes succeeded: keep the sandbox alive for the resume
            return

        if out.kind == "cancelled":
            ctx.emit("turn_done", {"status": "cancelled", "timing": out.timing})
            self.store.update_run(run.id, status="cancelled", sandbox_name=None)
            self.store.add_audit(run.user_id, run.id, "run.cancelled", {}, now)
            self._release(rt, session)
            return

        saved: list[str] = [a.path for a in captured]  # a session turn's deliverables are its artifacts
        skipped: list[str] = []
        if session is None:  # legacy run: copy the output folder next to the server (or to OUTPUTS_GCS_URI)
            try:
                saved, skipped = self._harvest(rt.sandbox, self.settings, run.id, self.storage_client, local=not self.settings.outputs_gcs_uri)
            except Exception as exc:  # noqa: BLE001  losing a deliverable is bad, but must not turn a finished run into a failure
                log.exception("harvesting outputs failed for run %s", run.id)
                skipped = [f"harvest failed: {exc}"]
        ctx.emit("turn_done", {"status": "completed", "chars": len(out.text), "outputs": saved, "timing": out.timing})
        self.store.update_run(run.id, status="completed", output=out.text, outputs=saved, sandbox_name=None, error="")
        self.store.add_audit(run.user_id, run.id, "run.completed", {"outputs": saved, "skipped": skipped}, now)
        self._release(rt, session)

    def _settle_failure(self, run: RunRecord, session: SessionRecord | None, rt, ctx: TurnContext, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"[:500]
        try:
            if rt is not None and not ctx.lost:
                self._capture(run, session, rt, ctx)
            if ctx.lost or not ctx.emit("turn_error", {"status": "failed", "error": msg}):
                if rt is not None:
                    rt.detach()
                return
            self.store.update_run(run.id, status="failed", error=msg, sandbox_name=None)
            self.store.add_audit(run.user_id, run.id, "run.failed", {"error": msg}, self.clock())
            if session is not None:
                self.store.update_session(session.id, last_active_at=self.clock())
        finally:
            if rt is not None and not ctx.lost:
                self._release(rt, session)
            elif rt is None and session is None and run.sandbox_name:
                self._delete_sandbox(run.sandbox_name)  # legacy resume that could not even attach: do not leak the parked sandbox

    # ═════════════════════════ following a turn ═════════════════════════
    def get_turn(self, run_id: str, *, user_id: str | None = None) -> RunRecord:
        run = self.get_run(run_id, user_id=user_id)
        return self._reap(run)[0] if run.status == "running" else run

    def tail_turn(self, run_id: str, after_seq: int = 0, *, user_id: str | None = None, heartbeat_seconds: float = 10.0,
                  max_seconds: float = 270.0, poll_seconds: float = 0.5) -> Iterator[dict]:
        """Follow a turn's event log from ``after_seq``. Yields events (with ``seq``), a ``heartbeat`` after each quiet stretch, and finally
        either nothing more (the turn stopped running: finished, parked, interrupted...) or ``reconnect`` (stream time is up: call again with ``after_seq``)."""
        run = self.get_turn(run_id, user_id=user_id)
        started = last_sent = self._mono()
        seq = after_seq
        while True:
            batch = self.store.list_events(run_id, seq)
            if batch:
                for e in batch:
                    seq = e["seq"]
                    yield e
                last_sent = self._mono()
                continue
            run = self.store.get_run(run_id)
            if run is None or run.status != "running":
                if self.store.list_events(run_id, seq):  # events written just before the status changed
                    continue
                return
            now = self._mono()
            if now - started >= max_seconds:
                yield {"type": "reconnect", "after_seq": seq}
                return
            if now - last_sent >= heartbeat_seconds:
                yield {"type": "heartbeat", "after_seq": seq}
                last_sent = now
            self._sleep(poll_seconds)
            run = self._reap(run)[0]

    # ═════════════════════════ maintenance & reads ═════════════════════════
    def _reap(self, run: RunRecord) -> tuple[RunRecord, bool]:
        """If this running turn's worker has gone quiet, mark it interrupted and (for session turns, within the policy limit) resume it once.
        Returns (turn as it is now, whether it was resumed)."""
        if run.status != "running" or run.id not in {r.id for r in self.store.stale_runs(self.clock(), self.stale_grace)}:
            return run, False
        if not self.store.mark_interrupted(run.id, self.clock(), self.stale_grace):
            return self.store.get_run(run.id), False
        self._emit(run.id, "turn_error", {"status": "interrupted", "resumable": True, "error": "the worker handling this turn stopped responding"})
        self.store.add_audit(run.user_id, run.id, "run.interrupted", {"resume_count": run.resume_count}, self.clock())
        fresh = self.store.get_run(run.id)
        if fresh.session_id and fresh.resume_count < self.policy.limits.max_auto_resumes:
            return self._relaunch(fresh, auto=True), True
        return fresh, False

    def expire_stale(self) -> int:
        """Expire approvals past their deadline. A legacy run frees its parked sandbox; a session keeps its sandbox (the idle sweep handles it)."""
        now = self.clock()
        expired = self.store.expire_approvals(now)
        for appr in expired:
            run = self.store.get_run(appr.run_id)
            if run and run.status == "awaiting_approval":
                if run.sandbox_name:
                    self._delete_sandbox(run.sandbox_name)
                self.store.update_run(run.id, status="expired", error="approval expired before a decision", sandbox_name=None)
                self._emit(run.id, "turn_error", {"status": "expired", "error": "the approval expired before a decision"})
                self.store.add_audit(run.user_id, run.id, "approval.expired", {"approval_id": appr.id}, now)
        return len(expired)

    def maintenance(self) -> dict:
        """Periodic housekeeping (call from a scheduler every few minutes): expire approvals, find lost workers, delete idle sandboxes."""
        report = {"approvals_expired": self.expire_stale(), "turns_interrupted": 0, "turns_resumed": 0, "sandboxes_deleted": 0, "spares_deleted": 0,
                  "schedules_fired": 0, "schedules_skipped": 0}
        for run in self.store.stale_runs(self.clock(), self.stale_grace):
            after, resumed = self._reap(run)
            if resumed:
                report["turns_resumed"] += 1
            elif after.status == "interrupted":
                report["turns_interrupted"] += 1
        try:
            report["sandboxes_deleted"] = self.sandboxes.sweep(self.policy.limits.sandbox_idle_minutes)
            report["spares_deleted"] = self._sweep_spares()
        except PolicyError:  # no policy file: a legacy-only deployment has no session sandboxes to sweep
            pass
        report.update(self.fire_due_schedules())   # last: housekeeping of old work first, then new work
        return report

    def get_run(self, run_id: str, *, user_id: str | None = None) -> RunRecord:
        run = self.store.get_run(run_id)
        if run is None or run.user_id != (user_id or self.settings.user_id):
            raise NotFound("run not found")
        return run

    def list_approvals(self, *, status: str | None = None, user_id: str | None = None, session_id: str | None = None) -> list[ApprovalRecord]:
        items = self.store.list_approvals(user_id or self.settings.user_id, status)
        if session_id is None:
            return items
        self._session(session_id, user_id)  # ownership check
        in_session = {r.id for r in self.store.list_session_runs(session_id, limit=1000)}
        return [a for a in items if a.run_id in in_session]

    def list_artifacts(self, *, user_id: str | None = None, session_id: str | None = None, limit: int = 50) -> list[dict]:
        uid = user_id or self.settings.user_id
        if session_id is not None:
            self._session(session_id, uid)  # ownership check
        return [a.to_dict() for a in self.artifacts.list(uid, session_id, max(1, min(limit, 200)))]

    def get_artifact(self, artifact_id: str, *, user_id: str | None = None) -> dict:
        try:
            return self.artifacts.get(user_id or self.settings.user_id, artifact_id).to_dict()
        except LookupError as exc:
            raise NotFound("artifact not found") from exc

    def read_artifact(self, artifact_id: str, *, user_id: str | None = None, max_bytes: int | None = None) -> tuple[dict, bytes]:
        """The file's metadata and bytes (small files only; larger ones go through a download URL)."""
        try:
            rec, data = self.artifacts.read(user_id or self.settings.user_id, artifact_id, **({"max_bytes": max_bytes} if max_bytes else {}))
        except LookupError as exc:
            raise NotFound("artifact not found") from exc
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        return rec.to_dict(), data

    def artifact_url(self, artifact_id: str, *, user_id: str | None = None, seconds: int = 600) -> str | None:
        """A short-lived download link, or None when the store cannot make one (local development: use ``read_artifact``)."""
        try:
            return self.artifacts.url(user_id or self.settings.user_id, artifact_id, seconds)
        except LookupError as exc:
            raise NotFound("artifact not found") from exc

    def get_events(self, run_id: str, after_seq: int = 0, limit: int = 500, *, user_id: str | None = None) -> list[dict]:
        """Events of a turn after ``after_seq`` (a polling alternative to ``tail_turn``)."""
        self.get_turn(run_id, user_id=user_id)  # ownership check (and lost-worker detection)
        return self.store.list_events(run_id, max(0, after_seq), max(1, min(limit, 1000)))

    def audit(self, run_id: str, *, user_id: str | None = None) -> list[dict]:
        self.get_run(run_id, user_id=user_id)  # ownership check
        return self.store.list_audit(run_id)
