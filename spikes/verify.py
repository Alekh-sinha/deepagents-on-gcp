#!/usr/bin/env python3
"""Agent Runtime spike: verify the platform assumptions BEFORE building Phase 2. See README.md.

    uv run python spikes/verify.py preflight          # read-only, free
    uv run python spikes/verify.py setup              # enable APIs, Artifact Registry repo, runtime service account
    uv run python spikes/verify.py sql-up             # small Cloud SQL Postgres (BILLED while it exists)
    uv run python spikes/verify.py build              # Cloud Build the probe image
    uv run python spikes/verify.py deploy             # create the Agent Runtime from that image
    uv run python spikes/verify.py probe [--long]     # run every check, print PASS/FAIL, write spikes/results.json
    uv run python spikes/verify.py down               # delete EVERYTHING this script created (asks first)

Every resource is named with PREFIX and labelled spike=pinboard. State is kept in spikes/.state.json.
"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
STATE = HERE / ".state.json"
RESULTS = HERE / "results.json"
PREFIX = "pbspike"
APIS = ["aiplatform", "artifactregistry", "cloudbuild", "secretmanager", "sqladmin", "iam", "cloudresourcemanager"]


# ── helpers ──────────────────────────────────────────────────────────────
def sh(cmd: list[str], check: bool = True, quiet: bool = False, retries: int = 0) -> str:
    """Run a command. retries: re-run on failure (IAM changes take a few seconds to propagate)."""
    if not quiet:
        print("  $", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    for attempt in range(retries):
        if p.returncode == 0:
            break
        wait = 5 * (attempt + 1)
        print(f"    failed, retrying in {wait}s ({attempt + 1}/{retries})...")
        time.sleep(wait)
        p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        sys.exit(f"command failed ({p.returncode}):\n{p.stderr.strip() or p.stdout.strip()}")
    return (p.stdout or "").strip() if p.returncode == 0 else ""


def load() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(**kv) -> dict:
    s = {**load(), **kv}
    STATE.write_text(json.dumps(s, indent=2))
    return s


def cfg(args) -> dict:
    project = args.project or sh(["gcloud", "config", "get-value", "project"], quiet=True)
    if not project:
        sys.exit("no project: pass --project or run `gcloud config set project ...`")
    number = sh(["gcloud", "projects", "describe", project, "--format=value(projectNumber)"], quiet=True)
    return {"project": project, "number": number, "location": args.location,
            "repo": f"{PREFIX}-repo", "sa": f"{PREFIX}-runtime", "sql": f"{PREFIX}-pg", "secret": f"{PREFIX}-db-password",
            "image": f"{args.location}-docker.pkg.dev/{project}/{PREFIX}-repo/probe:latest"}


def client(c: dict):
    import agentplatform

    return agentplatform.Client(project=c["project"], location=c["location"])


# ── steps ────────────────────────────────────────────────────────────────
def preflight(args) -> None:
    c = cfg(args)
    print(f"account : {sh(['gcloud', 'config', 'get-value', 'account'], quiet=True)}")
    print(f"project : {c['project']} (number {c['number']})   location: {c['location']}")
    enabled = set(sh(["gcloud", "services", "list", "--enabled", "--project", c["project"], "--format=value(config.name)"], quiet=True).split())
    for a in APIS + ["run"]:
        print(f"  API {a}.googleapis.com: {'enabled' if f'{a}.googleapis.com' in enabled else 'NOT enabled (setup enables it)'}")
    print(f"docker: {'yes' if shutil.which('docker') else 'no'} (not needed: Cloud Build builds the image)")
    print(f"terraform: {'yes' if shutil.which('terraform') else 'no'} (not needed for this spike)")
    print("\nCheck the account/project above are the ones you intend before running `setup`.")


def grant_service_agents(c: dict) -> None:
    """Let Google's Agent Runtime service agents pull our image. They may not exist until the first deploy."""
    for agent in ("gcp-sa-aiplatform-re", "gcp-sa-aiplatform"):
        member = f"serviceAccount:service-{c['number']}@{agent}.iam.gserviceaccount.com"
        ok = sh(["gcloud", "projects", "add-iam-policy-binding", c["project"], f"--member={member}",
                 "--role=roles/artifactregistry.reader", "--condition=None", "--quiet"], check=False, quiet=True)
        print(f"  artifactregistry.reader -> {agent}: {'granted' if ok else 'NOT granted (service agent does not exist yet; deploy retries)'}")


