"""The sandbox is created the first time a tool needs it: chat-only turns never wait for one. Skills are read from a local, read-only copy."""
from types import SimpleNamespace

import pytest

from pinboard.agent.backends import ReadOnlyFilesystemBackend, with_local_skills
from pinboard.gcp.sandbox import SandboxManager, probe_sandbox
from pinboard.gcp.skills import iter_gcs, local_skills_dir  # noqa: F401
from pinboard.core.guardrails import CommandGuard

from helpers import call, make_settings, say
from turn_harness import Farm, Hub


def hub(tmp_path, monkeypatch, scripts, **kw):
    h = Hub("memory", tmp_path, monkeypatch, scripts, **kw)
    h.new_process()
    return h


def skills_copy(tmp_path):
    """A throwaway copy of the example skills. Tests that let an agent poke at skills must NEVER point at the real folder: if the
    read-only protection ever regressed, the agent would rewrite the repository."""
    import shutil

    dest = tmp_path / "skills-src"
    shutil.copytree("skills_example", dest, ignore=shutil.ignore_patterns("__pycache__"))
    return str(dest)


def write(h, sb, name, cid="c1"):
    return call("write_file", {"file_path": f"{h.sbdir(sb)}/{name}", "content": "data"}, cid)


# ── when it is created ───────────────────────────────────────────────────

def test_a_chat_only_turn_never_creates_a_sandbox(tmp_path, monkeypatch):
    h = hub(tmp_path, monkeypatch, [[say("Hello! How can I help?")]])
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "hi")
    assert run.status == "completed" and h.builds == [{"adopt": None, "lazy": True}]
    assert h.farm.created == [] and h.farm.create_calls == [] and h.svc.store.get_session(s.id).sandbox_name is None
    assert h.svc.list_artifacts() == []
    h.close()


def test_the_first_tool_that_needs_a_sandbox_creates_exactly_one_and_later_turns_attach_to_it(tmp_path, monkeypatch):
    h = hub(tmp_path, monkeypatch, [[say("x")]])
    h.scripts[:] = [[write(h, "sb-1", "a.txt", "c1"), write(h, "sb-1", "b.txt", "c2"), say("two files")], [call("ls", {"path": str(h.sbdir("sb-1"))}), say("looked")]]
    s = h.svc.create_session()
    h.svc.send_message(s.id, "make files")
    assert h.farm.created == ["sb-1"] and (h.sbdir("sb-1") / "a.txt").exists() and (h.sbdir("sb-1") / "b.txt").exists()  # two tool calls, one sandbox
    assert h.farm.create_calls[0]["ttl"] == "3600s" and h.farm.create_calls[0]["display_name"] == "pinboard-agent"
    sess = h.svc.store.get_session(s.id)
    assert sess.sandbox_name == "sb-1" and sess.sandbox_expires_at == h.clock() + __import__("datetime").timedelta(seconds=3600)
    h.svc.send_message(s.id, "look around")
    assert h.farm.created == ["sb-1"] and h.builds[1] == {"adopt": "sb-1", "lazy": True}  # attached; nothing was created
    seen = " ".join(str(m.content) for msgs in h.models[1].seen for m in msgs if getattr(m, "type", "") == "tool")
    assert "a.txt" in seen  # the second turn really is looking at the first turn's files
    h.close()


def test_a_sandbox_that_cannot_be_created_is_reported_to_the_agent_once_not_retried_for_minutes(tmp_path, monkeypatch):
    h = hub(tmp_path, monkeypatch, [[say("x")]])
    h.farm.fail_create = RuntimeError("quota exceeded")
    h.scripts[:] = [[write(h, "sb-1", "a.txt", "c1"), write(h, "sb-1", "b.txt", "c2"), say("I could not use the sandbox")], [write(h, "sb-1", "a.txt"), say("worked now")]]
    s = h.svc.create_session()
    run = h.svc.send_message(s.id, "make files")
    assert run.status == "completed" and run.output == "I could not use the sandbox"  # the turn survives: the agent can explain
    tool_texts = [str(m.content) for msgs in h.models[0].seen for m in msgs if getattr(m, "type", "") == "tool"]
    assert any("could not create the sandbox" in t and "quota exceeded" in t for t in tool_texts)
    assert len(h.farm.create_calls) == 1 and h.farm.created == [] and h.svc.store.get_session(s.id).sandbox_name is None  # one attempt per turn
    h.farm.fail_create = None
    assert h.svc.send_message(s.id, "try again").status == "completed" and h.farm.created == ["sb-1"]  # the next turn tries afresh
    h.close()


