"""How the web backend calls the engine. Two implementations of one small interface:

* ``GoogleRuntimeClient``: the deployed Agent Runtime, through ``:query`` / ``:streamQuery`` with this service's own Google credentials.
* ``HttpRuntimeClient``: a ``pinboard serve`` on a URL with the bearer token (local development, and the tests: they point it at the real
  engine app in the same process).

Errors come back as ``RuntimeCallError(status, message)``. Verified on the real platform: Agent Runtime keeps the HTTP status of the
engine's answer (404, 403, 400...) and puts the engine's own message inside ``Error Details: {"detail": "..."}``.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Iterator, Protocol


class RuntimeCallError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class RuntimeClient(Protocol):
    def call(self, method: str, inp: dict) -> Any: ...

    def stream(self, method: str, inp: dict) -> Iterator[dict]: ...


PASS_THROUGH = {400, 403, 404, 409, 422, 429}  # statuses that mean something to the person using the app
_DETAIL = re.compile(r"Error Details:\s*(\{.*\})", re.S)
_GENERIC = {400: "the request was not accepted", 403: "not allowed", 404: "not found", 409: "conflict", 429: "the assistant is busy, try again shortly"}


def _detail_text(detail: Any) -> str:
    if isinstance(detail, list):  # FastAPI validation errors
        return "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)[:300]
    return str(detail)[:300]


def parse_error(status: int, body: str) -> RuntimeCallError:
    """Turn a failed engine call (either shape: Google-wrapped or direct) into an error with a status the browser should see."""
    message = ""
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("error"), dict):  # Agent Runtime's wrapper
            status = int(data["error"].get("code") or status)
            m = _DETAIL.search(str(data["error"].get("message", "")))
            if m:
                try:
                    message = _detail_text(json.loads(m.group(1)).get("detail", ""))
                except ValueError:
                    pass
        elif "detail" in data:  # the engine app answering directly
            message = _detail_text(data["detail"])
    if status in PASS_THROUGH:
        return RuntimeCallError(status, message or _GENERIC.get(status, "request failed"))
    return RuntimeCallError(502, "the assistant is unavailable")  # 401 here would be OUR credentials failing: never blame the user for it


class GoogleRuntimeClient:
    def __init__(self, resource: str, region: str = "us-central1", *, session_factory=None, timeout: float = 120.0, stream_timeout: float = 330.0):
        self.base = f"https://{region}-aiplatform.googleapis.com/v1/{resource}"
        self.timeout, self.stream_timeout = timeout, stream_timeout
        self._factory, self._local = session_factory or self._default_session, threading.local()

    @staticmethod
    def _default_session():
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return AuthorizedSession(creds)

    def _session(self):
        if not hasattr(self._local, "s"):  # one session per worker thread: requests sessions are not meant to be shared
            self._local.s = self._factory()
        return self._local.s

    def call(self, method: str, inp: dict) -> Any:
        import requests

        try:
            r = self._session().post(f"{self.base}:query", json={"classMethod": method, "input": inp}, timeout=self.timeout)
        except requests.Timeout as exc:
            raise RuntimeCallError(504, "the assistant took too long to answer") from exc
        except requests.RequestException as exc:
            raise RuntimeCallError(502, "the assistant is unavailable") from exc
        if r.status_code != 200:
            raise parse_error(r.status_code, r.text)
        return r.json().get("output")

    def stream(self, method: str, inp: dict) -> Iterator[dict]:
        import requests

        try:
            r = self._session().post(f"{self.base}:streamQuery", json={"classMethod": method, "input": inp}, stream=True,
                                     timeout=(10, self.stream_timeout))
        except requests.RequestException as exc:
            raise RuntimeCallError(504 if isinstance(exc, requests.Timeout) else 502, "the assistant is unavailable") from exc
        try:
            if r.status_code != 200:
                raise parse_error(r.status_code, r.text)
            try:
                for line in r.iter_lines(decode_unicode=True):
                    if line:
                        yield _stream_item(json.loads(line))
            except requests.RequestException as exc:
                raise RuntimeCallError(502, "the connection to the assistant was lost") from exc
        finally:
            r.close()


def _stream_item(obj: dict) -> dict:
    if "error" in obj:  # an error in the middle of a stream is reported in-band
        raise RuntimeCallError(502, "the assistant stopped unexpectedly")
    return obj["output"]


class HttpRuntimeClient:
    """``http`` is an httpx-style client (httpx.Client, or a Starlette TestClient wrapping the engine app)."""

    def __init__(self, http, token: str):  # timeouts belong to the client you pass in
        self.http, self.headers = http, {"Authorization": f"Bearer {token}"}

    def call(self, method: str, inp: dict) -> Any:
        r = self.http.post("/api/reasoning_engine", json={"class_method": method, "input": inp}, headers=self.headers)
        if r.status_code != 200:
            raise parse_error(r.status_code, r.text)
        return r.json().get("output")

    def stream(self, method: str, inp: dict) -> Iterator[dict]:
        with self.http.stream("POST", "/api/stream_reasoning_engine", json={"class_method": method, "input": inp}, headers=self.headers) as r:
            if r.status_code != 200:
                r.read()
                raise parse_error(r.status_code, r.text)
            for line in r.iter_lines():
                if line:
                    yield _stream_item(json.loads(line))
