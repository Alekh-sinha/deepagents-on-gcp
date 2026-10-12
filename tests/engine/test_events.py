"""EventMapper: the agent stream is reduced to small, capped events. Large content never reaches the event log."""
import json

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.types import Command

from pinboard.agent import build_agent
from pinboard.engine.events import (ARG_PREVIEW_CHARS, ASSISTANT_MESSAGE_MAX_CHARS, MAX_EVENT_BYTES, RESULT_PREVIEW_CHARS, TOKEN_FLUSH_CHARS, EventMapper,
                             cap_payload, text_of)
from pinboard.gcp.sandbox import GCPShellSandbox
from pinboard.gcp.vault import Vault
from pinboard.core.guardrails import CommandGuard

from helpers import FakeClient, ScriptedModel, call, make_settings, say


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def chunk(text, mid="m1", node="model"):
    return (AIMessageChunk(content=text, id=mid), {"langgraph_node": node})


def types(emits):
    return [e.type for e in emits]


def feed_all(m, items):
    out = []
    for ns, mode, data in items:
        out += m.feed(ns, mode, data)
    return out


# ── live text: batching ──────────────────────────────────────────────────

def test_text_chunks_are_batched_by_size():
    m = EventMapper(Clock())
    out = []
    for _ in range(20):
        out += m.feed((), "messages", chunk("x" * 100))
    assert types(out) == ["token", "token"]  # 2000 chars arrived -> two batches of 800+, the rest still buffered
    assert all(len(e.payload["text"]) >= TOKEN_FLUSH_CHARS and e.payload["message_id"] == "m1" for e in out)
    rest = m.flush()
    assert types(rest) == ["token"] and sum(len(e.payload["text"]) for e in out + rest) == 2000


def test_text_chunks_are_flushed_after_a_pause_even_when_small():
    clock = Clock()
    m = EventMapper(clock)
    assert m.feed((), "messages", chunk("Hel")) == []
    clock.t += 0.1
    assert m.feed((), "messages", chunk("lo")) == []
    clock.t += 0.5  # the model paused; the next chunk triggers the time-based flush
    out = m.feed((), "messages", chunk(" world"))
    assert [e.payload["text"] for e in out] == ["Hello world"]


def test_only_model_text_chunks_count_as_live_text():
    m = EventMapper(Clock())
    ignored = [chunk("hi", node="tools"), (AIMessage(content="whole message", id="m9"), {"langgraph_node": "model"}),
               (ToolMessage(content="out", tool_call_id="c1"), {"langgraph_node": "tools"}), chunk(""), "garbage", (None, None)]
    for item in ignored:
        assert m.feed((), "messages", item) == []
    assert m.flush() == []


def test_gemini_style_block_content_is_flattened():
    assert text_of([{"type": "text", "text": "a"}, {"type": "thinking", "x": 1}, "b", {"text": "c"}]) == "abc"
    assert text_of(None) == "" and text_of(5) == "5"


# ── finished messages replace their batches ──────────────────────────────

def test_a_finished_message_replaces_its_token_batches():
    m = EventMapper(Clock())
    sent = []
    for _ in range(9):
        sent += m.feed((), "messages", chunk("y" * 100))
    assert types(sent) == ["token"]
    out = m.feed((), "updates", {"model": {"messages": [AIMessage(content="y" * 900, id="m1")]}})
    assert types(out) == ["assistant_message", "_compact"]
    assert out[0].payload["text"] == "y" * 900 and out[0].payload["agent"] == "assistant" and out[1].payload == {"message_id": "m1"}
    assert m.flush() == []  # the unsent tail was dropped: the final message supersedes it


def test_no_compaction_is_requested_when_no_batch_was_ever_written():
    m = EventMapper(Clock())
    m.feed((), "messages", chunk("short"))  # buffered, never flushed
    out = m.feed((), "updates", {"model": {"messages": [AIMessage(content="short", id="m1")]}})
    assert types(out) == ["assistant_message"]


def test_a_huge_answer_is_capped_but_reports_its_real_size():
    out = EventMapper(Clock()).feed((), "updates", {"model": {"messages": [AIMessage(content="w" * 250_000, id="big")]}})
    p = out[0].payload
    assert len(p["text"]) == ASSISTANT_MESSAGE_MAX_CHARS and p["truncated"] is True and p["chars"] == 250_000


# ── tool calls and results ───────────────────────────────────────────────

