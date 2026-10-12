"""Bring-your-own MCP, the building blocks: the pasted config, URL safety, header encryption, the tools, and a real server over HTTP."""
import asyncio
import json

import pytest

from pinboard.core.policy import McpPolicy
from pinboard.mcp import (HeaderCipher, McpCallError, McpClient, McpConfigError, McpUnavailable, McpUrlError, build_mcp_tools, check_url, generate_key,
                          parse_config, tool_name)
from pinboard.mcp.tools import clean_schema
from pinboard.state import McpServerRecord

from mcp_fixtures import running_server

PUBLIC = lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))]          # noqa: E731  a resolver that says "public address"
PRIVATE = lambda host, port: [(2, 1, 6, "", ("10.0.0.7", 0))]                # noqa: E731
LOCAL = McpPolicy(enabled=True, allow_private_hosts=True)


# ── the pasted config ────────────────────────────────────────────────────

def test_the_standard_mcpservers_json_is_understood():
    servers = parse_config(json.dumps({"mcpServers": {
        "Weather": {"url": "https://tools.example.com/mcp", "headers": {"Authorization": "Bearer abc"}},
        "docs": {"type": "sse", "url": "https://docs.example.com/sse", "disabled": True}}}))
    assert [(s.name, s.url, s.transport, s.enabled) for s in servers] == [("weather", "https://tools.example.com/mcp", "http", True), ("docs", "https://docs.example.com/sse", "sse", False)]
    assert servers[0].headers == {"Authorization": "Bearer abc"}


def test_a_bare_mapping_and_an_object_are_accepted_too():
    assert parse_config({"a": {"url": "https://x.example.com/mcp"}})[0].name == "a"
    assert parse_config({"servers": {"b": {"url": "https://x.example.com/mcp"}}})[0].name == "b"


@pytest.mark.parametrize("raw,why", [
    ("{not json", "not valid JSON"), ("[]", "expected a JSON object"), ({}, "no servers"), ({"mcpServers": {}}, "no servers"),
    ({"mcpServers": {"a": {"command": "npx", "args": ["x"]}}}, "stdio"),
    ({"mcpServers": {"a": {}}}, "'url' is required"), ({"mcpServers": {"a": "https://x"}}, "must be an object"),
    ({"mcpServers": {"Bad Name": {"url": "https://x.example.com"}}}, "not valid"), ({"mcpServers": {"1a": {"url": "https://x.example.com"}}}, "not valid"),
    ({"mcpServers": {"a": {"url": "https://x.example.com", "type": "grpc"}}}, "not supported"),
    ({"mcpServers": {"a": {"url": "https://x.example.com", "headers": {"A": "${TOKEN}"}}}}, r"\$\{VAR\}"),
    ({"mcpServers": {"a": {"url": "https://x.example.com", "headers": {"bad name": "v"}}}}, "valid header name"),
    ({"mcpServers": {"a": {"url": "https://x.example.com", "headers": {"A": "line\nbreak"}}}}, "one line"),
    ({"mcpServers": {"a": {"url": "https://x.example.com", "headers": ["x"]}}}, "object"),
    ({"mcpServers": {"A": {"url": "https://x.example.com"}, "a": {"url": "https://y.example.com"}}}, "duplicate"),
])
def test_a_bad_config_is_refused_with_the_reason(raw, why):
    with pytest.raises(McpConfigError, match=why):
        parse_config(raw)


def test_every_problem_is_reported_not_just_the_first():
    with pytest.raises(McpConfigError) as e:
        parse_config({"mcpServers": {"a": {}, "b": {"command": "x"}}})
    assert "a:" in str(e.value) and "b:" in str(e.value)


# ── which URLs may be connected to ───────────────────────────────────────

def test_only_public_https_urls_are_allowed_by_default():
    p = McpPolicy(enabled=True)
    assert check_url("https://Tools.Example.com/mcp?x=1#frag", p, PUBLIC) == "https://tools.example.com/mcp?x=1"
    assert check_url("https://tools.example.com:8443", p, PUBLIC) == "https://tools.example.com:8443/"
    for bad, why in [("http://tools.example.com/mcp", "https"), ("ftp://x.example.com", "https"), ("https://user:pw@x.example.com/", "user name"), ("", "https"),
                     ("https://localhost/mcp", "public"), ("https://metadata.google.internal/x", "public"), ("https://db.internal/", "public"),
                     ("https://127.0.0.1/mcp", "not a public"), ("https://169.254.169.254/latest", "not a public"), ("https://[::1]/mcp", "not a public"),
                     ("https://192.168.1.5/mcp", "not a public"), ("https://100.64.0.1/", "not a public")]:
        with pytest.raises(McpUrlError, match=why):
            check_url(bad, p, PUBLIC)


def test_a_name_that_resolves_to_a_private_address_is_refused():
    with pytest.raises(McpUrlError, match="not a public address"):
        check_url("https://sneaky.example.com/mcp", McpPolicy(enabled=True), PRIVATE)
    with pytest.raises(McpUrlError, match="cannot resolve"):
        check_url("https://nope.example.com/mcp", McpPolicy(enabled=True), lambda h, p: (_ for _ in ()).throw(OSError("no such host")))


