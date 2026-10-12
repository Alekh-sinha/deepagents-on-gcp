"""Policy: hard limits loaded from config/policy.yaml and enforced inside tools, before any approval is requested.

The file is the single place to widen or narrow what the agent may do (who can be emailed, which repos and branches,
which models). A missing or invalid file raises PolicyError so the app never starts with an accidentally open policy.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY_PATH = "config/policy.yaml"
KNOWN_PROVIDERS = ("google_genai", "anthropic", "openai")
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class PolicyError(ValueError):
    """The policy file is missing or invalid (the app must not start)."""


class PolicyDenied(Exception):
    """An action was refused by policy. ``rule`` is a short stable id, str(exc) is safe to show to the model and the user."""

    def __init__(self, rule: str, reason: str):
        super().__init__(reason)
        self.rule, self.reason = rule, reason

    def message(self) -> str:
        return f"Blocked by policy ({self.rule}): {self.reason}"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    label: str
    rank: int = 1        # 1 = cheapest; higher = stronger and dearer. A helper may use models up to the rank of the model the user selected
    use_for: str = ""    # one line the orchestrator reads when it chooses a helper model
    location: str = ""   # where the model is served (some are only served from "global"); empty = the deployment's default

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "provider": self.provider, "label": self.label, "rank": self.rank, "use_for": self.use_for, "location": self.location}


@dataclass(frozen=True)
class EmailPolicy:
    from_inbox: str
    allowed_recipients: tuple[str, ...]
    max_per_day: int = 20
    max_attachment_mb: int = 5


@dataclass(frozen=True)
class GithubPolicy:
    allowed_repos: tuple[str, ...] = ()
    branch_prefix: str = "agent/"
    never_default_branch: bool = True


@dataclass(frozen=True)
class Limits:
    max_turn_minutes: int = 30
    max_concurrent_turns: int = 4
    max_auto_resumes: int = 1
    sandbox_idle_minutes: int = 20  # a session's sandbox is deleted after this long without activity (artifacts are kept)
    max_helper_model_calls: int = 40  # one helper task may call the model at most this many times (the parent's step cap does not reach inside helpers)
    max_auto_approvals_per_turn: int = 25  # a session on auto-approve still stops for a person after this many approvals in ONE turn


WARM_MODES = ("off", "on_activity", "always")


@dataclass(frozen=True)
class McpPolicy:
    """Bring-your-own MCP servers (remote, over HTTP). Off unless switched on. A server's tools are always approval-gated unless the user marks them safe."""

    enabled: bool = False
    allowed_hosts: tuple[str, ...] = ()   # empty = any public host; otherwise only these host names
    allow_private_hosts: bool = False     # True lets a server live on localhost / a private network (local development only)
    max_servers_per_user: int = 10
    max_tools_per_server: int = 40
    tool_timeout_seconds: int = 60
    max_result_chars: int = 20_000


@dataclass(frozen=True)
class SchedulePolicy:
    """Scheduled tasks: the agent runs a saved instruction on a cron schedule with nobody watching (approvals wait for the user)."""

    enabled: bool = False
    max_per_user: int = 20
    min_interval_minutes: int = 15        # a schedule may not fire more often than this
    max_runs_per_day: int = 24            # per user, across all schedules


@dataclass(frozen=True)
class WarmSandbox:
    """Hides the 30-130 s a sandbox takes to create: ONE spare is kept ready and handed to the next session that needs a sandbox.
    off = never; on_activity = a spare is made when the user starts typing (and replaced after it is used); always = also kept topped up.
    Extra cost is bounded: only UNCLAIMED spares cost anything extra, at most ``max_spares_per_day`` x ``spare_max_minutes``."""

    mode: str = "off"
    spare_max_minutes: int = 15
    max_spares_per_day: int = 10


@dataclass(frozen=True)
class Features:
    """Parts of the engine that an agent may not need. Both default to ON, which is how Pinboard itself runs.
    sandbox   = a shell + file workspace per conversation (lazy creation, reuse across turns, idle clean-up, warm pool).
    artifacts = files the agent saves under its output folder are kept for the user; they are copied out of the sandbox, so they need it."""

    sandbox: bool = True
    artifacts: bool = True


