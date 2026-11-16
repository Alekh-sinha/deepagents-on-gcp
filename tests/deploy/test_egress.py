"""Private networking and egress control (docs/EGRESS.md): everything is OFF by default, and when it is on the runtime gets exactly one
network attachment, one proxy and a short list of hosts. Nothing here touches a cloud."""
import copy
import importlib.util
import sys
from pathlib import Path

import pytest

from test_deploy_tools import ARGS, CFG, NOWHERE, deploy_config, runtime  # noqa: F401  (they load the deploy modules once)

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("deploy_egress", ROOT / "deploy" / "egress.py")
egress = importlib.util.module_from_spec(spec)
sys.modules["deploy_egress"] = egress
spec.loader.exec_module(egress)

ATT = "projects/p/regions/us-central1/networkAttachments/my-na"
VALID_ATT = "projects/my-proj/regions/us-central1/networkAttachments/demo-na"   # a project id that passes validation (load() checks the attachment string)
PROXY = "http://192.168.30.10:443"
DOT = {"SANDBOX_RUNTIME": "r"}


def with_cfg(**kv):
    return {**copy.deepcopy(CFG), **kv}


def load(**local_env):
    return deploy_config.load(env={"PB_NAME": "pinboard", **local_env}, local=NOWHERE, project_lookup=lambda: "test-proj", policy_file=NOWHERE)


# ── off by default ───────────────────────────────────────────────────────

