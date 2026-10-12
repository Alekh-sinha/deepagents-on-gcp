"""Approvals carry a kind + preview + allowed decisions, and the service enforces which decisions a gate accepts."""
import pytest
from fastapi.testclient import TestClient

from pinboard.engine.api import create_app
from pinboard.engine.errors import BadRequest

from helpers import call, say
from test_service import backend, events, world  # noqa: F401  (fixture `backend`, helper `world`)

PLAN = {"summary": "Write and send", "tasks": [{"title": "Write", "detail": "", "assignee": "assistant"}, {"title": "Send", "detail": "", "assignee": "mailer"}]}
TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_a_plan_approval_is_described_for_the_reviewer_and_resumes_on_approve(backend, tmp_path, monkeypatch):
    w = world(backend, tmp_path, monkeypatch, [call("submit_plan", PLAN), say("done")], approval_mode="none")
    svc = w.new_process()
    run = svc.start_run("write a report and mail it")
    assert run.status == "awaiting_approval"
    appr = svc.list_approvals(status="pending")[0]
    pv = appr.previews[0]
    assert pv["kind"] == "plan" and pv["title"] == "Approve the plan" and pv["allowed_decisions"] == ["approve", "edit", "reject"]
    assert [t["assignee"] for t in pv["preview"]["tasks"]] == ["assistant", "mailer"]
    assert appr.to_dict()["previews"][0]["kind"] == "plan"
    done = svc.decide(appr.id, "approve")
    assert done.status == "completed" and events(svc, run.id) == ["run.started", "approval.requested", "approval.approved", "run.completed"]
    w.close()


def test_a_question_accepts_only_an_answer(backend, tmp_path, monkeypatch):
    w = world(backend, tmp_path, monkeypatch, [call("ask_user", {"question": "Which format?", "options": ["pdf", "md"]}), say("done")], approval_mode="none")
    svc = w.new_process()
    run = svc.start_run("write it")
    appr = svc.list_approvals(status="pending")[0]
    assert appr.previews[0]["kind"] == "question" and appr.previews[0]["allowed_decisions"] == ["respond"]
    assert appr.previews[0]["preview"] == {"question": "Which format?", "options": ["pdf", "md"]}
    with pytest.raises(BadRequest, match="not allowed"):
        svc.decide(appr.id, "approve")
    with pytest.raises(BadRequest, match="not allowed"):
        svc.decide(appr.id, "reject")
    with pytest.raises(BadRequest, match="non-empty"):
        svc.decide(appr.id, decisions=[{"type": "respond", "message": "  "}])
    assert svc.get_run(run.id).status == "awaiting_approval"  # none of the refused attempts consumed the approval
    done = svc.decide(appr.id, decisions=[{"type": "respond", "message": "markdown"}])
    assert done.status == "completed"
    assert svc.audit(run.id)[2]["detail"]["decisions"] == [{"type": "respond", "message": "markdown"}]
    w.close()


def test_respond_is_refused_for_gates_that_do_not_allow_it(backend, tmp_path, monkeypatch):
    w = world(backend, tmp_path, monkeypatch, [call("execute", {"command": "echo hi"}), say("done")])
    svc = w.new_process()
    svc.start_run("go")
    appr = svc.list_approvals(status="pending")[0]
    assert appr.previews[0]["kind"] == "shell" and appr.previews[0]["preview"] == {"command": "echo hi"}
    with pytest.raises(BadRequest, match="not allowed"):
        svc.decide(appr.id, decisions=[{"type": "respond", "message": "sure"}])
    w.close()


def test_the_http_api_exposes_previews_and_accepts_an_answer(tmp_path, monkeypatch):
    w = world("memory", tmp_path, monkeypatch, [call("ask_user", {"question": "Which?"}), say("done")], approval_mode="none")
    c = TestClient(create_app(w.new_process(), TOKEN))
    c.post("/v1/runs", json={"prompt": "go"}, headers=AUTH)
    appr = c.get("/v1/approvals?status=pending", headers=AUTH).json()["approvals"][0]
    assert appr["previews"][0]["kind"] == "question" and appr["previews"][0]["allowed_decisions"] == ["respond"]
    assert c.post(f"/v1/approvals/{appr['id']}/decision", json={"decision": "approve"}, headers=AUTH).status_code == 400
    ok = c.post(f"/v1/approvals/{appr['id']}/decision", json={"decisions": [{"type": "respond", "message": "this one"}]}, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["status"] == "completed"
