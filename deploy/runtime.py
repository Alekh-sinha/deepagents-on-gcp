#!/usr/bin/env python3
"""Create / update / delete the Agent Runtime that serves Pinboard. Used by deploy.sh; also runnable directly.

    python deploy/runtime.py apply  --project P --region R --image IMG --service-account SA --sql-connection P:R:INSTANCE --bucket B
    python deploy/runtime.py show   --project P --region R
    python deploy/runtime.py delete --project P --region R

The resource name is remembered in deploy/.state.json. Nothing secret is ever put in the runtime's configuration: secrets are
named (`secret://<name>-database-url`) and read from Secret Manager by the runtime service account at start-up.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).parent))
import deploy_config  # noqa: E402  (the one place resource names are worked out)
import dotenv_secrets  # noqa: E402  (which .env variables are secrets and which are plain settings)
import egress  # noqa: E402  (the proxy settings of the egress control, docs/EGRESS.md)

STATE = Path(__file__).with_name(".state.json")  # --state-file points this somewhere else (the gateway spike keeps its own)
FRAMEWORK = "langgraph"  # a container deploy is refused without agent_framework (found in the spike)


def read_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def write_state(**kv) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({**read_state(), **kv}, indent=2))


def resolved(project: str) -> dict:
    """The deployment settings and resource names (deploy/config.yaml + config.local.yaml + PB_* variables) for this project."""
    return deploy_config.load(project_lookup=lambda: project)


def runtime_config(*, project: str, region: str, image: str, service_account: str, sql_connection: str, bucket: str,
                   dotenv: dict | None = None, min_instances: int | None = None, max_instances: int | None = None, display_name: str | None = None,
                   extra_env: dict | None = None, agent_gateway: str = "", psc_attachment: str = "", cfg: Mapping | None = None, sandbox: bool = True) -> dict:
    """The configuration handed to `client.runtimes.create/update`. Pure function: no network, easy to test. ``cfg`` is the resolved deployment
    config (names of the secrets, the artifacts prefix, the display name); by default it is read from deploy/config*.yaml.
    ``sandbox`` is the policy's ``features.sandbox``: an agent without a sandbox needs no sandbox runtime."""
    from pinboard.engine.api import CONTRACT_METHODS

    cfg = cfg if cfg is not None else resolved(project)
    display_name = display_name or cfg["display_name"]
    min_instances = cfg["min_instances"] if min_instances is None else min_instances
    max_instances = cfg["max_instances"] if max_instances is None else max_instances
    local = dotenv or {}

    def plain(name: str, default: str = "") -> str:
        v = (local.get(name) or "").strip()
        return default if not v or v.startswith("your-") or v.startswith("secret://") else v

    env = {
        "GCP_PROJECT": project,
        "GCP_LOCATION": region,
        "MODEL": plain("MODEL", "google_genai:gemini-2.5-flash"),
        "GOOGLE_USE_VERTEX": "true",
        "SECRETS_BACKEND": "gcp",                 # TAVILY_API_KEY, AGENTMAIL_API_KEY, GITHUB_APP_PRIVATE_KEY are read by NAME from Secret Manager
        "STATE_BACKEND": "postgres",
        "SQL_CONNECTION": sql_connection,         # start.sh runs the Cloud SQL Auth Proxy for this instance
        "DATABASE_URL": f"secret://{cfg['db_url_secret']}",
        "PINBOARD_API_TOKEN": f"secret://{cfg['api_token_secret']}",
        "TRUST_PLATFORM_AUTH": "true",            # the platform authenticates callers (IAM); the REST /v1 routes are not reachable anyway
        "APPROVAL_MODE": "execute",               # shell commands ask a person first
        "ARTIFACTS_URI": f"gs://{bucket}/{cfg['artifacts_prefix']}",
        "SANDBOX_RUNTIME": plain("SANDBOX_RUNTIME"),
        "SANDBOX_TTL_SECONDS": plain("SANDBOX_TTL_SECONDS", "3600"),
        "GITHUB_APP_ID": plain("GITHUB_APP_ID"),
        "AGENTMAIL_INBOX_ID": plain("AGENTMAIL_INBOX_ID"),
        "PINBOARD_USER_ID": "owner",
    }
    if plain("MODEL_LOCATION"):
        env["MODEL_LOCATION"] = plain("MODEL_LOCATION")
    parts = dotenv_secrets.split(local, cfg.get("env_exclude", []))
    for key, value in parts.plain.items():  # every other plain setting in .env goes along (what is set above wins)
        env.setdefault(key, value)
    if parts.secrets or cfg.get("secrets_provided"):  # the engine reads these by name from Secret Manager; the list lets it hide their values in output
        env["EXTRA_SECRET_NAMES"] = ",".join(sorted({*parts.secrets, *cfg.get("secrets_from_dotenv", []), *cfg.get("secrets_provided", [])}))
    # Private networking (all OFF unless deploy/config*.yaml sets them): a proxy for third-party hosts, and a database with a private address only
    env.update(egress.proxy_env(cfg))
    if cfg.get("sql_private_ip"):
        env["SQL_PRIVATE_IP"] = "true"
    env.update(extra_env or {})
    env = {k: v for k, v in env.items() if v != ""}
    if sandbox and "SANDBOX_RUNTIME" not in env:
        raise SystemExit("SANDBOX_RUNTIME is empty in .env: run `uv run pinboard ping` once (it creates the sandbox runtime and records its name), then retry")
    gateway = {}
    if agent_gateway:
        # Google's gateway instructions set this for every gateway deployment: an agent identity's tokens are bound to its certificate (mTLS to
        # Google APIs), but the gateway terminates TLS, so the binding must be off (it was the cause of our .mtls./handshake errors, we think)
        env["GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"] = "false"
        # egress through Agent Gateway needs an agent identity (it cannot be added to an existing runtime)
        gateway = {"identity_type": "AGENT_IDENTITY", "agent_gateway_config": {"agent_to_anywhere_config": {"agent_gateway": agent_gateway}}}
    sa = {} if agent_gateway else {"service_account": service_account if "@" in service_account else f"{service_account}@{project}.iam.gserviceaccount.com"}  # an agent identity replaces the service account (the API refuses both)
    psc_attachment = psc_attachment or str(cfg.get("network_attachment") or "")
    if psc_attachment and agent_gateway:
        raise SystemExit("a runtime cannot have both a PSC network attachment and an Agent Gateway: the API refuses it (see docs/EGRESS.md)")
    if cfg.get("egress_proxy") and not psc_attachment:
        raise SystemExit("egress_proxy is set but there is no network attachment: the engine could not reach the proxy")
    psc = {"psc_interface_config": {"network_attachment": psc_attachment}} if psc_attachment else {}
    if psc and cfg.get("network_dns_domains"):  # names under these domains are looked up in your VPC's private DNS zones (an MCP server reached by name)
        psc["psc_interface_config"]["dns_peering_configs"] = [{"domain": d, "target_project": project, "target_network": cfg["network"]} for d in cfg["network_dns_domains"]]  # the runtime itself joins a VPC (private Cloud SQL)
    return {
        **gateway,
        **psc,
        **sa,
        "display_name": display_name,
        "labels": {"app": cfg["name"]},
        "container_spec": {"image_uri": image},
        "class_methods": CONTRACT_METHODS,        # the single source of truth, checked by the server at start-up
        "env_vars": env,
        "min_instances": min_instances,
        "max_instances": max_instances,
        "agent_framework": FRAMEWORK,
    }


