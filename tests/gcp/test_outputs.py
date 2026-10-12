
import pytest

from pinboard.core.envfile import persist_env_value
from pinboard.core.guardrails import CommandGuard
from pinboard.gcp.outputs import collect_outputs, harvest, save_gcs, save_local
from pinboard.gcp.sandbox import GCPShellSandbox

from helpers import FakeClient, make_settings
from test_sandbox_skills import FakeStorage


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    (ws / "out" / "sub").mkdir(parents=True)
    sb = GCPShellSandbox(FakeClient(ws), "fake", guard=CommandGuard("allowlist", str(ws)), cwd=str(ws))
    settings = make_settings(ws, output_dir=str(tmp_path / "local_out"))
    return ws, sb, settings, tmp_path


def test_collect_finds_nested_files(env):
    ws, sb, settings, _ = env
    (ws / "out" / "brief.md").write_text("# hi")
    (ws / "out" / "sub" / "data.json").write_bytes(b'{"a": 1}')
    files, skipped = collect_outputs(sb, settings.outputs_path)
    assert dict(files) == {"brief.md": b"# hi", "sub/data.json": b'{"a": 1}'} and not skipped


def test_collect_handles_missing_dir_and_weird_filenames(env):
    ws, sb, settings, _ = env
    assert collect_outputs(sb, str(ws / "nope"))[0] == []
    (ws / "out" / "it's a & file.txt").write_text("x")  # spaces, quote, ampersand
    assert dict(collect_outputs(sb, settings.outputs_path)[0]) == {"it's a & file.txt": b"x"}


def test_collect_respects_limits(env):
    ws, sb, settings, _ = env
    for i in range(5):
        (ws / "out" / f"f{i}.txt").write_text("x" * 10)
    files, skipped = collect_outputs(sb, settings.outputs_path, max_files=2)
    assert len(files) == 2 and len(skipped) == 3
    files, skipped = collect_outputs(sb, settings.outputs_path, max_total=25)
    assert len(files) == 2 and any("size limit" in s for s in skipped)


def test_save_local_never_escapes_destination(tmp_path):
    saved = save_local([("ok/a.md", b"1"), ("../../evil.md", b"2")], tmp_path / "d", "run1")
    assert [p.name for p in saved] == ["a.md"] and not (tmp_path / "evil.md").exists()


def test_save_gcs_keys():
    st = FakeStorage()
    urls = save_gcs([("brief.md", b"x")], "gs://bkt/outputs", "run9", storage_client=st)
    assert urls == ["gs://bkt/outputs/run9/brief.md"] and st.store["outputs/run9/brief.md"] == b"x"


def test_harvest_local_and_gcs(env):
    ws, sb, settings, tmp = env
    (ws / "out" / "brief.md").write_text("report")
    st = FakeStorage()
    s2 = make_settings(ws, output_dir=str(tmp / "local_out"), outputs_gcs_uri="gs://b/o")
    saved, skipped = harvest(sb, s2, "r1", storage_client=st)
    assert (tmp / "local_out" / "r1" / "brief.md").read_text() == "report"
    assert "gs://b/o/r1/brief.md" in saved and not skipped


def test_harvest_with_nothing_to_save(env):
    _, sb, settings, _ = env
    assert harvest(sb, settings, "r2") == ([], [])


# ── .env persistence ──
def test_persist_fills_empty_value(tmp_path):
    f = tmp_path / ".env"
    f.write_text("A=1\nSANDBOX_RUNTIME=\nB=2\n")
    assert persist_env_value(f, "SANDBOX_RUNTIME", "projects/x/rt/1") is True
    assert f.read_text() == "A=1\nSANDBOX_RUNTIME=projects/x/rt/1\nB=2\n"


def test_persist_never_overwrites_and_appends_when_absent(tmp_path):
    f = tmp_path / ".env"
    f.write_text("SANDBOX_RUNTIME=keep-me\n")
    assert persist_env_value(f, "SANDBOX_RUNTIME", "new") is False and "keep-me" in f.read_text()
    g = tmp_path / "other.env"
    g.write_text("A=1\n")
    assert persist_env_value(g, "SANDBOX_RUNTIME", "v") is True and g.read_text().endswith("SANDBOX_RUNTIME=v\n")


def test_persist_missing_file_is_noop(tmp_path):
    assert persist_env_value(tmp_path / "none.env", "K", "v") is False


# ── guard hint ──
def test_parse_block_tells_the_model_how_to_recover():
    r = CommandGuard().check("echo '{\"t\":\"Nvidia's rise\",\"s\":\"S&P\"}' | python3 x.py")
    assert not r.ok and "write_file" in r.reason
