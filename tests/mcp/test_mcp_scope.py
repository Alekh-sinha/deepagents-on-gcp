"""Where a user may point an MCP server (policy mcp.scope = vpc_or_allowlist): inside the reserved VPC range = assumed approved (http or https);
on the approved list = allowed (public, https only); anything else is refused with a message that says why. The address is looked up on every check."""
import copy

import pytest

from pinboard.core.policy import McpPolicy, PolicyError, parse_policy
from pinboard.mcp import HeaderCipher, McpUrlError, build_mcp_tools, check_url, generate_key
from pinboard.state import McpServerRecord

from test_policy import BASE

RANGE = "192.168.40.0/24"
SCOPED = McpPolicy(enabled=True, scope="vpc_or_allowlist", vpc_ranges=(RANGE,), allowed_hosts=("mcp.vendor.example",))


def resolver(**table):
    """A fake DNS: host -> address(es). Anything not listed does not resolve."""
    def resolve(host, port):
        if host not in table:
            raise OSError("name not known")
        v = table[host]
        return [(2, 1, 6, "", (a, 0)) for a in ([v] if isinstance(v, str) else v)]
    return resolve


DNS = resolver(**{"calc.mcp.internal": "192.168.40.7", "mixed.mcp.internal": ["192.168.40.8", "10.146.76.3"], "db.mcp.internal": "10.146.76.3",
                  "mcp.vendor.example": "93.184.216.34", "other.example.com": "93.184.216.35", "sneaky.vendor": "169.254.169.254",
                  "private.vendor": "mcp.vendor.example"})


# ── inside the VPC range: approved ───────────────────────────────────────

@pytest.mark.parametrize("url", ["http://192.168.40.7:8000/mcp", "https://192.168.40.7/mcp", "http://calc.mcp.internal:8000/mcp"])
def test_a_server_inside_the_reserved_range_is_accepted_over_http_or_https_by_address_or_by_name(url):
    assert check_url(url, SCOPED, DNS).startswith(url.split("/mcp")[0].split("://")[0])


def test_a_name_that_resolves_partly_outside_the_range_is_refused():
    with pytest.raises(McpUrlError, match="cannot be added"):
        check_url("http://mixed.mcp.internal:8000/mcp", SCOPED, DNS)


@pytest.mark.parametrize("url", ["http://10.146.76.3:5432/", "http://192.168.30.10:443/", "http://192.168.10.2/", "http://db.mcp.internal/",
                                 "http://169.254.169.254/computeMetadata/v1/", "http://127.0.0.1:8000/mcp", "http://localhost:8000/mcp"])
def test_other_private_addresses_the_database_the_proxy_the_metadata_server_and_loopback_are_refused(url):
    with pytest.raises(McpUrlError, match="cannot be added"):
        check_url(url, SCOPED, DNS)


# ── the approved list ────────────────────────────────────────────────────

def test_a_listed_public_host_is_accepted_over_https_only():
    assert check_url("https://mcp.vendor.example/mcp", SCOPED, DNS) == "https://mcp.vendor.example/mcp"
    with pytest.raises(McpUrlError, match="only https"):
        check_url("http://mcp.vendor.example/mcp", SCOPED, DNS)


def test_a_listed_host_must_still_resolve_to_a_public_address():
    pol = McpPolicy(enabled=True, scope="vpc_or_allowlist", vpc_ranges=(RANGE,), allowed_hosts=("sneaky.vendor",))
    with pytest.raises(McpUrlError, match="not a public address"):
        check_url("https://sneaky.vendor/mcp", pol, DNS)


@pytest.mark.parametrize("url", ["https://other.example.com/mcp", "http://other.example.com/mcp", "https://nowhere.example.org/mcp"])
def test_any_other_host_is_refused_with_a_message_that_names_the_rule(url):
    with pytest.raises(McpUrlError) as e:
        check_url(url, SCOPED, DNS)
    msg = str(e.value)
    assert "cannot be added" in msg and RANGE in msg and "mcp.vendor.example" in msg and "administrator" in msg


def test_an_empty_list_means_no_public_host_at_all_not_any_host():
    pol = McpPolicy(enabled=True, scope="vpc_or_allowlist", vpc_ranges=(RANGE,))
    with pytest.raises(McpUrlError, match="empty"):
        check_url("https://other.example.com/mcp", pol, DNS)
    assert check_url("http://192.168.40.9:8000/mcp", pol, DNS)


