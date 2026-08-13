# SRE Platform — L2 Incident Automation

Four stateless agents (Triage → Diagnosis → Remediation → Validation) with
**two** human approval gates: diagnosis's root cause + remediation steps are
reviewed first, then remediation's exact playbook mapping of those steps is
reviewed before anything executes. **There is no orchestrator**: each agent
independently polls the shared session database and picks up incidents
matching its entry criteria (`agent_criteria` table). The session DB +
criteria table IS the coordination layer.

## Structure

```
sre-platform/
├── db/
│   ├── models.py        # Shared session DB — the spine (no runbook tables — see below)
│   └── seed.py          # Criteria table + playbooks only
├── agents/
│   ├── base.py          # Stateless pattern + polling loop; MCP connector plumbing
│   ├── triage.py        # 3 exits: non-issue / known (own steps, no gate 1) / unknown
│   ├── diagnosis.py     # 2 outcomes: diagnosed (root cause + steps -> gate 1) / unable
│   ├── remediation.py   # 2 phases: planning (own playbook mapping -> gate 2) / execute
│   └── validation.py    # Pass -> resolve; fail -> back to diagnosis
├── hitl.py              # approve()/reject() — call from your UI/API
├── run_agent.py         # Deployment: one polling worker process per agent
├── main.py              # Demo: three incidents, one per triage exit
├── smoke_test.py        # Full pipeline test with mocked LLM (no API key)
├── Dockerfile           # One image, agent type picked via CMD arg
├── playbook-server/     # Real Kubernetes API calls — the only thing that touches the live cluster
├── playbook-mcp/        # Discovery-only MCP gateway over the `playbooks` table (list/describe, no execution)
└── k8s/base/            # Kustomize base: Deployment per agent + seed Job + playbook-server/-mcp
```

## No runbook documents — diagnosis and remediation reason for themselves

There are no runbook pages anywhere in this system. Only **triage** talks to
Confluence, and only for KB-article classification (is this pattern a
verified non-issue or known fix?) — it declares `mcp_servers() ->
confluence_mcp_server()` (`agents/base.py`) and gets real Confluence
search/fetch tools via the Anthropic **MCP connector**
(`client.beta.messages.create(..., mcp_servers=..., tools=[{"type":
"mcp_toolset", ...}])`, beta `mcp-client-2025-11-20`) — the model decides
when and how to search, agentically, within `BaseAgent.run_llm()`. Set:

```bash
export CONFLUENCE_MCP_URL=https://your-mcp-server/...
export CONFLUENCE_MCP_TOKEN=...   # optional, if the server needs a bearer token
```

**Diagnosis** does its own root-cause analysis (logs/traces/deploys, no
Confluence) and states its own fix as free-text `remediation_steps` —
there's no document to cite. **Remediation** independently maps those
approved steps onto the `playbooks` catalog (exact playbook ids + params) —
also no document to cite, and no free-form actions: every executed step
must resolve to an entry in `playbooks`.

This produces **two** approval gates instead of one, both enforced the same
way (`RemediationAgent.eligible()`, `Approval.stage`):
1. **Diagnosis gate** (`Approval.stage == DIAGNOSIS`) — a human reviews the
   root cause and proposed steps before remediation is allowed to plan
   anything. Triage's `known_issue` verdict skips this gate entirely (and
   skips diagnosis) since it already states its own verified fix — but it
   still goes through gate 2.
2. **Remediation gate** (`Approval.stage == REMEDIATION`) — a human reviews
   the *exact* playbook-id + params mapping (`Approval.proposed_plan`) and
   its computed `risk_tier` (max tier across the playbooks actually used)
   before execution. Execution then runs that stored plan verbatim — no
   LLM call happens at execute time (`RemediationAgent.run_llm()` short-
   circuits for the execute phase), so what runs is provably identical to
   what was approved.

