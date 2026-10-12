#!/usr/bin/env python3
"""Run the app LOCALLY against the REAL services (Gemini, the sandbox service, Cloud Storage if configured) and report what really happens.

    uv run python scripts/verify_real.py --dry-run            # free: shows what would be used; calls no model or sandbox
    uv run python scripts/verify_real.py --only S1            # one scenario at a time is a good way to start
    uv run python scripts/verify_real.py                      # all scenarios except S9/S10 (several minutes of Gemini + sandbox time)
    uv run python scripts/verify_real.py --email              # also S9: sends ONE real e-mail to the first allowed recipient
    uv run python scripts/verify_real.py --github OWNER/REPO  # also S10: branch + commit + PR through the agent, then cleaned up

What it uses: your .env, config/policy.yaml, Application Default Credentials, the real model and sandbox service. State is IN MEMORY (your
Postgres is not touched). Shell approvals are switched on and answered automatically. Sandboxes it creates are deleted at the end.

Outcomes: PASS / FAIL (something broke) / WARN (the model chose differently from what the system prompt hopes for: not a bug, but worth reading).
Prints a story of each turn (one line per event) and writes scripts/verify_real_results.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
RESULTS_FILE = Path(__file__).with_name("verify_real_results.json")


# ── reporting ────────────────────────────────────────────────────────────

@dataclass
class Reporter:
    rows: list[dict] = field(default_factory=list)
    scenario: str = ""
    quiet: bool = False

    def add(self, status: str, what: str, detail: str = "") -> bool:
        self.rows.append({"scenario": self.scenario, "status": status, "what": what, "detail": detail})
        print(f"  [{status}] {what}" + (f": {detail}" if detail else ""))
        return status == "PASS"

    def check(self, ok: bool, what: str, detail: str = "") -> bool:
        return self.add("PASS" if ok else "FAIL", what, detail)

    def expect(self, ok: bool, what: str, detail: str = "") -> bool:
        """For things the MODEL decides: not meeting the hope is a WARN, not a failure of the system."""
        return self.add("PASS" if ok else "WARN", what, detail)

    def info(self, what: str, detail: str = "") -> None:
        self.add("INFO", what, detail)

    def summary(self) -> int:
        counts = {k: sum(1 for r in self.rows if r["status"] == k) for k in ("PASS", "FAIL", "WARN", "INFO")}
        print(f"\n{counts['PASS']} passed, {counts['FAIL']} FAILED, {counts['WARN']} warnings (model behaviour), {counts['INFO']} notes")
        return 1 if counts["FAIL"] else 0


@dataclass
class Ctx:
    svc: Any                                   # RunService
    rep: Reporter
    delete_sandbox: Callable[[str], None]
    new_service: Callable[[], Any]             # a second service object over the same state (a "restart")
    flags: argparse.Namespace
    sessions: list = field(default_factory=list)
    memo: dict = field(default_factory=dict)   # results shared between scenarios

    def session(self, title: str):
        s = self.svc.create_session(title=title)
        self.sessions.append(s)
        return s


# ── following a turn ─────────────────────────────────────────────────────

def _short(text: Any, n: int = 110) -> str:
    t = str(text).replace("\n", " ").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def describe_event(e: dict) -> str | None:
    p, kind = e.get("payload") or {}, e["type"]
    who = p.get("agent", "")
    if kind == "token":
        return None
    if kind == "assistant_message":
        return f"say   [{who}] {_short(p.get('text'))}"
    if kind == "tool_call":
        args = ", ".join(f"{k}={_short(v, 40)}" for k, v in (p.get("args") or {}).items())
        return f"call  [{who}] {p.get('name')}({_short(args, 90)})"
    if kind == "tool_result":
        return f"result[{who}] {p.get('name')} {p.get('status')}: {_short(p.get('preview'), 90)}"
    if kind == "interrupt":
        return "WAIT  approval needed: " + ", ".join(f"{i['kind']} ({i['title'][:50]})" for i in p.get("items", []))
    if kind == "artifact":
        return f"file  {p.get('path')} ({p.get('size')} bytes)"
    if kind == "usage":
        return f"usage [{who}] in={p.get('input_tokens')} out={p.get('output_tokens')} thinking={p.get('reasoning_tokens')} cached={p.get('cached_tokens')}"
    if kind == "turn_done" and p.get("timing"):
        t = p["timing"]
        return (f"done   {p.get('status')}  | setup {t.get('setup_seconds')}s (sandbox check {t.get('probe_seconds', '-')}s, agent build {t.get('build_seconds', '-')}s), "
                f"first output after {t.get('first_output_seconds')}s, streaming {t.get('stream_seconds')}s, total {t.get('total_seconds')}s")
    if kind in ("notice", "turn_error", "turn_done", "approval_decided", "turn_started"):
        return f"{kind:<6} {_short(json.dumps({k: v for k, v in p.items() if k not in ('agent',)}, default=str), 100)}"
    if kind == "todos":
        return "todos " + "; ".join(f"{t['status'][:4]}:{_short(t['content'], 30)}" for t in p.get("todos", []))
    return None


def default_answer(appr) -> dict:
    kind = appr.previews[0]["kind"]
    if kind == "question":
        return {"decisions": [{"type": "respond", "message": "blue"}]}
    return {"decision": "approve"}


MAX_APPROVALS_PER_TURN = 12  # a model that keeps asking must not loop (and cost money) forever


def drive(ctx: Ctx, session_id: str, text: str, *, answer: Callable[[Any], dict] = default_answer, timeout: float = 600,
          cancel_when: Callable[[dict, list], bool] | None = None, svc=None, **send) -> tuple[Any, list, dict]:
    """Send a message and follow the turn until it stops, answering approvals. Returns (final turn, events, stats)."""
    svc = svc or ctx.svc
    t0 = time.monotonic()
    turn = svc.send_message(session_id, text, **send)
    print(f"  > {_short(text, 120)}")
    stats: dict = {"approvals": [], "tokens": 0, "cancel_sent": False, "model_calls": 0, "in_tokens": 0, "out_tokens": 0, "thinking_tokens": 0}
    events: list[dict] = []
    seq = 0
    while True:
        for e in svc.tail_turn(turn.id, seq, heartbeat_seconds=5, max_seconds=60, poll_seconds=0.3):
            if e["type"] in ("heartbeat", "reconnect"):
                continue
            seq = e["seq"]
            events.append(e)
            stats["tokens"] += e["type"] == "token"
            if e["type"] == "usage":
                p = e["payload"]
                stats["model_calls"] += 1
                stats["in_tokens"] += p.get("input_tokens") or 0
                stats["out_tokens"] += p.get("output_tokens") or 0
                stats["thinking_tokens"] += p.get("reasoning_tokens") or 0
            line = describe_event(e)
            if line:
                print(f"    {time.monotonic() - t0:6.1f}s  {line}")
            if cancel_when and not stats["cancel_sent"] and cancel_when(e, events):
                svc.cancel_turn(turn.id)
                stats["cancel_sent"] = True
        run = svc.get_turn(turn.id)
        if run.status == "awaiting_approval":
            appr = next(a for a in svc.list_approvals(status="pending") if a.run_id == turn.id)
            stats["approvals"].append(appr.previews[0]["kind"])
            if len(stats["approvals"]) > MAX_APPROVALS_PER_TURN:  # stop a runaway: reject and cancel
                svc.decide(appr.id, decision="reject", message="too many approval requests in one turn")
                stats["approval_cap"] = True
                if not stats["cancel_sent"]:
                    svc.cancel_turn(turn.id)
                    stats["cancel_sent"] = True
                continue
            svc.decide(appr.id, **answer(appr))
            continue
        if run.status != "running":
            break
        if time.monotonic() - t0 > timeout and not stats["cancel_sent"]:
            svc.cancel_turn(turn.id)
            stats["cancel_sent"] = stats["timeout"] = True
    stats["seconds"] = round(time.monotonic() - t0, 1)
    print(f"    = {stats['seconds']}s, {stats['model_calls']} model call(s): {stats['in_tokens']} tokens in, {stats['out_tokens']} out "
          f"({stats['thinking_tokens']} of them thinking)")
    return svc.get_turn(turn.id), events, stats


def seconds_between(events: list[dict], first: Callable[[dict], bool], then: Callable[[dict], bool]) -> float | None:
    a = next((e for e in events if first(e)), None)
    b = next((e for e in events if then(e) and (a is None or e["seq"] > a["seq"])), None)
    if not (a and b):
        return None
    return round((datetime.fromisoformat(b["ts"]) - datetime.fromisoformat(a["ts"])).total_seconds(), 1)


def tool_names(events: list[dict]) -> list[str]:
    return [e["payload"]["name"] for e in events if e["type"] == "tool_call"]


def results_text(events: list[dict]) -> str:
    return " ".join(str(e["payload"].get("preview", "")) for e in events if e["type"] == "tool_result")


def say_text(events: list[dict]) -> str:
    return " ".join(e["payload"].get("text", "") for e in events if e["type"] == "assistant_message")


def sandbox_of(ctx: Ctx, session) -> str | None:
    return ctx.svc.store.get_session(session.id).sandbox_name


def artifact(ctx: Ctx, session, name: str):
    return next((a for a in ctx.svc.list_artifacts(session_id=session.id) if a["name"] == name), None)


# ── scenarios ────────────────────────────────────────────────────────────

def s1_chat_only(ctx: Ctx) -> None:
    """A greeting needs no tools: it must stream, finish fast, and not create a sandbox."""
    r = ctx.rep
    s = ctx.session("S1 chat only")
    turn, ev, st = drive(ctx, s.id, "Reply with one short friendly sentence. Do not use any tools.")
    r.check(turn.status == "completed", "the turn completes", f"status={turn.status} in {st['seconds']}s")
    r.check(sandbox_of(ctx, s) is None, "no sandbox was created for a chat-only turn")
    r.check(any(e["type"] == "assistant_message" for e in ev) and bool(turn.output.strip()), "an answer was produced", _short(turn.output, 80))
    first = seconds_between(ev, lambda e: e["type"] == "turn_started", lambda e: e["type"] in ("token", "assistant_message"))
    r.info("time to first text", f"{first}s; {st['tokens']} token batch(es) before the final message")
    r.info("live text batches", f"{st['tokens']} (a one-sentence answer is too short to stream; S12 checks a long one)")
    r.check(not any(e["type"] == "turn_error" for e in ev), "no errors in the event log")


def s12_streaming(ctx: Ctx) -> None:
    """A long answer: it must arrive as several live batches, and the stored log keeps only the final message."""
    r = ctx.rep
    s = ctx.session("S12 streaming")
    turn, ev, st = drive(ctx, s.id, "Without using any tools, explain in about 350 words how a bill becomes a law in the United States.")
    r.check(turn.status == "completed", "the turn completes", f"in {st['seconds']}s")
    r.check(sandbox_of(ctx, s) is None, "no sandbox was created")
    batches = [e for e in ev if e["type"] == "token"]
    first = seconds_between(ev, lambda e: e["type"] == "turn_started", lambda e: e["type"] == "token")
    r.expect(len(batches) >= 2, "the answer streamed in several live batches", f"{len(batches)} batch(es); first text after {first}s; whole answer {st['seconds']}s")
    stored = ctx.svc.store.list_events(turn.id, 0, 100_000)
    r.check(not any(e["type"] == "token" for e in stored) and any(e["type"] == "assistant_message" for e in stored),
            "the stored log keeps the final message, not the live batches")
    live = "".join(e["payload"]["text"] for e in batches)
    final = turn.output
    r.expect(final.startswith(live) or live.strip() in final, "the live batches were a faithful beginning of the final answer", f"{len(live)} of {len(final)} characters streamed live")


def s13_web_search(ctx: Ctx) -> None:
    """Tavily through the agent: a real search happens, its results are labelled untrusted, and the answer cites a source."""
    r = ctx.rep
    s = ctx.session("S13 web search")
    turn, ev, st = drive(ctx, s.id, "Use web search to find one current fact about the James Webb Space Telescope, and answer in one sentence that includes the source URL.")
    r.check(turn.status == "completed", "the turn completes", f"in {st['seconds']}s")
    calls = [e for e in ev if e["type"] == "tool_call" and e["payload"]["name"] == "web_search"]
    r.expect(bool(calls), "the model used web_search", f"tools used: {tool_names(ev)}")
    results = [e["payload"] for e in ev if e["type"] == "tool_result" and e["payload"].get("name") == "web_search"]
    if results:
        text = results[0].get("preview", "")
        r.check(results[0].get("status") == "success" and "Search failed" not in text and "No results" not in text, "Tavily answered with results", _short(text, 100))
        r.check("untrusted_content" in text, "the results were labelled as untrusted data")
    elif calls:
        r.add("FAIL", "web_search was called but no result came back")
    r.expect(bool(re.search(r"https?://", turn.output)), "the answer cites a source URL", _short(turn.output, 100))


def s2_plan_and_file(ctx: Ctx) -> None:
    """Multi-step work with a file: expect a plan first, a sandbox created on demand, and an artifact."""
    r = ctx.rep
    s = ctx.session("S2 plan and file")
    ctx.memo["s2"] = s
    turn, ev, st = drive(ctx, s.id, "Write a short 3-bullet note about why solar power is growing, save it as note.md in your output folder, and tell me when it is saved.")
    r.check(turn.status == "completed", "the turn completes", f"status={turn.status} in {st['seconds']}s, approvals={st['approvals']}")
    r.info("plan first?", "yes" if "submit_plan" in tool_names(ev) else "no (fine: one file in the sandbox is simple work)")
    name = sandbox_of(ctx, s)
    r.check(name is not None, "a sandbox exists now (created when a tool first needed it)", name or "")
    wait = seconds_between(ev, lambda e: e["type"] == "tool_call" and e["payload"]["name"] in ("write_file", "execute"), lambda e: e["type"] == "tool_result")
    r.info("first sandbox tool call (includes creating the sandbox and copying skills in)", f"{wait}s")
    note = artifact(ctx, s, "note.md")
    r.check(note is not None and note["size"] > 20, "note.md was saved as an artifact", f"{note and note['size']} bytes")
    if note:
        content = ctx.svc.read_artifact(note["id"])[1].decode(errors="replace")
        ctx.memo["note_first_line"] = next((ln.strip(" -*#\t") for ln in content.splitlines() if ln.strip()), "")
        r.info("note.md starts with", _short(content, 100))
    r.check(any(e["type"] == "artifact" for e in ev), "an artifact event was streamed before the turn ended")
    ctx.memo["s2_sandbox"] = name


def s3_same_sandbox(ctx: Ctx) -> None:
    """A second message in the same conversation reuses the sandbox and sees the file."""
    r = ctx.rep
    s = ctx.memo.get("s2")
    if s is None:
        return r.add("WARN", "skipped: needs S2")
    before = sandbox_of(ctx, s)
    turn, ev, st = drive(ctx, s.id, "Read note.md from your output folder and quote its first line exactly.")
    r.check(turn.status == "completed", "the turn completes", f"in {st['seconds']}s")
    r.check(sandbox_of(ctx, s) == before, "the same sandbox was used (attached, not recreated)", before or "")
    first = ctx.memo.get("note_first_line", "")
    r.expect(bool(first) and first[:20].lower() in say_text(ev).lower() + results_text(ev).lower(), "the answer quotes the file", f"expected something like: {_short(first, 60)}")


def s4_replaced_sandbox(ctx: Ctx) -> None:
    """The sandbox disappears: the next turn must notice, say so, and the agent should be able to get its file back from the saved files."""
    r = ctx.rep
    s = ctx.memo.get("s2")
    name = ctx.memo.get("s2_sandbox")
    if s is None or not name:
        return r.add("WARN", "skipped: needs S2")
    ctx.delete_sandbox(name)
    r.info("deleted the conversation's sandbox on purpose", name)
    turn, ev, st = drive(ctx, s.id, "Your sandbox may have been reset. Use your saved-files tools to find note.md from earlier, restore it, and quote its first line.")
    r.check(turn.status == "completed", "the turn completes", f"in {st['seconds']}s")
    r.check(any(e["type"] == "notice" and "replaced" in e["payload"].get("text", "") for e in ev), "the replacement was noticed and announced")
    new = sandbox_of(ctx, s)
    r.check(new is not None and new != name, "a new sandbox replaced the dead one", f"{name} -> {new}")
    used = tool_names(ev)
    r.expect("list_artifacts" in used and "restore_artifact" in used, "the model used list_artifacts and restore_artifact", f"tools used: {used}")
    r.expect("Restored" in results_text(ev), "the file was restored into the new sandbox")


def s5_shell_approval(ctx: Ctx) -> None:
    """A shell command must wait for a person; once approved it runs."""
    r = ctx.rep
    s = ctx.session("S5 shell approval")
    turn, ev, st = drive(ctx, s.id, "Use the shell to run: echo verified > check.txt && cat check.txt  and tell me what it printed.")
    r.check(turn.status == "completed", "the turn completes", f"in {st['seconds']}s")
    r.check("shell" in st["approvals"], "the shell command waited for approval", f"approvals={st['approvals']}")
    r.expect("verified" in (turn.output + results_text(ev)).lower(), "the command ran and its output came back")


def s6_question(ctx: Ctx) -> None:
    """The model asks the person a question and uses the typed answer."""
    r = ctx.rep
    s = ctx.session("S6 question")
    turn, ev, st = drive(ctx, s.id, "Before you write anything, ask me which colour I prefer (use your ask-the-user ability), then save colour.txt in your output folder containing my answer.")
    r.check(turn.status == "completed", "the turn completes", f"in {st['seconds']}s")
    r.expect("question" in st["approvals"], "the model asked a question and waited for the answer", f"approvals={st['approvals']}")
    f = artifact(ctx, s, "colour.txt") or artifact(ctx, s, "color.txt")  # the model may use the American spelling
    r.expect(bool(f) and b"blue" in ctx.svc.read_artifact(f["id"])[1].lower(), "the colour file contains the answer (blue)", f and f["name"])


def s7_cancel(ctx: Ctx) -> None:
    """Cancel a turn that is in progress; the conversation must be usable afterwards."""
    r = ctx.rep
    s = ctx.session("S7 cancel")
    prompt = "Write five separate files part1.md to part5.md in your output folder, each with a different 150-word paragraph about a different planet."
    turn, ev, st = drive(ctx, s.id, prompt, cancel_when=lambda e, evs: e["type"] == "tool_call" and e["payload"]["name"] in ("write_file", "execute"))
    r.expect(turn.status == "cancelled", "the turn stopped after the cancel request", f"final status={turn.status} (completed means it was already done)")
    r.check(turn.status in ("cancelled", "completed"), "the turn ended cleanly (no hang, no failure)", turn.status)
    turn2, _, st2 = drive(ctx, s.id, "Say OK.")
    r.check(turn2.status == "completed", "the conversation works after a cancel", f"in {st2['seconds']}s")


def s8_restart(ctx: Ctx) -> None:
    """The server restarts while a turn waits for approval: a fresh service decides and the real model carries on from the checkpoint."""
    r = ctx.rep
    s = ctx.session("S8 restart")
    turn = ctx.svc.send_message(s.id, "Use the shell to run: echo restart-test  and tell me the output.")
    print("  > (waiting for the first approval)")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline and ctx.svc.get_turn(turn.id).status == "running":
        time.sleep(0.5)
    st = ctx.svc.get_turn(turn.id).status
    pending = [a for a in ctx.svc.list_approvals(status="pending") if a.run_id == turn.id]
    if st != "awaiting_approval" or not pending:
        return r.add("WARN", "the model did not use the shell, so no approval was pending to survive a restart", f"status={st}")
    other = ctx.new_service()
    r.info("simulated a restart", "a brand-new service object over the same state decides the approval")
    kind = pending[0].previews[0]["kind"]
    other.decide(pending[0].id, **(default_answer(pending[0])))
    ok = _wait_done(other, turn.id, 300)
    final = other.get_turn(turn.id)
    r.check(ok and final.status in ("completed", "awaiting_approval"), "the turn continued after the restart", f"first approval was {kind}; status={final.status}")
    while final.status == "awaiting_approval":  # the model may ask for more approvals
        nxt = next(a for a in other.list_approvals(status="pending") if a.run_id == turn.id)
        other.decide(nxt.id, **default_answer(nxt))
        _wait_done(other, turn.id, 300)
        final = other.get_turn(turn.id)
    r.check(final.status == "completed" and "restart-test" in final.output.lower() + say_text([e for e in other.get_events(turn.id, 0, 1000)]).lower(),
            "the real model finished the task from the checkpoint", _short(final.output, 80))


def _wait_done(svc, run_id: str, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if svc.get_turn(run_id).status != "running":
            return True
        time.sleep(0.5)
    return False


def s9_email(ctx: Ctx) -> None:
    """(--email) The full path: plan, hand-over to the mailer, e-mail approval, a REAL message to the first allowed recipient."""
    r = ctx.rep
    to = ctx.svc.policy.email.allowed_recipients[0]
    s = ctx.session("S9 email")
    turn, ev, st = drive(ctx, s.id, f"Email {to} one sentence saying that the Pinboard end-to-end check ran. Keep it very short.")
    r.check(turn.status == "completed", "the turn completes", f"in {st['seconds']}s, approvals={st['approvals']}")
    r.expect("plan" in st["approvals"], "a plan was approved first")
    r.check("email" in st["approvals"], "the e-mail waited for approval")
    r.check("Email sent" in results_text(ev), "the e-mail was sent", _short(results_text(ev), 100))
    r.expect("task" in tool_names(ev), "the work was handed to the mailer helper", f"tools used: {tool_names(ev)}")


def s10_github(ctx: Ctx) -> None:
    """(--github OWNER/REPO) Branch, commit and pull request through the agent with approvals; then the test branch and PR are removed."""
    from pinboard.integrations.github import GitHubApp as App

    r = ctx.rep
    repo = ctx.flags.github
    stamp = int(time.time())
    branch = f"agent/verify-{stamp}"
    s = ctx.session("S10 github")
    turn, ev, st = drive(ctx, s.id, f"In the repository {repo}: create a branch named {branch}, commit a file docs/verify-{stamp}.md containing the single word 'verification', "
                                    f"and open a pull request titled 'Verification {stamp}'. Do it now.")
    r.check(turn.status == "completed", "the turn completes", f"in {st['seconds']}s, approvals={st['approvals']}")
    r.check(st["approvals"].count("github") >= 2, "branch/commit/PR each waited for approval", f"approvals={st['approvals']}")
    text = results_text(ev)
    m = re.search(r"pull request #(\d+)", text)
    r.check(bool(m), "a pull request was opened", _short(text, 100))
    try:  # cleanup: close the PR and delete the branch
        s_ = ctx.svc.settings
        app = App(s_.github_app_id, ctx.svc.vault.get("GITHUB_APP_PRIVATE_KEY", required=True), s_.github_installation_id)
        if m:
            app.api("PATCH", f"/repos/{repo}/pulls/{m.group(1)}", repo=repo, access="write", json={"state": "closed"})
        app.api("DELETE", f"/repos/{repo}/git/refs/heads/{branch}", repo=repo, access="write")
        r.info("cleanup", f"closed PR #{m.group(1) if m else '?'} and deleted {branch}")
    except Exception as exc:  # noqa: BLE001
        r.add("WARN", "cleanup failed, remove the test branch/PR by hand", f"{branch}: {exc}")


def s11_cloud_storage(ctx: Ctx) -> None:
    """If artifacts go to Cloud Storage: the bytes really arrived. (A download link needs the runtime's service account, so only try it.)"""
    r = ctx.rep
    s = ctx.memo.get("s2")
    uri = ctx.svc.settings.artifacts_uri
    if s is None or not uri.startswith("gs://"):
        return r.add("INFO", "skipped", f"ARTIFACTS_URI is {uri!r}: set it to gs://bucket/prefix to check Cloud Storage")
    note = artifact(ctx, s, "note.md")
    rec = ctx.svc.store.get_artifact(note["id"]) if note else None
    r.check(rec is not None and ctx.svc.artifacts.blobs.get(rec.blob_key) == ctx.svc.read_artifact(note["id"])[1], "the file is in the bucket and reads back identically", rec.blob_key if rec else "")
    try:
        url = ctx.svc.artifact_url(note["id"])
        r.info("signed download link", "created (" + url.split("?")[0][-60:] + ")" if url else "none")
    except Exception as exc:  # noqa: BLE001
        r.info("signed download link could not be made locally (expected with a personal login; the runtime service account is tested in M6)", _short(exc, 120))


def s14_auto_approve(ctx: Ctx) -> None:
    """With automatic approval on, shell commands run without anyone being asked, but a question still waits for the person."""
    r = ctx.rep
    s = ctx.session("S14 auto approve")
    turn, ev, st = drive(ctx, s.id, "Use the shell to run: echo auto-ok > auto.txt && cat auto.txt  and tell me what it printed.", auto_approve=True)
    r.check(turn.status == "completed", "the turn completes without anyone answering", f"in {st['seconds']}s")
    r.check("shell" not in st["approvals"], "nobody was asked to approve the command", f"approvals={st['approvals']}")
    n = sum(e["type"] == "approval_auto" for e in ev)
    r.check(n >= 1, "the engine approved on the user's behalf and said so", f"{n} automatic approval(s)")
    r.expect("auto-ok" in (turn.output + results_text(ev)).lower(), "the command ran and its output came back")
    turn2, ev2, st2 = drive(ctx, s.id, "Now ask me which fruit I prefer (use your ask-the-user ability) and tell me what I answered.")
    r.check("question" in st2["approvals"], "a question still waits for the person even on automatic approval", f"approvals={st2['approvals']}")
    turn3, _, st3 = drive(ctx, s.id, "Run: echo again in the shell.", auto_approve=False)
    r.check("shell" in st3["approvals"], "switching it off makes commands wait again", f"approvals={st3['approvals']}")


def s15_parallel_helpers(ctx: Ctx) -> None:
    """Two helpers started in the same step, each needing a shell approval: the pauses arrive together and one answer set resumes both."""
    r = ctx.rep
    s = ctx.session("S15 parallel helpers")
    turn, ev, st = drive(ctx, s.id, "Start TWO coder helpers AT THE SAME TIME (two task calls in one step, no plan needed). Helper one runs the shell command "
                                    "`echo one > one.txt`, helper two runs `echo two > two.txt`. Then tell me both are done.")
    r.check(turn.status == "completed", "the turn completes", f"in {st['seconds']}s")
    helpers = {e["payload"].get("agent") for e in ev if e["type"] == "tool_call" and e["payload"].get("name") == "execute"}
    r.expect(len(helpers - {"assistant"}) >= 1 and len([e for e in ev if e["type"] == "tool_call" and e["payload"].get("name") == "execute"]) >= 2,
             "both commands ran", f"agents={sorted(map(str, helpers))}")
    batched = [e for e in ev if e["type"] == "interrupt" and len(e["payload"].get("items", [])) >= 2]
    r.expect(bool(batched), "the model started the helpers in parallel (their approvals arrived as one batch)", f"{len(batched)} batch(es)")


def s16_helper_model_choice(ctx: Ctx) -> None:
    """The orchestrator picks a cheaper model for a simple helper task; a helper never runs above the user's model."""
    r = ctx.rep
    policy = ctx.svc.policy
    ceiling = "gemini-3.5-flash"
    s = ctx.svc.create_session(title="S16 helper model choice", model=ceiling)
    ctx.sessions.append(s)
    turn, ev, st = drive(ctx, s.id, "Use the researcher helper to look up the capital of Australia. It is a trivial lookup, so run the helper on the cheapest model you are allowed to. "
                                    "Then tell me the answer and which model the helper used.")
    r.check(turn.status == "completed", "the turn completes", f"in {st['seconds']}s")
    delegates = [e for e in ev if e["type"] == "tool_call" and e["payload"].get("name") in ("delegate", "task")]
    r.check(bool(delegates), "the work was handed to a helper", f"{len(delegates)} delegation(s)")
    chosen = [d["payload"]["args"].get("model") for d in delegates]
    r.expect(any(c and c != ceiling for c in chosen), "the orchestrator chose a cheaper model than its own for the helper", f"chosen={chosen}")
    allowed = {m.id for m in policy.allowed_for(ceiling)}
    used = {e["payload"].get("model") for e in ev if e["type"] == "usage" and e["payload"].get("agent") not in ("assistant", None)}
    r.check(used <= allowed, "no helper ran above the user's model", f"helpers used {sorted(m for m in used if m)}; allowed up to {ceiling}")
    r.expect("canberra" in (turn.output + say_text(ev)).lower(), "the answer came back (Canberra)")


def s17_mcp_tool(ctx: Ctx) -> None:
    """A tool from a connected MCP server is used by the real model: it asks first, runs on approval, and stops asking once the tool is marked safe."""
    r = ctx.rep
    sys.path.insert(0, str(ROOT / "tests" / "mcp"))
    sys.path.insert(0, str(ROOT / "tests" / "core"))
    from mcp_fixtures import running_server

    with running_server(token="s17-secret") as (url, seen):
        user = ctx.svc.settings.user_id
        out = ctx.svc.import_mcp_config({"mcpServers": {"calc": {"url": url, "headers": {"Authorization": "Bearer s17-secret"}}}}, user_id=user)
        res = out["results"][0]
        r.check(res["reachable"] and res["tool_count"] == 4, "the server connected and listed its tools", f"{res}")
        s = ctx.session("S17 mcp")
        turn, ev, st = drive(ctx, s.id, "Use the connected calc tool (mcp__calc__add) to add 17 and 25, then tell me the sum.")
        r.check(turn.status == "completed", "the turn completes", f"in {st['seconds']}s")
        r.check("mcp" in st["approvals"], "the connected tool asked for approval first", f"approvals={st['approvals']}")
        r.check(any(e["type"] == "tool_result" and e["payload"].get("name") == "mcp__calc__add" and e["payload"].get("status") == "success" for e in ev), "the tool ran on the server")
        r.expect("42" in (turn.output + say_text(ev)), "the answer used the tool's result (42)")
        r.check("Bearer s17-secret" in seen, "the saved token was sent to the server (decrypted only for the call)")
        ctx.svc.update_mcp_server("calc", {"no_approval_tools": ["add"]}, user_id=user)
        s2 = ctx.session("S17 mcp safe")
        turn2, ev2, st2 = drive(ctx, s2.id, "Use the connected calc tool (mcp__calc__add) to add 100 and 23, then tell me the sum.")
        r.check("mcp" not in st2["approvals"], "a tool marked safe runs without asking", f"approvals={st2['approvals']}")
        r.expect("123" in (turn2.output + say_text(ev2)), "and the answer is right (123)")


SCENARIOS: dict[str, tuple[str, Callable[[Ctx], None]]] = {
    "S1": ("chat only: streaming, no sandbox", s1_chat_only),
    "S2": ("plan, lazy sandbox, file saved as artifact", s2_plan_and_file),
    "S3": ("second message reuses the sandbox", s3_same_sandbox),
    "S4": ("sandbox lost: noticed, replaced, file restored", s4_replaced_sandbox),
    "S5": ("shell command waits for approval", s5_shell_approval),
    "S6": ("the model asks a question", s6_question),
    "S7": ("cancel a running turn", s7_cancel),
    "S8": ("restart while waiting for approval", s8_restart),
    "S9": ("(--email) real e-mail through plan + mailer + approval", s9_email),
    "S10": ("(--github) branch, commit, PR through the agent", s10_github),
    "S11": ("artifacts in Cloud Storage", s11_cloud_storage),
    "S12": ("a long answer streams live", s12_streaming),
    "S13": ("web search (Tavily) through the agent", s13_web_search),
    "S14": ("automatic approval on, then off", s14_auto_approve),
    "S15": ("two helpers in parallel, approvals in one batch", s15_parallel_helpers),
    "S16": ("the orchestrator picks a cheaper model for a helper", s16_helper_model_choice),
    "S17": ("a connected MCP tool: asks, runs, then marked safe", s17_mcp_tool),
}
DEFAULT = ["S1", "S12", "S13", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S11", "S14", "S15", "S16", "S17"]


def run_scenarios(ctx: Ctx, names: list[str]) -> int:
    for name in names:
        title, fn = SCENARIOS[name]
        ctx.rep.scenario = name
        print(f"\n== {name}: {title}")
        t0 = time.monotonic()
        try:
            fn(ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.rep.add("FAIL", "scenario crashed", f"{type(exc).__name__}: {str(exc)[:300]}")
        ctx.rep.info("scenario time", f"{time.monotonic() - t0:.0f}s")
    return ctx.rep.summary()


# ── set-up ───────────────────────────────────────────────────────────────

def preflight(settings, vault, policy) -> None:
    print("what will be used")
    print(f"  project={settings.gcp_project} location={settings.gcp_location} model={policy.default_model} (vertex={settings.google_use_vertex})")
    print(f"  sandbox runtime={'set' if settings.sandbox_runtime else 'NOT SET (a new one would be created)'}  ttl={settings.sandbox_ttl_seconds}s  skills={settings.skills_source}")
    print(f"  artifacts={settings.artifacts_uri}  state=memory  shell approvals=on (answered automatically)")
    print(f"  web search key: {'yes' if vault.get('TAVILY_API_KEY') else 'NO (the agent cannot start without it)'}")
    print(f"  email: {'yes' if vault.get('AGENTMAIL_API_KEY') else 'no'}   github: {'yes' if (settings.github_app_id and vault.get('GITHUB_APP_PRIVATE_KEY')) else 'no'}"
          f"   recipients={list(policy.email.allowed_recipients)}")


def build_real(args):
    os.environ["STATE_BACKEND"] = "memory"  # never touch the development database
    os.environ["APPROVAL_MODE"] = "execute"
    from pinboard.core.config import Settings
    from pinboard.gcp.sandbox import delete_sandbox
    from pinboard.gcp.vault import Vault
    from pinboard.core.policy import load_policy
    from pinboard.engine.service import RunService
    from pinboard.state import make_state
    from pinboard.engine.worker import ThreadExecutor

    import dataclasses

    from pinboard.mcp import generate_key

    os.environ.setdefault("MCP_ENCRYPTION_KEY", generate_key())  # saved MCP tokens are encrypted; this run keeps its own throwaway key
    settings = Settings.from_env()
    vault = Vault(settings.secrets_backend, settings.gcp_project)
    policy = load_policy()
    policy = dataclasses.replace(policy, mcp=dataclasses.replace(policy.mcp, enabled=True, allow_private_hosts=True))  # S17 connects a server on localhost
    preflight(settings, vault, policy)
    state = make_state(settings, vault)

    def new_service():
        return RunService(settings, vault, state.store, state.checkpointer, policy=policy, ledger=state.ledger, executor=ThreadExecutor(2))

    svc = new_service()
    return svc, settings, (lambda name: delete_sandbox(settings, name)), new_service


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated scenarios, e.g. S1,S2")
    ap.add_argument("--email", action="store_true", help="also run S9 (sends one real e-mail)")
    ap.add_argument("--github", metavar="OWNER/REPO", help="also run S10 against this repository")
    ap.add_argument("--dry-run", action="store_true", help="show the configuration and the scenario list, call nothing")
    ap.add_argument("--list", action="store_true", help="list scenarios")
    args = ap.parse_args(argv)
    if args.list:
        for k, (t, _) in SCENARIOS.items():
            print(f"{k:>4}  {t}")
        return 0
    names = [n.strip().upper() for n in args.only.split(",")] if args.only else list(DEFAULT)
    if args.email and "S9" not in names:
        names.append("S9")
    if args.github and "S10" not in names:
        names.append("S10")
    bad = [n for n in names if n not in SCENARIOS]
    if bad:
        print(f"unknown scenario(s): {bad}; use --list")
        return 2

    svc, settings, delete_sandbox, new_service = build_real(args)
    if args.dry_run:
        print("\nwould run: " + ", ".join(names))
        return 0
    ctx = Ctx(svc, Reporter(), delete_sandbox, new_service, args)
    code = run_scenarios(ctx, names)

    print("\n== clean-up")
    try:
        n = svc.sandboxes.sweep(0)
        print(f"  deleted {n} remaining sandbox(es)")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not sweep sandboxes: {exc}. List them with: uv run pinboard runtimes")
    RESULTS_FILE.write_text(json.dumps(ctx.rep.rows, indent=2))
    print(f"  results saved to {RESULTS_FILE}")
    return code


if __name__ == "__main__":
    sys.exit(main())
