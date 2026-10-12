import os

import pytest

from pinboard.core.guardrails import CommandGuard
from pinboard.gcp.sandbox import GCPShellSandbox
from pinboard.gcp.skills import _safe_rel, parse_gcs_uri, sync_skills, upload_skills_dir

from helpers import FakeClient


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def sb(ws):
    return GCPShellSandbox(FakeClient(ws), "fake", guard=CommandGuard("allowlist", str(ws)), cwd=str(ws))


def test_upload_download_roundtrip(sb, ws):
    small, big = b"hello \xf0\x9f\x98\x80 world", os.urandom(70_000)  # big forces chunking
    res = sb.upload_files([(f"{ws}/a/b/small.bin", small), (f"{ws}/big.bin", big)])
    assert [r.error for r in res] == [None, None]
    d = sb.download_files([f"{ws}/a/b/small.bin", f"{ws}/big.bin"])
    assert d[0].content == small and d[1].content == big


def test_download_errors(sb, ws):
    (ws / "dir").mkdir()
    r = sb.download_files([f"{ws}/missing.txt", f"{ws}/dir", "relative/path", f"{ws}/../etc/passwd"])
    assert [x.error for x in r] == ["file_not_found", "is_directory", "invalid_path", "invalid_path"]


def test_upload_rejects_bad_paths(sb):
    r = sb.upload_files([("relative.txt", b"x"), ("/tmp/../etc/x", b"x")])
    assert [x.error for x in r] == ["invalid_path", "invalid_path"]


def test_basesandbox_file_ops_work_through_unguarded_execute(sb, ws):
    # BaseSandbox's own ls/read/write/edit/grep use python3 heredocs via execute(); they must not be blocked
    assert sb.write(f"{ws}/notes.txt", "alpha\nbeta\n").error is None
    assert "alpha" in sb.read(f"{ws}/notes.txt").file_data["content"]
    assert sb.edit(f"{ws}/notes.txt", "beta", "gamma").error is None
    names = [e["path"] for e in sb.ls(str(ws)).entries]
    assert any(n.endswith("notes.txt") for n in names)
    assert sb.grep("gamma", str(ws)).matches


def test_guard_applies_to_agent_shell_path_only(sb):
    blocked = sb.execute_with_offload("sudo id", "/tmp/cap", max_inline_bytes=10_000)
    assert blocked.response.exit_code == 126 and "Blocked by security policy" in blocked.response.output
    ok = sb.execute_with_offload("echo hi", "/tmp/cap", max_inline_bytes=10_000)
    assert ok.response.exit_code == 0 and "hi" in ok.response.output


@pytest.mark.asyncio
async def test_guard_applies_to_async_path(sb):
    blocked = await sb.aexecute_with_offload("env", "/tmp/cap", max_inline_bytes=10_000)
    assert blocked.response.exit_code == 126


def test_output_redaction(ws):
    sb = GCPShellSandbox(FakeClient(ws), "f", cwd=str(ws), redact=lambda t: t.replace("topsecret123", "[REDACTED]"))
    assert "topsecret123" not in sb.execute("echo topsecret123").output


def test_transport_error_becomes_error_response(ws):
    class Boom:
        class sandboxes:
            @staticmethod
            def execute_bash(**kw):
                raise RuntimeError("network down")

    r = GCPShellSandbox(Boom(), "x", cwd=str(ws)).execute("ls")
    assert r.exit_code == 1 and "network down" in r.output


# ── skills ──
def test_parse_gcs_uri():
    assert parse_gcs_uri("gs://b/skills") == ("b", "skills/")
    assert parse_gcs_uri("gs://b") == ("b", "")
    with pytest.raises(ValueError):
        parse_gcs_uri("s3://b/x")


def test_safe_rel():
    assert _safe_rel("a/b.md") == "a/b.md"
    for bad in ["../x", "a/../../x", "..", "", "."]:
        assert _safe_rel(bad) is None
    assert _safe_rel("/abs/x") == "abs/x"


def test_sync_local_skills_into_sandbox(sb, ws):
    rep = sync_skills(sb, "skills_example", f"{ws}/skills")
    assert rep.skills == ["market-brief"] and rep.files >= 2 and not rep.skipped
    assert (ws / "skills/market-brief/SKILL.md").read_text().startswith("---")
    assert (ws / "skills/market-brief/scripts/render_brief.py").exists()


class _Blob:
    def __init__(self, name, data):
        self.name, self._d, self.size = name, data, len(data)

    def download_as_bytes(self):
        return self._d


class _Bucket:
    def __init__(self, store):
        self.store = store

    def blob(self, name):
        store = self.store

        class B:
            def upload_from_string(self, data):
                store[name] = data

        return B()


class FakeStorage:
    def __init__(self, blobs=None):
        self.store = dict(blobs or {})

    def list_blobs(self, bucket, prefix=""):
        return [_Blob(n, d) for n, d in self.store.items() if n.startswith(prefix)]

    def bucket(self, name):
        return _Bucket(self.store)


def test_sync_from_gcs_skips_unsafe_and_directories(sb, ws):
    st = FakeStorage({
        "skills/": b"",
        "skills/demo/SKILL.md": b"---\nname: demo\n---\n",
        "skills/demo/run.py": b"print(1)\n",
        "skills/../evil.txt": b"x",
    })
    rep = sync_skills(sb, "gs://bkt/skills", f"{ws}/skills", storage_client=st)
    assert rep.skills == ["demo"]
    assert any("unsafe" in s for s in rep.skipped)
    assert (ws / "skills/demo/run.py").exists() and not (ws / "evil.txt").exists()


def test_upload_skills_dir_to_gcs():
    st = FakeStorage()
    n = upload_skills_dir("skills_example", "gs://bkt/skills", storage_client=st)
    assert n >= 2 and "skills/market-brief/SKILL.md" in st.store
