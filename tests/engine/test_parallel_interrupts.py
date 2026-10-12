"""Several things can pause at once (parallel tool calls, parallel helpers), and a graph may pause with its own ``interrupt(value)``:
every pause is shown, answered under its own interrupt id, and nothing is dropped."""
import pytest
from langchain_core.tools import tool
from langgraph.types import interrupt

from pinboard.agent.runner import GENERIC_INTERRUPT, _pending, resume_value
from pinboard.engine.errors import BadRequest

from helpers import call, say
from turn_harness import BACKENDS, Hub, events_of, kinds


@pytest.fixture(params=BACKENDS)
def backend(request):
    return request.param


@tool
def pick_colour(item: str) -> str:
    """Ask the person which colour to use for an item."""
    answer = interrupt({"question": f"colour for {item}?"})
    return f"{item} will be {answer}"


@tool
def plain_value() -> str:
    """Pause with a bare string."""
    return f"heard: {interrupt('are you sure?')}"


def hub(backend, tmp_path, monkeypatch, scripts, tools):
    h = Hub(backend, tmp_path, monkeypatch, scripts, tools=tools)
    h.new_process()
    return h


def two_calls():
    from langchain_core.messages import AIMessage
    return AIMessage(content="", tool_calls=[{"name": "pick_colour", "args": {"item": "door"}, "id": "p1", "type": "tool_call"},
                                             {"name": "pick_colour", "args": {"item": "wall"}, "id": "p2", "type": "tool_call"}])


def tool_results(h):
    return [m.content for msgs in h.models[-1].seen for m in msgs if getattr(m, "type", "") == "tool"]


def test_a_graphs_own_interrupt_is_shown_and_answered_with_free_text(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[call("plain_value", {}), say("noted")]], [plain_value])
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "go")
    assert run.status == "awaiting_approval"  # it used to look finished: the interrupt was silently ignored
    appr = h.svc.list_approvals(status="pending")[0]
    item = appr.previews[0]
    assert item["kind"] == "interrupt" and item["allowed_decisions"] == ["respond"] and item["preview"]["value"] == "are you sure?"
    with pytest.raises(BadRequest):
        h.svc.decide(appr.id, "approve")  # a free-text question cannot be 'approved'
    done = h.svc.decide(appr.id, decisions=[{"type": "respond", "message": "yes, go on"}])
    assert done.status == "completed" and done.output == "noted"
    assert any("heard: yes, go on" in t for t in tool_results(h))
    h.close()


def test_two_things_pausing_at_once_are_one_approval_answered_together(backend, tmp_path, monkeypatch):
    h = hub(backend, tmp_path, monkeypatch, [[two_calls(), say("both painted")]], [pick_colour])
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "paint")
    assert run.status == "awaiting_approval"
    appr = h.svc.list_approvals(status="pending")[0]
    assert len(appr.action_requests) == 2 and len({a["interrupt_id"] for a in appr.action_requests}) == 2
    assert [p["kind"] for p in appr.previews] == ["interrupt", "interrupt"]
    asked = {p["preview"]["value"]["question"]: i for i, p in enumerate(appr.previews)}
    answers = [None, None]
    answers[asked["colour for door?"]] = {"type": "respond", "message": "red"}
    answers[asked["colour for wall?"]] = {"type": "respond", "message": "blue"}
    done = h.svc.decide(appr.id, decisions=answers)  # one answer per question, in the order shown
    assert done.status == "completed" and done.output == "both painted"
    texts = tool_results(h)
    assert any("door will be red" in t for t in texts) and any("wall will be blue" in t for t in texts)  # each answer reached ITS question
    assert kinds(events_of(h.svc, run.id)).count("interrupt") == 1
    h.close()


def test_the_pause_survives_a_restart_and_is_still_answered_by_id(backend, tmp_path, monkeypatch):
    if backend != "postgres":
        pytest.skip("a restart only loses state with a real database")
    h = hub(backend, tmp_path, monkeypatch, [[two_calls(), say("both painted")]], [pick_colour])
    s = h.svc.create_session()
    h.svc.send_message(s.id, "paint")
    appr_id = h.svc.list_approvals(status="pending")[0].id
    h.new_process()  # everything in memory is gone; the ids come from the database
    done = h.svc.decide(appr_id, decisions=[{"type": "respond", "message": "red"}, {"type": "respond", "message": "blue"}])
    assert done.status == "completed"
    h.close()


# ── the grouping itself ──────────────────────────────────────────────────

class Intr:  # what langgraph hands out: .id and .value
    def __init__(self, id, value):
        self.id, self.value = id, value


HITL = {"action_requests": [{"name": "send_email", "args": {}}, {"name": "execute", "args": {}}],
        "review_configs": [{"allowed_decisions": ["approve", "reject"]}, {"allowed_decisions": ["approve"]}]}


def test_pending_lists_hitl_requests_and_generic_interrupts_each_with_its_id():
    out = _pending({"__interrupt__": [Intr("a" * 32, HITL), Intr("b" * 32, "plain text"), Intr("c" * 32, {"free": "dict"})]})
    assert [p["name"] for p in out] == ["send_email", "execute", GENERIC_INTERRUPT, GENERIC_INTERRUPT]
    assert [p["interrupt_id"][0] for p in out] == ["a", "a", "b", "c"]
    assert out[0]["allowed_decisions"] == ["approve", "reject"] and out[1]["allowed_decisions"] == ["approve"]
    assert out[2]["args"] == {"value": "plain text"} and out[3]["args"] == {"value": {"free": "dict"}}


def test_resume_value_groups_decisions_under_the_interrupt_that_asked():
    pending = _pending({"__interrupt__": [Intr("a" * 32, HITL), Intr("b" * 32, "plain text")]})
    decisions = [{"type": "approve"}, {"type": "reject", "message": "no"}, {"type": "respond", "message": "fine"}]
    assert resume_value(pending, decisions) == {"a" * 32: {"decisions": decisions[:2]}, "b" * 32: "fine"}


def test_resume_value_keeps_the_old_plain_form_for_pauses_recorded_before_ids_existed():
    old = [{"name": "execute", "args": {}}]
    assert resume_value(old, [{"type": "approve"}]) == {"decisions": [{"type": "approve"}]}


def test_resume_value_refuses_a_wrong_number_of_decisions():
    with pytest.raises(ValueError):
        resume_value(_pending({"__interrupt__": [Intr("a" * 32, HITL)]}), [{"type": "approve"}])


def test_the_order_of_pending_actions_does_not_depend_on_where_the_list_was_read_from():
    """Decisions are positional. The run result and the saved checkpoint list the same pauses in different orders, so the order is fixed by id."""
    a, b = Intr("a" * 32, "first"), Intr("b" * 32, "second")
    assert _pending({"__interrupt__": [a, b]}) == _pending({"__interrupt__": [b, a]})
