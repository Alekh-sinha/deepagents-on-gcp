#!/usr/bin/env python3
"""Does GOOGLE'S OWN documented LangGraph + Cloud SQL checkpointer work behind Agent Gateway?  (run from pinboard-agent/)

    uv run --isolated --no-project --python 3.12 --with "google-cloud-aiplatform[agent_engines,langchain]==2.4.0" \
        --with langchain-google-cloud-sql-pg --with langchain-google-vertexai --with langgraph \
        python spikes/gateway/doc_agent/spike.py <command>      (the wrapper spikes/gateway/doc_agent/run.sh does this for you)

    setup-db            a throwaway database + user on the existing public instance pinboard-pg (password kept in .secret, git-ignored)
    local               run the agent on THIS machine with the checkpointer (does the documented code even work here?)
    deploy plain        control: deploy it to Agent Runtime with a service account, no gateway
    deploy gateway      deploy it with AGENT_IDENTITY + the gateway
    query [plain|gateway]   two questions on one thread: the second must remember the first (answer comes from the database checkpoint)
    down                delete both runtimes, the database and the user

The password is placed in checkpointer_kwargs, which is pickled into the deployed agent (a spike shortcut; the user is deleted by `down`).
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
STATE, SECRET = HERE / ".state.json", HERE / ".secret"
PROJECT, REGION = "automation-ai-507911", "us-central1"
INSTANCE = os.environ.get("PB_SQL_INSTANCE", "pinboard-pg")   # pbgw-pg = the private-IP-only instance
IP_TYPE = os.environ.get("PB_IP_TYPE", "PUBLIC")             # PRIVATE for pbgw-pg
DB, USER = "docspike", "docspike"
GATEWAY = f"projects/{PROJECT}/locations/{REGION}/agentGateways/{os.environ.get('PB_GATEWAY', 'my-egress-gw')}"
RUNTIME_SA = f"pinboard-runtime@{PROJECT}.iam.gserviceaccount.com"
REQUIREMENTS = ["google-cloud-aiplatform[agent_engines,langchain]==2.4.0", "langchain-google-cloud-sql-pg", "langchain-google-vertexai==2.1.2", "langgraph", "cloudpickle", "pydantic"]


def load() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(**kv) -> None:
    STATE.write_text(json.dumps({**load(), **kv}, indent=2))


def sh(*cmd: str, check: bool = True) -> str:
    print("  $", " ".join(c if "password" not in c else "--password=<hidden>" for c in cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode:
        sys.exit(f"failed: {p.stderr.strip()[:600]}")
    return p.stdout.strip()


def kwargs() -> dict:
    if not SECRET.exists():
        sys.exit("run `setup-db` first")
    kw = {"project_id": PROJECT, "region": REGION, "instance": INSTANCE, "database": DB, "user": USER, "password": SECRET.read_text().strip()}
    if IP_TYPE != "PUBLIC":
        kw["ip_type"] = IP_TYPE  # PostgresEngine.from_instance(..., ip_type="PRIVATE"): connect to the instance's private address
    return kw


def setup_db() -> None:
    sh("gcloud", "sql", "databases", "create", DB, "--instance", INSTANCE, "--project", PROJECT, check=False)
    pw = secrets.token_urlsafe(18)
    sh("gcloud", "sql", "users", "create", USER, "--instance", INSTANCE, f"--password={pw}", "--project", PROJECT, check=False)
    sh("gcloud", "sql", "users", "set-password", USER, "--instance", INSTANCE, f"--password={pw}", "--project", PROJECT)
    SECRET.write_text(pw)
    SECRET.chmod(0o600)
    print("ready: database", DB, "user", USER)


def local() -> None:
    from agent import make_agent

    agent = make_agent(kwargs())
    cfg = {"configurable": {"thread_id": f"local-{int(time.time())}"}}
    r1 = agent.query(input={"messages": [("user", "My name is Tom. Reply with one short sentence.")]}, config=cfg)
    r2 = agent.query(input={"messages": [("user", "What is my name? One word.")]}, config=cfg)
    print("first :", str(r1)[-300:])
    print("second:", str(r2)[-300:])
    print("PASS: remembered Tom" if "tom" in str(r2).lower() else "FAIL: did not remember")


def client():
    import agentplatform

    return agentplatform.Client(project=PROJECT, location=REGION)


def pickle_by_value() -> None:
    """The deployed agent is rebuilt from a pickle in a runtime that does not have agent.py: carry the module's code inside the pickle."""
    import agent as agent_module
    import cloudpickle

    cloudpickle.register_pickle_by_value(agent_module)


def deploy(mode: str) -> None:
    from agent import make_agent

    pickle_by_value()
    agent = make_agent(kwargs())
    config = {"display_name": f"docspike-{mode}", "requirements": REQUIREMENTS, "min_instances": 1, "max_instances": 1,
              "staging_bucket": "gs://privai", "python_version": "3.12"}  # the pickle is made with 3.12 (run.sh) and must be read by 3.12
    if mode == "gateway":
        # Google's gateway instructions: with an agent identity behind a gateway, token binding must be switched off (the gateway terminates TLS)
        config |= {"identity_type": "AGENT_IDENTITY", "agent_gateway_config": {"agent_to_anywhere_config": {"agent_gateway": GATEWAY}},
                   "env_vars": {"GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": "false"}}
    else:
        config |= {"service_account": RUNTIME_SA}
    print("creating (several minutes: the platform builds an image from the pickled agent) ...")
    remote = client().runtimes.create(agent=agent, config=config)
    res = getattr(remote, "api_resource", remote)
    name = getattr(res, "name", None) or str(res)
    save(**{mode: name})
    print("created:", name)


def rest(name: str):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds), f"https://{REGION}-aiplatform.googleapis.com/v1/{name}"


def query(mode: str) -> None:
    name = load().get(mode)
    if not name:
        sys.exit(f"no {mode} runtime recorded")
    http, base = rest(name)
    thread = f"t-{int(time.time())}"
    answers = []
    for text in ("My name is Tom. Reply with one short sentence.", "What is my name? One word."):
        r = http.post(f"{base}:query", json={"classMethod": "query", "input": {"input": {"messages": [["user", text]]}, "config": {"configurable": {"thread_id": thread}}}}, timeout=180)
        print(f"  HTTP {r.status_code}: {r.text[:400]}")
        answers.append(r.text)
    print(f"{mode}: PASS (remembered Tom, so the checkpoint round-trip through Cloud SQL worked)" if "tom" in answers[-1].lower() else f"{mode}: FAIL")


def down() -> None:
    st = load()
    for mode in ("plain", "gateway"):
        if st.get(mode):
            try:
                client().runtimes.delete(name=st[mode], force=True)
                print("deleted", st[mode])
            except Exception as exc:  # noqa: BLE001
                print("could not delete", st[mode], str(exc)[:150])
    sh("gcloud", "sql", "databases", "delete", DB, "--instance", INSTANCE, "--project", PROJECT, "--quiet", check=False)
    sh("gcloud", "sql", "users", "delete", USER, "--instance", INSTANCE, "--project", PROJECT, "--quiet", check=False)
    STATE.unlink(missing_ok=True)
    SECRET.unlink(missing_ok=True)


def main() -> None:
    cmd, *args = sys.argv[1:] or ["help"]
    if cmd == "setup-db":
        setup_db()
    elif cmd == "local":
        local()
    elif cmd == "deploy" and args and args[0] in ("plain", "gateway"):
        deploy(args[0])
    elif cmd == "query":
        query(args[0] if args else "plain")
    elif cmd == "down":
        down()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
