import pytest

from pinboard.cli import main
from pinboard.core.config import Settings


def test_settings_defaults_and_validation(monkeypatch, tmp_path):
    for k in ["COMMAND_POLICY", "APPROVAL_MODE", "SANDBOX_CWD", "SKILLS_SOURCE", "GCP_PROJECT"]:
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env(env_file=None)
    assert s.command_policy == "allowlist" and s.approval_mode == "none"
    assert s.skills_path == "/workspace/skills"
    monkeypatch.setenv("COMMAND_POLICY", "yolo")
    with pytest.raises(ValueError):
        Settings.from_env(env_file=None)


def test_settings_loads_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    f = tmp_path / ".env"
    f.write_text("GCP_PROJECT=my-proj\nSANDBOX_CWD=/work\n")
    monkeypatch.delenv("SANDBOX_CWD", raising=False)
    s = Settings.from_env(env_file=f)
    assert s.gcp_project == "my-proj" and s.skills_path == "/work/skills"
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("SANDBOX_CWD", raising=False)


def test_cli_requires_command():
    with pytest.raises(SystemExit):
        main([])


def test_skills_upload_needs_target(monkeypatch, capsys):
    monkeypatch.setenv("SKILLS_SOURCE", "./local")
    assert main(["skills-upload", "skills_example"]) == 2


@pytest.fixture(autouse=True)
def _no_dotenv(tmp_path, monkeypatch):
    """Settings.from_env() loads ./.env; run from an empty dir so a developer's real .env cannot change the outcome."""
    monkeypatch.chdir(tmp_path)


def test_config_errors_print_one_clear_line_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("STATE_BACKEND", "memory")
    monkeypatch.setenv("PINBOARD_API_TOKEN", "your-api-token-here")  # placeholder == not set
    monkeypatch.delenv("PINBOARD_DEBUG", raising=False)
    assert main(["serve"]) == 2
    err = capsys.readouterr().err
    assert "PINBOARD_API_TOKEN is not set" in err and "Traceback" not in err


def test_debug_flag_restores_the_traceback(monkeypatch):
    monkeypatch.setenv("STATE_BACKEND", "memory")
    monkeypatch.setenv("PINBOARD_API_TOKEN", "your-api-token-here")
    monkeypatch.setenv("PINBOARD_DEBUG", "1")
    with pytest.raises(Exception, match="PINBOARD_API_TOKEN"):
        main(["serve"])


def test_interactive_approver_answers_questions_and_approves_or_rejects_actions(monkeypatch, capsys):
    from pinboard.cli import _interactive_approver

    answers = iter(["markdown please", "y", "n", ""])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    out = _interactive_approver([
        {"name": "ask_user", "args": {"question": "Which format?"}, "allowed_decisions": ["respond"]},
        {"name": "send_email", "args": {"to": ["me@example.com"], "subject": "Hi", "body": "b"}},
        {"name": "execute", "args": {"command": "ls"}},
        {"name": "ask_user", "args": {"question": "Anything else?"}, "allowed_decisions": ["respond"]},
    ])
    assert out[0] == {"type": "respond", "message": "markdown please"}
    assert out[1] == {"type": "approve"} and out[2] == {"type": "reject", "message": "Rejected by user"}
    assert out[3] == {"type": "respond", "message": "The user chose not to answer."}
    shown = capsys.readouterr().out
    assert "[question]" in shown and "[email]" in shown and "[shell]" in shown and "Which format?" in shown