def test_tool_call_arguments_are_clipped():
    msg = AIMessage(content="", id="a1", tool_calls=[{"name": "write_file", "id": "c1", "type": "tool_call",
                                                     "args": {"file_path": "/workspace/out/r.md", "content": "z" * 90_000, "tags": list(range(50))}}])
    out = EventMapper(Clock()).feed((), "updates", {"model": {"messages": [msg]}})
    assert types(out) == ["tool_call"]
    a = out[0].payload["args"]
    assert a["file_path"] == "/workspace/out/r.md" and len(a["content"]) < ARG_PREVIEW_CHARS + 60 and "more characters" in a["content"]
    assert len(a["tags"]) == 21 and a["tags"][-1] == "[... 30 more items]"
    assert len(json.dumps(out[0].payload)) < 4000


def test_a_40000_token_tool_result_becomes_a_small_preview_event():
    big = "line of output\n" * 11_000  # ~165,000 characters, roughly 40,000 tokens
    msg = ToolMessage(content=big, tool_call_id="c1", name="read_file")
    out = EventMapper(Clock()).feed((), "updates", {"tools": {"messages": [msg]}})
    assert types(out) == ["tool_result"]
    p = out[0].payload
    assert p["chars"] == len(big) and p["truncated"] is True and len(p["preview"]) == RESULT_PREVIEW_CHARS
    assert p["name"] == "read_file" and p["status"] == "success" and len(json.dumps(p)) < 3000


def test_tool_errors_keep_their_status():
    msg = ToolMessage(content="boom", tool_call_id="c1", name="execute", status="error")
    assert EventMapper(Clock()).feed((), "updates", {"tools": {"messages": [msg]}})[0].payload["status"] == "error"


def test_a_call_that_reappears_is_shown_once_and_an_edit_is_marked():
    m = EventMapper(Clock())
    original = AIMessage(content="", id="a1", tool_calls=[{"name": "send_email", "id": "c1", "type": "tool_call", "args": {"subject": "Hi"}}])
    assert types(m.feed((), "updates", {"model": {"messages": [original]}})) == ["tool_call"]
    assert m.feed((), "updates", {"HumanInTheLoopMiddleware.after_model": {"messages": [original]}}) == []  # re-sent after approval
    edited = AIMessage(content="", id="a1", tool_calls=[{"name": "send_email", "id": "c1", "type": "tool_call", "args": {"subject": "Edited"}}])
    out = m.feed((), "updates", {"HumanInTheLoopMiddleware.after_model": {"messages": [edited]}})
    assert types(out) == ["tool_call"] and out[0].payload["edited"] is True and out[0].payload["args"]["subject"] == "Edited"


def test_results_are_shown_once():
    m = EventMapper(Clock())
    msg = ToolMessage(content="r", tool_call_id="c1", name="ls")
    assert len(m.feed((), "updates", {"tools": {"messages": [msg]}})) == 1
    assert m.feed((), "updates", {"tools": {"messages": [msg]}}) == []


# ── todos, interrupts, subagents ─────────────────────────────────────────

def test_todo_updates_are_capped():
    todos = [{"content": "t" * 1000, "status": "pending"}] * 80 + ["not a dict"]
    out = EventMapper(Clock()).feed((), "updates", {"tools": {"todos": todos}})
    p = out[0].payload
    assert out[0].type == "todos" and len(p["todos"]) == 50 and len(p["todos"][0]["content"]) == 300


def test_an_interrupt_flushes_pending_text_and_is_passed_on():
    m = EventMapper(Clock())
    m.feed((), "messages", chunk("about to ask"))
    out = m.feed((), "updates", {"__interrupt__": ("raw-interrupt",)})
    assert types(out) == ["token", "_interrupt"] and out[1].payload["raw"] == ("raw-interrupt",) and out[0].payload["text"] == "about to ask"


def test_a_helpers_pause_reported_twice_is_emitted_once():
    class Intr:
        def __init__(self, id, value="v"):
            self.id, self.value = id, value

    m = EventMapper(Clock())
    first = m.feed(("tools:abc",), "updates", {"__interrupt__": (Intr("i1"),)})
    again = m.feed((), "updates", {"__interrupt__": (Intr("i1"),)})  # the same pause, bubbled up through the parent
    other = m.feed((), "updates", {"__interrupt__": (Intr("i1"), Intr("i2"))})
    assert types(first) == ["_interrupt"] and again == []
    assert types(other) == ["_interrupt"] and [i.id for i in other[0].payload["raw"]] == ["i2"]


