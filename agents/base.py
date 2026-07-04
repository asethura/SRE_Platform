"""
BaseAgent — the stateless execution pattern every agent follows.

There is NO central orchestrator. Each agent is an independent worker that
polls the shared session DB for incidents matching its entry criteria
(agent_criteria table — the orchestration contract, to-do #13):

  1. find_work() — query incidents whose fields match this agent's
                   entry_condition rows, plus any per-agent gate (eligible())
  2. claim()     — atomically claim the incident (idempotency lock)
  3. build_context() — read EVERYTHING needed from session DB (no memory)
  4. run_llm()   — call Claude with system prompt + context, get structured JSON
  5. apply_output() — persist verdict, move incident status forward
  6. release    — mark run completed, claim freed

Run each agent as its own process/pool via run_forever() (see run_agent.py).
Concrete agents implement: system_prompt(), build_context(), apply_output().
Optional hooks: eligible() (extra dispatch gate), on_claim() (status marker).
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone

import anthropic
from sqlalchemy.exc import IntegrityError

from db.models import (
    TERMINAL_STATUSES,
    AgentCriteria,
    AgentRun,
    AgentType,
    Incident,
    RunStatus,
)

MODEL = os.environ.get("SRE_MODEL", "claude-sonnet-4-6")
POLL_INTERVAL = float(os.environ.get("SRE_POLL_INTERVAL", "2"))

# Heartbeat file for run_forever() — a Kubernetes liveness probe checks this
# file's mtime (see k8s/base/deployment-*.yaml) since this is a background
# polling loop with no HTTP server to probe.
HEALTHCHECK_FILE = os.environ.get("SRE_HEALTHCHECK_FILE", "/tmp/sre-agent-healthy")

# Confluence MCP server — runbooks and KB articles live there, not in this
# DB. Agents that need them declare mcp_servers() -> [confluence_mcp_server()]
# and run_llm() gives Claude the actual search/fetch tools via the MCP
# connector (server-side; Anthropic makes the connection, not this process).
CONFLUENCE_MCP_URL = os.environ.get("CONFLUENCE_MCP_URL", "")
CONFLUENCE_MCP_TOKEN = os.environ.get("CONFLUENCE_MCP_TOKEN")
MCP_BETA = "mcp-client-2025-11-20"
MAX_MCP_TURNS = 8  # pause_turn resumption cap — avoid an unbounded tool loop


def confluence_mcp_server() -> dict:
    if not CONFLUENCE_MCP_URL:
        raise RuntimeError(
            "CONFLUENCE_MCP_URL is not set — required for agents that read "
            "runbooks/KB articles from Confluence."
        )
    server = {"type": "url", "url": CONFLUENCE_MCP_URL, "name": "confluence"}
    if CONFLUENCE_MCP_TOKEN:
        server["authorization_token"] = CONFLUENCE_MCP_TOKEN
    return server


class BaseAgent:
    agent_type: AgentType = None

    def __init__(self, session_factory, instance_id: str = None):
        self.session_factory = session_factory
        self.instance_id = instance_id or f"{self.agent_type.value}-{uuid.uuid4().hex[:6]}"
        self.client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env

    # ------------------------------------------------------------------ #
    # Polling — each agent finds its own work from the session DB
    # ------------------------------------------------------------------ #

    @staticmethod
    def _matches(incident: Incident, condition: dict) -> bool:
        for field, expected in condition.items():
            actual = getattr(incident, field, None)
            actual = actual.value if hasattr(actual, "value") else actual
            if actual != expected:
                return False
        return True

    def eligible(self, db, incident: Incident) -> bool:
        """Extra per-agent dispatch gate beyond the criteria table.
        Remediation overrides this to require an APPROVED approval."""
        return True

    def find_work(self) -> list[str]:
        """Incidents matching this agent's enabled entry criteria."""
        db = self.session_factory()
        try:
            criteria = (db.query(AgentCriteria)
                        .filter(AgentCriteria.agent_type == self.agent_type,
                                AgentCriteria.enabled == True).all())
            if not criteria:
                return []
            candidates = (db.query(Incident)
                          .filter(Incident.status.notin_(TERMINAL_STATUSES))
                          .all())
            return [
                inc.id for inc in candidates
                if any(self._matches(inc, c.entry_condition) for c in criteria)
                and self.eligible(db, inc)
            ]
        finally:
            db.close()

    def poll_once(self) -> list[dict]:
        """One poll pass: claim and process every matching incident.
        Returns the outputs produced (empty list = no work / lost claims)."""
        results = []
        for incident_id in self.find_work():
            output = self.process(incident_id)
            if output is not None:
                results.append({"incident_id": incident_id, "output": output})
        return results

    def run_forever(self, interval: float = POLL_INTERVAL):
        """Deployment entry point — the agent as an autonomous worker."""
        print(f"[{self.instance_id}] polling every {interval}s")
        self._touch_healthcheck()  # mark alive before the first poll completes
        while True:
            try:
                self.poll_once()
            except Exception as e:
                print(f"[{self.instance_id}] error: {e}")
            self._touch_healthcheck()
            time.sleep(interval)

    @staticmethod
    def _touch_healthcheck():
        """Best-effort — a missing/unwritable health file should never crash
        the poll loop, it just means the liveness probe has nothing to read."""
        try:
            with open(HEALTHCHECK_FILE, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Lifecycle for one incident
    # ------------------------------------------------------------------ #

    def claim(self, incident_id: str):
        """Atomically claim an incident. Returns AgentRun or None if another
        instance holds the claim (unique index on active_claim)."""
        db = self.session_factory()
        try:
            run = AgentRun(
                incident_id=incident_id,
                agent_type=self.agent_type,
                instance_id=self.instance_id,
                status=RunStatus.CLAIMED,
                active_claim=f"{incident_id}:{self.agent_type.value}",
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            return run
        except IntegrityError:
            db.rollback()
            return None  # another instance got it first
        finally:
            db.close()

    def on_claim(self, db, incident: Incident) -> None:
        """Optional in-flight marker hook (e.g. triage sets TRIAGING)."""

    def process(self, incident_id: str) -> dict | None:
        """Full lifecycle for one incident. Returns the structured output."""
        run = self.claim(incident_id)
        if run is None:
            return None

        db = self.session_factory()
        try:
            incident = db.get(Incident, incident_id)
            self.on_claim(db, incident)
            context = self.build_context(db, incident)

            run = db.get(AgentRun, run.id)
            run.status = RunStatus.RUNNING
            run.input_context = context
            db.commit()

            output = self.run_llm(context)

            self.apply_output(db, incident, output)

            run.output = output
            run.status = RunStatus.COMPLETED
            run.active_claim = None
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            return output

        except Exception as e:
            db.rollback()
            run = db.get(AgentRun, run.id)
            run.status = RunStatus.FAILED
            run.error = str(e)
            run.active_claim = None  # free the claim so a retry can happen
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------ #
    # LLM call — structured JSON out, always
    # ------------------------------------------------------------------ #

    def mcp_servers(self) -> list[dict]:
        """Override to give this agent live MCP tool access (e.g. Confluence).
        Default: none — plain single-turn call."""
        return []

    def run_llm(self, context: dict) -> dict:
        user_message = {
            "role": "user",
            "content": (
                "Here is the incident context as JSON:\n\n"
                + json.dumps(context, indent=2, default=str)
                + "\n\nRespond ONLY with the JSON object described in your "
                  "instructions. No preamble, no markdown fences."
            ),
        }

        servers = self.mcp_servers()
        if not servers:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=2000,
                system=self.system_prompt(),
                messages=[user_message],
            )
        else:
            # Agentic MCP loop: Claude decides when/how to search or fetch
            # pages via the connected MCP server(s). The connector executes
            # the tool calls server-side; we only need to resume on
            # pause_turn (the server's own iteration cap), same shape as any
            # other server-side tool.
            tools = [
                {"type": "mcp_toolset", "mcp_server_name": s["name"]}
                for s in servers
            ]
            messages = [user_message]
            response = self.client.beta.messages.create(
                model=MODEL,
                max_tokens=2000,
                betas=[MCP_BETA],
                system=self.system_prompt(),
                mcp_servers=servers,
                tools=tools,
                messages=messages,
            )
            turns = 1
            while response.stop_reason == "pause_turn" and turns < MAX_MCP_TURNS:
                messages.append({"role": "assistant", "content": response.content})
                response = self.client.beta.messages.create(
                    model=MODEL,
                    max_tokens=2000,
                    betas=[MCP_BETA],
                    system=self.system_prompt(),
                    mcp_servers=servers,
                    tools=tools,
                    messages=messages,
                )
                turns += 1

        text = "".join(b.text for b in response.content if b.type == "text")
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)

    # ------------------------------------------------------------------ #
    # To implement per agent
    # ------------------------------------------------------------------ #

    def system_prompt(self) -> str:
        raise NotImplementedError

    def build_context(self, db, incident: Incident) -> dict:
        raise NotImplementedError

    def apply_output(self, db, incident: Incident, output: dict) -> None:
        raise NotImplementedError
