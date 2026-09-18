from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AgentRun, Approval, Incident, IncidentStatus, RunStatus, TERMINAL_STATUSES
from ..deps import get_db

router = APIRouter(prefix="/api/incidents", tags=["incidents"])

ACTIVE_RUN_STATUSES = (RunStatus.CLAIMED, RunStatus.RUNNING)


def _naive_utc(dt: datetime | None) -> datetime | None:
    """Incident.created_at is stored tz-naive (always UTC, see db/models.py
    utcnow()) -- strip any offset a client sends so the comparison isn't
    aware-vs-naive (which asyncpg/psycopg reject outright)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _incident_summary(incident: Incident, current_agent: str | None) -> dict:
    return {
        "incident_id": incident.id,
        "title": incident.title,
        "service": incident.service,
        "severity": incident.severity,
        "status": incident.status.value,
        "current_agent": current_agent,
        "created_at": incident.created_at.isoformat() if incident.created_at else None,
        "updated_at": incident.updated_at.isoformat() if incident.updated_at else None,
    }


@router.get("/in_progress")
def get_in_progress_incidents(db: Session = Depends(get_db)):
    """Every incident not yet in a terminal status, newest-updated first --
    what the sidebar's Incidents tab lists. `current_agent` is whichever
    agent type currently holds an active (claimed/running) run on it, or
    null if it's between agents (e.g. waiting on a human approval)."""
    incidents = db.execute(
        select(Incident)
        .where(Incident.status.notin_(TERMINAL_STATUSES))
        .order_by(Incident.updated_at.desc())
    ).scalars().all()
    if not incidents:
        return []

    active_runs = db.execute(
        select(AgentRun)
        .where(
            AgentRun.incident_id.in_([i.id for i in incidents]),
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    ).scalars().all()
    current_agent_by_incident = {r.incident_id: r.agent_type.value for r in active_runs}

    return [
        _incident_summary(inc, current_agent_by_incident.get(inc.id))
        for inc in incidents
    ]


def _incidents_in_range(db: Session, start: datetime | None, end: datetime | None) -> list[Incident]:
    """Incidents whose created_at falls in [start, end] (either bound
    optional), regardless of status -- shared by the list and stats
    endpoints so their counts always agree with what's on screen."""
    query = select(Incident)
    start, end = _naive_utc(start), _naive_utc(end)
    if start is not None:
        query = query.where(Incident.created_at >= start)
    if end is not None:
        query = query.where(Incident.created_at <= end)
    return db.execute(query.order_by(Incident.created_at.desc())).scalars().all()


@router.get("")
def get_incidents(
    start: datetime | None = None,
    end: datetime | None = None,
    db: Session = Depends(get_db),
):
    """Every incident reported in the given range -- the UI's Incidents tab
    time-frame filter. Unlike /in_progress, this includes terminal
    incidents (resolved, escalated, closed_non_issue)."""
    incidents = _incidents_in_range(db, start, end)
    if not incidents:
        return []

    active_runs = db.execute(
        select(AgentRun)
        .where(
            AgentRun.incident_id.in_([i.id for i in incidents]),
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    ).scalars().all()
    current_agent_by_incident = {r.incident_id: r.agent_type.value for r in active_runs}

    return [
        _incident_summary(inc, current_agent_by_incident.get(inc.id))
        for inc in incidents
    ]


@router.get("/stats")
def get_incident_stats(
    start: datetime | None = None,
    end: datetime | None = None,
    db: Session = Depends(get_db),
):
    """Stats bar for the Incidents tab, scoped to the same time range as the
    list above it:
    - picked_up_by_agents: incidents with at least one agent run (i.e. not
      still sitting untouched at status=new)
    - remediated: incidents validation confirmed fixed (status=resolved)
    - pending_approval: sitting at gate 1 or gate 2, waiting on a human
      (status in awaiting_diagnosis_approval | awaiting_remediation_approval)
    - escalated: incidents that needed a human (status=escalated) -- either
      an explicit unable_to_diagnose verdict, or an agent repeatedly
      erroring on this incident (see agents/base.py MAX_CONSECUTIVE_FAILURES)
    - escalated_by_last_agent: those escalated incidents grouped by whichever
      agent type most recently ran on them -- who to route the follow-up to
    """
    incidents = _incidents_in_range(db, start, end)
    total = len(incidents)
    if total == 0:
        return {
            "total": 0,
            "picked_up_by_agents": 0,
            "remediated": 0,
            "pending_approval": 0,
            "escalated": 0,
            "escalated_by_last_agent": {},
        }

    runs = db.execute(
        select(AgentRun)
        .where(AgentRun.incident_id.in_([i.id for i in incidents]))
        .order_by(AgentRun.started_at.asc())
    ).scalars().all()

    picked_up_ids = {r.incident_id for r in runs}
    # Ascending order + dict overwrite -> last write per incident is its
    # most recently started run.
    last_agent_by_incident: dict[str, str] = {}
    for run in runs:
        last_agent_by_incident[run.incident_id] = run.agent_type.value

    escalated_incidents = [i for i in incidents if i.status == IncidentStatus.ESCALATED]
    escalated_by_last_agent = Counter(
        last_agent_by_incident.get(inc.id, "unassigned") for inc in escalated_incidents
    )
    pending_approval = sum(
        1 for i in incidents
        if i.status in (IncidentStatus.AWAITING_DIAGNOSIS_APPROVAL, IncidentStatus.AWAITING_REMEDIATION_APPROVAL)
    )

    return {
        "total": total,
        "picked_up_by_agents": len(picked_up_ids),
        "remediated": sum(1 for i in incidents if i.status == IncidentStatus.RESOLVED),
        "pending_approval": pending_approval,
        "escalated": len(escalated_incidents),
        "escalated_by_last_agent": dict(escalated_by_last_agent),
    }


@router.get("/{incident_id}")
def get_incident_detail(incident_id: str, db: Session = Depends(get_db)):
    """Everything the drill-down view needs: the incident itself, its full
    agent-run timeline (handoff medium AND audit trail, see db/models.py),
    and any approvals raised against it."""
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(404, f"No incident {incident_id!r}")

    runs = db.execute(
        select(AgentRun)
        .where(AgentRun.incident_id == incident_id)
        .order_by(AgentRun.started_at.asc())
    ).scalars().all()

    approvals = db.execute(
        select(Approval)
        .where(Approval.incident_id == incident_id)
        .order_by(Approval.created_at.asc())
    ).scalars().all()

    return {
        "incident_id": incident.id,
        "title": incident.title,
        "description": incident.description,
        "source": incident.source,
        "service": incident.service,
        "severity": incident.severity,
        "status": incident.status.value,
        "triage_verdict": incident.triage_verdict.value if incident.triage_verdict else None,
        "diagnosis_outcome": incident.diagnosis_outcome.value if incident.diagnosis_outcome else None,
        "root_cause": incident.root_cause,
        "remediation_steps": incident.remediation_steps,
        "created_at": incident.created_at.isoformat() if incident.created_at else None,
        "updated_at": incident.updated_at.isoformat() if incident.updated_at else None,
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        "runs": [
            {
                "run_id": run.id,
                "agent_type": run.agent_type.value,
                "status": run.status.value,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "output": run.output,
                "error": run.error,
            }
            for run in runs
        ],
        "approvals": [
            {
                "approval_id": apr.id,
                "stage": apr.stage.value,
                "status": apr.status.value,
                "summary": apr.summary,
                "risk_tier": apr.risk_tier.value if apr.risk_tier else None,
                "decided_by": apr.decided_by,
                "decided_at": apr.decided_at.isoformat() if apr.decided_at else None,
                "reject_reason": apr.reject_reason,
                "created_at": apr.created_at.isoformat() if apr.created_at else None,
            }
            for apr in approvals
        ],
    }
