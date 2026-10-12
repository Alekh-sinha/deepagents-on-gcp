#!/usr/bin/env bash
# Run the privai web backend (talks to the cloud engine as you) and the frontend on this computer.
#   scripts/run_local_ui.sh start | stop | status | logs
# Settings come from .env.local-ui (git-ignored): WEB_AUTH, GOOGLE_OAUTH_CLIENT_ID, SESSION_SECRET, RUNTIME_RESOURCE, RUNTIME_REGION.
set -euo pipefail
cd "$(dirname "$0")/.."
RUN="$PWD/.local-ui"; mkdir -p "$RUN"
BACKEND_PORT="${BACKEND_PORT:-8081}"; FRONTEND_PORT="${FRONTEND_PORT:-5173}"
alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
case "${1:-status}" in
  start)
    [ -f .env.local-ui ] || { echo "missing .env.local-ui (see this script's header)"; exit 2; }
    set -a; . ./.env.local-ui; set +a
    alive $RUN/backend.pid || { setsid nohup .venv/bin/python -c "from pinboard.web.main import serve; serve('127.0.0.1', $BACKEND_PORT)" > $RUN/backend.log 2>&1 & echo $! > $RUN/backend.pid; }
    alive $RUN/frontend.pid || { PRIVAI_BACKEND="http://127.0.0.1:$BACKEND_PORT" setsid nohup npm --prefix frontend run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" > $RUN/frontend.log 2>&1 & echo $! > $RUN/frontend.pid; }
    for i in $(seq 1 40); do curl -fs "localhost:$BACKEND_PORT/healthz" >/dev/null 2>&1 && curl -fs "localhost:$FRONTEND_PORT" >/dev/null 2>&1 && break; sleep 0.5; done
    "$0" status ;;
  stop)
    for f in backend frontend; do alive $RUN/$f.pid && kill "$(cat $RUN/$f.pid)" 2>/dev/null || true; rm -f $RUN/$f.pid; done
    pkill -f "vite --host 127.0.0.1 --port $FRONTEND_PORT" 2>/dev/null || true; echo stopped ;;
  status)
    for f in backend frontend; do alive $RUN/$f.pid && echo "$f: running (pid $(cat $RUN/$f.pid))" || echo "$f: not running"; done
    echo "backend  http://127.0.0.1:$BACKEND_PORT   frontend  http://localhost:$FRONTEND_PORT" ;;
  logs) tail -n 40 $RUN/backend.log $RUN/frontend.log ;;
esac
