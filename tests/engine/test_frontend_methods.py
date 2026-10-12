"""The methods a frontend needs on top of sessions and turns: get_config, update_session (rename / model / archive), the archived filter on
list_sessions, and get_transcript (the conversation in one paged call). Service level on both backends, then through the HTTP routes."""
import pytest

from pinboard.engine import sessions as sessions_module
from pinboard.engine.errors import BadRequest, Conflict, NotFound

from helpers import call, say
from test_api_sessions import AUTH, ME, contract, make, out
from turn_harness import BACKENDS, Hub, kinds


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


def hub(backend, tmp_path, monkeypatch, scripts, **kw):
    h = Hub(backend, tmp_path, monkeypatch, scripts, **kw)
    h.new_process()
    return h


# ── get_config ───────────────────────────────────────────────────────────

def test_config_describes_the_rules_in_force_and_holds_no_secrets(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]], approval_mode="execute")
    cfg = h.svc.get_config(user_id=ME)
    assert cfg["user"] == ME and cfg["default_model"] == "m1" and [m["id"] for m in cfg["models"]] == ["m1", "m2"]
    assert cfg["email"]["allowed_recipients"] and cfg["github"]["branch_prefix"] and cfg["limits"]["max_concurrent_turns"] == 4
    assert cfg["approvals"]["shell_commands"] is True
    flat = str(cfg).lower()
    assert "token" not in flat and "password" not in flat and "api_key" not in flat
    h.close()


# ── update_session ───────────────────────────────────────────────────────

def test_a_session_can_be_renamed_given_a_model_and_archived(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]])
    svc = h.svc
    s = svc.create_session(user_id=ME, title="old")
    assert svc.update_session(s.id, {"title": "  New name  "}, user_id=ME).title == "New name"
    assert svc.update_session(s.id, {"model": "m1"}, user_id=ME).model == "m1"
    assert svc.update_session(s.id, {"model": None}, user_id=ME).model is None  # back to the policy default
    assert svc.update_session(s.id, {"model": ""}, user_id=ME).model is None
    assert [x.id for x in svc.list_sessions(user_id=ME)] == [s.id]
    assert svc.update_session(s.id, {"archived": True}, user_id=ME).archived is True
    assert svc.list_sessions(user_id=ME) == [] and [x.id for x in svc.list_sessions(user_id=ME, include_archived=True)] == [s.id]
    assert svc.update_session(s.id, {"archived": False}, user_id=ME).archived is False
    h.close()


@pytest.mark.parametrize("changes, message", [
    ({}, "at least one"), ({"sandbox_name": "x"}, "cannot change"), ({"title": 5}, "title"), ({"title": "x" * 121}, "title"),
    ({"archived": "yes"}, "archived"), ({"model": "gpt-9"}, "not available"),
])
def test_bad_session_changes_are_refused_and_change_nothing(backend, tmp_path, monkeypatch, changes, message):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]])
    s = h.svc.create_session(user_id=ME, title="keep")
    with pytest.raises(BadRequest, match=message):
        h.svc.update_session(s.id, changes, user_id=ME)
    after = h.svc.store.get_session(s.id)
    assert (after.title, after.model, after.archived) == ("keep", None, False)
    h.close()


def test_someone_elses_session_cannot_be_changed_or_read(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say("x")]])
    mine = h.svc.create_session(user_id=ME)
    with pytest.raises(NotFound):
        h.svc.update_session(mine.id, {"title": "mine now"}, user_id="other@example.com")
    with pytest.raises(NotFound):
        h.svc.get_transcript(mine.id, user_id="other@example.com")
    assert h.svc.store.get_session(mine.id).title == ""
    h.close()


def test_a_session_with_a_turn_waiting_for_approval_cannot_be_archived(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("execute", {"command": "echo hi"}), say("x")]], approval_mode="execute")
    s = h.svc.create_session(user_id=ME)
    assert h.svc.send_message(s.id, "run it", user_id=ME).status == "awaiting_approval"
    with pytest.raises(Conflict, match="awaiting approval"):
        h.svc.update_session(s.id, {"archived": True}, user_id=ME)
    assert h.svc.update_session(s.id, {"title": "renaming is fine"}, user_id=ME).title == "renaming is fine"
    h.close()


# ── get_transcript ───────────────────────────────────────────────────────

def talk(h, n):
    s = h.svc.create_session(user_id=ME)
    return s, [h.svc.send_message(s.id, f"question {i}", user_id=ME) for i in range(n)]


def test_the_transcript_is_the_latest_turns_oldest_first_with_their_events(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[say(f"answer {i}")] for i in range(5)])
    s, turns = talk(h, 5)
    t = h.svc.get_transcript(s.id, user_id=ME, limit=2)
    assert [x["turn"]["prompt"] for x in t["turns"]] == ["question 3", "question 4"] and t["has_more"] is True
    first = t["turns"][0]
    assert kinds(first["events"]) == ["user_message", "turn_started", "assistant_message", "turn_done"]
    assert first["events"][2]["payload"]["text"] == "answer 3" and first["truncated"] is False and first["next_seq"] == first["events"][-1]["seq"]

    older = h.svc.get_transcript(s.id, user_id=ME, limit=2, before_turn_id=t["turns"][0]["turn"]["id"])
    assert [x["turn"]["prompt"] for x in older["turns"]] == ["question 1", "question 2"] and older["has_more"] is True
    oldest = h.svc.get_transcript(s.id, user_id=ME, limit=2, before_turn_id=older["turns"][0]["turn"]["id"])
    assert [x["turn"]["prompt"] for x in oldest["turns"]] == ["question 0"] and oldest["has_more"] is False
    h.close()


