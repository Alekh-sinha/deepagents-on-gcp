import pytest

from pinboard.gcp.sandbox import GCPShellSandbox

NOT_READY = "400 FAILED_PRECONDITION. Bad Gateway: Unable to reach the sandbox environment."


class Flaky:
    """execute_bash fails with the not-ready error N times, then succeeds."""

    def __init__(self, failures, error=NOT_READY):
        self.failures, self.error, self.calls = failures, error, 0
        outer = self

        class sandboxes:
            @staticmethod
            def execute_bash(**kw):
                outer.calls += 1
                if outer.calls <= outer.failures:
                    raise RuntimeError(outer.error)
                return {"stdout": "ready\n", "stderr": "", "returncode": 0, "duration_ms": 1}

        self.sandboxes = sandboxes


def test_waits_through_the_not_ready_window():
    c = Flaky(failures=3)
    GCPShellSandbox(c, "s").wait_until_ready(timeout=5, interval=0.01)
    assert c.calls == 4


def test_ready_immediately_makes_one_call():
    c = Flaky(failures=0)
    GCPShellSandbox(c, "s").wait_until_ready(timeout=5, interval=0.01)
    assert c.calls == 1


def test_other_errors_are_not_retried():
    c = Flaky(failures=99, error="403 PERMISSION_DENIED: caller lacks aiplatform.user")
    with pytest.raises(RuntimeError, match="did not become ready"):
        GCPShellSandbox(c, "s").wait_until_ready(timeout=5, interval=0.01)
    assert c.calls == 1


def test_gives_up_after_timeout():
    c = Flaky(failures=10_000)
    with pytest.raises(RuntimeError, match="did not become ready"):
        GCPShellSandbox(c, "s").wait_until_ready(timeout=0.1, interval=0.02)
    assert c.calls >= 2


def test_normal_execute_is_not_retried():
    c = Flaky(failures=1)
    r = GCPShellSandbox(c, "s").execute("ls")
    assert r.exit_code == 1 and c.calls == 1  # arbitrary commands are never retried


def test_503_unavailable_is_treated_as_not_ready():
    c = Flaky(failures=2, error="503 UNAVAILABLE. {'error': {'code': 503, 'message': 'The service is currently unavailable.'}}")
    GCPShellSandbox(c, "s").wait_until_ready(timeout=5, interval=0.01)
    assert c.calls == 3


# ── manager cleans up after a failed start ──
from types import SimpleNamespace

from pinboard.gcp.sandbox import SandboxManager
from helpers import make_settings


class _MgrClient:
    def __init__(self, exec_error):
        self.deleted, self.created_with = [], {}
        outer = self

        class sandboxes:
            @staticmethod
            def create(**kw):
                outer.created_with = kw
                return SimpleNamespace(response=SimpleNamespace(name="sb-1"))

            @staticmethod
            def execute_bash(**kw):
                raise RuntimeError(exec_error)

            @staticmethod
            def delete(**kw):
                outer.deleted.append(kw["name"])

        self.sandboxes = sandboxes


def test_manager_deletes_sandbox_when_it_never_becomes_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(GCPShellSandbox, "wait_until_ready", lambda self, **kw: (_ for _ in ()).throw(RuntimeError("Sandbox did not become ready")))
    client = _MgrClient("x")
    mgr = SandboxManager(make_settings(tmp_path, sandbox_name=None, sandbox_runtime="rt-1"), client=client)
    with pytest.raises(RuntimeError):
        mgr.open()
    assert client.deleted == ["sb-1"]


def test_manager_passes_slow_poll_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(GCPShellSandbox, "wait_until_ready", lambda self, **kw: None)
    client = _MgrClient("x")
    SandboxManager(make_settings(tmp_path, sandbox_name=None, sandbox_runtime="rt-1"), client=client).open()
    assert client.created_with["poll_interval_seconds"] == 2.0
    assert client.created_with["name"] == "rt-1"
