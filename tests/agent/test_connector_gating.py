"""End to end: the real agent + scripted model + fake services.
Email and GitHub tools live in the subagents (mailer / coder), and every WRITE they make must ask a human first."""
import copy

import pytest

from pinboard.agent import build_agent, resume, run
from pinboard.agent.tools.connectors import email_connector, github_connector
from pinboard.agent.tools.effects import InMemoryLedger
from pinboard.integrations.github import GitHubApp
from pinboard.core.guardrails import CommandGuard
from pinboard.agent.models import ModelRouter
from pinboard.core.policy import parse_policy
from pinboard.gcp.sandbox import GCPShellSandbox
from pinboard.gcp.vault import Vault

from helpers import FakeClient, FakeMail, ScriptedModel, call, make_settings, say
from test_connector_github import PRIV, REPO, new_http, repo_routes
from test_policy import BASE

GOOD = {"to": ["me@example.com"], "subject": "Report", "body": "Here you go."}


def delegated(subagent, inner, sub_final="sub done", main_final="done"):
    """Script: main hands a job to `subagent`, which makes the tool call `inner`, finishes, and main finishes."""
    return [call("task", {"description": "do the job", "subagent_type": subagent}, "c1"), inner, say(sub_final), say(main_final)]


def policy():
    d = copy.deepcopy(BASE)
    d["models"] = {"default": "m1", "allowed": [{"id": "m1", "provider": "google_genai"}]}
    return parse_policy(d)


def gh_posts(w):
    return [c for c in w.http.calls if c["method"] == "POST" and c["path"].endswith("/git/refs")]


def tool_texts(model):
    """Every tool-message text any model call has seen (this includes what happened inside subagents)."""
    return [m.content for msgs in model.seen for m in msgs if getattr(m, "type", "") == "tool"]


def make_world(tmp_path, mode, approval_mode="none"):
    class W:
        pass

    w = W()
    w.mail, w.http, w.ledger = FakeMail(), new_http(), InMemoryLedger()
    repo_routes(w.http)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    w.pol = policy()
    conn = email_connector(w.mail, w.pol, "bot@agentmail.to", ledger=w.ledger).merge(
        github_connector(GitHubApp("1", PRIV, http=w.http), w.pol, ledger=w.ledger))
    settings = make_settings(ws, skills_source=None, approval_mode=approval_mode)  # APPROVAL_MODE=none must NOT switch the gates off
    sb = GCPShellSandbox(FakeClient(ws), "fake", guard=CommandGuard("allowlist", str(ws)), cwd=str(ws))

    def build(script):
        w.model = ScriptedModel(script=script)
        if mode == "fixed-model":
            return build_agent(settings, Vault("env"), model=w.model, sandbox=sb, tools=[], connectors=conn)
        return build_agent(settings, Vault("env"), sandbox=sb, tools=[], connectors=conn, router=ModelRouter(w.pol, lambda spec: w.model))

    w.build, w.conn, w.sb = build, conn, sb
    return w


@pytest.fixture(params=["fixed-model", "policy-router"])
def world(request, tmp_path):
    return make_world(tmp_path, request.param)


def test_send_email_parks_for_approval_even_when_approval_mode_is_none(world):
    rt = world.build(delegated("mailer", call("send_email", GOOD, "c2")))
    parked = run(rt, "mail me the report", thread_id="t")
    assert parked.status == "awaiting_approval" and parked.pending[0]["name"] == "send_email"
    assert parked.pending[0]["args"]["to"] == ["me@example.com"] and world.mail.sent == []
    assert parked.pending[0]["allowed_decisions"] == ["approve", "edit", "reject"]
    done = resume(rt, "t", [{"type": "approve"}])
    assert done.status == "completed" and done.output == "done" and len(world.mail.sent) == 1


