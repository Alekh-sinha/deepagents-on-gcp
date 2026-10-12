"""Saved MCP servers and scheduled tasks: the same behaviour in memory and in Postgres."""
import threading
from datetime import timedelta

import pytest

from pinboard.state import McpServerRecord, ScheduleRecord, ScheduleRunRecord, SessionRecord, new_mcp_id, new_schedule_id, new_schedule_run_id, new_session_id, utcnow

from test_state import store  # noqa: F401  (the `store` fixture runs every test on both backends)

DAY = timedelta(days=1)


def server(user="u@x.com", name="calc", **kw):
    base = dict(id=new_mcp_id(), user_id=user, name=name, url="https://tools.example.com/mcp", headers_enc="cipher", header_names=["Authorization"],
                tools=[{"name": "add", "description": "Add", "input_schema": {"type": "object"}}])
    base.update(kw)
    return McpServerRecord(**base)


def schedule(user="u@x.com", cron="0 9 * * *", next_run=None, **kw):
    return ScheduleRecord(id=new_schedule_id(), user_id=user, title="Brief", instructions="Say hi.", cron=cron, next_run_at=next_run, **kw)


# ── MCP servers ──────────────────────────────────────────────────────────

def test_a_server_round_trips_with_every_field(store):
    saved = store.put_mcp_server(server(transport="sse", enabled=False, requires_approval=False, no_approval_tools=["add"], tools_checked_at=utcnow(), last_error="oops"))
    got = store.get_mcp_server("u@x.com", "calc")
    assert (got.url, got.transport, got.headers_enc, got.header_names, got.enabled, got.requires_approval, got.no_approval_tools, got.last_error) == \
        ("https://tools.example.com/mcp", "sse", "cipher", ["Authorization"], False, False, ["add"], "oops")
    assert got.tools == [{"name": "add", "description": "Add", "input_schema": {"type": "object"}}] and got.tools_checked_at is not None and saved.id == got.id


def test_saving_the_same_name_again_replaces_it_and_keeps_the_original_id_and_creation_time(store):
    first = store.put_mcp_server(server(url="https://one.example.com/mcp"))
    second = store.put_mcp_server(server(url="https://two.example.com/mcp", headers_enc=""))
    assert second.id == first.id and second.created_at == first.created_at and store.get_mcp_server("u@x.com", "calc").url == "https://two.example.com/mcp"
    assert len(store.list_mcp_servers("u@x.com")) == 1


def test_servers_are_per_user_and_listed_by_name(store):
    for user, name in (("a@x.com", "zeta"), ("a@x.com", "alpha"), ("b@x.com", "alpha")):
        store.put_mcp_server(server(user, name))
    assert [s.name for s in store.list_mcp_servers("a@x.com")] == ["alpha", "zeta"] and [s.user_id for s in store.list_mcp_servers("b@x.com")] == ["b@x.com"]
    assert store.get_mcp_server("b@x.com", "zeta") is None and store.delete_mcp_server("b@x.com", "zeta") is False
    assert store.delete_mcp_server("a@x.com", "zeta") is True and [s.name for s in store.list_mcp_servers("a@x.com")] == ["alpha"]


def test_a_server_can_be_updated_field_by_field_but_only_the_allowed_ones(store):
    store.put_mcp_server(server())
    up = store.update_mcp_server("u@x.com", "calc", enabled=False, tools=[], header_names=[], last_error="down")
    assert (up.enabled, up.tools, up.header_names, up.last_error) == (False, [], [], "down")
    assert store.update_mcp_server("u@x.com", "missing", enabled=True) is None
    with pytest.raises(ValueError):
        store.update_mcp_server("u@x.com", "calc", id="evil")


# ── schedules ────────────────────────────────────────────────────────────

