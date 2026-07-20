# Single-container bundle of nginx (public auth gate) + our own Cloud Trace
# MCP server (internal, port 9000) -- same pattern as cloudrun/confluence-mcp,
# which sidesteps a "Container import failed" error Cloud Run's multi-container
# sidecar path hit in this project.
FROM nginx:alpine
RUN apk add --no-cache python3 py3-pip && \
    python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir mcp google-auth requests
COPY server.py /app/server.py
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
