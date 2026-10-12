"""MCP through the service: import, safety, isolation, the approval card, and a real local server used by the agent in a full turn."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from pinboard.core.policy import parse_policy
from pinboard.engine.api import create_app
from pinboard.engine.errors import BadRequest, NotFound
from pinboard.mcp import generate_key

from helpers import call, say
from mcp_fixtures import running_server
from test_policy import BASE
from turn_harness import Hub, events_of

PUBLIC = lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))]  # noqa: E731


def policy(**mcp):
    d = copy.deepcopy(BASE)
    d["limits"] = {"max_turn_minutes": 10, "max_concurrent_turns": 4, "max_auto_resumes": 1, "sandbox_idle_minutes": 20}
    d["mcp"] = {"enabled": True, "allow_private_hosts": True, **mcp}   # the test servers live on localhost
    return parse_policy(d)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("MCP_ENCRYPTION_KEY", generate_key())


def hub(tmp_path, monkeypatch, scripts=None, pol=None, **kw):
    h = Hub("memory", tmp_path, monkeypatch, scripts or [[say("hi")]], policy=pol or policy(), **kw)
    h.new_process()
    return h


def system_text(model) -> str:
    content = next(m.content for m in model.seen[0] if getattr(m, "type", "") == "system")
    return content if isinstance(content, str) else "".join(b.get("text", "") for b in content if isinstance(b, dict))


def config(url, token="s3cret", name="calc"):
    return {"mcpServers": {name: {"url": url, "headers": {"Authorization": f"Bearer {token}"}}}}


# ── importing and saving ─────────────────────────────────────────────────

def test_pasting_a_config_connects_the_server_lists_its_tools_and_never_returns_the_token(tmp_path, monkeypatch):
    with running_server(token="s3cret") as (url, seen):
        h = hub(tmp_path, monkeypatch)
        out = h.svc.import_mcp_config(config(url), user_id="me@example.com")
        assert out["results"] == [{"name": "calc", "saved": True, "reachable": True, "tool_count": 4, "error": "", "warnings": []}]
        [srv] = h.svc.list_mcp_servers(user_id="me@example.com")
        assert srv["header_names"] == ["Authorization"] and srv["tool_count"] == 4 and srv["last_error"] == "" and srv["requires_approval"] is True
        assert {t["name"] for t in srv["tools"]} == {"add", "shout", "boom", "long_text"} and all(t["needs_approval"] for t in srv["tools"])
        assert "s3cret" not in json.dumps(srv) and "headers_enc" not in srv
        stored = h.state.store.get_mcp_server("me@example.com", "calc")
        assert "s3cret" not in stored.headers_enc and stored.headers_enc  # encrypted at rest
        assert "Bearer s3cret" in seen


def test_a_server_that_cannot_be_reached_is_saved_with_its_error_and_the_others_still_connect(tmp_path, monkeypatch):
    with running_server(token="s3cret") as (url, _):
        h = hub(tmp_path, monkeypatch)
        out = h.svc.import_mcp_config({"mcpServers": {"good": {"url": url, "headers": {"Authorization": "Bearer s3cret"}},
                                                      "wrongtoken": {"url": url, "headers": {"Authorization": "Bearer nope"}}}}, user_id="me@example.com")
        by = {r["name"]: r for r in out["results"]}
        assert by["good"]["reachable"] and not by["wrongtoken"]["reachable"] and "HTTP 401" in by["wrongtoken"]["error"] and by["wrongtoken"]["saved"]
        assert h.svc.test_mcp_server("wrongtoken", user_id="me@example.com")["tool_count"] == 0


def test_saving_again_keeps_the_stored_token_unless_new_headers_are_sent(tmp_path, monkeypatch):
    with running_server(token="s3cret") as (url, _):
        h = hub(tmp_path, monkeypatch)
        h.svc.put_mcp_server("calc", url, headers={"Authorization": "Bearer s3cret"}, user_id="me@example.com")
        again = h.svc.put_mcp_server("calc", url, user_id="me@example.com", requires_approval=False)   # headers omitted: keep them
        assert again["tool_count"] == 4 and again["last_error"] == "" and again["requires_approval"] is False
        cleared = h.svc.put_mcp_server("calc", url, headers={}, user_id="me@example.com")                # explicitly cleared: the server now refuses us
        assert cleared["header_names"] == [] and "HTTP 401" in cleared["last_error"]


def test_everything_that_could_hurt_is_refused_with_a_reason(tmp_path, monkeypatch):
    h = hub(tmp_path, monkeypatch, pol=policy(allow_private_hosts=False, max_servers_per_user=1), mcp_resolver=PUBLIC)
    for url, why in [("http://tools.example.com/mcp", "https"), ("https://user:pw@tools.example.com/mcp", "user name"), ("https://localhost/mcp", "public"),
                     ("https://169.254.169.254/x", "not a public")]:
        with pytest.raises(BadRequest, match=why):
            h.svc.put_mcp_server("x", url, user_id="me@example.com", test=False)
    with pytest.raises(BadRequest, match="not valid"):
        h.svc.put_mcp_server("Bad Name", "https://tools.example.com/mcp", user_id="me@example.com", test=False)
    with pytest.raises(BadRequest, match="command"):
        h.svc.import_mcp_config({"mcpServers": {"x": {"command": "npx", "args": ["evil"]}}}, user_id="me@example.com")
    h.svc.put_mcp_server("one", "https://tools.example.com/mcp", user_id="me@example.com", test=False)
    with pytest.raises(BadRequest, match="at most 1"):
        h.svc.put_mcp_server("two", "https://tools.example.com/mcp", user_id="me@example.com", test=False)
    h.svc.put_mcp_server("one", "https://other.example.com/mcp", user_id="me@example.com", test=False)  # replacing an existing one is not "another server"


def test_the_address_is_checked_again_when_a_saved_server_is_tested(tmp_path, monkeypatch):
    names = {"ok": PUBLIC}
    h = hub(tmp_path, monkeypatch, pol=policy(allow_private_hosts=False), mcp_resolver=lambda host, port: names["ok"](host, port))
    h.svc.put_mcp_server("shifty", "https://tools.example.com/mcp", user_id="me@example.com", test=False)
    names["ok"] = lambda host, port: [(2, 1, 6, "", ("10.0.0.7", 0))]   # the name now points inside the network
    assert "not a public address" in h.svc.test_mcp_server("shifty", user_id="me@example.com")["last_error"]


def test_mcp_can_be_switched_off_and_needs_its_key(tmp_path, monkeypatch):
    off = hub(tmp_path, monkeypatch, pol=policy(enabled=False))
    for fn in (lambda: off.svc.import_mcp_config(config("https://x.example.com/mcp"), user_id="u"), lambda: off.svc.put_mcp_server("a", "https://x.example.com/mcp", user_id="u"),
               lambda: off.svc.test_mcp_server("a", user_id="u")):
        with pytest.raises(BadRequest, match="switched off"):
            fn()
    monkeypatch.delenv("MCP_ENCRYPTION_KEY")
    (tmp_path / "b").mkdir()
    on = hub(tmp_path / "b", monkeypatch)
    with pytest.raises(BadRequest, match="MCP_ENCRYPTION_KEY"):
        on.svc.put_mcp_server("a", "http://127.0.0.1:9/mcp", headers={"A": "b"}, user_id="u", test=False)


def test_servers_belong_to_one_user(tmp_path, monkeypatch):
    with running_server() as (url, _):
        h = hub(tmp_path, monkeypatch)
        h.svc.put_mcp_server("calc", url, user_id="alice@example.com")
        assert h.svc.list_mcp_servers(user_id="bob@example.com") == []
        for fn in (lambda: h.svc.delete_mcp_server("calc", user_id="bob@example.com"), lambda: h.svc.test_mcp_server("calc", user_id="bob@example.com"),
                   lambda: h.svc.update_mcp_server("calc", {"enabled": False}, user_id="bob@example.com")):
            with pytest.raises(NotFound):
                fn()
        assert h.svc.mcp_tools_for("bob@example.com") is None and h.svc.mcp_tools_for("alice@example.com") is not None


def test_toggles_are_validated_and_deleting_removes_the_tools(tmp_path, monkeypatch):
    with running_server() as (url, _):
        h = hub(tmp_path, monkeypatch)
        h.svc.put_mcp_server("calc", url, user_id="me@example.com")
        up = h.svc.update_mcp_server("calc", {"no_approval_tools": ["add", "add"], "enabled": True}, user_id="me@example.com")
        assert next(t for t in up["tools"] if t["name"] == "add")["needs_approval"] is False and next(t for t in up["tools"] if t["name"] == "boom")["needs_approval"] is True
        for bad in ({}, {"enabled": "yes"}, {"no_approval_tools": ["nosuch"]}, {"no_approval_tools": "add"}, {"url": "https://evil.example.com"}):
            with pytest.raises(BadRequest):
                h.svc.update_mcp_server("calc", bad, user_id="me@example.com")
        h.svc.update_mcp_server("calc", {"enabled": False}, user_id="me@example.com")
        assert h.svc.mcp_tools_for("me@example.com") is None
        assert h.svc.delete_mcp_server("calc", user_id="me@example.com") == {"deleted": "calc"} and h.svc.list_mcp_servers(user_id="me@example.com") == []


def test_building_the_agents_tools_never_raises(tmp_path, monkeypatch):
    with running_server() as (url, _):
        h = hub(tmp_path, monkeypatch)
        h.svc.put_mcp_server("calc", url, user_id="me@example.com")
        from pinboard.gcp.vault import Vault

        monkeypatch.setenv("MCP_ENCRYPTION_KEY", "garbage")
        h.svc.vault = Vault("env")  # a fresh vault reads the broken key (the old one had cached the good one)
        assert h.svc.mcp_tools_for("me@example.com") is None  # logged, and the turn simply has no MCP tools


# ── the agent uses them ──────────────────────────────────────────────────

def test_a_turn_pauses_on_an_mcp_card_then_runs_the_tool_on_the_real_server(tmp_path, monkeypatch):
    with running_server(token="s3cret") as (url, seen):
        h = hub(tmp_path, monkeypatch, [[call("mcp__calc__add", {"a": 40, "b": 2}, "m1"), say("the sum is 42")], [say("the sum is 42")]])
        h.svc.import_mcp_config(config(url), user_id=h.svc.settings.user_id)
        s = h.svc.create_session()
        run = h.svc.send_message(s.id, "add 40 and 2")
        assert run.status == "awaiting_approval"
        card = h.svc.list_approvals(status="pending")[0].previews[0]
        assert card["kind"] == "mcp" and card["preview"]["server"] == "calc" and card["preview"]["tool"] == "add" and card["preview"]["args"] == {"a": "40", "b": "2"}
        assert card["allowed_decisions"] == ["approve", "edit", "reject"] and "add on calc" in card["title"]
        calls_before = len([x for x in seen if x])
        done = h.svc.decide(h.svc.list_approvals(status="pending")[0].id, "approve")
        assert done.status == "completed" and done.output == "the sum is 42" and len([x for x in seen if x]) > calls_before
        results = [e["payload"] for e in events_of(h.svc, run.id) if e["type"] == "tool_result"]
        assert any("42" in str(r.get("preview")) and "untrusted" in str(r.get("preview")) for r in results)


def test_a_rejected_mcp_call_never_reaches_the_server(tmp_path, monkeypatch):
    with running_server() as (url, seen):
        h = hub(tmp_path, monkeypatch, [[call("mcp__calc__add", {"a": 1, "b": 2}, "m1"), say("ok")], [say("ok")]])
        h.svc.put_mcp_server("calc", url, user_id=h.svc.settings.user_id)
        s = h.svc.create_session()
        h.svc.send_message(s.id, "add")
        before = len(seen)
        assert h.svc.decide(h.svc.list_approvals(status="pending")[0].id, "reject", message="no thanks").status == "completed"
        assert len(seen) == before


def test_a_tool_marked_safe_runs_without_asking_and_the_default_approve_all_does_not_cover_mcp(tmp_path, monkeypatch):
    with running_server() as (url, _):
        h = hub(tmp_path, monkeypatch, [[call("mcp__calc__add", {"a": 1, "b": 2}, "m1"), say("three")]] * 3)
        h.svc.put_mcp_server("calc", url, user_id=h.svc.settings.user_id)
        s1 = h.svc.create_session(auto_approve=True)                              # "approve all" = shell + github only
        assert h.svc.send_message(s1.id, "add").status == "awaiting_approval"
        h.svc.update_mcp_server("calc", {"no_approval_tools": ["add"]}, user_id=h.svc.settings.user_id)
        s2 = h.svc.create_session()
        assert h.svc.send_message(s2.id, "add").status == "completed"             # the user said this tool is safe
        h.svc.update_mcp_server("calc", {"no_approval_tools": []}, user_id=h.svc.settings.user_id)
        s3 = h.svc.create_session(auto_approve={"kinds": ["mcp"]})                 # ...or the user switched the mcp kind on
        assert h.svc.send_message(s3.id, "add").status == "completed"


# ── over the contract routes ─────────────────────────────────────────────

def test_the_contract_methods_manage_servers_and_never_leak_the_token(tmp_path, monkeypatch):
    with running_server(token="s3cret") as (url, _):
        h = hub(tmp_path, monkeypatch)
        c = TestClient(create_app(h.svc, "t" * 32))
        auth = {"Authorization": "Bearer " + "t" * 32, "content-type": "application/json"}

        def contract(method, inp=None):
            return c.post("/api/reasoning_engine", content=json.dumps(json.dumps({"class_method": method, "input": inp or {}})), headers=auth)

        r = contract("import_mcp_config", {"config": json.dumps(config(url))})
        assert r.status_code == 200 and r.json()["output"]["results"][0]["reachable"] is True
        listed = contract("list_mcp_servers").json()["output"]["servers"]
        assert listed[0]["name"] == "calc" and "s3cret" not in json.dumps(listed)
        assert contract("update_mcp_server", {"name": "calc", "requires_approval": False}).json()["output"]["requires_approval"] is False
        assert contract("test_mcp_server", {"name": "calc"}).json()["output"]["tool_count"] == 4
        assert contract("put_mcp_server", {"name": "x", "url": "https://x.example.com", "headers": "no"}).status_code == 400
        assert contract("import_mcp_config", {"config": "{not json"}).status_code == 400
        assert contract("delete_mcp_server", {"name": "calc"}).json()["output"] == {"deleted": "calc"}
        assert contract("delete_mcp_server", {"name": "calc"}).status_code == 404


def test_the_model_is_told_which_servers_are_connected_and_that_their_output_is_untrusted(tmp_path, monkeypatch):
    with running_server() as (url, _):
        h = hub(tmp_path, monkeypatch, [[say("hello")]])
        h.svc.put_mcp_server("calc", url, user_id=h.svc.settings.user_id)
        s = h.svc.create_session()
        h.svc.send_message(s.id, "hi")
        system = system_text(h.models[0])
        assert "Connected MCP servers" in system and "calc: mcp__calc__add" in system and "untrusted" in system
        plain = hub(tmp_path / "x", monkeypatch, [[say("hello")]]) if (tmp_path / "x").mkdir() is None else None
        plain.svc.send_message(plain.svc.create_session().id, "hi")
        assert "Connected MCP servers" not in system_text(plain.models[0])
