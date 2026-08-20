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

import asyncio
import json
import os
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone

import anthropic
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy.exc import IntegrityError

from db.models import (
    TERMINAL_STATUSES,
    AgentCriteria,
    AgentRun,
    AgentType,
    Incident,
    IncidentStatus,
    LLMCall,
    RunStatus,
)

MODEL = os.environ.get("SRE_MODEL", "claude-sonnet-4-6")
POLL_INTERVAL = float(os.environ.get("SRE_POLL_INTERVAL", "2"))

# $/1M tokens (input, output) — first-party Anthropic API rates. Used only to
# compute LLMCall.cost_usd for observability; not billing-authoritative.
# Update if SRE_MODEL changes to a model not listed here.
MODEL_PRICING_PER_MTOK = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def _compute_cost_usd(model: str, input_tokens, output_tokens):
    rates = MODEL_PRICING_PER_MTOK.get(model)
    if rates is None or input_tokens is None or output_tokens is None:
        return None
    input_rate, output_rate = rates
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000

# Cap on consecutive failures (same agent, same incident) before giving up
# and escalating to a human instead of retrying forever. A failure that's
# going to recur (bad MCP creds, a model that can't produce valid JSON for
# this input) would otherwise retry every POLL_INTERVAL seconds indefinitely
# — each retry re-spending a full LLM call (or, with MCP tools, up to
# MAX_TOOL_TURNS calls) for no new outcome.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("SRE_MAX_CONSECUTIVE_FAILURES", "3"))

# Heartbeat file for run_forever() — a Kubernetes liveness probe checks this
# file's mtime (see k8s/base/deployment-*.yaml) since this is a background
# polling loop with no HTTP server to probe.
HEALTHCHECK_FILE = os.environ.get("SRE_HEALTHCHECK_FILE", "/tmp/sre-agent-healthy")

# Confluence MCP server — runbooks and KB articles live there, not in this
# DB. Agents that need them declare mcp_servers() -> [confluence_mcp_server()]
# and run_llm() connects to it directly (client-side): THIS process is the
# MCP client, not Anthropic's infrastructure. Claude only ever returns a
# tool_use request; _call_with_mcp_tools() below is what actually executes
# it, so every tool call is mediated, loggable, and gateable in our own code.
CONFLUENCE_MCP_URL = os.environ.get("CONFLUENCE_MCP_URL", "")
CONFLUENCE_MCP_TOKEN = os.environ.get("CONFLUENCE_MCP_TOKEN")
MAX_TOOL_TURNS = 8  # cap on tool-use round trips — avoid an unbounded loop


def confluence_mcp_server() -> dict:
    if not CONFLUENCE_MCP_URL:
        raise RuntimeError(
            "CONFLUENCE_MCP_URL is not set — required for agents that read "
            "runbooks/KB articles from Confluence."
        )
    server = {"url": CONFLUENCE_MCP_URL, "name": "confluence"}
    if CONFLUENCE_MCP_TOKEN:
        server["authorization_token"] = CONFLUENCE_MCP_TOKEN
    return server


# Prometheus MCP server (gap #4) — Validation reads post-fix metrics from
# Google Managed Prometheus this way instead of a stub. See
# cloudrun/prometheus-mcp/ for the server this URL points at.
PROMETHEUS_MCP_URL = os.environ.get("PROMETHEUS_MCP_URL", "")
PROMETHEUS_MCP_TOKEN = os.environ.get("PROMETHEUS_MCP_TOKEN")


def prometheus_mcp_server() -> dict:
    if not PROMETHEUS_MCP_URL:
        raise RuntimeError(
            "PROMETHEUS_MCP_URL is not set — required for agents that read "
            "metrics from Prometheus."
        )
    server = {"url": PROMETHEUS_MCP_URL, "name": "prometheus"}
    if PROMETHEUS_MCP_TOKEN:
        server["authorization_token"] = PROMETHEUS_MCP_TOKEN
    return server


# Cloud Logging MCP server — the "ELK" equivalent for Diagnosis's fetch_logs.
# See cloudrun/logging-mcp/.
LOGGING_MCP_URL = os.environ.get("LOGGING_MCP_URL", "")
LOGGING_MCP_TOKEN = os.environ.get("LOGGING_MCP_TOKEN")


def logging_mcp_server() -> dict:
    if not LOGGING_MCP_URL:
        raise RuntimeError(
            "LOGGING_MCP_URL is not set — required for agents that read "
            "logs from Cloud Logging."
        )
    server = {"url": LOGGING_MCP_URL, "name": "logging"}
    if LOGGING_MCP_TOKEN:
        server["authorization_token"] = LOGGING_MCP_TOKEN
    return server


# Cloud Trace MCP server — the "Tempo" equivalent for Diagnosis's
# fetch_traces. See cloudrun/trace-mcp/.
TRACE_MCP_URL = os.environ.get("TRACE_MCP_URL", "")
TRACE_MCP_TOKEN = os.environ.get("TRACE_MCP_TOKEN")


