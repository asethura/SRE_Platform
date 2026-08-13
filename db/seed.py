"""Seed the shared session DB: criteria table + playbooks.

KB articles (triage-only) are NOT seeded here — they live in Confluence and
triage reaches them live via the MCP connector (CONFLUENCE_MCP_URL). There
are no runbook documents anywhere: diagnosis states its own root cause +
remediation steps, and remediation independently maps those onto the
playbooks seeded below — see agents/remediation.py.

Playbooks ARE API docs, not local executable code: each row's `endpoint` is
a relative path on a single playbook server (PLAYBOOK_SERVER_URL), and
`params_schema` documents that endpoint's request body. `executor` is
descriptive only (what the playbook server does internally — call the
Kubernetes API, a config service, run a script) — remediation.py always
just POSTs to the endpoint, it never branches on `executor`."""

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
                          exit_statuses=["closed_non_issue", "remediating", "diagnosing"]),
            AgentCriteria(agent_type=AgentType.TRIAGE,
                          entry_condition={"status": "triaging"},
                          exit_statuses=["closed_non_issue", "remediating", "diagnosing"]),
            AgentCriteria(agent_type=AgentType.DIAGNOSIS,
                          entry_condition={"status": "diagnosing"},
                          exit_statuses=["awaiting_diagnosis_approval", "escalated"]),
            # Remediation runs in two phases, all routed to the same agent
            # (same idiom as Triage's NEW+TRIAGING rows above). Planning has
            # two possible entry statuses (triage known_issue vs. diagnosis
            # post gate-1-approval) — eligible() gates each appropriately;
            # execute hard-gates on an APPROVED REMEDIATION-stage Approval.
            AgentCriteria(agent_type=AgentType.REMEDIATION,
                          entry_condition={"status": "remediating"},
                          exit_statuses=["awaiting_remediation_approval", "escalated"]),
            AgentCriteria(agent_type=AgentType.REMEDIATION,
                          entry_condition={"status": "awaiting_diagnosis_approval"},
                          exit_statuses=["awaiting_remediation_approval", "escalated"]),
            AgentCriteria(agent_type=AgentType.REMEDIATION,
                          entry_condition={"status": "awaiting_remediation_approval"},
                          exit_statuses=["validating", "failed"]),
            AgentCriteria(agent_type=AgentType.VALIDATION,
                          entry_condition={"status": "validating"},
                          exit_statuses=["resolved", "diagnosing"]),
        ])

        # ---- Playbooks (API docs for the playbook server — to-do #4 done) ----
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
                     endpoint="/api/v1/pool",
                     params_schema={"service": "string", "max_connections": "int"},
                     rollback_playbook_id=None, risk_tier=RiskTier.MEDIUM),
            Playbook(id="PB-014", name="rolling_restart",
                     description="Rolling restart of service pods",
                     executor="kubernetes_api",
                     endpoint="/apis/apps/v1/deployments/restart",
                     params_schema={"service": "string"},
                     risk_tier=RiskTier.LOW),
            Playbook(id="PB-015", name="scale_deployment",
                     description="Directly patch a Deployment's replica count "
                                  "(distinct from PB-007's HPA min-replica patch)",
                     executor="kubernetes_api",
                     endpoint="/apis/apps/v1/deployments/scale",
                     params_schema={"service": "string", "replicas": "int"},
                     rollback_playbook_id="PB-016", risk_tier=RiskTier.LOW),
            Playbook(id="PB-016", name="restore_deployment",
                     description="Restore a Deployment to its previous replica count",
                     executor="kubernetes_api",
                     endpoint="/apis/apps/v1/deployments/scale",
                     params_schema={"service": "string"},
                     risk_tier=RiskTier.LOW),
            Playbook(id="PB-017", name="rollback_deployment",
                     description="Roll a Deployment back to its previous working image "
                                  "(previous ReplicaSet revision) -- fixes a bad deploy, "
                                  "distinct from PB-015/016's replica-count-only ops",
                     executor="kubernetes_api",
                     endpoint="/apis/apps/v1/deployments/rollback",
                     params_schema={"service": "string"},
                     rollback_playbook_id=None, risk_tier=RiskTier.MEDIUM),
        ])

        db.commit()
        print("Seeded: 7 criteria rows, 7 playbooks "
              "(KB articles live in Confluence, triage-only — see CONFLUENCE_MCP_URL)")
    finally:
        db.close()
