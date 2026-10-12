"""Which URLs the engine may connect to on a user's behalf. A user-supplied URL is a way to make the engine send requests, so it is checked:
https only, no credentials in the URL, an optional allow-list of host names, and the host must resolve to PUBLIC addresses only (no loopback,
private ranges, link-local or cloud metadata) unless the policy says ``allow_private_hosts`` (local development)."""
from __future__ import annotations

import ipaddress
import socket
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from pinboard.core.policy import McpPolicy


class McpUrlError(ValueError):
    pass


BLOCKED_NAMES = ("localhost", "metadata.google.internal", "metadata")
BLOCKED_SUFFIXES = (".internal", ".local", ".localhost")


def _addresses(host: str, resolver: Callable) -> list[ipaddress._BaseAddress]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = resolver(host, None)
    except OSError as exc:
        raise McpUrlError(f"cannot resolve host {host!r} ({exc})") from exc
    addrs = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0].split("%")[0]))
        except ValueError:
            continue
    if not addrs:
        raise McpUrlError(f"host {host!r} has no usable address")
    return addrs


def check_url(url: str, policy: McpPolicy, resolver: Callable = socket.getaddrinfo) -> str:
    """Returns the normalised URL, or raises McpUrlError saying what is wrong."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError as exc:
        raise McpUrlError(f"not a valid URL ({exc})") from exc
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host:
        raise McpUrlError("the URL must start with https:// and name a host")
    if parts.scheme == "http" and not policy.allow_private_hosts:
        raise McpUrlError("only https:// URLs are allowed")
    if parts.username or parts.password:
        raise McpUrlError("the URL must not contain a user name or password; put credentials in a header")
    if policy.allowed_hosts and host not in policy.allowed_hosts:
        raise McpUrlError(f"host {host!r} is not on the allowed list: {', '.join(policy.allowed_hosts)}")
    if not policy.allow_private_hosts:
        if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES):
            raise McpUrlError(f"host {host!r} is not a public address")
        for addr in _addresses(host, resolver):
            if not addr.is_global:
                raise McpUrlError(f"host {host!r} resolves to {addr}, which is not a public address")
    netloc = host if port is None else f"{host}:{port}"
    if ":" in host:  # an IPv6 literal needs its brackets back
        netloc = f"[{host}]" + ("" if port is None else f":{port}")
    return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
