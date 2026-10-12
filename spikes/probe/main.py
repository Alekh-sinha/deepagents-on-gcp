"""Throwaway probe container for the Agent Runtime spike. NOT part of the product.

Implements the Agent Runtime container contract and exposes small methods that each answer one
question about the platform (see ../README.md). It never returns secret values.
"""
import hashlib
import json
import os
import threading
import time
import uuid

import requests
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

BOOT = time.time()
INSTANCE_ID = uuid.uuid4().hex[:8]
BG = {"ticks": [], "running": False}

app = FastAPI()


class Call(BaseModel):
    class_method: str
    input: dict = Field(default_factory=dict)


async def parse_call(request: Request) -> Call:
    """The platform delivers the body DOUBLE-ENCODED (a JSON string that contains the JSON object), so decode until it is a dict."""
    body = await request.body()
    print(f"[contract] {request.url.path} content-type={request.headers.get('content-type')} raw={body[:200]!r}", flush=True)
    data = json.loads(body)
    for _ in range(3):
        if isinstance(data, str):
            data = json.loads(data)
    return Call(**data)


def _meta(path: str) -> str | None:
    try:
        r = requests.get(f"http://metadata.google.internal/computeMetadata/v1/{path}",
                         headers={"Metadata-Flavor": "Google"}, timeout=3)
        return r.text if r.ok else f"HTTP {r.status_code}"
    except Exception as exc:
        return f"unreachable: {type(exc).__name__}"


# ── unary methods ────────────────────────────────────────────────────────
def ping(_):
    return {
        "instance": INSTANCE_ID, "uptime_s": round(time.time() - BOOT, 1), "port_env": os.environ.get("PORT"),
        "env_names": sorted(os.environ), "service_account": _meta("instance/service-accounts/default/email"),
        "project_number": _meta("project/numeric-project-id"),
    }


def sleep(i):
    time.sleep(float(i.get("seconds", 1)))
    return {"slept": i.get("seconds", 1), "instance": INSTANCE_ID}


