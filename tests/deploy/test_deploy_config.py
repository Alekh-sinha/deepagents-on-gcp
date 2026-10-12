"""deploy/config.yaml: one file decides every resource name. The same file feeds deploy.sh, runtime.py and Terraform, so a wrong name here
would be wrong everywhere; hence the checks on the names, the layering, and what a hostile or sloppy value can do."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("deploy_config", ROOT / "deploy" / "deploy_config.py")
dc = importlib.util.module_from_spec(spec)
sys.modules["deploy_config"] = dc
spec.loader.exec_module(dc)

NOWHERE = ROOT / "deploy" / "no-such-local-file.yaml"


def load(env=None, local=NOWHERE, project="test-proj"):
    return dc.load(env=env or {}, local=local, project_lookup=lambda: project, policy_file=NOWHERE)  # never the real policy file


# ── the shipped defaults ─────────────────────────────────────────────────

def test_the_shipped_default_name_is_agent_and_every_resource_is_derived_from_it():
    c = load()
    assert c["name"] == "agent" and c["project"] == "test-proj" and c["region"] == "us-central1"
    assert (c["repo"], c["sql_instance"], c["job"], c["runtime_sa_id"], c["scheduler_sa_id"]) == ("agent-repo", "agent-pg", "agent-housekeeping", "agent-runtime", "agent-scheduler")
    assert (c["db_url_secret"], c["api_token_secret"], c["sql_database"], c["sql_user"]) == ("agent-database-url", "agent-api-token", "agent", "agent")
    assert c["image"] == "us-central1-docker.pkg.dev/test-proj/agent-repo/agent:latest"
    assert c["runtime_sa"] == "agent-runtime@test-proj.iam.gserviceaccount.com" and c["artifacts_bucket"] == "test-proj-agent-files"


def test_any_name_works_and_nothing_of_the_old_name_is_left():
    c = load({"PB_NAME": "weather-bot"})
    values = " ".join(str(v) for v in c.values())
    assert "weather-bot-repo" in values and "weather-bot-pg" in values and "weather-bot-database-url" in values
    assert "pinboard" not in values and "agent-" not in values.replace("test-proj-agent", "")


# ── layering: defaults < local file < environment ────────────────────────

def test_the_local_file_overrides_the_defaults_and_the_environment_overrides_both(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("name: mine\nregion: europe-west1\nsql_tier: db-g1-small\nmin_instances: 0\n")
    c = load(local=local)
    assert (c["name"], c["region"], c["sql_tier"], c["min_instances"], c["max_instances"]) == ("mine", "europe-west1", "db-g1-small", 0, 2)
    c = load({"PB_NAME": "env-wins", "PB_REGION": "us-east1"}, local=local)
    assert (c["name"], c["region"]) == ("env-wins", "us-east1")
    assert load({"PB_LOCAL_CONFIG": str(local)})["name"] == "mine"  # pointing at a different local file by variable


def test_the_project_comes_from_the_file_then_the_environment_then_gcloud():
    assert dc.load(env={}, local=NOWHERE, project_lookup=lambda: "from-gcloud")["project"] == "from-gcloud"
    assert dc.load(env={"PB_PROJECT": "from-env-1"}, local=NOWHERE, project_lookup=lambda: "from-gcloud")["project"] == "from-env-1"


# ── bad values stop everything early, with a reason ──────────────────────

@pytest.mark.parametrize("env, message", [
    ({"PB_NAME": "Agent"}, "name"), ({"PB_NAME": "a"}, "name"), ({"PB_NAME": "x" * 21}, "name"), ({"PB_NAME": "1agent"}, "name"),
    ({"PB_NAME": "agent-"}, "name"), ({"PB_NAME": "my_agent"}, "name"), ({"PB_NAME": "a b"}, "name"), ({"PB_NAME": "a;rm"}, "name"),
    ({"PB_REGION": "mars"}, "region"), ({"PB_SQL_TIER": "big"}, "sql_tier"), ({"PB_PROJECT": "X"}, "project"),
])
def test_invalid_values_are_refused(env, message):
    with pytest.raises(dc.ConfigError, match=message):
        load(env)


@pytest.mark.parametrize("text, message", [
    ("nme: typo\n", "unknown setting"), ("min_instances: many\n", "min_instances"), ("min_instances: 3\nmax_instances: 1\n", "max_instances"),
    ("secrets_from_dotenv: not-a-list\n", "secrets_from_dotenv"), ("secrets_provided: ['bad name!']\n", "secrets_provided"), ("- just\n- a list\n", "mapping"),
    ("name: [unclosed\n", "not valid YAML"),
])
def test_a_broken_local_file_is_refused(tmp_path, text, message):
    local = tmp_path / "local.yaml"
    local.write_text(text)
    with pytest.raises(dc.ConfigError, match=message):
        load(local=local)


# ── the bash side ────────────────────────────────────────────────────────

def test_the_shell_output_is_safe_to_eval_even_for_hostile_values(tmp_path):
    marker = tmp_path / "pwned"
    local = tmp_path / "local.yaml"
    local.write_text(f'schedule: "$(touch {marker})"\nsql_tier: "db-f1-micro"\n')  # a schedule is free text: it must stay inert
    out = dc.shell_lines(load(local=local))
    r = subprocess.run(["bash", "-c", f'eval {__import__("shlex").quote(out)}; printf "%s|%s|%s" "$NAME" "$SCHEDULE" "$RUNTIME_SA"'], capture_output=True, text=True)
    assert r.returncode == 0 and not marker.exists(), r.stderr
    assert r.stdout == f"agent|$(touch {marker})|agent-runtime@test-proj.iam.gserviceaccount.com"


def test_the_command_line_tool_prints_shell_and_reports_errors_with_exit_code_2(tmp_path):
    env = {**os.environ, "PB_LOCAL_CONFIG": str(NOWHERE), "PB_PROJECT": "test-proj", "PB_NAME": "cli-agent"}
    ok = subprocess.run([sys.executable, str(ROOT / "deploy" / "deploy_config.py"), "shell"], capture_output=True, text=True, env=env)
    assert ok.returncode == 0 and "NAME=cli-agent" in ok.stdout and "REPO=cli-agent-repo" in ok.stdout
    bad = subprocess.run([sys.executable, str(ROOT / "deploy" / "deploy_config.py"), "shell"], capture_output=True, text=True, env={**env, "PB_NAME": "BAD NAME"})
    assert bad.returncode == 2 and "deploy config error" in bad.stderr and not bad.stdout


def test_deploy_sh_dry_run_uses_the_configured_name_everywhere():
    env = {**os.environ, "DRY_RUN": "1", "PB_PROJECT": "my-proj", "PB_NAME": "weather-bot", "PB_LOCAL_CONFIG": str(NOWHERE)}
    for step in ("setup", "sql", "build", "deploy", "scheduler", "status", "down"):
        r = subprocess.run(["bash", str(ROOT / "deploy" / "deploy.sh"), step], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
        assert r.returncode == 0, (step, r.stdout[-300:], r.stderr[-300:])
        text = (r.stdout + r.stderr).lower()
        assert "pinboard" not in text and "weather-bot" in text, step
    sql = subprocess.run(["bash", str(ROOT / "deploy" / "deploy.sh"), "sql"], capture_output=True, text=True, env=env, cwd=ROOT).stdout
    assert "weather-bot-pg" in sql and "databases create weather-bot" in sql and "users create weather-bot" in sql


def test_a_bad_config_stops_deploy_sh_before_it_runs_anything():
    env = {**os.environ, "DRY_RUN": "1", "PB_PROJECT": "my-proj", "PB_NAME": "NOT VALID", "PB_LOCAL_CONFIG": str(NOWHERE)}
    r = subprocess.run(["bash", str(ROOT / "deploy" / "deploy.sh"), "setup"], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 2 and "deploy config error" in r.stderr and "gcloud" not in r.stdout


# ── Terraform reads the same file ────────────────────────────────────────

def test_terraform_takes_its_names_from_the_config_and_hard_codes_none():
    import re

    tf = {p.name: p.read_text() for p in (ROOT / "terraform").glob("*.tf")}
    assert 'file("${path.module}/../deploy/config.yaml")' in tf["config.tf"] and "config.local.yaml" in tf["config.tf"]
    used = set(re.findall(r"local\.cfg\.(\w+)", tf["config.tf"]))
    assert used <= dc.KEYS, used - dc.KEYS  # Terraform may only ask for settings the Python loader knows about too
    code = "\n".join(re.sub(r"#.*", "", text) for name, text in tf.items())
    for line in code.splitlines():
        if "pinboard" in line.lower() and "description" not in line:  # a help text may name the `pinboard` command
            assert "PINBOARD_" in line, f"a resource name is hard-coded in Terraform: {line.strip()}"  # only the app's own setting names may say pinboard


def test_the_web_access_list_is_the_engines_allowed_users_and_service_accounts_get_the_right_member_type(tmp_path):
    policy = tmp_path / "policy.yaml"
    policy.write_text("allowed_users:\n  - Alice@Example.com\n  - bot@proj.iam.gserviceaccount.com\n")
    c = dc.load(env={}, local=NOWHERE, project_lookup=lambda: "test-proj", policy_file=policy)
    assert c["web_users"] == ["alice@example.com", "bot@proj.iam.gserviceaccount.com"]
    assert dc.load(env={}, local=NOWHERE, project_lookup=lambda: "test-proj", policy_file=tmp_path / "missing.yaml")["web_users"] == []
    assert dc.load(env={"PINBOARD_POLICY": str(policy)}, local=NOWHERE, project_lookup=lambda: "test-proj")["web_users"] == c["web_users"]
    env = {**os.environ, "DRY_RUN": "1", "PB_PROJECT": "my-proj", "PB_NAME": "webtest", "PB_LOCAL_CONFIG": str(NOWHERE), "PINBOARD_POLICY": str(policy)}
    r = subprocess.run(["bash", str(ROOT / "deploy" / "deploy.sh"), "web"], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "--member=user:alice@example.com" in r.stdout and "--member=serviceAccount:bot@proj.iam.gserviceaccount.com" in r.stdout
    no_users = tmp_path / "empty.yaml"
    no_users.write_text("allowed_users: []\n")
    r = subprocess.run(["bash", str(ROOT / "deploy" / "deploy.sh"), "web"], capture_output=True, text=True, env={**env, "PINBOARD_POLICY": str(no_users)}, cwd=ROOT)
    assert r.returncode == 2 and "nobody could open" in r.stderr  # refuses to deploy an app nobody may open


def test_the_runtime_service_account_gets_the_same_roles_in_deploy_sh_and_terraform():
    """A role missing in only one place meant a bug in production once (file uploads failed: no serviceusage.serviceUsageConsumer)."""
    import re

    sh = (ROOT / "deploy" / "deploy.sh").read_text()
    line = next(l for l in sh.splitlines() if l.strip().startswith("for role in roles/"))
    sh_roles = set(re.findall(r"roles/[A-Za-z.]+", line))
    tf = (ROOT / "terraform" / "main.tf").read_text()
    tf_roles = set(re.findall(r'"(roles/[A-Za-z.]+)"', re.search(r'resource "google_project_iam_member" "runtime" \{.*?\n\}', tf, re.S).group(0)))
    assert sh_roles == tf_roles and "roles/serviceusage.serviceUsageConsumer" in sh_roles, (sh_roles ^ tf_roles)