def test_no_range_configured_means_only_the_list_applies():
    pol = McpPolicy(enabled=True, scope="vpc_or_allowlist", allowed_hosts=("mcp.vendor.example",))
    assert check_url("https://mcp.vendor.example/mcp", pol, DNS)
    with pytest.raises(McpUrlError, match="none configured"):
        check_url("http://192.168.40.7/mcp", pol, DNS)


def test_credentials_in_the_url_and_odd_schemes_are_refused_in_this_mode_too():
    for url in ("http://user:pw@192.168.40.7/mcp", "ftp://192.168.40.7/mcp", "192.168.40.7"):
        with pytest.raises(McpUrlError):
            check_url(url, SCOPED, DNS)


def test_the_default_mode_is_unchanged_public_https_only_and_private_hosts_refused():
    default = McpPolicy(enabled=True)
    assert default.scope == "open" and default.vpc_ranges == ()
    assert check_url("https://other.example.com/mcp", default, DNS)
    for url in ("http://192.168.40.7/mcp", "https://192.168.40.7/mcp"):
        with pytest.raises(McpUrlError):
            check_url(url, default, DNS)


# ── every call re-checks the address ─────────────────────────────────────

class Spy:
    def __init__(self):
        self.calls = []

    async def call_tool(self, url, headers, transport, name, args, timeout):
        from pinboard.mcp.client import CallResult
        self.calls.append(url)
        return CallResult("ok", False)


def saved(url):
    return McpServerRecord(id="m_1", user_id="u@x.com", name="calc", url=url, tools=[{"name": "add", "description": "", "input_schema": {"type": "object", "properties": {}}}])


def test_a_saved_server_stops_working_when_its_name_starts_pointing_somewhere_else():
    cipher, spy, audit = HeaderCipher(generate_key()), Spy(), []
    table = {"calc.mcp.internal": "192.168.40.7"}
    check = lambda url: check_url(url, SCOPED, lambda h, p: resolver(**table)(h, p))   # noqa: E731
    tool = build_mcp_tools([saved("http://calc.mcp.internal:8000/mcp")], SCOPED, cipher, spy, lambda e, d: audit.append(e), url_check=check).tools[0]
    assert "ok" in tool.invoke({})
    table["calc.mcp.internal"] = "93.184.216.99"            # re-pointed outside the network after it was approved
    out = tool.invoke({})
    assert "was blocked" in out and "cannot be added" in out and len(spy.calls) == 1 and "mcp.call_blocked" in audit


def test_tightening_the_policy_also_applies_to_servers_that_were_saved_earlier():
    cipher, spy = HeaderCipher(generate_key()), Spy()
    strict = McpPolicy(enabled=True, scope="vpc_or_allowlist", vpc_ranges=(RANGE,))
    tool = build_mcp_tools([saved("https://other.example.com/mcp")], strict, cipher, spy, url_check=lambda url: check_url(url, strict, DNS)).tools[0]
    assert "was blocked" in tool.invoke({}) and spy.calls == []


# ── the policy file ──────────────────────────────────────────────────────

def pol(**mcp):
    d = copy.deepcopy(BASE)
    d["mcp"] = {"enabled": True, **mcp}
    return parse_policy(d).mcp


def test_the_policy_parses_the_scope_and_the_ranges():
    p = pol(scope="vpc_or_allowlist", vpc_ranges=[RANGE], allowed_hosts=["mcp.vendor.example"])
    assert (p.scope, p.vpc_ranges, p.allowed_hosts) == ("vpc_or_allowlist", (RANGE,), ("mcp.vendor.example",))
    assert pol().scope == "open" and pol().vpc_ranges == ()


@pytest.mark.parametrize("mcp,why", [
    ({"scope": "everything"}, "mcp.scope"),
    ({"scope": "vpc_or_allowlist", "vpc_ranges": "192.168.40.0/24"}, "list of CIDR"),
    ({"vpc_ranges": ["not-a-range"]}, "not a valid CIDR"),
    ({"vpc_ranges": ["192.168.40.5/24"]}, "not a valid CIDR"),            # host bits set
    ({"vpc_ranges": ["8.8.8.0/24"]}, "private IPv4"),                      # public space
    ({"vpc_ranges": ["10.0.0.0/8"]}, "no wider than /16"),
    ({"vpc_ranges": ["169.254.0.0/16"]}, "private IPv4|overlaps"),         # the metadata server's range
    ({"vpc_ranges": ["127.0.0.0/24"]}, "private IPv4|overlaps"),
    ({"scope": "vpc_or_allowlist", "allow_private_hosts": True}, "cannot be combined"),
])
def test_a_bad_scope_or_range_stops_the_policy_from_loading(mcp, why):
    with pytest.raises(PolicyError, match=why):
        pol(**mcp)