@dataclass(frozen=True)
class Policy:
    allowed_users: tuple[str, ...]
    email: EmailPolicy
    github: GithubPolicy
    models: tuple[ModelSpec, ...]
    default_model: str
    limits: Limits = field(default_factory=Limits)
    warm_sandbox: WarmSandbox = field(default_factory=WarmSandbox)
    never_auto_tools: tuple[str, ...] = ()  # tools a person must always approve, even when a session is set to approve automatically
    mcp: McpPolicy = field(default_factory=McpPolicy)
    schedules: SchedulePolicy = field(default_factory=SchedulePolicy)
    helper_model_choice: bool = False  # True: the orchestrator may pick the model of each helper (never above the user's model); False: helpers use the user's model
    features: Features = field(default_factory=Features)

    # ── users ────────────────────────────────────────────────────────────
    def check_user(self, email: str | None) -> None:
        if (email or "").strip().lower() not in self.allowed_users:
            raise PolicyDenied("user.not_allowed", "this account is not allowed to use the assistant")

    # ── email ────────────────────────────────────────────────────────────
    def check_recipients(self, *groups: list[str] | tuple[str, ...] | str | None) -> list[str]:
        """Every address in to/cc/bcc must be allowed. Returns the normalised bare addresses; raises PolicyDenied otherwise.
        Tools MUST send to the returned list, never to the raw strings the model produced."""
        addrs: list[str] = []
        for g in groups:
            if not g:
                continue
            addrs.extend([g] if isinstance(g, str) else list(g))
        if not addrs:
            raise PolicyDenied("email.no_recipient", "an email needs at least one recipient")
        out = []
        for a in addrs:
            norm = _bare_address(a)
            if norm not in self.email.allowed_recipients:
                raise PolicyDenied("email.recipient_not_allowed",
                                   f"{norm or a!r} is not in the allowed recipient list; allowed: {', '.join(self.email.allowed_recipients)}")
            out.append(norm)
        return out

    def check_attachment_size(self, size_bytes: int) -> None:
        if size_bytes > self.email.max_attachment_mb * 1024 * 1024:
            raise PolicyDenied("email.attachment_too_big", f"attachment is larger than {self.email.max_attachment_mb} MB")

    def check_daily_email_quota(self, sent_today: int) -> None:
        if sent_today >= self.email.max_per_day:
            raise PolicyDenied("email.daily_limit", f"daily email limit of {self.email.max_per_day} reached")

    # ── github ───────────────────────────────────────────────────────────
    def check_repo(self, full_name: str) -> str:
        norm = (full_name or "").strip().lower()
        if not _REPO.match(norm):
            raise PolicyDenied("github.bad_repo", "repository must look like owner/name")
        if self.github.allowed_repos and norm not in self.github.allowed_repos:
            raise PolicyDenied("github.repo_not_allowed", f"{norm} is not in the allowed repository list")
        return norm

    def check_branch(self, branch: str, default_branch: str | None = None) -> str:
        b = (branch or "").strip()
        if not b or b.startswith("-") or ".." in b or b.endswith("/") or re.search(r"[\s~^:?*\[\\]", b):
            raise PolicyDenied("github.bad_branch", f"{branch!r} is not a valid branch name")
        if self.github.never_default_branch and default_branch and b == default_branch:
            raise PolicyDenied("github.default_branch", f"the default branch {default_branch!r} can never be written; use a branch starting with {self.github.branch_prefix!r}")
        if not b.startswith(self.github.branch_prefix):
            raise PolicyDenied("github.branch_prefix", f"branch names must start with {self.github.branch_prefix!r}")
        return b

    # ── models ───────────────────────────────────────────────────────────
    def resolve_model(self, model_id: str | None) -> ModelSpec:
        wanted = (model_id or "").strip() or self.default_model
        for m in self.models:
            if m.id == wanted:
                return m
        raise PolicyDenied("model.not_allowed", f"model {wanted!r} is not available; choose one of: {', '.join(m.id for m in self.models)}")

    def allowed_for(self, ceiling_id: str | None) -> list[ModelSpec]:
        """The models a helper may use when the user selected ``ceiling_id``: that model and every cheaper one, cheapest first."""
        top = self.resolve_model(ceiling_id).rank
        return sorted((m for m in self.models if m.rank <= top), key=lambda m: (m.rank, m.id))

    def list_models(self) -> dict[str, Any]:
        return {"default": self.default_model, "models": [m.to_dict() for m in self.models], "helper_model_choice": self.helper_model_choice}


def _bare_address(raw: str) -> str:
    """'Name <a@b.com>' -> 'a@b.com', lower-cased. Anything with a second address or odd characters is returned as-is (and so denied)."""
    raw = (raw or "").strip()
    m = re.fullmatch(r"([^<>]*)<([^<>]+)>", raw)
    if m:
        if re.search(r"[@,;]", m.group(1)):  # a second address hiding in the display name: refuse (returned as-is, so never allowed)
            return raw.lower()
        return m.group(2).strip().lower()
    return raw.lower()