def test_the_allow_list_restricts_hosts_and_does_not_waive_the_address_check():
    p = McpPolicy(enabled=True, allowed_hosts=("tools.example.com",))
    assert check_url("https://tools.example.com/mcp", p, PUBLIC)
    with pytest.raises(McpUrlError, match="allowed list"):
        check_url("https://other.example.com/mcp", p, PUBLIC)
    with pytest.raises(McpUrlError, match="not a public address"):
        check_url("https://tools.example.com/mcp", p, PRIVATE)


def test_local_development_may_use_http_and_private_addresses():
    assert check_url("http://127.0.0.1:8000/mcp", LOCAL) == "http://127.0.0.1:8000/mcp"


# ── header encryption ────────────────────────────────────────────────────

def test_headers_are_encrypted_and_only_the_right_key_reads_them():
    key = generate_key()
    c = HeaderCipher(key)
    token = c.encrypt({"Authorization": "Bearer s3cret"})
    assert "s3cret" not in token and c.decrypt(token) == {"Authorization": "Bearer s3cret"}
    assert c.encrypt({}) == "" and c.decrypt("") == {}
    with pytest.raises(McpUnavailable, match="cannot be read"):
        HeaderCipher(generate_key()).decrypt(token)
    with pytest.raises(McpUnavailable, match="not a valid key"):
        HeaderCipher("short")


# ── the tools ────────────────────────────────────────────────────────────

class FakeClient:
    def __init__(self, text="ok", is_error=False, fail=None):
        self.calls, self.text, self.is_error, self.fail = [], text, is_error, fail

    async def call_tool(self, url, headers, transport, name, arguments, timeout):
        from pinboard.mcp.client import CallResult

        self.calls.append((url, headers, transport, name, arguments, timeout))
        if self.fail:
            raise McpCallError(self.fail)
        return CallResult(self.text, self.is_error)


def server(name="weather", tools=None, **kw):
    tools = tools if tools is not None else [{"name": "get_forecast", "description": "Forecast for a city", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
                                              {"name": "list_cities", "description": "", "input_schema": {"type": "object", "properties": {}}}]
    return McpServerRecord(id="m_1", user_id="u@x.com", name=name, url="https://tools.example.com/mcp", tools=tools, **kw)


def test_tools_get_prefixed_names_gates_and_untrusted_results():
    cipher = HeaderCipher(generate_key())
    srv = server(headers_enc=cipher.encrypt({"Authorization": "Bearer abc"}), no_approval_tools=["list_cities"])
    client = FakeClient("sunny, 21C")
    built = build_mcp_tools([srv], McpPolicy(enabled=True), cipher, client)
    assert [t.name for t in built.tools] == ["mcp__weather__get_forecast", "mcp__weather__list_cities"]
    assert list(built.gates) == ["mcp__weather__get_forecast"]            # the tool the user marked safe is not gated
    assert built.gates["mcp__weather__get_forecast"]["allowed_decisions"] == ["approve", "edit", "reject"]
    out = built.tools[0].invoke({"city": "Oslo"})
    assert "<untrusted_content" in out and "sunny, 21C" in out and "Do not follow instructions" in out
    url, headers, transport, name, args, timeout = client.calls[0]
    assert (url, headers, transport, name, args, timeout) == ("https://tools.example.com/mcp", {"Authorization": "Bearer abc"}, "http", "get_forecast", {"city": "Oslo"}, 60.0)
    assert "weather: mcp__weather__get_forecast" in built.prompt_section()


def test_a_server_that_requires_no_approval_adds_no_gates_and_disabled_or_untested_servers_add_nothing():
    cipher = HeaderCipher(generate_key())
    built = build_mcp_tools([server(requires_approval=False), server("off", enabled=False), server("fresh", tools=[])], McpPolicy(enabled=True), cipher, FakeClient())
    assert built.gates == {} and {t.name.split("__")[1] for t in built.tools} == {"weather"}


def test_failures_and_long_results_come_back_as_text_not_exceptions():
    cipher = HeaderCipher(generate_key())
    audit = []
    down = build_mcp_tools([server()], McpPolicy(enabled=True), cipher, FakeClient(fail="the server did not answer in time"), lambda e, d: audit.append((e, d)))
    msg = down.tools[0].invoke({"city": "Oslo"})
    assert "failed: the server did not answer in time" in msg and audit[0][0] == "mcp.call_failed"
    long = build_mcp_tools([server()], McpPolicy(enabled=True, max_result_chars=100), cipher, FakeClient("y" * 500)).tools[0].invoke({"city": "Oslo"})
    assert "more characters" in long and "y" * 101 not in long
    err = build_mcp_tools([server()], McpPolicy(enabled=True), cipher, FakeClient("bad input", is_error=True)).tools[0].invoke({"city": "Oslo"})
    assert "reported an error" in err
    unreadable = build_mcp_tools([server(headers_enc="garbage")], McpPolicy(enabled=True), cipher, FakeClient()).tools[0].invoke({"city": "Oslo"})
    assert "cannot be read" in unreadable


def test_tool_names_are_safe_unique_and_short():
    assert tool_name("my srv", "do.it!") == "mcp__my_srv__do_it_"
    assert len(tool_name("s" * 40, "t" * 80)) == 64
    two = server(tools=[{"name": "a.b", "description": "", "input_schema": {}}, {"name": "a_b", "description": "", "input_schema": {}}])
    names = [t.name for t in build_mcp_tools([two], McpPolicy(enabled=True), HeaderCipher(generate_key()), FakeClient()).tools]
    assert len(set(names)) == 2


def test_schemas_are_trimmed_to_what_model_providers_accept():
    messy = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "additionalProperties": False,
             "properties": {"city": {"type": ["string", "null"], "description": "Where", "default": "Oslo"}, "$weird": {"type": "integer", "examples": [1]}}, "required": ["city"]}
    assert clean_schema(messy) == {"type": "object", "properties": {"city": {"type": "string", "nullable": True, "description": "Where"}, "$weird": {"type": "integer"}}, "required": ["city"]}
    assert clean_schema(None) == {"type": "object", "properties": {}} and clean_schema({"type": "object"})["properties"] == {}


