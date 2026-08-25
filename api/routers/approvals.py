from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

import hitl
from db.models import Approval, ApprovalStatus, Incident
from ..deps import get_db, session_factory

router = APIRouter(tags=["approvals"])


@router.get("/api/approvals")
def get_pending_approvals(db: Session = Depends(get_db)):
    rows = db.execute(
        select(Approval, Incident)
        .join(Incident, Approval.incident_id == Incident.id)
        .where(Approval.status == ApprovalStatus.PENDING)
        .order_by(Approval.created_at.asc())
    ).all()

    return [
        {
            "approval_id": apr.id,
            "incident_id": apr.incident_id,
            "stage": apr.stage.value,
            "summary": apr.summary,
            "proposed_plan": apr.proposed_plan,
            "risk_tier": apr.risk_tier.value if apr.risk_tier else None,
            "created_at": apr.created_at.isoformat() if apr.created_at else None,
            "incident": {
                "title": incident.title,
                "service": incident.service,
                "severity": incident.severity,
                "status": incident.status.value,
            },
        }
        for apr, incident in rows
    ]


class DecisionRequest(BaseModel):
    decided_by: str


class RejectRequest(DecisionRequest):
    reason: str


@router.post("/api/incidents/{incident_id}/approve")
def approve_incident(incident_id: str, body: DecisionRequest):
    # hitl.approve() opens+closes its own session from the factory -- same
    # contract run_agent.py/main.py already use, no approval logic
    # duplicated here.
    try:
        hitl.approve(session_factory, incident_id, body.decided_by)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.post("/api/incidents/{incident_id}/reject")
def reject_incident(incident_id: str, body: RejectRequest):
    try:
        hitl.reject(session_factory, incident_id, body.decided_by, body.reason)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}