# ── loading / validation ─────────────────────────────────────────────────

def _req(d: dict, key: str, where: str) -> Any:
    if not isinstance(d, dict) or key not in d or d[key] in (None, ""):
        raise PolicyError(f"policy: {where}{key} is required")
    return d[key]


def _emails(values: Any, where: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not values:
        raise PolicyError(f"policy: {where} must be a non-empty list of email addresses")
    out = []
    for v in values:
        if not isinstance(v, str) or not _EMAIL.match(v.strip()):
            raise PolicyError(f"policy: {where} contains an invalid email address: {v!r}")
        out.append(v.strip().lower())
    return tuple(dict.fromkeys(out))


def _int(d: dict, key: str, default: int, where: str, minimum: int = 0) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise PolicyError(f"policy: {where}{key} must be an integer >= {minimum}")
    return v


_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


def _bool(d: dict, key: str, default: bool, where: str) -> bool:
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise PolicyError(f"policy: {where}{key} must be true or false")
    return v


def _mcp(raw: Any) -> McpPolicy:
    m = {} if raw is None else raw
    if not isinstance(m, dict):
        raise PolicyError("policy: mcp must be a mapping")
    hosts = m.get("allowed_hosts") or []
    if not (isinstance(hosts, list) and all(isinstance(h, str) and _HOST.match(h.strip().lower()) for h in hosts)):
        raise PolicyError("policy: mcp.allowed_hosts must be a list of host names (no scheme, no port)")
    return McpPolicy(_bool(m, "enabled", False, "mcp."), tuple(dict.fromkeys(h.strip().lower() for h in hosts)), _bool(m, "allow_private_hosts", False, "mcp."),
                     _int(m, "max_servers_per_user", 10, "mcp.", 1), _int(m, "max_tools_per_server", 40, "mcp.", 1),
                     _int(m, "tool_timeout_seconds", 60, "mcp.", 1), _int(m, "max_result_chars", 20_000, "mcp.", 100))


def _schedules(raw: Any) -> SchedulePolicy:
    s = {} if raw is None else raw
    if not isinstance(s, dict):
        raise PolicyError("policy: schedules must be a mapping")
    return SchedulePolicy(_bool(s, "enabled", False, "schedules."), _int(s, "max_per_user", 20, "schedules.", 1),
                          _int(s, "min_interval_minutes", 15, "schedules.", 1), _int(s, "max_runs_per_day", 24, "schedules.", 1))


def _features(raw: Any, warm_mode: str) -> Features:
    f = raw or {}
    if not isinstance(f, dict):
        raise PolicyError("policy: features must be a mapping")
    unknown = sorted(set(f) - {"sandbox", "artifacts"})
    if unknown:
        raise PolicyError(f"policy: unknown features: {', '.join(map(str, unknown))} (known: sandbox, artifacts)")
    sandbox = _bool(f, "sandbox", True, "features.")
    artifacts = _bool(f, "artifacts", sandbox, "features.")  # unless said otherwise, artifacts follow the sandbox they are copied from
    if artifacts and not sandbox:
        raise PolicyError("policy: features.artifacts needs features.sandbox (artifacts are files copied out of the sandbox)")
    if warm_mode != "off" and not sandbox:
        raise PolicyError("policy: warm_sandbox.mode must be off when features.sandbox is false (there is no sandbox to keep warm)")
    return Features(sandbox, artifacts)


def parse_policy(data: Any) -> Policy:
    if not isinstance(data, dict):
        raise PolicyError("policy: the file must contain a YAML mapping")
    allowed_users = _emails(data.get("allowed_users"), "allowed_users")

    e = data.get("email")
    from_inbox = str(_req(e, "from_inbox", "email.")).strip().lower()
    if not _EMAIL.match(from_inbox):
        raise PolicyError("policy: email.from_inbox is not a valid address")
    email = EmailPolicy(from_inbox, _emails(e.get("allowed_recipients"), "email.allowed_recipients"),
                        _int(e, "max_per_day", 20, "email."), _int(e, "max_attachment_mb", 5, "email.", 1))

    g = data.get("github") or {}
    repos = g.get("allowed_repos") or []
    if not isinstance(repos, list) or not all(isinstance(r, str) and _REPO.match(r.strip()) for r in repos):
        raise PolicyError("policy: github.allowed_repos must be a list of owner/name strings")
    prefix = g.get("branch_prefix", "agent/")
    if not isinstance(prefix, str) or not prefix.endswith("/") or len(prefix) < 2:
        raise PolicyError("policy: github.branch_prefix must be a string like 'agent/'")
    github = GithubPolicy(tuple(r.strip().lower() for r in repos), prefix, bool(g.get("never_default_branch", True)))

    m = data.get("models")
    default = str(_req(m, "default", "models.")).strip()
    raw_models = _req(m, "allowed", "models.")
    if not isinstance(raw_models, list):
        raise PolicyError("policy: models.allowed must be a list")
    specs: list[ModelSpec] = []
    for position, item in enumerate(raw_models, 1):
        if not isinstance(item, dict) or not item.get("id") or not item.get("provider"):
            raise PolicyError("policy: every models.allowed entry needs id and provider")
        if item["provider"] not in KNOWN_PROVIDERS:
            raise PolicyError(f"policy: unknown provider {item['provider']!r}; known: {', '.join(KNOWN_PROVIDERS)}")
        rank = item.get("rank", position)  # without a rank, the position in the list is the rank
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise PolicyError(f"policy: models.allowed[{position}].rank must be a whole number of at least 1")
        location = item.get("location") or ""
        if not isinstance(location, str) or (location and not re.fullmatch(r"[a-z0-9-]{3,40}", location)):
            raise PolicyError(f"policy: models.allowed[{position}].location must be a region name such as us-central1 or global")
        specs.append(ModelSpec(str(item["id"]).strip(), item["provider"], str(item.get("label") or item["id"]), rank, str(item.get("use_for") or "").strip()[:200], location))
    if len({s.id for s in specs}) != len(specs):
        raise PolicyError("policy: models.allowed has duplicate ids")
    if default not in {s.id for s in specs}:
        raise PolicyError(f"policy: models.default {default!r} is not in models.allowed")

    lim = data.get("limits") or {}
    limits = Limits(  # by name: these are all integers, and a positional mix-up would be silent
        max_turn_minutes=_int(lim, "max_turn_minutes", 30, "limits.", 1), max_concurrent_turns=_int(lim, "max_concurrent_turns", 4, "limits.", 1),
        max_auto_resumes=_int(lim, "max_auto_resumes", 1, "limits."), sandbox_idle_minutes=_int(lim, "sandbox_idle_minutes", 20, "limits.", 1),
        max_helper_model_calls=_int(lim, "max_helper_model_calls", 40, "limits.", 1),
        max_auto_approvals_per_turn=_int(lim, "max_auto_approvals_per_turn", 25, "limits.", 1))
    w = data.get("warm_sandbox") or {}
    mode = w.get("mode", "off")
    if mode not in WARM_MODES:
        raise PolicyError(f"policy: warm_sandbox.mode must be one of {', '.join(WARM_MODES)}")
    warm = WarmSandbox(mode, _int(w, "spare_max_minutes", 15, "warm_sandbox.", 1), _int(w, "max_spares_per_day", 10, "warm_sandbox."))
    if warm.spare_max_minutes > 45:
        raise PolicyError("policy: warm_sandbox.spare_max_minutes must be at most 45 (a sandbox lives 60 minutes; a spare must have time left when it is used)")
    choice = m.get("helper_model_choice", False)
    if isinstance(choice, str):
        choice = {"on": True, "off": False}.get(choice.strip().lower(), choice)
    if not isinstance(choice, bool):
        raise PolicyError("policy: models.helper_model_choice must be on or off")
    ap = data.get("approvals") or {}
    never = ap.get("never_auto_tools") or []
    if not (isinstance(never, list) and all(isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9_.\-]{1,100}", t) for t in never)):
        raise PolicyError("policy: approvals.never_auto_tools must be a list of tool names")
    return Policy(allowed_users, email, github, tuple(specs), default, limits, warm, tuple(dict.fromkeys(never)), _mcp(data.get("mcp")), _schedules(data.get("schedules")), choice,
                  features=_features(data.get("features"), warm.mode))


def load_policy(path: str | os.PathLike | None = None) -> Policy:
    """Load from ``path``, else $PINBOARD_POLICY, else ./config/policy.yaml. Fails closed."""
    p = Path(path or os.environ.get("PINBOARD_POLICY") or DEFAULT_POLICY_PATH)
    try:
        text = p.read_text()
    except OSError as exc:
        raise PolicyError(f"policy file not found or unreadable: {p} ({exc.strerror or exc})") from exc
    try:
        return parse_policy(yaml.safe_load(text))
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy file {p} is not valid YAML: {exc}") from exc
