"""
Remediation Agent — two phases, same class, same "multiple agent_criteria
rows route to one agent" idiom TriageAgent already uses for NEW+TRIAGING:

  PLANNING (incident.status in {REMEDIATING, AWAITING_DIAGNOSIS_APPROVAL}):
    No Confluence, no runbook — maps incident.root_cause /
    incident.remediation_steps (written by triage's known_issue path or by
    diagnosis, post gate-1 approval) onto the `playbooks` catalog via an
    LLM call, constrained to playbook ids actually in that catalog. The
    catalog is both embedded directly in build_context() (guaranteed
    present) AND live-discoverable via playbook-mcp (mcp_servers() below) —
    the model may call list_playbooks/describe_playbook, which are
    read-only by construction; it can never trigger real execution during
    this phase. Writes the resulting execution_plan onto a NEW
    Approval(stage=REMEDIATION) row and asks for gate 2.
  EXECUTE (incident.status == AWAITING_REMEDIATION_APPROVAL, gated on an
    APPROVED REMEDIATION-stage Approval):
    Executes the EXACT plan a human already approved — no LLM call, no
    re-generation, see run_llm() override below. Less "LLM reasoning", more
    "reliable executor", same character remediation had before this split.

On step failure: risk-tier behavior (risk_tier computed once during
planning, from the max risk tier among playbooks actually used — stored on
the REMEDIATION-stage Approval, not re-derived here):
  LOW    -> auto-rollback executed steps, mark FAILED, alert human
  MEDIUM -> pause in place, mark FAILED, alert human with state
  HIGH   -> (should have had per-step approval; treated like MEDIUM here)
"""

import os

import requests

from db.models import (
    Approval,
    ApprovalStage,
    ApprovalStatus,
    AgentType,
    Incident,
    IncidentStatus,
    Playbook,
    ResourceLock,
    RiskTier,
)
from .base import BaseAgent, playbook_mcp_server

_RISK_ORDER = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}

_PLANNING_STATUSES = {
    IncidentStatus.REMEDIATING,
    IncidentStatus.AWAITING_DIAGNOSIS_APPROVAL,
}

# Playbooks are API docs, not local code: each Playbook row's `endpoint`
# (a relative path) + `params_schema` document one API on a single
# playbook server -- that server owns the actual remediation mechanism
# (Kubernetes API, a config service, a script runner, whatever `executor`
# describes). This process is just an HTTP client calling the documented
# endpoint; it never touches Kubernetes or anything else directly.
PLAYBOOK_SERVER_URL = os.environ.get("PLAYBOOK_SERVER_URL", "")
PLAYBOOK_SERVER_TOKEN = os.environ.get("PLAYBOOK_SERVER_TOKEN")
PLAYBOOK_CALL_TIMEOUT = float(os.environ.get("PLAYBOOK_CALL_TIMEOUT", "30"))


def execute_playbook(playbook: Playbook, params: dict) -> dict:
    """POST params to the playbook server's documented endpoint for this
    playbook. Success is the HTTP status code alone (2xx) -- the server's
    response body isn't interpreted. Network errors, timeouts, and non-2xx
    responses are all failures."""
    if not PLAYBOOK_SERVER_URL:
        raise RuntimeError(
            "PLAYBOOK_SERVER_URL is not set — required to execute playbooks."
        )
    url = f"{PLAYBOOK_SERVER_URL.rstrip('/')}{playbook.endpoint}"
    headers = {"Authorization": f"Bearer {PLAYBOOK_SERVER_TOKEN}"} if PLAYBOOK_SERVER_TOKEN else {}
    print(f"    [executor] POST {url} {params}")
    try:
        resp = requests.post(url, json=params, headers=headers, timeout=PLAYBOOK_CALL_TIMEOUT)
    except requests.RequestException as e:
        return {"success": False, "detail": f"{playbook.name}: request failed: {e}"}
    if 200 <= resp.status_code < 300:
        return {"success": True, "detail": f"{playbook.name}: {resp.status_code}"}
    return {"success": False, "detail": f"{playbook.name}: {resp.status_code} {resp.text[:200]}"}


