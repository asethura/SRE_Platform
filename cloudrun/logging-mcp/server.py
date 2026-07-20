"""
Cloud Logging MCP server — the "ELK" equivalent (gap in diagnosis.py's
fetch_logs stub). No self-hosted ELK stack exists here; GKE's built-in
Cloud Logging already collects every pod's stdout/stderr (WORKLOADS
component logging is enabled on this cluster), so this queries that
directly instead of standing up a redundant pipeline.

Runs behind nginx's bearer gate (see default.conf.template); this process
has no auth of its own and must never be reachable directly. Auth to Cloud
Logging is via THIS container's own Cloud Run service account (Application
Default Credentials).
"""

import os

import google.auth
import google.auth.transport.requests
import requests
from mcp.server.fastmcp import FastMCP

PROJECT_ID = os.environ["GCP_PROJECT_ID"]
ENTRIES_LIST_URL = "https://logging.googleapis.com/v2/entries:list"
SCOPES = ["https://www.googleapis.com/auth/logging.read"]

_credentials, _ = google.auth.default(scopes=SCOPES)


def _access_token() -> str:
    if not _credentials.valid:
        _credentials.refresh(google.auth.transport.requests.Request())
    return _credentials.token


mcp = FastMCP("logging", host="127.0.0.1", port=9000)


@mcp.tool()
def search_logs(filter: str, page_size: int = 50, order_by: str = "timestamp desc") -> dict:
    """Search GKE pod logs via Cloud Logging's filter query language.

    filter: a Cloud Logging query, e.g.
      'resource.type="k8s_container" AND resource.labels.namespace_name="default" '
      'AND resource.labels.container_name="paymentservice" AND severity>=ERROR'
    Combine with timestamp constraints for a window, e.g.
      'timestamp>="2026-07-19T00:00:00Z"'.
    order_by: 'timestamp desc' (newest first, default) or 'timestamp asc'.
    """
    resp = requests.post(
        ENTRIES_LIST_URL,
        headers={"Authorization": f"Bearer {_access_token()}"},
        json={
            "resourceNames": [f"projects/{PROJECT_ID}"],
            "filter": filter,
            "orderBy": order_by,
            "pageSize": page_size,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    entries = [
        {
            "timestamp": e.get("timestamp"),
            "severity": e.get("severity"),
            "resource": (e.get("resource") or {}).get("labels"),
            "text": e.get("textPayload")
            or (e.get("jsonPayload") or {}).get("message")
            or e.get("jsonPayload"),
        }
        for e in data.get("entries", [])
    ]
    return {"entries": entries, "count": len(entries)}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
