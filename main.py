"""
Demo runner — walks three incidents through the platform, one per triage exit:

  1. Non-issue        (maintenance window alert)     -> closed by triage
  2. Known issue      (matches KB-041 / RB-012)      -> approval -> remediation -> validation -> resolved
  3. Unknown issue    (novel symptom)                -> diagnosis -> (runbook found) -> approval -> ...

No orchestrator: every agent polls the shared session DB for work matching
its entry criteria. The demo drives poll cycles synchronously so the output
is readable; in deployment each agent polls in its own process (run_agent.py).

Requires: ANTHROPIC_API_KEY in env.
Run:      python main.py
"""

import os

from dotenv import load_dotenv

import hitl
from db.models import Incident, get_engine, init_db
from db.seed import seed
from agents.triage import TriageAgent
from agents.diagnosis import DiagnosisAgent
from agents.remediation import RemediationAgent
from agents.validation import ValidationAgent


def create_incident(session_factory, **kwargs) -> str:
    db = session_factory()
    try:
        inc = Incident(**kwargs)
        db.add(inc)
        db.commit()
        return inc.id
    finally:
        db.close()


def show(session_factory, incident_id):
    db = session_factory()
    try:
        inc = db.get(Incident, incident_id)
        print(f"  => {inc.id} | status={inc.status.value} "
              f"| triage={inc.triage_verdict.value if inc.triage_verdict else '-'} "
              f"| runbook={inc.matched_runbook_id or '-'}\n")
    finally:
        db.close()


def poll_cycle(agents, max_rounds: int = 10):
    """Let each agent poll in turn until a full round finds no work.
    Stand-in for the independent run_forever() loops in deployment."""
    for _ in range(max_rounds):
        progressed = False
        for agent in agents:
            picked = agent.poll_once()
            for r in picked:
                print(f"  [{agent.agent_type.value}] processed {r['incident_id']}")
            progressed = progressed or bool(picked)
        if not progressed:
            return  # everyone idle -> blocked on human or done


def status_of(session_factory, incident_id) -> str:
    db = session_factory()
    try:
        return db.get(Incident, incident_id).status.value
    finally:
        db.close()


def main():
    load_dotenv()
    engine = get_engine(os.environ.get("DATABASE_URL", "sqlite:///sre_platform.db"))
    session_factory = init_db(engine)
    seed(session_factory)
    agents = [
        TriageAgent(session_factory),
        DiagnosisAgent(session_factory),
        RemediationAgent(session_factory),
        ValidationAgent(session_factory),
    ]

    # ---- Scenario 1: non-issue --------------------------------------------
    print("=" * 70)
    print("SCENARIO 1 — maintenance window alert (expected: closed_non_issue)")
    inc1 = create_incident(
        session_factory,
        title="DB latency alert",
        description="Latency alert fired during scheduled maintenance window "
                    "for database migration at 02:00 UTC",
        service="orders-service", severity="P3", source="datadog",
    )
    poll_cycle(agents)
    show(session_factory, inc1)

    # ---- Scenario 2: known issue ------------------------------------------
    print("=" * 70)
    print("SCENARIO 2 — known pattern (expected: approval -> remediation -> resolved)")
    inc2 = create_incident(
        session_factory,
        title="Payment 5xx spike",
        description="payment service 5xx spike during pod scaling after "
                    "deployment, error rate 4%",
        service="payment-service", severity="P2", source="pagerduty",
    )
    poll_cycle(agents)
    if status_of(session_factory, inc2) == "awaiting_approval":
        hitl.approve(session_factory, inc2, decided_by="oncall@company.com")  # <- your UI calls this
        poll_cycle(agents)
    show(session_factory, inc2)

    # ---- Scenario 3: unknown issue ----------------------------------------
    print("=" * 70)
    print("SCENARIO 3 — novel symptom (expected: diagnosis -> approval or escalated)")
    inc3 = create_incident(
        session_factory,
        title="Checkout timeouts",
        description="Users report intermittent checkout timeouts, upstream "
                    "timeout errors calling stripe-api, queue depth growing",
        service="payment-service", severity="P1", source="pagerduty",
    )
    poll_cycle(agents)
    if status_of(session_factory, inc3) == "awaiting_approval":
        hitl.approve(session_factory, inc3, decided_by="oncall@company.com")
        poll_cycle(agents)
    show(session_factory, inc3)


if __name__ == "__main__":
    main()
