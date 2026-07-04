"""ITSM client — where incidents actually come from.

Real deployment: wraps the PagerDuty/ServiceNow/Jira Service Mgmt API,
paginating through the open-incident queue for this team/service.
`StubITSMClient` below fakes that queue for local dev — swap it for a real
client that implements the same `fetch_open_incidents()` contract.
"""

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


class Severity(str, enum.Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


SEVERITY_RANK = {Severity.P1: 0, Severity.P2: 1, Severity.P3: 2, Severity.P4: 3}


@dataclass
class ITSMTicket:
    """One open ticket as returned by the ITSM API."""
    ticket_id: str
    title: str
    description: str
    service: str
    severity: str          # P1..P4
    source: str            # pagerduty | datadog | manual
    reported_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def priority_key(ticket: ITSMTicket) -> tuple:
    """Prioritization criteria: highest severity first, then oldest first (FIFO
    within a severity band) so a queued P1 always jumps a fresher P3."""
    return (SEVERITY_RANK.get(ticket.severity, len(SEVERITY_RANK)), ticket.reported_at)


class ITSMClient:
    """Contract every ITSM backend must satisfy."""

    def fetch_open_incidents(self) -> list[ITSMTicket]:
        raise NotImplementedError


class StubITSMClient(ITSMClient):
    """Fakes an open-incident queue for local dev/demo — no network calls.

    Returns a fixed, deterministic set of tickets so repeated polls behave
    like a real queue (same tickets until triage consumes/dedupes them by
    ticket_id in the caller).
    """

    def __init__(self):
        now = datetime.now(timezone.utc)
        self._queue = [
            ITSMTicket(
                ticket_id="ITSM-3001",
                title="Payment 5xx",
                description="payment service 5xx spike during pod scaling deployment",
                service="payment-service",
                severity=Severity.P2,
                source="pagerduty",
                reported_at=now - timedelta(minutes=12),
            ),
            ITSMTicket(
                ticket_id="ITSM-3002",
                title="Maint alert",
                description="alert during scheduled maintenance window database migration",
                service="orders-service",
                severity=Severity.P4,
                source="datadog",
                reported_at=now - timedelta(minutes=40),
            ),
            ITSMTicket(
                ticket_id="ITSM-3003",
                title="Checkout timeouts",
                description="upstream timeout errors calling stripe api queue depth growing",
                service="payment-service",
                severity=Severity.P1,
                source="pagerduty",
                reported_at=now - timedelta(minutes=3),
            ),
        ]

    def fetch_open_incidents(self) -> list[ITSMTicket]:
        return list(self._queue)


class NullITSMClient(ITSMClient):
    """Empty queue — for tests/demos that manage incidents manually and don't
    want intake mixing in unrelated tickets."""

    def fetch_open_incidents(self) -> list[ITSMTicket]:
        return []
