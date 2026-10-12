"""Scheduled tasks: a saved instruction that the agent runs on a cron schedule with nobody watching.

Each firing opens its OWN session (titled with the schedule and the date) and sends the instruction as an ordinary message from the user, so
everything the user can do in a chat works unattended: the agent can use tools, write files (saved as artifacts), and so on. What cannot happen
unattended is approval: anything that needs a person waits in that session (the UI shows a badge) unless the schedule carries its own auto-approve
setting. Housekeeping (``maintenance()``) fires what is due, so the granularity is the housekeeping interval.

Rules: a schedule never fires while its previous run is still working or waiting for approval (they must not pile up); a missed firing (the server
was down) runs once, not once per miss; per-user limits on the number of schedules, the minimum interval and the runs per day come from the policy."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from pinboard.engine import cron
from pinboard.engine.errors import BadRequest, Busy, Conflict, NotFound
from pinboard.state import ScheduleRecord, ScheduleRunRecord, new_schedule_id, new_schedule_run_id

log = logging.getLogger(__name__)

CATCHUP_AFTER = timedelta(minutes=10)   # a firing this late counts as a catch-up after downtime
MAX_TITLE, MAX_INSTRUCTIONS = 80, 8000


class SchedulesMixin:
    """The schedule side of RunService (needs self.store, self.policy, self.clock, self.prompt_guard and the session/turn methods)."""

    def _sched_policy(self):
        p = self.policy.schedules
        if not p.enabled:
            raise BadRequest("scheduled tasks are switched off in this deployment (policy schedules.enabled)")
        return p

    def _sched(self, schedule_id: str, user_id: str | None) -> ScheduleRecord:
        s = self.store.get_schedule(schedule_id)
        if s is None or s.user_id != (user_id or self.settings.user_id):
            raise NotFound("schedule not found")
        return s

    @staticmethod
    def _title(value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_TITLE:
            raise BadRequest(f"'title' must be text of 1-{MAX_TITLE} characters")
        return value.strip()

    def _instructions(self, value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_INSTRUCTIONS:
            raise BadRequest(f"'instructions' must be text of 1-{MAX_INSTRUCTIONS} characters")
        verdict = self.prompt_guard.check(value)
        if not verdict.ok:
            raise BadRequest(verdict.message())
        return value.strip()

    # ── reading ──
    def list_schedules(self, *, user_id: str | None = None) -> list[dict]:
        return [s.to_dict() for s in self.store.list_schedules(user_id or self.settings.user_id)]

    def list_schedule_runs(self, schedule_id: str, *, user_id: str | None = None, limit: int = 20) -> list[dict]:
        self._sched(schedule_id, user_id)
        return [r.to_dict() for r in self.store.list_schedule_runs(schedule_id, limit)]

    # ── changing ──
    def create_schedule(self, title: str, instructions: str, cron_expr: str, *, timezone: str = "UTC", model: str | None = None, auto_approve: Any = None,
                        enabled: bool = True, user_id: str | None = None) -> dict:
        policy, uid, now = self._sched_policy(), user_id or self.settings.user_id, self.clock()
        if len(self.store.list_schedules(uid)) >= policy.max_per_user:
            raise BadRequest(f"at most {policy.max_per_user} scheduled tasks per user; remove one first")
        first = cron.validate(cron_expr, timezone, policy.min_interval_minutes, now)
        s = ScheduleRecord(id=new_schedule_id(), user_id=uid, title=self._title(title), instructions=self._instructions(instructions), cron=" ".join(cron_expr.split()),
                           timezone=timezone, model=self._model(model), auto_approve=self._auto_setting(auto_approve), enabled=bool(enabled),
                           next_run_at=first if enabled else None, created_at=now, updated_at=now)
        self.store.create_schedule(s)
        self.store.add_audit(uid, None, "schedule.created", {"schedule_id": s.id, "cron": s.cron, "timezone": s.timezone}, now)
        return s.to_dict()

    def update_schedule(self, schedule_id: str, changes: dict, *, user_id: str | None = None) -> dict:
        policy, now = self._sched_policy(), self.clock()
        s = self._sched(schedule_id, user_id)
        allowed = {"title", "instructions", "cron", "timezone", "model", "auto_approve", "enabled"}
        if not changes or set(changes) - allowed:
            raise BadRequest(f"send at least one of: {', '.join(sorted(allowed))}")
        fields: dict[str, Any] = {}
        if "title" in changes:
            fields["title"] = self._title(changes["title"])
        if "instructions" in changes:
            fields["instructions"] = self._instructions(changes["instructions"])
        if "model" in changes:
            fields["model"] = self._model(changes["model"] or None)
        if "auto_approve" in changes:
            fields["auto_approve"] = self._auto_setting(changes["auto_approve"])
        if "enabled" in changes and not isinstance(changes["enabled"], bool):
            raise BadRequest("'enabled' must be true or false")
        enabled = changes.get("enabled", s.enabled)
        expr, tz = " ".join(str(changes.get("cron", s.cron)).split()), changes.get("timezone", s.timezone)
        if "cron" in changes or "timezone" in changes:
            fields.update(cron=expr, timezone=tz)
        if "enabled" in changes:
            fields["enabled"] = enabled
        if enabled and ("cron" in changes or "timezone" in changes or ("enabled" in changes and not s.enabled)):
            fields["next_run_at"] = cron.validate(expr, tz, policy.min_interval_minutes, now)   # a changed or re-enabled schedule counts from now
        elif not enabled:
            fields["next_run_at"] = None
        return self.store.update_schedule(s.id, **fields).to_dict()

    def delete_schedule(self, schedule_id: str, *, user_id: str | None = None) -> dict:
        s = self._sched(schedule_id, user_id)
        self.store.delete_schedule(s.id)
        self.store.add_audit(s.user_id, None, "schedule.deleted", {"schedule_id": s.id}, self.clock())
        return {"deleted": s.id}

    # ── running ──
    def run_schedule_now(self, schedule_id: str, *, user_id: str | None = None) -> dict:
        """Start a run immediately (the next scheduled firing is unchanged). Refuses, with the reason, if it cannot start."""
        self._sched_policy()
        s = self._sched(schedule_id, user_id)
        run = self._fire(s, "manual")
        if run.status != "started":
            raise Conflict(run.note or "the run did not start")
        return run.to_dict()

    def _previous_run_active(self, s: ScheduleRecord) -> bool:
        if not s.last_session_id:
            return False
        session = self.store.get_session(s.last_session_id)
        return bool(session and self.store.active_run_for_thread(session.thread_id))

    def _fire(self, s: ScheduleRecord, trigger: str) -> ScheduleRunRecord:
        """Open a session for this schedule and send its instructions. Always records what happened (started / skipped / failed)."""
        now, policy = self.clock(), self.policy.schedules
        rec = ScheduleRunRecord(id=new_schedule_run_id(), schedule_id=s.id, user_id=s.user_id, trigger=trigger, status="skipped", created_at=now)
        if self._previous_run_active(s):
            rec.note = "the previous run is still working or waiting for your approval"
        elif self.store.count_schedule_runs_since(s.user_id, now - timedelta(hours=24)) >= policy.max_runs_per_day:
            rec.note = f"the daily limit of {policy.max_runs_per_day} scheduled runs was reached"
        else:
            try:
                session = self.create_session(user_id=s.user_id, title=f"{s.title} · {now.astimezone(cron.zone(s.timezone)).strftime('%b %d %H:%M')}", model=s.model,
                                              auto_approve=s.auto_approve, schedule_id=s.id)
                turn = self.send_message(session.id, s.instructions, user_id=s.user_id)   # the user's own words: source "user", not untrusted
                rec.status, rec.session_id, rec.turn_id = ("started" if turn.status in ("running", "awaiting_approval", "completed") else "failed"), session.id, turn.id
                if rec.status == "failed":
                    rec.note = turn.output or turn.status
            except Busy:
                rec.note = "the server was busy"
            except Exception as exc:  # noqa: BLE001  one broken schedule must not stop the others
                log.exception("schedule %s failed to start", s.id)
                rec.status, rec.note = "failed", f"{type(exc).__name__}: {exc}"[:200]
        self.store.add_schedule_run(rec)
        fields = {"last_run_at": now, "last_status": rec.status}
        if rec.session_id:
            fields["last_session_id"] = rec.session_id
        if rec.status == "started":
            fields["run_count"] = s.run_count + 1
        self.store.update_schedule(s.id, **fields)
        return rec

    def fire_due_schedules(self) -> dict:
        """Called from housekeeping: start every schedule whose time has come. Safe to call from several instances at once (a claim moves the schedule on)."""
        report = {"schedules_fired": 0, "schedules_skipped": 0}
        try:
            if not self.policy.schedules.enabled:
                return report
        except Exception:  # noqa: BLE001  no usable policy: nothing is scheduled
            return report
        now = self.clock()
        for s in self.store.due_schedules(now):
            try:
                nxt = cron.next_after(s.cron, s.timezone, now)   # from NOW: a long downtime yields one catch-up run, not a burst
            except BadRequest:
                self.store.update_schedule(s.id, enabled=False, next_run_at=None, last_status="failed")
                continue
            if not self.store.claim_schedule(s.id, s.next_run_at, nxt, now):
                continue   # another instance took it
            rec = self._fire(s, "catchup" if now - s.next_run_at > CATCHUP_AFTER else "schedule")
            report["schedules_fired" if rec.status == "started" else "schedules_skipped"] += 1
        return report


__all__ = ["SchedulesMixin"]
