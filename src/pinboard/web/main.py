"""Builds the web backend from environment variables (what Cloud Run sets) and runs it.

    WEB_AUTH=iap        IAP_AUDIENCE=<audience of the IAP-protected service>          (production; the default)
    WEB_AUTH=google     GOOGLE_OAUTH_CLIENT_ID=<web client id>  SESSION_SECRET=<32+ random characters>   (Sign in with Google; local or self-hosted)
    WEB_AUTH=dev        DEV_USER_EMAIL=you@example.com                                 (local development only; refused on Cloud Run)
    RUNTIME_RESOURCE=projects/N/locations/R/reasoningEngines/ID   RUNTIME_REGION=us-central1   (the deployed engine)
    RUNTIME_URL=http://127.0.0.1:8080  RUNTIME_TOKEN=<PINBOARD_API_TOKEN>               (a local `pinboard serve` instead)
    STATIC_DIR=web/dist  (the built React app, served at /; optional)
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from pinboard.web.app import create_app
from pinboard.web.auth import Authenticator, DevAuthenticator, GoogleAuthenticator, IapAuthenticator
from pinboard.web.runtime_client import GoogleRuntimeClient, HttpRuntimeClient, RuntimeClient


def _need(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} must be set (see pinboard/web/main.py)")
    return value


def authenticator_from_env() -> Authenticator:
    mode = os.environ.get("WEB_AUTH", "iap").strip().lower()
    if mode == "iap":
        return IapAuthenticator(_need("IAP_AUDIENCE"))
    if mode == "google":
        return GoogleAuthenticator(_need("GOOGLE_OAUTH_CLIENT_ID"), _need("SESSION_SECRET"))
    if mode == "dev":
        return DevAuthenticator(_need("DEV_USER_EMAIL"))
    raise SystemExit("WEB_AUTH must be 'iap', 'google' or 'dev'")


def runtime_from_env() -> RuntimeClient:
    if os.environ.get("RUNTIME_URL"):
        import httpx

        return HttpRuntimeClient(httpx.Client(base_url=_need("RUNTIME_URL"), timeout=httpx.Timeout(120.0, read=330.0)), _need("RUNTIME_TOKEN"))
    return GoogleRuntimeClient(_need("RUNTIME_RESOURCE"), os.environ.get("RUNTIME_REGION", "us-central1"))


def build_app() -> FastAPI:
    static = os.environ.get("STATIC_DIR", "web/dist")
    return create_app(runtime_from_env(), authenticator_from_env(), static_dir=static if Path(static).is_dir() else None)


def serve(host: str = "0.0.0.0", port: int | None = None) -> None:
    import uvicorn

    uvicorn.run(build_app(), host=host, port=port or int(os.environ.get("PORT", "8080")), log_level="info")
