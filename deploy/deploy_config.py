#!/usr/bin/env python3
"""Reads deploy/config.yaml (+ config.local.yaml, + PB_* environment variables) and works out every resource name from `name`.

    python deploy/deploy_config.py shell     KEY='value' lines for bash:  eval "$(python deploy/deploy_config.py shell)"
    python deploy/deploy_config.py show      the resolved settings, readable
    python deploy/deploy_config.py get KEY   one value (e.g. image)
Plain standard library + PyYAML, so deploy.sh can run it anywhere the project's Python runs.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

HERE = Path(__file__).parent
DEFAULTS_FILE, LOCAL_FILE = HERE / "config.yaml", HERE / "config.local.yaml"
ENV_OVERRIDES = {"PB_NAME": "name", "PB_PROJECT": "project", "PB_REGION": "region", "PB_BUCKET": "artifacts_bucket", "PB_SQL_TIER": "sql_tier",
                 "PB_SCHEDULE": "schedule"}
KEYS = {"name", "project", "region", "artifacts_bucket", "sql_tier", "sql_database", "sql_user", "schedule", "min_instances", "max_instances",
        "web_min_instances", "web_max_instances", "secrets_from_dotenv", "secrets_provided", "env_exclude"}
POLICY_FILE = HERE.parent / "config" / "policy.yaml"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,18}[a-z0-9]$")  # 2-20 chars: '<name>-scheduler' must fit a 30-character service account id
PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
REGION_RE = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class ConfigError(ValueError):
    pass


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a mapping of settings")
    unknown = set(data) - KEYS
    if unknown:
        raise ConfigError(f"{path.name}: unknown setting(s) {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(KEYS))}")
    return data


def _gcloud_project() -> str:
    try:
        return subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _allowed_users(policy_file: Path) -> list[str]:
    """Who may open the web app = who the engine's policy allows (one list, so the two cannot drift apart)."""
    try:
        users = (yaml.safe_load(policy_file.read_text()) or {}).get("allowed_users") or []
    except (OSError, yaml.YAMLError):
        return []
    return [str(u).strip().lower() for u in users if isinstance(u, str) and "@" in u]


def load(*, env: Mapping[str, str] | None = None, defaults: Path = DEFAULTS_FILE, local: Path = LOCAL_FILE,
         project_lookup: Callable[[], str] | None = None, policy_file: Path | None = None) -> dict[str, Any]:
    """The settings from the files and the environment, validated, plus every derived resource name."""
    env = os.environ if env is None else env
    policy_file = policy_file or Path(env.get("PINBOARD_POLICY") or POLICY_FILE)  # the same variable the app itself uses
    if env.get("PB_LOCAL_CONFIG"):
        local = Path(env["PB_LOCAL_CONFIG"])  # tests (and anyone with several deployments) point this somewhere else
    cfg: dict[str, Any] = {**_read(defaults), **_read(local)}
    for var, key in ENV_OVERRIDES.items():
        if env.get(var):
            cfg[key] = env[var]
    missing = KEYS - set(cfg)
    if missing:
        raise ConfigError(f"missing setting(s): {', '.join(sorted(missing))} (see deploy/config.yaml)")

    name = str(cfg["name"])
    if not NAME_RE.match(name):
        raise ConfigError(f"name {name!r} is not valid: use 2-20 lower-case letters, digits or '-', starting with a letter and not ending with '-'")
    if not REGION_RE.match(str(cfg["region"])):
        raise ConfigError(f"region {cfg['region']!r} does not look like a Google Cloud region (e.g. us-central1)")
    if not str(cfg["sql_tier"]).startswith("db-"):
        raise ConfigError(f"sql_tier {cfg['sql_tier']!r} must be a Cloud SQL machine type such as db-f1-micro")
    for key in ("min_instances", "max_instances", "web_min_instances", "web_max_instances"):
        if isinstance(cfg[key], bool) or not isinstance(cfg[key], int) or cfg[key] < 0:
            raise ConfigError(f"{key} must be a whole number >= 0")
    if cfg["max_instances"] < max(1, cfg["min_instances"]):
        raise ConfigError("max_instances must be at least 1 and at least min_instances")
    if cfg["web_max_instances"] < max(1, cfg["web_min_instances"]):
        raise ConfigError("web_max_instances must be at least 1 and at least web_min_instances")
    for key in ("secrets_from_dotenv", "secrets_provided", "env_exclude"):
        if not isinstance(cfg[key], list) or not all(isinstance(s, str) and SECRET_RE.match(s) for s in cfg[key]):
            raise ConfigError(f"{key} must be a list of names (letters, digits, '_' and '-')")

    project = str(cfg["project"] or "").strip() or ((project_lookup or _gcloud_project)() if project_lookup is not False else "")
    if project and not PROJECT_RE.match(project):
        raise ConfigError(f"project {project!r} does not look like a Google Cloud project id")
    region = str(cfg["region"])
    out = dict(cfg)
    out.update({
        "project": project,
        "artifacts_bucket": str(cfg["artifacts_bucket"] or f"{project}-{name}-files"),
        "sql_database": str(cfg["sql_database"] or name),
        "sql_user": str(cfg["sql_user"] or name),
        "repo": f"{name}-repo",
        "image": f"{region}-docker.pkg.dev/{project}/{name}-repo/{name}:latest",
        "runtime_sa_id": f"{name}-runtime",
        "runtime_sa": f"{name}-runtime@{project}.iam.gserviceaccount.com",
        "scheduler_sa_id": f"{name}-scheduler",
        "scheduler_sa": f"{name}-scheduler@{project}.iam.gserviceaccount.com",
        "sql_instance": f"{name}-pg",
        "job": f"{name}-housekeeping",
        "db_url_secret": f"{name}-database-url",
        "api_token_secret": f"{name}-api-token",
        "artifacts_prefix": name,
        "display_name": name,
        "web_service": f"{name}-web",
        "web_sa_id": f"{name}-web",
        "web_sa": f"{name}-web@{project}.iam.gserviceaccount.com",
        "web_image": f"{region}-docker.pkg.dev/{project}/{name}-repo/{name}-web:latest",
        "web_users": _allowed_users(policy_file),
    })
    return out


