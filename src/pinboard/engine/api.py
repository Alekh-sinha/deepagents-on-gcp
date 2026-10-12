"""HTTP API: REST routes for clients, plus the Agent Runtime container-contract routes.

Auth: every route except /healthz needs ``Authorization: Bearer <PINBOARD_API_TOKEN>``.
The contract routes (/api/reasoning_engine, /api/stream_reasoning_engine) can instead trust
the platform's IAM when TRUST_PLATFORM_AUTH=true. Only enable that when the service is NOT
publicly reachable (e.g. deployed on Agent Runtime, which authenticates callers itself).

Contract methods are declared once, in UNARY_METHODS / STREAM_METHODS, and CONTRACT_METHODS (the ``class_methods`` list given to Agent
Runtime at deploy time) is derived from them; create_app() refuses to start if the dispatch tables ever disagree with the declaration.
The frontend server passes the signed-in user as ``user_id`` in the input; any user other than the owner must be on the policy's allowlist.
"""
from __future__ import annotations

import base64
import hmac
import json
from typing import Any, Callable, Iterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from pinboard.engine import auto_approve as auto
from pinboard.engine.errors import BadRequest, Busy, Conflict, Forbidden, NotFound, ServiceError

from pinboard.engine.service import RunService

MIN_TOKEN_LENGTH = 24

UNARY_METHODS = (
    # sessions and turns
    "create_session", "list_sessions", "get_session", "update_session", "get_transcript", "warm_sandbox", "send_message", "get_turn", "get_events",
    "cancel_turn", "resume_turn",
    # approvals, models, housekeeping
    "list_approvals", "decide", "list_models", "get_config", "get_audit", "expire",
    # the user's saved files
    "list_artifacts", "get_artifact", "read_artifact", "get_artifact_url",
    # bring-your-own MCP servers
    "list_mcp_servers", "put_mcp_server", "import_mcp_config", "update_mcp_server", "test_mcp_server", "delete_mcp_server",
    # scheduled tasks
    "list_schedules", "create_schedule", "update_schedule", "delete_schedule", "run_schedule_now", "list_schedule_runs",
    # single-shot runs (the original synchronous API)
    "run", "get_run",
)
STREAM_METHODS = ("stream_turn",)
CONTRACT_METHODS = [{"name": n, "api_mode": ""} for n in UNARY_METHODS] + [{"name": n, "api_mode": "stream"} for n in STREAM_METHODS]

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # largest file served through the API itself
INLINE_READ_BYTES = 2 * 1024 * 1024  # largest file returned inside a contract response (base64)
HEARTBEAT_RANGE = (1.0, 30.0)
STREAM_SECONDS_RANGE = (5.0, 300.0)  # 300 s is the longest streaming request proven on the real platform


class RunIn(BaseModel):
    prompt: str = Field(max_length=32_000)
    thread_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.\-]+$")


class SessionIn(BaseModel):
    title: str = Field(default="", max_length=120)
    model: str | None = Field(default=None, max_length=100)
    auto_approve: bool | dict[str, Any] | None = None


class SessionPatch(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=100)
    archived: bool | None = None
    auto_approve: bool | dict[str, Any] | None = None  # true / {"kinds": [...], "max_per_turn": n} = approve automatically; false / null = ask every time


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=32_000)
    model: str | None = Field(default=None, max_length=100)
    auto_approve: bool | dict[str, Any] | None = None  # only when present: replaces the session's setting


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"] | None = None
    message: str | None = Field(default=None, max_length=1000)
    decisions: list[dict[str, Any]] | None = None


class ContractIn(BaseModel):
    class_method: str
    input: dict[str, Any] = Field(default_factory=dict)


async def parse_contract(request: Request) -> ContractIn:
    """Agent Runtime delivers the contract body JSON-encoded TWICE (a JSON string that contains the JSON object),
    verified on the real platform. Decode until it is an object; a plain object (local/tests) works too."""
    try:
        data = json.loads(await request.body())
        for _ in range(3):
            if isinstance(data, str):
                data = json.loads(data)
        return ContractIn.model_validate(data)
    except ValueError as exc:  # json errors and pydantic ValidationError are both ValueErrors
        raise HTTPException(422, f"invalid contract body: {str(exc)[:200]}") from exc


# ── small input validators for contract methods (a bad value is a clean 400, never a stack trace) ──

