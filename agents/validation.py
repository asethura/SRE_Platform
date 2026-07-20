"""
Validation Agent — confirms the fix actually worked.

Pass -> resolve incident, close ITSM ticket
Fail -> route BACK to diagnosis with execution context (gap #1 fix:
        validation reject is no longer a dead end)

Reads post-fix metrics live from Google Managed Prometheus via the MCP
connector (gap #4 fix) — mcp_servers() gives the model query/query_range/
list_metric_names tools, same shape as Confluence for triage/diagnosis.
"""

from datetime import datetime, timezone

from db.models import AgentType, Incident, IncidentStatus
from .base import BaseAgent, prometheus_mcp_server


class ValidationAgent(BaseAgent):
    agent_type = AgentType.VALIDATION

    def mcp_servers(self) -> list[dict]:
        return [prometheus_mcp_server()]

    def system_prompt(self) -> str:
        return """You are the Validation agent in an SRE incident-automation platform.
Remediation just finished. Use your Prometheus tools to query post-fix metrics
for this incident's service and compare them against its original symptoms and
the health thresholds provided. Call list_metric_names first if you're unsure
what's available, then query/query_range for the specific signals relevant to
the original symptom (e.g. CPU, 5xx rate, restart count, p99 latency) over the
observation window.

Rules:
- Declare "pass" only if the ORIGINAL symptom is gone AND no new symptom appeared.
- Respect the observation window: if metrics are healthy but the window is
  shorter than min_observation_minutes, declare "insufficient_observation".
- Any regression or new anomaly -> "fail".

Respond ONLY with JSON:
{
  "result": "pass" | "fail" | "insufficient_observation",
  "checks": [{"metric": "...", "value": ..., "threshold": ..., "ok": true|false}],
  "summary": "one sentence"
}"""

    def build_context(self, db, incident: Incident) -> dict:
        return {
            "incident": {"id": incident.id, "title": incident.title,
                         "description": incident.description,
                         "service": incident.service},
            "health_thresholds": {
                "error_rate_5xx_max": 0.01,
                "cpu_pct_max": 80,
                "p99_ms_max": 1000,
                "min_observation_minutes": 10,
            },
        }

    def apply_output(self, db, incident: Incident, output: dict) -> None:
        if output["result"] == "pass":
            incident.status = IncidentStatus.RESOLVED
            incident.resolved_at = datetime.now(timezone.utc)
            # -> ITSM close + KB update happen in the feedback/closure step
        elif output["result"] == "insufficient_observation":
            incident.status = IncidentStatus.VALIDATING  # re-check later
        else:
            # Fix didn't hold — back to diagnosis with fresh context.
            incident.status = IncidentStatus.DIAGNOSING
            incident.diagnosis_outcome = None
