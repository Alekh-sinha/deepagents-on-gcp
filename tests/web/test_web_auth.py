"""Sign-in verification, and how the web backend reads the engine's errors (shapes captured from the REAL Agent Runtime)."""
import json

import pytest

from pinboard.web.auth import IAP_HEADER, IAP_ISSUER, AuthError, DevAuthenticator, GoogleAuthenticator, IapAuthenticator
from pinboard.web.runtime_client import GoogleRuntimeClient, RuntimeCallError, parse_error


# ── IAP ──────────────────────────────────────────────────────────────────

def verifier(claims=None, error=None):
    def verify(token, audience):
        verify.seen = (token, audience)
        if error:
            raise error
        return claims
    return verify


def test_a_valid_iap_token_gives_the_lower_cased_email_and_is_checked_against_the_audience():
    v = verifier({"iss": IAP_ISSUER, "email": "Me@Example.com"})
    assert IapAuthenticator("/projects/1/x", verify=v).email({IAP_HEADER: "jwt"}) == "me@example.com" and v.seen == ("jwt", "/projects/1/x")


@pytest.mark.parametrize("headers, v", [
    ({}, verifier({"iss": IAP_ISSUER, "email": "a@b.c"})),                                            # no token at all
    ({IAP_HEADER: "jwt"}, verifier(error=ValueError("bad signature or audience"))),                 # forged / expired / wrong audience
    ({IAP_HEADER: "jwt"}, verifier({"iss": "https://accounts.google.com", "email": "a@b.c"})),      # a token from another issuer
    ({IAP_HEADER: "jwt"}, verifier({"iss": IAP_ISSUER})),                                          # no email
])
def test_anything_else_is_not_signed_in(headers, v):
    with pytest.raises(AuthError):
        IapAuthenticator("aud", verify=v).email(headers)


def test_the_plain_email_header_is_never_trusted():
    with pytest.raises(AuthError):
        IapAuthenticator("aud", verify=verifier({"iss": IAP_ISSUER, "email": "a@b.c"})).email({"x-goog-authenticated-user-email": "accounts.google.com:a@b.c"})


def test_iap_needs_an_audience():
    with pytest.raises(ValueError, match="IAP_AUDIENCE"):
        IapAuthenticator("")


def test_dev_auth_is_one_fixed_user_and_refused_on_cloud_run(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert DevAuthenticator(" Dev@Example.com ").email({}) == "dev@example.com"
    monkeypatch.setenv("K_SERVICE", "pinboard-web")
    with pytest.raises(RuntimeError, match="refused on Cloud Run"):
        DevAuthenticator("dev@example.com")


# ── reading the engine's errors ──────────────────────────────────────────

def google_error(code, status, detail):
    """Exactly the shape the real platform returned for a container error (captured 2026-10-09)."""
    return json.dumps({"error": {"code": code, "status": status, "message":
        'Reasoning Engine Execution failed.\nPlease refer to our documentation (https://cloud.google.com/...) for checking logs and other '
        f'troubleshooting tips.\nError Details: {json.dumps({"detail": detail})}'}})


@pytest.mark.parametrize("code, status, detail", [
    (404, "NOT_FOUND", "session not found"),
    (403, "PERMISSION_DENIED", "Blocked by policy (user.not_allowed): this account is not allowed to use the assistant"),
    (400, "INVALID_ARGUMENT", "'limit' must be a whole number between 1 and 100"),
    (409, "ALREADY_EXISTS", "this session already has a turn in progress or awaiting approval"),
    (429, "RESOURCE_EXHAUSTED", "too many turns are running"),
])
def test_the_engines_status_and_reason_survive_the_platforms_wrapper(code, status, detail):
    err = parse_error(code, google_error(code, status, detail))
    assert (err.status, err.message) == (code, detail)


def test_the_engine_answering_directly_is_read_too_including_validation_lists():
    assert parse_error(404, json.dumps({"detail": "session not found"})).message == "session not found"
    err = parse_error(422, json.dumps({"detail": [{"msg": "Field required"}, {"msg": "too long"}]}))
    assert (err.status, err.message) == (422, "Field required; too long")


@pytest.mark.parametrize("code", [401, 500, 502, 503, 504])
def test_our_own_credential_and_server_problems_are_never_shown_as_the_users_fault(code):
    err = parse_error(code, google_error(code, "X", "details we must not leak: stack trace..."))
    assert err.status == 502 and err.message == "the assistant is unavailable" and "stack" not in err.message


def test_unreadable_bodies_still_give_a_sensible_error():
    assert parse_error(404, "<html>nope</html>").message == "not found"
    assert parse_error(500, "").status == 502


# ── the Google client (a fake session stands in for the network) ─────────

class Resp:
    def __init__(self, status=200, body="", lines=()):
        self.status_code, self.text, self._lines, self.closed = status, body, list(lines), False

    def json(self):
        return json.loads(self.text)

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, resp=None, error=None):
        self.resp, self.error, self.posts = resp, error, []

    def post(self, url, **kw):
        self.posts.append((url, kw))
        if self.error:
            raise self.error
        return self.resp


