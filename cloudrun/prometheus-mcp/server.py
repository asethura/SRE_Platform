"""
Prometheus MCP server — exposes Google Managed Prometheus (GMP) metrics to
agents over the MCP streamable-http transport. Runs behind nginx's bearer
gate (see default.conf.template); this process has no auth of its own and
must never be reachable directly.

Auth to GMP is via THIS container's own Cloud Run service account
(Application Default Credentials) — separate from the incoming request's
bearer token, which is only the gate secret checked by nginx. GMP exposes a
Prometheus-compatible query API on Cloud Monitoring:
https://cloud.google.com/stackdriver/docs/managed-prometheus/query
"""

import os

import google.auth
import google.auth.transport.requests
import requests
from mcp.server.fastmcp import FastMCP

PROJECT_ID = os.environ["GMP_PROJECT_ID"]
QUERY_BASE = (
    f"https://monitoring.googleapis.com/v1/projects/{PROJECT_ID}"
    "/location/global/prometheus/api/v1"
)
SCOPES = ["https://www.googleapis.com/auth/monitoring.read"]

_credentials, _ = google.auth.default(scopes=SCOPES)


def _access_token() -> str:
    if not _credentials.valid:
        _credentials.refresh(google.auth.transport.requests.Request())
    return _credentials.token


def _get(path: str, params: dict) -> dict:
    resp = requests.get(
        f"{QUERY_BASE}/{path}",
        headers={"Authorization": f"Bearer {_access_token()}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


mcp = FastMCP("prometheus", host="127.0.0.1", port=9000)


@mcp.tool()
def query(promql: str, time: str = "") -> dict:
    """Run an instant PromQL query against Google Managed Prometheus.

    promql: a PromQL expression, e.g.
      'rate(kubernetes_io:container_cpu_core_usage_time{namespace="default"}[5m])'
    time: optional RFC3339 timestamp to evaluate at (default: now).
    Call list_metric_names first if you're not sure what's available.
    """
    params = {"query": promql}
    if time:
        params["time"] = time
    return _get("query", params)


@mcp.tool()
def query_range(promql: str, start: str, end: str, step: str = "60s") -> dict:
    """Run a PromQL range query against Google Managed Prometheus.

    start/end: RFC3339 timestamps (e.g. incident start -> now).
    step: resolution, e.g. '60s', '5m'.
    """
    return _get(
        "query_range",
        {"query": promql, "start": start, "end": end, "step": step},
    )


@mcp.tool()
def list_metric_names(match: str = "") -> dict:
    """List available Prometheus metric names, optionally filtered by a
    substring (e.g. match='cpu' or match='<service-name>')."""
    data = _get("label/__name__/values", {})
    if match:
        data["data"] = [name for name in data.get("data", []) if match in name]
    return data


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