`Incident.root_cause` / `Incident.remediation_steps` hold the free-text
reasoning (written by diagnosis or triage's known_issue path). `Playbook`
rows themselves ARE API docs — `endpoint` (a relative path) + `params_schema`
document one API on a single playbook server (`PLAYBOOK_SERVER_URL`); that
server owns the actual remediation mechanism (Kubernetes API, a config
service, a script runner). `execute_playbook()` (`agents/remediation.py`)
is just an HTTP client POSTing to the documented endpoint — a 2xx response
is success, anything else (including network errors/timeouts) is a failure.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export CONFLUENCE_MCP_URL=...      # required for triage only
export PLAYBOOK_SERVER_URL=...     # required for remediation's execute phase
python main.py            # demo: real Claude calls, synchronous poll cycles
python smoke_test.py      # no API key or Confluence needed — mocked LLM
```

Deployment model — one worker per terminal/process, coordinating only
through the DB:

```bash
python run_agent.py triage
python run_agent.py diagnosis
python run_agent.py remediation
python run_agent.py validation
```

## Deploying to Kubernetes

Each agent type is one Deployment running `python run_agent.py <type>` in a
loop — there's no orchestrator to deploy, just four independent workers
coordinating through the DB, exactly as above. `k8s/base/` is a Kustomize
base with a Deployment per agent, a shared ConfigMap, and a one-time seed
Job.

**1. Build and push the image** (one image, four agent types — the
Deployments pick the type via `args:`):

```bash
docker build -t your-registry/sre-platform:latest .
docker push your-registry/sre-platform:latest
```

**2. Point `DATABASE_URL` at Postgres.** This deployment assumes an
*external* managed Postgres (RDS, Cloud SQL, etc.) — the manifests don't run
Postgres in-cluster. SQLite is a single-file DB and multiple pods writing to
it concurrently will corrupt it; swap the URL, nothing else changes
(`db/models.get_engine()` already accepts any SQLAlchemy URL, and
`requirements.txt` already has `psycopg[binary]`).

**3. Create the secret** (never commit real values — see
`k8s/base/secret.example.yaml` for the fields):

```bash
kubectl create namespace sre-platform
kubectl create secret generic sre-secrets -n sre-platform \
  --from-literal=ANTHROPIC_API_KEY='sk-ant-...' \
  --from-literal=DATABASE_URL='postgresql+psycopg://user:pass@host:5432/sre' \
  --from-literal=CONFLUENCE_MCP_TOKEN='...'
```

**4. Edit `k8s/base/configmap.yaml`** — set `CONFLUENCE_MCP_URL` to your real
MCP server, and decide `SRE_ITSM_CLIENT` (see below).

**5. Point the image at your registry and apply:**

```bash
cd k8s/base
kustomize edit set image sre-platform=your-registry/sre-platform:v1
kubectl apply -k .
```

This creates the namespace, ConfigMap, four Deployments, and the seed Job
(criteria rows + playbooks — nothing polls anything without it). The seed
Job is **not idempotent**; re-applying after it already succeeded will fail
on duplicate Playbook rows — delete the completed Job first if you mean to
reseed.

**ITSM intake:** `SRE_ITSM_CLIENT` supports `stub` (replays 3 fixed demo
tickets forever), `null` (no intake at all — incidents only arrive via
whatever creates `Incident` rows directly, e.g. a webhook handler you add),
or `jira` (pulls real open requests from **Jira Cloud Service Management**
via `JiraServiceManagementITSMClient` in `integrations/itsm.py`, using the
enhanced JQL search API and HTTP Basic auth with an email + API token). The
ConfigMap defaults to `null` so a fresh cluster doesn't spin on fake
tickets. To enable Jira: set `JIRA_BASE_URL` and `JIRA_PROJECT_KEY` (or a
custom `JIRA_JQL`) in `configmap.yaml`, add `JIRA_EMAIL`/`JIRA_API_TOKEN` to
`sre-secrets` (see `secret.example.yaml`), and set `SRE_ITSM_CLIENT: "jira"`.
For PagerDuty/Datadog instead, implement the same `ITSMClient` contract and
register it in `run_agent.py`'s `ITSM_CLIENTS` map.

**Liveness, not readiness:** these pods don't serve traffic (no Service
needed), so there's no readiness probe — only a liveness probe that checks
a heartbeat file `BaseAgent.run_forever()` touches every poll cycle
(`SRE_HEALTHCHECK_FILE`), so Kubernetes restarts a pod that's genuinely
hung rather than one mid-incident.

**Scaling:** `AgentRun.active_claim`'s unique index and `ResourceLock`
already make triage/diagnosis/remediation/validation safe to run with
`replicas > 1` — that's the whole point of the choreography design (see
below). Remediation defaults to 1 anyway since it's the highest-blast-radius
agent; raise it deliberately.

## How coordination works without an orchestrator

1. Each agent polls: `find_work()` matches incidents against its enabled
   `agent_criteria.entry_condition` rows (the orchestration contract,
   to-do #13; rows are HITL-editable, to-do #14).
2. `claim()` inserts an `agent_runs` row with a unique `active_claim` key —
   only one instance in a pool can win; the claim frees on completion so
   repeat runs (re-diagnosis after failed validation) are allowed.
3. The agent writes its output and moves `incident.status`, which is what the
   next agent's entry criteria match on. Status transitions are the handoffs.
4. Remediation runs in two phases on the same class (planning, then
   execute — same "multiple `agent_criteria` rows route to one agent" idiom
   triage already uses for NEW+TRIAGING). Each phase's `eligible()` override
   requires the matching `Approval.stage` to be APPROVED before that phase's
   entry status is dispatchable at all — the human gate is enforced at
   dispatch, not just inside the agent, for both gates.

## How it maps to the design

| Design decision | Where in code |
|---|---|
| Shared session state | `db/models.py` — `incidents` + `agent_runs` (handoff medium) |
| Entry/exit criteria table (to-do #13) | `agent_criteria` table + `BaseAgent.find_work()` |
| Criteria editable via HITL (to-do #14) | `AgentCriteria.updated_by` — expose via your UI |
| No runbook documents | Diagnosis states its own root cause + `remediation_steps`; remediation's planning phase maps those onto `playbooks` — no Confluence page, no local runbook table |
| Playbooks invoked over API (to-do #4) | `remediation.execute_playbook()` — POSTs to `PLAYBOOK_SERVER_URL` + `Playbook.endpoint`, implemented in `playbook-server/` (real Kubernetes API calls, in-cluster, own RBAC-scoped ServiceAccount) |
| Playbook discovery for the LLM | `playbook-mcp/` — read-only `list_playbooks`/`describe_playbook` MCP tools; `RemediationAgent.mcp_servers()` (planning phase only) |
| Pre-approved changes (to-do #12) | Every executed remediation step must resolve to a `playbooks` row (pre-approved by construction) — `RemediationAgent._apply_planning()` downgrades any step citing an unknown/inactive playbook id to `manual_check` rather than trust the LLM |
| Two approval gates | `approvals.stage` (DIAGNOSIS, REMEDIATION); hard gate per phase in `RemediationAgent.eligible()` |
| Triage closes non-issue tickets | `triage.apply_output()` → `CLOSED_NON_ISSUE` |
| Validation fail → re-diagnose | `validation.apply_output()` → `DIAGNOSING` |
| Unable to diagnose → escalate | `diagnosis.apply_output()` → `ESCALATED` |
| Resource locks (pool safety) | `resource_locks` table in `remediation.py` |
| Risk-tier failure behavior | `remediation._handle_failure()` — LOW auto-rollback |
| Feedback loop | `feedback` table with `destination` routing + review flag |
| Idempotent instance pools | `AgentRun.active_claim` unique index |

## Swapping stubs for production

1. **Observability** — `validation.py` reads live Prometheus metrics via the
   MCP connector (`PROMETHEUS_MCP_URL`, see `cloudrun/prometheus-mcp/` —
   queries Google Managed Prometheus, same bundled nginx-gate + Cloud Run
   pattern as Confluence). `diagnosis.py`'s `fetch_metrics/logs/traces/deploys`
   are still stubs — point `fetch_metrics` at the same Prometheus MCP server
   next, then swap `fetch_logs/traces/deploys` for ELK / Tempo / GitHub MCP
   clients.
2. **Playbook executor** — done: `execute_playbook()` (`agents/remediation.py`)
   POSTs each executed step's params to `{PLAYBOOK_SERVER_URL}{Playbook.endpoint}`
   on a single playbook server, which owns the actual remediation mechanism
   (Kubernetes API, a config service, a script runner — whatever
   `Playbook.executor` documents). Success is the HTTP status code (2xx)
   alone; the response body isn't interpreted. Set `PLAYBOOK_SERVER_URL`
   (and `PLAYBOOK_SERVER_TOKEN` if the server needs bearer auth).
3. **KB retrieval** — done for triage: it searches and fetches Confluence
   directly via the MCP connector (`CONFLUENCE_MCP_URL`) instead of keyword
   matching a local table. Diagnosis and remediation don't use Confluence at
   all — they reason for themselves (root cause + steps; playbook mapping).
4. **Database** — swap SQLite URL in `get_engine()` for Postgres. Under
   concurrent pools, SQLite serializes writers; Postgres is the real target.
5. **Polling** — `run_forever()` is deliberate simple polling; tune
   `SRE_POLL_INTERVAL`, or replace the wake-up with a Redis queue /
   LISTEN-NOTIFY later. Agents and the criteria table stay unchanged.
6. **Models** — set `SRE_MODEL` env var; consider `claude-haiku-4-5-20251001`
   for triage/validation once stable, Sonnet for diagnosis/remediation.
