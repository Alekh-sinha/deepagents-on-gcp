import pytest

from pinboard.gcp.vault import SecretNotFound, Vault, is_placeholder, push_secrets


class FakeRemote:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.put_calls = []

    def get(self, name):
        return self.data.get(name)

    def put(self, name, value):
        self.put_calls.append(name)
        status = "updated" if name in self.data else "created"
        self.data[name] = value
        return status


def test_placeholders_are_unset(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "your-tavily-key-here")
    assert is_placeholder("your-tavily-key-here") and is_placeholder("") and is_placeholder("<key>")
    v = Vault("env")
    assert v.get("TAVILY_API_KEY") is None
    with pytest.raises(SecretNotFound):
        v.get("TAVILY_API_KEY", required=True)


def test_plain_value(monkeypatch):
    monkeypatch.setenv("K", "tvly-realvalue123")
    assert Vault("env").get("K") == "tvly-realvalue123"


def test_secret_ref_resolves_from_remote(monkeypatch):
    monkeypatch.setenv("K", "secret://REMOTE_NAME")
    v = Vault("gcp", remote=FakeRemote({"REMOTE_NAME": "from-vault-123456"}))
    assert v.get("K") == "from-vault-123456"


def test_secret_ref_without_remote_errors(monkeypatch):
    monkeypatch.setenv("K", "secret://X")
    with pytest.raises(SecretNotFound):
        Vault("env").get("K")


def test_fallback_to_remote_when_unset(monkeypatch):
    monkeypatch.delenv("K", raising=False)
    v = Vault("gcp", remote=FakeRemote({"K": "remote-value-123"}))
    assert v.get("K") == "remote-value-123"


def test_env_backend_does_not_use_remote(monkeypatch):
    monkeypatch.delenv("K", raising=False)
    assert Vault("env", remote=None).get("K") is None


def test_bad_backend():
    with pytest.raises(ValueError):
        Vault("nope")


def test_redact(monkeypatch):
    monkeypatch.setenv("K", "supersecretvalue99")
    v = Vault("env")
    v.get("K")
    assert v.redact("token=supersecretvalue99 end") == "token=[REDACTED:K] end"
    assert v.redact("nothing here") == "nothing here"


def test_push_secrets(monkeypatch):
    monkeypatch.setenv("A", "real-a-value-1")
    monkeypatch.setenv("B", "your-b-here")
    monkeypatch.setenv("C", "secret://C")
    monkeypatch.delenv("D", raising=False)
    remote = FakeRemote()
    report = push_secrets(["A", "B", "C", "D"], "proj", remote=remote)
    assert report["A"] == "created"
    assert report["B"].startswith("skipped") and report["C"].startswith("skipped") and report["D"].startswith("skipped")
    assert remote.put_calls == ["A"]


# ── remembering "not found" (a real run showed 1-4 s of setup per turn; every turn asked Secret Manager again for secrets that do not exist) ──

class CountingRemote:
    def __init__(self, data=None):
        self.data, self.calls = data or {}, []

    def get(self, name):
        self.calls.append(name)
        return self.data.get(name)


def test_a_missing_secret_is_not_asked_for_again_on_every_turn(monkeypatch):
    monkeypatch.delenv("NOT_THERE", raising=False)
    now = [100.0]
    remote = CountingRemote()
    v = Vault("gcp", "p", remote=remote, miss_ttl=120, clock=lambda: now[0])
    assert v.get("NOT_THERE") is None and v.get("NOT_THERE") is None and v.get("NOT_THERE") is None
    assert remote.calls == ["NOT_THERE"]  # asked once, remembered
    now[0] += 121
    assert v.get("NOT_THERE") is None and remote.calls == ["NOT_THERE", "NOT_THERE"]  # asked again after the memory expires


def test_a_secret_that_appears_later_is_found_once_the_memory_expires(monkeypatch):
    monkeypatch.delenv("LATE_SECRET", raising=False)
    now = [0.0]
    remote = CountingRemote()
    v = Vault("gcp", "p", remote=remote, miss_ttl=60, clock=lambda: now[0])
    assert v.get("LATE_SECRET") is None
    remote.data["LATE_SECRET"] = "now-it-exists-1234"
    assert v.get("LATE_SECRET") is None  # still inside the memory window
    now[0] += 61
    assert v.get("LATE_SECRET") == "now-it-exists-1234" and v.get("LATE_SECRET") == "now-it-exists-1234" and remote.calls.count("LATE_SECRET") == 2


def test_required_secrets_still_fail_loudly_and_quickly_when_missing(monkeypatch):
    monkeypatch.delenv("NEEDED", raising=False)
    remote = CountingRemote()
    v = Vault("gcp", "p", remote=remote, miss_ttl=120, clock=lambda: 0.0)
    for _ in range(3):
        with pytest.raises(SecretNotFound):
            v.get("NEEDED", required=True)
    assert remote.calls == ["NEEDED"]


def test_found_secrets_are_unaffected_by_the_miss_memory(monkeypatch):
    monkeypatch.delenv("FOUND", raising=False)
    remote = CountingRemote({"FOUND": "value-12345678"})
    v = Vault("gcp", "p", remote=remote, clock=lambda: 0.0)
    assert v.get("FOUND") == "value-12345678" and v.get("FOUND") == "value-12345678" and remote.calls == ["FOUND"]
    assert v.redact("token value-12345678 here") == "token [REDACTED:FOUND] here"