def setup(args) -> None:
    c = cfg(args)
    P, L = c["project"], c["location"]
    sh(["gcloud", "services", "enable", *[f"{a}.googleapis.com" for a in APIS], "--project", P])
    if not sh(["gcloud", "artifacts", "repositories", "describe", c["repo"], "--location", L, "--project", P], check=False, quiet=True):
        sh(["gcloud", "artifacts", "repositories", "create", c["repo"], "--repository-format=docker", "--location", L,
            "--labels=spike=pinboard", "--project", P])
    sa_email = f"{c['sa']}@{P}.iam.gserviceaccount.com"
    if not sh(["gcloud", "iam", "service-accounts", "describe", sa_email, "--project", P], check=False, quiet=True):
        sh(["gcloud", "iam", "service-accounts", "create", c["sa"], "--display-name=pinboard spike runtime", "--project", P])
    for role in ("roles/aiplatform.user", "roles/secretmanager.secretAccessor", "roles/cloudsql.client", "roles/logging.logWriter"):
        sh(["gcloud", "projects", "add-iam-policy-binding", P, f"--member=serviceAccount:{sa_email}", f"--role={role}",
            "--condition=None", "--quiet"], quiet=True, retries=6)
    grant_service_agents(c)
    save(sa=sa_email)
    print(f"\nsetup done. runtime service account: {sa_email}")


def sql_up(args) -> None:
    c = cfg(args)
    P, L = c["project"], c["location"]
    password = secrets.token_urlsafe(24)
    sh(["gcloud", "sql", "instances", "create", c["sql"], "--database-version=POSTGRES_16", "--edition=ENTERPRISE", "--tier=db-f1-micro",
        "--region", L, "--availability-type=zonal", "--no-backup", "--project", P])
    sh(["gcloud", "sql", "databases", "create", "pinboard", "--instance", c["sql"], "--project", P])
    sh(["gcloud", "sql", "users", "create", "pinboard", "--instance", c["sql"], f"--password={password}", "--project", P], quiet=True)
    if not sh(["gcloud", "secrets", "describe", c["secret"], "--project", P], check=False, quiet=True):
        sh(["gcloud", "secrets", "create", c["secret"], "--replication-policy=automatic", "--labels=spike=pinboard", "--project", P])
    p = subprocess.run(["gcloud", "secrets", "versions", "add", c["secret"], "--data-file=-", "--project", P], input=password, text=True, capture_output=True)
    if p.returncode:
        sys.exit(p.stderr)
    save(sql_connection=f"{P}:{L}:{c['sql']}")
    print("Cloud SQL ready; password is only in Secret Manager.")


def build(args) -> None:
    c = cfg(args)
    # New projects run Cloud Build as the Compute Engine default service account, which starts with no permissions.
    sh(["gcloud", "projects", "add-iam-policy-binding", c["project"],
        f"--member=serviceAccount:{c['number']}-compute@developer.gserviceaccount.com",
        "--role=roles/cloudbuild.builds.builder", "--condition=None", "--quiet"], quiet=True, retries=6)
    sh(["gcloud", "builds", "submit", str(HERE / "probe"), "--tag", c["image"], "--region", c["location"], "--project", c["project"]])


METHODS = [{"name": n, "api_mode": ""} for n in ("ping", "sleep", "bg_start", "bg_status", "egress", "secret", "gemini", "db", "db_psycopg")] + \
          [{"name": "stream_beat", "api_mode": "stream"}]


