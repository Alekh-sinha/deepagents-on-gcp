import copy

import pytest
import yaml

from pinboard.core.policy import Policy, PolicyDenied, PolicyError, load_policy, parse_policy

BASE = {
    "allowed_users": ["Me@Example.com"],
    "email": {"from_inbox": "bot@agentmail.to", "allowed_recipients": ["me@example.com"], "max_per_day": 2, "max_attachment_mb": 1},
    "github": {"allowed_repos": [], "branch_prefix": "agent/", "never_default_branch": True},
    "models": {"default": "m1", "allowed": [{"id": "m1", "provider": "google_genai", "label": "One"}, {"id": "m2", "provider": "anthropic"}]},
    "limits": {"max_turn_minutes": 10},
}


def cfg(**over):
    d = copy.deepcopy(BASE)
    for k, v in over.items():
        d[k] = v
    return d


@pytest.fixture
def pol() -> Policy:
    return parse_policy(cfg())


def test_the_shipped_policy_file_is_valid_and_matches_the_decisions():
    p = load_policy("config/policy.yaml")
    assert p.email.allowed_recipients == ("alekh.sinha09@gmail.com",)
    assert p.email.from_inbox == "privai-agent-0909@agentmail.to"
    assert p.default_model == "gemini-2.5-flash" and p.github.branch_prefix == "agent/" and p.limits.max_auto_resumes == 1
    assert p.limits.sandbox_idle_minutes == 20


def test_recipients_are_allowlisted_and_normalised(pol):
    assert pol.check_recipients(["ME@example.com"]) == ["me@example.com"]
    assert pol.check_recipients("Me <me@example.com>", ["me@example.com"]) == ["me@example.com", "me@example.com"]
    for bad in (["evil@x.com"], ["me@example.com", "evil@x.com"], ["me@example.com, evil@x.com"],
                ["evil@x.com <me@example.com>"], ["me@example.com;evil@x.com"], ["me@example.com.evil.com"], [""]):
        with pytest.raises(PolicyDenied) as e:
            pol.check_recipients(bad)
        assert e.value.rule in ("email.recipient_not_allowed", "email.no_recipient"), bad
    with pytest.raises(PolicyDenied):
        pol.check_recipients(None, [], "")
    with pytest.raises(PolicyDenied):  # bcc/cc count too
        pol.check_recipients(["me@example.com"], ["evil@x.com"])


def test_email_limits(pol):
    pol.check_daily_email_quota(1)
    with pytest.raises(PolicyDenied, match="daily"):
        pol.check_daily_email_quota(2)
    pol.check_attachment_size(1024 * 1024)
    with pytest.raises(PolicyDenied, match="larger"):
        pol.check_attachment_size(1024 * 1024 + 1)


def test_branches_must_use_the_prefix_and_never_the_default(pol):
    assert pol.check_branch("agent/report-1", "main") == "agent/report-1"
    for bad, rule in (("main", "github.default_branch"), ("feature/x", "github.branch_prefix"), ("agent/../main", "github.bad_branch"),
                      ("", "github.bad_branch"), ("agent/a b", "github.bad_branch"), ("-agent/x", "github.bad_branch")):
        with pytest.raises(PolicyDenied) as e:
            pol.check_branch(bad, "main")
        assert e.value.rule == rule, bad
    p2 = parse_policy(cfg(github={"branch_prefix": "agent/", "never_default_branch": True}))
    with pytest.raises(PolicyDenied) as e:
        p2.check_branch("agent/", "agent/")  # trailing slash is invalid
    assert e.value.rule == "github.bad_branch"


def test_repo_allowlist_empty_means_installation_scope_and_nonempty_restricts(pol):
    assert pol.check_repo("Alekh-sinha/Google_Translator") == "alekh-sinha/google_translator"
    with pytest.raises(PolicyDenied, match="owner/name"):
        pol.check_repo("../../etc")
    locked = parse_policy(cfg(github={"allowed_repos": ["Alekh-sinha/google_translator"]}))
    assert locked.check_repo("alekh-sinha/google_translator")
    with pytest.raises(PolicyDenied, match="not in the allowed"):
        locked.check_repo("someone/else")


def test_model_resolution_and_listing(pol):
    assert pol.resolve_model(None).id == "m1" and pol.resolve_model("  ").id == "m1" and pol.resolve_model("m2").provider == "anthropic"
    with pytest.raises(PolicyDenied) as e:
        pol.resolve_model("gpt-9")
    assert e.value.rule == "model.not_allowed" and "m1, m2" in str(e.value)
    assert pol.list_models() == {"default": "m1", "helper_model_choice": False, "models": [
        {"id": "m1", "provider": "google_genai", "label": "One", "rank": 1, "use_for": "", "location": ""},
        {"id": "m2", "provider": "anthropic", "label": "m2", "rank": 2, "use_for": "", "location": ""}]}


