# Single-container bundle of nginx (public auth gate) + mcp-atlassian
# (internal, port 9000). Combined into one image so Cloud Run only ever has
# to import/run a single container -- the multi-container "sidecar" YAML
# path hit an opaque "Container import failed" error in this project with
# no actionable detail, so this sidesteps it entirely.
FROM nginx:alpine
RUN apk add --no-cache python3 py3-pip && \
    python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir mcp-atlassian
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