def deploy(args) -> None:
    c = cfg(args)
    st = load()
    grant_service_agents(c)
    base = {
        "display_name": f"{PREFIX}-probe", "labels": {"spike": "pinboard"},
        "container_spec": {"image_uri": c["image"]}, "class_methods": METHODS,
        "service_account": st.get("sa") or f"{c['sa']}@{c['project']}.iam.gserviceaccount.com",
        "env_vars": {"SPIKE": "1", **({"SQL_CONNECTION": st["sql_connection"]} if st.get("sql_connection") else {})}, "min_instances": 1, "max_instances": 2,
    }
    # The docs' examples set agent_framework; whether a container deploy REQUIRES it is unknown. Try the variants in order.
    if st.get("runtime"):  # redeploy: remove the previous one first so nothing is orphaned (and billed)
        print(f"deleting previous runtime {st['runtime']} ...")
        try:
            client(c).runtimes.delete(name=st["runtime"], force=True)
        except Exception as exc:
            print(f"  (could not delete it, continuing: {str(exc)[:200]})")
    variants = [args.framework] if args.framework else [st.get("agent_framework") or "langgraph", "google-adk", None]
    for fw in variants:
        conf = {**base, **({"agent_framework": fw} if fw else {})}
        print(f"\ntrying agent_framework={fw!r} ...")
        try:
            op = client(c).runtimes.create(config=conf)
        except Exception as exc:
            print(f"  FAILED: {str(exc)[:600]}")
            continue
        res = getattr(op, "api_resource", op)
        save(runtime=res.name, agent_framework=fw)
        print(f"deployed: {res.name} (agent_framework={fw!r})")
        return
    sys.exit("all variants failed; send the output above. Failed attempts leave no runtime behind.")


# ── probes ───────────────────────────────────────────────────────────────
class Prober:
    def __init__(self, c: dict, name: str):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self.http = AuthorizedSession(creds)
        self.base = f"https://{c['location']}-aiplatform.googleapis.com/v1/{name}"
        self.c, self.name, self.results = c, name, []

    def record(self, q: str, ok: bool | None, detail: str) -> None:
        tag = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
        print(f"[{tag}] {q}: {detail}")
        self.results.append({"question": q, "ok": ok, "detail": detail})

    def query(self, method: str, inp: dict | None = None, timeout: float = 120):
        r = self.http.post(f"{self.base}:query", json={"classMethod": method, "input": inp or {}}, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:3000]}")
        out = r.json().get("output")
        if isinstance(out, dict) and "error" in out:
            raise RuntimeError(out["error"])
        return out

    def stream(self, method: str, inp: dict, timeout: float):
        r = self.http.post(f"{self.base}:streamQuery", json={"classMethod": method, "input": inp}, stream=True, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:3000]}")
        return [ln for ln in r.iter_lines(decode_unicode=True) if ln]


