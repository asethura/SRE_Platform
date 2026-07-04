"""Seed the shared session DB: criteria table + playbooks.

Runbooks and KB articles are NOT seeded here — they live in Confluence and
agents reach them live via the MCP connector (CONFLUENCE_MCP_URL)."""

from db.models import (
    AgentCriteria,
    AgentType,
    Playbook,
    RiskTier,
)


def seed(session_factory):
    db = session_factory()
    try:
        # ---- Entry criteria (to-do #13) — the orchestration contract ----
        db.add_all([
            AgentCriteria(agent_type=AgentType.TRIAGE,
                          entry_condition={"status": "new"},
                          exit_statuses=["closed_non_issue", "awaiting_approval", "diagnosing"]),
            AgentCriteria(agent_type=AgentType.TRIAGE,
                          entry_condition={"status": "triaging"},
                          exit_statuses=["closed_non_issue", "awaiting_approval", "diagnosing"]),
            AgentCriteria(agent_type=AgentType.DIAGNOSIS,
                          entry_condition={"status": "diagnosing"},
                          exit_statuses=["awaiting_approval", "escalated"]),
            # Remediation enters only when approval granted — orchestrator
            # moves status to remediating via the approve() hook + this row:
            AgentCriteria(agent_type=AgentType.REMEDIATION,
                          entry_condition={"status": "awaiting_approval"},
                          exit_statuses=["validating", "failed"]),
            AgentCriteria(agent_type=AgentType.VALIDATION,
                          entry_condition={"status": "validating"},
                          exit_statuses=["resolved", "diagnosing"]),
        ])

        # ---- Playbooks (executable, invoked over API — to-do #4) ----
        db.add_all([
            Playbook(id="PB-007", name="scale_hpa",
                     description="Patch HPA min replica count",
                     executor="kubernetes_api",
                     endpoint="/apis/autoscaling/v2/hpa/patch",
                     params_schema={"service": "string", "min_pods": "int"},
                     rollback_playbook_id="PB-008", risk_tier=RiskTier.LOW),
            Playbook(id="PB-008", name="restore_hpa",
                     description="Restore HPA to previous replica count",
                     executor="kubernetes_api",
                     endpoint="/apis/autoscaling/v2/hpa/patch",
                     params_schema={"service": "string"},
                     risk_tier=RiskTier.LOW),
            Playbook(id="PB-011", name="increase_db_pool",
                     description="Raise DB connection pool max via config API",
                     executor="http",
                     endpoint="https://config.internal/api/v1/pool",
                     params_schema={"service": "string", "max_connections": "int"},
                     rollback_playbook_id=None, risk_tier=RiskTier.MEDIUM),
            Playbook(id="PB-014", name="rolling_restart",
                     description="Rolling restart of service pods",
                     executor="kubernetes_api",
                     endpoint="/apis/apps/v1/deployments/restart",
                     params_schema={"service": "string"},
                     risk_tier=RiskTier.LOW),
        ])

        db.commit()
        print("Seeded: 5 criteria rows, 4 playbooks "
              "(runbooks/KB articles live in Confluence — see CONFLUENCE_MCP_URL)")
    finally:
        db.close()