def _str(i: dict, key: str, *, required: bool = True, max_len: int = 200, min_len: int = 1) -> str | None:
    v = i.get(key)
    if v is None or v == "":
        if required:
            raise BadRequest(f"missing input field '{key}'")
        return None
    if not isinstance(v, str) or not (min_len <= len(v) <= max_len):
        raise BadRequest(f"'{key}' must be a string of {min_len}-{max_len} characters")
    return v


def _num(i: dict, key: str, default: float, lo: float, hi: float, *, integer: bool = False) -> float:
    v = i.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and int(v) != v) or not lo <= v <= hi:
        raise BadRequest(f"'{key}' must be a {'whole ' if integer else ''}number between {lo:g} and {hi:g}")
    return int(v) if integer else float(v)


def _dict(i: dict, key: str) -> dict | None:
    v = i.get(key)
    if v is None:
        return None
    if not isinstance(v, dict):
        raise BadRequest(f"'{key}' must be an object")
    return v


def _strlist(i: dict, key: str, *, max_items: int = 200) -> list[str] | None:
    v = i.get(key)
    if v is None:
        return None
    if not (isinstance(v, list) and len(v) <= max_items and all(isinstance(x, str) and 0 < len(x) <= 200 for x in v)):
        raise BadRequest(f"'{key}' must be a list of up to {max_items} names")
    return v


def _bool(i: dict, key: str, default: bool = False) -> bool:
    v = i.get(key, default)
    if not isinstance(v, bool):
        raise BadRequest(f"'{key}' must be true or false")
    return v


