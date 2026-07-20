"""
Cloud Trace MCP server — the "Tempo" equivalent (gap in diagnosis.py's
fetch_traces stub). No self-hosted Tempo/Jaeger exists here; Cloud Trace is
already enabled project-wide, so this queries that directly (whether any
service currently emits traces into it is a separate question from whether
this tool can read what's there).

Runs behind nginx's bearer gate (see default.conf.template); this process
has no auth of its own and must never be reachable directly. Auth to Cloud
Trace is via THIS container's own Cloud Run service account (Application
Default Credentials).
"""

import os

import google.auth
import google.auth.transport.requests
import requests
from mcp.server.fastmcp import FastMCP

PROJECT_ID = os.environ["GCP_PROJECT_ID"]
TRACE_BASE = f"https://cloudtrace.googleapis.com/v1/projects/{PROJECT_ID}/traces"
SCOPES = ["https://www.googleapis.com/auth/trace.readonly"]

_credentials, _ = google.auth.default(scopes=SCOPES)


def _access_token() -> str:
    if not _credentials.valid:
        _credentials.refresh(google.auth.transport.requests.Request())
    return _credentials.token


def _get(url: str, params: dict) -> dict:
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {_access_token()}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


mcp = FastMCP("trace", host="127.0.0.1", port=9000)


@mcp.tool()
def list_traces(start_time: str, end_time: str, filter: str = "", page_size: int = 20) -> dict:
    """List recent traces in a time window.

    start_time/end_time: RFC3339 timestamps.
    filter: optional Cloud Trace filter, e.g. 'root:checkoutservice' or
      a minimum latency like 'span.latency:100ms' (only spans slower than
      100ms). Leave empty to list all traces in the window.
    """
    params = {"startTime": start_time, "endTime": end_time, "pageSize": page_size}
    if filter:
        params["filter"] = filter
    return _get(TRACE_BASE, params)


@mcp.tool()
def get_trace(trace_id: str) -> dict:
    """Fetch one trace's full span tree by its trace ID (from list_traces)."""
    return _get(f"{TRACE_BASE}/{trace_id}", {})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
