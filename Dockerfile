# Container for the pinboard service on Agent Runtime (also runs locally or on Cloud Run).
# Listens on 0.0.0.0:$PORT (8080), which is what the Agent Runtime container contract expects.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
RUN pip install --no-cache-dir uv && useradd --create-home --uid 10001 app

# Cloud SQL Auth Proxy: lets psycopg (which the Cloud SQL Python Connector does not support) reach the database on localhost.
ADD https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.18.0/cloud-sql-proxy.linux.amd64 /usr/local/bin/cloud-sql-proxy
RUN chmod 0755 /usr/local/bin/cloud-sql-proxy

WORKDIR /app
# Dependencies first (cached unless the lockfile changes), exactly as locked.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
# Default skills; in the cloud SKILLS_SOURCE may instead point at gs://bucket/skills.
COPY skills_example ./skills_example
COPY deploy/start.sh ./start.sh
# Optional extra root CAs (the Agent Gateway's, when egress goes through it). Python's HTTP clients read the system bundle via these variables.
COPY deploy/extra-ca /tmp/extra-ca
RUN for f in /tmp/extra-ca/*.crt; do [ -e "$f" ] && cp "$f" /usr/local/share/ca-certificates/; done; update-ca-certificates; rm -rf /tmp/extra-ca
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/etc/ssl/certs/ca-certificates.crt
RUN uv sync --frozen --no-dev && chmod +x start.sh && chown -R app:app /app

USER app
ENV PINBOARD_POLICY=/app/config/policy.yaml SKILLS_SOURCE=/app/skills_example
EXPOSE 8080
CMD ["./start.sh"]
