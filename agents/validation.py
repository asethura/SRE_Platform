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
from integrations.itsm import ITSMClient, NullITSMClient
from .base import BaseAgent, prometheus_mcp_server


class ValidationAgent(BaseAgent):
    agent_type = AgentType.VALIDATION

    def __init__(self, session_factory, instance_id: str = None,
                 itsm_client: ITSMClient = None):
        super().__init__(session_factory, instance_id)
        self.itsm_client = itsm_client or NullITSMClient()

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

You are validating the SERVICE, not any one pod instance. Remediation
(rollback, restart, rescale, reschedule) routinely replaces pods, so a pod
that existed when the incident was opened may already be gone — that is
expected and not itself a failure. Concretely:
- Query aggregated by service/deployment label (e.g. sum() over
  kube_deployment_status_replicas_available/unavailable, or a rate() summed
  across current pods for the deployment), not a specific pod name carried
  over from the incident's description or root cause.
- If you do look at pod-level series, first confirm via kube_pod_info (or
  equivalent) that the pod is part of the CURRENT pod set for this
  deployment. A series for a pod that no longer exists is stale leftover
  data (Prometheus keeps recently-terminated series queryable for a
  retention window) — disregard it rather than treat it as still failing.
- The question is always "is the symptom gone for the service now", never
  "does this specific old pod look healthy".

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
            if incident.itsm_ticket_id:
                try:
                    self.itsm_client.close_ticket(
                        incident.itsm_ticket_id,
                        comment=f"Auto-resolved by SRE platform: {output.get('summary', '')}",
                    )
                except Exception as e:
                    print(f"[{self.instance_id}] failed to close ITSM ticket "
                          f"{incident.itsm_ticket_id}: {e}")
            # -> KB update happens in the feedback/closure step
        elif output["result"] == "insufficient_observation":
            incident.status = IncidentStatus.VALIDATING  # re-check later
        else:
            # Fix didn't hold — back to diagnosis with fresh context.
            incident.status = IncidentStatus.DIAGNOSING
            incident.diagnosis_outcome = None