def trace_mcp_server() -> dict:
    if not TRACE_MCP_URL:
        raise RuntimeError(
            "TRACE_MCP_URL is not set — required for agents that read "
            "traces from Cloud Trace."
        )
    server = {"url": TRACE_MCP_URL, "name": "trace"}
    if TRACE_MCP_TOKEN:
        server["authorization_token"] = TRACE_MCP_TOKEN
    return server


# GitHub MCP server — for Diagnosis's fetch_recent_deploys. GitHub's own
# hosted endpoint (not self-hosted — the open-source github-mcp-server
# binary only speaks stdio, no HTTP mode to bundle behind nginx like the
# others). Auth is a plain PAT via Bearer header, same as any other server
# here — our own agent code still mediates every tool call either way.
GITHUB_MCP_URL = os.environ.get("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")
GITHUB_MCP_TOKEN = os.environ.get("GITHUB_MCP_TOKEN")


def github_mcp_server() -> dict:
    if not GITHUB_MCP_TOKEN:
        raise RuntimeError(
            "GITHUB_MCP_TOKEN is not set — required for agents that read "
            "recent deploys/commits from GitHub."
        )
    return {"url": GITHUB_MCP_URL, "name": "github", "authorization_token": GITHUB_MCP_TOKEN}


# Playbook discovery MCP server (playbook-mcp/) — lets RemediationAgent's
# planning phase discover available playbooks live instead of relying only
# on the DB-query catalog embedded in build_context(). Deliberately
# read-only (list_playbooks/describe_playbook): the actual mutating
# execution is a separate, non-MCP HTTP call (execute_playbook() in
# agents/remediation.py, straight to PLAYBOOK_SERVER_URL) that no LLM
# tool-use loop can ever reach — see playbook-mcp/server.py's docstring.
PLAYBOOK_MCP_URL = os.environ.get("PLAYBOOK_MCP_URL", "")
PLAYBOOK_MCP_TOKEN = os.environ.get("PLAYBOOK_MCP_TOKEN")


def playbook_mcp_server() -> dict:
    if not PLAYBOOK_MCP_URL:
        raise RuntimeError(
            "PLAYBOOK_MCP_URL is not set — required for remediation's "
            "planning phase to discover playbooks."
        )
    server = {"url": PLAYBOOK_MCP_URL, "name": "playbook"}
    if PLAYBOOK_MCP_TOKEN:
        server["authorization_token"] = PLAYBOOK_MCP_TOKEN
    return server