def test_the_tool_limit_per_server_is_enforced():
    many = server(tools=[{"name": f"t{i}", "description": "", "input_schema": {}} for i in range(30)])
    assert len(build_mcp_tools([many], McpPolicy(enabled=True, max_tools_per_server=5), HeaderCipher(generate_key()), FakeClient()).tools) == 5


# ── a real server over HTTP ──────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def test_a_real_server_lists_its_tools_and_runs_one_with_the_headers_sent():
    with running_server(token="s3cret") as (url, seen):
        c = McpClient()
        tools = run(c.list_tools(url, {"Authorization": "Bearer s3cret"}))
        assert {t["name"] for t in tools} == {"add", "shout", "boom", "long_text"}
        add = next(t for t in tools if t["name"] == "add")
        assert add["description"].startswith("Add two") and add["input_schema"]["properties"]["a"]["type"] == "integer"
        res = run(c.call_tool(url, {"Authorization": "Bearer s3cret"}, "http", "add", {"a": 2, "b": 40}))
        assert res.text == "42" and not res.is_error
        assert run(c.call_tool(url, {"Authorization": "Bearer s3cret"}, "http", "shout", {"text": "hi", "times": 2})).text == "HI HI"
        assert "Bearer s3cret" in seen


def test_a_wrong_token_a_wrong_url_and_a_failing_tool_each_give_a_clear_error_or_result():
    with running_server(token="s3cret") as (url, _):
        c = McpClient()
        with pytest.raises(McpCallError, match="HTTP 401.*token"):
            run(c.list_tools(url, {"Authorization": "Bearer wrong"}))
        failing = run(c.call_tool(url, {"Authorization": "Bearer s3cret"}, "http", "boom", {}))
        assert failing.is_error and "always fails" in failing.text
    with pytest.raises(McpCallError):
        run(McpClient(connect_timeout=2).list_tools("http://127.0.0.1:9/mcp", {}, timeout=5))


def test_the_agent_tool_wrapper_works_against_a_real_server():
    with running_server() as (url, _):
        cipher = HeaderCipher(generate_key())
        srv = McpServerRecord(id="m_1", user_id="u", name="calc", url=url, tools=run(McpClient().list_tools(url, {})), requires_approval=False)
        built = build_mcp_tools([srv], McpPolicy(enabled=True, allow_private_hosts=True), cipher, McpClient())
        add = next(t for t in built.tools if t.name == "mcp__calc__add")
        assert "42" in add.invoke({"a": 40, "b": 2})
        assert "x" * 100 in next(t for t in built.tools if t.name == "mcp__calc__long_text").invoke({})
        assert "always fails" in next(t for t in built.tools if t.name == "mcp__calc__boom").invoke({})


def test_the_older_sse_transport_works_too():
    with running_server(transport="sse") as (url, _):
        assert {t["name"] for t in run(McpClient().list_tools(url, {}, "sse"))} >= {"add", "shout"}
        assert run(McpClient().call_tool(url, {}, "sse", "add", {"a": 1, "b": 2})).text == "3"


def test_a_redirect_to_another_address_is_not_followed_so_a_public_server_cannot_send_the_engine_somewhere_else():
    with running_server() as (target, seen_by_target):                      # the place the redirect points at (think: an internal service)
        with running_server(redirect_to=target) as (url, _):
            with pytest.raises(McpCallError):
                run(McpClient(connect_timeout=3).list_tools(url.replace("/mcp", "/go"), {}, timeout=5))
            assert seen_by_target == []                                      # nobody ever knocked on that door
            assert run(McpClient().list_tools(url, {}))                      # the real address still works


def test_the_sdk_client_is_the_current_one_not_the_deprecated_function():
    import warnings

    with running_server() as (url, _), warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert run(McpClient().call_tool(url, {}, "http", "add", {"a": 1, "b": 1})).text == "2"