def probe(args) -> None:
    c, st = cfg(args), load()
    if not st.get("runtime"):
        sys.exit("no runtime in state: run `deploy` first")
    p, P = Prober(c, st["runtime"]), c["project"]

    def attempt(q: str, fn, ok_fmt=lambda o: json.dumps(o)[:300]):
        try:
            p.record(q, True, ok_fmt(fn()))
        except Exception as exc:
            p.record(q, False, str(exc)[:3000])

    attempt("Q1 invoke unary via REST :query (contract route + class_methods)", lambda: p.query("ping"))
    attempt("Q1b invoke stream via REST :streamQuery", lambda: p.stream("stream_beat", {"seconds": 6, "every": 2}, 60),
            lambda lines: f"{len(lines)} lines, first={lines[0][:80]}")
    attempt("Q2 runtime identity (service account / project number / PORT)", lambda: {k: v for k, v in p.query("ping").items() if k != "env_names"})

    if args.only_db:
        p.results = []
        attempt("Q9 psycopg 3 + pool + LangGraph PostgresSaver via Cloud SQL Auth Proxy (localhost)", lambda: p.query(
            "db_psycopg", {"project": P, "user": "pinboard", "db": "pinboard", "password_secret": c["secret"]}, timeout=120))
        RESULTS.write_text(json.dumps(p.results, indent=2))
        return
    if args.quick:
        RESULTS.write_text(json.dumps(p.results, indent=2))
        return print("\n--quick: stopped after Q1/Q1b/Q2")

    # Q3 timeouts: how long can ONE request run, unary and streaming?
    durations = [30, 60, 120, 300] + ([600, 900] if args.long else [])
    for d in durations:
        attempt(f"Q3 unary request held open {d}s", lambda d=d: p.query("sleep", {"seconds": d}, timeout=d + 60), lambda o: "completed")
    for d in ([300] + ([900, 1800] if args.long else [])):
        attempt(f"Q3 streaming request held open {d}s (beat every 10s)", lambda d=d: p.stream("stream_beat", {"seconds": d, "every": 10}, d + 120),
                lambda lines: f"{len(lines)} lines, last={lines[-1][:80]}")

    # Q4 does work continue after the response is sent? (background thread)
    try:
        p.query("bg_start", {"seconds": 120})
        time.sleep(45)
        s1 = p.query("bg_status")
        time.sleep(30)
        s2 = p.query("bg_status")
        same = s1["instance"] == s2["instance"]
        ok = same and s2["ticks"] > s1["ticks"] + 20 and s2["max_gap_s"] < 3
        p.record("Q4 background work continues between requests (needed for async turns)", ok,
                 f"ticks {s1['ticks']}->{s2['ticks']}, max_gap={s2['max_gap_s']}s, same_instance={same}")
    except Exception as exc:
        p.record("Q4 background work continues between requests", False, str(exc)[:300])

    attempt("Q5 outbound internet (no gateway yet)", lambda: p.query("egress"))
    attempt("Q6 read Secret Manager with the runtime SA", lambda: p.query("secret", {"project": P, "name": c["secret"]}))
    attempt("Q7 Gemini via Vertex with the runtime SA", lambda: p.query("gemini", {"project": P, "location": c["location"]}))
    if st.get("sql_connection"):
        attempt("Q8 Cloud SQL via connector (public IP, no VPC)", lambda: p.query(
            "db", {"project": P, "instance": st["sql_connection"], "user": "pinboard", "db": "pinboard", "password_secret": c["secret"]}, timeout=120))
        attempt("Q9 psycopg 3 + pool + LangGraph PostgresSaver via Cloud SQL Auth Proxy (localhost)", lambda: p.query(
            "db_psycopg", {"project": P, "user": "pinboard", "db": "pinboard", "password_secret": c["secret"]}, timeout=120))
    else:
        p.record("Q8/Q9 Cloud SQL", None, "skipped: run `sql-up` first")

    RESULTS.write_text(json.dumps(p.results, indent=2))
    fails = [r for r in p.results if r["ok"] is False]
    print(f"\n{len(p.results) - len(fails)}/{len(p.results)} ok. Details saved to {RESULTS}")


def down(args) -> None:
    c, st = cfg(args), load()
    P, L = c["project"], c["location"]
    todo = [f"Agent Runtime {st.get('runtime') or '(none)'}", f"Cloud SQL instance {c['sql']}", f"secret {c['secret']}",
            f"Artifact Registry repo {c['repo']} (and the probe image)", f"service account {c['sa']}"]
    print("About to DELETE in project", P, ":\n  - " + "\n  - ".join(todo))
    if input("Type 'delete' to confirm: ").strip() != "delete":
        sys.exit("aborted")
    if st.get("runtime"):
        client(c).runtimes.delete(name=st["runtime"], force=True)
    sh(["gcloud", "sql", "instances", "delete", c["sql"], "--quiet", "--project", P], check=False)
    sh(["gcloud", "secrets", "delete", c["secret"], "--quiet", "--project", P], check=False)
    sh(["gcloud", "artifacts", "repositories", "delete", c["repo"], "--location", L, "--quiet", "--project", P], check=False)
    sh(["gcloud", "iam", "service-accounts", "delete", f"{c['sa']}@{P}.iam.gserviceaccount.com", "--quiet", "--project", P], check=False)
    STATE.unlink(missing_ok=True)
    print("done. (APIs stay enabled; that is free.)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--location", default="us-central1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("preflight", preflight), ("setup", setup), ("sql-up", sql_up), ("build", build), ("down", down)):
        sub.add_parser(name).set_defaults(fn=fn)
    dp = sub.add_parser("deploy")
    dp.add_argument("--framework", help="force one agent_framework value (default: try langgraph, google-adk, then none)")
    dp.set_defaults(fn=deploy)
    pr = sub.add_parser("probe")
    pr.add_argument("--only-db", action="store_true", help="only Q9 (psycopg through the Auth Proxy)")
    pr.add_argument("--quick", action="store_true", help="only Q1/Q1b/Q2 (a few seconds): use while debugging the invoke path")
    pr.add_argument("--long", action="store_true", help="also hold requests open 600/900/1800s (slower, costs a bit more)")
    pr.set_defaults(fn=probe)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
