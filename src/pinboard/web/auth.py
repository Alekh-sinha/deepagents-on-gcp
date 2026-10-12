"""Who is calling. Three ways, chosen by ``WEB_AUTH``:

* ``iap``    Cloud Run sits behind IAP, which adds a signed JWT to every request; we VERIFY it (signature, audience, issuer) and take the email from it.
             The plain ``X-Goog-Authenticated-User-Email`` header is never trusted: it could be forged by anything that reaches the service without IAP.
* ``google`` "Sign in with Google": the browser gets a Google ID token, ``login`` verifies it (signature, audience = our OAuth client id, issuer, expiry,
             verified email) and hands back a signed, HttpOnly session cookie that the later requests carry. For a local or self-hosted app.
* ``dev``    one fixed user, local development only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
import time
from http.cookies import SimpleCookie
from typing import Callable, Mapping, Protocol

log = logging.getLogger(__name__)

IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"


class AuthError(Exception):
    pass


class Authenticator(Protocol):
    def email(self, headers: Mapping[str, str]) -> str: ...

    def public_config(self) -> dict: ...


def _verify_with_google(token: str, audience: str) -> dict:
    from google.auth.transport import requests as grequests
    from google.oauth2 import id_token

    return id_token.verify_token(token, grequests.Request(), audience=audience, certs_url=IAP_CERTS_URL)


def _unverified(token: str, *claims: str) -> list[str]:
    """For the log only: the named claims from a token WITHOUT verifying it (never used for any decision)."""
    try:
        body = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return [str(data.get(c))[:150] for c in claims]
    except Exception:  # noqa: BLE001
        return ["?"] * len(claims)


class IapAuthenticator:
    def __init__(self, audience: str, *, verify: Callable[[str, str], dict] | None = None):
        if not audience:
            raise ValueError("IAP_AUDIENCE is required (Cloud Run service > Security > IAP, or `gcloud iap settings get`)")
        self.audience, self._verify = audience, verify or _verify_with_google

    def email(self, headers: Mapping[str, str]) -> str:
        token = headers.get(IAP_HEADER)
        if not token:
            raise AuthError("missing IAP token: this service must be reached through IAP")
        try:
            claims = self._verify(token, self.audience)
        except Exception as exc:  # noqa: BLE001  any verification failure (signature, expiry, audience) is just "not signed in"
            log.warning("IAP token refused (%s: %s); the token's own, UNVERIFIED claims say aud=%s iss=%s; this service expects aud=%s",
                        type(exc).__name__, str(exc)[:120], *_unverified(token, "aud", "iss"), self.audience)
            raise AuthError("invalid IAP token") from exc
        if claims.get("iss") != IAP_ISSUER:
            raise AuthError("unexpected token issuer")
        email = str(claims.get("email") or "").strip().lower()
        if not email:
            raise AuthError("the token has no email")
        return email

    def public_config(self) -> dict:
        return {"mode": "iap"}


GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
SESSION_COOKIE = "privai_session"
SESSION_SECONDS = 12 * 3600


def _verify_google_id_token(token: str, client_id: str) -> dict:
    from google.auth.transport import requests as grequests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, grequests.Request(), client_id)   # signature, expiry and audience (= our client id)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class GoogleAuthenticator:
    """Sign in with Google. ``login(credential)`` -> (email, cookie value). ``email(headers)`` reads the cookie on later requests.
    The cookie is ``payload.signature`` (HMAC-SHA256 with ``secret``): it carries the email and an expiry, so nothing is stored on the server and a
    restart does not sign anyone out; changing the secret signs everyone out."""

    cookie_name, lifetime = SESSION_COOKIE, SESSION_SECONDS

    def __init__(self, client_id: str, secret: str, *, verify: Callable[[str, str], dict] | None = None, clock: Callable[[], float] = time.time,
                 lifetime: int = SESSION_SECONDS):
        if not client_id or not client_id.strip():
            raise ValueError("GOOGLE_OAUTH_CLIENT_ID is required (Cloud console > APIs & Services > Credentials > OAuth client ID > Web application)")
        if len(secret or "") < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters (try: openssl rand -hex 32)")
        self.client_id, self._secret, self._verify, self._clock, self.lifetime = client_id.strip(), secret.encode(), verify or _verify_google_id_token, clock, lifetime

    def public_config(self) -> dict:
        return {"mode": "google", "client_id": self.client_id}

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._secret, payload.encode(), hashlib.sha256).digest())

    def make_cookie(self, email: str) -> str:
        payload = _b64(json.dumps({"e": email, "x": int(self._clock()) + self.lifetime, "n": _secrets.token_hex(4)}, separators=(",", ":")).encode())
        return f"{payload}.{self._sign(payload)}"

    def login(self, credential: str) -> tuple[str, str]:
        try:
            claims = self._verify(credential, self.client_id)
        except Exception as exc:  # noqa: BLE001  any failure (signature, expiry, audience) is just "not signed in"
            log.warning("Google sign-in refused (%s: %s); the token's own, UNVERIFIED claims say aud=%s iss=%s; this app expects aud=%s",
                        type(exc).__name__, str(exc)[:120], *_unverified(credential, "aud", "iss"), self.client_id)
            raise AuthError("could not verify the Google sign-in") from exc
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise AuthError("unexpected token issuer")
        email = str(claims.get("email") or "").strip().lower()
        if not email or claims.get("email_verified") is not True:
            raise AuthError("the Google account has no verified email")
        return email, self.make_cookie(email)

    def email(self, headers: Mapping[str, str]) -> str:
        jar = SimpleCookie()
        try:
            jar.load(headers.get("cookie", ""))
        except Exception:  # noqa: BLE001
            raise AuthError("not signed in") from None
        morsel = jar.get(self.cookie_name)
        if morsel is None or "." not in morsel.value:
            raise AuthError("not signed in")
        payload, _, sig = morsel.value.partition(".")
        if not hmac.compare_digest(sig, self._sign(payload)):
            raise AuthError("invalid session")
        try:
            data = json.loads(_unb64(payload))
            email, expires = str(data["e"]), int(data["x"])
        except (ValueError, KeyError, TypeError):
            raise AuthError("invalid session") from None
        if self._clock() >= expires:
            raise AuthError("the session has expired; sign in again")
        return email


class DevAuthenticator:
    """Local development only: everybody is one fixed user. Refuses to exist on Cloud Run."""

    def __init__(self, email: str):
        if os.environ.get("K_SERVICE"):
            raise RuntimeError("WEB_AUTH=dev is for local development and is refused on Cloud Run")
        self._email = email.strip().lower()

    def email(self, headers: Mapping[str, str]) -> str:
        return self._email

    def public_config(self) -> dict:
        return {"mode": "dev", "email": self._email}
