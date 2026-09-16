# Single-container bundle of nginx (public auth gate) + our own Playbook
# discovery MCP server (internal, port 9000) -- same pattern as the other
# cloudrun/*-mcp/ servers. Unlike those, this one's build context is the
# REPO ROOT (not this directory) so it can import db.models directly and
# read the same `playbooks` table RemediationAgent validates against.
#
# Build from repo root:  docker build -f playbook-mcp/Dockerfile -t playbook-mcp .
FROM nginx:alpine
ENV PYTHONUNBUFFERED=1
RUN apk add --no-cache python3 py3-pip && \
    python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir "mcp==1.28.1" sqlalchemy "psycopg[binary]"
COPY playbook-mcp/server.py /app/server.py
COPY db/ /app/db/
COPY playbook-mcp/default.conf.template /etc/nginx/templates/default.conf.template
COPY playbook-mcp/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