def _client(project: str, region: str):
    import agentplatform

    return agentplatform.Client(project=project, location=region)


def _still_exists(client, name: str) -> bool:
    """False only when the platform says the runtime is not there; any other error counts as 'exists' (never drop state on a guess)."""
    try:
        client.runtimes.get(name=name)
        return True
    except Exception as exc:  # noqa: BLE001
        text = f"{type(exc).__name__} {exc}"
        return not ("NOT_FOUND" in text or "404" in text or "is not found" in text)


def policy_has_sandbox() -> bool:
    """The policy file the image will contain says whether this agent uses a sandbox. Unreadable = assume yes: the stricter check stays on."""
    try:
        from pinboard.core.policy import load_policy

        return load_policy().features.sandbox
    except Exception:  # noqa: BLE001
        return True


def cmd_apply(a) -> int:
    extra = dict(kv.split("=", 1) for kv in a.env)
    cfg = runtime_config(project=a.project, region=a.region, image=a.image, service_account=a.service_account, sql_connection=a.sql_connection,
                         bucket=a.bucket, dotenv=dotenv_values(a.env_file), min_instances=a.min_instances, max_instances=a.max_instances,
                         display_name=a.display_name, extra_env=extra, agent_gateway=a.agent_gateway, psc_attachment=a.psc_attachment,
                         sandbox=policy_has_sandbox())
    client = _client(a.project, a.region)
    existing = read_state().get("runtime")
    if existing and not _still_exists(client, existing):
        print(f"{existing} no longer exists (deleted earlier); creating a new one")
        write_state(runtime="")
        existing = ""
    if existing and not a.recreate:
        print(f"updating {existing} ...")
        try:
            op = client.runtimes.update(name=existing, config=cfg)
            print("updated.")
            return 0
        except Exception as exc:  # noqa: BLE001  update support for container images is unverified: say so and offer the fallback
            print(f"update failed ({type(exc).__name__}: {str(exc)[:300]}).\nRe-run with --recreate to delete and create it again (the URL changes; the scheduler job is re-pointed by `deploy.sh scheduler`).", file=sys.stderr)
            return 1
    if existing and a.recreate:
        print(f"deleting {existing} first ...")
        try:
            client.runtimes.delete(name=existing, force=True)
        except Exception as exc:  # noqa: BLE001  an earlier --recreate may already have deleted it before its create failed
            print(f"(could not delete it, continuing: {type(exc).__name__}: {str(exc)[:160]})")
    print("creating the runtime (takes a few minutes) ...")
    op = client.runtimes.create(config=cfg)
    res = getattr(op, "api_resource", op)
    write_state(runtime=res.name, image=a.image)
    print(f"created: {res.name}")
    return 0


