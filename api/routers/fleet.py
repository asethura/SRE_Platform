from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AgentRun, AgentType, Incident, RunStatus
from ..deps import get_db
from ..k8s_fleet import fleet_status

router = APIRouter(prefix="/api/fleet", tags=["fleet"])

ACTIVE_RUN_STATUSES = (RunStatus.CLAIMED, RunStatus.RUNNING)


@router.get("")
def get_fleet():
    return fleet_status()


@router.get("/{agent_type}/active")
def get_active_incidents(agent_type: str, db: Session = Depends(get_db)):
    try:
        agent_type_enum = AgentType(agent_type)
    except ValueError:
        raise HTTPException(404, f"Unknown agent_type {agent_type!r}")

    rows = db.execute(
        select(AgentRun, Incident)
        .join(Incident, AgentRun.incident_id == Incident.id)
        .where(
            AgentRun.agent_type == agent_type_enum,
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .order_by(AgentRun.started_at.desc())
    ).all()

    return [
        {
            "incident_id": incident.id,
            "run_id": run.id,
            "title": incident.title,
            "service": incident.service,
            "severity": incident.severity,
            "status": incident.status.value,
            "started_at": run.started_at.isoformat() if run.started_at else None,
        }
        for run, incident in rows
    ]