def test_users(pol):
    pol.check_user(" ME@example.com ")
    for bad in (None, "", "other@example.com"):
        with pytest.raises(PolicyDenied):
            pol.check_user(bad)


@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d.pop("allowed_users"), "allowed_users"),
    (lambda d: d.update(allowed_users=["not-an-email"]), "invalid email"),
    (lambda d: d["email"].pop("allowed_recipients"), "allowed_recipients"),
    (lambda d: d["email"].update(allowed_recipients=[]), "non-empty"),
    (lambda d: d["email"].update(from_inbox="nope"), "from_inbox"),
    (lambda d: d["email"].update(max_per_day=-1), "max_per_day"),
    (lambda d: d["email"].update(max_per_day=True), "max_per_day"),
    (lambda d: d["github"].update(allowed_repos=["bad"]), "owner/name"),
    (lambda d: d["github"].update(branch_prefix="agent"), "branch_prefix"),
    (lambda d: d["models"].update(default="zzz"), "not in models.allowed"),
    (lambda d: d["models"]["allowed"].append({"id": "m1", "provider": "openai"}), "duplicate"),
    (lambda d: d["models"]["allowed"].append({"id": "m3", "provider": "bing"}), "unknown provider"),
    (lambda d: d["models"]["allowed"].append({"provider": "openai"}), "id and provider"),
    (lambda d: d["limits"].update(max_turn_minutes=0), "max_turn_minutes"),
    (lambda d: d["limits"].update(sandbox_idle_minutes=0), "sandbox_idle_minutes"),
])
def test_invalid_policies_fail_closed(mutate, msg):
    d = copy.deepcopy(BASE)
    mutate(d)
    with pytest.raises(PolicyError, match=msg):
        parse_policy(d)


def test_loading_errors_and_env_path(tmp_path, monkeypatch):
    with pytest.raises(PolicyError, match="not found"):
        load_policy(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unclosed")
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_policy(bad)
    (tmp_path / "list.yaml").write_text("- 1\n- 2\n")
    with pytest.raises(PolicyError, match="mapping"):
        load_policy(tmp_path / "list.yaml")
    good = tmp_path / "p.yaml"
    good.write_text(yaml.safe_dump(BASE))
    monkeypatch.setenv("PINBOARD_POLICY", str(good))
    assert load_policy().email.max_per_day == 2
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PINBOARD_POLICY")
    with pytest.raises(PolicyError):  # no ./config/policy.yaml here: refuses to start with a default-open policy
        load_policy()


# ── ranks, notes, locations and the helper-model switch ──────────────────

def ranked():
    d = cfg()
    d["models"] = {"default": "b", "helper_model_choice": "on", "allowed": [
        {"id": "a", "provider": "google_genai", "rank": 1, "use_for": "simple lookups", "location": "us-central1"},
        {"id": "b", "provider": "google_genai", "rank": 2},
        {"id": "c", "provider": "google_genai", "rank": 3, "location": "global"},
        {"id": "d", "provider": "google_genai", "rank": 3},  # ties are fine: same price tier
        {"id": "e", "provider": "google_genai", "rank": 5}]}
    return parse_policy(d)


def test_a_helper_may_use_the_selected_model_and_every_cheaper_one():
    p = ranked()
    assert [m.id for m in p.allowed_for("a")] == ["a"]
    assert [m.id for m in p.allowed_for("b")] == ["a", "b"]
    assert [m.id for m in p.allowed_for("c")] == ["a", "b", "c", "d"]  # a tie is allowed
    assert [m.id for m in p.allowed_for(None)] == ["a", "b"]           # no selection = the default's rank
    assert [m.id for m in p.allowed_for("e")] == ["a", "b", "c", "d", "e"]
    with pytest.raises(PolicyDenied):
        p.allowed_for("zzz")


def test_ranks_notes_and_locations_are_read_and_listed():
    p = ranked()
    assert p.helper_model_choice is True
    a = p.resolve_model("a")
    assert (a.rank, a.use_for, a.location) == (1, "simple lookups", "us-central1") and p.resolve_model("c").location == "global"
    listed = {m["id"]: m for m in p.list_models()["models"]}
    assert listed["a"]["rank"] == 1 and listed["a"]["use_for"] == "simple lookups" and p.list_models()["helper_model_choice"] is True


def test_without_a_rank_the_position_in_the_list_is_the_rank_and_the_switch_defaults_to_off():
    p = parse_policy(cfg())
    assert [m.rank for m in p.models] == [1, 2] and p.helper_model_choice is False


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["models"]["allowed"][0].update(rank=0), "rank"),
    (lambda d: d["models"]["allowed"][0].update(rank="high"), "rank"),
    (lambda d: d["models"]["allowed"][0].update(rank=True), "rank"),
    (lambda d: d["models"]["allowed"][0].update(location="Mars Base 1"), "location"),
    (lambda d: d["models"].update(helper_model_choice="maybe"), "helper_model_choice"),
    (lambda d: d["models"].update(helper_model_choice=2), "helper_model_choice"),
])
def test_bad_rank_location_or_switch_is_refused(mutate, expect):
    d = copy.deepcopy(BASE)
    mutate(d)
    with pytest.raises(PolicyError, match=expect):
        parse_policy(d)


