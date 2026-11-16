"""With an egress proxy (HTTPS_PROXY, docs/EGRESS.md) the engine can reach the internet ONLY through it. So every client the product uses to
call a third party must read that setting. A fake proxy records each CONNECT and refuses it: nothing leaves the machine through it.
A client that ignores the setting would connect directly instead (the keys here are fake, so that is harmless) and be reported as 'proxy saw nothing'.

Found by this check: Tavily's ASYNC path (aiohttp) ignores the proxy settings; `SyncOnlySearch` keeps every call on the sync path."""
import asyncio
import threading

import pytest

PORT_HOLDER: dict = {}


@pytest.fixture()
def proxy(monkeypatch):
    seen: list[str] = []
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def handle(r, w):
        seen.append((await r.readline()).decode(errors="replace").strip())
        w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await w.drain()
        w.close()

    async def serve():
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        PORT_HOLDER["port"] = srv.sockets[0].getsockname()[1]
        ready.set()
        async with srv:
            try:
                await srv.serve_forever()
            except asyncio.CancelledError:
                pass

    def run():
        task = loop.create_task(serve())
        PORT_HOLDER["task"] = task
        loop.run_until_complete(task)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(5)
    url = f"http://127.0.0.1:{PORT_HOLDER['port']}"
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(var, url)
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")

    def hosts() -> list[str]:
        return [line.split()[1] for line in seen if line.startswith("CONNECT")]

    yield hosts
    loop.call_soon_threadsafe(PORT_HOLDER["task"].cancel)
    t.join(5)


def test_the_github_connector_goes_through_the_proxy(proxy):
    from pinboard.integrations.github import GitHubApp, GitHubError

    app = GitHubApp("1", "unused", sleep=lambda s: None)
    with pytest.raises(GitHubError):
        app._raw("GET", "/app/installations", bearer="x")
    assert "api.github.com:443" in proxy()


def test_the_agentmail_client_goes_through_the_proxy(proxy):
    from pinboard.integrations.agentmail import AgentMailGateway

    try:
        AgentMailGateway("fake-key").list("inbox@agentmail.to")
    except Exception:  # noqa: BLE001  the proxy refuses; only WHERE the client connected matters
        pass
    assert any(h.startswith("api.agentmail.to") for h in proxy())


def test_the_mcp_clients_go_through_the_proxy(proxy):
    from pinboard.mcp.client import _client, _sse_factory

    async def go():
        for make in (lambda: _client(None, 5.0), lambda: _sse_factory()):
            async with make() as c:
                try:
                    await c.get("https://mcp.example-saas.com/")
                except Exception:  # noqa: BLE001
                    pass

    asyncio.run(go())
    assert proxy().count("mcp.example-saas.com:443") == 2


def test_web_search_goes_through_the_proxy_whether_it_is_called_sync_or_async(proxy):
    from pinboard.agent.tools.web_search import make_web_search_tool

    tool = make_web_search_tool("fake-key")
    tool.invoke({"query": "x"})
    assert proxy().count("api.tavily.com:443") == 1
    asyncio.run(tool.ainvoke({"query": "x"}))
    assert proxy().count("api.tavily.com:443") == 2


def test_the_search_client_itself_stays_on_the_proxy_even_when_called_async(proxy):
    """The case the wrapper exists for: someone calls ``client.ainvoke`` directly (Tavily's own async path ignores the proxy)."""
    from langchain_tavily import TavilySearch

    from pinboard.agent.tools.web_search import SyncOnlySearch

    inner = TavilySearch(max_results=1, tavily_api_key="fake-key")
    asyncio.run(SyncOnlySearch(inner).ainvoke({"query": "x"}))
    assert proxy().count("api.tavily.com:443") == 1


def test_tavilys_own_async_path_really_does_ignore_the_proxy_which_is_why_the_wrapper_exists(proxy):
    """If this ever starts passing the other way (a newer Tavily honours the proxy), the wrapper is no longer needed and this test says so."""
    from langchain_tavily import TavilySearch

    inner = TavilySearch(max_results=1, tavily_api_key="fake-key")
    try:
        asyncio.run(asyncio.wait_for(inner.ainvoke({"query": "x"}), timeout=15))
    except Exception:  # noqa: BLE001
        pass
    assert "api.tavily.com:443" not in proxy()


# ── servers inside our own network are contacted directly ────────────────
# Found on the real engine: httpx ignores address ranges in NO_PROXY (requests and Go honour them), and a host NAME can never match a range, so every MCP
# server in the VPC was sent to the proxy and refused (HTTP 403). The MCP client now decides itself: private destination = direct, public = the proxy.

def fake_dns(table):
    return lambda host, port: [(2, 1, 6, "", (a, 0)) for a in table[host]] if host in table else (_ for _ in ()).throw(OSError("unknown"))


@pytest.mark.parametrize("url,table,expected", [
    ("http://192.168.40.10:8000/mcp", {}, True),
    ("https://10.1.2.3/mcp", {}, True),
    ("http://calc.mcp.internal:8000/mcp", {"calc.mcp.internal": ["192.168.40.10"]}, True),
    ("https://mcp.vendor.example/mcp", {"mcp.vendor.example": ["93.184.216.34"]}, False),
    ("https://93.184.216.34/mcp", {}, False),
    ("http://mixed.example/mcp", {"mixed.example": ["192.168.40.10", "93.184.216.34"]}, False),   # one public address: stay on the proxy
    ("http://nowhere.example/mcp", {}, False),                                                      # cannot be resolved: keep the safe default
])
def test_a_private_destination_goes_direct_and_everything_else_keeps_the_proxy_settings(url, table, expected):
    from pinboard.mcp.client import goes_direct

    assert goes_direct(url, fake_dns(table)) is expected


def test_an_httpx_client_really_does_send_a_private_address_to_the_proxy_unless_told_not_to(proxy):
    """Proves why the decision is ours: with the environment's proxy settings, httpx sends 127.0.0.2 (private, not listed) to the proxy."""
    from pinboard.mcp.client import _client

    async def go(direct):
        async with _client(None, 2.0, direct) as c:
            try:
                await c.get("https://127.0.0.2:9/")
            except Exception:  # noqa: BLE001  refused either way; only WHERE it went matters
                pass

    asyncio.run(go(False))
    assert proxy().count("127.0.0.2:9") == 1       # through the proxy: this is what made every server in the VPC fail
    asyncio.run(go(True))
    assert proxy().count("127.0.0.2:9") == 1       # direct: the proxy saw nothing new


def test_the_mcp_session_contacts_a_private_server_directly_and_a_public_one_through_the_proxy(proxy):
    from pinboard.mcp.client import McpClient

    client = McpClient(connect_timeout=2.0)
    for url in ("https://127.0.0.2:9/mcp", "https://mcp.example-saas.com/mcp"):
        try:
            asyncio.run(client.list_tools(url, {}, "http", 3.0))
        except Exception:  # noqa: BLE001
            pass
    assert "127.0.0.2:9" not in " ".join(proxy())   # private: direct
    assert "mcp.example-saas.com:443" in proxy()     # public: the proxy
