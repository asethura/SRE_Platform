"""Smoke test: full pipeline with mocked LLM responses (no API key needed).

No orchestrator — each agent polls the shared session DB itself; the test
drives synchronous poll cycles across the four agents.
"""
from unittest.mock import patch
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import hitl
from db.models import Incident, get_engine, init_db
from db.seed import seed
from integrations.itsm import NullITSMClient
from agents.triage import TriageAgent
from agents.diagnosis import DiagnosisAgent
from agents.remediation import RemediationAgent
from agents.validation import ValidationAgent

engine = get_engine("sqlite:///:memory:")
sf = init_db(engine)
seed(sf)
# NullITSMClient: this test drives specific scenarios via manually created
# incidents, so intake must not mix in unrelated stub ITSM tickets.
agents = [TriageAgent(sf, itsm_client=NullITSMClient()), DiagnosisAgent(sf), RemediationAgent(sf), ValidationAgent(sf)]

MOCKS = {
    "triage_nonissue": {"verdict": "non_issue", "confidence": 0.95,
        "reasoning": "Maintenance window", "runbook_id": None,
        "runbook_name": None, "runbook_risk_tier": None,
        "matched_kb_ids": ["KB-055"]},
    "triage_known": {"verdict": "known_issue", "confidence": 0.9,
        "reasoning": "Matches KB-041", "runbook_id": "RB-012",
        "runbook_name": "High CPU — autoscaling lag", "runbook_risk_tier": "low",
        "matched_kb_ids": ["KB-041"]},
    "triage_unknown": {"verdict": "unknown_issue", "confidence": 0.6,
        "reasoning": "No pattern match", "runbook_id": None,
        "runbook_name": None, "runbook_risk_tier": None, "matched_kb_ids": []},
    "diagnosis": {"outcome": "root_cause_with_remediation",
        "root_cause": "DB pool exhaustion", "evidence": ["pool exhausted logs"],
        "runbook_id": "RB-019", "runbook_name": "DB connection pool exhaustion",
        "runbook_risk_tier": "medium", "suggested_fix": None, "confidence": 0.85},
    "remediation_rb012": {"execution_plan": [
        {"order": 1, "step": "Check HPA", "playbook_id": None, "params": {}, "action": "manual_check"},
        {"order": 2, "step": "Scale pods", "playbook_id": "PB-007",
         "params": {"service": "payment-service", "min_pods": 3}, "action": "execute"}], "notes": ""},
    "remediation_rb019": {"execution_plan": [
        {"order": 1, "step": "Raise pool", "playbook_id": "PB-011",
         "params": {"service": "payment-service", "max_connections": 50}, "action": "execute"},
        {"order": 2, "step": "Restart", "playbook_id": "PB-014",
         "params": {"service": "payment-service"}, "action": "execute"}], "notes": ""},
    "validation": {"result": "pass", "checks": [
        {"metric": "error_rate_5xx", "value": 0.001, "threshold": 0.01, "ok": True}],
        "summary": "Healthy"},
}

def mk(inc_kwargs):
    db = sf()
    inc = Incident(**inc_kwargs); db.add(inc); db.commit(); iid = inc.id; db.close()
    return iid

def status_of(iid):
    db = sf(); s = db.get(Incident, iid).status.value; db.close(); return s

def poll_cycle(max_rounds=10):
    for _ in range(max_rounds):
        if not any(agent.poll_once() for agent in agents):
            return

# Scenario 1: non-issue
i1 = mk(dict(title="Maint alert", description="alert during scheduled maintenance window database migration", service="orders-service"))
with patch("agents.base.BaseAgent.run_llm", return_value=MOCKS["triage_nonissue"]):
    poll_cycle()
assert status_of(i1) == "closed_non_issue", status_of(i1)
print("PASS scenario 1: non-issue ->", status_of(i1))

# Scenario 2: known issue full path
i2 = mk(dict(title="Payment 5xx", description="payment service 5xx spike during pod scaling deployment", service="payment-service"))
responses = [MOCKS["triage_known"]]
with patch("agents.base.BaseAgent.run_llm", side_effect=lambda ctx: responses.pop(0)):
    poll_cycle()
assert status_of(i2) == "awaiting_approval", status_of(i2)
responses = [MOCKS["remediation_rb012"], MOCKS["validation"]]
with patch("agents.base.BaseAgent.run_llm", side_effect=lambda ctx: responses.pop(0)):
    # Remediation polls BEFORE approval: must find nothing (hard gate)
    assert RemediationAgent(sf).poll_once() == [], "remediation ran without approval"
    hitl.approve(sf, i2, "tester")
    poll_cycle()
assert status_of(i2) == "resolved", status_of(i2)
print("PASS scenario 2: known issue -> approval -> remediation -> validation ->", status_of(i2))

# Scenario 3: unknown -> diagnosis -> approval -> resolved
i3 = mk(dict(title="Checkout timeouts", description="upstream timeout errors calling stripe api queue depth growing", service="payment-service"))
responses = [MOCKS["triage_unknown"], MOCKS["diagnosis"]]
with patch("agents.base.BaseAgent.run_llm", side_effect=lambda ctx: responses.pop(0)):
    poll_cycle()
assert status_of(i3) == "awaiting_approval", status_of(i3)
hitl.approve(sf, i3, "tester")
responses = [MOCKS["remediation_rb019"], MOCKS["validation"]]
with patch("agents.base.BaseAgent.run_llm", side_effect=lambda ctx: responses.pop(0)):
    poll_cycle()
assert status_of(i3) == "resolved", status_of(i3)
print("PASS scenario 3: unknown -> diagnosis -> remediation ->", status_of(i3))
print("\nAll 3 pipeline paths verified — no orchestrator, agents self-polled.")
