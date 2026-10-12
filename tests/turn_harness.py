"""Shared test world for session turns: a fake sandbox farm (one directory per sandbox), scripted models, a fake clock, a gate tool that can
block a worker, and a service wired like production (same worker code), minus Google Cloud."""
import copy
import dataclasses
import os
import subprocess
import threading
import time
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.tools import tool

from pinboard.agent import build_agent
from pinboard.gcp.spares import create_spare_sandbox
from pinboard.gcp.vault import Vault
from pinboard.core.policy import parse_policy
from pinboard.engine.service import RunService
from pinboard.state import make_pool, make_state
from pinboard.engine.worker import InlineExecutor

from helpers import ScriptedModel, make_settings
from test_policy import BASE

DSN = os.environ.get("PINBOARD_TEST_DATABASE_URL")
BACKENDS = ["memory"] + (["postgres"] if DSN else [pytest.param("postgres", marks=pytest.mark.skip(reason="PINBOARD_TEST_DATABASE_URL not set"))])


def wait_until(pred, timeout=10.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Farm:
    """Fake agent platform (what ``agentplatform.Client`` offers the sandbox manager): which sandboxes exist, and which were created / deleted.
    Each sandbox is a folder under ``root``; commands run on the host in that folder."""

    def __init__(self, root):
        self.root, self.alive, self.created, self.deleted, self.create_calls = root, set(), [], [], []
        self.fail_create: Exception | None = None
        self.sandboxes = SimpleNamespace(create=self._create, delete=self._delete_call, execute_bash=self._execute)

    def next_name(self):
        return f"sb-{len(self.created) + 1}"

    def delete(self, name):
        self.alive.discard(name)
        self.deleted.append(name)

    def probe(self, name):
        return name in self.alive

    def _create(self, *, name, poll_interval_seconds=None, spec=None, config=None):
        self.create_calls.append(config or {})
        if self.fail_create:
            raise self.fail_create
        n = self.next_name()
        self.created.append(n)
        self.alive.add(n)
        (self.root / n).mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(response=SimpleNamespace(name=n))

    def _delete_call(self, *, name):
        self.delete(name)

    def _execute(self, *, name, command, cwd=None, timeout=None, **_):
        if name not in self.alive:
            raise RuntimeError("400 FAILED_PRECONDITION: Bad Gateway: Unable to reach the sandbox")
        t0 = time.time()
        p = subprocess.run(["bash", "-c", command], cwd=cwd or str(self.root / name), capture_output=True, text=True, timeout=timeout or 60)
        return {"stdout": p.stdout, "stderr": p.stderr, "returncode": p.returncode, "duration_ms": int((time.time() - t0) * 1000)}


class Gate:
    """A tool that blocks the worker until the test releases it (so a turn can be 'in flight' while the test acts)."""

    def __init__(self, first_call_only=False):
        self.entered, self.release_event, self.calls, self.first_only = threading.Event(), threading.Event(), 0, first_call_only

    def release(self):
        self.release_event.set()

    def tool(self):
        gate = self

        @tool
        def block() -> str:
            """Wait until the test lets this continue."""
            gate.calls += 1
            if not gate.first_only or gate.calls == 1:
                gate.entered.set()
                gate.release_event.wait(15)
            return "done waiting"

        return block


def make_policy(**limits):
    d = copy.deepcopy(BASE)
    d["limits"] = {"max_turn_minutes": 10, "max_concurrent_turns": 4, "max_auto_resumes": 1, "sandbox_idle_minutes": 20, **limits}
    return parse_policy(d)


class Hub:
    """One test environment. ``scripts[i]`` is the model script of the i-th agent build (every worker segment builds a fresh agent;
    the conversation itself lives in the shared checkpointer, exactly as in production)."""

    def __init__(self, backend, tmp_path, monkeypatch, scripts, *, approval_mode="none", tools=None, executor=None, policy=None, settings_over=None, **svc_kw):
        self.tmp, self.scripts, self.tools, self.clock = tmp_path, scripts, tools or [], Clock()
        self.root = tmp_path / "sandboxes"
        self.root.mkdir()
        self.farm = Farm(self.root)
        self.backend, self.builds, self.models, self.executor = backend, [], [], executor or InlineExecutor()
        self.settings = make_settings(tmp_path / "ws", sandbox_name=None, sandbox_runtime="rt-1", approval_mode=approval_mode, state_backend=backend,
                                      approval_ttl_seconds=1200, output_dir=str(tmp_path / "outputs"), sandbox_ttl_seconds=3600, **(settings_over or {}))
        (tmp_path / "ws").mkdir()
        self.policy, self.vault, self.svc_kw = policy or make_policy(), Vault("env"), svc_kw
        self._states, self._memory_state = [], None
        if backend == "postgres":
            monkeypatch.setenv("DATABASE_URL", DSN)
            pool = make_pool(DSN)
            with pool.connection() as c:
                c.execute("DROP TABLE IF EXISTS pb_schedule_runs, pb_schedules, pb_mcp_servers, pb_spares, pb_artifacts, pb_events, pb_effects, pb_sessions, pb_audit, pb_approvals, pb_runs CASCADE")
            pool.close()

    def sbdir(self, name):
        return self.root / name

    def new_process(self, **over):
        """A fresh service. With postgres every in-memory object is thrown away (a restart); with memory the same state is shared."""
        if self.backend == "postgres":
            state = make_state(self.settings, Vault("env"))
            self._states.append(state)
        else:
            self._memory_state = self._memory_state or make_state(self.settings, self.vault)
            state = self._memory_state
        self.state = state
        kw = dict(build=self._build, sandbox_deleter=self.farm.delete, clock=self.clock, policy=self.policy, executor=self.executor,
                  ledger=state.ledger, lease_thread=False, sleep=lambda s: time.sleep(0.005), sandbox_probe=self.farm.probe,
                  spare_factory=self._make_spare, spare_runner=lambda fn: fn())  # spares are made immediately, in the calling thread
        kw.update(self.svc_kw)
        kw.update(over)
        self.svc = RunService(self.settings, self.vault, state.store, state.checkpointer, **kw)
        return self.svc

    def _make_spare(self):
        """What the service calls to make a spare: the REAL creation code against the fake platform (its folder is chosen up front, as for builds)."""
        folder = self.sbdir(self.farm.next_name())
        folder.mkdir(parents=True, exist_ok=True)
        return create_spare_sandbox(dataclasses.replace(self.settings, sandbox_cwd=str(folder)), client=self.farm)

    def _build(self, settings, vault, *, checkpointer, adopt_sandbox, on_block, storage_client, artifacts=None, user_id=None, session_id=None,
               lazy_sandbox=False, on_sandbox_created=None, claim_sandbox=None, on_sandbox_claimed=None, mcp=None, **_extras):
        """Builds the REAL agent with the REAL sandbox manager against the fake platform. Each sandbox gets its own working folder:
        the name the platform will hand out next is known in advance (names are sequential), so the folder can be chosen up front.
        When a ready spare exists and the turn may claim one, that spare's folder is the one the agent will end up in."""
        self.builds.append({"adopt": adopt_sandbox, "lazy": lazy_sandbox})
        spare = self.state.store.current_spare(self.clock() - timedelta(hours=1)) if claim_sandbox else None
        usable = (spare and spare.status == "ready" and spare.name in self.farm.alive
                  and spare.expires_at - self.clock() > timedelta(seconds=settings.sandbox_ttl_seconds / 2))  # the service's own rules for a claim
        folder = self.sbdir(adopt_sandbox or (spare.name if usable else None) or self.farm.next_name())
        folder.mkdir(parents=True, exist_ok=True)
        script = self.scripts[min(len(self.builds) - 1, len(self.scripts) - 1)]
        model = ScriptedModel(script=list(script))
        self.models.append(model)
        return build_agent(dataclasses.replace(settings, sandbox_cwd=str(folder)), vault, model=model, tools=list(self.tools), checkpointer=checkpointer,
                           on_block=on_block, adopt_sandbox=adopt_sandbox, client=self.farm, lazy_sandbox=lazy_sandbox,
                           on_sandbox_created=on_sandbox_created, claim_sandbox=claim_sandbox, on_sandbox_claimed=on_sandbox_claimed,
                           artifacts=artifacts, user_id=user_id, session_id=session_id, storage_client=storage_client, mcp=mcp)

    def close(self):
        if hasattr(self.executor, "join"):
            self.executor.join(5)
        for s in self._states:
            s.close()


def kinds(events):
    return [e["type"] for e in events]


def events_of(svc, run_id):
    return svc.store.list_events(run_id, 0, limit=100_000)
