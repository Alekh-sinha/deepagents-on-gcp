"""The model pool: ids, ranks and where each model lives, loaded from models.yaml; plus the middleware that swaps the model of a graph
per call from its run context (the same idea as pinboard.agent.models.ModelSelectionMiddleware, but driven by this pool)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypedDict

import yaml
from langchain.agents.middleware import AgentMiddleware

HERE = Path(__file__).parent


class RunContext(TypedDict, total=False):
    model: str


@dataclass(frozen=True)
class PoolModel:
    id: str
    provider: str
    rank: int
    location: str = "us-central1"
    label: str = ""
    use_for: str = ""


class PoolError(ValueError):
    pass


@dataclass
class ModelPool:
    models: tuple[PoolModel, ...]
    default: str
    enforce_ceiling: bool = True
    factory: Callable[[PoolModel], Any] | None = None  # builds a chat model; injectable so tests need no network
    _cache: dict = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, path: str | Path = HERE / "models.yaml", factory=None) -> "ModelPool":
        data = yaml.safe_load(Path(path).read_text()) or {}
        models = tuple(PoolModel(**m) for m in data.get("models") or [])
        if not models:
            raise PoolError("models.yaml lists no models")
        ids = [m.id for m in models]
        if len(set(ids)) != len(ids):
            raise PoolError("duplicate model ids")
        if data.get("default") not in ids:
            raise PoolError(f"default {data.get('default')!r} is not in the pool")
        if any(not isinstance(m.rank, int) or isinstance(m.rank, bool) or m.rank < 1 for m in models):
            raise PoolError("every model needs a whole-number rank of at least 1")
        return cls(models, data["default"], bool(data.get("enforce_ceiling", True)), factory)

    @property
    def ids(self) -> list[str]:
        return [m.id for m in self.models]

    def spec(self, model_id: str | None) -> PoolModel:
        wanted = model_id or self.default
        for m in self.models:
            if m.id == wanted:
                return m
        raise PoolError(f"{wanted!r} is not in the model pool ({', '.join(self.ids)})")

    def allowed_for(self, ceiling_id: str | None) -> list[PoolModel]:
        """The models a helper may use when the user selected ``ceiling_id``: same rank or lower (everything when the ceiling is switched off)."""
        top = self.spec(ceiling_id).rank
        pick = [m for m in self.models if not self.enforce_ceiling or m.rank <= top]
        return sorted(pick, key=lambda m: m.rank)

    def get(self, model_id: str | None = None):
        spec = self.spec(model_id)
        with self._lock:
            if spec.id not in self._cache:
                self._cache[spec.id] = (self.factory or vertex_factory)(spec)
            return self._cache[spec.id]


def vertex_factory(spec: PoolModel):
    """A Gemini model on Vertex AI through Application Default Credentials (no API key)."""
    import os

    from langchain_google_genai import ChatGoogleGenerativeAI

    project = os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise PoolError("set GCP_PROJECT to the Google Cloud project to use")
    return ChatGoogleGenerativeAI(model=spec.id, vertexai=True, project=project, location=spec.location, timeout=120, max_retries=2)


def context_model(runtime: Any) -> str | None:
    ctx = getattr(runtime, "context", None)
    return ctx.get("model") if isinstance(ctx, dict) else getattr(ctx, "model", None)


class PoolSelectionMiddleware(AgentMiddleware):
    """Use the model named in the run context (``context={"model": id}``) for this graph's model calls; no context = the graph's own model."""

    def __init__(self, pool: ModelPool):
        super().__init__()
        self.pool = pool

    def _pick(self, request):
        wanted = context_model(request.runtime)
        return request.override(model=self.pool.get(wanted)) if wanted else request

    def wrap_model_call(self, request, handler):
        return handler(self._pick(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._pick(request))