def test_a_new_sandbox_that_fails_to_set_up_is_deleted_not_leaked(tmp_path):
    farm = Farm(tmp_path / "farm")
    (tmp_path / "ws").mkdir()
    (tmp_path / "farm" / "sb-1").mkdir(parents=True)
    settings = make_settings(tmp_path / "ws", sandbox_name=None, sandbox_runtime="rt-1")
    mgr = SandboxManager(settings, guard=CommandGuard("allowlist", settings.sandbox_cwd), client=farm)

    def broken_setup(sb, name):
        raise RuntimeError("skills sync failed")

    sb = mgr.open_lazy(on_created=broken_setup)
    assert not sb.is_open and farm.created == []  # nothing yet
    first = sb.execute("echo hi")
    assert first.exit_code == 1 and "skills sync failed" in first.output
    assert farm.created == ["sb-1"] and farm.deleted == ["sb-1"] and not sb.is_open  # created, found unusable, deleted
    second = sb.execute("echo hi")
    assert "could not create the sandbox" in second.output and len(farm.create_calls) == 1  # and not attempted again


def test_attaching_to_a_known_sandbox_binds_at_once_without_creating_or_calling_the_setup_hook(tmp_path):
    farm = Farm(tmp_path)
    farm.alive.add("sb-9")
    (tmp_path / "sb-9").mkdir()
    settings = make_settings(tmp_path / "ws", sandbox_name=None, sandbox_runtime="rt-1")
    called = []
    mgr = SandboxManager(settings, client=farm, adopt="sb-9")
    sb = mgr.open_lazy(on_created=lambda *a: called.append(a))
    assert sb.is_open and sb.id == "sb-9" and farm.created == [] and called == []
    assert mgr.detach() == "sb-9"  # a session turn hands the sandbox back without deleting it
    mgr.close()
    assert farm.deleted == []


# ── skills: read locally, run in the sandbox ─────────────────────────────

def test_skills_can_be_listed_and_read_without_a_sandbox_and_run_once_one_exists(tmp_path, monkeypatch):
    h = hub(tmp_path, monkeypatch, [[say("x")]], settings_over={"skills_source": skills_copy(tmp_path)})
    skills = f"{h.sbdir('sb-1')}/skills"
    h.scripts[:] = [[call("read_file", {"file_path": f"{skills}/market-brief/SKILL.md"}), call("ls", {"path": skills}), say("I read the skill")],
                    [call("execute", {"command": f"ls {skills}/market-brief/scripts"}), say("ran")]]
    s = h.svc.create_session()
    h.svc.send_message(s.id, "what can you do?")
    assert h.farm.created == []  # reading skills did not create a sandbox
    first_tools = " ".join(str(m.content) for msgs in h.models[0].seen for m in msgs if getattr(m, "type", "") == "tool")
    assert "Market brief" in first_tools and "market-brief" in first_tools
    system = [str(m.content) for m in h.models[0].seen[0] if getattr(m, "type", "") == "system"]
    assert any("market-brief" in t for t in system)  # the skill is advertised to the model in the prompt
    h.svc.send_message(s.id, "run it")
    assert h.farm.created == ["sb-1"]
    third = " ".join(str(m.content) for msgs in h.models[1].seen for m in msgs if getattr(m, "type", "") == "tool")
    assert "render_brief.py" in third  # the skills were copied INTO the new sandbox, so their scripts can run there
    assert (h.sbdir("sb-1") / "skills" / "market-brief" / "scripts" / "render_brief.py").exists()
    h.close()


def test_the_agent_cannot_change_the_shared_skills_copy(tmp_path, monkeypatch):
    import hashlib
    import pathlib

    def fingerprint(root):
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(pathlib.Path(root).rglob("*")) if p.is_file() and "__pycache__" not in p.parts}

    src = skills_copy(tmp_path)
    before, repo_before = fingerprint(src), fingerprint("skills_example")
    h = hub(tmp_path, monkeypatch, [[say("x")]], settings_over={"skills_source": src})
    skills = f"{h.sbdir('sb-1')}/skills"
    h.scripts[:] = [[call("write_file", {"file_path": f"{skills}/market-brief/SKILL.md", "content": "PWNED"}, "c1"),
                     call("edit_file", {"file_path": f"{skills}/market-brief/SKILL.md", "old_string": "Market", "new_string": "Evil"}, "c2"),
                     call("write_file", {"file_path": f"{skills}/evil/SKILL.md", "content": "---\nname: evil\n---"}, "c3"), say("tried")]]
    s = h.svc.create_session()
    h.svc.send_message(s.id, "tamper")
    results = {m.tool_call_id: str(m.content) for msgs in h.models[0].seen for m in msgs if getattr(m, "type", "") == "tool"}
    assert all("read-only" in results[c] for c in ("c1", "c2", "c3")), results
    assert fingerprint(src) == before and not (pathlib.Path(src) / "evil").exists()  # the shared copy is untouched ...
    assert fingerprint("skills_example") == repo_before  # ... and so is the repository
    h.close()


