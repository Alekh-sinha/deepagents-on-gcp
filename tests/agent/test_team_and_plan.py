"""M3: the plan gate, asking the user, the least-privilege team, and how approvals are described for a reviewer."""
import pytest

from pinboard.agent import resume, run
from pinboard.agent.approvals import describe, describe_all

from helpers import call, say
from test_connector_gating import GOOD, REPO, delegated, gh_posts, make_world, tool_texts

PLAN = {"summary": "Email the report", "tasks": [
    {"title": "Write the report", "detail": "markdown, 1 page", "assignee": "assistant"},
    {"title": "Send it", "detail": "to the user", "assignee": "mailer"}]}


@pytest.fixture(params=["fixed-model", "policy-router"])
def world(request, tmp_path):
    return make_world(tmp_path, request.param)


# ── the plan gate ────────────────────────────────────────────────────────

def test_the_plan_waits_for_approval_even_when_approval_mode_is_none(world):
    rt = world.build([call("submit_plan", PLAN), say("working on it")])
    parked = run(rt, "prepare a report and mail it", thread_id="t")
    assert parked.status == "awaiting_approval" and parked.pending[0]["name"] == "submit_plan"
    assert parked.pending[0]["allowed_decisions"] == ["approve", "edit", "reject"]
    done = resume(rt, "t", [{"type": "approve"}])
    assert done.status == "completed" and done.output == "working on it"
    text = "\n".join(tool_texts(world.model))
    assert "Plan approved by the user" in text and "1. [assistant] Write the report" in text and "2. [mailer] Send it" in text


def test_an_edited_plan_is_what_the_agent_carries_out(world):
    rt = world.build([call("submit_plan", PLAN), say("ok")])
    run(rt, "go", thread_id="t")
    edited = {**PLAN, "tasks": [PLAN["tasks"][0]]}  # the user removed the email step
    resume(rt, "t", [{"type": "edit", "edited_action": {"name": "submit_plan", "args": edited}}])
    text = "\n".join(tool_texts(world.model))
    assert "Write the report" in text and "Send it" not in text


def test_a_rejected_plan_carries_the_feedback_back_to_the_model(world):
    rt = world.build([call("submit_plan", PLAN), say("I will revise")])
    run(rt, "go", thread_id="t")
    done = resume(rt, "t", [{"type": "reject", "message": "too many steps, just write it"}])
    assert done.status == "completed" and any("too many steps" in t for t in tool_texts(world.model))


@pytest.mark.parametrize("bad,expect", [
    ({"summary": "x", "tasks": [{"title": "t", "detail": "", "assignee": "hacker"}]}, "assistant"),   # rejected by the tool's type schema
    ({"summary": "x", "tasks": []}, "plan.invalid"),                                                   # passes the schema, fails the plan check
    ({"summary": "  ", "tasks": [{"title": "t", "detail": "", "assignee": "coder"}]}, "plan.invalid"),
    ({"summary": "x", "tasks": [{"title": "   ", "detail": "", "assignee": "coder"}]}, "plan.invalid"),
])
def test_an_unusable_plan_is_bounced_to_the_model_without_asking_the_human(world, bad, expect):
    rt = world.build([call("submit_plan", bad), say("fixing it")])
    res = run(rt, "go", thread_id="t")
    assert res.status == "completed" and res.pending == [] and res.output == "fixing it"
    assert any(expect in t for t in tool_texts(world.model))


# ── asking the user ──────────────────────────────────────────────────────

def test_ask_user_returns_the_typed_answer_as_the_tool_result(world):
    rt = world.build([call("ask_user", {"question": "Which format?", "options": ["pdf", "md"]}), say("using markdown")])
    parked = run(rt, "write the report", thread_id="t")
    assert parked.status == "awaiting_approval" and parked.pending[0]["name"] == "ask_user"
    assert parked.pending[0]["allowed_decisions"] == ["respond"]
    done = resume(rt, "t", [{"type": "respond", "message": "markdown please"}])
    assert done.status == "completed" and "markdown please" in tool_texts(world.model)


def test_a_question_cannot_be_approved_only_answered(world):
    rt = world.build([call("ask_user", {"question": "Which format?"}), say("x")])
    run(rt, "go", thread_id="t")
    with pytest.raises(ValueError, match="not allowed"):
        resume(rt, "t", [{"type": "approve"}])


# ── least privilege: who can hold what ───────────────────────────────────

@pytest.mark.parametrize("who,forbidden", [
    ("researcher", call("send_email", GOOD, "c2")),
    ("researcher", call("github_create_branch", {"repo": REPO, "branch": "agent/x"}, "c2")),
    ("coder", call("send_email", GOOD, "c2")),
    ("coder", call("github_create_branch", {"repo": REPO, "branch": "agent/x"}, "c2")),
    ("coder", call("github_commit_files", {"repo": REPO, "branch": "agent/x", "message": "m", "files": []}, "c2")),
    ("coder", call("github_open_pr", {"repo": REPO, "head": "agent/x", "title": "t"}, "c2")),
    ("mailer", call("github_create_branch", {"repo": REPO, "branch": "agent/x"}, "c2")),
    ("general-purpose", call("send_email", GOOD, "c2")),
])
def test_no_helper_can_use_a_connector_it_was_not_given(world, who, forbidden):
    res = run(world.build(delegated(who, forbidden)), "try it", thread_id="t")
    assert res.status == "completed" and res.pending == [] and world.mail.sent == [] and not gh_posts(world)
    assert any("not a valid tool" in t for t in tool_texts(world.model))


