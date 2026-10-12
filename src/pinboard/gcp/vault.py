"""Secret vault: values come from .env / the environment, or Google Secret Manager.

Resolution order for a secret NAME:
  1. Environment (loaded from .env). If the value is ``secret://OTHER_NAME`` it is
     resolved from Secret Manager under OTHER_NAME.
  2. If SECRETS_BACKEND=gcp and NAME is not set: Secret Manager under NAME.
Placeholder values from example.env (e.g. "your-tavily-key-here") count as unset.
"""
from __future__ import annotations

import os
import re
import time
from typing import Protocol

SECRET_REF_PREFIX = "secret://"
_PLACEHOLDER = re.compile(r"^(your[-_ ]|change[-_ ]?me|todo\b|<.*>$|x{3,}$)", re.I)


class SecretNotFound(KeyError):
    """A required secret could not be resolved."""


class RemoteSecrets(Protocol):
    def get(self, name: str) -> str | None: ...


def is_placeholder(value: str | None) -> bool:
    return not value or bool(_PLACEHOLDER.match(value.strip()))


class GCPSecretManager:
    """Thin wrapper over Google Secret Manager (latest version of a secret)."""

    def __init__(self, project: str, client=None):
        if is_placeholder(project):
            raise ValueError("GCP_PROJECT must be set to use the gcp secrets backend")
        self.project = project
        self._client = client

    def _c(self):
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient(transport="rest")  # plain HTTPS: gRPC does not get through Agent Gateway's proxy (found in the gateway spike)
        return self._client

    def get(self, name: str) -> str | None:
        from google.api_core import exceptions as gexc

        path = f"projects/{self.project}/secrets/{name}/versions/latest"
        try:
            resp = self._c().access_secret_version(request={"name": path})
        except gexc.NotFound:
            return None
        return resp.payload.data.decode("utf-8")

    def put(self, name: str, value: str) -> str:
        """Create the secret if needed and add a new version. Returns 'created' or 'updated'."""
        from google.api_core import exceptions as gexc

        parent = f"projects/{self.project}"
        status = "updated"
        try:
            self._c().create_secret(
                request={
                    "parent": parent,
                    "secret_id": name,
                    "secret": {"replication": {"automatic": {}}},
                }
            )
            status = "created"
        except gexc.AlreadyExists:
            pass
        self._c().add_secret_version(
            request={
                "parent": f"{parent}/secrets/{name}",
                "payload": {"data": value.encode("utf-8")},
            }
        )
        return status


class Vault:
    def __init__(
        self,
        backend: str = "env",
        project: str | None = None,
        remote: RemoteSecrets | None = None,
        miss_ttl: float = 120.0,
        clock=time.monotonic,
    ):
        if backend not in ("env", "gcp"):
            raise ValueError(f"SECRETS_BACKEND must be 'env' or 'gcp', got {backend!r}")
        self.backend = backend
        self._remote = remote
        if self._remote is None and backend == "gcp":
            self._remote = GCPSecretManager(project or "")
        self._cache: dict[str, str] = {}
        self._missing: dict[str, float] = {}  # name -> when the "not found" answer expires
        self._miss_ttl, self._clock = miss_ttl, clock

    def get(self, name: str, *, required: bool = False) -> str | None:
        if name in self._cache:
            return self._cache[name]
        if self._clock() < self._missing.get(name, 0.0):
            value = None  # asked a moment ago and Secret Manager said there is none: do not ask again on every turn
            if required:
                raise SecretNotFound(f"Secret {name} is not set (see example.env)")
            return None
        raw = os.environ.get(name)
        value: str | None = None
        if raw and raw.startswith(SECRET_REF_PREFIX):
            if self._remote is None:
                raise SecretNotFound(
                    f"{name} points at {raw!r} but SECRETS_BACKEND is not 'gcp'"
                )
            value = self._remote.get(raw[len(SECRET_REF_PREFIX):])
        elif not is_placeholder(raw):
            value = raw
        elif self._remote is not None:
            value = self._remote.get(name)
        if value is None:
            self._missing[name] = self._clock() + self._miss_ttl
            if required:
                raise SecretNotFound(f"Secret {name} is not set (see example.env)")
            return None
        self._cache[name] = value
        return value

    def redact(self, text: str) -> str:
        """Replace any known secret value in text with [REDACTED:NAME]."""
        for name, value in self._cache.items():
            if len(value) >= 8 and value in text:
                text = text.replace(value, f"[REDACTED:{name}]")
        return text


def push_secrets(names: list[str], project: str, remote: GCPSecretManager | None = None) -> dict[str, str]:
    """Upload plain values from the environment (.env) into Secret Manager."""
    remote = remote or GCPSecretManager(project)
    report: dict[str, str] = {}
    for name in names:
        raw = os.environ.get(name)
        if is_placeholder(raw):
            report[name] = "skipped (not set)"
        elif raw.startswith(SECRET_REF_PREFIX):
            report[name] = "skipped (already a secret:// reference)"
        else:
            report[name] = remote.put(name, raw)
    return report
