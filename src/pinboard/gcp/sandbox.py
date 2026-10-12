"""deepagents sandbox backend on the Agent Platform shell sandbox.

Design notes
- ``BaseSandbox`` implements ls/read/write/edit/glob/grep by running small python3
  scripts through ``execute()``. So ``execute()`` itself must stay UNguarded; the
  command guard lives on ``execute_with_offload`` (the path the agent's shell tool
  uses) and therefore also covers sub-agents, which share this backend.
- File upload/download use only ``base64``/``tr``/``mkdir`` over the shell, because
  the shell sandbox exposes ``/exec`` and nothing else.
- The sandbox holds no secrets: credentials stay in the trusted agent process.
"""
from __future__ import annotations

import base64
import logging
import threading
import posixpath
import shlex
import time
from collections.abc import Callable

from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteOffloadResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

from pinboard.core.config import Settings
from pinboard.core.envfile import persist_env_value
from pinboard.core.guardrails import CommandGuard

log = logging.getLogger(__name__)

_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
RUNTIME_DISPLAY_NAME = "pinboard-sandbox"

# A new sandbox is reported "created" a little before its shell server accepts requests.
# Early calls fail with, for example, 400 FAILED_PRECONDITION "Bad Gateway: Unable to reach the
# sandbox" or 503 UNAVAILABLE. The readiness probe is an idempotent `echo`, so retrying these is safe.
# Real errors (403 PERMISSION_DENIED, 404, ...) are deliberately NOT in this list.
_NOT_READY_MARKERS = (
    "unable to reach the sandbox", "bad gateway", "failed_precondition",
    "unavailable", "503", "502", "504", "deadline_exceeded",
)


def _map_error(output: str) -> str:
    low = (output or "").lower()
    if "permission denied" in low or "read-only file system" in low:
        return "permission_denied"
    if "no such file" in low or "not found" in low:
        return "file_not_found"
    if "is a directory" in low:
        return "is_directory"
    return (output or "unknown error").strip()[:300]


def _validate_path(path: str) -> str | None:
    if not path.startswith("/") or "\x00" in path:
        return "invalid_path"
    if ".." in path.split("/"):  # check the raw path: normpath() would collapse the '..' away
        return "invalid_path"
    return None


