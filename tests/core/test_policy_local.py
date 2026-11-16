"""The shipped policy holds placeholders; a developer's own values live in an optional, git-ignored policy.local.yaml that is merged over it.
The app, the deploy tools and the image build must all see the SAME merged policy."""
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from pinboard.core.policy import PolicyError, load_policy, parse_policy, read_policy_data

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "config" / "policy.yaml"


def write(tmp_path, base=None, local=None):
    p = tmp_path / "policy.yaml"
    p.write_text(base if base is not None else SHIPPED.read_text())
    if local is not None:
        (tmp_path / "policy.local.yaml").write_text(local)
    return p


def sibling(p):
    """The local file next to ``p``. Passed explicitly because the whole suite runs with PINBOARD_POLICY_LOCAL=off (see conftest)."""
    return p.with_name("policy.local.yaml")


def test_the_shipped_file_has_no_personal_address_and_a_sibling_local_file_supplies_it(tmp_path):
    assert "@gmail.com" not in SHIPPED.read_text() and "agentmail.to" in SHIPPED.read_text()
    p = write(tmp_path, local="allowed_users: [me@mail.test]\nemail:\n  from_inbox: mine@agentmail.to\n  allowed_recipients: [me@mail.test]\n")
    pol = load_policy(p, local=sibling(p))
    assert pol.allowed_users == ("me@mail.test",) and pol.email.from_inbox == "mine@agentmail.to" and pol.email.allowed_recipients == ("me@mail.test",)
    assert pol.email.max_per_day == 20 and pol.email.max_attachment_mb == 5       # what the local file does not mention comes from the shipped file


def test_mappings_merge_but_lists_and_values_are_replaced_never_appended(tmp_path):
    p = write(tmp_path, local="mcp:\n  allowed_hosts: [mcp.vendor.example]\nlimits:\n  max_turn_minutes: 7\n")
    data = read_policy_data(p, local=sibling(p))
    assert data["mcp"]["allowed_hosts"] == ["mcp.vendor.example"] and data["mcp"]["scope"] == "vpc_or_allowlist"   # merged into the mapping
    assert data["limits"]["max_turn_minutes"] == 7 and data["limits"]["max_concurrent_turns"] > 0
    assert data["allowed_users"] == ["you@example.com"]                                                             # untouched
    other = tmp_path / "second"
    other.mkdir()
    both = write(other, local="allowed_users: [a@x.test]\n")
    assert read_policy_data(both, local=sibling(both))["allowed_users"] == ["a@x.test"]                             # replaced, not ["you@example.com", "a@x.test"]


def test_no_local_file_means_just_the_shipped_policy_and_an_empty_one_changes_nothing(tmp_path):
    p = write(tmp_path)
    assert read_policy_data(p, local=sibling(p)) == yaml.safe_load(SHIPPED.read_text())
    (tmp_path / "policy.local.yaml").write_text("# nothing here yet\n")
    assert read_policy_data(p, local=sibling(p)) == yaml.safe_load(SHIPPED.read_text())


def test_a_broken_local_file_stops_the_app_instead_of_being_ignored(tmp_path):
    for bad in ("allowed_users: [unterminated", "- a\n- list\n", "just text"):
        p = write(tmp_path, local=bad)
        with pytest.raises(PolicyError, match="local policy file"):
            read_policy_data(p, local=sibling(p))


def test_a_local_value_is_still_validated_like_any_other(tmp_path):
    p = write(tmp_path, local="mcp:\n  vpc_ranges: [8.8.8.0/24]\n")
    with pytest.raises(PolicyError, match="private IPv4"):
        load_policy(p, local=sibling(p))


def test_the_environment_can_name_the_local_file_or_switch_it_off(tmp_path, monkeypatch):
    p = write(tmp_path, local="allowed_users: [sibling@x.test]\n")
    other = tmp_path / "elsewhere.yaml"
    other.write_text("allowed_users: [other@x.test]\n")
    monkeypatch.setenv("PINBOARD_POLICY_LOCAL", str(other))
    assert read_policy_data(p)["allowed_users"] == ["other@x.test"]
    monkeypatch.setenv("PINBOARD_POLICY_LOCAL", "off")
    assert read_policy_data(p)["allowed_users"] == ["you@example.com"]
    monkeypatch.delenv("PINBOARD_POLICY_LOCAL")
    assert read_policy_data(p)["allowed_users"] == ["sibling@x.test"]


def test_the_test_suite_itself_never_reads_a_developers_own_local_policy():
    import os

    assert os.environ.get("PINBOARD_POLICY_LOCAL") == "off"
    assert load_policy(SHIPPED).allowed_users == ("you@example.com",)


def test_the_deploy_tools_see_exactly_the_policy_the_app_sees(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("deploy_config_mirror", ROOT / "deploy" / "deploy_config.py")
    dc = importlib.util.module_from_spec(spec)
    sys.modules["deploy_config_mirror"] = dc
    spec.loader.exec_module(dc)
    p = write(tmp_path, local="allowed_users: [Me@Mail.test]\nmcp:\n  allowed_hosts: [mcp.vendor.example]\n  vpc_ranges: [192.168.50.0/24]\n")
    monkeypatch.setenv("PINBOARD_POLICY_LOCAL", "")           # "unset": use the sibling file, as in a real deployment
    assert dc.read_policy_data(p) == read_policy_data(p)
    assert dc._allowed_users(p) == ["me@mail.test"]            # the web app's access list comes from the same merged file
    egress_spec = importlib.util.spec_from_file_location("deploy_egress_mirror", ROOT / "deploy" / "egress.py")
    sys.path.insert(0, str(ROOT / "deploy"))
    eg = importlib.util.module_from_spec(egress_spec)
    egress_spec.loader.exec_module(eg)
    assert eg.policy_mcp_hosts(p) == ["mcp.vendor.example"] and eg.policy_vpc_ranges(p) == ["192.168.50.0/24"]


def test_the_local_file_is_ignored_by_git_and_not_by_the_image_build():
    assert "config/policy.local.yaml" in (ROOT / ".gitignore").read_text()
    for name in (".dockerignore", ".gcloudignore"):
        assert "policy.local" not in (ROOT / name).read_text() and "config" not in [l.strip() for l in (ROOT / name).read_text().splitlines()]
    assert (ROOT / "config" / "policy.local.example.yaml").exists()
    assert parse_policy({**yaml.safe_load(SHIPPED.read_text()), **{}}) is not None
