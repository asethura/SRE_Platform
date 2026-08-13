"""
Diagnosis Agent — deep root-cause analysis. Two outcomes:
  diagnosed            -> root cause + remediation steps -> gate 1 approval
  unable_to_diagnose   -> escalate to human

Reads logs and traces live via MCP (Cloud Logging = the "ELK" equivalent,
Cloud Trace = the "Tempo" equivalent — neither is self-hosted here, see
cloudrun/logging-mcp/ and cloudrun/trace-mcp/) and recent deploys/commits
from GitHub's hosted MCP endpoint. fetch_metrics (Prometheus) is still a
stub — wire it into the same prometheus-mcp server ValidationAgent already
uses, next.

No Confluence here — diagnosis reasons out its own root cause AND its own
free-text remediation steps (no runbook to search for or cite). A human
approves that reasoning (Approval.stage=DIAGNOSIS) before remediation's
planning phase maps the steps onto the playbook catalog and asks for a
second approval (Approval.stage=REMEDIATION) to execute them.
"""

from db.models import (
    Approval,
    ApprovalStage,
    AgentType,
    DiagnosisOutcome,
    Incident,
    IncidentStatus,
)
from .base import (
    BaseAgent,
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

Reason step by step internally, then commit to ONE outcome:
- diagnosed: you found the root cause AND can state concrete remediation
  steps to fix it
- unable_to_diagnose: evidence is insufficient or contradictory, OR you
  found a cause but genuinely cannot state a fix for it

Correlate signals: a deploy 2h ago + errors starting 2h ago is causation-shaped.

If you commit to "diagnosed", state your remediation_steps as plain,
concrete actions (e.g. "scale the deployment back to its previous replica
count", "restart the pods", "raise the DB connection pool limit") — a human
will review this reasoning, and then a separate step maps these steps onto
an approved catalog of executable actions. Do not reference playbook ids or
any specific automation mechanism; just state what should happen.

Respond ONLY with JSON:
{
  "outcome": "diagnosed" | "unable_to_diagnose",
  "root_cause": "concise statement or null",
  "evidence": ["signal 1", "signal 2", ...],
  "remediation_steps": ["step 1", "step 2", ...] or null,
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

        root_cause = output.get("root_cause")
        steps = output.get("remediation_steps")
        if outcome == DiagnosisOutcome.DIAGNOSED and (not root_cause or not steps):
            # Claimed a diagnosis but couldn't state both a cause and
            # concrete steps — degrade safely rather than hand gate 1
            # nothing to review.
            outcome = DiagnosisOutcome.UNABLE_TO_DIAGNOSE

        incident.diagnosis_outcome = outcome

        if outcome == DiagnosisOutcome.DIAGNOSED:
            incident.root_cause = root_cause
            incident.remediation_steps = steps
            incident.status = IncidentStatus.AWAITING_DIAGNOSIS_APPROVAL
            db.add(Approval(
                incident_id=incident.id,
                stage=ApprovalStage.DIAGNOSIS,
                summary=(
                    f"Root cause: {root_cause} | "
                    f"Proposed steps: {'; '.join(steps)}"
                ),
            ))
        else:
            # unable_to_diagnose escalates to a human.
            # Feedback loop later turns these into new KB articles.
            incident.status = IncidentStatus.ESCALATED
