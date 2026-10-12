# The web backend (Cloud Run behind IAP): `pinboard web`. Build:  gcloud builds submit . --config deploy/cloudbuild-web.yaml --substitutions _IMAGE=...
# Same Python package as the engine, a different command. Runs as a non-root user; no .env, no keys, no sandbox code paths are used.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
RUN pip install --no-cache-dir uv && useradd --create-home --uid 10001 app
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev && chown -R app:app /app
USER app
EXPOSE 8080
CMD ["pinboard", "web"]
