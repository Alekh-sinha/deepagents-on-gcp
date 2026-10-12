"""The deployment tooling that can be checked without a cloud: the runtime configuration, deploy.sh in dry-run mode, and
scripts/smoke_runtime.py driven against the real API app (so a bug in the script is not mistaken for a bug in the deployment)."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pinboard.engine.api import CONTRACT_METHODS, create_app

from helpers import call, say
from turn_harness import Hub

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


runtime = load("deploy_runtime", "deploy/runtime.py")
smoke = load("smoke_runtime", "scripts/smoke_runtime.py")

deploy_config = load("deploy_config", "deploy/deploy_config.py")
NOWHERE = ROOT / "deploy" / "no-such-local-file.yaml"  # the real config.local.yaml (this machine's own values) must never leak into a test
CFG = deploy_config.load(env={"PB_NAME": "pinboard"}, local=NOWHERE, project_lookup=lambda: "test-proj")
ARGS = dict(project="p", region="us-central1", image="img:1", service_account="sa", sql_connection="p:r:i", bucket="b", cfg=CFG)


# ── runtime configuration ────────────────────────────────────────────────

def test_runtime_config_declares_exactly_what_the_server_serves_and_holds_no_secret_values():
    cfg = runtime.runtime_config(**ARGS, dotenv={"SANDBOX_RUNTIME": "projects/p/x", "GITHUB_APP_ID": "123", "TAVILY_API_KEY": "tvly-REAL"})
    assert cfg["class_methods"] == CONTRACT_METHODS
    assert cfg["agent_framework"] == "langgraph"
    assert cfg["service_account"] == "sa@p.iam.gserviceaccount.com"
    env = cfg["env_vars"]
    assert env["DATABASE_URL"] == "secret://pinboard-database-url" and env["PINBOARD_API_TOKEN"] == "secret://pinboard-api-token"
    assert env["SECRETS_BACKEND"] == "gcp" and env["STATE_BACKEND"] == "postgres" and env["ARTIFACTS_URI"] == "gs://b/pinboard"
    assert env["GITHUB_APP_ID"] == "123"
    assert "tvly-REAL" not in json.dumps(cfg) and "TAVILY_API_KEY" not in env


def test_an_agent_without_a_sandbox_is_deployed_without_a_sandbox_runtime():
    env = runtime.runtime_config(**ARGS, dotenv={}, sandbox=False)["env_vars"]
    assert "SANDBOX_RUNTIME" not in env
    with pytest.raises(SystemExit, match="SANDBOX_RUNTIME"):  # the default is unchanged: a sandbox needs its runtime
        runtime.runtime_config(**ARGS, dotenv={})


def test_the_deploy_reads_the_sandbox_switch_from_the_policy_file(tmp_path, monkeypatch):
    import yaml

    data = yaml.safe_load(open("config/policy.yaml"))
    path = tmp_path / "p.yaml"
    monkeypatch.setenv("PINBOARD_POLICY", str(path))
    path.write_text(yaml.safe_dump(data))
    assert runtime.policy_has_sandbox() is True                                    # the shipped policy
    path.write_text(yaml.safe_dump({**data, "features": {"sandbox": False}, "warm_sandbox": {"mode": "off"}}))
    assert runtime.policy_has_sandbox() is False
    path.write_text(yaml.safe_dump({**data, "features": {"sandbox": False}}))      # inconsistent (warm pool still on): refused, so stay strict
    assert runtime.policy_has_sandbox() is True
    path.write_text("not: [valid")
    assert runtime.policy_has_sandbox() is True                                    # unreadable: stay strict


def test_runtime_config_ignores_placeholder_values_and_needs_a_sandbox_runtime():
    with pytest.raises(SystemExit, match="SANDBOX_RUNTIME"):
        runtime.runtime_config(**ARGS, dotenv={"SANDBOX_RUNTIME": "your-sandbox-runtime"})
    env = runtime.runtime_config(**ARGS, dotenv={"SANDBOX_RUNTIME": "r", "GITHUB_APP_ID": "your-app-id"})["env_vars"]
    assert "GITHUB_APP_ID" not in env


# ── deploy.sh (dry run: prints what it would do, changes nothing) ────────

def sh(*args, **env):
    e = {**os.environ, "DRY_RUN": "1", "PB_PROJECT": "my-proj", "PB_NAME": "pinboard", "PB_LOCAL_CONFIG": str(NOWHERE), **env}
    return subprocess.run(["bash", str(ROOT / "deploy" / "deploy.sh"), *args], capture_output=True, text=True, env=e, cwd=ROOT, timeout=60)


@pytest.mark.parametrize("cmd", ["setup", "sql", "build", "deploy", "scheduler", "status"])
def test_every_deploy_step_dry_runs_cleanly(cmd):
    r = sh(cmd)
    if cmd == "build" and list((ROOT / "deploy" / "extra-ca").glob("*.crt")):  # the gateway spike's certificate is lying around: refusing is the correct answer
        assert r.returncode == 1 and "extra-ca" in r.stderr
        return
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Traceback" not in r.stdout + r.stderr


def test_resource_names_do_not_pick_up_the_shells_own_name_variable():
    r = sh("setup", NAME="LAPTOP-SOMETHING")  # in WSL, NAME is the computer's name
    assert "LAPTOP" not in r.stdout
    assert "pinboard-repo" in r.stdout


def test_dry_run_never_prints_secret_values_and_the_scripts_are_valid_shell():
    r = sh("setup", TAVILY_API_KEY="tvly-SHOULD-NOT-APPEAR")
    assert "tvly-SHOULD-NOT-APPEAR" not in r.stdout + r.stderr
    for f in ("deploy.sh", "start.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "deploy" / f)]).returncode == 0


# ── smoke_runtime.py against the real app ────────────────────────────────

class InProcess:
    """Same interface as smoke.RestTransport, but talks to the app through the container contract routes (double-encoded)."""

    def __init__(self, client):
        self.c = client

    def _post(self, path, method, inp):
        body = json.dumps({"class_method": method, "input": inp or {}})
        return self.c.post(path, content=json.dumps(body), headers={"content-type": "application/json"})

    def call(self, method, inp=None, timeout=0):
        r = self._post("/api/reasoning_engine", method, inp)
        assert r.status_code == 200, r.text
        return smoke._unwrap(r.json(), method)

    def stream(self, method, inp, timeout=0):
        r = self._post("/api/stream_reasoning_engine", method, inp)
        assert r.status_code == 200, r.text
        for line in r.text.splitlines():
            if line:
                yield smoke._unwrap(json.loads(line), method)


def smoke_for(tmp_path, monkeypatch, scripts, **kw):
    h = Hub("memory", tmp_path, monkeypatch, scripts, **kw)
    svc = h.new_process()
    app = create_app(svc, "t" * 32, trust_platform_auth=True)
    lines = []
    return smoke.Smoke(InProcess(TestClient(app)), say=lines.append), lines


def test_smoke_basics_pass_against_a_healthy_app(tmp_path, monkeypatch):
    s, lines = smoke_for(tmp_path, monkeypatch, [[say("pong")]])
    s.basics()
    assert s.ok(), "\n".join(lines)
    assert sum("[PASS]" in x for x in lines) == 4


def test_smoke_reports_a_wrong_answer_as_a_failure_not_a_crash(tmp_path, monkeypatch):
    s, lines = smoke_for(tmp_path, monkeypatch, [[say("something else")]])
    s.basics()
    assert not s.ok()
    assert any("[FAIL] chat turn" in x and "unexpected answer" in x for x in lines)


def test_smoke_sandbox_check_requests_then_rejects_an_approval(tmp_path, monkeypatch):
    s, lines = smoke_for(tmp_path, monkeypatch, [[call("execute", {"command": "echo hello-from-sandbox"}), say("x")], [say("ok, skipped")]], approval_mode="execute")
    s.sandbox()
    assert s.ok(), "\n".join(lines)


# ── terraform ─────────────────────────────────────────────────────────────

def test_terraform_class_methods_match_what_the_server_serves():
    on_disk = json.loads((ROOT / "terraform" / "class_methods.json").read_text())
    assert on_disk == CONTRACT_METHODS, ("regenerate: uv run python -c \"import json;from pinboard.engine.api import CONTRACT_METHODS as C;"
                                         "json.dump(C,open('terraform/class_methods.json','w'),indent=2)\"")


def test_terraform_runtime_env_matches_the_python_deploy_tool():
    import re

    tf = (ROOT / "terraform" / "main.tf").read_text()
    tf_names = set(re.findall(r"^\s{6}([A-Z][A-Z_]+)\s+=", tf, re.M)) | {"SANDBOX_RUNTIME", "GITHUB_APP_ID", "AGENTMAIL_INBOX_ID"}  # these three are set conditionally in main.tf
    py = set(runtime.runtime_config(**ARGS, dotenv={"SANDBOX_RUNTIME": "r", "GITHUB_APP_ID": "1", "AGENTMAIL_INBOX_ID": "i"})["env_vars"]) - {"MODEL_LOCATION"}
    assert py == tf_names, (py ^ tf_names)


# ── gateway variant of the runtime ───────────────────────────────────────

def test_a_gateway_runtime_gets_an_agent_identity_and_the_gateway_but_the_normal_one_does_not():
    plain = runtime.runtime_config(**ARGS, dotenv={"SANDBOX_RUNTIME": "r"})
    assert "identity_type" not in plain and "agent_gateway_config" not in plain
    gw = runtime.runtime_config(**ARGS, dotenv={"SANDBOX_RUNTIME": "r"}, agent_gateway="projects/p/locations/l/agentGateways/g",
                                display_name="pinboard-gw", extra_env={"DATABASE_URL": "secret://other"})
    assert gw["identity_type"] == "AGENT_IDENTITY"
    assert gw["env_vars"]["GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"] == "false"  # Google's gateway instructions
    assert "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES" not in plain["env_vars"]
    assert "service_account" not in gw and plain["service_account"] == "sa@p.iam.gserviceaccount.com"  # the API refuses both together
    assert gw["agent_gateway_config"] == {"agent_to_anywhere_config": {"agent_gateway": "projects/p/locations/l/agentGateways/g"}}
    assert gw["display_name"] == "pinboard-gw" and gw["env_vars"]["DATABASE_URL"] == "secret://other"


def test_the_image_trusts_extra_certificates_and_the_normal_build_refuses_to_include_one(tmp_path):
    docker = (ROOT / "Dockerfile").read_text()
    assert "deploy/extra-ca" in docker and "update-ca-certificates" in docker
    script = (ROOT / "deploy" / "deploy.sh").read_text()
    assert 'compgen -G "deploy/extra-ca/*.crt"' in script  # a certificate left over from the gateway spike must not reach the normal image


# ── container entrypoint ─────────────────────────────────────────────────

@pytest.mark.parametrize("private, expected", [("", False), ("true", True)])
def test_start_sh_passes_private_ip_to_the_proxy_only_when_asked(tmp_path, private, expected):
    import socket
    import textwrap

    with socket.socket() as probe:  # any free port: start.sh takes it from SQL_PROXY_PORT
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    rec = tmp_path / "args"
    (tmp_path / "cloud-sql-proxy").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" > {rec}
        exec python3 -c "import socket,time; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1',{port})); s.listen(); time.sleep(5)"
        """))
    (tmp_path / "pinboard").write_text("#!/bin/sh\necho started\n")
    (tmp_path / "python").symlink_to(sys.executable)  # the container has `python`; a developer machine may only have python3
    for f in ("cloud-sql-proxy", "pinboard"):
        (tmp_path / f).chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "SQL_CONNECTION": "p:r:i", "SQL_PRIVATE_IP": private, "SQL_PROXY_PORT": str(port)}
    r = subprocess.run(["sh", str(ROOT / "deploy" / "start.sh")], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "started" in r.stdout, r.stdout + r.stderr
    args = rec.read_text().split()
    assert ("--private-ip" in args) is expected and args[-1] == "p:r:i"


# ── a recorded runtime that no longer exists ─────────────────────────────

class _Runtimes:
    def __init__(self, get_error=None):
        self.get_error, self.calls = get_error, []

    def get(self, name):
        if self.get_error:
            raise self.get_error
        return object()


def test_a_deleted_runtime_is_forgotten_but_other_errors_are_not_taken_as_deleted():
    gone = type("C", (), {"runtimes": _Runtimes(RuntimeError("404 NOT_FOUND. The ReasoningEngine does not exist."))})()
    assert runtime._still_exists(gone, "projects/p/x") is False
    flaky = type("C", (), {"runtimes": _Runtimes(RuntimeError("503 UNAVAILABLE"))})()
    assert runtime._still_exists(flaky, "projects/p/x") is True
    fine = type("C", (), {"runtimes": _Runtimes()})()
    assert runtime._still_exists(fine, "projects/p/x") is True


def test_a_runtime_can_join_a_vpc_through_a_psc_interface():
    att = "projects/p/regions/r/networkAttachments/na"
    cfg = runtime.runtime_config(**ARGS, dotenv={"SANDBOX_RUNTIME": "r"}, psc_attachment=att)
    assert cfg["psc_interface_config"] == {"network_attachment": att}
    assert "psc_interface_config" not in runtime.runtime_config(**ARGS, dotenv={"SANDBOX_RUNTIME": "r"})


def test_the_runtime_configuration_follows_the_deployment_name():
    cfg = deploy_config.load(env={"PB_NAME": "weather-bot"}, local=NOWHERE, project_lookup=lambda: "test-proj")
    c = runtime.runtime_config(**{**ARGS, "cfg": cfg}, dotenv={"SANDBOX_RUNTIME": "r"})
    env = c["env_vars"]
    assert env["DATABASE_URL"] == "secret://weather-bot-database-url" and env["PINBOARD_API_TOKEN"] == "secret://weather-bot-api-token"
    assert env["ARTIFACTS_URI"] == "gs://b/weather-bot" and c["display_name"] == "weather-bot" and c["labels"] == {"app": "weather-bot"}
    assert (c["min_instances"], c["max_instances"]) == (1, 2)  # the config's defaults
    scaled = deploy_config.load(env={}, local=NOWHERE, project_lookup=lambda: "test-proj") | {"min_instances": 0, "max_instances": 5}
    c = runtime.runtime_config(**{**ARGS, "cfg": scaled}, dotenv={"SANDBOX_RUNTIME": "r"})
    assert (c["min_instances"], c["max_instances"]) == (0, 5)


def test_the_mcp_encryption_key_is_created_by_both_deploy_paths():
    """Tokens users paste for their MCP servers are encrypted with MCP_ENCRYPTION_KEY: both the script and Terraform must create it."""
    sh = (ROOT / "deploy" / "deploy.sh").read_text()
    tf = (ROOT / "terraform" / "main.tf").read_text()
    assert "MCP_ENCRYPTION_KEY" in sh and "generate_key" in sh and 'MCP_ENCRYPTION_KEY"])' in tf and 'random_id.mcp_key.b64_url}="' in tf
    from pinboard.core.config import SECRET_NAMES

    assert "MCP_ENCRYPTION_KEY" in SECRET_NAMES  # so the vault hides its value in output
