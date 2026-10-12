"""The plumbing of scripts/verify_real.py (following a turn, answering approvals, reporting), run against the fake harness.
The scenarios themselves need the real services; this only makes sure the script does not fail because of a bug of its own."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


from helpers import call, say
from turn_harness import Hub

SPEC = importlib.util.spec_from_file_location("verify_real", Path(__file__).resolve().parents[2] / "scripts" / "verify_real.py")
vr = importlib.util.module_from_spec(SPEC)
sys.modules["verify_real"] = vr  # dataclasses look the module up by name
SPEC.loader.exec_module(vr)


def ctx_for(tmp_path, monkeypatch, scripts, **kw):
    h = Hub("memory", tmp_path, monkeypatch, scripts, **kw)
    svc = h.new_process()
    return h, vr.Ctx(svc, vr.Reporter(), h.farm.delete, lambda: h.new_process(), SimpleNamespace(github=None))


def statuses(ctx, scenario=None):
    return [(r["status"], r["what"]) for r in ctx.rep.rows if scenario is None or r["scenario"] == scenario]


def test_a_chat_only_scenario_runs_end_to_end_on_the_harness(tmp_path, monkeypatch):
    h, ctx = ctx_for(tmp_path, monkeypatch, [[say("Hello there, friend!")]])
    ctx.rep.scenario = "S1"
    vr.s1_chat_only(ctx)
    rows = statuses(ctx)
    assert not [r for r in rows if r[0] == "FAIL"], rows
    assert ("PASS", "no sandbox was created for a chat-only turn") in rows and ("PASS", "an answer was produced") in rows
    assert ("INFO", "live text batches") in rows  # a one-sentence answer is too short to stream: noted, not warned about
    h.close()


def test_the_streaming_scenario_runs_on_the_harness_and_reports_a_non_streaming_model_as_a_warning(tmp_path, monkeypatch):
    h, ctx = ctx_for(tmp_path, monkeypatch, [[say("A long answer. " * 20)]])
    ctx.rep.scenario = "S12"
    vr.s12_streaming(ctx)
    rows = statuses(ctx)
    assert not [r for r in rows if r[0] == "FAIL"], rows
    assert ("PASS", "the stored log keeps the final message, not the live batches") in rows
    assert any(st == "WARN" and "several live batches" in w for st, w in rows)  # a scripted model never streams: reported, not failed
    h.close()


def test_the_web_search_scenario_checks_results_labelling_and_a_cited_source(tmp_path, monkeypatch):
    from langchain_core.tools import tool

    from pinboard.core.guardrails import wrap_untrusted

    @tool
    def web_search(query: str) -> str:
        """Fake search."""
        return wrap_untrusted("[1] JWST news\n    https://example.com/jwst\n    A fact.", "web_search")

    h, ctx = ctx_for(tmp_path, monkeypatch, [[call("web_search", {"query": "jwst"}), say("JWST did a thing: https://example.com/jwst")]], tools=[web_search])
    ctx.rep.scenario = "S13"
    vr.s13_web_search(ctx)
    rows = statuses(ctx)
    assert not [r for r in rows if r[0] in ("FAIL", "WARN")], rows
    assert ("PASS", "the results were labelled as untrusted data") in rows and ("PASS", "the answer cites a source URL") in [(a, b) for a, b in rows]
    h.close()
    # and a search that failed must be a FAIL, not a quiet pass
    @tool
    def web_search(query: str) -> str:  # noqa: F811
        """Fake failing search."""
        return "Search failed: 401 Unauthorized"

    (tmp_path / "second").mkdir()
    h2, ctx2 = ctx_for(tmp_path / "second", monkeypatch, [[call("web_search", {"query": "x"}), say("no luck")]], tools=[web_search])
    ctx2.rep.scenario = "S13"
    vr.s13_web_search(ctx2)
    assert any(r["status"] == "FAIL" and "Tavily answered" in r["what"] for r in ctx2.rep.rows)
    h2.close()


def test_drive_answers_approvals_and_returns_the_story(tmp_path, monkeypatch, capsys):
    h, ctx = ctx_for(tmp_path, monkeypatch, [[call("execute", {"command": "echo verified > check.txt && cat check.txt"}), say("x")], [say("it printed verified")]], approval_mode="execute")
    s = ctx.session("t")
    turn, events, stats = vr.drive(ctx, s.id, "run something")
    assert turn.status == "completed" and stats["approvals"] == ["shell"] and stats["seconds"] >= 0
    assert vr.tool_names(events) == ["execute"] and "verified" in vr.results_text(events) and "verified" in vr.say_text(events)
    out = capsys.readouterr().out
    assert "call  [assistant] execute(" in out and "WAIT  approval needed: shell" in out and "say   [assistant] it printed verified" in out
    h.close()


def test_a_question_is_answered_with_the_scenario_answer(tmp_path, monkeypatch):
    h, ctx = ctx_for(tmp_path, monkeypatch, [[call("ask_user", {"question": "Which colour?"}), say("x")], [say("noted")]])
    s = ctx.session("t")
    turn, events, stats = vr.drive(ctx, s.id, "ask me")
    assert turn.status == "completed" and stats["approvals"] == ["question"]
    seen = [m.content for msgs in h.models[-1].seen for m in msgs if getattr(m, "type", "") == "tool"]
    assert "blue" in seen
    h.close()


def test_a_model_that_keeps_asking_for_approval_is_stopped(tmp_path, monkeypatch):
    # every resumed segment asks again (the harness reuses one script): the script must not loop forever
    h, ctx = ctx_for(tmp_path, monkeypatch, [[call("execute", {"command": "echo again"}), say("x")]], approval_mode="execute")
    s = ctx.session("t")
    turn, events, stats = vr.drive(ctx, s.id, "loop")
    assert stats.get("approval_cap") is True and len(stats["approvals"]) == vr.MAX_APPROVALS_PER_TURN + 1
    assert turn.status in ("cancelled", "completed", "failed")
    h.close()


def test_a_cancel_request_from_the_script_is_honoured(tmp_path, monkeypatch):
    from turn_harness import Gate

    gate = Gate(first_call_only=True)
    h, ctx = ctx_for(tmp_path, monkeypatch, [[call("block", {}), call("block", {}, "c2"), say("not reached")]], tools=[gate.tool()])
    import pinboard.engine.service as service_mod

    monkeypatch.setattr(service_mod, "_CANCEL_POLL_SECONDS", 0)
    h.executor = __import__("pinboard.engine.worker", fromlist=["ThreadExecutor"]).ThreadExecutor(2)
    h.svc.executor = h.executor
    s = ctx.session("t")
    import threading

    threading.Timer(0.4, gate.release).start()
    turn, events, stats = vr.drive(ctx, s.id, "go", cancel_when=lambda e, evs: e["type"] == "tool_call")
    assert stats["cancel_sent"] and turn.status in ("cancelled", "completed")
    h.close()


def test_the_restart_scenario_works_with_a_fresh_service_object(tmp_path, monkeypatch):
    h, ctx = ctx_for(tmp_path, monkeypatch, [[call("execute", {"command": "echo restart-test"}), say("x")], [say("The output was restart-test")]], approval_mode="execute")
    ctx.rep.scenario = "S8"
    vr.s8_restart(ctx)
    rows = statuses(ctx)
    assert not [r for r in rows if r[0] == "FAIL"], rows
    assert ("PASS", "the real model finished the task from the checkpoint") in [(s, w) for s, w in rows] or any("checkpoint" in w and s == "PASS" for s, w in rows)
    h.close()


def test_scenarios_that_need_an_earlier_one_say_so_instead_of_crashing(tmp_path, monkeypatch):
    h, ctx = ctx_for(tmp_path, monkeypatch, [[say("x")]])
    code = vr.run_scenarios(ctx, ["S3", "S4", "S11"])
    assert code == 0 and not [r for r in ctx.rep.rows if r["status"] == "FAIL"]
    assert any("skipped" in r["what"] for r in ctx.rep.rows)
    h.close()


def test_a_crashing_scenario_is_reported_as_a_failure_not_a_traceback(tmp_path, monkeypatch):
    h, ctx = ctx_for(tmp_path, monkeypatch, [[say("x")]])
    monkeypatch.setitem(vr.SCENARIOS, "S1", ("boom", lambda c: 1 / 0))
    assert vr.run_scenarios(ctx, ["S1"]) == 1
    assert any(r["status"] == "FAIL" and "crashed" in r["what"] and "ZeroDivisionError" in r["detail"] for r in ctx.rep.rows)
    h.close()


def test_event_lines_and_helpers():
    ev = lambda t, p, seq=1, ts="2026-10-08T10:00:00+00:00": {"seq": seq, "type": t, "payload": p, "ts": ts}  # noqa: E731
    assert "usage [assistant] in=10 out=2 thinking=1 cached=None" in vr.describe_event(ev("usage", {"agent": "assistant", "input_tokens": 10, "output_tokens": 2, "reasoning_tokens": 1}))
    assert vr.describe_event(ev("token", {"text": "x"})) is None
    assert "say   [researcher] hello" in vr.describe_event(ev("assistant_message", {"text": "hello", "agent": "researcher"}))
    assert "result[assistant] execute success: ok" in vr.describe_event(ev("tool_result", {"name": "execute", "status": "success", "preview": "ok", "agent": "assistant"}))
    assert "file  note.md (12 bytes)" in vr.describe_event(ev("artifact", {"path": "note.md", "size": 12}))
    assert "WAIT  approval needed: plan" in vr.describe_event(ev("interrupt", {"items": [{"kind": "plan", "title": "Approve the plan"}]}))
    assert vr.describe_event(ev("heartbeat", {})) is None
    events = [ev("turn_started", {}, 1, "2026-10-08T10:00:00+00:00"), ev("token", {}, 2, "2026-10-08T10:00:02.500+00:00")]
    assert vr.seconds_between(events, lambda e: e["type"] == "turn_started", lambda e: e["type"] == "token") == 2.5
    assert vr.seconds_between(events, lambda e: e["type"] == "nope", lambda e: True) is None
    assert vr._short("x" * 300, 20).endswith("…") and len(vr._short("x" * 300, 20)) == 20


def test_the_reporter_counts_and_warnings_do_not_fail_the_run(capsys):
    r = vr.Reporter()
    r.check(True, "ok")
    r.expect(False, "the model chose differently")
    r.info("note")
    assert r.summary() == 0
    r.check(False, "something broke")
    assert r.summary() == 1
    assert "1 FAILED" in capsys.readouterr().out


def test_list_and_dry_run_do_not_call_anything(tmp_path, monkeypatch, capsys):
    assert vr.main(["--list"]) == 0 and "S11" in capsys.readouterr().out
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINBOARD_POLICY", str(Path(__file__).resolve().parents[2] / "config" / "policy.yaml"))
    monkeypatch.setenv("GCP_PROJECT", "some-project")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-not-real")
    monkeypatch.setenv("GOOGLE_USE_VERTEX", "true")
    monkeypatch.setenv("SANDBOX_RUNTIME", "projects/p/locations/l/reasoningEngines/1")
    assert vr.main(["--dry-run", "--only", "S1,S2"]) == 0
    out = capsys.readouterr().out
    assert "project=some-project" in out and "would run: S1, S2" in out and "state=memory" in out
    assert vr.main(["--only", "S99"]) == 2
