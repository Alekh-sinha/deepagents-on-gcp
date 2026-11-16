#!/bin/sh
# Container entrypoint. With SQL_CONNECTION set (PROJECT:REGION:INSTANCE) the Cloud SQL Auth Proxy runs beside the app so the app
# can reach the database on 127.0.0.1:5432 with an ordinary Postgres driver. The app opens the database while starting, so wait for the proxy first.
set -e
if [ -n "$SQL_CONNECTION" ]; then
  proxy_port="${SQL_PROXY_PORT:-5432}"  # the app's DATABASE_URL must use the same port
  # SQL_PRIVATE_IP=true: the database has only a private address (reached through the VPC attachment, see docs/EGRESS.md)
  if [ -n "$SQL_PRIVATE_IP" ]; then private_ip="--private-ip"; else private_ip=""; fi
  # shellcheck disable=SC2086  # $private_ip is intentionally empty or a single flag
  cloud-sql-proxy --address 127.0.0.1 --port "$proxy_port" $private_ip "$SQL_CONNECTION" &
  i=0
  until python -c "import socket; socket.create_connection(('127.0.0.1', $proxy_port), 1).close()" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
      echo "start.sh: the Cloud SQL proxy did not start listening within 60 s (SQL_CONNECTION=$SQL_CONNECTION)" >&2
      exit 1
    fi
    sleep 1
  done
  echo "start.sh: Cloud SQL proxy is ready"
fi
exec pinboard serve --host 0.0.0.0 --port "${PORT:-8080}"