def test_by_default_there_is_no_network_attachment_no_proxy_and_no_private_sql():
    c = runtime.runtime_config(**ARGS, dotenv=DOT)
    assert "psc_interface_config" not in c
    assert not {"HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "SQL_PRIVATE_IP"} & set(c["env_vars"])
    assert CFG["network_attachment"] == "" and CFG["egress_proxy"] == "" and CFG["sql_private_ip"] is False and CFG["egress_allowed_hosts"] == []


# ── turned on ────────────────────────────────────────────────────────────

def test_with_an_attachment_and_a_proxy_the_runtime_joins_the_vpc_and_every_client_is_told_about_the_proxy():
    c = runtime.runtime_config(**{**ARGS, "cfg": with_cfg(network_attachment=ATT, egress_proxy=PROXY, sql_private_ip=True)}, dotenv=DOT)
    assert c["psc_interface_config"] == {"network_attachment": ATT}
    env = c["env_vars"]
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == env["https_proxy"] == PROXY
    assert env["SQL_PRIVATE_IP"] == "true"
    no_proxy = env["NO_PROXY"].split(",")
    # Google APIs, the metadata server, this machine and every private range stay OFF the proxy
    assert {".googleapis.com", "metadata.google.internal", "127.0.0.1", "10.0.0.0/8", "192.168.0.0/16"} <= set(no_proxy)
    assert "api.tavily.com" not in no_proxy


def test_an_attachment_alone_changes_only_the_network_not_the_environment():
    c = runtime.runtime_config(**{**ARGS, "cfg": with_cfg(network_attachment=ATT)}, dotenv=DOT)
    assert c["psc_interface_config"] == {"network_attachment": ATT} and "HTTPS_PROXY" not in c["env_vars"]


def test_an_explicit_environment_setting_still_wins_over_the_proxy_defaults():
    c = runtime.runtime_config(**{**ARGS, "cfg": with_cfg(network_attachment=ATT, egress_proxy=PROXY)}, dotenv=DOT, extra_env={"NO_PROXY": "only.this"})
    assert c["env_vars"]["NO_PROXY"] == "only.this"


# ── refusals ─────────────────────────────────────────────────────────────

def test_a_proxy_without_a_network_attachment_is_refused_because_the_engine_could_not_reach_it():
    with pytest.raises(SystemExit, match="network attachment"):
        runtime.runtime_config(**{**ARGS, "cfg": with_cfg(egress_proxy=PROXY)}, dotenv=DOT)
    with pytest.raises(deploy_config.ConfigError, match="needs network_attachment"):
        deploy_config.load(env={"PB_NAME": "pinboard"}, local=_local({"egress_proxy": PROXY}), project_lookup=lambda: "p", policy_file=NOWHERE)


def test_a_vpc_attachment_and_an_agent_gateway_cannot_be_combined_the_api_refuses_it():
    with pytest.raises(SystemExit, match="both a PSC network attachment and an Agent Gateway"):
        runtime.runtime_config(**{**ARGS, "cfg": with_cfg(network_attachment=ATT)}, dotenv=DOT, agent_gateway="projects/p/locations/l/agentGateways/g")


@pytest.mark.parametrize("key,bad", [
    ("network_attachment", "my-na"), ("network_attachment", "projects/p/regions/r/networks/x"),
    ("egress_proxy", "https://1.2.3.4:443"), ("egress_proxy", "1.2.3.4"), ("egress_proxy", "http://1.2.3.4:443/path"),
    ("egress_allowed_hosts", ["API.example.com"]), ("egress_allowed_hosts", ["a.com' || true || '"]), ("egress_allowed_hosts", ["*.example.com"]),
    ("egress_allowed_hosts", "api.example.com"), ("egress_allowed_hosts", ["localhost"]), ("sql_private_ip", "yes"),
    # a trailing newline must not slip through `$` (these values end up inside a proxy rule)
    ("egress_allowed_hosts", ["api.example.com\n"]), ("egress_proxy", "http://1.2.3.4:443\n"),
    ("network_attachment", "projects/my-proj/regions/us-central1/networkAttachments/na\n"),
])
def test_bad_values_are_refused_at_load_time(key, bad):
    """Each case must fail on ITS OWN key (match=key): an unrelated error, such as a bad project id, must not make the test pass."""
    extra = {"network_attachment": VALID_ATT} if key == "egress_proxy" else {}
    with pytest.raises(deploy_config.ConfigError, match=key):
        deploy_config.load(env={"PB_NAME": "pinboard"}, local=_local({**extra, key: bad}), project_lookup=lambda: "my-proj", policy_file=NOWHERE)


def _local(data, _n=[0]):
    import tempfile
    import yaml

    d = Path(tempfile.mkdtemp())
    f = d / "local.yaml"
    f.write_text(yaml.safe_dump(data))
    return f


# ── the allowlist ────────────────────────────────────────────────────────

def policy_with(tmp_path, hosts):
    import yaml

    f = tmp_path / "p.yaml"
    f.write_text(yaml.safe_dump({"mcp": {"allowed_hosts": hosts}}))
    return f


def test_the_allowlist_is_the_connectors_plus_the_mcp_hosts_plus_the_configured_extras_without_duplicates(tmp_path):
    hosts = egress.allowed_hosts(with_cfg(egress_allowed_hosts=["api.example.com", "api.tavily.com"]), policy_with(tmp_path, ["mcp.notion.com", "Mcp.Linear.app"]))
    assert hosts == ["api.tavily.com", "api.github.com", "api.agentmail.to", "mcp.notion.com", "mcp.linear.app", "api.example.com"]


def test_google_apis_are_never_in_the_allowlist(tmp_path):
    assert not [h for h in egress.allowed_hosts(with_cfg(), policy_with(tmp_path, [])) if h.endswith("googleapis.com")]


def test_a_policy_host_that_could_break_the_proxy_rule_is_refused(tmp_path):
    with pytest.raises(deploy_config.ConfigError, match="not a valid host"):
        egress.allowed_hosts(with_cfg(), policy_with(tmp_path, ["x.com' || host() != 'y"]))


def test_the_proxy_rule_matches_each_host_exactly_and_an_empty_list_is_refused():
    assert egress.session_matcher(["a.com", "b.org"]) == "host() == 'a.com' || host() == 'b.org'"
    with pytest.raises(deploy_config.ConfigError):
        egress.session_matcher([])


def test_an_unreadable_policy_gives_just_the_builtin_hosts(tmp_path):
    assert egress.allowed_hosts(with_cfg(), tmp_path / "missing.yaml") == list(egress.BUILTIN_HOSTS)


# ── deploy/network.sh (dry run: prints what it would do, changes nothing) ─

import os
import subprocess


def net(*args, **env):
    e = {**os.environ, "DRY_RUN": "1", "PB_PROJECT": "my-proj", "PB_NAME": "demo", "PB_LOCAL_CONFIG": str(NOWHERE), **env}
    return subprocess.run(["bash", str(ROOT / "deploy" / "network.sh"), *args], capture_output=True, text=True, env=e, cwd=ROOT, timeout=60)


def test_network_up_dry_run_builds_the_attachment_the_proxy_and_a_nat_for_the_proxy_subnet_only():
    r = net("up")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "network-attachments create demo-na" in out and "gateways import demo-egress-gw" in out
    assert "--nat-custom-subnet-ip-ranges=demo-proxy-subnet" in out   # NOT the engine's subnet: otherwise it could bypass the proxy
    assert "--nat-all-subnet-ip-ranges" not in out
    for host in egress.BUILTIN_HOSTS:
        assert host in out
    assert "network_attachment: projects/my-proj/regions/us-central1/networkAttachments/demo-na" in out and "egress_proxy: http://192.168.30.10:443" in out


def test_network_up_never_deletes_anything_and_never_touches_the_database_itself():
    out = net("up").stdout
    run_lines = [line for line in out.splitlines() if line.startswith("  $ ")]
    assert run_lines and not [line for line in run_lines if " delete " in line or "sql instances" in line]
    assert "sql instances patch demo-pg" in out  # only PRINTED as the next step for the person to run


def test_network_hosts_and_bad_commands():
    assert net("hosts").stdout.split() == list(egress.BUILTIN_HOSTS)
    assert net("nonsense").returncode == 2


def test_deploy_passes_recreate_through_to_the_runtime_tool():
    r = subprocess.run(["bash", str(ROOT / "deploy" / "deploy.sh"), "deploy", "--recreate"], capture_output=True, text=True, cwd=ROOT, timeout=60,
                       env={**os.environ, "DRY_RUN": "1", "PB_PROJECT": "my-proj", "PB_NAME": "pinboard", "PB_LOCAL_CONFIG": str(NOWHERE)})
    assert r.returncode == 0 and r.stdout.rstrip().splitlines()[-1].endswith("--recreate")


def test_terraform_uses_the_same_no_proxy_list_and_the_same_opt_in_settings_as_the_python_tool():
    tf = (ROOT / "terraform" / "config.tf").read_text()
    import re

    assert re.search(r'no_proxy_base = "([^"]+)"', tf).group(1) == ",".join(egress.NO_PROXY)
    assert 'trimsuffix(d, ".")' in tf   # the private DNS domains are added the same way egress.py adds them
    main = (ROOT / "terraform" / "main.tf").read_text()
    assert 'dynamic "psc_interface_config"' in main and 'local.network_attachment == "" ? []' in main   # off unless configured
    assert "local.proxy_env" in main and "local.sql_private_env" in main


def test_re_pointing_the_scheduler_uses_the_update_flag_for_headers_not_the_create_flag():
    """`gcloud scheduler jobs update http` rejects --headers (it wants --update-headers): found when the job had to follow a new runtime."""
    script = (ROOT / "deploy" / "deploy.sh").read_text()
    assert 'headers=--update-headers' in script and '"$headers=Content-Type=application/json"' in script
    assert '--headers="Content-Type' not in script


# ── MCP servers inside the VPC: subnet, firewall, private DNS ────────────

def test_dns_peering_is_added_only_when_domains_are_configured_and_points_at_the_deployments_own_network():
    off = runtime.runtime_config(**{**ARGS, "cfg": with_cfg(network_attachment=ATT)}, dotenv=DOT)
    assert "dns_peering_configs" not in off["psc_interface_config"]
    on = runtime.runtime_config(**{**ARGS, "cfg": with_cfg(network_attachment=ATT, network_dns_domains=["mcp.internal."], network="pinboard-net")}, dotenv=DOT)
    assert on["psc_interface_config"]["dns_peering_configs"] == [{"domain": "mcp.internal.", "target_project": "p", "target_network": "pinboard-net"}]


@pytest.mark.parametrize("bad", ["mcp.internal", "MCP.internal.", "internal.", ".", "a b.internal.", "x.internal.\n"])
def test_a_dns_domain_must_be_a_lower_case_name_ending_in_a_dot(bad):
    with pytest.raises(deploy_config.ConfigError, match="network_dns_domains"):
        deploy_config.load(env={"PB_NAME": "pinboard"}, local=_local({"network_attachment": VALID_ATT, "network_dns_domains": [bad]}), project_lookup=lambda: "my-proj", policy_file=NOWHERE)


def test_dns_domains_without_a_network_attachment_are_refused():
    with pytest.raises(deploy_config.ConfigError, match="needs network_attachment"):
        deploy_config.load(env={"PB_NAME": "pinboard"}, local=_local({"network_dns_domains": ["mcp.internal."]}), project_lookup=lambda: "my-proj", policy_file=NOWHERE)


def net_with(local_yaml, policy_file=None, name="demo"):
    env = {**os.environ, "DRY_RUN": "1", "PB_PROJECT": "my-proj", "PB_NAME": name, "PB_LOCAL_CONFIG": str(local_yaml)}
    if policy_file:
        env["PINBOARD_POLICY"] = str(policy_file)
    return subprocess.run(["bash", str(ROOT / "deploy" / "network.sh"), "up"], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)


def test_network_up_builds_the_mcp_subnet_from_the_policys_own_range_opens_it_to_the_engine_only_and_adds_the_dns_zone():
    r = net_with(_local({"network_attachment": VALID_ATT, "network_dns_domains": ["mcp.internal."]}), ROOT / "config" / "policy.yaml")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "subnets create demo-mcp-subnet --network demo-net --range=192.168.40.0/24" in out
    assert "firewall-rules create demo-allow-agent-to-mcp" in out and "--source-ranges=192.168.10.0/28" in out and "--target-tags=demo-mcp" in out
    assert "managed-zones create demo-zone-mcp-internal --dns-name=mcp.internal. --visibility=private --networks=demo-net" in out
    assert out.count("roles/dns.peer") == 2 and "gcp-sa-aiplatform.iam.gserviceaccount.com" in out and "gcp-sa-aiplatform-re.iam.gserviceaccount.com" in out
    assert "--nat-custom-subnet-ip-ranges=demo-proxy-subnet" in out and "demo-mcp-subnet" not in out.split("routers nats create")[1].split("\n")[0]   # the MCP servers get no way out


def test_without_dns_domains_or_a_range_nothing_extra_is_created(tmp_path):
    import yaml

    pol = tmp_path / "p.yaml"
    pol.write_text(yaml.safe_dump({"mcp": {"enabled": True}}))
    out = net_with(NOWHERE, pol).stdout
    assert "mcp-subnet" not in out and "managed-zones" not in out and "dns.peer" not in out and "mcp.vpc_ranges is empty" in out


def test_the_mcp_range_in_the_shipped_policy_does_not_overlap_any_other_range_the_network_script_builds():
    import ipaddress
    import re

    script = (ROOT / "deploy" / "network.sh").read_text()
    others = [ipaddress.ip_network(m) for m in re.findall(r'^(?:AGENT|PROXY|GW)_RANGE="([^"]+)"', script, re.M)]
    assert len(others) == 3
    mcp = [ipaddress.ip_network(r) for r in egress.policy_vpc_ranges(ROOT / "config" / "policy.yaml")]
    assert mcp and not [(a, b) for a in mcp for b in others if a.overlaps(b)]


def test_the_shipped_policy_uses_the_vpc_or_allowlist_rule_with_no_public_host_approved_yet():
    from pinboard.core.policy import load_policy

    p = load_policy(ROOT / "config" / "policy.yaml").mcp
    assert p.scope == "vpc_or_allowlist" and p.vpc_ranges == ("192.168.40.0/24",) and p.allowed_hosts == () and not p.allow_private_hosts


def test_the_private_dns_domains_are_kept_off_the_proxy_bare_and_as_a_suffix():
    env = egress.proxy_env(with_cfg(egress_proxy=PROXY, network_dns_domains=["mcp.internal.", "tools.corp."]))
    parts = env["NO_PROXY"].split(",")
    assert {"mcp.internal", ".mcp.internal", "tools.corp", ".tools.corp"} <= set(parts) and env["no_proxy"] == env["NO_PROXY"]
    assert "mcp.internal" not in egress.proxy_env(with_cfg(egress_proxy=PROXY))["NO_PROXY"]


def test_terraform_takes_the_web_users_from_the_local_policy_so_the_shipped_placeholder_is_never_granted_access():
    """The shipped policy holds `you@example.com`; your own address is in policy.local.yaml. If Terraform read only the shipped file,
    IAP access would go to the placeholder."""
    web = (ROOT / "terraform" / "web.tf").read_text()
    assert "config/policy.local.yaml" in web and 'fileexists("${path.module}/../config/policy.local.yaml")' in web
    assert web.index("policy_local.allowed_users") < web.index("policy.allowed_users")   # the local list wins, the shipped one is the fallback
