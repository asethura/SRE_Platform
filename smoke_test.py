"""Smoke test: full pipeline with mocked LLM responses (no API key needed).

No orchestrator — each agent polls the shared session DB itself; the test
drives synchronous poll cycles across the four agents. Two approval gates
now exist (Approval.stage): DIAGNOSIS (root cause + steps) and REMEDIATION
(exact playbook mapping) — this test exercises both, plus the pre-approval
hard-gate check for each. agents.remediation.execute_playbook() makes a
real HTTP call to PLAYBOOK_SERVER_URL in production, so it's mocked here
too, same as run_llm.
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
        "reasoning": "Maintenance window", "remediation_steps": None,
        "matched_kb_ids": ["KB-055"]},
    "triage_known": {"verdict": "known_issue", "confidence": 0.9,
        "reasoning": "Matches KB-041", "matched_kb_ids": ["KB-041"],
        "remediation_steps": ["Scale pods via HPA", "Verify pod count recovers"]},
    "triage_unknown": {"verdict": "unknown_issue", "confidence": 0.6,
        "reasoning": "No pattern match", "remediation_steps": None, "matched_kb_ids": []},
    "diagnosis": {"outcome": "diagnosed",
        "root_cause": "DB pool exhaustion", "evidence": ["pool exhausted logs"],
        "remediation_steps": ["Raise DB connection pool limit", "Restart the service"],
        "confidence": 0.85},
    "remediation_plan_known": {"execution_plan": [
        {"order": 1, "step": "Check HPA", "playbook_id": None, "params": {}, "action": "manual_check"},
        {"order": 2, "step": "Scale pods", "playbook_id": "PB-007",
         "params": {"service": "payment-service", "min_pods": 3}, "action": "execute"}], "notes": ""},
    "remediation_plan_diagnosed": {"execution_plan": [
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

# Scenario 2: known issue -- skips gate 1 (no diagnosis), still needs gate 2
# NOTE: a single agents[0] (triage) poll, not poll_cycle() -- REMEDIATING is
# ungated (no gate 1 to block it), so a full poll_cycle() would let
# RemediationAgent immediately pick the incident up in the same round and
# consume this same triage_known mock instead of its own planning mock.
i2 = mk(dict(title="Payment 5xx", description="payment service 5xx spike during pod scaling deployment", service="payment-service"))
with patch("agents.base.BaseAgent.run_llm", return_value=MOCKS["triage_known"]):
    agents[0].poll_once()
assert status_of(i2) == "remediating", status_of(i2)

with patch("agents.base.BaseAgent.run_llm", return_value=MOCKS["remediation_plan_known"]):
    assert RemediationAgent(sf).poll_once() != [], "planning did not run pre-approval"
assert status_of(i2) == "awaiting_remediation_approval", status_of(i2)

# Hard gate: execute must not run without gate-2 approval (no run_llm patch
# needed here -- eligible() blocks it before any LLM call happens)
assert RemediationAgent(sf).poll_once() == [], "remediation executed without approval"

hitl.approve(sf, i2, "tester")
with patch("agents.base.BaseAgent.run_llm", return_value=MOCKS["validation"]), \
     patch("agents.remediation.execute_playbook", return_value={"success": True, "detail": "stubbed"}):
    poll_cycle()  # execute phase bypasses run_llm entirely; only validation calls it
assert status_of(i2) == "resolved", status_of(i2)
print("PASS scenario 2: known issue -> planning -> gate 2 -> execute -> validation ->", status_of(i2))

# Scenario 3: unknown -> diagnosis -> gate 1 -> planning -> gate 2 -> execute -> resolved
i3 = mk(dict(title="Checkout timeouts", description="upstream timeout errors calling stripe api queue depth growing", service="payment-service"))
responses = [MOCKS["triage_unknown"], MOCKS["diagnosis"]]
with patch("agents.base.BaseAgent.run_llm", side_effect=lambda ctx: responses.pop(0)):
    poll_cycle()
assert status_of(i3) == "awaiting_diagnosis_approval", status_of(i3)

hitl.approve(sf, i3, "tester")  # gate 1: root cause + steps

with patch("agents.base.BaseAgent.run_llm", return_value=MOCKS["remediation_plan_diagnosed"]):
    assert RemediationAgent(sf).poll_once() != [], "planning did not run after gate 1 approval"
assert status_of(i3) == "awaiting_remediation_approval", status_of(i3)

assert RemediationAgent(sf).poll_once() == [], "remediation executed without gate 2 approval"

hitl.approve(sf, i3, "tester")  # gate 2: exact playbook mapping
with patch("agents.base.BaseAgent.run_llm", return_value=MOCKS["validation"]), \
     patch("agents.remediation.execute_playbook", return_value={"success": True, "detail": "stubbed"}):
    poll_cycle()
assert status_of(i3) == "resolved", status_of(i3)
print("PASS scenario 3: unknown -> diagnosis -> gate 1 -> planning -> gate 2 -> execute ->", status_of(i3))
print("\nAll 3 pipeline paths verified — no orchestrator, agents self-polled, two approval gates.")
