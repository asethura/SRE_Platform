"""
Diagnosis Agent — deep root-cause analysis. Three outcomes (your diagram):
  root_cause_with_remediation -> attach runbook -> approval -> remediation
  root_cause_no_remediation   -> escalate to human (new runbook needed)
  unable_to_diagnose          -> escalate to human  (gap #5 fix)

Reads logs and traces live via MCP (Cloud Logging = the "ELK" equivalent,
Cloud Trace = the "Tempo" equivalent — neither is self-hosted here, see
cloudrun/logging-mcp/ and cloudrun/trace-mcp/) and recent deploys/commits
from GitHub's hosted MCP endpoint. fetch_metrics (Prometheus) is still a
stub — wire it into the same prometheus-mcp server ValidationAgent already
uses, next.

Runbook candidates come live from Confluence via the MCP connector
(mcp_servers()) — same as triage, no local runbooks table.
"""

from db.models import (
    Approval,
    AgentType,
    DiagnosisOutcome,
    Incident,
    IncidentStatus,
    RiskTier,
)
from .base import (
    BaseAgent,
    confluence_mcp_server,
    github_mcp_server,
    logging_mcp_server,
    trace_mcp_server,
)


# --- Still a stub: swap for the same prometheus-mcp server validation.py uses --

def fetch_metrics(service: str) -> dict:
    """Prometheus MCP in production."""
    return {"cpu_pct": 91, "mem_pct": 62, "error_rate_5xx": 0.07,
            "pod_restarts_last_hour": 4}


class DiagnosisAgent(BaseAgent):
    agent_type = AgentType.DIAGNOSIS

    def mcp_servers(self) -> list[dict]:
        return [
            confluence_mcp_server(),
            logging_mcp_server(),
            trace_mcp_server(),
            github_mcp_server(),
        ]

    def system_prompt(self) -> str:
        return """You are the Diagnosis agent in an SRE incident-automation platform.
Triage could not match this incident to a known pattern. Perform root-cause
analysis using the incident description, service topology, and the metrics
(pre-fetched below — still a stub, see fetch_metrics), plus tools to pull
your own logs, traces, and recent deploys/commits live:
- logging tools: search Cloud Logging for this service's recent errors/warnings.
- trace tools: list/get recent traces for this service to spot slow or
  failing spans.
- github tools: check recent commits/PRs merged to this repo — a deploy
  shortly before symptoms started is causation-shaped.
- confluence tools: search for a runbook page once you have a root cause.

Reason step by step internally, then commit to ONE outcome:
- root_cause_with_remediation: you found the cause AND an existing runbook fixes it
- root_cause_no_remediation: you found the cause but no runbook covers it
- unable_to_diagnose: evidence is insufficient or contradictory

Correlate signals: a deploy 2h ago + errors starting 2h ago is causation-shaped.

Once you have a root cause, use your Confluence tools to search for a runbook
page covering it (by service name and by root-cause keywords). Never invent a
runbook_id — only cite a page you actually found, and read its risk tier
(low/medium/high) exactly as stated on the page; never guess it. If you find
a root cause but no runbook page covers it, that's root_cause_no_remediation.

Respond ONLY with JSON:
{
  "outcome": "root_cause_with_remediation" | "root_cause_no_remediation" | "unable_to_diagnose",
  "root_cause": "concise statement or null",
  "evidence": ["signal 1", "signal 2", ...],
  "runbook_id": "confluence page id, or null",
  "runbook_name": "runbook page title, or null",
  "runbook_risk_tier": "low" | "medium" | "high" | null,
  "suggested_fix": "free-text fix suggestion when no runbook exists, else null",
  "confidence": 0.0-1.0
}"""

    def build_context(self, db, incident: Incident) -> dict:
        svc = incident.service or "unknown"
        return {
            "incident": {
                "id": incident.id, "title": incident.title,
                "description": incident.description,
                "service": svc, "severity": incident.severity,
            },
            "metrics": fetch_metrics(svc),
        }

    def apply_output(self, db, incident: Incident, output: dict) -> None:
        outcome = DiagnosisOutcome(output["outcome"])
        incident.diagnosis_outcome = outcome

        if outcome == DiagnosisOutcome.ROOT_CAUSE_WITH_REMEDIATION:
            runbook_id = output.get("runbook_id")
            risk_tier_raw = output.get("runbook_risk_tier")
            risk_tier = RiskTier(risk_tier_raw) if risk_tier_raw in {t.value for t in RiskTier} else None
            if not runbook_id or risk_tier is None:
                # Claimed a fix but couldn't cite a runbook page with an
                # explicit risk tier — escalate rather than guess.
                incident.status = IncidentStatus.ESCALATED
                return

            runbook_name = output.get("runbook_name") or runbook_id
            incident.matched_runbook_id = runbook_id
            incident.matched_runbook_name = runbook_name
            incident.matched_runbook_risk_tier = risk_tier
            incident.status = IncidentStatus.AWAITING_APPROVAL
            db.add(Approval(
                incident_id=incident.id,
                runbook_id=runbook_id,
                summary=(
                    f"Diagnosis: {output.get('root_cause')} | "
                    f"Runbook {runbook_id} ({runbook_name}) | "
                    f"risk: {risk_tier.value}"
                ),
            ))
        else:
            # Both no-remediation and unable-to-diagnose escalate to a human.
            # Feedback loop later turns these into new runbooks / KB articles.
            incident.status = IncidentStatus.ESCALATED
