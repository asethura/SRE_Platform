"""
Validation Agent — confirms the fix actually worked.

Pass -> resolve incident, close ITSM ticket
Fail -> route BACK to diagnosis with execution context (gap #1 fix:
        validation reject is no longer a dead end)

Needs its own observability access (gap #4 fix) — same MCP toolbox as
diagnosis, smaller scope. Stubs here.
"""

from datetime import datetime, timezone

from db.models import AgentType, Incident, IncidentStatus
from .base import BaseAgent


def fetch_post_fix_metrics(service: str) -> dict:
    """Prometheus MCP in production. Simulates healthy post-fix state."""
    return {"cpu_pct": 41, "error_rate_5xx": 0.001,
            "pod_restarts_last_hour": 0, "p99_ms": 220,
            "observation_window_minutes": 15}


class ValidationAgent(BaseAgent):
    agent_type = AgentType.VALIDATION

    def system_prompt(self) -> str:
        return """You are the Validation agent in an SRE incident-automation platform.
Remediation just finished. Compare post-fix metrics against the incident's
original symptoms and the service health thresholds provided.

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
            "post_fix_metrics": fetch_post_fix_metrics(incident.service or ""),
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
