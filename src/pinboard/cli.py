"""Command line: run the agent, check the sandbox, manage skills and secrets."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid

from pinboard.core.config import SECRET_NAMES, Settings
from pinboard.gcp.vault import SecretNotFound, Vault, push_secrets


def _settings() -> Settings:
    return Settings.from_env()


def _vault(s: Settings) -> Vault:
    return Vault(s.secrets_backend, s.gcp_project)


def _interactive_approver(requests: list[dict]) -> list[dict]:
    """Terminal reviewer: shows each pending action in a readable form; questions get a typed answer, everything else y/N."""
    from pinboard.agent.approvals import describe

    decisions = []
    for r in requests:
        d = describe(r)
        print(f"\n[{d['kind']}] {d['title']}")
        print(json.dumps(d["preview"], indent=2, ensure_ascii=False))
        if "respond" in d["allowed_decisions"] and "approve" not in d["allowed_decisions"]:
            answer = input("Your answer (empty = no answer): ").strip()
            decisions.append({"type": "respond", "message": answer or "The user chose not to answer."})
            continue
        ans = input("Approve? [y/N] ").strip().lower()
        decisions.append({"type": "approve"} if ans == "y" else {"type": "reject", "message": "Rejected by user"})
    return decisions


def cmd_run(args) -> int:
    from pinboard.agent import build_agent, run

    s = _settings()
    rt = build_agent(s, _vault(s))
    try:
        thread = args.thread or uuid.uuid4().hex[:8]
        res = run(rt, args.prompt, thread_id=thread, approver=_interactive_approver)  # connector writes are always gated, whatever APPROVAL_MODE says
        print(res.output)
        if res.status == "completed":
            from pinboard.gcp.outputs import harvest

            saved, skipped = harvest(rt.sandbox, s, thread)
            for path in saved:
                print(f"[saved] {path}")
            if not saved:
                print(f"[no files found in {s.outputs_path}; nothing saved]")
            for item in skipped:
                print(f"[skipped] {item}")
        if res.status == "awaiting_approval":
            print(f"\n[parked] thread {thread} is waiting for approval of: {json.dumps(res.pending)}")
        return 0 if res.status == "completed" else 2
    finally:
        rt.close()


def cmd_ping(args) -> int:
    """Create a sandbox and verify everything the agent relies on."""
    from pinboard.core.guardrails import CommandGuard
    from pinboard.gcp.sandbox import SandboxManager

    s = _settings()
    mgr = SandboxManager(s, guard=CommandGuard(s.command_policy, s.sandbox_cwd))
    if not s.sandbox_runtime:
        print("note: SANDBOX_RUNTIME is empty in .env, so a NEW runtime will be created. "
              "Set it after this run to avoid piling them up.")
    ok = True
    try:
        try:
            sb = mgr.open()
        except RuntimeError as exc:
            print(f"\nSANDBOX DID NOT START: {exc}")
            return 1
        print(f"sandbox: {sb.id}")
        checks = {
            "shell": "echo ok",
            "whoami / cwd": "whoami && pwd",
            "python3 (needed by BaseSandbox file tools)": "python3 --version",
            "base64 (needed for uploads)": "echo aGk= | base64 -d",
            "tr / wc": "echo hi | tr a-z A-Z && echo hi | wc -c",
            "internet (expected OFF)": "python3 -c \"import urllib.request as u; u.urlopen('https://example.com', timeout=4); print('REACHABLE')\" 2>&1 | tail -1",
        }
        for label, cmd in checks.items():
            r = sb.execute(cmd)
            if "internet" in label:
                reachable = "REACHABLE" in r.output
                print(f"  [{'WARN' if reachable else 'ok'}] {label}: {'internet IS reachable' if reachable else 'blocked, as intended'}")
                continue
            print(f"  [{'ok' if r.exit_code == 0 else 'FAIL'}] {label}: {r.output.strip()[:120]}")
            if r.exit_code != 0:
                ok = False
        path = f"{s.sandbox_cwd.rstrip('/')}/_ping.txt"
        w = sb.write(path, "ping\n")
        rd = sb.read(path)
        roundtrip = w.error is None and rd.error is None and "ping" in str((rd.file_data or {}).get("content", ""))
        print(f"  [{'ok' if roundtrip else 'FAIL'}] file write/read through deepagents tools: {w.error or rd.error or 'good'}")
        ok = ok and roundtrip
    finally:
        mgr.close()
    print("\nSANDBOX READY" if ok else "\nSANDBOX HAS PROBLEMS: see FAIL lines above")
    return 0 if ok else 1


def cmd_skills_upload(args) -> int:
    from pinboard.gcp.skills import upload_skills_dir

    s = _settings()
    target = args.to or (s.skills_source if (s.skills_source or "").startswith("gs://") else None)
    if not target:
        print("Provide --to gs://bucket/prefix or set SKILLS_SOURCE=gs://... in .env", file=sys.stderr)
        return 2
    n = upload_skills_dir(args.folder, target)
    print(f"uploaded {n} files from {args.folder} to {target}")
    return 0


def cmd_secrets_push(args) -> int:
    s = _settings()
    if not s.gcp_project:
        print("GCP_PROJECT must be set", file=sys.stderr)
        return 2
    for name, status in push_secrets(SECRET_NAMES, s.gcp_project).items():
        print(f"  {name}: {status}")
    print("\nNext: set SECRETS_BACKEND=gcp and replace values in .env with secret://NAME (or delete them).")
    return 0


def cmd_runtimes(args) -> int:
    import agentplatform

    from pinboard.gcp.runtimes import delete_runtimes, list_runtimes

    s = _settings()
    if not s.gcp_project:
        print("GCP_PROJECT must be set", file=sys.stderr)
        return 2
    client = agentplatform.Client(project=s.gcp_project, location=s.gcp_location)
    if not args.delete:
        for r in list_runtimes(client, s.sandbox_runtime):
            tag = "IN USE" if r.in_use else ("created by pinboard" if r.ours else "NOT created by pinboard")
            print(f"{r.name}\n    name={r.display_name or '(none)'}  created={r.created}  [{tag}]")
        return 0

    def confirm(info) -> bool:
        warn = "" if info.ours else "\n  WARNING: this runtime was NOT created by pinboard."
        print(f"About to delete {info.name} (name={info.display_name or '(none)'}, created={info.created}) "
              f"including its sandboxes and templates.{warn}")
        return input("Type 'delete' to confirm: ").strip() == "delete"

    for name, status in delete_runtimes(client, args.delete, s.sandbox_runtime, confirm).items():
        print(f"  {name}: {status}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from pinboard.engine.api import create_app
    from pinboard.engine.service import RunService
    from pinboard.state import make_state

    s = _settings()
    vault = _vault(s)
    token = vault.get("PINBOARD_API_TOKEN", required=True)
    if s.state_backend == "memory":
        print("WARNING: STATE_BACKEND=memory. Runs, approvals and checkpoints are lost on restart, so a parked "
              "approval cannot survive one. Use STATE_BACKEND=postgres with DATABASE_URL for anything real.", file=sys.stderr)
    from pinboard.core.policy import load_policy
    from pinboard.engine.worker import ThreadExecutor

    policy = load_policy()  # fails closed: the server does not start without a valid policy file
    state = make_state(s, vault)
    service = RunService(s, vault, state.store, state.checkpointer, policy=policy, ledger=state.ledger,
                         executor=ThreadExecutor(policy.limits.max_concurrent_turns))
    app = create_app(service, token, trust_platform_auth=s.trust_platform_auth)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        state.close()
    return 0


def cmd_web(args) -> int:
    from pinboard.web.main import serve

    serve(args.host, args.port)
    return 0


def cmd_db_setup(args) -> int:
    from pinboard.state import make_state

    s = _settings()
    if s.state_backend != "postgres":
        print("STATE_BACKEND is not 'postgres'; nothing to set up.")
        return 0
    state = make_state(s, _vault(s))  # creates pinboard tables and the LangGraph checkpoint tables (idempotent)
    state.close()
    print("database ready: pb_runs, pb_approvals, pb_audit and the LangGraph checkpoint tables exist")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="pinboard")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the agent on a prompt")
    r.add_argument("prompt")
    r.add_argument("--thread", help="conversation thread id")
    r.set_defaults(fn=cmd_run)

    sub.add_parser("ping", help="create a sandbox and verify it works").set_defaults(fn=cmd_ping)

    sk = sub.add_parser("skills-upload", help="upload a local skills folder to Cloud Storage")
    sk.add_argument("folder")
    sk.add_argument("--to", help="gs://bucket/prefix (defaults to SKILLS_SOURCE)")
    sk.set_defaults(fn=cmd_skills_upload)

    rt = sub.add_parser("runtimes", help="list Agent Platform runtimes, or delete ones you name")
    rt.add_argument("--delete", nargs="+", metavar="NAME", help="full resource name(s) to delete (asks to confirm)")
    rt.set_defaults(fn=cmd_runtimes)

    sv = sub.add_parser("serve", help="run the HTTP API")
    sv.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 inside a container")
    sv.add_argument("--port", type=int, default=8080)
    sv.set_defaults(fn=cmd_serve)

    wb = sub.add_parser("web", help="run the web backend (the public face: sign-in check + relay to the engine); configured by environment variables")
    wb.add_argument("--host", default="0.0.0.0")
    wb.add_argument("--port", type=int, default=None)
    wb.set_defaults(fn=cmd_web)

    sub.add_parser("db-setup", help="create database tables (idempotent)").set_defaults(fn=cmd_db_setup)

    sub.add_parser("secrets-push", help="copy secrets from .env into Secret Manager").set_defaults(fn=cmd_secrets_push)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (SecretNotFound, ValueError, RuntimeError) as exc:
        if os.environ.get("PINBOARD_DEBUG"):
            raise
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        print(f"error: {msg}\n(set PINBOARD_DEBUG=1 to see the full traceback)", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
