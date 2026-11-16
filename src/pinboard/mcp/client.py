"""The connection to a remote MCP server, over the official ``mcp`` SDK (streamable HTTP, or the older SSE transport).

Every operation opens its own short-lived session (connect, initialise, do the one thing, close). That is the simplest thing that is correct for an
engine that may be replaced between two calls: nothing to keep alive, nothing to leak, and a server that restarts is simply reconnected to."""
from __future__ import annotations

import asyncio
import functools
import ipaddress
import socket
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, TypeVar
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

T = TypeVar("T")
MAX_TOOLS = 200


class McpCallError(RuntimeError):
    """A connection or call failed; the message is safe to show (no secrets)."""


@dataclass
class CallResult:
    text: str
    is_error: bool = False


def _root(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def explain(exc: BaseException) -> str:
    """One readable line for whatever the SDK or the network raised."""
    exc = _root(exc)
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return "the server did not answer in time"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        hint = (" (check the token in the headers)" if status in (401, 403) else " (check the URL)" if status == 404
                else " (the server redirected; redirects are not followed, use the final address)" if 300 <= status < 400 else "")
        return f"the server answered HTTP {status}{hint}"
    name = type(exc).__name__
    text = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
    return f"{name}: {text}" if text else name


READ_SECONDS = 300.0


def goes_direct(url: str, resolver=socket.getaddrinfo) -> bool:
    """True when every address behind ``url`` is private (inside our own network): such a server is contacted DIRECTLY, never through the egress proxy.
    httpx does not understand address ranges in NO_PROXY (requests and Go do), and a host NAME can never match a range, so the decision is made here.
    Anything public, or anything that cannot be resolved, keeps the environment's proxy settings (a public host is reachable only through the proxy)."""
    host = (urlsplit(url).hostname or "").strip("[]")
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in resolver(host, None)]
        except (OSError, ValueError):
            return False
    return bool(addrs) and not any(a.is_global for a in addrs)


def _client(headers: dict[str, str] | None, timeout: float, direct: bool = False) -> httpx.AsyncClient:
    """Redirects are NOT followed. The address was checked before connecting (https, public host); a server that answered with a redirect could
    otherwise send the engine on to an address that would never have passed that check (an internal service, the metadata server).
    ``direct`` ignores the proxy settings of the environment (a server inside our own network)."""
    return httpx.AsyncClient(headers=headers or None, timeout=httpx.Timeout(timeout, read=READ_SECONDS), follow_redirects=False, trust_env=not direct)


def _sse_factory(headers=None, timeout=None, auth=None, direct: bool = False) -> httpx.AsyncClient:   # what sse_client calls to make its client
    return httpx.AsyncClient(headers=headers, timeout=timeout or httpx.Timeout(30.0), auth=auth, follow_redirects=False, trust_env=not direct)


@asynccontextmanager
async def _session(url: str, headers: dict[str, str], transport: str, timeout: float):
    direct = await asyncio.to_thread(goes_direct, url)   # a DNS lookup: keep it off the event loop
    async with AsyncExitStack() as stack:
        if transport == "sse":
            factory = functools.partial(_sse_factory, direct=direct)
            streams = await stack.enter_async_context(sse_client(url, headers=headers or None, timeout=timeout, httpx_client_factory=factory))
        else:
            http = await stack.enter_async_context(_client(headers, timeout, direct))
            streams = await stack.enter_async_context(streamable_http_client(url, http_client=http))
        async with ClientSession(streams[0], streams[1]) as session:
            await asyncio.wait_for(session.initialize(), timeout)
            yield session


def _render(content: list[Any]) -> str:
    parts = []
    for c in content or []:
        kind = getattr(c, "type", "")
        if kind == "text":
            parts.append(getattr(c, "text", ""))
        elif kind == "image":
            parts.append(f"[image: {getattr(c, 'mimeType', 'unknown type')} omitted]")
        elif kind == "resource":
            res = getattr(c, "resource", None)
            text = getattr(res, "text", None)
            parts.append(text if isinstance(text, str) else f"[resource: {getattr(res, 'uri', 'unknown')}]")
        else:
            parts.append(f"[{kind or 'content'} omitted]")
    return "\n".join(p for p in parts if p)


class McpClient:
    def __init__(self, connect_timeout: float = 15.0):
        self.connect_timeout = connect_timeout

    async def list_tools(self, url: str, headers: dict[str, str], transport: str = "http", timeout: float = 20.0) -> list[dict]:
        async def go() -> list[dict]:
            async with _session(url, headers, transport, self.connect_timeout) as s:
                tools, cursor = [], None
                while len(tools) < MAX_TOOLS:
                    page = await s.list_tools(cursor) if cursor else await s.list_tools()
                    tools += [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema or {"type": "object", "properties": {}}} for t in page.tools]
                    cursor = getattr(page, "nextCursor", None)
                    if not cursor:
                        break
                return tools[:MAX_TOOLS]

        try:
            return await asyncio.wait_for(go(), timeout)
        except BaseException as exc:  # noqa: BLE001  the SDK raises exception groups; anything that is not a cancellation becomes one clear error
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise McpCallError(explain(exc)) from None

    async def call_tool(self, url: str, headers: dict[str, str], transport: str, name: str, arguments: dict, timeout: float = 60.0) -> CallResult:
        async def go() -> CallResult:
            async with _session(url, headers, transport, self.connect_timeout) as s:
                res = await s.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=timeout))
                return CallResult(_render(res.content), bool(getattr(res, "isError", False)))

        try:
            return await asyncio.wait_for(go(), timeout + self.connect_timeout)
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise McpCallError(explain(exc)) from None


def run_sync(coro: Awaitable[T]) -> T:
    """Run a coroutine to completion from synchronous code (a tool called by the agent's worker thread). If a loop is already running in this thread,
    run it in a fresh thread instead of blocking that loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]
