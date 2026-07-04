"""
Remediation Agent — executes the approved runbook's playbooks sequentially.

Runs ONLY after human approval (single gate at runbook level). With no
central orchestrator, the gate is enforced in eligible(): this agent's
polling skips any incident without an APPROVED approval row, no matter
what the criteria table says. apply_output() re-checks as defense in depth.
Holds a resource lock on the service so no other instance mutates it.
On step failure: risk-tier behavior
  LOW    -> auto-rollback executed steps, mark FAILED, alert human
  MEDIUM -> pause in place, mark FAILED, alert human with state
  HIGH   -> (should have had per-step approval; treated like MEDIUM here)

Note: this agent is less "LLM reasoning", more "reliable executor".
The LLM maps runbook steps to playbook invocations with params; execution
itself is deterministic code.

The runbook's step list lives in Confluence (matched_runbook_id is a
Confluence page id, set by triage/diagnosis) — this agent fetches it live
via the MCP connector rather than a local runbooks table. risk_tier is
NOT re-derived here: triage/diagnosis already read it off the page and it's
stored on the incident, which is what failure handling below trusts.
"""

from db.models import (
    Approval,
    ApprovalStatus,
    AgentType,
    Incident,
    IncidentStatus,
    Playbook,
    ResourceLock,
    RiskTier,
)
from .base import BaseAgent, confluence_mcp_server


# --- Playbook executor: swap for real API/MCP calls -------------------------

def execute_playbook(playbook: Playbook, params: dict) -> dict:
    """Invoke playbook over API (to-do #4). Stubbed as success."""
    print(f"    [executor] {playbook.executor} -> {playbook.name}({params})")
    return {"success": True, "detail": f"{playbook.name} executed"}


class RemediationAgent(BaseAgent):
    agent_type = AgentType.REMEDIATION

    def mcp_servers(self) -> list[dict]:
        return [confluence_mcp_server()]

    def system_prompt(self) -> str:
        return """You are the Remediation agent in an SRE incident-automation platform.
A human has ALREADY approved the runbook identified by runbook_id. Use your
Confluence tools to fetch that exact page and read its step list. Then
translate each step into a concrete playbook invocation with parameters,
using the incident context to fill parameter values (service names, counts,
etc.) and only playbook ids from the "playbooks" list provided to you.

Steps without a matching playbook (informational checks, manual verification)
are "manual_check" — mark them and continue. Never invent playbook ids or
parameters not in the schema. If the fetched page's step list references a
playbook id you were not given, treat that step as "manual_check" too.

Respond ONLY with JSON:
{
  "execution_plan": [
    {"order": 1, "step": "description", "playbook_id": "PB-xxx or null",
     "params": {...} , "action": "execute" | "manual_check"}
  ],
  "notes": "anything the human should know"
}"""

    def build_context(self, db, incident: Incident) -> dict:
        playbooks = {
            pb.id: {"id": pb.id, "name": pb.name, "executor": pb.executor,
                    "params_schema": pb.params_schema,
                    "rollback_playbook_id": pb.rollback_playbook_id}
            for pb in db.query(Playbook).filter(Playbook.active == True).all()
        }
        return {
            "incident": {"id": incident.id, "title": incident.title,
                         "description": incident.description,
                         "service": incident.service},
            "runbook_id": incident.matched_runbook_id,
            "runbook_name": incident.matched_runbook_name,
            "risk_tier": incident.matched_runbook_risk_tier.value,
            "playbooks": playbooks,
        }

    # ------------------------------------------------------------------ #

    def eligible(self, db, incident: Incident) -> bool:
        """HARD GATE: never pick up work without an approved runbook."""
        return self.approval_granted(db, incident)

    def approval_granted(self, db, incident: Incident) -> bool:
        apr = (db.query(Approval)
               .filter(Approval.incident_id == incident.id,
                       Approval.status == ApprovalStatus.APPROVED)
               .first())
        return apr is not None

    def acquire_lock(self, db, incident: Incident) -> bool:
        if db.get(ResourceLock, incident.service):
            return False
        db.add(ResourceLock(service=incident.service,
                            incident_id=incident.id,
                            instance_id=self.instance_id))
        db.commit()
        return True

    def release_lock(self, db, incident: Incident):
        lock = db.get(ResourceLock, incident.service)
        if lock and lock.instance_id == self.instance_id:
            db.delete(lock)
            db.commit()

    def apply_output(self, db, incident: Incident, output: dict) -> None:
        if not self.approval_granted(db, incident):
            raise RuntimeError("Remediation invoked without an approved runbook")
        if not self.acquire_lock(db, incident):
            raise RuntimeError(f"Service {incident.service} locked by another remediation")

        executed: list[Playbook] = []
        try:
            incident.status = IncidentStatus.REMEDIATING
            db.commit()

            for item in output["execution_plan"]:
                if item["action"] != "execute" or not item.get("playbook_id"):
                    print(f"    [remediation] manual check: {item['step']}")
                    continue
                pb = db.get(Playbook, item["playbook_id"])
                result = execute_playbook(pb, item.get("params", {}))
                if not result["success"]:
                    self._handle_failure(db, incident, executed, item)
                    return
                executed.append(pb)

            incident.status = IncidentStatus.VALIDATING
        finally:
            self.release_lock(db, incident)

    def _handle_failure(self, db, incident, executed, failed_item):
        """Risk-tier failure behavior — the open decision, now encoded.
        risk_tier comes from what triage/diagnosis read off the Confluence
        runbook page, stored on the incident — not re-derived here."""
        if incident.matched_runbook_risk_tier == RiskTier.LOW:
            for pb in reversed(executed):
                if pb.rollback_playbook_id:
                    rb_pb = db.get(Playbook, pb.rollback_playbook_id)
                    execute_playbook(rb_pb, {"service": incident.service})
            print(f"    [remediation] rolled back {len(executed)} steps")
        # MEDIUM / HIGH: leave state as-is for human inspection
        incident.status = IncidentStatus.FAILED
        print(f"    [remediation] FAILED at: {failed_item['step']} — human alerted")
