#!/usr/bin/env bash
# wrapper: runs spike.py in a throwaway environment with exactly the packages Google's page lists (your project's venv is not touched)
cd "$(dirname "$0")/../../.." || exit 1
exec uv run --isolated --no-project --python 3.12 \
  --with "google-cloud-aiplatform[agent_engines,langchain]==2.4.0" --with langchain-google-cloud-sql-pg --with langchain-google-vertexai --with langgraph \
  python spikes/gateway/doc_agent/spike.py "$@"