def client(session):
    return GoogleRuntimeClient("projects/1/locations/us-central1/reasoningEngines/9", "us-central1", session_factory=lambda: session)


def test_a_call_posts_the_class_method_and_returns_the_output():
    s = FakeSession(Resp(body=json.dumps({"output": {"ok": 1}})))
    assert client(s).call("list_models", {"user_id": "u"}) == {"ok": 1}
    url, kw = s.posts[0]
    assert url == "https://us-central1-aiplatform.googleapis.com/v1/projects/1/locations/us-central1/reasoningEngines/9:query"
    assert kw["json"] == {"classMethod": "list_models", "input": {"user_id": "u"}}


def test_failed_calls_become_runtime_errors_with_the_right_status():
    import requests

    with pytest.raises(RuntimeCallError) as e:
        client(FakeSession(Resp(404, google_error(404, "NOT_FOUND", "session not found")))).call("get_session", {})
    assert (e.value.status, e.value.message) == (404, "session not found")
    with pytest.raises(RuntimeCallError) as e:
        client(FakeSession(error=requests.Timeout())).call("get_session", {})
    assert e.value.status == 504
    with pytest.raises(RuntimeCallError) as e:
        client(FakeSession(error=requests.ConnectionError())).call("get_session", {})
    assert e.value.status == 502


def test_a_stream_yields_the_outputs_closes_the_response_and_reports_in_band_errors():
    lines = [json.dumps({"output": {"seq": 1, "type": "token"}}), "", json.dumps({"output": {"seq": 2, "type": "turn_done"}})]
    resp = Resp(lines=lines)
    assert [e["seq"] for e in client(FakeSession(resp)).stream("stream_turn", {})] == [1, 2] and resp.closed
    broken = Resp(lines=[json.dumps({"output": {"seq": 1}}), json.dumps({"error": "boom"})])
    got = []
    with pytest.raises(RuntimeCallError) as e:
        for item in client(FakeSession(broken)).stream("stream_turn", {}):
            got.append(item)
    assert got == [{"seq": 1}] and e.value.status == 502 and broken.closed and "boom" not in e.value.message
    with pytest.raises(RuntimeCallError) as e:
        list(client(FakeSession(Resp(404, google_error(404, "NOT_FOUND", "turn not found")))).stream("stream_turn", {}))
    assert e.value.status == 404


def test_a_refused_token_logs_what_audience_it_claimed_but_never_trusts_it(caplog):
    import base64
    import logging

    def jwt(payload):
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
        return f"{enc({'alg': 'ES256'})}.{enc(payload)}.sig"

    forged = jwt({"aud": "/projects/1/locations/r/services/other", "iss": IAP_ISSUER, "email": "attacker@example.com"})
    with caplog.at_level(logging.WARNING, logger="pinboard.web.auth"), pytest.raises(AuthError):
        IapAuthenticator("/projects/1/locations/r/services/mine", verify=verifier(error=ValueError("audience mismatch"))).email({IAP_HEADER: forged})
    assert "aud=/projects/1/locations/r/services/other" in caplog.text and "expects aud=/projects/1/locations/r/services/mine" in caplog.text
    with pytest.raises(AuthError):  # and garbage that is not even a token must not crash the logging
        IapAuthenticator("aud", verify=verifier(error=ValueError("x"))).email({IAP_HEADER: "not-a-jwt"})


