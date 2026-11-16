#!/usr/bin/env python3
"""Read-only GitHub App diagnostic: shows exactly what GitHub answers for each way of minting an installation token.

    uv run python scripts/debug_github.py [OWNER/REPO]

Prints status codes, request ids and permission names. Never prints a token or the private key. Creates nothing.
"""
from __future__ import annotations

import sys

import jwt
import requests

from pinboard.core.config import Settings
from pinboard.gcp.vault import Vault

API = "https://api.github.com"
H = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def show(label: str, r: requests.Response, extra: str = "") -> None:
    body = r.text.strip().replace("\n", " ")[:300] or "(empty body)"
    print(f"  {label}: HTTP {r.status_code}  request-id={r.headers.get('x-github-request-id')}  {extra}")
    if r.status_code >= 400:
        print(f"      body: {body}")


def main() -> int:
    if len(sys.argv) < 2 or "/" not in sys.argv[1]:
        print("usage: debug_github.py OWNER/REPO   (a repository the GitHub App is installed on)", file=sys.stderr)
        return 2
    repo = sys.argv[1]
    name = repo.split("/", 1)[1]
    s = Settings.from_env()
    vault = Vault(s.secrets_backend, s.gcp_project)
    pem = vault.get("GITHUB_APP_PRIVATE_KEY")
    if not (s.github_app_id and pem):
        print("need GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY (SECRETS_BACKEND=gcp)")
        return 2
    import time

    now = int(time.time())
    app_jwt = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(s.github_app_id)}, pem, algorithm="RS256")
    J = {**H, "Authorization": f"Bearer {app_jwt}"}

    r = requests.get(f"{API}/app", headers=J, timeout=20)
    show("GET /app", r)
    if r.ok:
        a = r.json()
        print(f"      app={a.get('slug')} permissions={a.get('permissions')} events={a.get('events')}")
    r = requests.get(f"{API}/app/installations", headers=J, timeout=20)
    show("GET /app/installations", r)
    inst = r.json()[0] if r.ok and r.json() else None
    if not inst:
        return 1
    print(f"      installation={inst['id']} account={inst['account']['login']} repository_selection={inst.get('repository_selection')} "
          f"permissions={inst.get('permissions')} suspended={inst.get('suspended_at')}")

    variants = {
        "A no body (what test_github_app.py does)": None,
        "B repositories only": {"repositories": [name]},
        "C permissions metadata:read only": {"permissions": {"metadata": "read"}},
        "D contents:read + repositories": {"permissions": {"contents": "read"}, "repositories": [name]},
        "E contents:write + pull_requests:write + repositories": {"permissions": {"contents": "write", "pull_requests": "write"}, "repositories": [name]},
    }
    for label, body in variants.items():
        print(f"\n{label}")
        r = requests.post(f"{API}/app/installations/{inst['id']}/access_tokens", headers=J, json=body, timeout=20)
        show("mint token", r, f"granted={r.json().get('permissions') if r.ok else ''}")
        if r.ok:
            T = {**H, "Authorization": f"Bearer {r.json()['token']}"}
            show("GET /installation/repositories", requests.get(f"{API}/installation/repositories", headers=T, timeout=20))
            show(f"GET /repos/{repo}", requests.get(f"{API}/repos/{repo}", headers=T, timeout=20))

    # F: the connector's OWN code path, with every request logged
    print("\nF the connector class itself (GitHubApp), every request logged")
    from pinboard.integrations.github import GitHubApp, GitHubError

    class Logging(requests.Session):
        def request(self, method, url, **kw):
            r = super().request(method, url, **kw)
            body = kw.get("json")
            print(f"  -> {method} {url.replace(API, '')}  body={None if body is None else sorted(body)}  params={kw.get('params')}  "
                  f"=> HTTP {r.status_code}  request-id={r.headers.get('x-github-request-id')}")
            return r

    app = GitHubApp(s.github_app_id, pem, s.github_installation_id, http=Logging())
    for label, fn in (("token(None) + list repos", lambda: app.api("GET", "/installation/repositories", params={"per_page": 100})),
                      (f"read-scoped GET /repos/{repo}", lambda: app.api("GET", f"/repos/{repo}", repo=repo)),
                      ("write-scoped token", lambda: app.token(repo, "write") and "ok")):
        try:
            fn()
            print(f"  {label}: OK")
        except GitHubError as exc:
            print(f"  {label}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
