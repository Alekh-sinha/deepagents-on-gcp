"""Artifacts through the service: files appear at the end of every turn, survive the sandbox, and the agent can find and restore them."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from pinboard.engine.api import create_app

from helpers import call, say
from turn_harness import BACKENDS, Hub, events_of, kinds

ME = "me@example.com"


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


def hub(backend, tmp_path, monkeypatch, scripts, **kw):
    h = Hub(backend, tmp_path, monkeypatch, scripts, **kw)
    h.new_process()
    return h


def out_path(h, name="sb-1", rel="report.md"):
    return f"{h.sbdir(name)}/out/{rel}"


def write(h, rel, content, cid="c1", name="sb-1"):
    return call("write_file", {"file_path": out_path(h, name, rel), "content": content}, cid)


def test_files_in_the_output_folder_become_artifacts_before_the_turn_ends(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("placeholder")]])
    h.scripts[0] = [write(h, "report.md", "# Q3 report"), write(h, "data/rows.csv", "a,b\n1,2\n", "c2"), say("saved both")]
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "make a report")
    assert run.status == "completed"
    evs = events_of(h.svc, run.id)
    arts = [e for e in evs if e["type"] == "artifact"]
    assert sorted(e["payload"]["path"] for e in arts) == ["data/rows.csv", "report.md"]
    assert all(e["seq"] < evs[-1]["seq"] for e in arts) and kinds(evs)[-1] == "turn_done"  # the files appear BEFORE the turn is over
    listed = h.svc.list_artifacts(session_id=s.id)
    assert sorted(a["path"] for a in listed) == ["data/rows.csv", "report.md"] and "blob_key" not in listed[0]
    meta, data = h.svc.read_artifact(next(a["id"] for a in listed if a["path"] == "report.md"))
    assert data == b"# Q3 report" and meta["content_type"] == "text/markdown"
    assert h.svc.store.get_session(s.id).sandbox_name == "sb-1"  # and the sandbox itself is untouched
    h.close()


def test_an_unchanged_file_is_not_announced_again_and_a_changed_one_is_a_new_version(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]])
    h.scripts[:] = [[write(h, "r.md", "v1"), say("one")], [say("nothing new")], [call("edit_file", {"file_path": out_path(h, "sb-1", "r.md"), "old_string": "v1", "new_string": "v2"}), say("edited")]]
    s = h.svc.create_session()
    runs = []
    for text in ("make it", "just chat", "change it"):
        runs.append(h.svc.send_message(s.id, text))
        h.clock.advance(minutes=1)  # the harness clock is frozen; real time never ties like that
    r1, r2, r3 = runs
    assert [kinds(events_of(h.svc, r.id)).count("artifact") for r in (r1, r2, r3)] == [1, 0, 1]
    versions = [a for a in h.svc.list_artifacts(session_id=s.id) if a["path"] == "r.md"]
    assert len(versions) == 2 and versions[0]["run_id"] == r3.id
    assert h.svc.read_artifact(versions[0]["id"])[1] == b"v2" and h.svc.read_artifact(versions[1]["id"])[1] == b"v1"
    h.close()


def test_files_are_saved_even_when_the_turn_parks_is_cancelled_or_fails(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]], approval_mode="execute")
    h.scripts[:] = [[write(h, "early.md", "saved before the pause"), call("execute", {"command": "echo hi"}, "c2"), say("x")]]
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "go")
    assert run.status == "awaiting_approval"
    assert [a["path"] for a in h.svc.list_artifacts(session_id=s.id)] == ["early.md"]  # safe even if the sandbox is later swept
    ev = events_of(h.svc, run.id)
    assert kinds(ev).index("artifact") < kinds(ev).index("interrupt")
    h.close()


def test_a_sandbox_that_was_never_used_has_nothing_to_save_and_is_not_created(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("just talking")]])
    s = h.svc.create_session()
    h.svc.send_message(s.id, "hello")
    assert h.svc.list_artifacts() == []
    h.close()


def test_artifacts_belong_to_the_user_not_the_session(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]])
    h.scripts[:] = [[write(h, "mine.md", "private"), say("ok")], [say("someone else")]]
    a = h.svc.create_session(user_id=ME)
    h.svc.send_message(a.id, "write", user_id=ME)
    art = h.svc.list_artifacts(user_id=ME)[0]
    assert h.svc.list_artifacts() == []  # the owner account sees none of it
    b = h.svc.create_session(user_id=ME)  # a different conversation of the same user sees it
    assert [x["id"] for x in h.svc.list_artifacts(user_id=ME)] == [art["id"]]
    from pinboard.engine.errors import NotFound

    with pytest.raises(NotFound):
        h.svc.get_artifact(art["id"])  # another user's id looks like a missing one
    with pytest.raises(NotFound):
        h.svc.read_artifact(art["id"])
    with pytest.raises(NotFound):
        h.svc.artifact_url(art["id"])
    with pytest.raises(NotFound):
        h.svc.list_artifacts(session_id=a.id)  # not even through the session id
    assert b.id != a.id
    h.close()


def test_the_agent_can_list_and_restore_files_after_its_sandbox_is_gone(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]])
    h.scripts[:] = [[write(h, "notes.md", "important notes"), say("written")], [say("placeholder")]]
    s = h.svc.create_session(user_id=ME)
    h.svc.send_message(s.id, "write notes", user_id=ME)
    art_id = h.svc.list_artifacts(user_id=ME)[0]["id"]
    h.farm.alive.discard("sb-1")  # the sandbox disappears; the saved file must not
    h.scripts[1] = [call("list_artifacts", {}, "c1"), call("restore_artifact", {"artifact_id": art_id}, "c2"),
                    call("read_file", {"file_path": f"{h.sbdir('sb-2')}/restored/notes.md"}, "c3"), say("brought it back")]
    run = h.svc.send_message(s.id, "I need my notes again", user_id=ME)
    assert run.status == "completed" and (h.sbdir("sb-2") / "restored" / "notes.md").read_text() == "important notes"
    seen = " ".join(str(m.content) for msgs in h.models[-1].seen for m in msgs if getattr(m, "type", "") == "tool")
    assert art_id in seen and "notes.md" in seen and "Restored notes.md" in seen and "important notes" in seen
    note = [m.content for msgs in h.models[-1].seen for m in msgs if getattr(m, "type", "") == "human"][-1]
    assert "list_artifacts" in note and "restore_artifact" in note  # the replaced-sandbox note points the agent at them
    h.close()


def test_restoring_is_confined_to_the_working_folder_and_to_the_users_own_files(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]])
    h.scripts[:] = [[write(h, "f.md", "data"), say("ok")], [say("x")]]
    s = h.svc.create_session(user_id=ME)
    h.svc.send_message(s.id, "w", user_id=ME)
    art_id = h.svc.list_artifacts(user_id=ME)[0]["id"]
    skills = h.settings.skills_path
    h.scripts[1] = [call("restore_artifact", {"artifact_id": art_id, "to_path": "/etc/cron.d/evil"}, "c1"),
                    call("restore_artifact", {"artifact_id": art_id, "to_path": f"{skills}/skill/SKILL.md"}, "c2"),
                    call("restore_artifact", {"artifact_id": art_id, "to_path": f"{h.sbdir('sb-1')}/../escape.md"}, "c3"),
                    call("restore_artifact", {"artifact_id": "a_not_mine"}, "c4"), say("done")]
    h.svc.send_message(s.id, "try bad restores", user_id=ME)
    results = {m.tool_call_id: m.content for msgs in h.models[-1].seen for m in msgs if getattr(m, "type", "") == "tool"}
    assert all("Cannot restore" in results[c] for c in ("c1", "c2", "c3")) and "No such saved file" in results["c4"]
    assert not (tmp_path / "sandboxes" / "escape.md").exists() and not any(h.sbdir("sb-1").rglob("evil"))
    h.close()


# ── over HTTP ────────────────────────────────────────────────────────────

def test_artifacts_over_the_contract_and_rest(tmp_path, monkeypatch):
    h = hub("memory", tmp_path, monkeypatch, [[say("x")]])
    h.scripts[:] = [[write(h, "reports/q3.md", "# Q3 numbers"), write(h, "big.bin", "z" * 10, "c2"), say("done")]]
    c = TestClient(create_app(h.svc, "t" * 32))
    auth = {"Authorization": "Bearer " + "t" * 32}

    def contract(method, inp=None):
        body = json.dumps({"class_method": method, "input": inp or {}})
        return c.post("/api/reasoning_engine", content=json.dumps(body), headers={**auth, "content-type": "application/json"})

    s = c.post("/v1/sessions", json={}, headers=auth).json()
    c.post(f"/v1/sessions/{s['id']}/messages", json={"text": "make files"}, headers=auth)
    listed = contract("list_artifacts", {"session_id": s["id"]}).json()["output"]
    assert sorted(a["path"] for a in listed) == ["big.bin", "reports/q3.md"]
    q3 = next(a for a in listed if a["path"] == "reports/q3.md")
    assert contract("get_artifact", {"artifact_id": q3["id"]}).json()["output"]["size"] == 12
    got = contract("read_artifact", {"artifact_id": q3["id"]}).json()["output"]
    assert base64.b64decode(got["content_base64"]) == b"# Q3 numbers" and got["artifact"]["name"] == "q3.md"
    assert contract("get_artifact_url", {"artifact_id": q3["id"]}).json()["output"] == {"url": None}  # local store: no signed links
    assert contract("get_artifact", {"artifact_id": "a_missing"}).status_code == 404
    assert contract("get_artifact", {}).status_code == 400 and contract("list_artifacts", {"limit": 9999}).status_code == 400

    r = c.get(f"/v1/artifacts/{q3['id']}/content", headers=auth)
    assert r.status_code == 200 and r.content == b"# Q3 numbers" and r.headers["content-type"].startswith("text/markdown")
    assert r.headers["content-disposition"] == 'attachment; filename="q3.md"' and r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/v1/artifacts", params={"session_id": s["id"]}, headers=auth).json()["artifacts"][0]["session_id"] == s["id"]
    assert c.get("/v1/artifacts/a_missing/content", headers=auth).status_code == 404 and c.get("/v1/artifacts").status_code == 401
    other = contract("list_artifacts", {"user_id": ME}).json()["output"]
    assert other == []  # a different user sees none of it
    h.close()
