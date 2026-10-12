#!/usr/bin/env python3
"""Check a DEPLOYED Pinboard runtime from outside, the way the frontend will use it (REST :query / :streamQuery as you).

    uv run python scripts/smoke_runtime.py                 # basics: models, housekeeping, a chat turn, a restart-proof state read
    uv run python scripts/smoke_runtime.py --search        # + the agent uses web search (Tavily reachable from the runtime?)
    uv run python scripts/smoke_runtime.py --sandbox       # + a shell command that waits for approval, then is rejected (creates a sandbox)
    uv run python scripts/smoke_runtime.py --all

Needs: deploy/.state.json (written by `deploy.sh deploy`) and `gcloud auth application-default login`.
Exit code 0 only when every check passed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

STATE = Path(__file__).resolve().parent.parent / "deploy" / ".state.json"


class RestTransport:
    """Calls a deployed runtime. Unary: POST :query. Streaming: POST :streamQuery, newline-delimited JSON."""

    def __init__(self, resource: str, region: str):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self.http = AuthorizedSession(creds)
        self.base = f"https://{region}-aiplatform.googleapis.com/v1/{resource}"

    def call(self, method: str, inp: dict | None = None, timeout: float = 120) -> Any:
        r = self.http.post(f"{self.base}:query", json={"classMethod": method, "input": inp or {}}, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"{method}: HTTP {r.status_code}: {r.text[:600]}")
        return _unwrap(r.json(), method)

    def stream(self, method: str, inp: dict, timeout: float = 300) -> Iterator[Any]:
        r = self.http.post(f"{self.base}:streamQuery", json={"classMethod": method, "input": inp}, stream=True, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"{method}: HTTP {r.status_code}: {r.text[:600]}")
        for line in r.iter_lines(decode_unicode=True):
            if line:
                yield _unwrap(json.loads(line), method)


def _unwrap(body: dict, method: str) -> Any:
    if isinstance(body, dict) and "error" in body:
        raise RuntimeError(f"{method}: {body['error']}")
    out = body.get("output") if isinstance(body, dict) else body
    if isinstance(out, dict) and "error" in out and len(out) <= 3:
        raise RuntimeError(f"{method}: {out['error']}")
    return out


class Smoke:
    def __init__(self, transport, say: Callable[[str], None] = print):
        self.t, self.say, self.results = transport, say, []

    def check(self, name: str, fn: Callable[[], str]) -> bool:
        t0 = time.monotonic()
        try:
            detail = fn()
            ok = True
        except Exception as exc:  # noqa: BLE001  every failure is reported, none stops the other checks
            detail, ok = f"{type(exc).__name__}: {str(exc)[:500]}", False
        self.say(f"[{'PASS' if ok else 'FAIL'}] {name} ({time.monotonic() - t0:.1f}s): {detail}")
        self.results.append((name, ok))
        return ok

    def follow(self, turn_id: str, *, until: Callable[[dict], bool], limit_s: float = 300) -> list[dict]:
        """Read the turn's event stream (reconnecting like the frontend) until `until(event)` or the deadline."""
        events, seq, deadline = [], 0, time.monotonic() + limit_s
        while time.monotonic() < deadline:
            for e in self.t.stream("stream_turn", {"turn_id": turn_id, "after_seq": seq, "heartbeat_seconds": 5, "max_seconds": 60}):
                if e["type"] in ("heartbeat", "reconnect"):
                    continue
                seq = e["seq"]
                events.append(e)
                if until(e):
                    return events
        raise TimeoutError(f"no matching event within {limit_s:.0f}s; saw {[e['type'] for e in events][-8:]}")

    def turn(self, session_id: str, text: str) -> str:
        return self.t.call("send_message", {"session_id": session_id, "text": text})["id"]

    # ── the checks ───────────────────────────────────────────────────────
    def basics(self) -> str | None:
        self.check("list_models", lambda: f"default={self.t.call('list_models')['default']}")
        self.check("housekeeping (what the scheduler calls)", lambda: json.dumps(self.t.call("expire"))[:200])
        sid: list[str] = []

        def chat() -> str:
            sid.append(self.t.call("create_session", {"title": "smoke"})["id"])
            tid = self.turn(sid[0], "Reply with exactly the single word: pong")
            ev = self.follow(tid, until=lambda e: e["type"] in ("turn_done", "turn_error"))
            last = ev[-1]
            if last["type"] != "turn_done":
                raise RuntimeError(f"turn ended with {last['type']}: {last['payload']}")
            text = "".join(e["payload"].get("text", "") for e in ev if e["type"] == "assistant_message").lower()
            if "pong" not in text:
                raise RuntimeError(f"unexpected answer: {text[:200]!r}")
            t = last["payload"].get("timing") or {}
            return f"answered; setup {t.get('setup_seconds')}s, first output {t.get('first_output_seconds')}s, total {t.get('total_seconds')}s"

        self.check("chat turn through the background worker + event stream", chat)
        if sid:
            self.check("state is durable (session read back)", lambda: self._durable(sid[0]))
        return sid[0] if sid else None

    def _durable(self, session_id: str) -> str:
        got = self.t.call("get_session", {"session_id": session_id})
        if got["session"]["id"] != session_id or not got["turns"] or got["turns"][0]["status"] != "completed":
            raise RuntimeError(f"unexpected session state: {json.dumps(got)[:300]}")
        return f"session found with {len(got['turns'])} completed turn(s)"

    def search(self) -> None:
        def run() -> str:
            sid = self.t.call("create_session", {"title": "smoke-search"})["id"]
            tid = self.turn(sid, "Use the web_search tool once to find the current stable Python version. Answer in one line.")
            ev = self.follow(tid, until=lambda e: e["type"] in ("turn_done", "turn_error"))
            res = [e["payload"] for e in ev if e["type"] == "tool_result" and e["payload"].get("name") == "web_search"]
            if not res:
                raise RuntimeError("the agent never called web_search")
            if res[0].get("status") not in ("success", "ok"):
                raise RuntimeError(f"web_search failed: {str(res[0].get('preview'))[:300]}")
            return f"web_search succeeded: {str(res[0].get('preview'))[:80]!r}"

        self.check("web search from the runtime (Tavily egress + secret)", run)

    def sandbox(self) -> None:
        def run() -> str:
            sid = self.t.call("create_session", {"title": "smoke-sandbox"})["id"]
            tid = self.turn(sid, "Run the shell command `echo hello-from-sandbox` and tell me its output.")
            ev = self.follow(tid, until=lambda e: e["type"] in ("interrupt", "turn_done", "turn_error"), limit_s=420)
            if ev[-1]["type"] != "interrupt":
                raise RuntimeError(f"expected an approval request, got {ev[-1]['type']}")
            pending = [a for a in self.t.call("list_approvals", {"status": "pending", "session_id": sid})]
            if not pending:
                raise RuntimeError("the turn is waiting but list_approvals shows nothing pending")
            self.t.call("decide", {"approval_id": pending[0]["id"], "decision": "reject", "message": "smoke test: not needed"})
            ev2 = self.follow(tid, until=lambda e: e["type"] in ("turn_done", "turn_error", "interrupt"), limit_s=180)
            if ev2[-1]["type"] == "interrupt":  # the model asked again; reject once more so nothing is left parked
                again = self.t.call("list_approvals", {"status": "pending", "session_id": sid})
                for a in again:
                    self.t.call("decide", {"approval_id": a["id"], "decision": "reject", "message": "smoke test"})
            return "approval requested, rejected, turn continued"

        self.check("shell command waits for approval (sandbox created in the cloud)", run)

    def ok(self) -> bool:
        bad = [n for n, ok in self.results if not ok]
        self.say(f"\n{len(self.results) - len(bad)}/{len(self.results)} checks passed" + (f"; FAILED: {', '.join(bad)}" if bad else ""))
        return not bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="us-central1")
    ap.add_argument("--runtime", help="resource name; default: the one in deploy/.state.json")
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--sandbox", action="store_true")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    name = a.runtime or (json.loads(STATE.read_text()).get("runtime") if STATE.exists() else "")
    if not name:
        print("no runtime recorded: run `deploy/deploy.sh deploy` first (or pass --runtime)", file=sys.stderr)
        return 2
    print(f"runtime: {name}")
    s = Smoke(RestTransport(name, a.region))
    s.basics()
    if a.search or a.all:
        s.search()
    if a.sandbox or a.all:
        s.sandbox()
    return 0 if s.ok() else 1


if __name__ == "__main__":
    raise SystemExit(main())
