"""Parse the JSON a user pastes to connect MCP servers. The same ``mcpServers`` shape other MCP clients use, for servers reached by URL:

    {"mcpServers": {"weather": {"url": "https://tools.example.com/mcp", "headers": {"Authorization": "Bearer abc"}}}}

Not supported (and refused with a reason): servers started by a command (``command`` / ``args``) — those would run on the engine's own machine —
and ``${VAR}`` references, because the engine has no environment of yours to fill them from.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
MAX_HEADERS = 20
MAX_HEADER_VALUE = 4096
HTTP_TYPES = {"http", "https", "streamable-http", "streamable_http", "streamablehttp"}
SSE_TYPES = {"sse"}
STDIO_KEYS = ("command", "args", "env", "cwd")


class McpConfigError(ValueError):
    pass


@dataclass
class ServerInput:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    transport: str = "http"          # http = streamable HTTP, sse = the older server-sent-events transport
    enabled: bool = True
    warnings: list[str] = field(default_factory=list)


def valid_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not NAME_RE.match(n):
        raise McpConfigError(f"server name {name!r} is not valid: use 1-32 lower-case letters, digits, '-' or '_', starting with a letter")
    return n


def clean_headers(headers: Any, where: str = "headers") -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise McpConfigError(f"{where} must be an object of header names and values")
    if len(headers) > MAX_HEADERS:
        raise McpConfigError(f"{where}: at most {MAX_HEADERS} headers")
    out: dict[str, str] = {}
    for k, v in headers.items():
        if not isinstance(k, str) or not HEADER_NAME_RE.match(k):
            raise McpConfigError(f"{where}: {k!r} is not a valid header name")
        if not isinstance(v, str) or len(v) > MAX_HEADER_VALUE or re.search(r"[\r\n\x00]", v):
            raise McpConfigError(f"{where}: the value of {k} must be text of at most {MAX_HEADER_VALUE} characters on one line")
        if "${" in v:
            raise McpConfigError(f"{where}: {k} contains a ${{VAR}} reference; paste the actual value (the engine cannot fill in variables)")
        out[k] = v
    return out


def parse_server(name: str, raw: Any) -> ServerInput:
    key = valid_name(name)
    if not isinstance(raw, dict):
        raise McpConfigError(f"{name}: the entry must be an object")
    if any(k in raw for k in STDIO_KEYS) and not raw.get("url"):
        raise McpConfigError(f"{name}: servers started by a command (stdio) are not supported here; connect a server that is already deployed, by its URL")
    url = raw.get("url") or raw.get("serverUrl")
    if not isinstance(url, str) or not url.strip():
        raise McpConfigError(f"{name}: a 'url' is required")
    kind = str(raw.get("type") or raw.get("transport") or "").strip().lower()
    if kind and kind not in HTTP_TYPES | SSE_TYPES:
        raise McpConfigError(f"{name}: transport {kind!r} is not supported (use http or sse)")
    transport = "sse" if kind in SSE_TYPES else "http"
    enabled = not bool(raw.get("disabled", False)) and bool(raw.get("enabled", True))
    known = ("url", "serverUrl", "type", "transport", "headers", "disabled", "enabled", "description", *STDIO_KEYS)
    warnings = [f"{name}: ignored setting '{k}'" for k in raw if k not in known]
    return ServerInput(key, url.strip(), clean_headers(raw.get("headers"), f"{name}.headers"), transport, enabled, warnings)


def parse_config(raw: Any) -> list[ServerInput]:
    """The pasted text (JSON string) or an already-parsed object -> the servers in it. Raises McpConfigError with every problem found."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise McpConfigError(f"that is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise McpConfigError("expected a JSON object like {\"mcpServers\": {\"name\": {\"url\": \"https://...\"}}}")
    servers = raw.get("mcpServers", raw.get("servers"))
    if servers is None:
        servers = raw if raw and all(isinstance(v, dict) for v in raw.values()) else None
    if not isinstance(servers, dict) or not servers:
        raise McpConfigError("no servers found: put them under \"mcpServers\" as {\"name\": {\"url\": \"https://...\"}}")
    out, problems, seen = [], [], set()
    for name, entry in servers.items():
        try:
            parsed = parse_server(name, entry)
            if parsed.name in seen:
                raise McpConfigError(f"{name}: duplicate server name")
            seen.add(parsed.name)
            out.append(parsed)
        except McpConfigError as exc:
            problems.append(str(exc))
    if problems:
        raise McpConfigError("; ".join(problems))
    return out
