"""
Human-in-the-loop hooks — call these from your UI / API.

With no central orchestrator, approval works purely through the session DB.
Two gates, distinguished by Approval.stage:
  DIAGNOSIS   — reviews root cause + free-text remediation steps
  REMEDIATION — reviews the exact playbook-id + params mapping of those steps
approve() flips the latest PENDING Approval row to APPROVED — incident.status
never changes on approve; RemediationAgent.eligible() (per phase) is what
actually unlocks its next poll. reject() routes the incident back for
another attempt, targeted at what was actually rejected (see below).
"""

from db.models import Approval, ApprovalStage, ApprovalStatus, Incident, IncidentStatus


def _latest_pending(db, incident_id: str):
    return (db.query(Approval)
            .filter(Approval.incident_id == incident_id,
                    Approval.status == ApprovalStatus.PENDING)
            .order_by(Approval.created_at.desc())
            .first())


def approve(session_factory, incident_id: str, decided_by: str):
    db = session_factory()
    try:
        apr = _latest_pending(db, incident_id)
        if apr is None:
            raise ValueError("No pending approval")
        apr.status = ApprovalStatus.APPROVED
        apr.decided_by = decided_by
        db.commit()
        print(f"  [hitl] {decided_by} approved {apr.stage.value} stage "
              f"for {incident_id}: {apr.summary}")
    finally:
        db.close()


def reject(session_factory, incident_id: str, decided_by: str, reason: str):
    db = session_factory()
    try:
        apr = _latest_pending(db, incident_id)
        if apr is None:
            raise ValueError("No pending approval")
        apr.status = ApprovalStatus.REJECTED
        apr.decided_by = decided_by
        apr.reject_reason = reason

        incident = db.get(Incident, incident_id)
        if apr.stage == ApprovalStage.DIAGNOSIS:
            # The reasoning itself was rejected -- re-diagnose from scratch.
            incident.status = IncidentStatus.DIAGNOSING
        else:
            # The playbook mapping was wrong, but the already-approved root
            # cause/steps aren't in question -- send back to replanning
            # rather than all the way back to diagnosis. If a DIAGNOSIS-stage
            # approval exists it's still APPROVED (untouched by this
            # rejection), so eligible() is immediately true again once
            # incident.status matches it. A known_issue incident (no gate 1
            # ever ran) goes back to REMEDIATING instead.
            gate1 = (db.query(Approval)
                     .filter(Approval.incident_id == incident_id,
                             Approval.stage == ApprovalStage.DIAGNOSIS,
                             Approval.status == ApprovalStatus.APPROVED)
                     .order_by(Approval.created_at.desc())
                     .first())
            incident.status = (IncidentStatus.AWAITING_DIAGNOSIS_APPROVAL if gate1
                                else IncidentStatus.REMEDIATING)
        db.commit()
        print(f"  [hitl] {decided_by} rejected {apr.stage.value} stage "
              f"for {incident_id}: {reason}")
    finally:
        db.close()
