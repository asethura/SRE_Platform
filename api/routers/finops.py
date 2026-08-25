from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import LLMCall
from ..deps import get_db

router = APIRouter(prefix="/api/finops", tags=["finops"])


@router.get("/summary")
def get_summary(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)

    def cost_since(since: datetime | None) -> float:
        q = select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
        if since is not None:
            q = q.where(LLMCall.created_at >= since)
        return db.execute(q).scalar_one()

    call_count = db.execute(select(func.count(LLMCall.id))).scalar_one()

    return {
        "total_cost_usd": cost_since(None),
        "cost_last_24h_usd": cost_since(now - timedelta(hours=24)),
        "cost_last_7d_usd": cost_since(now - timedelta(days=7)),
        "call_count": call_count,
    }


@router.get("/by_agent")
def get_by_agent(db: Session = Depends(get_db)):
    rows = db.execute(
        select(
            LLMCall.agent_type,
            func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
            func.count(LLMCall.id),
        ).group_by(LLMCall.agent_type)
    ).all()
    return [
        {"agent_type": agent_type.value, "cost_usd": cost, "call_count": count}
        for agent_type, cost, count in rows
    ]


@router.get("/timeseries")
def get_timeseries(days: int = Query(14, ge=1, le=90), db: Session = Depends(get_db)):
    """Daily cost trend, broken down by agent type. Bucketed in Python
    rather than SQL so this works identically on SQLite (local dev) and
    Postgres (cluster) without dialect-specific date-truncation."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(LLMCall.created_at, LLMCall.agent_type, LLMCall.cost_usd)
        .where(LLMCall.created_at >= since)
    ).all()

    by_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for created_at, agent_type, cost_usd in rows:
        day = created_at.date().isoformat()
        by_day[day][agent_type.value] += cost_usd or 0.0

    return [
        {"date": day, "by_agent": dict(agents), "total_usd": sum(agents.values())}
        for day, agents in sorted(by_day.items())
    ]