def create_app(service: RunService, token: str, *, trust_platform_auth: bool = False) -> FastAPI:
    if len(token or "") < MIN_TOKEN_LENGTH:
        raise ValueError(f"PINBOARD_API_TOKEN must be at least {MIN_TOKEN_LENGTH} characters (try: openssl rand -hex 32)")

    app = FastAPI(title="pinboard", docs_url=None, redoc_url=None, openapi_url=None)
    token_bytes = token.encode()

    def bearer_ok(authorization: str | None) -> bool:
        scheme, _, cred = (authorization or "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(cred.strip().encode(), token_bytes)

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if not bearer_ok(authorization):
            raise HTTPException(401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    def require_contract_auth(authorization: str | None = Header(default=None)) -> None:
        if not trust_platform_auth:
            require_auth(authorization)

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError):
        if isinstance(exc, NotFound):
            code, headers = 404, None
        elif isinstance(exc, Forbidden):
            code, headers = 403, None
        elif isinstance(exc, Busy):
            code, headers = 429, {"Retry-After": "5"}
        elif isinstance(exc, Conflict):
            code, headers = 409, None
        else:
            code, headers = 400, None
        return JSONResponse({"detail": str(exc)}, status_code=code, headers=headers)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    def ndjson(items: Iterator[dict], wrap: bool) -> StreamingResponse:
        def lines() -> Iterator[str]:
            try:
                for item in items:
                    yield json.dumps({"output": item} if wrap else item, default=str) + "\n"
            except Exception as exc:  # noqa: BLE001  the status line is already sent; tell the client in-band
                yield json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:300]}"}) + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    def follow(run_id: str, after_seq: int, heartbeat: float, max_seconds: float, user_id: str | None) -> Iterator[dict]:
        service.get_turn(run_id, user_id=user_id)  # ownership/404 BEFORE the response starts, so errors get a proper status code
        return service.tail_turn(run_id, after_seq, user_id=user_id, heartbeat_seconds=heartbeat, max_seconds=max_seconds)

    # ── REST: single-shot runs (original API) ────────────────────────────
    @app.post("/v1/runs", dependencies=[Depends(require_auth)])
    def start_run(body: RunIn):
        run = service.start_run(body.prompt, thread_id=body.thread_id)
        return JSONResponse(run.to_dict(), status_code=422 if run.status == "blocked" else 200)

    @app.get("/v1/runs/{run_id}", dependencies=[Depends(require_auth)])
    def get_run(run_id: str):
        return service.get_run(run_id).to_dict()

    @app.get("/v1/runs/{run_id}/audit", dependencies=[Depends(require_auth)])
    def get_audit(run_id: str):
        return {"events": service.audit(run_id)}

    # ── REST: sessions and turns ─────────────────────────────────────────
    @app.post("/v1/sessions", dependencies=[Depends(require_auth)])
    def create_session(body: SessionIn):
        return service.create_session(title=body.title, model=body.model,
                                      auto_approve=body.auto_approve if "auto_approve" in body.model_fields_set else auto.UNSET).to_dict()

    @app.get("/v1/sessions", dependencies=[Depends(require_auth)])
    def list_sessions(limit: int = 50):
        return {"sessions": [s.to_dict() for s in service.list_sessions(limit=max(1, min(limit, 100)))]}

    @app.get("/v1/sessions/{session_id}", dependencies=[Depends(require_auth)])
    def get_session(session_id: str):
        return service.get_session(session_id)

    @app.patch("/v1/sessions/{session_id}", dependencies=[Depends(require_auth)])
    def update_session(session_id: str, body: SessionPatch):
        return service.update_session(session_id, body.model_dump(exclude_unset=True)).to_dict()

    @app.get("/v1/sessions/{session_id}/transcript", dependencies=[Depends(require_auth)])
    def get_transcript(session_id: str, limit: int = 20, before_turn_id: str | None = None):
        return service.get_transcript(session_id, limit=limit, before_turn_id=before_turn_id)

    @app.post("/v1/sessions/{session_id}/warm", dependencies=[Depends(require_auth)])
    def warm_sandbox(session_id: str):
        return service.warm_sandbox(session_id)

    @app.get("/v1/config", dependencies=[Depends(require_auth)])
    def get_config():
        return service.get_config()

    @app.post("/v1/sessions/{session_id}/messages", dependencies=[Depends(require_auth)])
    def send_message(session_id: str, body: MessageIn):
        turn = service.send_message(session_id, body.text, model=body.model, auto_approve=body.auto_approve if "auto_approve" in body.model_fields_set else auto.UNSET)
        return JSONResponse(turn.to_dict(), status_code=422 if turn.status == "blocked" else 200)

    @app.get("/v1/turns/{turn_id}", dependencies=[Depends(require_auth)])
    def get_turn(turn_id: str):
        return service.get_turn(turn_id).to_dict()

    @app.get("/v1/turns/{turn_id}/events", dependencies=[Depends(require_auth)])
    def get_events(turn_id: str, after_seq: int = 0, limit: int = 500):
        return {"events": service.get_events(turn_id, after_seq, limit)}

    @app.get("/v1/turns/{turn_id}/stream", dependencies=[Depends(require_auth)])
    def stream_turn(turn_id: str, after_seq: int = 0, heartbeat_seconds: float = 10.0, max_seconds: float = 270.0):
        hb = min(max(heartbeat_seconds, HEARTBEAT_RANGE[0]), HEARTBEAT_RANGE[1])
        mx = min(max(max_seconds, STREAM_SECONDS_RANGE[0]), STREAM_SECONDS_RANGE[1])
        return ndjson(follow(turn_id, max(0, after_seq), hb, mx, None), wrap=False)

    @app.post("/v1/turns/{turn_id}/cancel", dependencies=[Depends(require_auth)])
    def cancel_turn(turn_id: str):
        return service.cancel_turn(turn_id).to_dict()

    @app.post("/v1/turns/{turn_id}/resume", dependencies=[Depends(require_auth)])
    def resume_turn(turn_id: str):
        return service.resume_turn(turn_id).to_dict()

    @app.get("/v1/models", dependencies=[Depends(require_auth)])
    def list_models():
        return service.list_models()

    # ── REST: saved files (artifacts) ────────────────────────────────────
    @app.get("/v1/artifacts", dependencies=[Depends(require_auth)])
    def list_artifacts(session_id: str | None = None, limit: int = 50):
        return {"artifacts": service.list_artifacts(session_id=session_id, limit=limit)}

    @app.get("/v1/artifacts/{artifact_id}", dependencies=[Depends(require_auth)])
    def get_artifact(artifact_id: str):
        return service.get_artifact(artifact_id)

    @app.get("/v1/artifacts/{artifact_id}/content", dependencies=[Depends(require_auth)])
    def artifact_content(artifact_id: str):
        meta, data = service.read_artifact(artifact_id, max_bytes=MAX_DOWNLOAD_BYTES)
        safe_name = "".join(ch for ch in meta["name"] if ch.isalnum() or ch in "._- ")[:100] or "download"
        return Response(data, media_type=meta["content_type"], headers={"Content-Disposition": f'attachment; filename="{safe_name}"', "X-Content-Type-Options": "nosniff"})

    # ── REST: approvals and housekeeping ─────────────────────────────────
    @app.get("/v1/approvals", dependencies=[Depends(require_auth)])
    def list_approvals(status: Literal["pending", "approved", "rejected", "expired"] | None = None, session_id: str | None = None):
        return {"approvals": [a.to_dict() for a in service.list_approvals(status=status, session_id=session_id)]}

    @app.post("/v1/approvals/{approval_id}/decision", dependencies=[Depends(require_auth)])
    def decide(approval_id: str, body: DecisionIn):
        return service.decide(approval_id, body.decision, message=body.message, decisions=body.decisions).to_dict()

    @app.post("/v1/maintenance/expire", dependencies=[Depends(require_auth)])
    def expire():
        return {"expired": service.expire_stale()}

    @app.post("/v1/maintenance", dependencies=[Depends(require_auth)])
    def maintenance():
        return service.maintenance()

    # ── Agent Runtime container contract ─────────────────────────────────
    def uid(i: dict) -> str:
        return service.authorize_user(_str(i, "user_id", required=False))

    def housekeeping(_: dict) -> dict:
        report = service.maintenance()
        return {**report, "expired": report["approvals_expired"]}

    def read_artifact(i: dict) -> dict:
        meta, data = service.read_artifact(_str(i, "artifact_id"), user_id=uid(i), max_bytes=INLINE_READ_BYTES)
        return {"artifact": meta, "content_base64": base64.b64encode(data).decode()}

    unary: dict[str, Callable[[dict], Any]] = {
        "create_session": lambda i: service.create_session(user_id=uid(i), title=_str(i, "title", required=False, max_len=120) or "",
                                                           model=_str(i, "model", required=False, max_len=100),
                                                           auto_approve=i["auto_approve"] if "auto_approve" in i else auto.UNSET).to_dict(),
        "list_sessions": lambda i: [s.to_dict() for s in service.list_sessions(user_id=uid(i), limit=_num(i, "limit", 50, 1, 100, integer=True),
                                                                                include_archived=_bool(i, "include_archived"))],
        "update_session": lambda i: service.update_session(_str(i, "session_id"), {k: i[k] for k in ("title", "model", "archived", "auto_approve") if k in i},
                                                           user_id=uid(i)).to_dict(),
        "get_transcript": lambda i: service.get_transcript(_str(i, "session_id"), user_id=uid(i), limit=_num(i, "limit", 20, 1, 50, integer=True),
                                                           before_turn_id=_str(i, "before_turn_id", required=False)),
        "get_config": lambda i: service.get_config(user_id=uid(i)),
        "list_mcp_servers": lambda i: {"servers": service.list_mcp_servers(user_id=uid(i))},
        "put_mcp_server": lambda i: service.put_mcp_server(_str(i, "name", max_len=32), _str(i, "url", max_len=2000), headers=_dict(i, "headers"),
                                                           transport=_str(i, "transport", required=False, max_len=10), enabled=_bool(i, "enabled", True),
                                                           requires_approval=_bool(i, "requires_approval", True), no_approval_tools=_strlist(i, "no_approval_tools"), user_id=uid(i)),
        "import_mcp_config": lambda i: service.import_mcp_config(i.get("config") if isinstance(i.get("config"), dict) else _str(i, "config", max_len=200_000), user_id=uid(i)),
        "update_mcp_server": lambda i: service.update_mcp_server(_str(i, "name", max_len=32), {k: i[k] for k in ("enabled", "requires_approval", "no_approval_tools") if k in i}, user_id=uid(i)),
        "test_mcp_server": lambda i: service.test_mcp_server(_str(i, "name", max_len=32), user_id=uid(i)),
        "delete_mcp_server": lambda i: service.delete_mcp_server(_str(i, "name", max_len=32), user_id=uid(i)),
        "list_schedules": lambda i: {"schedules": service.list_schedules(user_id=uid(i))},
        "create_schedule": lambda i: service.create_schedule(_str(i, "title", max_len=200), _str(i, "instructions", max_len=20_000), _str(i, "cron", max_len=100),
                                                             timezone=_str(i, "timezone", required=False, max_len=64) or "UTC", model=_str(i, "model", required=False, max_len=100),
                                                             auto_approve=i.get("auto_approve"), enabled=_bool(i, "enabled", True), user_id=uid(i)),
        "update_schedule": lambda i: service.update_schedule(_str(i, "schedule_id", max_len=40),
                                                             {k: i[k] for k in ("title", "instructions", "cron", "timezone", "model", "auto_approve", "enabled") if k in i}, user_id=uid(i)),
        "delete_schedule": lambda i: service.delete_schedule(_str(i, "schedule_id", max_len=40), user_id=uid(i)),
        "run_schedule_now": lambda i: service.run_schedule_now(_str(i, "schedule_id", max_len=40), user_id=uid(i)),
        "list_schedule_runs": lambda i: {"runs": service.list_schedule_runs(_str(i, "schedule_id", max_len=40), user_id=uid(i), limit=_num(i, "limit", 20, 1, 100, integer=True))},
        "warm_sandbox": lambda i: service.warm_sandbox(_str(i, "session_id"), user_id=uid(i)),
        "get_session": lambda i: service.get_session(_str(i, "session_id"), user_id=uid(i)),
        "send_message": lambda i: service.send_message(_str(i, "session_id"), _str(i, "text", max_len=32_000), user_id=uid(i),
                                                       model=_str(i, "model", required=False, max_len=100),
                                                       auto_approve=i["auto_approve"] if "auto_approve" in i else auto.UNSET).to_dict(),
        "get_turn": lambda i: service.get_turn(_str(i, "turn_id"), user_id=uid(i)).to_dict(),
        "get_events": lambda i: {"events": service.get_events(_str(i, "turn_id"), _num(i, "after_seq", 0, 0, 2**31, integer=True),
                                                              _num(i, "limit", 500, 1, 1000, integer=True), user_id=uid(i))},
        "cancel_turn": lambda i: service.cancel_turn(_str(i, "turn_id"), user_id=uid(i)).to_dict(),
        "resume_turn": lambda i: service.resume_turn(_str(i, "turn_id"), user_id=uid(i)).to_dict(),
        "list_approvals": lambda i: [a.to_dict() for a in service.list_approvals(status=_str(i, "status", required=False, max_len=20), user_id=uid(i),
                                                                               session_id=_str(i, "session_id", required=False))],
        "decide": lambda i: service.decide(_str(i, "approval_id"), i.get("decision"), user_id=uid(i), message=i.get("message"), decisions=i.get("decisions")).to_dict(),
        "list_models": lambda i: service.list_models(),
        "get_audit": lambda i: {"events": service.audit(_str(i, "run_id"), user_id=uid(i))},
        "expire": housekeeping,
        "list_artifacts": lambda i: service.list_artifacts(user_id=uid(i), session_id=_str(i, "session_id", required=False),
                                                           limit=_num(i, "limit", 50, 1, 200, integer=True)),
        "get_artifact": lambda i: service.get_artifact(_str(i, "artifact_id"), user_id=uid(i)),
        "read_artifact": read_artifact,
        "get_artifact_url": lambda i: {"url": service.artifact_url(_str(i, "artifact_id"), user_id=uid(i), seconds=_num(i, "seconds", 600, 30, 3600, integer=True))},
        "run": lambda i: service.start_run(_str(i, "prompt", max_len=32_000), user_id=uid(i), thread_id=_str(i, "thread_id", required=False, max_len=128)).to_dict(),
        "get_run": lambda i: service.get_run(_str(i, "run_id"), user_id=uid(i)).to_dict(),
    }
    streams: dict[str, Callable[[dict], Iterator[dict]]] = {
        "stream_turn": lambda i: follow(_str(i, "turn_id"), _num(i, "after_seq", 0, 0, 2**31, integer=True),
                                        _num(i, "heartbeat_seconds", 10.0, *HEARTBEAT_RANGE), _num(i, "max_seconds", 270.0, *STREAM_SECONDS_RANGE), uid(i)),
    }
    if set(unary) != set(UNARY_METHODS) or set(streams) != set(STREAM_METHODS):  # the deploy-time declaration must match what is served
        raise RuntimeError("contract method tables are out of sync with UNARY_METHODS / STREAM_METHODS")

    def lookup(table: dict, body: ContractIn):
        fn = table.get(body.class_method)
        if fn is None:
            known = sorted({*unary, *streams})
            if body.class_method in unary or body.class_method in streams:
                raise BadRequest(f"{body.class_method!r} is a {'streaming' if body.class_method in streams else 'unary'} method; "
                                 f"call it on /api/{'stream_reasoning_engine' if body.class_method in streams else 'reasoning_engine'}")
            raise BadRequest(f"unknown class_method {body.class_method!r}; expected one of {known}")
        return fn

    @app.post("/api/reasoning_engine", dependencies=[Depends(require_contract_auth)])
    def contract_unary(body: ContractIn = Depends(parse_contract)):
        return {"output": lookup(unary, body)(body.input)}

    @app.post("/api/stream_reasoning_engine", dependencies=[Depends(require_contract_auth)])
    def contract_stream(body: ContractIn = Depends(parse_contract)):
        if body.class_method in streams:
            return ndjson(streams[body.class_method](body.input), wrap=True)
        out = lookup(unary, body)(body.input)  # a unary method on the stream route: run it, then stream its single result
        return ndjson(iter([out]), wrap=True)

    return app
