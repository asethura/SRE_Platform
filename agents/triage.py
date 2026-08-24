"""
Triage Agent — intake + fast, cheap gate. Three exits:
  non_issue      -> close ticket with reason
  known_issue    -> KB confirms a known fix -> remediation planning
                     (skips diagnosis AND gate 1 — see agents/remediation.py)
  unknown_issue  -> diagnosis

Also the intake process: pulls open tickets from the ITSM tool (stubbed —
see integrations/itsm.py) into the session DB before each poll, so incidents
never need to be created manually. New tickets are prioritized by severity
then age (integrations.itsm.priority_key) and deduped by itsm_ticket_id.

Reads: incident + KB articles live from Confluence via the MCP connector
(mcp_servers()) — the model searches and fetches pages itself, no local KB
table. Triage is the ONLY agent that still uses Confluence: it's read only
for classification (is this pattern verified real/non-issue?), never for
citing a fix — known_issue states its own free-text remediation_steps,
which remediation later maps onto the playbook catalog.
"""

from datetime import datetime, timezone

from db.models import (
    AgentType,
    Incident,
    IncidentStatus,
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
2. If real, is this an exact match for a pattern a KB article already
   verified, with a known fix?

You have tools to search and read Confluence KB articles. Use them to look
for past verified triage patterns and their verdicts — a KB page's content
will say whether that pattern is a non_issue or a known_issue and, for
known_issue, what fixed it. Trust pages that read as reviewed/verified
strongly.

Decision rules:
- A KB article says this pattern is a non-issue -> verdict non_issue
- A KB article verifies this exact pattern AND states a concrete fix ->
  verdict known_issue, state that fix yourself as remediation_steps (plain
  text, one step per list item) — do not just cite the page, restate what
  it says to do so remediation can act on it directly
- Otherwise -> verdict unknown_issue (diagnosis will investigate)
- Maintenance windows, deployments in progress, test alerts, misconfigured
  thresholds are classic non-issues.
- When uncertain between non_issue and unknown_issue, choose unknown_issue.
  A false "non-issue" on a real outage is the worst mistake you can make.
- If you cannot state concrete remediation_steps from a verified KB match,
  do not claim known_issue — use unknown_issue instead (diagnosis will
  investigate properly).

Respond ONLY with JSON:
{
  "verdict": "non_issue" | "known_issue" | "unknown_issue",
  "confidence": 0.0-1.0,
  "reasoning": "one or two sentences",
  "remediation_steps": ["step 1", "step 2", ...] or null,
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
            if incident.itsm_ticket_id:
                try:
                    self.itsm_client.close_ticket(
                        incident.itsm_ticket_id,
                        comment=f"Closed as non-issue by SRE platform: {output.get('reasoning', '')}",
                    )
                except Exception as e:
                    print(f"[{self.instance_id}] failed to close ITSM ticket "
                          f"{incident.itsm_ticket_id}: {e}")

        elif verdict == TriageVerdict.KNOWN_ISSUE:
            steps = output.get("remediation_steps")
            if not steps:
                # Model claimed known_issue but couldn't state concrete
                # remediation steps — degrade safely rather than send an
                # empty plan into remediation.
                incident.triage_verdict = TriageVerdict.UNKNOWN_ISSUE
                incident.status = IncidentStatus.DIAGNOSING
                return

            incident.root_cause = output["reasoning"]
            incident.remediation_steps = steps
            # No Approval row here — known_issue skips gate 1 entirely.
            # Remediation's planning phase (ungated for this entry status)
            # maps these steps onto the playbook catalog and creates gate 2.
            incident.status = IncidentStatus.REMEDIATING

        else:  # UNKNOWN_ISSUE
            incident.status = IncidentStatus.DIAGNOSING
