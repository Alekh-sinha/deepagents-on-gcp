"""A real MCP server (the official SDK's FastMCP, streamable HTTP) running on localhost in a thread, for tests that must prove the SDK path end to end."""
import socket
import threading
import time
from contextlib import contextmanager

import uvicorn
from mcp.server.fastmcp import FastMCP


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running_server(token: str | None = None, transport: str = "streamable-http", redirect_to: str = "/mcp"):
    """Yields (url, seen) where ``seen`` collects the Authorization header of every request. With ``token``, requests without it get 401."""
    port, seen = free_port(), []
    mcp = FastMCP("test-server", host="127.0.0.1", port=port)

    @mcp.tool()
    def add(a: int, b: int) -> str:
        """Add two whole numbers."""
        return str(a + b)

    @mcp.tool()
    def shout(text: str, times: int = 1) -> str:
        """Repeat text in capitals."""
        return " ".join([text.upper()] * times)

    @mcp.tool()
    def boom() -> str:
        """Always fails."""
        raise ValueError("this tool always fails")

    @mcp.tool()
    def long_text() -> str:
        """Returns a very long text."""
        return "x" * 5000

    inner = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/go"):   # a server that sends you somewhere else
            await send({"type": "http.response.start", "status": 307, "headers": [(b"location", redirect_to.encode())]})
            await send({"type": "http.response.body", "body": b""})
            return
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            got = headers.get(b"authorization", b"").decode()
            seen.append(got)
            if token and got != f"Bearer {token}":
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await inner(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}" + ("/mcp" if transport == "streamable-http" else "/sse"), seen
    finally:
        server.should_exit = True
        thread.join(5)
