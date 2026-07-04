"""
Shared Session Database — the spine of the SRE platform.

Every agent is stateless: it reads full context from these tables,
does its work, writes its output back, and terminates.

Tables:
  incidents        — the incident itself (mirrors ITSM ticket)
  agent_runs       — one row per agent execution (output, status, timing)
  agent_criteria   — entry/exit criteria table (the orchestration contract)
  playbooks        — executable artifacts: HOW to do it (API/script)
  approvals        — human-in-the-loop approval records
  feedback         — human feedback on wrong agent conclusions
  resource_locks   — prevents two remediation instances touching same service

Runbooks and KB articles are NOT tables here — they live in Confluence.
Triage/diagnosis/remediation agents reach them live via the Confluence MCP
server (see agents/base.py: confluence_mcp_server(), BaseAgent.mcp_servers()).
Only the identifiers and risk tier the model extracts from a matched
Confluence page are persisted, on Incident/Approval, for audit and so
remediation doesn't have to re-derive risk tier from scratch.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker


def utcnow():
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums — these ARE your platform vocabulary. Agents route on these values.
# ---------------------------------------------------------------------------

class IncidentStatus(str, enum.Enum):
    NEW = "new"
    TRIAGING = "triaging"
    AWAITING_APPROVAL = "awaiting_approval"
    DIAGNOSING = "diagnosing"
    REMEDIATING = "remediating"
    VALIDATING = "validating"
    RESOLVED = "resolved"
    CLOSED_NON_ISSUE = "closed_non_issue"
    ESCALATED = "escalated"       # unable to diagnose / needs human
    FAILED = "failed"             # remediation failed, rolled back


TERMINAL_STATUSES = {
    IncidentStatus.RESOLVED,
    IncidentStatus.CLOSED_NON_ISSUE,
    IncidentStatus.ESCALATED,
    IncidentStatus.FAILED,
}


class AgentType(str, enum.Enum):
    TRIAGE = "triage"
    DIAGNOSIS = "diagnosis"
    REMEDIATION = "remediation"
    VALIDATION = "validation"


class RunStatus(str, enum.Enum):
    CLAIMED = "claimed"           # instance checked out the work (lock)
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TriageVerdict(str, enum.Enum):
    NON_ISSUE = "non_issue"
    KNOWN_ISSUE = "known_issue"       # runbook exists -> remediation
    UNKNOWN_ISSUE = "unknown_issue"   # -> diagnosis


class DiagnosisOutcome(str, enum.Enum):
    ROOT_CAUSE_WITH_REMEDIATION = "root_cause_with_remediation"
    ROOT_CAUSE_NO_REMEDIATION = "root_cause_no_remediation"
    UNABLE_TO_DIAGNOSE = "unable_to_diagnose"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RiskTier(str, enum.Enum):
    LOW = "low"        # auto-rollback on failure
    MEDIUM = "medium"  # pause + alert human on failure
    HIGH = "high"      # per-step approval required


# ---------------------------------------------------------------------------
# Core tables
# ---------------------------------------------------------------------------

class Incident(Base):
    """Mirrors the ITSM ticket. The unit of work flowing through the graph."""
    __tablename__ = "incidents"

    id = Column(String, primary_key=True, default=lambda: new_id("INC"))
    itsm_ticket_id = Column(String, nullable=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    source = Column(String, default="manual")     # pagerduty | datadog | manual
    service = Column(String, index=True)           # affected service
    severity = Column(String, default="P3")        # P1..P4
    status = Column(Enum(IncidentStatus), default=IncidentStatus.NEW, index=True)

    # Routing context written by agents as they run
    triage_verdict = Column(Enum(TriageVerdict), nullable=True)
    diagnosis_outcome = Column(Enum(DiagnosisOutcome), nullable=True)

    # Runbook match — the runbook itself lives in Confluence; only the
    # Confluence page id + what triage/diagnosis extracted from it are kept
    # here, so remediation has an authoritative risk_tier without needing to
    # re-fetch and re-judge the page itself.
    matched_runbook_id = Column(String, nullable=True)
    matched_runbook_name = Column(String, nullable=True)
    matched_runbook_risk_tier = Column(Enum(RiskTier), nullable=True)

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    resolved_at = Column(DateTime, nullable=True)

    runs = relationship("AgentRun", back_populates="incident")
    approvals = relationship("Approval", back_populates="incident")


class AgentRun(Base):
    """One row per agent execution — the audit trail AND the handoff medium.

    The next agent reads the previous agent's `output` JSON from here.
    """
    __tablename__ = "agent_runs"

    id = Column(String, primary_key=True, default=lambda: new_id("RUN"))
    incident_id = Column(String, ForeignKey("incidents.id"), nullable=False, index=True)
    agent_type = Column(Enum(AgentType), nullable=False)
    instance_id = Column(String, nullable=False)   # which pool instance ran this
    status = Column(Enum(RunStatus), default=RunStatus.CLAIMED)

    # Idempotency guard: "{incident_id}:{agent_type}" while the run is active,
    # NULL once finished. The unique index means only one instance can hold a
    # claim, while still allowing repeat runs of the same agent on the same
    # incident (e.g. re-diagnosis after a failed validation).
    active_claim = Column(String, nullable=True, unique=True)

    input_context = Column(JSON, nullable=True)    # what the agent saw
    output = Column(JSON, nullable=True)           # structured verdict/result
    error = Column(Text, nullable=True)

    started_at = Column(DateTime, default=utcnow)
    completed_at = Column(DateTime, nullable=True)

    incident = relationship("Incident", back_populates="runs")


class AgentCriteria(Base):
    """Entry/exit criteria table — the orchestration contract (to-do #13).

    entry_condition / exit_condition are JSON expressions evaluated by the
    orchestrator against the incident row, e.g.:
      {"status": "new"}                       -> triage may start
      {"triage_verdict": "known_issue"}       -> remediation may start
    Editable via human-in-the-loop approvals (to-do #14).
    """
    __tablename__ = "agent_criteria"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_type = Column(Enum(AgentType), nullable=False)
    entry_condition = Column(JSON, nullable=False)
    exit_statuses = Column(JSON, nullable=False)   # statuses agent may set on exit
    max_runtime_seconds = Column(Integer, default=300)
    enabled = Column(Boolean, default=True)
    updated_by = Column(String, default="system")  # HITL edits recorded here
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Knowledge layer — playbooks only. Runbooks and KB articles live in
# Confluence and are reached live via MCP (see agents/base.py).
# ---------------------------------------------------------------------------

class Playbook(Base):
    """Executable artifact — HOW to do it. Invoked over API (to-do #4)."""
    __tablename__ = "playbooks"

    id = Column(String, primary_key=True)          # e.g. PB-007
    name = Column(String, nullable=False)
    description = Column(Text)
    executor = Column(String, nullable=False)       # kubernetes_api | http | script
    endpoint = Column(String, nullable=True)        # API endpoint or script path
    params_schema = Column(JSON, default=dict)      # expected params
    rollback_playbook_id = Column(String, nullable=True)  # inverse action if exists
    risk_tier = Column(Enum(RiskTier), default=RiskTier.LOW)
    active = Column(Boolean, default=True)


# ---------------------------------------------------------------------------
# Human in the loop
# ---------------------------------------------------------------------------

class Approval(Base):
    """Single approval gate at runbook level before remediation executes."""
    __tablename__ = "approvals"

    id = Column(String, primary_key=True, default=lambda: new_id("APR"))
    incident_id = Column(String, ForeignKey("incidents.id"), nullable=False)
    runbook_id = Column(String, nullable=False)     # Confluence page id — no local runbooks table
    status = Column(Enum(ApprovalStatus), default=ApprovalStatus.PENDING, index=True)
    summary = Column(Text)                          # what human sees: fix, risk, ETA
    decided_by = Column(String, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    reject_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    incident = relationship("Incident", back_populates="approvals")


class Feedback(Base):
    """Human corrections that feed the learning loop.

    destination routes where the learning goes:
      kb_article | runbook_update | prompt_update
    prompt_update requires review (governance) before applying.
    """
    __tablename__ = "feedback"

    id = Column(String, primary_key=True, default=lambda: new_id("FB"))
    incident_id = Column(String, ForeignKey("incidents.id"), nullable=False)
    agent_type = Column(Enum(AgentType), nullable=False)
    agent_run_id = Column(String, ForeignKey("agent_runs.id"), nullable=True)
    correct_verdict = Column(String, nullable=False)
    reason = Column(Text, nullable=False)
    destination = Column(String, default="kb_article")
    reviewed = Column(Boolean, default=False)       # governance gate
    submitted_by = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow)


class ResourceLock(Base):
    """Prevents two remediation instances mutating the same service."""
    __tablename__ = "resource_locks"

    service = Column(String, primary_key=True)
    incident_id = Column(String, nullable=False)
    instance_id = Column(String, nullable=False)
    acquired_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Engine / session factory
# ---------------------------------------------------------------------------

def get_engine(url: str = "sqlite:///sre_platform.db"):
    """SQLite for quick launch; swap url for Postgres in production:
    postgresql+psycopg://user:pass@host/sre
    """
    return create_engine(url, echo=False)


def init_db(engine):
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