def test_helper_events_carry_the_helper_name():
    m = EventMapper(Clock())
    task = AIMessage(content="", id="a1", tool_calls=[{"name": "task", "id": "t1", "type": "tool_call", "args": {"description": "d", "subagent_type": "researcher"}}])
    assert m.feed((), "updates", {"model": {"messages": [task]}})[0].payload["agent"] == "assistant"
    sub = m.feed(("tools:abc",), "updates", {"model": {"messages": [AIMessage(content="found it", id="s1")]}})
    assert sub[0].payload["agent"] == "researcher"
    again = m.feed(("tools:abc",), "updates", {"model": {"messages": [AIMessage(content="more", id="s2")]}})
    assert again[0].payload["agent"] == "researcher"  # the namespace keeps its label
    other = m.feed(("tools:zzz",), "updates", {"model": {"messages": [AIMessage(content="x", id="s3")]}})
    assert other[0].payload["agent"] == "helper"  # no pending task call to pair it with


def test_malformed_stream_items_never_raise():
    m = EventMapper(Clock())
    for ns, mode, data in [((), "updates", None), ((), "updates", {"model": "str"}), ((), "updates", {"model": {"messages": [object()]}}),
                           ((), "messages", 5), ((), "custom", {"a": 1}), ((), "values", {})]:
        assert m.feed(ns, mode, data) == []


# ── the last line of defence ─────────────────────────────────────────────

def test_cap_payload_leaves_small_payloads_alone_and_shrinks_big_nested_ones():
    small = {"a": "b", "n": [1, 2]}
    assert cap_payload(small) is small
    big = {"name": "x", "nested": {"rows": [{"t": "q" * 200_000}, {"t": "r" * 100_000}]}, "tail": "k" * 5000}
    capped = cap_payload(big)
    assert len(json.dumps(capped).encode()) <= MAX_EVENT_BYTES and capped["_capped"] is True and capped["name"] == "x"
    hopeless = {"items": [str(i) * 30 for i in range(5000)]}  # many medium strings: cannot be squeezed by halving the longest
    out = cap_payload(hopeless)
    assert len(json.dumps(out).encode()) <= MAX_EVENT_BYTES and out["_capped"] is True


# ── a real agent stream ──────────────────────────────────────────────────

