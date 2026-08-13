"""
Playbook MCP server — discovery-only gateway over the `playbooks` DB table.

Exposes list_playbooks/describe_playbook as MCP tools so RemediationAgent's
planning phase can discover what's available live, the same way it already
discovers Confluence/Prometheus/logs/traces. Deliberately READ-ONLY: there
is no tool here that executes a playbook -- that stays a plain (non-MCP)
REST call from agents/remediation.py's execute_playbook() straight to
playbook-server, never reachable through the LLM tool-use loop
(_call_with_mcp_tools() in agents/base.py executes ANY tool_use block the
model emits, so anything exposed here must be safe to actually call).

Runs behind nginx's bearer gate (see default.conf.template); this process
has no auth of its own and must never be reachable directly.

Reads the `playbooks` table directly (not a proxy to playbook-server) so
there is exactly one source of truth for the catalog -- the same table
RemediationAgent.build_context()/_apply_planning() already query.
"""

import os
import sys

# Build context is the repo root (unlike the other cloudrun/*-mcp/ servers,
# which are self-contained) specifically so this can import db.models
# directly instead of keeping a second copy of the catalog. See Dockerfile.
sys.path.insert(0, "/app")

from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Playbook

DATABASE_URL = os.environ["DATABASE_URL"]
_engine = create_engine(DATABASE_URL, echo=False)
_Session = sessionmaker(bind=_engine)

mcp = FastMCP("playbook", host="127.0.0.1", port=9000)


@mcp.tool()
def list_playbooks() -> list[dict]:
    """List active playbooks available for remediation. Returns id, name,
    description, and risk_tier for each -- call describe_playbook(id) for
    the full params_schema before proposing a step that uses one."""
    db = _Session()
    try:
        rows = db.query(Playbook).filter(Playbook.active == True).all()
        return [
            {"id": pb.id, "name": pb.name, "description": pb.description,
             "risk_tier": pb.risk_tier.value if pb.risk_tier else None}
            for pb in rows
        ]
    finally:
        db.close()


@mcp.tool()
def describe_playbook(playbook_id: str) -> dict:
    """Full detail for one playbook: params_schema (exact params to
    supply), rollback_playbook_id (if any), and risk_tier. Returns {} if
    the id doesn't exist or isn't active."""
    db = _Session()
    try:
        pb = db.get(Playbook, playbook_id)
        if pb is None or not pb.active:
            return {}
        return {
            "id": pb.id, "name": pb.name, "description": pb.description,
            "params_schema": pb.params_schema,
            "rollback_playbook_id": pb.rollback_playbook_id,
            "risk_tier": pb.risk_tier.value if pb.risk_tier else None,
        }
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
