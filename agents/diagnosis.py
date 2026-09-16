"""
Diagnosis Agent — deep root-cause analysis. Two outcomes:
  diagnosed            -> root cause + remediation steps -> gate 1 approval
  unable_to_diagnose   -> escalate to human

Reads logs, traces, and metrics live via MCP (Cloud Logging = the "ELK"
equivalent, Cloud Trace = the "Tempo" equivalent, Prometheus = the same
prometheus-mcp server ValidationAgent already uses — none of these are
self-hosted here, see cloudrun/logging-mcp/, cloudrun/trace-mcp/, and
cloudrun/prometheus-mcp/) and recent deploys/commits from GitHub's hosted
MCP endpoint.

No Confluence here — diagnosis reasons out its own root cause AND its own
free-text remediation steps (no runbook to search for or cite). A human
approves that reasoning (Approval.stage=DIAGNOSIS) before remediation's
planning phase maps the steps onto the playbook catalog and asks for a
second approval (Approval.stage=REMEDIATION) to execute them.
"""

from datetime import datetime, timezone

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
    prometheus_mcp_server,
    trace_mcp_server,
)
from .service_graph import hinted_metric, load_service_graph


class DiagnosisAgent(BaseAgent):
    agent_type = AgentType.DIAGNOSIS

    def mcp_servers(self) -> list[dict]:
        return [
            logging_mcp_server(),
            trace_mcp_server(),
            prometheus_mcp_server(),
            github_mcp_server(),
        ]

    def system_prompt(self) -> str:
        return """You are the Diagnosis agent in an SRE incident-automation platform.
Triage could not match this incident to a known pattern. Perform root-cause
analysis using the incident description and service topology, plus tools to
pull your own logs, traces, metrics, and recent deploys/commits live:
- logging tools: search Cloud Logging for this service's recent errors/warnings.
- trace tools: list/get recent traces for this service to spot slow or
  failing spans.
- prometheus tools: query CURRENT metrics for this service (CPU, memory,
  5xx rate, restart count, pod status) — this is real live data, not a
  pre-fetched snapshot, so use it to confirm what's happening right now.
- github tools: check recent commits/PRs merged to this repo — a deploy
  shortly before symptoms started is causation-shaped.

IDENTIFY THE REAL SERVICE FIRST — don't waste tool calls on the wrong name:
- The incident's `service` field comes from ITSM ticket metadata (e.g. a
  Jira "Components" field) and can be null, generic, or simply not a real
  Kubernetes service/deployment name (e.g. a ticketing project code).
  Never issue your first queries filtered on `service` alone.
- Read the incident title and description first and identify the actual
  affected service/deployment name from that text (e.g. "product catalog is
  not coming up" -> productcatalogservice). Prefer this over `service`
  whenever they disagree or `service` looks unlikely to be a real deployment.
  `service_graph.services` in the context below lists every real deployment
  name this platform knows about — match against those names, don't invent one.
- If neither the description nor `service` names a specific service clearly,
  start with a broad/listing query (e.g. list deployments, list available
  metrics) to discover the right name before scoping further queries to it —
  do not guess-and-check a specific string across every tool in parallel.

USE THE SERVICE GRAPH — `service_graph` in the context below is a hand-
authored dependency map and metric-query vocabulary (a PRIOR, not verified
ground truth — confirm everything against live tool results, never cite it
as evidence on its own):
- `service_graph.services[name].depends_on` lists what that service calls,
  each with a `criticality`: "hard" means the caller's request fails without
  it; "soft" means the caller degrades gracefully (logs the error, returns a
  partial/empty result) and stays up without it.
  A symptom in one service with a recent problem in something it depends on
  (or that depends on it) is a causation-shaped correlation worth checking
  — e.g. checkoutservice errors while paymentservice is also unhealthy.
- When MULTIPLE upstream dependencies are unhealthy at once, criticality is
  what separates the actual root cause from unrelated noise: a "hard"
  dependency that just became unhealthy is causation-shaped for a fresh
  outage; a "soft" dependency being down does not, by itself, explain the
  caller being fully down — treat it as a separate, lower-priority finding
  (note it in evidence/remediation_steps if genuinely broken, but do not
  name it as *the* root cause of an outage it cannot fully explain). Also
  weigh recency: a dependency that just went unhealthy explains a symptom
  that just started; one that has been unhealthy for hours did not cause a
  symptom that started minutes ago.
- `service_graph.metrics` gives one canonical query template per named
  signal ({service} is a placeholder for the resolved deployment name) —
  a starting point for your Prometheus queries, not guaranteed to match
  what this cluster actually scrapes; confirm with list_metric_names first.
- `service_graph.hinted_metric` (if not null) is a keyword-matched guess at
  which signal the ticket's language points to — a hint for where to look
  first, not a substitute for checking the actual evidence.

CRITICAL — evidence must be CURRENT, not historical:
- "now" is given in the incident context below. Logging/trace tools can
  return results from hours or days ago if you don't constrain the time
  window — an old log line proves something happened once, not that it is
  still happening. Always weigh how old your evidence actually is relative
  to "now", and scope queries to a recent window (e.g. the last 15-30
  minutes) rather than an unbounded search.
- Before citing a specific pod/instance by name as evidence, confirm via a
  fresh Prometheus/logging query that it is CURRENTLY part of the live pod
  set for this service, not a prior instance that has since been replaced
  (remediation, rollbacks, and normal rescheduling all replace pod names —
  a name appearing in old logs does not mean it exists now).
- If your tools show the symptom is NOT currently reproducible (e.g. the
  service's current metrics/logs look healthy) even though older evidence
  looked bad, do not diagnose a still-ongoing incident from stale evidence
  alone — that is unable_to_diagnose (say so, and note the issue may have
  already self-resolved) rather than reporting a resolved problem as active.

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
        graph = load_service_graph()
        return {
            "now": datetime.now(timezone.utc).isoformat(),
            "incident": {
                "id": incident.id, "title": incident.title,
                "description": incident.description,
                "service": incident.service or "unknown",
                "severity": incident.severity,
                "reported_at": incident.created_at.isoformat() if incident.created_at else None,
            },
            "service_graph": {
                "services": graph["services"],
                "metrics": graph["metrics"],
                "hinted_metric": hinted_metric(f"{incident.title} {incident.description}"),
            },
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