def _json_safe(value):
    """Recursively convert a messages/content structure (a mix of plain
    dicts and Anthropic SDK pydantic objects) into something JSON-
    serializable, for persisting to LLMCall.input_messages/response_content."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


async def _call_with_mcp_tools(client, servers, system, user_message, model,
                                max_tokens, max_turns, log_prefix, log_call):
    """Client-side tool-use loop: THIS process connects to each MCP server,
    discovers its tools, hands Claude plain tool definitions, and executes
    every tool_use request itself before feeding the result back. Claude
    never touches the MCP server directly — this function is the mediation
    point for logging/rate-limiting/authorization."""
    async with AsyncExitStack() as stack:
        sessions_by_tool: dict[str, ClientSession] = {}
        anthropic_tools = []
        for server in servers:
            headers = None
            if server.get("authorization_token"):
                headers = {"Authorization": f"Bearer {server['authorization_token']}"}
            read, write, _get_session_id = await stack.enter_async_context(
                streamablehttp_client(server["url"], headers=headers)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
            for tool in listed.tools:
                sessions_by_tool[tool.name] = session
                anthropic_tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema,
                })

        messages = [user_message]
        response = client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            tools=anthropic_tools, messages=messages,
        )
        log_call(0, anthropic_tools, messages, response)
        turns = 0
        while response.stop_reason == "tool_use" and turns < max_turns:
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                print(f"    [{log_prefix}] tool call: {block.name}({block.input})")
                session = sessions_by_tool.get(block.name)
                if session is None:
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": f"error: unknown tool {block.name}",
                        "is_error": True,
                    })
                    continue
                try:
                    result = await session.call_tool(block.name, block.input)
                    text = "".join(c.text for c in result.content if c.type == "text")
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": text, "is_error": bool(result.isError),
                    })
                except Exception as e:
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": f"error calling {block.name}: {e}",
                        "is_error": True,
                    })
            messages.append({"role": "user", "content": tool_results})
            response = client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                tools=anthropic_tools, messages=messages,
            )
            turns += 1
            log_call(turns, anthropic_tools, messages, response)

        if response.stop_reason == "tool_use":
            # Cut off at max_turns mid tool-call: response.content has no
            # text block at all, so letting this through would make run_llm()
            # try to json.loads("") and fail with a cryptic "Expecting
            # value: line 1 column 1 (char 0)" that looks nothing like the
            # real problem.
            raise RuntimeError(
                f"[{log_prefix}] tool loop exhausted after {max_turns} turns "
                "without a final answer"
            )
        return response


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

    def _consecutive_failures(self, db, incident_id: str, exclude_run_id: str) -> int:
        """FAILED runs by this agent on this incident since the last
        COMPLETED run (or since the beginning, if none) — walked newest
        first and stopped at the first non-FAILED row. `exclude_run_id`
        keeps the run currently being failed out of its own count."""
        runs = (db.query(AgentRun)
                .filter(AgentRun.incident_id == incident_id,
                        AgentRun.agent_type == self.agent_type,
                        AgentRun.id != exclude_run_id)
                .order_by(AgentRun.started_at.desc()).all())
        count = 0
        for r in runs:
            if r.status != RunStatus.FAILED:
                break
            count += 1
        return count

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

            output = self.run_llm(context, run.id)

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
            run.completed_at = datetime.now(timezone.utc)

            failures = self._consecutive_failures(db, incident_id, run.id) + 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                # This failure is likely to recur (bad creds, a model that
                # can't produce valid output for this input, ...) — leaving
                # the claim free would just let the next poll retry it
                # again in POLL_INTERVAL seconds, forever. Escalate instead
                # of burning an LLM call every cycle with no new outcome.
                incident = db.get(Incident, incident_id)
                incident.status = IncidentStatus.ESCALATED
                run.active_claim = None
                print(f"[{self.instance_id}] {incident_id} escalated after "
                      f"{failures} consecutive {self.agent_type.value} failures: {e}")
            else:
                run.active_claim = None  # free the claim so a retry can happen
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

    def _log_llm_call(self, run_id: str, turn: int, model: str, system: str,
                       tools: list, messages: list, response) -> None:
        """Best-effort persistence of one raw client.messages.create() call —
        a logging failure must never fail the agent's actual work."""
        db = self.session_factory()
        try:
            text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
            input_tokens = getattr(response.usage, "input_tokens", None)
            output_tokens = getattr(response.usage, "output_tokens", None)
            db.add(LLMCall(
                agent_run_id=run_id,
                agent_type=self.agent_type,
                turn=turn,
                model=model,
                system_prompt=system,
                tools=_json_safe(tools) if tools else None,
                input_messages=_json_safe(messages),
                response_content=_json_safe(response.content),
                response_text=text,
                stop_reason=response.stop_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=_compute_cost_usd(model, input_tokens, output_tokens),
            ))
            db.commit()
        except Exception as e:
            print(f"[{self.instance_id}] failed to log LLM call: {e}")
        finally:
            db.close()

    def run_llm(self, context: dict, run_id: str) -> dict:
        user_message = {
            "role": "user",
            "content": (
                "Here is the incident context as JSON:\n\n"
                + json.dumps(context, indent=2, default=str)
                + "\n\nRespond ONLY with the JSON object described in your "
                  "instructions. No preamble, no markdown fences."
            ),
        }
        system = self.system_prompt()

        servers = self.mcp_servers()
        if not servers:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=2000,
                system=system,
                messages=[user_message],
            )
            self._log_llm_call(run_id, 0, MODEL, system, [], [user_message], response)
        else:
            # Client-side tool loop — this process is the MCP client, not
            # Anthropic's infrastructure. See _call_with_mcp_tools().
            log_call = lambda turn, tools, messages, resp: self._log_llm_call(
                run_id, turn, MODEL, system, tools, messages, resp
            )
            response = asyncio.run(_call_with_mcp_tools(
                self.client, servers, system, user_message,
                MODEL, 2000, MAX_TOOL_TURNS, self.instance_id, log_call,
            ))

        text = "".join(b.text for b in response.content if b.type == "text")
        text = text.replace("```json", "").replace("```", "").strip()
        if not text:
            # The model ended its turn (stop_reason != "tool_use", so the
            # guard above didn't fire) but produced no text block at all —
            # e.g. it gave up after repeated tool errors (bad MCP creds)
            # instead of still returning the required JSON verdict. Fail
            # with the real cause instead of json.loads("")'s cryptic
            # "Expecting value: line 1 column 1 (char 0)".
            raise RuntimeError(
                f"[{self.instance_id}] model returned no text content "
                f"(stop_reason={response.stop_reason!r}) — check for MCP "
                "tool errors upstream (e.g. expired Confluence credentials)"
            )
        # Despite "no preamble" in every system prompt, the model sometimes
        # narrates its reasoning before the JSON object anyway (seen after
        # long tool-call chains, e.g. triage explaining an exhaustive KB
        # search before its verdict). Extract the {...} span instead of
        # assuming the whole response is JSON, so a stray sentence doesn't
        # fail the run.
        start, end = text.find("{"), text.rfind("}")
        json_text = text[start:end + 1] if start != -1 and end > start else text
        try:
            return json.loads(json_text)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"[{self.instance_id}] model response was not valid JSON: {e}\n"
                f"response text: {text!r}"
            ) from e

    # ------------------------------------------------------------------ #
    # To implement per agent
    # ------------------------------------------------------------------ #

    def system_prompt(self) -> str:
        raise NotImplementedError

    def build_context(self, db, incident: Incident) -> dict:
        raise NotImplementedError

    def apply_output(self, db, incident: Incident, output: dict) -> None:
        raise NotImplementedError