def test_a_real_agent_stream_maps_to_a_readable_story(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    s = make_settings(ws, skills_source=None, approval_mode="execute")
    sb = GCPShellSandbox(FakeClient(ws), "f", guard=CommandGuard("allowlist", str(ws)), cwd=str(ws))
    script = [call("write_todos", {"todos": [{"content": "look", "status": "in_progress"}]}, "c1"),
              call("task", {"description": "look it up", "subagent_type": "researcher"}, "c2"), say("the sub result"),
              call("execute", {"command": "echo hi"}, "c3"), say("all done")]
    rt = build_agent(s, Vault("env"), model=ScriptedModel(script=script), sandbox=sb, tools=[])
    cfg = {"configurable": {"thread_id": "t"}}
    mapper = EventMapper(Clock())

    def story(payload):
        out = []
        for ns, mode, data in rt.agent.stream(payload, config=cfg, stream_mode=["updates", "messages"], subgraphs=True):
            out += mapper.feed(ns, mode, data)
        return out + mapper.flush()

    first = story({"messages": [{"role": "user", "content": "go"}]})
    kinds = types(first)
    assert kinds.count("todos") == 1 and "_interrupt" in kinds and kinds[-1] == "_interrupt"
    calls = [(e.payload["name"], e.payload["agent"]) for e in first if e.type == "tool_call"]
    assert calls == [("write_todos", "assistant"), ("task", "assistant"), ("execute", "assistant")]
    sub = [e for e in first if e.type == "assistant_message" and e.payload["agent"] == "researcher"]
    assert sub and sub[0].payload["text"] == "the sub result"
    results = {e.payload["tool_call_id"] for e in first if e.type == "tool_result"}
    assert {"c1", "c2"} <= results and "c3" not in results  # the shell call is waiting for approval
    assert all(len(json.dumps(e.payload).encode()) <= MAX_EVENT_BYTES for e in first if not e.type.startswith("_"))  # control outputs are not stored

    second = story(Command(resume={"decisions": [{"type": "approve"}]}))
    assert [e.payload["name"] for e in second if e.type == "tool_result"] == ["execute"]
    assert [e.payload["text"] for e in second if e.type == "assistant_message"] == ["all done"]
    assert "tool_call" not in types(second)  # the approved call is not announced a second time


def test_priming_with_the_conversation_so_far_stops_old_steps_being_announced_again():
    old_call = AIMessage(content="", id="a1", tool_calls=[{"name": "write_file", "id": "c1", "type": "tool_call", "args": {"file_path": "/x"}}])
    old_result = ToolMessage(content="ok", tool_call_id="c1", name="write_file")
    old_answer = AIMessage(content="Done earlier.", id="a2")
    m = EventMapper(Clock()).prime_messages([old_call, old_result, old_answer])
    # the agent's start-up step reports the whole history again
    replay = m.feed((), "updates", {"PatchToolCallsMiddleware.before_agent": {"messages": [old_call, old_result, old_answer]}})
    assert replay == []
    fresh = m.feed((), "updates", {"model": {"messages": [AIMessage(content="A new answer.", id="a3")]}})
    assert types(fresh) == ["assistant_message"] and fresh[0].payload["text"] == "A new answer."
    placeholder = ToolMessage(content="Tool call write_file did not complete", tool_call_id="c-cut-off", name="write_file", status="error")
    assert types(EventMapper(Clock()).prime_messages([old_call]).feed((), "updates", {"x": {"messages": [placeholder]}})) == ["tool_result"]  # genuinely new
    assert EventMapper(Clock()).prime_messages(None).prime_messages([object(), "junk"]) is not None  # odd input never raises


def test_each_model_call_reports_its_token_usage_once():
    usage = {"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280, "output_token_details": {"reasoning": 55}, "input_token_details": {"cache_read": 900}}
    msg = AIMessage(content="Hello", id="a1", usage_metadata=usage)
    m = EventMapper(Clock())
    out = m.feed((), "updates", {"model": {"messages": [msg]}})
    assert types(out) == ["usage", "assistant_message"]  # a model call's cost first, then what it produced
    assert out[0].payload == {"message_id": "a1", "agent": "assistant", "input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280,
                              "reasoning_tokens": 55, "cached_tokens": 900, "model": None}
    assert m.feed((), "updates", {"again": {"messages": [msg]}}) == []  # the same message is not counted twice
    plain = EventMapper(Clock()).feed((), "updates", {"model": {"messages": [AIMessage(content="no usage info", id="a2")]}})
    assert types(plain) == ["assistant_message"]  # models that report nothing simply add no event
    calls = EventMapper(Clock()).feed(("tools:x",), "updates", {"model": {"messages": [AIMessage(content="", id="a3", usage_metadata={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
                                                                                                 tool_calls=[{"name": "ls", "id": "c1", "type": "tool_call", "args": {}}])]}})
    assert types(calls) == ["usage", "tool_call"] and calls[0].payload["reasoning_tokens"] is None


def test_a_helper_keeps_its_name_in_a_later_segment_and_after_a_restart():
    first = EventMapper(Clock())
    task = AIMessage(content="", id="a1", tool_calls=[{"name": "task", "id": "t1", "type": "tool_call", "args": {"description": "d", "subagent_type": "mailer"}}])
    first.feed((), "updates", {"model": {"messages": [task]}})
    said = first.feed(("tools:abc",), "updates", {"model": {"messages": [AIMessage(content="drafting", id="s1")]}})
    assert said[0].payload["agent"] == "mailer" and said[0].payload["ns"] == "tools:abc"
    main = first.feed((), "updates", {"model": {"messages": [AIMessage(content="main speaking", id="m1")]}})
    assert "ns" not in main[0].payload  # the main agent has no namespace
    log = [{"type": e.type, "payload": e.payload} for e in [*said, *main]]
    # a brand-new mapper (resume after an approval, possibly in another process) learns the name from the log
    later = EventMapper(Clock()).prime(log)
    after = later.feed(("tools:abc",), "updates", {"model": {"messages": [AIMessage(content="sent it", id="s2")]}})
    assert after[0].payload["agent"] == "mailer"
    unprimed = EventMapper(Clock()).feed(("tools:abc",), "updates", {"model": {"messages": [AIMessage(content="sent it", id="s2")]}})
    assert unprimed[0].payload["agent"] == "helper"  # without the log the name would be lost


def test_usage_names_the_model_that_made_the_call_so_helpers_on_other_models_are_visible():
    msg = AIMessage(content="ok", id="u1", usage_metadata={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}, response_metadata={"model_name": "gemini-3.5-flash"})
    out = EventMapper(Clock()).feed(("tools:abc",), "updates", {"model": {"messages": [msg]}})
    assert out[0].type == "usage" and out[0].payload["model"] == "gemini-3.5-flash"


def test_delegate_names_its_helper_exactly_like_the_library_task_tool_does():
    for tool_name in ("task", "delegate"):
        m = EventMapper(Clock())
        call = AIMessage(content="", id="a1", tool_calls=[{"name": tool_name, "id": "t1", "type": "tool_call",
                                                           "args": {"description": "d", "subagent_type": "researcher", "model": "gemini-2.5-flash-lite"}}])
        m.feed((), "updates", {"model": {"messages": [call]}})
        said = m.feed(("tools:abc",), "updates", {"model": {"messages": [AIMessage(content="found it", id="s1")]}})
        assert [e.payload["agent"] for e in said if e.type == "assistant_message"] == ["researcher"], tool_name
