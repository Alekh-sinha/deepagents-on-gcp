"""Model selection: build chat models from the policy allowlist and switch per request.

The caller picks a model id (validated against policy). It travels as run-scoped ``context={"model": id}`` and a
small middleware swaps the model for that request. The agent is built once with the policy default.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Awaitable, Callable, TypedDict

from langchain.agents.middleware import AgentMiddleware
from langchain.chat_models import init_chat_model

from pinboard.core.policy import ModelSpec, Policy

PROVIDER_KEYS = {
    "google_genai": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


class RunContext(TypedDict, total=False):
    """Immutable per-run context passed as ``agent.invoke(..., context=...)``."""

    model: str


def build_chat_model(settings, vault, provider: str, name: str, location: str = ""):
    """Gemini via Vertex AI (ADC / service account, no API key) or any init_chat_model provider.
    ``location``: where this model is served (the newer Gemini models exist only in "global"); empty = MODEL_LOCATION, then the deployment region."""
    if provider == "google_genai" and settings.google_use_vertex:
        if not settings.gcp_project:
            raise ValueError("GCP_PROJECT must be set when GOOGLE_USE_VERTEX=true")
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=name,
            vertexai=True,
            project=settings.gcp_project,
            location=location or settings.model_location or settings.gcp_location,
            timeout=settings.model_timeout_seconds,  # a real run showed a 165 s stall inside a model call: bound it, and fail loudly instead of hanging
            max_retries=settings.model_max_retries,
        )
    key_name = PROVIDER_KEYS.get(provider)
    if key_name:
        os.environ[key_name] = vault.get(key_name, required=True)  # the agent process is trusted; the sandbox never sees this
    return init_chat_model(f"{provider}:{name}")


class ModelRouter:
    """Policy-validated, cached chat models. ``factory(spec)`` builds one (injectable for tests)."""

    def __init__(self, policy: Policy, factory: Callable[[ModelSpec], Any]):
        self.policy = policy
        self._factory = factory
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def default_id(self) -> str:
        return self.policy.default_model

    def resolve(self, model_id: str | None) -> ModelSpec:
        """Validate against the allowlist (raises PolicyDenied); None means the default."""
        return self.policy.resolve_model(model_id)

    def get(self, model_id: str | None = None):
        spec = self.resolve(model_id)
        with self._lock:
            if spec.id not in self._cache:
                self._cache[spec.id] = self._factory(spec)
            return self._cache[spec.id]


def _context_model(runtime: Any) -> str | None:
    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, dict):
        return ctx.get("model")
    return getattr(ctx, "model", None)


class ModelSelectionMiddleware(AgentMiddleware):
    """Swap the model for this request when ``context["model"]`` names an allowed model other than the default."""

    def __init__(self, router: ModelRouter):
        super().__init__()
        self.router = router

    def _pick(self, request):
        wanted = _context_model(request.runtime)
        if not wanted or wanted == self.router.default_id:
            return request
        return request.override(model=self.router.get(wanted))  # PolicyDenied for anything not on the allowlist

    def wrap_model_call(self, request, handler):
        return handler(self._pick(request))

    async def awrap_model_call(self, request, handler: Callable[[Any], Awaitable[Any]]):
        return await handler(self._pick(request))