# bash variable name for each setting, in the order they are printed
SHELL_VARS = {
    "name": "NAME", "project": "PROJECT", "region": "REGION", "artifacts_bucket": "BUCKET", "sql_tier": "SQL_TIER", "schedule": "SCHEDULE",
    "min_instances": "MIN_INSTANCES", "max_instances": "MAX_INSTANCES", "repo": "REPO", "image": "IMAGE", "runtime_sa_id": "RUNTIME_SA_ID",
    "runtime_sa": "RUNTIME_SA", "scheduler_sa_id": "SCHEDULER_SA_ID", "scheduler_sa": "SCHEDULER_SA", "sql_instance": "SQL_INSTANCE", "job": "JOB",
    "sql_database": "DB_NAME", "sql_user": "DB_USER", "db_url_secret": "DB_URL_SECRET", "api_token_secret": "API_TOKEN_SECRET",
    "artifacts_prefix": "ARTIFACTS_PREFIX", "secrets_from_dotenv": "SECRETS_FROM_DOTENV", "secrets_provided": "SECRETS_PROVIDED", "env_exclude": "ENV_EXCLUDE",
    "web_service": "WEB_SERVICE", "web_sa_id": "WEB_SA_ID", "web_sa": "WEB_SA", "web_image": "WEB_IMAGE", "web_min_instances": "WEB_MIN",
    "web_max_instances": "WEB_MAX", "web_users": "WEB_USERS",
}


def shell_lines(cfg: Mapping[str, Any]) -> str:
    def val(v: Any) -> str:
        return " ".join(v) if isinstance(v, list) else str(v)
    return "\n".join(f"{var}={shlex.quote(val(cfg[key]))}" for key, var in SHELL_VARS.items())


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "show"
    try:
        cfg = load()
    except ConfigError as exc:
        print(f"deploy config error: {exc}", file=sys.stderr)
        return 2
    if cmd == "shell":
        print(shell_lines(cfg))
    elif cmd == "get" and len(args) == 2 and args[1] in cfg:
        print(cfg[args[1]] if not isinstance(cfg[args[1]], list) else " ".join(cfg[args[1]]))
    elif cmd == "show":
        for k, v in cfg.items():
            print(f"  {k:20s} {' '.join(v) if isinstance(v, list) else v}")
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