def test_the_read_only_backend_refuses_every_kind_of_change_but_serves_reads(tmp_path):
    (tmp_path / "s" / "demo").mkdir(parents=True)
    (tmp_path / "s" / "demo" / "SKILL.md").write_text("hello skill")
    b = ReadOnlyFilesystemBackend(root_dir=tmp_path / "s", virtual_mode=True)
    assert "hello skill" in str(b.read("/demo/SKILL.md"))
    assert b.write("/demo/new.md", "x").error and b.edit("/demo/SKILL.md", "hello", "bye").error and b.delete("/demo/SKILL.md").error
    assert b.upload_files([("/demo/z.md", b"z")])[0].error == "permission_denied"
    assert (tmp_path / "s" / "demo" / "SKILL.md").read_text() == "hello skill" and not (tmp_path / "s" / "demo" / "new.md").exists()


def test_only_the_skills_folder_is_routed_to_the_local_copy(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "a.md").write_text("from skills")

    class Default:
        def ls(self, path):
            from deepagents.backends.protocol import LsResult

            return LsResult(entries=[{"path": f"{path}/FROM-SANDBOX", "is_dir": False}])

    comp = with_local_skills(Default(), "/workspace/skills", tmp_path / "s")
    assert "from skills" in str(comp.read("/workspace/skills/a.md"))
    assert "FROM-SANDBOX" in repr(comp.ls("/workspace/elsewhere"))  # everything outside the skills folder still goes to the sandbox
    assert "FROM-SANDBOX" not in repr(comp.ls("/workspace/skills"))


def test_gcs_skills_are_copied_locally_once_and_refreshed_after_the_ttl(tmp_path):
    class Blob:
        def __init__(self, name, data):
            self.name, self.size, self._d = name, len(data), data

        def download_as_bytes(self):
            return self._d

    class Client:
        calls = 0

        def list_blobs(self, bucket, prefix=""):
            Client.calls += 1
            return [Blob(prefix + "demo/SKILL.md", b"v%d" % Client.calls), Blob(prefix + "../escape.md", b"x"), Blob(prefix + "demo/", b"")]

    now = [1000.0]
    one = local_skills_dir("gs://bkt/skills", Client(), ttl=300, clock=lambda: now[0])
    assert (one / "demo" / "SKILL.md").read_bytes() == b"v1" and not (one.parent / "escape.md").exists()
    assert local_skills_dir("gs://bkt/skills", Client(), ttl=300, clock=lambda: now[0]) == one and Client.calls == 1  # served from the copy
    now[0] += 301
    two = local_skills_dir("gs://bkt/skills", Client(), ttl=300, clock=lambda: now[0])
    assert two != one and (two / "demo" / "SKILL.md").read_bytes() == b"v2" and not one.exists()  # refreshed; the stale copy is removed
    assert local_skills_dir("skills_example").name == "skills_example"
    with pytest.raises(FileNotFoundError):
        local_skills_dir(str(tmp_path / "nope"))


# ── is a recorded sandbox still alive? ───────────────────────────────────

def make_client(*outcomes):
    queue = list(outcomes)

    def execute_bash(**kw):
        o = queue.pop(0)
        if isinstance(o, Exception):
            raise o
        return {"returncode": o}

    return SimpleNamespace(sandboxes=SimpleNamespace(execute_bash=execute_bash))


def test_the_probe_says_alive_for_a_sandbox_that_answers_and_dead_after_two_failures():
    s = make_settings("/tmp/x", gcp_project="p")
    assert probe_sandbox(s, "sb", make_client(0)) is True
    assert probe_sandbox(s, "sb", make_client(RuntimeError("blip"), 0)) is True  # one hiccup is forgiven
    assert probe_sandbox(s, "sb", make_client(RuntimeError("gone"), RuntimeError("gone"))) is False
    assert probe_sandbox(s, "sb", make_client(1, 1)) is False and probe_sandbox(s, "sb", make_client(1, 0)) is True


def test_a_probe_that_cannot_even_reach_the_platform_reports_dead(monkeypatch):
    import sys

    class Boom:
        @staticmethod
        def Client(**kw):
            raise RuntimeError("no credentials")

    monkeypatch.setitem(sys.modules, "agentplatform", Boom)
    assert probe_sandbox(make_settings("/tmp/x", gcp_project="p"), "sb") is False