# ── Sign in with Google (ID token -> signed session cookie) ──────────────

SECRET = "s" * 40
CLIENT = "1234-abc.apps.googleusercontent.com"


def google(claims=None, error=None, now=None, **kw):
    clock = now or (lambda: 1_000_000.0)
    return GoogleAuthenticator(CLIENT, SECRET, verify=verifier(claims, error), clock=clock, **kw)


GOOD = {"iss": "https://accounts.google.com", "email": "Me@Example.com", "email_verified": True}


def test_a_google_token_is_verified_against_our_client_id_and_becomes_a_cookie_that_signs_you_in():
    a = google(GOOD)
    email, cookie = a.login("id-token")
    assert email == "me@example.com" and a.email({"cookie": f"{a.cookie_name}={cookie}"}) == "me@example.com"
    assert a.email({"cookie": f"theme=dark; {a.cookie_name}={cookie}; other=1"}) == "me@example.com"
    assert a.public_config() == {"mode": "google", "client_id": CLIENT} and "accounts.google.com" in GOOD["iss"]


@pytest.mark.parametrize("claims, error", [
    (None, ValueError("wrong audience")),                                                                  # not our client id / forged / expired
    ({**GOOD, "iss": "https://evil.example.com"}, None), ({**GOOD, "email_verified": False}, None), ({**GOOD, "email_verified": "true"}, None),
    ({"iss": "accounts.google.com", "email_verified": True}, None), ({**GOOD, "email": "  "}, None),
])
def test_a_token_that_does_not_check_out_never_gets_a_cookie(claims, error):
    with pytest.raises(AuthError):
        google(claims, error).login("id-token")


def test_both_google_issuer_spellings_are_accepted():
    for iss in ("accounts.google.com", "https://accounts.google.com"):
        assert google({**GOOD, "iss": iss}).login("t")[0] == "me@example.com"


def test_the_session_cookie_cannot_be_forged_edited_or_used_after_it_expires():
    t = [1_000_000.0]
    a = google(GOOD, now=lambda: t[0], lifetime=3600)
    _, cookie = a.login("t")
    name = a.cookie_name
    payload, sig = cookie.split(".")
    import base64

    forged = base64.urlsafe_b64encode(json.dumps({"e": "admin@example.com", "x": 9_999_999_999, "n": "00"}).encode()).decode().rstrip("=")
    other = google(GOOD, now=lambda: t[0])
    other._secret = b"x" * 40
    for bad in (f"{forged}.{sig}", f"{payload}.{sig[:-2]}AA", f"{payload}.", payload, "", "garbage.garbage", other.make_cookie("me@example.com")):
        with pytest.raises(AuthError):
            a.email({"cookie": f"{name}={bad}"})
    with pytest.raises(AuthError):
        a.email({})
    with pytest.raises(AuthError):
        a.email({"cookie": "not a cookie ;;; ==="})
    t[0] += 3599
    assert a.email({"cookie": f"{name}={cookie}"}) == "me@example.com"
    t[0] += 2
    with pytest.raises(AuthError, match="expired"):
        a.email({"cookie": f"{name}={cookie}"})


def test_the_google_authenticator_needs_a_client_id_and_a_real_secret():
    for client, secret in (("", SECRET), ("  ", SECRET), (CLIENT, ""), (CLIENT, "short")):
        with pytest.raises(ValueError):
            GoogleAuthenticator(client, secret)


def test_main_builds_each_auth_mode_from_the_environment(monkeypatch):
    from pinboard.web.main import authenticator_from_env

    monkeypatch.setenv("WEB_AUTH", "google")
    for var in ("GOOGLE_OAUTH_CLIENT_ID", "SESSION_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit):
        authenticator_from_env()
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", CLIENT)
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    assert isinstance(authenticator_from_env(), GoogleAuthenticator)
    monkeypatch.setenv("WEB_AUTH", "nonsense")
    with pytest.raises(SystemExit):
        authenticator_from_env()
