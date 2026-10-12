"""scripts/smoke_web.py against a REAL local server (uvicorn on a free port): the in-process test client buffers whole responses, so only a real
socket can show whether a stream arrives in pieces. The engine behind it is scripted; the question is what the browser-side checks conclude."""
import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import requests
import uvicorn

from pinboard.web.app import create_app
from pinboard.web.auth import DevAuthenticator

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("smoke_web", ROOT / "scripts" / "smoke_web.py")
sw = importlib.util.module_from_spec(spec)
sys.modules["smoke_web"] = sw
spec.loader.exec_module(sw)


class ScriptedEngine:
    def __init__(self, gap: float, tokens: int = 6):
        self.gap, self.tokens = gap, tokens

    def call(self, method, inp):
        return {"create_session": {"id": "s1"}, "send_message": {"id": "t1", "status": "running"}, "get_turn": {"id": "t1", "status": "running"},
                "get_config": {"user": inp["user_id"], "default_model": "m"}}[method]

    def stream(self, method, inp):
        after = inp.get("after_seq", 0)
        events = [{"seq": 1, "type": "user_message", "payload": {}}, {"seq": 2, "type": "turn_started", "payload": {}}]
        events += [{"seq": 3 + i, "type": "token", "payload": {"text": "x"}} for i in range(self.tokens)]
        events += [{"seq": 3 + self.tokens, "type": "assistant_message", "payload": {}}, {"seq": 4 + self.tokens, "type": "turn_done", "payload": {}}]
        for e in events:
            if e["seq"] > after:
                if e["type"] == "token":
                    time.sleep(self.gap)
                yield e


@pytest.fixture
def server():
    started = []

    def start(engine):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cfg = uvicorn.Config(create_app(engine, DevAuthenticator("dev@example.com")), host="127.0.0.1", port=port, log_level="error")
        srv = uvicorn.Server(cfg)
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        for _ in range(100):
            try:
                requests.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5)
                break
            except requests.RequestException:
                time.sleep(0.05)
        started.append((srv, t))
        return f"http://127.0.0.1:{port}"

    yield start
    for srv, t in started:
        srv.should_exit = True
        t.join(5)


def test_a_stream_spread_over_time_passes_every_check(server):
    base = server(ScriptedEngine(gap=0.5))
    rep = sw.run(base, sa=None, local=True, long=False)
    assert rep.ok, [c for c in rep.checks if not c.ok]
    names = [c.name for c in rep.checks]
    assert any("incrementally" in n for n in names) and any("Last-Event-ID" in n for n in names) and any("verified the IAP header" in n for n in names)


def test_a_stream_delivered_in_one_lump_is_reported_as_buffered(server):
    base = server(ScriptedEngine(gap=0.0))  # all events at once: exactly what a buffering proxy would look like to the client
    rep = sw.run(base, sa=None, local=True, long=False)
    failed = [c.name for c in rep.checks if not c.ok]
    assert failed == ["stream: tokens arrive incrementally, not in one lump"], failed


def test_the_iap_request_is_signed_as_the_service_account_for_the_exact_url():
    seen = {}

    def signer(sa, payload):
        seen.update(sa=sa, payload=payload)
        return "signed.jwt.value"

    h = sw.iap_headers("bot@proj.iam.gserviceaccount.com", "https://svc-abc.a.run.app", signer=signer)
    assert h == {"Authorization": "Bearer signed.jwt.value"}
    p = seen["payload"]
    assert p["iss"] == p["sub"] == "bot@proj.iam.gserviceaccount.com" and p["aud"] == "https://svc-abc.a.run.app" and 0 < p["exp"] - p["iat"] <= 3600


def test_sse_parsing_handles_comments_ids_and_multi_line_data():
    class Resp:
        def iter_lines(self, chunk_size=1, decode_unicode=True):
            return iter(["retry: 3000", "", ": keep-alive", "", "id: 7", 'data: {"a":1}', "", "event: end", 'data: {"x":2}', ""])

    comments = []
    got = [(ev, id_, data) for _, ev, id_, data in sw.read_sse(Resp(), comments)]
    assert got == [("message", "7", '{"a":1}'), ("end", None, '{"x":2}')] and [c[1] for c in comments] == [": keep-alive"]


def test_the_audience_is_the_service_url_with_a_wildcard_path(monkeypatch):
    signed = []
    monkeypatch.setattr(sw, "sign_jwt", lambda sa, payload: signed.append(payload) or "jwt")

    class Http:  # every request is refused: we only look at what was signed
        def get(self, *a, **k):
            return type("R", (), {"status_code": 404, "text": "", "headers": {}})()

    sw.run("https://svc-abc.a.run.app/", sa="bot@proj.iam.gserviceaccount.com", local=False, long=False, http=Http())
    assert [p["aud"] for p in signed] == ["https://example.invalid/not-this-service", "https://svc-abc.a.run.app/*"]


def test_a_held_connection_passes_when_ticks_keep_coming_and_the_stream_ends_by_itself(server):
    base = server(ScriptedEngine(gap=0.0))
    rep = sw.Report()
    sw.check_hold(requests.Session(), base, {}, rep, seconds=3, every=0.5)
    assert rep.ok, rep.checks
    assert "ended by itself: True" in rep.checks[0].detail


def test_the_hold_route_needs_a_sign_in_and_limits_its_parameters():
    from fastapi.testclient import TestClient
    from pinboard.web.app import create_app
    from pinboard.web.auth import AuthError

    class Auth:
        def email(self, h):
            if not h.get("x-u"):
                raise AuthError("no")
            return "u@example.com"

    c = TestClient(create_app(ScriptedEngine(0), Auth()))
    assert c.get("/_diag/hold?seconds=1").status_code == 401
    assert c.get("/_diag/hold?seconds=99999", headers={"x-u": "1"}).status_code == 422  # not an unbounded resource drain
    assert c.get("/_diag/hold?seconds=1&every=0.0001", headers={"x-u": "1"}).status_code == 422
    r = c.get("/_diag/hold?seconds=1&every=0.5", headers={"x-u": "1"})
    assert r.status_code == 200 and "event: end" in r.text and r.text.count("data: {\"tick\"") >= 1