def bg_start(i):
    """Start a background thread that ticks every second, then return immediately."""
    BG.update(ticks=[], running=True)

    def work():
        for _ in range(int(i.get("seconds", 120))):
            BG["ticks"].append(time.time())
            time.sleep(1)
        BG["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"started": True, "instance": INSTANCE_ID}


def bg_status(_):
    t = BG["ticks"]
    gaps = [round(b - a, 1) for a, b in zip(t, t[1:])]
    return {"instance": INSTANCE_ID, "ticks": len(t), "running": BG["running"], "max_gap_s": max(gaps, default=0),
            "last_tick_age_s": round(time.time() - t[-1], 1) if t else None}


def egress(i):
    urls = i.get("urls") or ["https://api.github.com", "https://api.agentmail.to", "https://api.tavily.com",
                             "https://example.com", "https://secretmanager.googleapis.com", "https://api.ipify.org"]
    out = {}
    for u in urls:
        try:
            r = requests.get(u, timeout=6)
            out[u] = f"HTTP {r.status_code}" + (f" body={r.text[:40]!r}" if "ipify" in u else "")
        except Exception as exc:
            out[u] = f"FAIL {type(exc).__name__}: {str(exc)[:100]}"
    return out


def secret(i):
    from google.cloud import secretmanager

    name = f"projects/{i['project']}/secrets/{i['name']}/versions/latest"
    v = secretmanager.SecretManagerServiceClient().access_secret_version(request={"name": name}).payload.data
    return {"length": len(v), "sha256_prefix": hashlib.sha256(v).hexdigest()[:8]}


def gemini(i):
    from google import genai

    c = genai.Client(vertexai=True, project=i["project"], location=i.get("location", "us-central1"))
    r = c.models.generate_content(model=i.get("model", "gemini-2.5-flash"), contents="Reply with the single word: pong")
    return {"model": i.get("model", "gemini-2.5-flash"), "reply": (r.text or "")[:40]}


def db(i):
    """Connect with the Cloud SQL Python Connector (public IP, TLS, no authorized networks), round-trip a row."""
    from google.cloud import secretmanager
    from google.cloud.sql.connector import Connector

    pw = secretmanager.SecretManagerServiceClient().access_secret_version(
        request={"name": f"projects/{i['project']}/secrets/{i['password_secret']}/versions/latest"}).payload.data.decode()
    t0 = time.time()
    with Connector() as conn_factory:
        conn = conn_factory.connect(i["instance"], "pg8000", user=i["user"], password=pw, db=i["db"])
        t_connect = round(time.time() - t0, 2)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS spike_probe (id serial primary key, instance text, at timestamptz default now())")
        cur.execute("INSERT INTO spike_probe (instance) VALUES (%s)", (INSTANCE_ID,))
        conn.commit()
        cur.execute("SELECT count(*), version() FROM spike_probe")
        n, ver = cur.fetchone()
        conn.close()
    return {"connect_s": t_connect, "rows": n, "server": ver[:40]}


def db_psycopg(i):
    """psycopg 3 + psycopg_pool + LangGraph PostgresSaver through the Cloud SQL Auth Proxy on localhost:5432 (what state.py really uses)."""
    import socket

    import psycopg
    from google.cloud import secretmanager
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg_pool import ConnectionPool

    pw = secretmanager.SecretManagerServiceClient().access_secret_version(
        request={"name": f"projects/{i['project']}/secrets/{i['password_secret']}/versions/latest"}).payload.data.decode()
    for _ in range(30):  # the proxy starts in the background; wait for it
        try:
            socket.create_connection(("127.0.0.1", 5432), timeout=1).close()
            break
        except OSError:
            time.sleep(1)
    else:
        return {"error": "proxy never listened on 127.0.0.1:5432 (is SQL_CONNECTION set? did the proxy start?)"}
    conninfo = psycopg.conninfo.make_conninfo(host="127.0.0.1", port=5432, user=i["user"], password=pw, dbname=i["db"], sslmode="disable")
    t0 = time.time()
    with psycopg.connect(conninfo, autocommit=True) as c:
        version = c.execute("SELECT version()").fetchone()[0][:30]
    t_plain = round(time.time() - t0, 2)
    with ConnectionPool(conninfo, min_size=1, max_size=2, kwargs={"autocommit": True, "prepare_threshold": 0}, open=True) as pool:
        saver = PostgresSaver(pool)
        saver.setup()
        n = len(list(saver.list({"configurable": {"thread_id": "spike"}})))
    return {"psycopg": "ok", "connect_s": t_plain, "server": version, "postgres_saver_setup": "ok", "checkpoints_listed": n}


UNARY = {f.__name__: f for f in (ping, sleep, bg_start, bg_status, egress, secret, gemini, db, db_psycopg)}


# ── streaming method ─────────────────────────────────────────────────────
def stream_beat(i):
    total, every = float(i.get("seconds", 30)), float(i.get("every", 5))
    start = time.time()
    while time.time() - start < total:
        yield {"beat": round(time.time() - start, 1), "instance": INSTANCE_ID}
        time.sleep(every)
    yield {"done": True, "elapsed": round(time.time() - start, 1)}


STREAM = {"stream_beat": stream_beat}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/reasoning_engine")
async def unary(request: Request):
    body = await parse_call(request)
    fn = UNARY.get(body.class_method)
    if fn is None:
        return {"output": {"error": f"unknown method {body.class_method}"}}
    try:
        return {"output": fn(body.input)}
    except Exception as exc:  # report, never crash: the probe must tell us what failed
        return {"output": {"error": f"{type(exc).__name__}: {str(exc)[:400]}"}}


@app.post("/api/stream_reasoning_engine")
async def stream(request: Request):
    body = await parse_call(request)
    fn = STREAM.get(body.class_method)
    if fn is None:
        return {"error": f"unknown stream method {body.class_method}"}
    return StreamingResponse((json.dumps({"output": c}) + "\n" for c in fn(body.input)), media_type="application/x-ndjson")
