#!/usr/bin/env python3
"""Egress control: which third-party hosts the engine may reach, and the proxy settings that make it use the proxy. See docs/EGRESS.md.

    python deploy/egress.py hosts      the allowed host names, one per line (what `deploy.sh network` puts into the proxy rule)
    python deploy/egress.py matcher    the proxy rule's session matcher built from them
    python deploy/egress.py vpc_range  the address block reserved for MCP servers inside the VPC (mcp.vpc_ranges of the policy)

One list feeds everything: the built-in hosts of the connectors, the MCP hosts of config/policy.yaml (`mcp.allowed_hosts`) and
`egress_allowed_hosts` of deploy/config*.yaml. Nothing here talks to the cloud.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Mapping


sys.path.insert(0, str(Path(__file__).parent))
import deploy_config  # noqa: E402

# Hosts the product's own connectors call (Tavily search, GitHub App, AgentMail). Google APIs are NOT listed: they are reached privately and
# stay out of the proxy (NO_PROXY below); IAM is what limits them.
BUILTIN_HOSTS = ("api.tavily.com", "api.github.com", "api.agentmail.to")

# Never sent to the proxy: Google APIs (private access), the metadata server, this machine, and every private address range (a private
# Cloud SQL address, a server inside your own VPC, the link-local metadata address). CIDR entries are honoured by requests, httpx and Go.
NO_PROXY = (".googleapis.com", "googleapis.com", "metadata.google.internal", "localhost", "127.0.0.1",
            "169.254.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def _policy_path(given: Path | None) -> Path:
    """The policy file: the one passed in, else PINBOARD_POLICY (the variable the app itself honours), else config/policy.yaml."""
    return given or Path(os.environ.get("PINBOARD_POLICY") or deploy_config.POLICY_FILE)


def policy_mcp_hosts(policy_file: Path | None = None) -> list[str]:
    hosts = (deploy_config.read_policy_data(_policy_path(policy_file)).get("mcp") or {}).get("allowed_hosts") or []
    return [str(h).strip().lower() for h in hosts if isinstance(h, str)]


def policy_vpc_ranges(policy_file: Path | None = None) -> list[str]:
    """The CIDR block(s) reserved for MCP servers (config/policy.yaml: mcp.vpc_ranges). The network is built from the same list the app enforces."""
    ranges = (deploy_config.read_policy_data(_policy_path(policy_file)).get("mcp") or {}).get("vpc_ranges") or []
    return [str(r).strip() for r in ranges if isinstance(r, str)]


def allowed_hosts(cfg: Mapping[str, Any], policy_file: Path | None = None) -> list[str]:
    """Built-in + MCP + configured hosts, de-duplicated, validated (the result goes into a proxy rule, so nothing odd may pass)."""
    out: list[str] = []
    for h in (*BUILTIN_HOSTS, *policy_mcp_hosts(policy_file), *cfg.get("egress_allowed_hosts", [])):
        if not deploy_config.HOST_RE.match(h):
            raise deploy_config.ConfigError(f"{h!r} is not a valid host name for the egress allowlist")
        if h not in out:
            out.append(h)
    return out


def session_matcher(hosts: list[str]) -> str:
    """The proxy rule's CEL expression: exact host names only (no wildcards)."""
    if not hosts:
        raise deploy_config.ConfigError("the egress allowlist is empty")
    return " || ".join(f"host() == '{h}'" for h in hosts)


def proxy_env(cfg: Mapping[str, Any]) -> dict[str, str]:
    """Environment variables for the engine so every client uses the proxy (empty when no proxy is configured)."""
    proxy = str(cfg.get("egress_proxy") or "")
    if not proxy:
        return {}
    domains = [d.rstrip(".") for d in cfg.get("network_dns_domains", [])]   # private zones: names under them live inside the VPC
    no_proxy = ",".join([*NO_PROXY, *[x for d in domains for x in (d, "." + d)]])
    return {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "NO_PROXY": no_proxy, "https_proxy": proxy, "http_proxy": proxy, "no_proxy": no_proxy}


def main(argv: list[str] | None = None) -> int:
    cmd = (sys.argv[1:] if argv is None else argv or ["hosts"])[0:1] or ["hosts"]
    try:
        hosts = allowed_hosts(deploy_config.load())
    except deploy_config.ConfigError as exc:
        print(f"egress config error: {exc}", file=sys.stderr)
        return 2
    if cmd[0] == "hosts":
        print("\n".join(hosts))
    elif cmd[0] == "matcher":
        print(session_matcher(hosts))
    elif cmd[0] == "vpc_range":
        print("\n".join(policy_vpc_ranges()))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