def test_the_transcript_limit_is_clamped_and_unknown_turns_are_not_found(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(sessions_module, "TRANSCRIPT_MAX_TURNS", 2)
    h = hub(backend, tmp_path, monkeypatch, [[say(f"a{i}")] for i in range(3)])
    s, _ = talk(h, 3)
    lowest = h.svc.get_transcript(s.id, user_id=ME, limit=0)
    assert [x["turn"]["prompt"] for x in lowest["turns"]] == ["question 2"] and lowest["has_more"] is True  # at least one: the latest
    highest = h.svc.get_transcript(s.id, user_id=ME, limit=10_000)
    assert [x["turn"]["prompt"] for x in highest["turns"]] == ["question 1", "question 2"]  # never more than the cap
    with pytest.raises(NotFound):
        h.svc.get_transcript(s.id, user_id=ME, before_turn_id="run_nope")
    empty = h.svc.create_session(user_id=ME)
    assert h.svc.get_transcript(empty.id, user_id=ME) == {"session_id": empty.id, "turns": [], "has_more": False}
    h.close()


def test_a_long_turn_is_cut_short_and_says_where_to_continue(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(sessions_module, "TRANSCRIPT_EVENTS_PER_TURN", 2)
    h = hub(backend, tmp_path, monkeypatch, [[say("a long answer")]])
    s, _ = talk(h, 1)
    t = h.svc.get_transcript(s.id, user_id=ME)["turns"][0]
    assert t["truncated"] is True and [e["seq"] for e in t["events"]] == [1, 2] and t["next_seq"] == 2
    rest = h.svc.get_events(t["turn"]["id"], t["next_seq"], user_id=ME)  # the client continues from next_seq
    assert kinds(rest) == ["assistant_message", "turn_done"]
    h.close()


# ── over HTTP ────────────────────────────────────────────────────────────

def test_the_new_methods_work_over_the_contract_routes(tmp_path, monkeypatch):
    h, c = make(tmp_path, monkeypatch, [[say("one")], [say("two")]])
    cfg = out(contract(c, "get_config", {"user_id": ME}))
    assert cfg["user"] == ME and cfg["default_model"] == "m1"
    s = out(contract(c, "create_session", {"user_id": ME, "title": "Chat"}))
    out(contract(c, "send_message", {"user_id": ME, "session_id": s["id"], "text": "hi"}))
    renamed = out(contract(c, "update_session", {"user_id": ME, "session_id": s["id"], "title": "Renamed"}))
    assert renamed["title"] == "Renamed"
    tr = out(contract(c, "get_transcript", {"user_id": ME, "session_id": s["id"]}))
    assert [x["turn"]["prompt"] for x in tr["turns"]] == ["hi"] and tr["has_more"] is False
    out(contract(c, "update_session", {"user_id": ME, "session_id": s["id"], "archived": True}))
    assert out(contract(c, "list_sessions", {"user_id": ME})) == []
    assert [x["id"] for x in out(contract(c, "list_sessions", {"user_id": ME, "include_archived": True}))] == [s["id"]]


def test_bad_input_to_the_new_methods_is_a_clean_400_and_other_users_are_refused(tmp_path, monkeypatch):
    h, c = make(tmp_path, monkeypatch, [[say("x")]])
    s = out(contract(c, "create_session", {"user_id": ME}))
    assert contract(c, "update_session", {"user_id": ME, "session_id": s["id"]}).status_code == 400
    assert contract(c, "update_session", {"user_id": ME, "session_id": s["id"], "title": 5}).status_code == 400
    assert contract(c, "get_transcript", {"user_id": ME, "session_id": s["id"], "limit": 51}).status_code == 400
    assert contract(c, "list_sessions", {"user_id": ME, "include_archived": "yes"}).status_code == 400
    assert contract(c, "update_session", {"user_id": "stranger@example.com", "session_id": s["id"], "title": "x"}).status_code == 403
    assert contract(c, "update_session", {"user_id": ME, "session_id": "ses_none", "title": "x"}).status_code == 404


def test_the_rest_mirror_has_the_new_routes(tmp_path, monkeypatch):
    h, c = make(tmp_path, monkeypatch, [[say("x")]])
    s = c.post("/v1/sessions", json={"title": "t"}, headers=AUTH).json()
    assert c.get("/v1/config", headers=AUTH).json()["default_model"] == "m1"
    r = c.patch(f"/v1/sessions/{s['id']}", json={"title": "u"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["title"] == "u"
    assert c.patch(f"/v1/sessions/{s['id']}", json={}, headers=AUTH).status_code == 400
    assert c.get(f"/v1/sessions/{s['id']}/transcript", headers=AUTH).json()["turns"] == []
    assert c.get("/v1/config").status_code == 401
