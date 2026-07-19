"""
Scenario injector — create a known-issue demo incident from a named scenario
and drive it through the full pipeline (triage -> approval -> remediation ->
validation), so you can simulate different conditions by switching one CLI
argument instead of editing main.py.

Usage:
    python inject_scenario.py --list
    python inject_scenario.py hpa_scale_up
    python inject_scenario.py db_pool_exhaustion --no-approve
    python inject_scenario.py hpa_scale_up bad_deploy_restart   # run several in sequence

Each scenario matches one of the seeded playbooks in db/seed.py (see
README's "4 simple scenarios" for the mapping) — triage still has to find a
matching Confluence runbook page for these to actually resolve as
known_issue rather than falling through to diagnosis.

Requires: ANTHROPIC_API_KEY (and CONFLUENCE_MCP_URL for real runbook
matching) in env, same as main.py. --list needs neither.
"""

import argparse
import os

from dotenv import load_dotenv

import hitl
from db.models import AgentCriteria, get_engine, init_db
from db.seed import seed
from agents.triage import TriageAgent
from agents.diagnosis import DiagnosisAgent
from agents.remediation import RemediationAgent
from agents.validation import ValidationAgent
from main import create_incident, poll_cycle, show, status_of

SCENARIOS = {
    "hpa_scale_up": dict(
        title="Payment 5xx spike under load",
        description="payment-service CPU pinned above 90%, 5xx error rate "
                     "rising, pod count maxed at current HPA min replicas "
                     "after a traffic spike",
        service="payment-service", severity="P2", source="datadog",
    ),
    "db_pool_exhaustion": dict(
        title="Orders service latency, pool exhausted",
        description="orders-service latency climbing, logs show "
                     "'connection pool exhausted: max=20 in_use=20', "
                     "no recent deploy",
        service="orders-service", severity="P2", source="datadog",
    ),
    "bad_deploy_restart": dict(
        title="Orders service unhealthy after deploy",
        description="orders-service intermittently returning 5xx since a "
                     "deploy 20 minutes ago; prior incidents matching this "
                     "pattern were fixed by a rolling restart",
        service="orders-service", severity="P2", source="pagerduty",
    ),
    "hpa_scale_down": dict(
        title="Payment service over-provisioned",
        description="payment-service HPA min replicas still elevated from "
                     "a manual override during last week's traffic event, "
                     "event is long over, no user-facing symptom, cost "
                     "review flagged it",
        service="payment-service", severity="P4", source="manual",
    ),
}


def ensure_seeded(session_factory):
    """seed() is not idempotent (duplicate Playbook rows) — only run it
    once per DB, on first use, so repeated injector runs don't crash."""
    db = session_factory()
    try:
        already_seeded = db.query(AgentCriteria).first() is not None
    finally:
        db.close()
    if not already_seeded:
        seed(session_factory)


def run(scenario: str, auto_approve: bool = True, poll_rounds: int = 10) -> str:
    if scenario not in SCENARIOS:
        raise SystemExit(
            f"Unknown scenario '{scenario}'. Choices: {', '.join(SCENARIOS)}"
        )

    load_dotenv()
    engine = get_engine(os.environ.get("DATABASE_URL", "sqlite:///sre_platform.db"))
    session_factory = init_db(engine)
    ensure_seeded(session_factory)

    agents = [
        TriageAgent(session_factory),
        DiagnosisAgent(session_factory),
        RemediationAgent(session_factory),
        ValidationAgent(session_factory),
    ]

    print("=" * 70)
    print(f"SCENARIO: {scenario}")
    inc_id = create_incident(session_factory, **SCENARIOS[scenario])
    print(f"  created {inc_id}")

    poll_cycle(agents, max_rounds=poll_rounds)

    if status_of(session_factory, inc_id) == "awaiting_approval":
        if auto_approve:
            hitl.approve(session_factory, inc_id, decided_by="oncall@company.com")
            poll_cycle(agents, max_rounds=poll_rounds)
        else:
            print(f"  awaiting_approval — call hitl.approve(session_factory, "
                  f"'{inc_id}', decided_by=...) to continue")
            show(session_factory, inc_id)
            return inc_id

    show(session_factory, inc_id)
    return inc_id


def _print_scenarios():
    print("Available scenarios:")
    for name, s in SCENARIOS.items():
        print(f"  {name:20s} {s['service']:16s} {s['title']}")


def main():
    parser = argparse.ArgumentParser(
        description="Inject one or more demo incidents and run them through "
                     "the full triage -> approval -> remediation -> "
                     "validation pipeline."
    )
    parser.add_argument("scenarios", nargs="*",
                         help="Scenario name(s) to run, in order (see --list)")
    parser.add_argument("--list", action="store_true",
                         help="List available scenarios and exit")
    parser.add_argument("--no-approve", action="store_true",
                         help="Stop at awaiting_approval instead of auto-approving")
    parser.add_argument("--poll-rounds", type=int, default=10,
                         help="Max poll rounds per scenario before giving up (default 10)")
    args = parser.parse_args()

    if args.list or not args.scenarios:
        _print_scenarios()
        if not args.scenarios:
            print("\nUsage: python inject_scenario.py <scenario> [<scenario> ...]")
        return

    for scenario in args.scenarios:
        run(scenario, auto_approve=not args.no_approve, poll_rounds=args.poll_rounds)


if __name__ == "__main__":
    main()
