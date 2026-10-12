"""Settings loaded from .env / environment. No secrets live here (see vault.py)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Names the vault manages. Add new connectors' secrets here in later phases.
SECRET_NAMES = ["TAVILY_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DATABASE_URL", "PINBOARD_API_TOKEN",
                "AGENTMAIL_API_KEY", "GITHUB_APP_PRIVATE_KEY", "MCP_ENCRYPTION_KEY"]


def secret_names() -> list[str]:
    """Every secret the vault should know: the built-in names plus the ones the deployment lists in EXTRA_SECRET_NAMES (found in .env by name),
    so output redaction covers an agent's own secrets too."""
    extra = [n.strip() for n in os.environ.get("EXTRA_SECRET_NAMES", "").split(",") if n.strip()]
    return list(dict.fromkeys([*SECRET_NAMES, *extra]))


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    model: str
    secrets_backend: str
    gcp_project: str | None
    gcp_location: str
    sandbox_runtime: str | None
    sandbox_name: str | None
    sandbox_ttl_seconds: int
    sandbox_cwd: str
    skills_source: str | None
    command_policy: str
    max_prompt_chars: int
    approval_mode: str
    tavily_max_results: int
    google_use_vertex: bool = False
    model_location: str | None = None
    output_dir: str = "./outputs"
    outputs_gcs_uri: str | None = None
    state_backend: str = "memory"
    approval_ttl_seconds: int = 1200
    user_id: str = "owner"
    trust_platform_auth: bool = False
    github_app_id: str | None = None
    github_installation_id: str | None = None  # empty = auto-detect (only when the app has exactly one installation)
    agentmail_inbox_id: str | None = None  # empty = policy email.from_inbox
    model_timeout_seconds: int = 120  # one model call may take this long; the client default is NO limit
    model_max_retries: int = 3  # automatic retries on rate-limit / server errors (the client default is 6, with waits that can add up to minutes)
    artifacts_uri: str = "./artifacts"  # gs://bucket/prefix in the cloud, or a local folder in development
    agent_builder: str | None = None  # "package.module:function" of the agent to run instead of Pinboard's own (see agent/contract.py)

    @property
    def skills_path(self) -> str:
        """Where skills are placed inside the sandbox."""
        return f"{self.sandbox_cwd.rstrip('/')}/skills"

    @property
    def outputs_path(self) -> str:
        """Folder inside the sandbox where the agent saves deliverables."""
        return f"{self.sandbox_cwd.rstrip('/')}/out"

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = ".env") -> "Settings":
        if env_file and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        g = os.environ.get
        s = cls(
            model=g("MODEL", "google_genai:gemini-2.5-flash"),
            secrets_backend=g("SECRETS_BACKEND", "env"),
            gcp_project=_clean(g("GCP_PROJECT")),
            gcp_location=g("GCP_LOCATION", "us-central1"),
            sandbox_runtime=_clean(g("SANDBOX_RUNTIME")),
            sandbox_name=_clean(g("SANDBOX_NAME")),
            sandbox_ttl_seconds=int(g("SANDBOX_TTL_SECONDS", "3600")),
            sandbox_cwd=g("SANDBOX_CWD", "/workspace"),
            skills_source=_clean(g("SKILLS_SOURCE")),
            command_policy=g("COMMAND_POLICY", "allowlist"),
            max_prompt_chars=int(g("MAX_PROMPT_CHARS", "8000")),
            approval_mode=g("APPROVAL_MODE", "none"),
            tavily_max_results=int(g("TAVILY_MAX_RESULTS", "5")),
            google_use_vertex=g("GOOGLE_USE_VERTEX", "false").strip().lower() in ("1", "true", "yes"),
            model_location=_clean(g("MODEL_LOCATION")),
            output_dir=g("OUTPUT_DIR", "./outputs"),
            outputs_gcs_uri=_clean(g("OUTPUTS_GCS_URI")),
            state_backend=g("STATE_BACKEND", "memory"),
            approval_ttl_seconds=int(g("APPROVAL_TTL_SECONDS", "1200")),
            user_id=g("PINBOARD_USER_ID", "owner"),
            trust_platform_auth=g("TRUST_PLATFORM_AUTH", "false").strip().lower() in ("1", "true", "yes"),
            github_app_id=_clean(g("GITHUB_APP_ID")),
            github_installation_id=_clean(g("GITHUB_INSTALLATION_ID")),
            agentmail_inbox_id=_clean(g("AGENTMAIL_INBOX_ID")),
            artifacts_uri=_clean(g("ARTIFACTS_URI")) or "./artifacts",
            agent_builder=_clean(g("AGENT_BUILDER")),
            model_timeout_seconds=int(g("MODEL_TIMEOUT_SECONDS", "120")),
            model_max_retries=int(g("MODEL_MAX_RETRIES", "3")),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.command_policy not in ("allowlist", "denylist"):
            raise ValueError("COMMAND_POLICY must be 'allowlist' or 'denylist'")
        if self.approval_mode not in ("none", "execute"):
            raise ValueError("APPROVAL_MODE must be 'none' or 'execute'")
        if self.state_backend not in ("memory", "postgres"):
            raise ValueError("STATE_BACKEND must be 'memory' or 'postgres'")
        if self.approval_mode != "none" and self.approval_ttl_seconds + 300 > self.sandbox_ttl_seconds:
            raise ValueError(
                "APPROVAL_TTL_SECONDS + 300 must be <= SANDBOX_TTL_SECONDS: a parked run keeps its sandbox alive "
                "while waiting, and the sandbox must outlive the approval window"
            )
        if not self.sandbox_cwd.startswith("/"):
            raise ValueError("SANDBOX_CWD must be an absolute path")
