#!/bin/sh
# Start the Cloud SQL Auth Proxy in the background when SQL_CONNECTION is set, then the web server.
if [ -n "$SQL_CONNECTION" ]; then
  cloud-sql-proxy --address 127.0.0.1 --port 5432 "$SQL_CONNECTION" &
fi
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}"