def cmd_show(a) -> int:
    name = read_state().get("runtime")
    if not name:
        print("no runtime recorded")
        return 1
    res = getattr(_client(a.project, a.region).runtimes.get(name=name), "api_resource", None)
    print(json.dumps({"name": name, "display_name": getattr(res, "display_name", None), "state": str(getattr(res, "state", ""))}, indent=2))
    return 0


def cmd_delete(a) -> int:
    name = read_state().get("runtime")
    if not name:
        print("no runtime recorded")
        return 0
    _client(a.project, a.region).runtimes.delete(name=name, force=True)
    write_state(runtime="")
    print(f"deleted {name}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("apply", "show", "delete"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        p.add_argument("--region", default="us-central1")
        p.add_argument("--state-file", help="where the runtime's resource name is remembered (default deploy/.state.json)")
        if name == "apply":
            p.add_argument("--image", required=True)
            p.add_argument("--service-account", required=True)
            p.add_argument("--sql-connection", required=True)
            p.add_argument("--bucket", required=True)
            p.add_argument("--env-file", default=".env")
            p.add_argument("--min-instances", type=int, default=None, help="default: min_instances from deploy/config.yaml")
            p.add_argument("--max-instances", type=int, default=None, help="default: max_instances from deploy/config.yaml")
            p.add_argument("--display-name", default=None, help="default: the deployment name from deploy/config.yaml")
            p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="extra or overriding environment variable (repeatable)")
            p.add_argument("--agent-gateway", default="", help="projects/P/locations/R/agentGateways/NAME: send all outbound traffic through it")
            p.add_argument("--psc-attachment", default="", help="projects/P/regions/R/networkAttachments/NAME: attach the runtime to a VPC through Private Service Connect")
            p.add_argument("--recreate", action="store_true", help="delete the existing runtime and create it again instead of updating it")
    a = ap.parse_args(argv)
    if a.state_file:
        global STATE
        STATE = Path(a.state_file)
    return {"apply": cmd_apply, "show": cmd_show, "delete": cmd_delete}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
