"""GitHub App API client: RS256 JWT -> installation token (scoped per repo, cached) -> REST, with safe retries.
No LangChain and no policy here: the tools in agent/tools/github_tools.py decide what the agent may do with it.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import jwt
import requests

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
TOKEN_REFRESH_MARGIN_S = 300
RETRY_STATUS = (500, 502, 503, 504)
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = (0.5, 1.5)
# Calls that are safe to repeat after a transient failure: reads, token minting, content-addressed Git Data objects, moving a ref to the same sha.
_SAFE_TO_RETRY_SUFFIXES = ("/access_tokens", "/git/blobs", "/git/trees", "/git/commits")


class GitHubError(Exception):
    def __init__(self, status: int, message: str, *, method: str = "", path: str = "", request_id: str | None = None):
        where = f" on {method} {path}" if method else ""
        rid = f" (request-id {request_id})" if request_id else ""
        super().__init__(f"GitHub {status}{where}{rid}: {message}")
        self.status, self.message, self.method, self.path, self.request_id = status, message, method, path, request_id


class GitHubApp:
    def __init__(self, app_id: str, private_key: str, installation_id: str | int | None = None, *,
                 http=None, clock: Callable[[], float] = time.time, base: str = API, sleep: Callable[[float], None] = time.sleep):
        self.app_id, self._key, self._installation = str(app_id), private_key, (int(installation_id) if installation_id else None)
        self._http = http or requests.Session()
        self._clock, self._base, self._sleep = clock, base, sleep
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}

    # ── auth ─────────────────────────────────────────────────────────────
    def _jwt(self) -> str:
        now = int(self._clock())
        return jwt.encode({"iat": now - 60, "exp": now + 9 * 60, "iss": self.app_id}, self._key, algorithm="RS256")

    @staticmethod
    def _retryable(method: str, path: str) -> bool:
        return method in ("GET", "PATCH") or path.endswith(_SAFE_TO_RETRY_SUFFIXES)

    def _raw(self, method: str, path: str, *, bearer: str, json: dict | None = None, params: dict | None = None) -> Any:
        attempts = RETRY_ATTEMPTS if self._retryable(method, path) else 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                r = self._http.request(method, f"{self._base}{path}", headers={**HEADERS, "Authorization": f"Bearer {bearer}"},
                                       json=json, params=params, timeout=20)
            except requests.RequestException as exc:  # network trouble: same retry rule as a 5xx
                if last:
                    raise GitHubError(0, f"{type(exc).__name__}: {str(exc)[:200]}", method=method, path=path) from exc
                self._sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
                continue
            if r.status_code in RETRY_STATUS and not last:
                self._sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
                continue
            break
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except Exception:  # noqa: BLE001
                msg = r.text
            rid = (getattr(r, "headers", None) or {}).get("x-github-request-id")
            raise GitHubError(r.status_code, (str(msg).strip() or "(empty response body)")[:300], method=method, path=path, request_id=rid)
        return r.json() if r.content else {}

    def installation_id(self) -> int:
        if self._installation is None:
            found = self._raw("GET", "/app/installations", bearer=self._jwt())
            if not found:
                raise GitHubError(404, "the GitHub App is not installed anywhere")
            if len(found) > 1:
                raise GitHubError(409, "the GitHub App has several installations; set GITHUB_INSTALLATION_ID")
            self._installation = int(found[0]["id"])
        return self._installation

    def token(self, repo: str | None = None, access: str = "read") -> str:
        """Installation token scoped to one repo (name part) and to read or write permissions. Cached until near expiry."""
        name = repo.split("/", 1)[1] if repo and "/" in repo else repo
        key = (name or "*", access)
        cached = self._tokens.get(key)
        if cached and cached[1] - self._clock() > TOKEN_REFRESH_MARGIN_S:
            return cached[0]
        perms = ({"contents": "write", "pull_requests": "write"} if access == "write" else {"contents": "read"}) if name else {"metadata": "read"}
        body: dict[str, Any] = {"permissions": perms}
        if name:
            body["repositories"] = [name]
        res = self._raw("POST", f"/app/installations/{self.installation_id()}/access_tokens", bearer=self._jwt(), json=body)
        expires = _parse_ts(res.get("expires_at")) or self._clock() + 3600
        self._tokens[key] = (res["token"], expires)
        return res["token"]

    def api(self, method: str, path: str, *, repo: str | None = None, access: str = "read", json: dict | None = None, params: dict | None = None) -> Any:
        return self._raw(method, path, bearer=self.token(repo, access), json=json, params=params)


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime, timezone

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None
