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

import requests


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
    service: str | None    # None when the ITSM ticket has no reliable service field
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

    def close_ticket(self, ticket_id: str, comment: str = None) -> None:
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

    def close_ticket(self, ticket_id: str, comment: str = None) -> None:
        print(f"  [itsm-stub] close {ticket_id}: {comment}")


class NullITSMClient(ITSMClient):
    """Empty queue — for tests/demos that manage incidents manually and don't
    want intake mixing in unrelated tickets."""

    def fetch_open_incidents(self) -> list[ITSMTicket]:
        return []

    def close_ticket(self, ticket_id: str, comment: str = None) -> None:
        pass


def _adf_to_text(node) -> str:
    """Flatten a Jira Cloud Atlassian Document Format description into plain
    text. ADF is a nested JSON tree (paragraphs/text runs), not a string —
    the v3 API returns it that way for the `description` field."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    parts = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text":
                parts.append(n.get("text", ""))
            for child in n.get("content") or []:
                walk(child)
            if n.get("type") in ("paragraph", "heading"):
                parts.append("\n")
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(node)
    return "".join(parts).strip()


class JiraServiceManagementITSMClient(ITSMClient):
    """Pulls open JSM incidents from a Jira Cloud project via the classic
    Jira Platform JQL search API (`/rest/api/3/search/jql`). Auth is HTTP
    Basic with an Atlassian account email + API token, per Jira Cloud's REST
    API convention (see id.atlassian.com/manage-profile/security/api-tokens),
    scoped with read:jira-work + read:jira-user.

    Note this is deliberately NOT the dedicated JSM Incidents REST API
    (`/jsm/incidents/...`, scopes read:incident:jira-service-management) —
    that API only supports get-one-by-id/create/delete, no list or search
    operation, so it can't back a "find open incidents" poll. A JSM Incident
    is still a regular Jira issue under the hood (issue type "[System]
    Incident" for the out-of-the-box feature), so the classic search API is
    what actually lists them.

    Scoped API tokens must call through the api.atlassian.com gateway
    (`/ex/jira/{cloudId}/...`) rather than the site's own domain directly —
    the cloud id is resolved once at construction via the unauthenticated
    `{base_url}/_edge/tenant_info` endpoint.

    `statusCategory != Done` is used instead of a specific status name so
    this works regardless of how a project's workflow renamed its statuses.
    Fetches a single page (`max_results`, default 50) — triage's own
    `priority_key()` sort + intake cap (10/cycle) already means only the
    highest-priority tickets get consumed each poll, so paginating through
    a large backlog isn't needed for that to work correctly.
    """

    _PRIORITY_MAP = {
        "highest": Severity.P1, "blocker": Severity.P1, "critical": Severity.P1,
        "high": Severity.P2,
        "medium": Severity.P3,
        "low": Severity.P4, "lowest": Severity.P4,
    }

    _INCIDENT_ISSUE_TYPE = "[System] Incident"
    
    def __init__(self, base_url: str, email: str, api_token: str,
                 project_key: str = None, jql: str = None, max_results: int = 50):
        site_url = base_url.rstrip("/")
        tenant_info = requests.get(f"{site_url}/_edge/tenant_info", timeout=10)
        tenant_info.raise_for_status()
        cloud_id = tenant_info.json()["cloudId"]
        self.api_base = f"https://api.atlassian.com/ex/jira/{cloud_id}"

        self.auth = (email, api_token)
        self.max_results = max_results
        if jql:
            self.jql = jql
        elif project_key:
            self.jql = (
                f'project = "{project_key}" AND issuetype = "{self._INCIDENT_ISSUE_TYPE}" '
                f"AND statusCategory != Done ORDER BY created ASC"
            )
        else:
            raise ValueError(
                "JiraServiceManagementITSMClient needs project_key or jql"
            )

    def fetch_open_incidents(self) -> list[ITSMTicket]:
        resp = requests.post(
            f"{self.api_base}/rest/api/3/search/jql",
            auth=self.auth,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={
                "jql": self.jql,
                "maxResults": self.max_results,
                "fields": ["summary", "description", "priority", "created",
                           "components", "project"],
            },
            timeout=10,
        )
        resp.raise_for_status()
        return [self._to_ticket(issue) for issue in resp.json().get("issues", [])]

    def close_ticket(self, ticket_id: str, comment: str = None) -> None:
        """Transition the issue to whatever status in its workflow is
        category "Done" -- statuses are per-project/workflow-configurable,
        so there's no fixed status name to target, only the fixed
        statusCategory every Jira workflow's terminal status maps to."""
        resp = requests.get(
            f"{self.api_base}/rest/api/3/issue/{ticket_id}/transitions",
            auth=self.auth,
            headers={"Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        transitions = resp.json().get("transitions", [])
        done_transitions = [
            t for t in transitions
            if (t.get("to") or {}).get("statusCategory", {}).get("key") == "done"
        ]
        if not done_transitions:
            raise RuntimeError(
                f"{ticket_id}: no transition to a Done-category status found "
                f"(available: {[t['name'] for t in transitions]})"
            )
        # A workflow can expose more than one Done-category transition (e.g.
        # both "Close" -> Closed and "Cancel" -> Canceled) -- statusCategory
        # alone can't tell a successful resolution from an abandoned ticket,
        # so prefer whichever transition doesn't read as a cancellation.
        done = next(
            (t for t in done_transitions if "cancel" not in t["name"].lower()),
            done_transitions[0],
        )

        if comment:
            requests.post(
                f"{self.api_base}/rest/api/3/issue/{ticket_id}/comment",
                auth=self.auth,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={"body": {
                    "type": "doc", "version": 1,
                    "content": [{"type": "paragraph",
                                 "content": [{"type": "text", "text": comment}]}],
                }},
                timeout=10,
            ).raise_for_status()

        requests.post(
            f"{self.api_base}/rest/api/3/issue/{ticket_id}/transitions",
            auth=self.auth,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"transition": {"id": done["id"]}},
            timeout=10,
        ).raise_for_status()

    def _to_ticket(self, issue: dict) -> ITSMTicket:
        fields = issue["fields"]
        priority_name = ((fields.get("priority") or {}).get("name") or "").lower()
        severity = self._PRIORITY_MAP.get(priority_name, Severity.P3)
        components = fields.get("components") or []
        # No project-key fallback here: the Jira project key (e.g. "ZPM") is
        # a ticketing category, not a Kubernetes service/deployment name --
        # handing it to an agent as `service` reads as a real, verified
        # identifier and gets queried against literally (wasted tool calls
        # hunting for a resource that doesn't exist) before the agent falls
        # back to reading the actual service name out of the description.
        # None is honest about "ITSM didn't tell us"; agents already derive
        # the real name from title/description when this is unset.
        service = components[0]["name"] if components else None
        return ITSMTicket(
            ticket_id=issue["key"],
            title=fields.get("summary") or "",
            description=_adf_to_text(fields.get("description")),
            service=service,
            severity=severity,
            source="jira",
            reported_at=datetime.fromisoformat(fields["created"]),
        )