def test_a_schedule_round_trips_and_updates(store):
    s = schedule(next_run=utcnow() + DAY, timezone="Asia/Kolkata", model="m1", auto_approve={"kinds": ["shell"], "max_per_turn": 5})
    store.create_schedule(s)
    got = store.get_schedule(s.id)
    assert (got.title, got.cron, got.timezone, got.model, got.auto_approve, got.enabled, got.run_count) == ("Brief", "0 9 * * *", "Asia/Kolkata", "m1", {"kinds": ["shell"], "max_per_turn": 5}, True, 0)
    up = store.update_schedule(s.id, title="Evening", auto_approve=None, run_count=3, last_status="started")
    assert (up.title, up.auto_approve, up.run_count, up.last_status) == ("Evening", None, 3, "started")
    assert store.update_schedule("nope", title="x") is None
    with pytest.raises(ValueError):
        store.update_schedule(s.id, user_id="evil")


def test_only_enabled_schedules_whose_time_has_come_are_due_oldest_first(store):
    now = utcnow()
    early, late, future, paused, never = (schedule(next_run=now - timedelta(minutes=30)), schedule(next_run=now - timedelta(minutes=5)), schedule(next_run=now + DAY),
                                          schedule(next_run=now - DAY, enabled=False), schedule(next_run=None))
    for s in (late, future, paused, never, early):
        store.create_schedule(s)
    assert [s.id for s in store.due_schedules(now)] == [early.id, late.id]
    assert len(store.due_schedules(now, limit=1)) == 1


def test_a_claim_moves_a_due_schedule_on_exactly_once_even_when_instances_race(store):
    now = utcnow()
    due = now - timedelta(minutes=1)
    s = schedule(next_run=due)
    store.create_schedule(s)
    wins, gate = [], threading.Barrier(8)

    def contend():
        gate.wait()
        wins.append(store.claim_schedule(s.id, due, now + DAY, now))

    threads = [threading.Thread(target=contend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1 and store.get_schedule(s.id).next_run_at == now + DAY
    assert store.claim_schedule(s.id, due, now + 2 * DAY, now) is False            # a stale view of the schedule loses
    store.update_schedule(s.id, enabled=False)
    assert store.claim_schedule(s.id, now + DAY, now + 2 * DAY, now) is False       # a paused schedule cannot be claimed


def test_runs_are_listed_newest_first_counted_per_user_and_deleted_with_their_schedule(store):
    now = utcnow()
    s, other = schedule(), schedule(user="o@x.com")
    store.create_schedule(s)
    store.create_schedule(other)
    for i, (status, ago) in enumerate([("started", 3), ("skipped", 2), ("started", 1)]):
        store.add_schedule_run(ScheduleRunRecord(id=new_schedule_run_id(), schedule_id=s.id, user_id=s.user_id, trigger="schedule", status=status, created_at=now - timedelta(hours=ago)))
    store.add_schedule_run(ScheduleRunRecord(id=new_schedule_run_id(), schedule_id=other.id, user_id="o@x.com", trigger="manual", status="started", created_at=now))
    assert [r.status for r in store.list_schedule_runs(s.id)] == ["started", "skipped", "started"] and len(store.list_schedule_runs(s.id, limit=2)) == 2
    assert store.count_schedule_runs_since("u@x.com", now - timedelta(hours=24)) == 2        # skipped runs do not count against the daily limit
    assert store.count_schedule_runs_since("u@x.com", now - timedelta(hours=2, minutes=30)) == 1
    rid = store.list_schedule_runs(s.id)[0].id
    store.update_schedule_run(rid, session_id="s_1", turn_id="t_1", note="n")
    assert (store.list_schedule_runs(s.id)[0].session_id, store.list_schedule_runs(s.id)[0].turn_id) == ("s_1", "t_1")
    assert store.delete_schedule(s.id) is True and store.list_schedule_runs(s.id) == [] and len(store.list_schedule_runs(other.id)) == 1
    assert store.delete_schedule(s.id) is False


def test_a_session_remembers_which_schedule_opened_it_and_its_auto_approve_setting(store):
    sid = new_session_id()
    store.create_session(SessionRecord(id=sid, user_id="u@x.com", thread_id=sid, schedule_id="sc_1", auto_approve={"kinds": ["shell"], "max_per_turn": 3}))
    got = store.get_session(sid)
    assert got.schedule_id == "sc_1" and got.auto_approve == {"kinds": ["shell"], "max_per_turn": 3} and got.to_dict()["schedule_id"] == "sc_1"