# ── MCP and schedule blocks ──────────────────────────────────────────────

def test_mcp_and_schedules_are_off_unless_the_policy_says_otherwise():
    p = parse_policy(cfg())
    assert p.mcp.enabled is False and p.mcp.allowed_hosts == () and p.mcp.allow_private_hosts is False and p.schedules.enabled is False
    d = cfg()
    d["mcp"] = {"enabled": True, "allowed_hosts": ["Tools.Example.com", "tools.example.com", "api.example.org"], "max_servers_per_user": 3, "tool_timeout_seconds": 30}
    d["schedules"] = {"enabled": True, "min_interval_minutes": 60, "max_runs_per_day": 5}
    p = parse_policy(d)
    assert p.mcp.enabled and p.mcp.allowed_hosts == ("tools.example.com", "api.example.org") and p.mcp.max_servers_per_user == 3 and p.mcp.tool_timeout_seconds == 30
    assert p.schedules.enabled and p.schedules.min_interval_minutes == 60 and p.schedules.max_runs_per_day == 5 and p.schedules.max_per_user == 20


@pytest.mark.parametrize("block,value,expect", [
    ("mcp", "yes", "mcp must be a mapping"), ("mcp", {"enabled": "yes"}, "mcp.enabled"), ("mcp", {"allowed_hosts": "x.com"}, "allowed_hosts"),
    ("mcp", {"allowed_hosts": ["https://x.com"]}, "allowed_hosts"), ("mcp", {"allowed_hosts": ["x.com:8080"]}, "allowed_hosts"),
    ("mcp", {"max_servers_per_user": 0}, "max_servers_per_user"), ("mcp", {"tool_timeout_seconds": True}, "tool_timeout_seconds"),
    ("mcp", {"allow_private_hosts": 1}, "allow_private_hosts"),
    ("schedules", [], "schedules must be a mapping"), ("schedules", {"enabled": "on"}, "schedules.enabled"), ("schedules", {"min_interval_minutes": 0}, "min_interval_minutes"),
])
def test_bad_mcp_or_schedule_settings_are_refused(block, value, expect):
    d = cfg()
    d[block] = value
    with pytest.raises(PolicyError, match=expect):
        parse_policy(d)


def test_every_limit_lands_in_its_own_field_both_by_default_and_when_set():
    """Regression: two integer limits were once swapped by a positional mix-up and nothing noticed. Distinct values make that impossible to miss."""
    d = copy.deepcopy(BASE)
    d["limits"] = {"max_turn_minutes": 11, "max_concurrent_turns": 12, "max_auto_resumes": 13, "sandbox_idle_minutes": 14, "max_helper_model_calls": 15, "max_auto_approvals_per_turn": 16}
    lim = parse_policy(d).limits
    assert (lim.max_turn_minutes, lim.max_concurrent_turns, lim.max_auto_resumes, lim.sandbox_idle_minutes, lim.max_helper_model_calls, lim.max_auto_approvals_per_turn) == (11, 12, 13, 14, 15, 16)
    defaults = parse_policy(copy.deepcopy({**BASE, "limits": {}})).limits
    assert (defaults.max_helper_model_calls, defaults.max_auto_approvals_per_turn) == (40, 25)


def test_the_shipped_policy_has_the_limits_it_says():
    p = load_policy("config/policy.yaml")
    assert (p.limits.max_helper_model_calls, p.limits.max_auto_approvals_per_turn) == (40, 25)