def test_reject_sends_nothing_and_tells_the_model(world):
    rt = world.build(delegated("mailer", call("send_email", GOOD, "c2")))
    run(rt, "mail me", thread_id="t")
    done = resume(rt, "t", [{"type": "reject", "message": "not now, thanks"}])
    assert done.status == "completed" and world.mail.sent == []
    assert any("not now, thanks" in t for t in tool_texts(world.model))


def test_edit_is_allowed_but_policy_is_rechecked_on_the_edited_arguments(world):
    rt = world.build(delegated("mailer", call("send_email", GOOD, "c2")))
    run(rt, "mail me", thread_id="t")
    resume(rt, "t", [{"type": "edit", "edited_action": {"name": "send_email", "args": {**GOOD, "subject": "Edited subject"}}}])
    assert world.mail.sent[0]["subject"] == "Edited subject"

    rt2 = world.build(delegated("mailer", call("send_email", GOOD, "c2")))
    run(rt2, "mail me", thread_id="t2")
    resume(rt2, "t2", [{"type": "edit", "edited_action": {"name": "send_email", "args": {**GOOD, "to": ["evil@x.com"]}}}])
    assert len(world.mail.sent) == 1  # editing cannot widen the allowlist
    assert any("Blocked by policy" in t for t in tool_texts(world.model))


def test_a_policy_denied_send_is_refused_at_once_and_never_shown_to_the_human(world):
    rt = world.build(delegated("mailer", call("send_email", {**GOOD, "to": ["evil@x.com"]}, "c2")))
    res = run(rt, "mail evil", thread_id="t")
    assert res.status == "completed" and res.pending == [] and world.mail.sent == []
    assert any("Blocked by policy" in t for t in tool_texts(world.model))


def test_reading_never_asks_for_approval(world):
    script = [call("task", {"description": "read my inbox", "subagent_type": "mailer"}, "c1"), call("list_emails", {}, "c2"),
              call("read_email", {"message_id": "m1"}, "c3"), say("inbox summarised"), say("done")]
    res = run(world.build(script), "what is in my inbox", thread_id="t")
    assert res.status == "completed" and res.output == "done"
    assert any("untrusted_content" in t for t in tool_texts(world.model))


def test_github_writes_are_gated_but_policy_denied_ones_are_not(world):
    bad = world.build([call("github_create_branch", {"repo": REPO, "branch": "main"}, "c2"), say("could not")])
    res = run(bad, "push to main", thread_id="a")
    assert res.status == "completed" and res.pending == [] and not gh_posts(world)
    good = world.build([call("github_create_branch", {"repo": REPO, "branch": "agent/x"}, "c2"), say("branch made")])
    parked = run(good, "make a branch", thread_id="b")
    assert parked.status == "awaiting_approval" and parked.pending[0]["name"] == "github_create_branch" and not gh_posts(world)
    assert resume(good, "b", [{"type": "approve"}]).status == "completed" and gh_posts(world)


def test_the_orchestrator_pushes_and_the_coder_can_only_read_github(world):
    """One party changes GitHub, so parallel coders can never push competing branches. The coder may still read."""
    push = world.build(delegated("coder", call("github_create_branch", {"repo": REPO, "branch": "agent/x"}, "c2")))
    res = run(push, "let the coder push", thread_id="a")
    assert res.status == "completed" and res.pending == [] and not gh_posts(world)
    assert any("not a valid tool" in t for t in tool_texts(world.model))
    read = world.build(delegated("coder", call("github_read_file", {"repo": REPO, "path": "README.md"}, "c2")))
    assert run(read, "let the coder look", thread_id="b").pending == []  # reading never asks


def test_the_main_agent_holds_github_but_not_email(world):
    rt = world.build([call("send_email", GOOD, "c1"), say("could not")])
    res = run(rt, "just mail it yourself", thread_id="t")
    assert res.status == "completed" and res.pending == [] and world.mail.sent == []
    assert any("not a valid tool" in t for t in tool_texts(world.model))
