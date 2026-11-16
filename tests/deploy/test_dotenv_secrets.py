"""Secrets are found in .env by name, not by a list someone has to keep up to date; everything else is a plain setting."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))

import dotenv_secrets  # noqa: E402
import runtime  # noqa: E402

from test_deploy_config import NOWHERE  # noqa: E402  (the shared 'no local config file' path)
import deploy_config  # noqa: E402

ENV = {
    "TAVILY_API_KEY": "tvly-real", "MY_NEW_SERVICE_TOKEN": "tok", "DB_PASSWORD": "pw", "GITHUB_APP_PRIVATE_KEY": "-----BEGIN KEY-----\nabc\n-----END KEY-----\n",
    "GITHUB_APP_ID": "123456", "MODEL": "google_genai:gemini-2.5-flash", "TAVILY_MAX_RESULTS": "5", "SANDBOX_RUNTIME": "rt-1",
    "GOOGLE_API_KEY": "your-gemini-api-key-here", "EMPTY_TOKEN": "", "REF_SECRET": "secret://x",   # placeholders, empty values and references are skipped
    "DATABASE_URL": "postgresql://local", "PINBOARD_API_TOKEN": "local-token", "SKILLS_SOURCE": "./skills_example", "OUTPUT_DIR": "./outputs",  # local / deployment-managed
    "lower_case": "x",
}


def test_secrets_are_found_by_name_and_everything_else_is_a_plain_setting():
    parts = dotenv_secrets.split(ENV)
    assert sorted(parts.secrets) == ["DB_PASSWORD", "GITHUB_APP_PRIVATE_KEY", "MY_NEW_SERVICE_TOKEN", "TAVILY_API_KEY"]
    assert parts.secrets["GITHUB_APP_PRIVATE_KEY"].endswith("-----END KEY-----\n")  # a private key is kept exactly as written
    assert parts.plain == {"GITHUB_APP_ID": "123456", "MODEL": "google_genai:gemini-2.5-flash", "TAVILY_MAX_RESULTS": "5", "SANDBOX_RUNTIME": "rt-1"}


def test_names_the_deployment_decides_or_that_only_make_sense_locally_never_travel():
    parts = dotenv_secrets.split(ENV)
    for never in ("DATABASE_URL", "PINBOARD_API_TOKEN", "SKILLS_SOURCE", "OUTPUT_DIR"):
        assert never not in parts.secrets and never not in parts.plain


def test_the_config_can_exclude_more():
    assert "TAVILY_API_KEY" not in dotenv_secrets.split(ENV, ["TAVILY_API_KEY"]).secrets


def test_the_engine_gets_the_plain_settings_and_the_names_of_the_secrets():
    cfg = deploy_config.load(env={"PB_LOCAL_CONFIG": str(NOWHERE), "PB_PROJECT": "test-proj"}, project_lookup=False, policy_file=NOWHERE)
    conf = runtime.runtime_config(project="test-proj", region="us-central1", image="img@sha256:1", service_account="sa", sql_connection="p:r:i",
                                  bucket="b", dotenv=ENV, cfg=cfg)
    env = conf["env_vars"]
    assert env["TAVILY_MAX_RESULTS"] == "5" and env["GITHUB_APP_ID"] == "123456"
    assert env["SECRETS_BACKEND"] == "gcp" and env["STATE_BACKEND"] == "postgres"  # the deployment's own choices cannot be overridden from .env
    assert env["DATABASE_URL"].startswith("secret://") and env["PINBOARD_API_TOKEN"].startswith("secret://")
    assert "SKILLS_SOURCE" not in env and "OUTPUT_DIR" not in env
    names = set(env["EXTRA_SECRET_NAMES"].split(","))
    assert {"MY_NEW_SERVICE_TOKEN", "DB_PASSWORD", "TAVILY_API_KEY"} <= names
    assert not any("tvly-real" in str(v) or v == "pw" for v in env.values())  # a secret's VALUE never goes into the runtime configuration


def test_output_redaction_covers_the_extra_secrets(monkeypatch):
    from pinboard.core.config import secret_names

    monkeypatch.setenv("EXTRA_SECRET_NAMES", "MY_NEW_SERVICE_TOKEN, DB_PASSWORD")
    assert {"MY_NEW_SERVICE_TOKEN", "DB_PASSWORD", "TAVILY_API_KEY"} <= set(secret_names())
    monkeypatch.delenv("EXTRA_SECRET_NAMES")
    assert "MY_NEW_SERVICE_TOKEN" not in secret_names()
