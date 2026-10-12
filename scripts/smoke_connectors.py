#!/usr/bin/env python3
"""Smoke-test the REAL AgentMail and GitHub connectors with your real credentials. You run this, not the test suite.

    uv run python scripts/smoke_connectors.py                       # read-only: policy, inbox list, GitHub auth + repo list
    uv run python scripts/smoke_connectors.py --send                # also sends ONE email to the first allowed recipient
    uv run python scripts/smoke_connectors.py --github-write OWNER/REPO
                                                                    # also: branch agent/smoke-<ts> -> commit -> PR, then closes the PR and deletes the branch

Needs: AGENTMAIL_API_KEY (in .env or Secret Manager), GITHUB_APP_ID (in .env), GITHUB_APP_PRIVATE_KEY from Secret Manager
(set SECRETS_BACKEND=gcp in .env and run `gcloud auth application-default login`). Nothing here prints a secret.
This script plays the human approver, so it calls the tools directly (the agent's approval gate is covered by the tests).
"""
from __future__ import annotations

import argparse
import sys
import time

from pinboard.core.config import Settings
from pinboard.integrations.agentmail import AgentMailGateway
from pinboard.agent.tools.email_tools import make_email_tools
from pinboard.integrations.github import GitHubApp
from pinboard.agent.tools.github_tools import make_github_tools
from pinboard.core.policy import load_policy
from pinboard.gcp.vault import Vault

failures = 0


def report(ok: bool, label: str, detail: str = "") -> None:
    global failures
    failures += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f": {detail}" if detail else ""))


def call(tools, name, args, cid):
    t = {x.name: x for x in tools}[name]
    return t.invoke({"name": name, "args": args, "id": cid, "type": "tool_call"}, config={"configurable": {"thread_id": "smoke"}}).content


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--send", action="store_true", help="send one real email to the first allowed recipient")
    ap.add_argument("--github-write", metavar="OWNER/REPO", help="create a branch, commit, open a PR, then clean up")
    args = ap.parse_args()

    s = Settings.from_env()
    vault = Vault(s.secrets_backend, s.gcp_project)
    policy = load_policy()
    print(f"policy: recipients={list(policy.email.allowed_recipients)} inbox={s.agentmail_inbox_id or policy.email.from_inbox} "
          f"repos={list(policy.github.allowed_repos) or 'installation scope'} branch_prefix={policy.github.branch_prefix}")

    # ── AgentMail ────────────────────────────────────────────────────────
    key = vault.get("AGENTMAIL_API_KEY")
    if not key:
        report(False, "AgentMail", "AGENTMAIL_API_KEY not found")
    else:
        inbox = s.agentmail_inbox_id or policy.email.from_inbox
        tools, _ = make_email_tools(AgentMailGateway(key), policy, inbox, outputs_path=s.outputs_path)
        out = call(tools, "list_emails", {"limit": 3}, "s1")
        report(not out.startswith("Could not"), f"AgentMail list_emails on {inbox}", out.splitlines()[0][:120] if out else "")
        if args.send:
            to = policy.email.allowed_recipients[0]
            out = call(tools, "send_email", {"to": [to], "subject": "Pinboard smoke test", "body": "If you can read this, the AgentMail connector works."}, f"s2-{int(time.time())}")
            report(out.startswith("Email sent"), "AgentMail send_email", out)
        else:
            print("(skipped sending; re-run with --send)")

    # ── GitHub ───────────────────────────────────────────────────────────
    pem = vault.get("GITHUB_APP_PRIVATE_KEY")
    if not (s.github_app_id and pem):
        report(False, "GitHub", "GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY not found (is SECRETS_BACKEND=gcp?)")
    else:
        app = GitHubApp(s.github_app_id, pem, s.github_installation_id)
        try:
            report(True, "GitHub app authenticated; installation", str(app.installation_id()))
        except Exception as exc:  # noqa: BLE001
            report(False, "GitHub installation lookup", str(exc))
            return 1
        tools, _ = make_github_tools(app, policy, outputs_path=s.outputs_path)
        out = call(tools, "github_list_repos", {}, "g1")
        report("failed" not in out.split("\n")[0], "GitHub github_list_repos", out.replace("\n", " | ")[:600])
        if args.github_write:
            repo, branch = args.github_write, f"agent/smoke-{int(time.time())}"
            out = call(tools, "github_create_branch", {"repo": repo, "branch": branch}, "g2")
            report(out.startswith("Created"), "GitHub create branch", out)
            out = call(tools, "github_commit_files", {"repo": repo, "branch": branch, "message": "Pinboard smoke test",
                                                      "files": [{"path": "docs/pinboard-smoke-test.md", "content": "# smoke test\n"}]}, "g3")
            report(out.startswith("Committed"), "GitHub commit", out)
            out = call(tools, "github_open_pr", {"repo": repo, "head": branch, "title": "Pinboard smoke test (safe to close)", "body": "Opened by scripts/smoke_connectors.py"}, "g4")
            report(out.startswith("Opened"), "GitHub open PR", out)
            # also prove the guard rails against the REAL service
            denied = call(tools, "github_create_branch", {"repo": repo, "branch": "main"}, "g5")
            report("Blocked by policy" in denied, "GitHub refuses a branch outside the agent prefix", denied[:100])
            try:  # cleanup: close the PR, delete the branch
                num = int(out.split("#")[1].split(":")[0]) if out.startswith("Opened") else None
                if num:
                    app.api("PATCH", f"/repos/{repo}/pulls/{num}", repo=repo, access="write", json={"state": "closed"})
                app.api("DELETE", f"/repos/{repo}/git/refs/heads/{branch}", repo=repo, access="write")
                report(True, "cleanup", f"closed PR #{num} and deleted {branch}")
            except Exception as exc:  # noqa: BLE001
                report(False, "cleanup", f"{exc}. Delete branch {branch} in {repo} by hand.")
        else:
            print("(skipped GitHub writes; re-run with --github-write OWNER/REPO)")

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
