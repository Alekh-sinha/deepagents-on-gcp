"""Bring-your-own MCP: a user connects a remote MCP server (already deployed, reached over HTTP) by pasting the standard ``mcpServers`` JSON.
The agent then sees that server's tools (named ``mcp__<server>__<tool>``), each behind a human approval unless the user marked it safe.

config.py    parse the pasted JSON (paste-compatible with Claude Desktop / Cursor entries that have a ``url``)
security.py  which URLs may be connected to (https, public hosts, optional allow-list)
crypto.py    header values (tokens) are stored encrypted and never leave the engine
client.py    the connection: list a server's tools, call one (the official ``mcp`` SDK)
tools.py     turn a user's saved servers into agent tools and approval gates
"""
from __future__ import annotations

from pinboard.mcp.client import McpCallError, McpClient
from pinboard.mcp.config import McpConfigError, ServerInput, parse_config
from pinboard.mcp.crypto import HeaderCipher, McpUnavailable, generate_key
from pinboard.mcp.security import McpUrlError, check_url
from pinboard.mcp.tools import McpTools, build_mcp_tools, tool_name

__all__ = ["HeaderCipher", "McpCallError", "McpClient", "McpConfigError", "McpTools", "McpUnavailable", "McpUrlError", "ServerInput", "build_mcp_tools",
           "check_url", "generate_key", "parse_config", "tool_name"]