class RemediationAgent(BaseAgent):
    agent_type = AgentType.REMEDIATION

    def mcp_servers(self) -> list[dict]:
        # Only ever consulted during planning: run_llm()'s execute-phase
        # branch (below) returns before ever calling super().run_llm(), and
        # self.mcp_servers() is only read from inside BaseAgent.run_llm() --
        # so this is structurally unreachable during execute already. Kept
        # unconditional (not phase-branched) because even if some future
        # edit to run_llm() broke that invariant, exposing a read-only
        # discovery server during execute would be harmless, not unsafe.
        return [playbook_mcp_server()]

    # ------------------------------------------------------------------ #
    # Phase discrimination
    # ------------------------------------------------------------------ #

    @staticmethod
    def _phase(incident: Incident) -> str:
        return "planning" if incident.status in _PLANNING_STATUSES else "execute"

    def _latest_approval(self, db, incident: Incident, stage: ApprovalStage,
                          status: ApprovalStatus = None) -> Approval | None:
        q = (db.query(Approval)
             .filter(Approval.incident_id == incident.id,
                     Approval.stage == stage))
        if status is not None:
            q = q.filter(Approval.status == status)
        return q.order_by(Approval.created_at.desc()).first()

    def eligible(self, db, incident: Incident) -> bool:
        """Per-phase hard gate:
        - REMEDIATING (triage known_issue): no gate 1 exists, always eligible.
        - AWAITING_DIAGNOSIS_APPROVAL: eligible once gate 1 is APPROVED.
        - AWAITING_REMEDIATION_APPROVAL: eligible once gate 2 is APPROVED.
        """
        if incident.status == IncidentStatus.REMEDIATING:
            return True
        if incident.status == IncidentStatus.AWAITING_DIAGNOSIS_APPROVAL:
            return self._latest_approval(db, incident, ApprovalStage.DIAGNOSIS,
                                          ApprovalStatus.APPROVED) is not None
        if incident.status == IncidentStatus.AWAITING_REMEDIATION_APPROVAL:
            return self._latest_approval(db, incident, ApprovalStage.REMEDIATION,
                                          ApprovalStatus.APPROVED) is not None
        return False

    # ------------------------------------------------------------------ #
    # LLM call — execute phase must run the EXACT approved plan, never a
    # freshly-generated one, so it never touches the LLM at all.
    # ------------------------------------------------------------------ #

    def run_llm(self, context: dict) -> dict:
        if context.get("phase") == "execute":
            return {"execution_plan": context["approved_plan"],
                     "notes": "executing pre-approved plan verbatim"}
        return super().run_llm(context)

    def system_prompt(self) -> str:
        # Only the planning phase ever reaches the LLM (see run_llm above).
        return """You are the Remediation agent in an SRE incident-automation platform.
Diagnosis (or triage, for an already-known issue) has already identified a
root cause and proposed remediation_steps in plain language — a human has
already reviewed and approved that reasoning. Your job is to map each step
onto a concrete playbook invocation with parameters, using the incident
context to fill parameter values (service names, counts, etc.) and only
playbook ids from the "playbooks" list provided to you.

Steps without a matching playbook (informational checks, manual verification,
or anything not covered by the catalog) are "manual_check" — mark them and
continue. Never invent playbook ids or parameters not in the schema.

Respond ONLY with JSON:
{
  "execution_plan": [
    {"order": 1, "step": "description", "playbook_id": "PB-xxx or null",
     "params": {...} , "action": "execute" | "manual_check"}
  ],
  "notes": "anything the human should know"
}"""

    def build_context(self, db, incident: Incident) -> dict:
        phase = self._phase(incident)
        if phase == "execute":
            apr = self._latest_approval(db, incident, ApprovalStage.REMEDIATION,
                                         ApprovalStatus.APPROVED)
            return {
                "phase": "execute",
                "approved_plan": apr.proposed_plan,
                "risk_tier": apr.risk_tier.value if apr.risk_tier else None,
            }

        playbooks = {
            pb.id: {"id": pb.id, "name": pb.name, "executor": pb.executor,
                    "params_schema": pb.params_schema,
                    "rollback_playbook_id": pb.rollback_playbook_id}
            for pb in db.query(Playbook).filter(Playbook.active == True).all()
        }
        return {
            "phase": "planning",
            "incident": {"id": incident.id, "title": incident.title,
                         "description": incident.description,
                         "service": incident.service},
            "root_cause": incident.root_cause,
            "remediation_steps": incident.remediation_steps,
            "playbooks": playbooks,
        }

    # ------------------------------------------------------------------ #

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
        if self._phase(incident) == "planning":
            self._apply_planning(db, incident, output)
        else:
            self._apply_execute(db, incident, output)

    def _apply_planning(self, db, incident: Incident, output: dict) -> None:
        plan = []
        validated_playbooks = []
        for item in output["execution_plan"]:
            if item.get("action") == "execute" and item.get("playbook_id"):
                pb = db.get(Playbook, item["playbook_id"])
                if pb is None or not pb.active:
                    # Don't trust the LLM's id blindly — downgrade rather
                    # than let a fabricated/inactive playbook id through to
                    # what the human approves and what execute later runs.
                    item = {**item, "action": "manual_check", "playbook_id": None}
                else:
                    validated_playbooks.append(pb)
            plan.append(item)

        if not validated_playbooks:
            # No playbook in the catalog covers any proposed step — nothing
            # for a human to approve, escalate instead of a no-op plan.
            incident.status = IncidentStatus.ESCALATED
            return

        risk_tier = max((pb.risk_tier for pb in validated_playbooks), key=_RISK_ORDER.get)
        db.add(Approval(
            incident_id=incident.id,
            stage=ApprovalStage.REMEDIATION,
            proposed_plan=plan,
            risk_tier=risk_tier,
            summary=(f"{len(validated_playbooks)} executable step(s), risk: {risk_tier.value} | "
                     f"{output.get('notes', '')}"),
        ))
        incident.status = IncidentStatus.AWAITING_REMEDIATION_APPROVAL

    def _apply_execute(self, db, incident: Incident, output: dict) -> None:
        if not self.acquire_lock(db, incident):
            raise RuntimeError(f"Service {incident.service} locked by another remediation")

        apr = self._latest_approval(db, incident, ApprovalStage.REMEDIATION,
                                     ApprovalStatus.APPROVED)
        risk_tier = apr.risk_tier if apr else None

        executed: list[Playbook] = []
        try:
            for item in output["execution_plan"]:
                if item["action"] != "execute" or not item.get("playbook_id"):
                    print(f"    [remediation] manual check: {item['step']}")
                    continue
                pb = db.get(Playbook, item["playbook_id"])
                result = execute_playbook(pb, item.get("params", {}))
                if not result["success"]:
                    self._handle_failure(db, incident, executed, item, risk_tier)
                    return
                executed.append(pb)

            incident.status = IncidentStatus.VALIDATING
        finally:
            self.release_lock(db, incident)

    def _handle_failure(self, db, incident, executed, failed_item, risk_tier):
        """Risk-tier failure behavior — risk_tier comes from the approved
        REMEDIATION-stage Approval (max tier across playbooks actually used),
        not re-derived here."""
        if risk_tier == RiskTier.LOW:
            for pb in reversed(executed):
                if pb.rollback_playbook_id:
                    rb_pb = db.get(Playbook, pb.rollback_playbook_id)
                    execute_playbook(rb_pb, {"service": incident.service})
            print(f"    [remediation] rolled back {len(executed)} steps")
        # MEDIUM / HIGH: leave state as-is for human inspection
        incident.status = IncidentStatus.FAILED
        print(f"    [remediation] FAILED at: {failed_item['step']} — human alerted")