class GCPShellSandbox(BaseSandbox):
    def __init__(
        self,
        client,
        name: str | None,
        *,
        opener: Callable[["GCPShellSandbox"], str] | None = None,
        guard: CommandGuard | None = None,
        redact: Callable[[str], str] | None = None,
        on_block: Callable[[str, object], None] | None = None,
        cwd: str = "/workspace",
        read_only: tuple[str, ...] = (),
        default_timeout: int = 120,
        upload_chunk_bytes: int = 24_000,
    ) -> None:
        self._client = client
        self._name = name  # None until a lazily created sandbox is first used
        self._opener = opener
        self._open_lock = threading.Lock()
        self._open_error: str | None = None
        self._guard = guard
        self._redact = redact
        self._on_block = on_block
        self._cwd = cwd.rstrip("/") or "/"
        self._read_only = tuple(p.rstrip("/") for p in read_only)
        self._default_timeout = default_timeout
        self._chunk = upload_chunk_bytes

    @property
    def id(self) -> str:
        return self._name

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def is_open(self) -> bool:
        """True once a real sandbox exists behind this object (always true unless it was created lazily and never used)."""
        return self._name is not None

    def _ensure_open(self) -> str:
        """The sandbox name, creating the sandbox first if this one is lazy and has not been used yet. Only the first caller creates it;
        a failure is remembered so the rest of the turn does not retry a creation that takes minutes."""
        if self._name is not None:
            return self._name
        if self._opener is None:
            raise RuntimeError("this sandbox has no name and no way to create one")
        with self._open_lock:
            if self._name is not None:
                return self._name
            if self._open_error:
                raise RuntimeError(self._open_error)
            try:
                self._name = self._opener(self)
            except Exception as exc:
                self._open_error = f"could not create the sandbox: {type(exc).__name__}: {str(exc)[:200]}"
                raise
            return self._name

    # ── execution ────────────────────────────────────────────────────────
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Raw execution. NOT guarded: BaseSandbox uses this for its own file operations."""
        try:
            res = self._client.sandboxes.execute_bash(
                name=self._ensure_open(),
                command=command,
                cwd=self._cwd,
                timeout=timeout or self._default_timeout,
            )
        except Exception as exc:  # transport/API failure: surface to the agent, log for us
            booting = any(m in str(exc).lower() for m in _NOT_READY_MARKERS)  # a sandbox that was just created answers "unable to reach" for a while
            (log.info if booting else log.warning)("sandbox execute failed%s: %s", " (sandbox still starting)" if booting else "", str(exc)[:300])
            return ExecuteResponse(output=f"Sandbox error: {exc}", exit_code=1, truncated=False)
        out = res.get("stdout") or ""
        err = res.get("stderr") or ""
        text = out + (f"\n<stderr>{err}</stderr>" if err else "")
        if self._redact:
            text = self._redact(text)
        return ExecuteResponse(output=text, exit_code=res.get("returncode"), truncated=False)

    def wait_until_ready(self, timeout: float = 180.0, interval: float = 3.0) -> None:
        """Block until the sandbox shell answers. Only the known not-ready error is retried."""
        deadline = time.monotonic() + timeout
        while True:
            r = self.execute("echo ready", timeout=15)
            if r.exit_code == 0:
                return
            not_ready = any(m in (r.output or "").lower() for m in _NOT_READY_MARKERS)
            if not not_ready or time.monotonic() >= deadline:
                raise RuntimeError(f"Sandbox did not become ready: {(r.output or '')[:300]}")
            log.info("sandbox shell not ready yet; retrying in %.0fs", interval)
            time.sleep(interval)

    def _blocked(self, command: str) -> ExecuteOffloadResult | None:
        if self._guard is None:
            return None
        verdict = self._guard.check(command)
        if verdict.ok:
            return None
        log.warning("command blocked: %s (%s) | %r", verdict.rule, verdict.reason, command[:500])
        if self._on_block:
            try:
                self._on_block(command, verdict)
            except Exception:  # auditing must never break a run
                log.exception("on_block callback failed")
        return ExecuteOffloadResult(
            offloaded=False,
            response=ExecuteResponse(output=verdict.message(), exit_code=126, truncated=False),
        )

    def execute_with_offload(self, command, capture_path, *, max_inline_bytes, max_capture_bytes=None, timeout=None):
        blocked = self._blocked(command)
        if blocked is not None:
            return blocked
        return super().execute_with_offload(
            command, capture_path, max_inline_bytes=max_inline_bytes,
            max_capture_bytes=max_capture_bytes, timeout=timeout,
        )

    async def aexecute_with_offload(self, command, capture_path, *, max_inline_bytes, max_capture_bytes=None, timeout=None):
        blocked = self._blocked(command)
        if blocked is not None:
            return blocked
        return await super().aexecute_with_offload(
            command, capture_path, max_inline_bytes=max_inline_bytes,
            max_capture_bytes=max_capture_bytes, timeout=timeout,
        )


    # ── path policy for the agent's file tools ───────────────────────────
    # (deepagents' own `permissions` option does not support execution backends,
    #  so the same rules are enforced here, on every sync and async code path.)
    def _path_error(self, path: str | None, *, write: bool) -> str | None:
        if path is None:
            return None
        if not path.startswith("/") or ".." in path.split("/") or "\x00" in path:
            return f"Access denied: {path!r} is not a valid absolute path"
        norm = posixpath.normpath(path)
        if not (norm == self._cwd or norm.startswith(self._cwd + "/")):
            return f"Access denied: only paths under {self._cwd} are available"
        if write and any(norm == r or norm.startswith(r + "/") for r in self._read_only):
            return "Access denied: this location is read-only"
        return None

    def read(self, file_path, offset=0, limit=2000):
        if (e := self._path_error(file_path, write=False)):
            return ReadResult(error=e)
        return super().read(file_path, offset, limit)

    async def aread(self, file_path, offset=0, limit=2000):
        if (e := self._path_error(file_path, write=False)):
            return ReadResult(error=e)
        return await super().aread(file_path, offset, limit)

    def ls(self, path):
        if (e := self._path_error(path, write=False)):
            return LsResult(error=e)
        return super().ls(path)

    async def als(self, path):
        if (e := self._path_error(path, write=False)):
            return LsResult(error=e)
        return await super().als(path)

    def glob(self, pattern, path=None):
        if (e := self._path_error(path, write=False)):
            return GlobResult(error=e)
        return super().glob(pattern, path or self._cwd)

    async def aglob(self, pattern, path=None):
        if (e := self._path_error(path, write=False)):
            return GlobResult(error=e)
        return await super().aglob(pattern, path or self._cwd)

    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        if (e := self._path_error(path, write=False)):
            return GrepResult(error=e)
        return super().grep(pattern, path or self._cwd, glob, max_count=max_count)

    async def agrep(self, pattern, path=None, glob=None, *, max_count=None):
        if (e := self._path_error(path, write=False)):
            return GrepResult(error=e)
        return await super().agrep(pattern, path or self._cwd, glob, max_count=max_count)

    def write(self, file_path, content):
        if (e := self._path_error(file_path, write=True)):
            return WriteResult(error=e)
        return super().write(file_path, content)

    async def awrite(self, file_path, content):
        if (e := self._path_error(file_path, write=True)):
            return WriteResult(error=e)
        return await super().awrite(file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        if (e := self._path_error(file_path, write=True)):
            return EditResult(error=e)
        return super().edit(file_path, old_string, new_string, replace_all)

    async def aedit(self, file_path, old_string, new_string, replace_all=False):
        if (e := self._path_error(file_path, write=True)):
            return EditResult(error=e)
        return await super().aedit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path):
        if (e := self._path_error(file_path, write=True)):
            return DeleteResult(error=e)
        return super().delete(file_path)

    async def adelete(self, file_path):
        if (e := self._path_error(file_path, write=True)):
            return DeleteResult(error=e)
        return await super().adelete(file_path)

    # ── files ────────────────────────────────────────────────────────────
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results: list[FileUploadResponse] = []
        for path, data in files:
            bad = _validate_path(path)
            if bad:
                results.append(FileUploadResponse(path=path, error=bad))
                continue
            q_path, q_dir = shlex.quote(path), shlex.quote(posixpath.dirname(path) or "/")
            error: str | None = None
            if len(data) <= self._chunk:
                b64 = base64.b64encode(data).decode()
                r = self.execute(f"mkdir -p {q_dir} && printf %s {shlex.quote(b64)} | base64 -d > {q_path}")
                error = _map_error(r.output) if r.exit_code != 0 else None
            else:
                r = self.execute(f"mkdir -p {q_dir} && : > {q_path}")
                error = _map_error(r.output) if r.exit_code != 0 else None
                offset = 0
                while error is None and offset < len(data):
                    b64 = base64.b64encode(data[offset : offset + self._chunk]).decode()
                    r = self.execute(f"printf %s {shlex.quote(b64)} | base64 -d >> {q_path}")
                    error = _map_error(r.output) if r.exit_code != 0 else None
                    offset += self._chunk
            results.append(FileUploadResponse(path=path, error=error))
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        results: list[FileDownloadResponse] = []
        for path in paths:
            bad = _validate_path(path)
            if bad:
                results.append(FileDownloadResponse(path=path, content=None, error=bad))
                continue
            q = shlex.quote(path)
            r = self.execute(
                f"if [ -d {q} ]; then echo __DIR__; elif [ ! -f {q} ]; then echo __NOENT__; "
                f"elif [ \"$(wc -c < {q})\" -gt {_MAX_DOWNLOAD_BYTES} ]; then echo __TOOBIG__; "
                f"else base64 < {q} | tr -d '\\n'; fi"
            )
            out = (r.output or "").strip()
            if r.exit_code != 0:
                results.append(FileDownloadResponse(path=path, content=None, error=_map_error(r.output)))
            elif out == "__DIR__":
                results.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
            elif out == "__NOENT__":
                results.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
            elif out == "__TOOBIG__":
                results.append(FileDownloadResponse(path=path, content=None, error=f"file larger than {_MAX_DOWNLOAD_BYTES} bytes"))
            else:
                try:
                    results.append(FileDownloadResponse(path=path, content=base64.b64decode(out), error=None))
                except Exception:
                    results.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
        return results


# ── lifecycle ────────────────────────────────────────────────────────────


class SandboxManager:
    """Creates/attaches the Agent Platform instance and sandbox; deletes it on close."""

    def __init__(self, settings: Settings, *, guard: CommandGuard | None = None,
                 redact: Callable[[str], str] | None = None, on_block=None, client=None,
                 adopt: str | None = None):
        """adopt: name of a sandbox created by an earlier request (a parked run). We attach to it
        and, unlike a SANDBOX_NAME you configured yourself, delete it when the run finishes."""
        self.settings = settings
        self.guard = guard
        self.redact = redact
        self.on_block = on_block
        self._adopt = adopt
        self._client = client
        self._created_sandbox: str | None = None

    def _get_client(self):
        if self._client is None:
            import agentplatform

            if not self.settings.gcp_project:
                raise RuntimeError("GCP_PROJECT must be set to use the GCP sandbox")
            self._client = agentplatform.Client(
                project=self.settings.gcp_project, location=self.settings.gcp_location
            )
        return self._client

    def _ensure_runtime(self) -> str:
        if self.settings.sandbox_runtime:
            return self.settings.sandbox_runtime
        runtime = self._get_client().runtimes.create(
            config={"display_name": RUNTIME_DISPLAY_NAME, "labels": {"created-by": "pinboard-agent"}}
        )
        name = getattr(getattr(runtime, "api_resource", runtime), "name")
        log.warning("Created Agent Platform instance %s. Set SANDBOX_RUNTIME=%s in .env to reuse it.", name, name)
        try:
            saved = persist_env_value(".env", "SANDBOX_RUNTIME", name)
        except OSError:
            saved = False
        print(f"[pinboard] created runtime: {name}")
        print("[pinboard] saved SANDBOX_RUNTIME to .env" if saved else f"[pinboard] add to .env:  SANDBOX_RUNTIME={name}")
        return name

    def _provision(self) -> str:
        """Create (or choose) the sandbox this manager works on and remember that it owns it. Returns the name; waits for nothing."""
        client = self._get_client()
        if self._adopt:
            self._created_sandbox = self._adopt
            return self._adopt
        if self.settings.sandbox_name:
            return self.settings.sandbox_name
        runtime = self._ensure_runtime()
        op = client.sandboxes.create(
            name=runtime,
            poll_interval_seconds=2.0,  # SDK default is 0.1s (about 10 requests/second)
            spec={"shell_environment": {}},
            config={
                "display_name": "pinboard-agent",
                "wait_for_completion": True,
                "ttl": f"{self.settings.sandbox_ttl_seconds}s",
            },
        )
        self._created_sandbox = op.response.name
        return self._created_sandbox

    def _make(self, name: str | None, opener=None) -> GCPShellSandbox:
        return GCPShellSandbox(
            self._get_client(), name, opener=opener, guard=self.guard, redact=self.redact, on_block=self.on_block,
            cwd=self.settings.sandbox_cwd, read_only=(self.settings.skills_path,),
        )

    def open(self) -> GCPShellSandbox:
        sandbox = self._make(self._provision())
        try:
            sandbox.wait_until_ready()
        except BaseException:
            self.close()  # do not leak a sandbox we created but could not use
            raise
        return sandbox

    def open_lazy(self, on_created: Callable[[GCPShellSandbox, str], None] | None = None, *,
                  claim: Callable[[], str | None] | None = None, on_claimed: Callable[[str], None] | None = None) -> GCPShellSandbox:
        """A sandbox object that exists at once and is created on first use. Attaching to a known sandbox (``adopt``) or using the
        configured ``SANDBOX_NAME`` binds immediately with no waiting. ``on_created(sandbox, name)`` runs once, right after a NEW
        sandbox is ready (to sync skills, record it, ...); if it fails the new sandbox is deleted.
        ``claim()`` is asked first, at the moment a tool needs the sandbox: it may return the name of a ready SPARE (already created, skills
        already inside) to use instead of creating one; ``on_claimed(name)`` then records it. A spare that does not answer is dropped and a
        new sandbox is created as usual."""
        if self._adopt or self.settings.sandbox_name:
            return self._make(self._provision())

        def opener(sb: GCPShellSandbox) -> str:
            spare = claim() if claim is not None else None
            if spare:
                sb._name = spare
                try:
                    sb.wait_until_ready(timeout=30.0, interval=2.0)  # it was ready when made; this only confirms it is still there
                except Exception:  # noqa: BLE001
                    log.warning("claimed spare sandbox %s does not answer; creating a new one", spare)
                    sb._name = None
                    delete_sandbox(self.settings, spare, self._client)
                else:
                    self._created_sandbox = spare  # owned by this manager from now on, like one it created itself
                    if on_claimed is not None:
                        on_claimed(spare)
                    return spare
            name = self._provision()
            sb._name = name  # readiness probing goes through sb.execute()
            try:
                sb.wait_until_ready()
                if on_created is not None:
                    on_created(sb, name)
            except BaseException:
                sb._name = None
                self.close()
                raise
            return name

        return self._make(None, opener)

    def detach(self) -> str | None:
        """Stop managing the sandbox WITHOUT deleting it (a run parked on an approval). Returns its name."""
        name, self._created_sandbox = self._created_sandbox or self.settings.sandbox_name, None
        return name

    def close(self) -> None:
        if self._created_sandbox:
            try:
                self._get_client().sandboxes.delete(name=self._created_sandbox)
            except Exception:
                log.exception("failed to delete sandbox %s", self._created_sandbox)
            self._created_sandbox = None


def delete_sandbox(settings: Settings, name: str, client=None) -> None:
    """Delete a sandbox by name (used when a parked run expires). Errors are logged, not raised."""
    try:
        if client is None:
            import agentplatform

            client = agentplatform.Client(project=settings.gcp_project, location=settings.gcp_location)
        client.sandboxes.delete(name=name)
    except Exception:
        log.exception("failed to delete sandbox %s", name)


def probe_sandbox(settings: Settings, name: str, client=None, *, attempts: int = 2) -> bool:
    """Is this sandbox still there and answering? One cheap command; a sandbox that fails twice in a row is treated as gone."""
    try:
        if client is None:
            import agentplatform

            client = agentplatform.Client(project=settings.gcp_project, location=settings.gcp_location)
    except Exception:  # noqa: BLE001
        log.exception("cannot reach the sandbox service to check %s", name)
        return False
    for _ in range(attempts):
        try:
            res = client.sandboxes.execute_bash(name=name, command="echo ok", cwd=settings.sandbox_cwd, timeout=15)
            if res.get("returncode") == 0:
                return True
        except Exception as exc:  # noqa: BLE001
            log.info("sandbox %s did not answer: %s", name, str(exc)[:200])
    return False