def test_the_team_can_do_its_own_jobs(world):
    mail = run(world.build(delegated("mailer", call("send_email", GOOD, "c2"))), "mail", thread_id="m")
    assert mail.status == "awaiting_approval" and mail.pending[0]["name"] == "send_email"
    code = run(world.build([call("github_create_branch", {"repo": REPO, "branch": "agent/x"}, "c2"), say("done")]), "branch", thread_id="c")  # the orchestrator publishes
    assert code.status == "awaiting_approval" and code.pending[0]["name"] == "github_create_branch"


def test_with_shell_approvals_on_the_coder_and_the_assistant_both_ask_first(tmp_path):
    w = make_world(tmp_path, "fixed-model", approval_mode="execute")
    parked = run(w.build([call("execute", {"command": "echo main"}, "c1"), say("done")]), "run it", thread_id="a")
    assert parked.status == "awaiting_approval" and parked.pending[0]["name"] == "execute"
    sub = run(w.build(delegated("coder", call("execute", {"command": "echo sub"}, "c2"))), "delegate", thread_id="b")
    assert sub.status == "awaiting_approval" and sub.pending[0]["args"]["command"] == "echo sub"


def test_the_system_prompt_describes_the_team_and_the_plan_rule():
    from pinboard.agent import SYSTEM_PROMPT

    for needle in ("submit_plan", "ask_user", "researcher", "coder", "mailer", "write_todos", "untrusted"):
        assert needle in SYSTEM_PROMPT
    assert "not available yet" not in SYSTEM_PROMPT


# ── how approvals are described ──────────────────────────────────────────

def test_describe_gives_each_gate_a_kind_and_a_compact_preview():
    d = describe({"name": "send_email", "args": {**GOOD, "cc": None, "attachments": ["/workspace/out/r.md"]}})
    assert d["kind"] == "email" and d["preview"]["to"] == ["me@example.com"] and d["preview"]["attachments"] == ["/workspace/out/r.md"]
    assert d["allowed_decisions"] == ["approve", "edit", "reject"] and "Report" in d["title"]
    assert describe({"name": "execute", "args": {"command": "ls"}})["kind"] == "shell"
    p = describe({"name": "submit_plan", "args": PLAN})
    assert p["kind"] == "plan" and len(p["preview"]["tasks"]) == 2
    q = describe({"name": "ask_user", "args": {"question": "Which?"}, "allowed_decisions": ["respond"]})
    assert q["kind"] == "question" and q["allowed_decisions"] == ["respond"]
    g = describe({"name": "github_commit_files", "args": {"repo": REPO, "branch": "agent/x", "message": "m",
                                                           "files": [{"path": "a.md", "content": "hello"}, {"path": "b.pdf", "from_sandbox": "/workspace/out/b.pdf"}]}})
    assert g["kind"] == "github" and g["preview"]["files"][0]["chars"] == 5 and g["preview"]["files"][1]["source"] == "/workspace/out/b.pdf"
    assert describe({"name": "something_new", "args": {"x": 1}})["kind"] == "other"


def test_describe_clips_huge_text_and_tolerates_odd_arguments():
    big = describe({"name": "send_email", "args": {"to": ["a@b.c"], "subject": "s" * 500, "body": "x" * 50_000}})
    assert len(big["preview"]["body"]) < 4200 and "more characters" in big["preview"]["body"] and len(big["title"]) < 120
    odd = describe({"name": "github_commit_files", "args": {"files": ["not a dict", None], "repo": None}})
    assert odd["kind"] == "github" and odd["preview"]["files"] == []
    assert [d["kind"] for d in describe_all([{"name": "execute", "args": {}}, {"name": "send_email", "args": {}}])] == ["shell", "email"]


def test_the_agent_really_has_every_tool_the_prompt_names(world):
    """Regression: deepagents 0.7.x ships no write_todos by default, yet the prompt told the model to use it."""
    from pinboard.agent import SYSTEM_PROMPT

    rt = world.build([say("x")])
    names = set(rt.agent.nodes["tools"].bound.tools_by_name)
    assert {"write_todos", "submit_plan", "ask_user", "task", "execute", "write_file", "read_file"} <= names
    for tool_name in ("write_todos", "submit_plan", "ask_user"):  # every tool the prompt tells the model to use must exist
        assert tool_name in SYSTEM_PROMPT and tool_name in names
    assert {"github_create_branch", "github_commit_files", "github_open_pr", "github_list_repos"} <= names  # the orchestrator publishes
    assert "send_email" not in names  # email stays with the mailer


def test_todo_updates_arrive_as_state_updates_the_ui_can_render(world):
    todos = [{"content": "Write the report", "status": "in_progress"}, {"content": "Send it", "status": "pending"}]
    rt = world.build([call("write_todos", {"todos": todos}), say("ok")])
    seen = [upd["todos"] for _, mode, data in rt.agent.stream({"messages": [{"role": "user", "content": "go"}]}, config={"configurable": {"thread_id": "t"}},
                                                           stream_mode=["updates"], subgraphs=True)
            for upd in (data.values() if mode == "updates" else []) if isinstance(upd, dict) and "todos" in upd]
    assert seen and seen[-1][0]["content"] == "Write the report" and seen[-1][1]["status"] == "pending"
