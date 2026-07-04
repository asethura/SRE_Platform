"""
Triage Agent — intake + fast, cheap gate. Three exits:
  non_issue      -> close ticket with reason
  known_issue    -> attach runbook, request human approval, -> remediation
  unknown_issue  -> diagnosis

Also the intake process: pulls open tickets from the ITSM tool (stubbed —
see integrations/itsm.py) into the session DB before each poll, so incidents
never need to be created manually. New tickets are prioritized by severity
then age (integrations.itsm.priority_key) and deduped by itsm_ticket_id.

Reads: incident + KB articles/runbooks live from Confluence via the MCP
connector (mcp_servers()) — the model searches and fetches pages itself,
no local KB/runbook tables.
"""

from datetime import datetime, timezone

from db.models import (
    Approval,
    AgentType,
    Incident,
    IncidentStatus,
    RiskTier,
    TriageVerdict,
)
from integrations.itsm import ITSMClient, StubITSMClient, priority_key
from .base import BaseAgent, confluence_mcp_server


class TriageAgent(BaseAgent):
    agent_type = AgentType.TRIAGE

    def __init__(self, session_factory, instance_id: str = None,
                 itsm_client: ITSMClient = None):
        super().__init__(session_factory, instance_id)
        self.itsm_client = itsm_client or StubITSMClient()

    def poll_once(self) -> list[dict]:
        self.intake()
        return super().poll_once()

    def intake(self, limit: int = 10) -> list[str]:
        """Pull open ITSM tickets into the session DB as NEW incidents.
        Dedupes on itsm_ticket_id and takes the top `limit` by priority so a
        burst of tickets doesn't flood one poll cycle."""
        db = self.session_factory()
        try:
            tickets = self.itsm_client.fetch_open_incidents()
            known_ids = {
                row[0] for row in
                db.query(Incident.itsm_ticket_id)
                .filter(Incident.itsm_ticket_id.isnot(None)).all()
            }
            new_tickets = sorted(
                (t for t in tickets if t.ticket_id not in known_ids),
                key=priority_key,
            )[:limit]

            created_ids = []
            for t in new_tickets:
                incident = Incident(
                    itsm_ticket_id=t.ticket_id,
                    title=t.title,
                    description=t.description,
                    source=t.source,
                    service=t.service,
                    severity=t.severity,
                )
                db.add(incident)
                db.flush()
                created_ids.append(incident.id)
            db.commit()
            return created_ids
        finally:
            db.close()

    def on_claim(self, db, incident: Incident) -> None:
        if incident.status == IncidentStatus.NEW:
            incident.status = IncidentStatus.TRIAGING

    def mcp_servers(self) -> list[dict]:
        return [confluence_mcp_server()]

    def system_prompt(self) -> str:
        return """You are the Triage agent in an SRE incident-automation platform.

Your ONLY job is to read an incident and decide, WITHOUT deep analysis:
1. Is this a real issue or working as designed?
2. If real, does a known runbook fix it?

You have tools to search and read Confluence. Use them to look for:
- KB articles: past verified triage patterns and their verdicts (a KB page's
  content will say whether that pattern is a non_issue or a known_issue and,
  for known_issue, which runbook page fixes it). Trust pages that read as
  reviewed/verified strongly.
- Runbook pages: search by service name and by symptom keywords from the
  incident. A runbook page states its risk tier (low/medium/high) near the
  top — read it exactly as written; never guess a risk tier.

Decision rules:
- A KB article says this pattern is a non-issue -> verdict non_issue
- Incident matches a runbook's pattern -> verdict known_issue, include the
  runbook's Confluence page id, its title, and its stated risk_tier
- Otherwise -> verdict unknown_issue (diagnosis will investigate)
- Maintenance windows, deployments in progress, test alerts, misconfigured
  thresholds are classic non-issues.
- When uncertain between non_issue and unknown_issue, choose unknown_issue.
  A false "non-issue" on a real outage is the worst mistake you can make.
- If you cannot find a runbook page with an explicit risk tier, do not
  claim known_issue — use unknown_issue instead.

Respond ONLY with JSON:
{
  "verdict": "non_issue" | "known_issue" | "unknown_issue",
  "confidence": 0.0-1.0,
  "reasoning": "one or two sentences",
  "runbook_id": "confluence page id, or null",
  "runbook_name": "runbook page title, or null",
  "runbook_risk_tier": "low" | "medium" | "high" | null,
  "matched_kb_ids": ["confluence page id", ...]
}"""

    def build_context(self, db, incident: Incident) -> dict:
        return {
            "incident": {
                "id": incident.id, "title": incident.title,
                "description": incident.description,
                "service": incident.service, "severity": incident.severity,
                "source": incident.source,
            },
        }

    def apply_output(self, db, incident: Incident, output: dict) -> None:
        verdict = TriageVerdict(output["verdict"])
        incident.triage_verdict = verdict

        if verdict == TriageVerdict.NON_ISSUE:
            # Gap #2 from the diagram review: triage closes the ITSM ticket.
            incident.status = IncidentStatus.CLOSED_NON_ISSUE
            incident.resolved_at = datetime.now(timezone.utc)

        elif verdict == TriageVerdict.KNOWN_ISSUE:
            runbook_id = output.get("runbook_id")
            risk_tier_raw = output.get("runbook_risk_tier")
            risk_tier = RiskTier(risk_tier_raw) if risk_tier_raw in {t.value for t in RiskTier} else None
            if not runbook_id or risk_tier is None:
                # Model claimed known_issue but couldn't cite a runbook page
                # with an explicit risk tier from Confluence — degrade safely.
                incident.triage_verdict = TriageVerdict.UNKNOWN_ISSUE
                incident.status = IncidentStatus.DIAGNOSING
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
                    f"Runbook {runbook_id} ({runbook_name}) | "
                    f"risk: {risk_tier.value} | "
                    f"reason: {output['reasoning']}"
                ),
            ))

        else:  # UNKNOWN_ISSUE
            incident.status = IncidentStatus.DIAGNOSING
