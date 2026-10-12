#!/usr/bin/env python3
"""What in a .env file is a secret and what is a plain setting. No list of names to maintain: the NAME decides.

A variable whose name contains KEY, TOKEN, SECRET, PASSWORD, PASSWD, PRIVATE or CREDENTIAL is a SECRET: `deploy.sh setup` copies it into Secret
Manager under the same name (the value goes through a pipe, never a command line) and the engine reads it from there by name. Every other variable
is a plain setting and becomes an environment variable of the engine. Placeholders (``your-...``), empty values and ``secret://`` references are skipped.

Never taken from .env, because the deployment decides them or they only make sense on this computer (RESERVED below, plus ``env_exclude`` in
deploy/config.yaml):  the database, the API token, the state/secrets backends, local folders and sandbox names.

    python deploy/dotenv_secrets.py names [.env]    secret names, one per line
    python deploy/dotenv_secrets.py plain [.env]    NAME=value lines of the plain settings
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

SECRET_WORDS = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|CREDENTIAL)", re.IGNORECASE)
RESERVED = frozenset({
    # decided by the deployment
    "GCP_PROJECT", "GCP_LOCATION", "GOOGLE_USE_VERTEX", "SECRETS_BACKEND", "STATE_BACKEND", "SQL_CONNECTION", "DATABASE_URL", "PINBOARD_API_TOKEN",
    "TRUST_PLATFORM_AUTH", "APPROVAL_MODE", "ARTIFACTS_URI", "PINBOARD_USER_ID", "EXTRA_SECRET_NAMES", "PINBOARD_POLICY",
    # only meaningful on this computer
    "SANDBOX_NAME", "SKILLS_SOURCE", "OUTPUT_DIR", "OUTPUTS_GCS_URI", "SANDBOX_CWD", "PINBOARD_DEBUG",
})


@dataclass
class Split:
    secrets: dict[str, str] = field(default_factory=dict)
    plain: dict[str, str] = field(default_factory=dict)


def usable(value: str | None) -> bool:
    from pinboard.gcp.vault import is_placeholder  # the same test the app and dotenv_get.py use

    return not is_placeholder(value) and not str(value).strip().startswith("secret://")


def split(values: Mapping[str, str | None], exclude: Iterable[str] = ()) -> Split:
    skip = RESERVED | set(exclude)
    out = Split()
    for name, value in values.items():
        if name in skip or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or not usable(value):
            continue
        if SECRET_WORDS.search(name):
            out.secrets[name] = str(value)  # exactly as written (a private key must not be trimmed or re-wrapped)
        else:
            out.plain[name] = str(value).strip()
    return out


def main(argv: list[str]) -> int:
    from dotenv import dotenv_values

    if not argv or argv[0] not in ("names", "plain"):
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1]) if len(argv) > 1 else Path(".env")
    parts = split(dotenv_values(path), os.environ.get("ENV_EXCLUDE", "").split())
    if argv[0] == "names":
        print("\n".join(sorted(parts.secrets)))
    else:
        print("\n".join(f"{k}={v}" for k, v in sorted(parts.plain.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
