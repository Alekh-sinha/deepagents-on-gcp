#!/usr/bin/env python3
"""Check a web backend from OUTSIDE, the way a browser would meet it, including the parts only the real Cloud Run + IAP front end can show.

  Against the deployed service (signs an IAP-accepted request as a service account; see https://docs.cloud.google.com/iap/docs/authentication-howto):
      uv run python scripts/smoke_web.py --url https://SERVICE-xxxxx.run.app --sa SERVICE-ACCOUNT-EMAIL [--long]
  Against a local `pinboard web` started with WEB_AUTH=dev:
      uv run python scripts/smoke_web.py --url http://127.0.0.1:8081 --local

Checks (each PASS/FAIL):
  * IAP: an anonymous request is refused; a request with a token for ANOTHER audience is refused;
  * the backend verified IAP's signed header: /api/me answers with the signed-in identity's e-mail;
  * a real turn streams INCREMENTALLY through the front end (token messages spread over time, not delivered in one lump at the end);
  * reconnecting with Last-Event-ID replays only the events after that one;
  * with --long: a long essay streams for a good while;
  * with --hold SECONDS: a connection held open that long (a tick every 5 s) is not cut by Cloud Run, IAP or anything in between.
Exit code 0 only when everything passed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

CSRF = {"X-Requested-With": "pinboard"}
SHORT_PROMPT = "Write about 250 words on why an agent's state must survive a restart."
LONG_PROMPT = "Answer directly in this chat, without using any tool or file: write a detailed, well structured essay of at least 1500 words on how production AI agents should handle failure, retries and human approval."


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        return ok

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


# ── pieces ───────────────────────────────────────────────────────────────

def iap_headers(sa_email: str, audience: str, *, signer: Callable[[str, dict], str] | None = None) -> dict:
    """Authorization header IAP accepts for a service account: a JWT the service account signed (via the IAM Credentials API), aud = the URL."""
    now = int(time.time())
    payload = {"iss": sa_email, "sub": sa_email, "aud": audience, "iat": now, "exp": now + 3000}
    return {"Authorization": f"Bearer {(signer or sign_jwt)(sa_email, payload)}"}


def sign_jwt(sa_email: str, payload: dict) -> str:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    r = AuthorizedSession(creds).post(f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa_email}:signJwt",
                                      json={"payload": json.dumps(payload)}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"could not sign a JWT as {sa_email}: HTTP {r.status_code} {r.text[:300]} (the caller needs roles/iam.serviceAccountTokenCreator on it)")
    return r.json()["signedJwt"]


def read_sse(resp, report_chunks: list | None = None):
    """Yield (arrival_seconds, event, id, data) for each SSE message of a streaming `requests` response, timing the moment each one arrives."""
    t0 = time.monotonic()
    ev, id_, data = None, None, []
    for raw in resp.iter_lines(chunk_size=1, decode_unicode=True):
        line = raw or ""
        if line == "":
            if data:
                yield time.monotonic() - t0, ev or "message", id_, "\n".join(data)
            ev, id_, data = None, None, []
        elif line.startswith(":"):
            if report_chunks is not None:
                report_chunks.append((time.monotonic() - t0, line))
        elif line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("id:"):
            id_ = line[3:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


# ── the checks ───────────────────────────────────────────────────────────

def check_stream(http, base: str, headers: dict, report: Report, *, prompt: str, min_tokens: int = 3, min_spread: float = 1.5, label: str = "stream") -> str | None:
    """Start a turn, follow it live, and judge whether it really arrived in pieces. Returns the turn id."""
    h = {**headers, **CSRF}
    s = http.post(f"{base}/api/sessions", json={"title": "smoke-web"}, headers=h, timeout=60)
    if not report.add(f"{label}: create a conversation", s.status_code == 200, f"HTTP {s.status_code} {s.text[:120] if s.status_code != 200 else ''}"):
        return None
    sid = s.json()["id"]
    t = http.post(f"{base}/api/sessions/{sid}/messages", json={"text": prompt}, headers=h, timeout=120)
    if not report.add(f"{label}: send a message", t.status_code == 200 and t.json().get("status") in ("running", "completed"), f"HTTP {t.status_code} status={t.json().get('status') if t.status_code == 200 else t.text[:120]}"):
        return None
    turn = t.json()["id"]
    comments: list = []
    msgs = []
    started = time.monotonic()
    with http.get(f"{base}/api/turns/{turn}/stream", headers=headers, stream=True, timeout=(15, 330)) as r:
        ctype = r.headers.get("content-type", "")
        if not report.add(f"{label}: the stream opens as text/event-stream", r.status_code == 200 and ctype.startswith("text/event-stream"), f"HTTP {r.status_code} {ctype}"):
            return turn
        for item in read_sse(r, comments):
            msgs.append(item)
            if item[1] in ("end", "error"):
                break
    total = time.monotonic() - started
    tokens = [m for m in msgs if m[1] == "message" and json.loads(m[3]).get("type") == "token"]
    types = [json.loads(m[3]).get("type") for m in msgs if m[1] == "message"]
    spread = (tokens[-1][0] - tokens[0][0]) if len(tokens) >= 2 else 0.0
    report.add(f"{label}: tokens arrive incrementally, not in one lump", len(tokens) >= min_tokens and spread >= min_spread,
               f"{len(tokens)} token messages over {spread:.1f}s (first at {tokens[0][0]:.1f}s, last at {tokens[-1][0]:.1f}s)" if tokens else "no token messages")
    report.add(f"{label}: the turn finishes and the stream ends politely", "turn_done" in types and msgs[-1][1] == "end", f"{len(msgs)} messages in {total:.1f}s; last = {msgs[-1][1] if msgs else None}; heartbeats seen: {len(comments)}")
    return turn


def check_resume(http, base: str, headers: dict, report: Report, turn: str) -> None:
    seqs = []
    with http.get(f"{base}/api/turns/{turn}/stream", headers=headers, stream=True, timeout=(15, 120)) as r:
        for _, ev, id_, data in read_sse(r):
            if ev == "message":
                seqs.append(int(id_))
            if ev == "end":
                break
    if len(seqs) < 3:
        report.add("resume: replay of a finished turn", False, f"only {len(seqs)} events")
        return
    cut = seqs[len(seqs) // 2]
    later = []
    with http.get(f"{base}/api/turns/{turn}/stream", headers={**headers, "Last-Event-ID": str(cut)}, stream=True, timeout=(15, 120)) as r:
        for _, ev, id_, data in read_sse(r):
            if ev == "message":
                later.append(int(id_))
            if ev == "end":
                break
    report.add("resume: Last-Event-ID replays exactly the events after that one", later == [s for s in seqs if s > cut] and bool(later), f"cut at {cut}: got {later}, expected {[s for s in seqs if s > cut]}")


def check_hold(http, base: str, headers: dict, report: Report, seconds: int, every: float = 5.0) -> None:
    """Hold one SSE connection open for ``seconds`` and see whether every tick arrives on time and the stream ends by itself."""
    ticks, end, started, gaps, last = 0, None, time.monotonic(), [], time.monotonic()
    with http.get(f"{base}/_diag/hold", params={"seconds": seconds, "every": every}, headers=headers, stream=True, timeout=(15, seconds + 120)) as r:
        if r.status_code != 200:
            report.add(f"hold {seconds}s: the connection opens", False, f"HTTP {r.status_code} {r.text[:120]}")
            return
        for _, ev, _, data in read_sse(r):
            now = time.monotonic()
            if ev == "message":
                ticks += 1
                gaps.append(now - last)
                last = now
            elif ev == "end":
                end = json.loads(data)
                break
    held = time.monotonic() - started
    expected = int(seconds // every)
    report.add(f"hold {seconds}s: the connection stays open and every tick arrives", end is not None and abs(ticks - expected) <= 1 and held >= seconds * 0.95,
               f"{ticks}/{expected} ticks, held {held:.0f}s, longest gap {max(gaps, default=0):.1f}s, ended by itself: {end is not None}")


def run(base: str, *, sa: str | None, local: bool, long: bool, hold: int = 0, http=None) -> Report:
    import requests

    http = http or requests.Session()
    rep = Report()
    base = base.rstrip("/")
    if local:
        headers: dict = {}
    else:
        def refused(r) -> bool:  # "refused" = IAP answered itself and our app never saw the request (IAP uses 302 for browsers, a bare 404/401/403 for others)
            return r.status_code != 200 and "pinboard" not in r.text.lower() and '"status":"ok"' not in r.text.replace(" ", "")
        anon = http.get(f"{base}/healthz", timeout=30, allow_redirects=False)
        rep.add("IAP: an anonymous request is refused", refused(anon), f"HTTP {anon.status_code}, answered by IAP: {anon.headers.get('x-goog-iap-generated-response') == 'true'}")
        browser = http.get(f"{base}/", headers={"Accept": "text/html"}, timeout=30, allow_redirects=False)
        rep.add("IAP: a browser is sent to Google sign-in", browser.status_code == 302 and "accounts.google.com" in browser.headers.get("location", ""),
                f"HTTP {browser.status_code} -> {browser.headers.get('location', '')[:60]}")
        wrong = http.get(f"{base}/healthz", headers=iap_headers(sa, "https://example.invalid/not-this-service"), timeout=30, allow_redirects=False)
        rep.add("IAP: a token for another audience is refused", refused(wrong), f"HTTP {wrong.status_code}")
        headers = iap_headers(sa, base + "/*")  # IAP matches `aud` against the requested URL: the wildcard covers every path of this service
    me = http.get(f"{base}/api/me", headers=headers, timeout=60)
    expected = sa if sa else None
    if me.status_code == 200:
        who = me.json().get("user")
        rep.add("sign-in: the backend verified the IAP header and knows who is calling", bool(who) and (expected is None or who == expected.lower()), f"/api/me says user={who}")
    else:
        rep.add("sign-in: the backend verified the IAP header and knows who is calling", False, f"HTTP {me.status_code} {me.text[:200]}")
        return rep
    turn = check_stream(http, base, headers, rep, prompt=SHORT_PROMPT, label="stream")
    if turn:
        check_resume(http, base, headers, rep, turn)
    if long:
        check_stream(http, base, headers, rep, prompt=LONG_PROMPT, min_tokens=10, min_spread=10.0, label="long stream")
    if hold:
        check_hold(http, base, headers, rep, hold)
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="the service URL (the IAP token's audience is this exact URL)")
    ap.add_argument("--sa", help="service account to sign the request as; it needs roles/iap.httpsResourceAccessor on the service")
    ap.add_argument("--local", action="store_true", help="a local backend with WEB_AUTH=dev: no IAP checks")
    ap.add_argument("--long", action="store_true", help="also stream a long essay")
    ap.add_argument("--hold", type=int, default=0, metavar="SECONDS", help="also hold one live connection open this long (try 330: past Cloud Run's and IAP's usual limits)")
    a = ap.parse_args(argv)
    if not a.local and not a.sa:
        ap.error("--sa is required unless --local")
    rep = run(a.url, sa=a.sa, local=a.local, long=a.long, hold=a.hold)
    print(f"\n{sum(c.ok for c in rep.checks)}/{len(rep.checks)} checks passed")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
