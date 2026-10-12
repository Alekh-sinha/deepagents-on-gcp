"""The web backend (Cloud Run): sign-in, CSRF, relay to the engine, user isolation, live streaming. The engine is the REAL engine app running
in the same process (HttpRuntimeClient over a TestClient), so these are end-to-end through both layers."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from pinboard.engine.api import create_app as create_engine_app
from pinboard.core.policy import parse_policy
from pinboard.web.app import StreamLimiter, create_app, sse
from pinboard.web.auth import AuthError
from pinboard.web.runtime_client import HttpRuntimeClient, RuntimeCallError

from helpers import call, say
from test_policy import BASE
from turn_harness import Hub

TOKEN = "t" * 32
ALICE, BOB = "me@example.com", "bob@example.com"
H_ALICE = {"x-test-user": ALICE, "x-requested-with": "pinboard"}
H_BOB = {"x-test-user": BOB, "x-requested-with": "pinboard"}


class HeaderAuth:
    """Test double for the sign-in check (the real ones are tested in test_web_auth.py)."""

    def email(self, headers):
        if not headers.get("x-test-user"):
            raise AuthError("not signed in")
        return headers["x-test-user"].lower()


def two_user_policy():
    d = copy.deepcopy(BASE)
    d["allowed_users"] = [ALICE, BOB]
    return parse_policy(d)


class Stack:
    def __init__(self, tmp_path, monkeypatch, scripts, **kw):
        self.hub = Hub("memory", tmp_path, monkeypatch, scripts, policy=kw.pop("policy", two_user_policy()), **kw)
        self.hub.new_process()
        self.engine = TestClient(create_engine_app(self.hub.svc, TOKEN))
        self.runtime = HttpRuntimeClient(self.engine, TOKEN)
        self.app = create_app(self.runtime, HeaderAuth())
        self.web = TestClient(self.app)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    return lambda scripts, **kw: Stack(tmp_path, monkeypatch, scripts, **kw)


def events_of_sse(text):
    """Parse an SSE body into [(event, id, data)]; comments and retry lines are skipped."""
    out = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "data" in fields:
            out.append((fields.get("event", "message"), fields.get("id"), json.loads(fields["data"])))
    return out


# ── sign-in and CSRF ─────────────────────────────────────────────────────

def test_nothing_works_without_signing_in_except_the_health_check(stack):
    s = stack([[say("x")]])
    assert s.web.get("/healthz").json() == {"status": "ok"}
    for path in ("/api/me", "/api/models", "/api/sessions", "/api/approvals", "/api/artifacts"):
        assert s.web.get(path).status_code == 401, path
    assert s.web.post("/api/sessions", json={}).status_code == 401


def test_changes_need_the_csrf_header_and_a_same_origin_request(stack):
    s = stack([[say("x")]])
    assert s.web.post("/api/sessions", json={}, headers={"x-test-user": ALICE}).status_code == 403  # a plain cross-site form post has no such header
    foreign = {**H_ALICE, "origin": "https://evil.example"}
    assert s.web.post("/api/sessions", json={}, headers=foreign).status_code == 403
    same = {**H_ALICE, "origin": "http://testserver"}
    assert s.web.post("/api/sessions", json={}, headers=same).status_code == 200
    assert s.web.get("/api/sessions", headers={"x-test-user": ALICE}).status_code == 200  # reading needs no CSRF header


def test_responses_carry_the_security_headers(stack):
    s = stack([[say("x")]])
    r = s.web.get("/api/me", headers=H_ALICE)
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


# ── a conversation through both layers ───────────────────────────────────

def test_a_conversation_end_to_end(stack):
    s = stack([[say("Hello Alice")], [say("Second")]])
    me = s.web.get("/api/me", headers=H_ALICE).json()
    assert me["user"] == ALICE and me["default_model"] == "m1"
    assert [m["id"] for m in s.web.get("/api/models", headers=H_ALICE).json()["models"]] == ["m1", "m2"]

    sess = s.web.post("/api/sessions", json={"title": "Chat"}, headers=H_ALICE).json()
    turn = s.web.post(f"/api/sessions/{sess['id']}/messages", json={"text": "hi"}, headers=H_ALICE).json()
    assert turn["status"] == "completed" and turn["output"] == "Hello Alice"
    assert s.web.get(f"/api/turns/{turn['id']}", headers=H_ALICE).json()["status"] == "completed"

    ev = s.web.get(f"/api/turns/{turn['id']}/events?after_seq=1", headers=H_ALICE).json()["events"]
    assert [e["type"] for e in ev] == ["turn_started", "assistant_message", "turn_done"]
    tr = s.web.get(f"/api/sessions/{sess['id']}/transcript", headers=H_ALICE).json()
    assert [t["turn"]["prompt"] for t in tr["turns"]] == ["hi"] and tr["has_more"] is False

    patched = s.web.patch(f"/api/sessions/{sess['id']}", json={"title": "Renamed", "model": "m2"}, headers=H_ALICE).json()
    assert patched["title"] == "Renamed" and patched["model"] == "m2"
    cleared = s.web.patch(f"/api/sessions/{sess['id']}", json={"model": None}, headers=H_ALICE).json()
    assert cleared["model"] is None and cleared["title"] == "Renamed"  # only the sent fields change; null clears the model
    assert s.web.patch(f"/api/sessions/{sess['id']}", json={}, headers=H_ALICE).status_code == 400

    s.web.patch(f"/api/sessions/{sess['id']}", json={"archived": True}, headers=H_ALICE)
    assert s.web.get("/api/sessions", headers=H_ALICE).json() == []
    assert [x["id"] for x in s.web.get("/api/sessions?archived=true", headers=H_ALICE).json()] == [sess["id"]]
    assert s.web.get(f"/api/turns/{turn['id']}/audit", headers=H_ALICE).json()["events"][0]["event"] == "run.started"
    s.hub.close()


def test_a_message_can_pick_a_model_and_a_bad_one_is_a_400_with_the_reason(stack):
    s = stack([[say("x")]])
    sess = s.web.post("/api/sessions", json={}, headers=H_ALICE).json()
    r = s.web.post(f"/api/sessions/{sess['id']}/messages", json={"text": "hi", "model": "gpt-9"}, headers=H_ALICE)
    assert r.status_code == 400 and "not available" in r.json()["detail"]


def test_an_approval_is_listed_decided_and_the_turn_finishes(stack):
    s = stack([[call("execute", {"command": "echo hi"}), say("x")], [say("finished")]], approval_mode="execute")
    sess = s.web.post("/api/sessions", json={}, headers=H_ALICE).json()
    turn = s.web.post(f"/api/sessions/{sess['id']}/messages", json={"text": "run it"}, headers=H_ALICE).json()
    assert turn["status"] == "awaiting_approval"
    pending = s.web.get("/api/approvals?status=pending", headers=H_ALICE).json()
    assert len(pending) == 1 and pending[0]["previews"][0]["kind"] == "shell"
    done = s.web.post(f"/api/approvals/{pending[0]['id']}/decision", json={"decision": "approve"}, headers=H_ALICE).json()
    assert done["status"] == "completed"
    assert s.web.post(f"/api/approvals/{pending[0]['id']}/decision", json={"decision": "approve"}, headers=H_ALICE).status_code in (404, 409)
    assert s.web.post(f"/api/approvals/{pending[0]['id']}/decision", json={"decision": "maybe"}, headers=H_ALICE).status_code == 422


def test_saved_files_can_be_listed_and_downloaded(stack):
    s = stack([[None]])
    path = f"{s.hub.sbdir('sb-1')}/out/report.md"
    s.hub.scripts[0] = [call("write_file", {"file_path": path, "content": "# Report"}, "c1"), say("saved")]
    sess = s.web.post("/api/sessions", json={}, headers=H_ALICE).json()
    s.web.post(f"/api/sessions/{sess['id']}/messages", json={"text": "make it"}, headers=H_ALICE)
    arts = s.web.get(f"/api/artifacts?session_id={sess['id']}", headers=H_ALICE).json()
    assert [a["path"] for a in arts] == ["report.md"]
    one = s.web.get(f"/api/artifacts/{arts[0]['id']}", headers=H_ALICE).json()
    assert one["name"] == "report.md"
    got = s.web.get(f"/api/artifacts/{arts[0]['id']}/content", headers=H_ALICE)
    assert got.content == b"# Report" and got.headers["content-disposition"] == 'attachment; filename="report.md"'
    assert got.headers["x-content-type-options"] == "nosniff"
    assert s.web.get(f"/api/artifacts/{arts[0]['id']}/url", headers=H_ALICE).json() == {"url": None}  # local store: no signed links


def test_typing_triggers_the_warm_up_and_only_for_your_own_conversation(stack):
    import dataclasses

    from pinboard.core.policy import WarmSandbox

    s = stack([[say("x")]], policy=dataclasses.replace(two_user_policy(), warm_sandbox=WarmSandbox("on_activity")))
    sess = s.web.post("/api/sessions", json={}, headers=H_ALICE).json()
    assert s.web.post(f"/api/sessions/{sess['id']}/warm", headers=H_ALICE).json() == {"status": "warming"}
    assert s.web.post(f"/api/sessions/{sess['id']}/warm", headers=H_ALICE).json() == {"status": "ready"}
    assert s.web.post(f"/api/sessions/{sess['id']}/warm", headers=H_BOB).status_code == 404
    assert s.web.post(f"/api/sessions/{sess['id']}/warm", headers={"x-test-user": ALICE}).status_code == 403  # needs the CSRF header too
    assert s.web.get("/api/me", headers=H_ALICE).json()["warm_sandbox"] == {"mode": "on_activity"}


def test_warm_up_is_off_unless_the_policy_turns_it_on(stack):
    s = stack([[say("x")]])
    sess = s.web.post("/api/sessions", json={}, headers=H_ALICE).json()
    assert s.web.post(f"/api/sessions/{sess['id']}/warm", headers=H_ALICE).json() == {"status": "off"}


# ── nobody sees or changes anybody else's things ─────────────────────────

def test_users_are_isolated_and_the_browser_cannot_choose_the_user(stack):
    s = stack([[say("alice's answer")], [say("bob's answer")]])
    mine = s.web.post("/api/sessions", json={"title": "private"}, headers=H_ALICE).json()
    turn = s.web.post(f"/api/sessions/{mine['id']}/messages", json={"text": "secret"}, headers=H_ALICE).json()

    assert s.web.get("/api/sessions", headers=H_BOB).json() == []
    for path in (f"/api/sessions/{mine['id']}", f"/api/sessions/{mine['id']}/transcript", f"/api/turns/{turn['id']}", f"/api/turns/{turn['id']}/events",
                 f"/api/turns/{turn['id']}/stream", f"/api/turns/{turn['id']}/audit"):
        assert s.web.get(path, headers=H_BOB).status_code == 404, path
    assert s.web.patch(f"/api/sessions/{mine['id']}", json={"title": "mine now"}, headers=H_BOB).status_code == 404
    assert s.web.post(f"/api/sessions/{mine['id']}/messages", json={"text": "hi"}, headers=H_BOB).status_code == 404
    assert s.hub.svc.store.get_session(mine["id"]).title == "private"

    # Bob tries to pose as Alice in the body and the query string: both are ignored, the verified identity wins
    created = s.web.post(f"/api/sessions?user_id={ALICE}", json={"title": "x", "user_id": ALICE}, headers=H_BOB).json()
    assert s.hub.svc.store.get_session(created["id"]).user_id == BOB
    assert s.web.get(f"/api/sessions?user_id={ALICE}", headers=H_BOB).json()[0]["id"] == created["id"]


def test_someone_not_on_the_allowed_list_gets_403_from_the_engine(stack):
    s = stack([[say("x")]])
    r = s.web.get("/api/sessions", headers={"x-test-user": "stranger@example.com", "x-requested-with": "pinboard"})
    assert r.status_code == 403 and "not allowed" in r.json()["detail"]


def test_ids_and_inputs_are_validated_before_anything_is_sent(stack):
    s = stack([[say("x")]])
    assert s.web.get("/api/sessions/bad%20id%21", headers=H_ALICE).status_code == 422
    assert s.web.get("/api/sessions/" + "a" * 200, headers=H_ALICE).status_code == 422
    sess = s.web.post("/api/sessions", json={}, headers=H_ALICE).json()
    assert s.web.post(f"/api/sessions/{sess['id']}/messages", json={"text": ""}, headers=H_ALICE).status_code == 422
    assert s.web.post(f"/api/sessions/{sess['id']}/messages", json={"text": "x" * 32_001}, headers=H_ALICE).status_code == 422
    assert s.web.post("/api/sessions", json={"title": "t" * 121}, headers=H_ALICE).status_code == 422
    assert s.web.get("/api/sessions?limit=0", headers=H_ALICE).status_code == 422
    assert s.web.get("/api/approvals?status=whatever", headers=H_ALICE).status_code == 422


# ── live output ──────────────────────────────────────────────────────────

def test_the_stream_of_a_finished_turn_replays_it_and_resumes_from_last_event_id(stack):
    s = stack([[say("Hello there")]])
    sess = s.web.post("/api/sessions", json={}, headers=H_ALICE).json()
    turn = s.web.post(f"/api/sessions/{sess['id']}/messages", json={"text": "hi"}, headers=H_ALICE).json()
    r = s.web.get(f"/api/turns/{turn['id']}/stream", headers=H_ALICE)
    assert r.headers["content-type"].startswith("text/event-stream") and r.headers["cache-control"].startswith("no-cache")
    got = events_of_sse(r.text)
    assert [e[2]["type"] for e in got if e[0] == "message"] == ["user_message", "turn_started", "assistant_message", "turn_done"]
    assert [e[1] for e in got if e[0] == "message"] == ["1", "2", "3", "4"] and got[-1][0] == "end" and got[-1][2]["after_seq"] == 4
    rest = events_of_sse(s.web.get(f"/api/turns/{turn['id']}/stream", headers={**H_ALICE, "last-event-id": "3"}).text)
    assert [e[2]["type"] for e in rest if e[0] == "message"] == ["turn_done"]  # the browser's automatic reconnect picks up exactly where it was
    assert s.app.state.limiter._n[ALICE] == 0  # the slot was given back


class FakeEngine:
    """A scripted engine: each stream() call replays the next list; records what it was asked."""

    def __init__(self, streams, turn_ok=True):
        self.streams, self.calls, self.turn_ok = list(streams), [], turn_ok

    def call(self, method, inp):
        self.calls.append((method, inp))
        if method == "get_turn" and not self.turn_ok:
            raise RuntimeCallError(404, "turn not found")
        return {"id": inp.get("turn_id")}

    def stream(self, method, inp):
        self.calls.append((method, inp))
        items = self.streams.pop(0)
        for it in items:
            if isinstance(it, Exception):
                raise it
            yield it


def fake_stack(streams, **kw):
    engine = FakeEngine(streams, kw.pop("turn_ok", True))
    app = create_app(engine, HeaderAuth(), **kw)
    return engine, app, TestClient(app)


def ev(seq, kind="token", **payload):
    return {"seq": seq, "type": kind, "payload": payload}


def test_the_relay_reconnects_to_the_engine_by_itself_and_hides_it_from_the_browser():
    engine, app, web = fake_stack([
        [ev(1), ev(2), {"type": "heartbeat", "after_seq": 2}, {"type": "reconnect", "after_seq": 2}],
        [ev(3), ev(4, "turn_done")],
    ])
    r = web.get("/api/turns/t1/stream", headers=H_ALICE)
    assert ": keep-alive" in r.text and "reconnect" not in r.text.replace("retry", "")
    msgs = events_of_sse(r.text)
    assert [e[1] for e in msgs if e[0] == "message"] == ["1", "2", "3", "4"] and msgs[-1][0] == "end"
    asks = [c[1]["after_seq"] for c in engine.calls if c[0] == "stream_turn"]
    assert asks == [0, 2] and all(c[1]["user_id"] == ALICE for c in engine.calls)  # the second call starts after the last event seen


def test_a_stream_that_runs_too_long_ends_politely_so_the_browser_reconnects():
    clock = iter([0.0, 5000.0, 10000.0])
    engine, app, web = fake_stack([[ev(1), {"type": "reconnect", "after_seq": 1}], [ev(2)]], monotonic=lambda: next(clock))
    msgs = events_of_sse(web.get("/api/turns/t1/stream", headers=H_ALICE).text)
    assert msgs[-1] == ("end", None, {"after_seq": 1, "reason": "time"}) and len(engine.streams) == 1  # the second engine stream was never opened


def test_an_error_in_the_middle_of_a_stream_is_reported_in_band_and_the_slot_is_released():
    engine, app, web = fake_stack([[ev(1), RuntimeCallError(502, "the assistant stopped unexpectedly")]])
    msgs = events_of_sse(web.get("/api/turns/t1/stream", headers=H_ALICE).text)
    assert msgs[-1][0] == "error" and msgs[-1][2]["status"] == 502 and msgs[-1][2]["after_seq"] == 1
    assert app.state.limiter._n[ALICE] == 0


def test_streaming_someone_elses_turn_fails_before_the_stream_starts():
    engine, app, web = fake_stack([], turn_ok=False)
    r = web.get("/api/turns/t1/stream", headers=H_ALICE)
    assert r.status_code == 404 and r.json() == {"detail": "turn not found"} and app.state.limiter._n[ALICE] == 0


def test_too_many_open_streams_is_a_429_with_retry_after():
    engine, app, web = fake_stack([[ev(1)]], max_streams_per_user=1)
    hold = app.state.limiter.reserve(ALICE)  # a stream that is still open
    r = web.get("/api/turns/t1/stream", headers=H_ALICE)
    assert r.status_code == 429 and r.headers["retry-after"] == "5"
    hold()
    assert web.get("/api/turns/t1/stream", headers=H_ALICE).status_code == 200  # a slot is free again
    other = web.get("/api/turns/t1/stream", headers={"x-test-user": BOB, "x-requested-with": "pinboard"})
    assert other.status_code == 200  # the limit is per user


def test_the_limiter_counts_and_releases_once():
    lim = StreamLimiter(2)
    a, b = lim.reserve("u"), lim.reserve("u")
    with pytest.raises(RuntimeCallError):
        lim.reserve("u")
    a(); a()  # releasing twice must not free two slots
    c = lim.reserve("u")
    with pytest.raises(RuntimeCallError):
        lim.reserve("u")
    b(); c()


def test_sse_formatting():
    assert sse({"a": 1}, id=5) == 'id: 5\ndata: {"a":1}\n\n'
    assert sse({"x": 1}, event="end") == 'event: end\ndata: {"x":1}\n\n'
    assert sse(comment="keep-alive") == ": keep-alive\n\n"


# ── cancel / resume through the web layer ────────────────────────────────

def test_a_turn_waiting_for_approval_can_be_cancelled_and_the_conversation_goes_on(stack):
    s = stack([[call("execute", {"command": "echo hi"}), say("x")], [say("later answer")]], approval_mode="execute")
    sess = s.web.post("/api/sessions", json={}, headers=H_ALICE).json()
    turn = s.web.post(f"/api/sessions/{sess['id']}/messages", json={"text": "run it"}, headers=H_ALICE).json()
    assert turn["status"] == "awaiting_approval"
    cancelled = s.web.post(f"/api/turns/{turn['id']}/cancel", headers=H_ALICE)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert s.web.get("/api/approvals?status=pending", headers=H_ALICE).json() == []  # nothing is left waiting
    again = s.web.post(f"/api/sessions/{sess['id']}/messages", json={"text": "next"}, headers=H_ALICE).json()
    assert again["status"] == "completed" and again["output"] == "later answer"


def test_cancelling_or_resuming_a_finished_turn_is_a_409_with_the_reason(stack):
    s = stack([[say("done")]])
    sess = s.web.post("/api/sessions", json={}, headers=H_ALICE).json()
    turn = s.web.post(f"/api/sessions/{sess['id']}/messages", json={"text": "hi"}, headers=H_ALICE).json()
    assert s.web.post(f"/api/turns/{turn['id']}/cancel", headers=H_ALICE).status_code == 409
    r = s.web.post(f"/api/turns/{turn['id']}/resume", headers=H_ALICE)
    assert r.status_code == 409 and "interrupted" in r.json()["detail"]
    assert s.web.post(f"/api/turns/{turn['id']}/cancel", headers=H_BOB).status_code == 404  # and not yours to touch


# ── every route reaches the right engine method with the verified user ───

ROUTES = [
    ("GET", "/api/me", None, "get_config", {}),
    ("GET", "/api/models", None, "list_models", {}),
    ("GET", "/api/sessions?archived=true&limit=7", None, "list_sessions", {"include_archived": True, "limit": 7}),
    ("POST", "/api/sessions", {"title": "t", "model": "m1"}, "create_session", {"title": "t", "model": "m1"}),
    ("GET", "/api/sessions/s1", None, "get_session", {"session_id": "s1"}),
    ("PATCH", "/api/sessions/s1", {"title": "n", "archived": True}, "update_session", {"session_id": "s1", "title": "n", "archived": True}),
    ("GET", "/api/sessions/s1/transcript?limit=5&before_turn_id=t9", None, "get_transcript", {"session_id": "s1", "limit": 5, "before_turn_id": "t9"}),
    ("POST", "/api/sessions/s1/warm", None, "warm_sandbox", {"session_id": "s1"}),
    ("POST", "/api/sessions/s1/messages", {"text": "hi", "model": "m2"}, "send_message", {"session_id": "s1", "text": "hi", "model": "m2"}),
    ("GET", "/api/turns/t1", None, "get_turn", {"turn_id": "t1"}),
    ("GET", "/api/turns/t1/events?after_seq=3&limit=10", None, "get_events", {"turn_id": "t1", "after_seq": 3, "limit": 10}),
    ("POST", "/api/turns/t1/cancel", None, "cancel_turn", {"turn_id": "t1"}),
    ("POST", "/api/turns/t1/resume", None, "resume_turn", {"turn_id": "t1"}),
    ("GET", "/api/turns/t1/audit", None, "get_audit", {"run_id": "t1"}),
    ("GET", "/api/approvals?status=pending&session_id=s1", None, "list_approvals", {"status": "pending", "session_id": "s1"}),
    ("POST", "/api/approvals/a1/decision", {"decision": "reject", "message": "no"}, "decide", {"approval_id": "a1", "decision": "reject", "message": "no"}),
    ("GET", "/api/artifacts?session_id=s1&limit=9", None, "list_artifacts", {"session_id": "s1", "limit": 9}),
    ("GET", "/api/artifacts/f1", None, "get_artifact", {"artifact_id": "f1"}),
    ("GET", "/api/artifacts/f1/url?seconds=60", None, "get_artifact_url", {"artifact_id": "f1", "seconds": 60}),
    # automatic approval travels with sessions and messages; an explicit null / false is passed through (it means "off")
    ("POST", "/api/sessions", {"auto_approve": True}, "create_session", {"auto_approve": True}),
    ("PATCH", "/api/sessions/s1", {"auto_approve": {"kinds": ["shell"], "max_per_turn": 3}}, "update_session", {"session_id": "s1", "auto_approve": {"kinds": ["shell"], "max_per_turn": 3}}),
    ("PATCH", "/api/sessions/s1", {"auto_approve": None}, "update_session", {"session_id": "s1", "auto_approve": None}),
    ("POST", "/api/sessions/s1/messages", {"text": "hi", "auto_approve": False}, "send_message", {"session_id": "s1", "text": "hi", "auto_approve": False}),
    # bring-your-own MCP servers
    ("GET", "/api/mcp/servers", None, "list_mcp_servers", {}),
    ("POST", "/api/mcp/import", {"config": {"mcpServers": {"a": {"url": "https://x.example.com/mcp"}}}}, "import_mcp_config", {"config": {"mcpServers": {"a": {"url": "https://x.example.com/mcp"}}}}),
    ("PUT", "/api/mcp/servers/calc", {"url": "https://x.example.com/mcp", "headers": {"Authorization": "Bearer t"}}, "put_mcp_server",
     {"name": "calc", "url": "https://x.example.com/mcp", "headers": {"Authorization": "Bearer t"}}),
    ("PUT", "/api/mcp/servers/calc", {"url": "https://x.example.com/mcp", "requires_approval": False, "no_approval_tools": ["add"]}, "put_mcp_server",
     {"name": "calc", "url": "https://x.example.com/mcp", "requires_approval": False, "no_approval_tools": ["add"]}),
    ("PATCH", "/api/mcp/servers/calc", {"enabled": False}, "update_mcp_server", {"name": "calc", "enabled": False}),
    ("POST", "/api/mcp/servers/calc/test", None, "test_mcp_server", {"name": "calc"}),
    ("DELETE", "/api/mcp/servers/calc", None, "delete_mcp_server", {"name": "calc"}),
    # scheduled tasks
    ("GET", "/api/schedules", None, "list_schedules", {}),
    ("POST", "/api/schedules", {"title": "Brief", "instructions": "Say hi.", "cron": "0 9 * * 1-5", "timezone": "Europe/Berlin"}, "create_schedule",
     {"title": "Brief", "instructions": "Say hi.", "cron": "0 9 * * 1-5", "timezone": "Europe/Berlin"}),
    ("PATCH", "/api/schedules/sc_1", {"enabled": False, "model": None}, "update_schedule", {"schedule_id": "sc_1", "enabled": False, "model": None}),
    ("DELETE", "/api/schedules/sc_1", None, "delete_schedule", {"schedule_id": "sc_1"}),
    ("POST", "/api/schedules/sc_1/run", None, "run_schedule_now", {"schedule_id": "sc_1"}),
    ("GET", "/api/schedules/sc_1/runs?limit=5", None, "list_schedule_runs", {"schedule_id": "sc_1", "limit": 5}),
]


@pytest.mark.parametrize("verb, path, body, method, expected", ROUTES)
def test_each_route_relays_to_the_right_engine_method_with_the_verified_user(verb, path, body, method, expected):
    engine, app, web = fake_stack([])
    r = web.request(verb, path, json=body, headers={**H_ALICE, "x-test-user": ALICE})
    assert r.status_code == 200, r.text
    called, sent = engine.calls[-1]
    assert called == method and sent["user_id"] == ALICE
    assert {k: sent.get(k) for k in expected} == expected  # exactly what the browser asked for, nothing invented
    assert set(sent) <= set(expected) | {"user_id"}  # and nothing extra


def test_the_file_download_route_decodes_the_engines_base64(tmp_path):
    import base64

    class Files:
        calls = []

        def call(self, method, inp):
            self.calls.append((method, inp))
            return {"artifact": {"name": "my report.md", "content_type": "text/markdown"}, "content_base64": base64.b64encode(b"# hi").decode()}

    app = create_app(Files(), HeaderAuth())
    r = TestClient(app).get("/api/artifacts/f1/content", headers=H_ALICE)
    assert r.content == b"# hi" and r.headers["content-disposition"] == 'attachment; filename="my report.md"' and r.headers["content-type"].startswith("text/markdown")


def test_the_diagnostics_page_needs_a_sign_in_and_calls_only_our_own_api():
    engine, app, web = fake_stack([])
    assert web.get("/_diag").status_code == 401
    page = web.get("/_diag", headers={"x-test-user": ALICE})
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    html = page.text
    assert "X-Requested-With" in html and "/api/me" in html and "EventSource" in html
    assert "http://" not in html.replace("http://www.w3.org", "") and "https://" not in html  # nothing is loaded from anywhere else


# ── the new routes are guarded like every other ──────────────────────────

@pytest.mark.parametrize("verb, path, body", [
    ("PUT", "/api/mcp/servers/calc", {"url": "https://x.example.com/mcp"}), ("DELETE", "/api/mcp/servers/calc", None), ("PATCH", "/api/mcp/servers/calc", {"enabled": True}),
    ("POST", "/api/mcp/import", {"config": "{}"}), ("POST", "/api/mcp/servers/calc/test", None),
    ("POST", "/api/schedules", {"title": "t", "instructions": "i", "cron": "0 9 * * *"}), ("DELETE", "/api/schedules/sc_1", None), ("POST", "/api/schedules/sc_1/run", None),
])
def test_state_changing_mcp_and_schedule_routes_need_a_sign_in_and_the_csrf_header(verb, path, body):
    engine, app, web = fake_stack([])
    assert web.request(verb, path, json=body).status_code == 401
    assert web.request(verb, path, json=body, headers={"x-test-user": ALICE}).status_code == 403          # signed in but no CSRF header
    assert web.request(verb, path, json=body, headers={**H_ALICE, "origin": "https://evil.example.com"}).status_code == 403
    assert engine.calls == []


@pytest.mark.parametrize("verb, path, body", [
    ("PUT", "/api/mcp/servers/Bad Name", {"url": "https://x.example.com/mcp"}), ("PUT", "/api/mcp/servers/calc", {"url": "x" * 2001}),
    ("PUT", "/api/mcp/servers/calc", {"url": "https://x.example.com", "transport": "grpc"}), ("PUT", "/api/mcp/servers/calc", {"url": "https://x.example.com", "headers": ["a"]}),
    ("PATCH", "/api/mcp/servers/calc", {}), ("POST", "/api/mcp/import", {}),
    ("POST", "/api/schedules", {"title": "t", "instructions": "i"}), ("POST", "/api/schedules", {"title": "", "instructions": "i", "cron": "0 9 * * *"}),
    ("POST", "/api/schedules", {"title": "t", "instructions": "x" * 20_001, "cron": "0 9 * * *"}), ("PATCH", "/api/schedules/sc_1", {}),
    ("GET", "/api/schedules/sc_1/runs?limit=0", None), ("POST", "/api/sessions/s1/messages", {"text": ""}),
])
def test_bad_input_is_refused_before_it_reaches_the_engine(verb, path, body):
    engine, app, web = fake_stack([])
    r = web.request(verb, path, json=body, headers=H_ALICE)
    assert r.status_code in (400, 422) and engine.calls == []


def test_one_user_cannot_see_or_touch_another_users_mcp_servers_or_schedules(stack):
    from pinboard.mcp import generate_key

    s = stack([[say("x")]])
    import os

    os.environ["MCP_ENCRYPTION_KEY"] = generate_key()
    try:
        s.hub.svc.policy.mcp  # noqa: B018  (the policy of this stack has MCP off: that is itself part of the contract)
        r = s.web.put("/api/mcp/servers/calc", json={"url": "https://x.example.com/mcp"}, headers=H_ALICE)
        assert r.status_code == 400 and "switched off" in r.json()["detail"]
        r = s.web.post("/api/schedules", json={"title": "t", "instructions": "i", "cron": "0 9 * * *"}, headers=H_ALICE)
        assert r.status_code == 400 and "switched off" in r.json()["detail"]
        assert s.web.get("/api/mcp/servers", headers=H_BOB).json() == {"servers": []} and s.web.get("/api/schedules", headers=H_BOB).json() == {"schedules": []}
    finally:
        os.environ.pop("MCP_ENCRYPTION_KEY", None)


# ── the Google sign-in routes ────────────────────────────────────────────

class GoogleStack:
    def __init__(self, allowed=(ALICE,), claims=None):
        from pinboard.web.auth import GoogleAuthenticator

        outer = self
        self.calls = []

        class Engine:
            def call(self, method, inp):
                outer.calls.append((method, inp))
                if method == "get_config" and inp["user_id"] not in allowed:
                    raise RuntimeCallError(403, "this account is not allowed to use the assistant")
                return {"user": inp["user_id"]}

            def stream(self, *a, **k):
                return iter(())

        def verify(token, client_id):
            if token != "g" * 30:
                raise ValueError("bad token")
            return claims or {"iss": "https://accounts.google.com", "email": ALICE, "email_verified": True}

        self.auth = GoogleAuthenticator("client-id.apps.googleusercontent.com", "k" * 40, verify=verify)
        self.web = TestClient(create_app(Engine(), self.auth))


def test_signing_in_with_google_sets_a_cookie_that_then_signs_every_request():
    g = GoogleStack()
    assert g.web.get("/api/auth/config").json() == {"mode": "google", "client_id": "client-id.apps.googleusercontent.com"}   # public: the page needs it to show the button
    assert g.web.get("/api/me").status_code == 401
    r = g.web.post("/api/auth/google", json={"credential": "g" * 30}, headers={"x-requested-with": "pinboard"})
    assert r.status_code == 200 and r.json() == {"email": ALICE}
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie and "Secure" not in cookie    # plain http on localhost
    assert g.web.get("/api/me").json() == {"user": ALICE}                                                             # the test client keeps the cookie
    assert g.calls[-1] == ("get_config", {"user_id": ALICE})


def test_the_cookie_is_marked_secure_behind_https():
    g = GoogleStack()
    r = g.web.post("/api/auth/google", json={"credential": "g" * 30}, headers={"x-requested-with": "pinboard", "x-forwarded-proto": "https"})
    assert "Secure" in r.headers["set-cookie"]


def test_an_account_the_engine_does_not_allow_gets_no_cookie():
    g = GoogleStack(allowed=(BOB,))
    r = g.web.post("/api/auth/google", json={"credential": "g" * 30}, headers={"x-requested-with": "pinboard"})
    assert r.status_code == 403 and "not allowed" in r.json()["detail"] and "set-cookie" not in r.headers
    assert g.web.get("/api/me").status_code == 401


def test_a_bad_google_token_and_a_missing_csrf_header_are_refused():
    g = GoogleStack()
    assert g.web.post("/api/auth/google", json={"credential": "x" * 30}, headers={"x-requested-with": "pinboard"}).status_code == 401
    assert g.web.post("/api/auth/google", json={"credential": "g" * 30}).status_code == 403
    assert g.web.post("/api/auth/google", json={"credential": "g" * 30}, headers={"x-requested-with": "pinboard", "origin": "https://evil.example.com"}).status_code == 403
    assert g.web.post("/api/auth/google", json={"credential": "short"}, headers={"x-requested-with": "pinboard"}).status_code == 422


def test_signing_out_clears_the_cookie():
    g = GoogleStack()
    g.web.post("/api/auth/google", json={"credential": "g" * 30}, headers={"x-requested-with": "pinboard"})
    assert g.web.get("/api/me").status_code == 200
    out = g.web.post("/api/auth/logout", headers={"x-requested-with": "pinboard"})
    assert out.status_code == 200 and "privai_session=" in out.headers["set-cookie"] and "Max-Age=0" in out.headers["set-cookie"]
    assert g.web.get("/api/me").status_code == 401
    assert g.web.post("/api/auth/logout").status_code == 403


def test_the_sign_in_routes_exist_only_for_google_mode():
    engine, app, web = fake_stack([])                       # HeaderAuth stands in for iap / dev
    assert web.post("/api/auth/google", json={"credential": "g" * 30}, headers=H_ALICE).status_code in (404, 405)
    assert web.get("/api/auth/config").json() == {"mode": "iap"}


# ── serving the built React app ──────────────────────────────────────────

def test_the_built_app_is_served_with_deep_links_but_real_404s_for_missing_files_and_api_paths(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    engine, app, web = fake_stack([], static_dir=tmp_path)
    assert web.get("/").text == "<html>app</html>"
    assert web.get("/chat/s_123").text == "<html>app</html>"          # a page of the app: a reload or a shared link must work
    assert web.get("/schedules").text == "<html>app</html>"
    assert web.get("/assets/app.js").text == "console.log(1)"
    assert web.get("/assets/missing.js").status_code == 404          # a file that is not there is a 404, not the app
    assert web.get("/api/nope").status_code == 404
    assert web.get("/api/me").status_code == 401                      # the API still comes first
    assert web.get("/healthz").json() == {"status": "ok"}


def test_the_app_is_served_with_a_policy_that_blocks_remote_images_and_framing_but_the_api_and_diagnostics_are_left_alone(tmp_path):
    (tmp_path / "index.html").write_text("<html>app</html>")
    engine, app, web = fake_stack([], static_dir=tmp_path)
    csp = web.get("/").headers["content-security-policy"]
    assert "img-src 'self' data: https://*.googleusercontent.com" in csp and "frame-ancestors 'none'" in csp and "script-src 'self' https://accounts.google.com" in csp
    assert "http:" not in csp and "*" not in csp.replace("https://*.googleusercontent.com", "")      # nothing is allowed from anywhere
    assert "content-security-policy" not in web.get("/api/auth/config").headers
    assert "content-security-policy" not in web.get("/healthz").headers
    assert "content-security-policy" not in web.get("/_diag", headers={"x-test-user": ALICE}).headers    # the diagnostics page has an inline script
