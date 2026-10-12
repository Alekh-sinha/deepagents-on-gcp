import pytest

from pinboard.agent import build_model
from pinboard.gcp.vault import SecretNotFound, Vault

from helpers import make_settings


def test_vertex_model_uses_adc_not_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    captured = {}

    class Stub:
        def __init__(self, **kw):
            captured.update(kw)

    import langchain_google_genai
    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", Stub)
    s = make_settings(tmp_path, model="google_genai:gemini-2.5-flash", google_use_vertex=True, gcp_project="proj", model_location="global")
    build_model(s, Vault("env"))
    assert captured == {"model": "gemini-2.5-flash", "vertexai": True, "project": "proj", "location": "global", "timeout": 120, "max_retries": 3}
    assert "api_key" not in captured and "google_api_key" not in captured


def test_vertex_location_defaults_to_gcp_location(monkeypatch, tmp_path):
    captured = {}

    class Stub:
        def __init__(self, **kw):
            captured.update(kw)

    import langchain_google_genai
    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", Stub)
    build_model(make_settings(tmp_path, model="google_genai:m", google_use_vertex=True), Vault("env"))
    assert captured["location"] == "us-central1"


def test_vertex_requires_project(tmp_path):
    with pytest.raises(ValueError):
        build_model(make_settings(tmp_path, model="google_genai:m", google_use_vertex=True, gcp_project=None), Vault("env"))


def test_api_key_path_still_requires_key(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(SecretNotFound):
        build_model(make_settings(tmp_path, model="google_genai:m", google_use_vertex=False), Vault("env"))


def test_settings_reads_vertex_flag(monkeypatch):
    from pinboard.core.config import Settings
    monkeypatch.setenv("GOOGLE_USE_VERTEX", "true")
    monkeypatch.setenv("MODEL_LOCATION", "global")
    s = Settings.from_env(env_file=None)
    assert s.google_use_vertex is True and s.model_location == "global"


def test_model_call_limits_come_from_settings_and_are_bounded_by_default(monkeypatch, tmp_path):
    from pinboard.core.config import Settings

    captured = {}

    class Stub:
        def __init__(self, **kw):
            captured.update(kw)

    import langchain_google_genai
    monkeypatch.setattr(langchain_google_genai, "ChatGoogleGenerativeAI", Stub)
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("MODEL_MAX_RETRIES", "1")
    s = Settings.from_env(env_file=None)
    assert (s.model_timeout_seconds, s.model_max_retries) == (45, 1)
    build_model(make_settings(tmp_path, model="google_genai:m", google_use_vertex=True, model_timeout_seconds=45, model_max_retries=1), Vault("env"))
    assert captured["timeout"] == 45 and captured["max_retries"] == 1
    monkeypatch.delenv("MODEL_TIMEOUT_SECONDS")
    monkeypatch.delenv("MODEL_MAX_RETRIES")
    d = Settings.from_env(env_file=None)
    assert d.model_timeout_seconds == 120 and d.model_max_retries == 3  # never unbounded unless someone chooses it
