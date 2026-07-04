"""
Human-in-the-loop hooks — call these from your UI / API.

With no central orchestrator, approval works purely through the session DB:
approve() flips the Approval row to APPROVED, and the remediation agent's
next poll picks the incident up (its eligible() gate requires that row).
reject() routes the incident to diagnosis instead (the dashed reject path).
"""

from db.models import Approval, ApprovalStatus, Incident, IncidentStatus


def approve(session_factory, incident_id: str, decided_by: str):
    db = session_factory()
    try:
        apr = (db.query(Approval)
               .filter(Approval.incident_id == incident_id,
                       Approval.status == ApprovalStatus.PENDING).first())
        if apr is None:
            raise ValueError("No pending approval")
        apr.status = ApprovalStatus.APPROVED
        apr.decided_by = decided_by
        db.commit()
        print(f"  [hitl] {decided_by} approved runbook {apr.runbook_id}")
    finally:
        db.close()


def reject(session_factory, incident_id: str, decided_by: str, reason: str):
    db = session_factory()
    try:
        apr = (db.query(Approval)
               .filter(Approval.incident_id == incident_id,
                       Approval.status == ApprovalStatus.PENDING).first())
        if apr is None:
            raise ValueError("No pending approval")
        apr.status = ApprovalStatus.REJECTED
        apr.decided_by = decided_by
        apr.reject_reason = reason
        # Rejected known-fix -> send to diagnosis instead (dashed reject path)
        incident = db.get(Incident, incident_id)
        incident.status = IncidentStatus.DIAGNOSING
        db.commit()
        print(f"  [hitl] {decided_by} rejected runbook {apr.runbook_id}: {reason}")
    finally:
        db.close()
