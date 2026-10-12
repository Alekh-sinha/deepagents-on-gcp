"""Bring-your-own MCP servers, as the service offers them: save/import/test/toggle/delete a user's remote MCP servers, and hand the agent their tools.

A server is reached by URL over HTTP (never started by a command). The user pastes the standard ``mcpServers`` JSON; each server is checked
(https, public address, optional host allow-list), tested by listing its tools, and saved with its header values encrypted. Clients never see header
values, only their names."""
from __future__ import annotations

import logging
import socket
from typing import Any

from pinboard.engine.errors import BadRequest, NotFound
from pinboard.mcp import (HeaderCipher, McpCallError, McpClient, McpConfigError, McpTools, McpUnavailable, McpUrlError, build_mcp_tools, check_url, parse_config)
from pinboard.mcp.client import run_sync
from pinboard.mcp.config import ServerInput, clean_headers, valid_name
from pinboard.mcp.crypto import cipher_from_vault
from pinboard.state import McpServerRecord, new_mcp_id, utcnow

log = logging.getLogger(__name__)


class McpMixin:
    """The MCP side of RunService (needs self.store, self.vault, self.policy, self.settings, self.clock, self.mcp_client, self._mcp_resolver)."""

    def _mcp_on(self):
        p = self.policy.mcp
        if not p.enabled:
            raise BadRequest("MCP servers are switched off in this deployment (policy mcp.enabled)")
        return p

    def _cipher(self) -> HeaderCipher:
        try:
            return cipher_from_vault(self.vault)
        except McpUnavailable as exc:
            raise BadRequest(str(exc)) from exc

    def _mcp_user(self, user_id: str | None) -> str:
        return user_id or self.settings.user_id

    # ── reading ──
    def list_mcp_servers(self, *, user_id: str | None = None) -> list[dict]:
        return [s.to_dict() for s in self.store.list_mcp_servers(self._mcp_user(user_id))]

    def _server(self, name: str, user_id: str | None) -> McpServerRecord:
        srv = self.store.get_mcp_server(self._mcp_user(user_id), valid_name_or_404(name))
        if srv is None:
            raise NotFound("MCP server not found")
        return srv

    # ── saving ──
    def put_mcp_server(self, name: str, url: str, *, headers: dict | None = None, transport: str | None = None, enabled: bool = True,
                       requires_approval: bool = True, no_approval_tools: list[str] | None = None, user_id: str | None = None, test: bool = True) -> dict:
        """Save (or replace) one server. ``headers=None`` on an existing server keeps its stored headers; ``{}`` clears them. The server is tested
        (its tools are listed) and the result is returned with it: a server that cannot be reached is still saved, with the error, so it can be fixed."""
        policy, uid = self._mcp_on(), self._mcp_user(user_id)
        try:
            key = valid_name(name)
            safe_url = check_url(url, policy, self._mcp_resolver)
            clean = clean_headers(headers) if headers is not None else None
        except (McpConfigError, McpUrlError) as exc:
            raise BadRequest(str(exc)) from exc
        if transport not in (None, "http", "sse"):
            raise BadRequest("transport must be 'http' or 'sse'")
        existing = self.store.get_mcp_server(uid, key)
        if existing is None and len(self.store.list_mcp_servers(uid)) >= policy.max_servers_per_user:
            raise BadRequest(f"at most {policy.max_servers_per_user} MCP servers per user; remove one first")
        cipher = self._cipher()
        if clean is None:
            headers_enc, header_names = (existing.headers_enc, existing.header_names) if existing else ("", [])
        else:
            headers_enc, header_names = cipher.encrypt(clean), sorted(clean)
        rec = McpServerRecord(id=existing.id if existing else new_mcp_id(), user_id=uid, name=key, url=safe_url,
                              transport=transport or (existing.transport if existing and existing.url == safe_url else ("sse" if safe_url.rstrip("/").endswith("/sse") else "http")),
                              headers_enc=headers_enc, header_names=header_names, enabled=enabled, requires_approval=requires_approval,
                              no_approval_tools=list(no_approval_tools or (existing.no_approval_tools if existing else [])),
                              tools=[] if existing is None or existing.url != safe_url else existing.tools,
                              created_at=existing.created_at if existing else utcnow())
        rec = self.store.put_mcp_server(rec)
        self.store.add_audit(uid, None, "mcp.saved", {"server": key, "url": safe_url, "headers": header_names}, self.clock())
        return self.test_mcp_server(key, user_id=uid) if test else rec.to_dict()

    def import_mcp_config(self, config: Any, *, user_id: str | None = None) -> dict:
        """Connect every server in a pasted ``mcpServers`` JSON. One bad server does not stop the others."""
        self._mcp_on()
        try:
            servers = parse_config(config)
        except McpConfigError as exc:
            raise BadRequest(str(exc)) from exc
        results = []
        for s in servers:
            try:
                saved = self.put_mcp_server(s.name, s.url, headers=s.headers, transport=s.transport, enabled=s.enabled, user_id=user_id)
                results.append({"name": s.name, "saved": True, "reachable": not saved.get("last_error"), "tool_count": saved.get("tool_count", 0),
                                "error": saved.get("last_error") or "", "warnings": s.warnings})
            except BadRequest as exc:
                results.append({"name": s.name, "saved": False, "reachable": False, "tool_count": 0, "error": str(exc), "warnings": s.warnings})
        return {"results": results}

    def test_mcp_server(self, name: str, *, user_id: str | None = None) -> dict:
        """Connect, list the tools and remember them (this is also how the cached tool list is refreshed). Never raises for an unreachable server."""
        policy, uid = self._mcp_on(), self._mcp_user(user_id)
        srv = self._server(name, uid)
        try:
            check_url(srv.url, policy, self._mcp_resolver)  # the address is checked again every time: a name can start pointing somewhere else
            headers = self._cipher().decrypt(srv.headers_enc)
            tools = run_sync(self.mcp_client.list_tools(srv.url, headers, srv.transport, float(policy.tool_timeout_seconds)))
            tools = tools[: policy.max_tools_per_server]
            keep = [t for t in srv.no_approval_tools if t in {x["name"] for x in tools}]
            updated = self.store.update_mcp_server(uid, srv.name, tools=tools, tools_checked_at=self.clock(), last_error="", no_approval_tools=keep)
        except (McpCallError, McpUrlError, McpUnavailable, BadRequest) as exc:
            log.info("MCP server %s of %s failed its test: %s", srv.name, uid, exc)
            updated = self.store.update_mcp_server(uid, srv.name, last_error=str(exc)[:300], tools_checked_at=self.clock())
        return updated.to_dict()

    def update_mcp_server(self, name: str, changes: dict, *, user_id: str | None = None) -> dict:
        """Switch a server on/off, ask-before-every-call on/off, or mark individual tools as safe (no approval)."""
        self._mcp_on()
        uid, srv = self._mcp_user(user_id), self._server(name, user_id)
        allowed = {"enabled", "requires_approval", "no_approval_tools"}
        if not changes or set(changes) - allowed:
            raise BadRequest(f"send at least one of: {', '.join(sorted(allowed))}")
        fields: dict[str, Any] = {}
        for k in ("enabled", "requires_approval"):
            if k in changes:
                if not isinstance(changes[k], bool):
                    raise BadRequest(f"'{k}' must be true or false")
                fields[k] = changes[k]
        if "no_approval_tools" in changes:
            tools = changes["no_approval_tools"]
            known = {t["name"] for t in srv.tools}
            if not (isinstance(tools, list) and all(isinstance(t, str) for t in tools)) or set(tools) - known:
                raise BadRequest("no_approval_tools must be a list of this server's tool names")
            fields["no_approval_tools"] = sorted(set(tools))
        updated = self.store.update_mcp_server(uid, srv.name, **fields)
        self.store.add_audit(uid, None, "mcp.updated", {"server": srv.name, **{k: v for k, v in fields.items()}}, self.clock())
        return updated.to_dict()

    def delete_mcp_server(self, name: str, *, user_id: str | None = None) -> dict:
        uid = self._mcp_user(user_id)
        if not self.store.delete_mcp_server(uid, valid_name_or_404(name)):
            raise NotFound("MCP server not found")
        self.store.add_audit(uid, None, "mcp.deleted", {"server": name}, self.clock())
        return {"deleted": name}

    # ── what the agent gets ──
    def mcp_tools_for(self, user_id: str, audit=None) -> McpTools | None:
        """The user's enabled, tested servers as agent tools. Never raises: a turn must not fail because of MCP."""
        try:
            policy = self.policy.mcp
            if not policy.enabled:
                return None
            servers = [s for s in self.store.list_mcp_servers(user_id) if s.enabled and s.tools]
            if not servers:
                return None
            return build_mcp_tools(servers, policy, cipher_from_vault(self.vault), self.mcp_client, audit)
        except Exception:  # noqa: BLE001
            log.exception("could not prepare MCP tools for %s", user_id)
            return None


def valid_name_or_404(name: str) -> str:
    try:
        return valid_name(name)
    except McpConfigError as exc:
        raise NotFound("MCP server not found") from exc


DEFAULT_RESOLVER = socket.getaddrinfo
__all__ = ["McpMixin", "McpClient", "DEFAULT_RESOLVER", "ServerInput"]
