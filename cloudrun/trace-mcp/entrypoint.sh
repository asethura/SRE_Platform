#!/bin/sh
set -e

# trace-mcp, internal only (localhost:9000) -- never exposed directly.
/opt/venv/bin/python3 /app/server.py &

# It takes a few seconds to import its dependencies and bind the port.
# nginx must not start accepting traffic before that or every early request
# 502s (nginx up, proxy target not listening yet).
echo "Waiting for trace-mcp to bind :9000..."
i=0
until /opt/venv/bin/python3 -c "import socket; socket.create_connection(('127.0.0.1', 9000), timeout=1)" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -ge 60 ]; then
        echo "trace-mcp did not bind :9000 within 60s -- starting nginx anyway" >&2
        break
    fi
    sleep 1
done
echo "trace-mcp ready after ${i}s, starting nginx"

# nginx's own entrypoint handles envsubst templating (MCP_AUTH_SECRET) then
# execs nginx in the foreground -- this is what Cloud Run health-checks.
exec /docker-entrypoint.sh nginx -g "daemon off;"
