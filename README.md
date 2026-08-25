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
├── inject_scenario.py   # Fault injector for a live Online Boutique cluster (see below)
├── Dockerfile           # One image, agent type picked via CMD arg
├── playbook-server/     # Real Kubernetes API calls — the only thing that touches the live cluster
├── playbook-mcp/        # Discovery-only MCP gateway over the `playbooks` table (list/describe, no execution)
├── api/                 # FastAPI backend for the UI — Fleet/FinOps/Tasks (see below)
├── ui/                  # React SPA served by api/ — Fleet/FinOps/Tasks tabs
└── k8s/base/            # Kustomize base: Deployment per agent + seed Job + playbook-server/-mcp/api
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
way (`RemediationAgent.eligible()`, `Approval.stage`). A gate checks the
*latest* `Approval` row for its stage, not just whether any row was ever
approved — a fresh row from a later diagnosis/remediation cycle (e.g. after
a failed validation sends the incident back to diagnosis) must be reviewed
on its own; an approval from an earlier cycle can't keep authorizing it:
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

**1. Build and push the images** (one image for all four agent types — the
Deployments pick the type via `args:` — plus separate images for
`playbook-server`, `playbook-mcp`, and `sre-api`):

```bash
docker build -t your-registry/sre-platform:latest .
docker push your-registry/sre-platform:latest

# api/'s build context is the repo root (needs db/ and hitl.py), like playbook-mcp/:
docker build -f api/Dockerfile -t your-registry/sre-api:latest .
docker push your-registry/sre-api:latest
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

**Ticket closure:** triage closes the ITSM ticket when it verdicts
`non_issue`, and validation closes it when a remediation passes (both call
`ITSMClient.close_ticket()`) — so `run_agent.py` wires an ITSM client into
`ValidationAgent` as well as `TriageAgent`. `JiraServiceManagementITSMClient`
resolves whichever of the issue's available transitions leads to a
`statusCategory: done` status (workflow-agnostic — Jira status names aren't
fixed across projects), preferring a non-cancel transition when more than
one qualifies.

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

## Testing end-to-end against a live cluster

`inject_scenario.py` introduces a REAL fault on a running Online Boutique app
(same cluster as the deployed agents, namespace `default`) so the already-
running pipeline has something genuine to find and fix — it makes no DB
writes and doesn't touch the LLM or ITSM itself:

```bash
python inject_scenario.py --list                              # services + symptoms
python inject_scenario.py bad-deploy productcatalogservice     # ImagePullBackOff, does not self-heal
python inject_scenario.py scale-zero cartservice                # real outage, does not self-heal
python inject_scenario.py cpu-stress --duration 45              # paymentservice only, self-ends
python inject_scenario.py restore productcatalogservice deploy  # manual undo if you don't trust the pipeline
```

After injecting `scale-zero` or `bad-deploy`, file the matching ITSM ticket
yourself (Jira Service Management, if `SRE_ITSM_CLIENT=jira`) so triage picks
it up — the fault only exists in the cluster until something makes an
`Incident` row for it. `bad-deploy` maps to `PB-017` (rollback_deployment),
which the pipeline can execute and validate without any manual step once the
two gates are approved.

## UI — Fleet, FinOps, and Tasks

`api/` (FastAPI) + `ui/` (React SPA, built and served as static files by
`api/`) give a human three things, with no new approval logic — the Tasks
tab calls `hitl.py`'s `approve()`/`reject()` directly:

- **Fleet** — live Deployment replica counts per agent type, read from the
  Kubernetes API (`api/k8s_fleet.py`; new read-only `sre-api`
  ServiceAccount/Role/RoleBinding, `k8s/base/api-rbac.yaml`, scoped to
  `get`+`list` on `deployments` in the `sre-platform` namespace only).
  Clicking a card drills into which incidents that agent type currently has
  claimed (`agent_runs.status IN (CLAIMED, RUNNING)`).
- **FinOps** — total/24h/7d cost, a cost-trend chart stacked by agent type,
  and a by-agent breakdown, all from the existing `llm_calls` table. The
  trend endpoint buckets by day in Python rather than SQL so it behaves
  identically on SQLite (dev) and Postgres (cluster).
- **Tasks** — pending `Approval` rows with Approve/Reject buttons. Reject is
  intentionally plain for now: it stores a reason via the existing
  `Approval.reject_reason` field and nothing more — no `feedback`-table
  write, no automated Confluence KB-article update (that's a separate,
  not-yet-built worker; the current MCP connector is read-only anyway).

**Reaching it:** `kubectl port-forward -n sre-platform svc/sre-api 8000:8000`,
then open `http://localhost:8000`. No Ingress, TLS, or app-level auth exists
for this service (v1 scope) — it's deliberately never exposed beyond
`kubectl`'s own access control, the same way every other in-cluster
resource in this repo has been reached so far.

**Local dev:** `uvicorn api.app:app --reload` (reads `DATABASE_URL`/falls
back to local SQLite, same as every other component) + `cd ui && npm run
dev` (Vite proxies `/api` to `:8000`, see `ui/vite.config.js`).

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

1. **Observability** — done: both `validation.py` and `diagnosis.py` read live
   metrics via the same Prometheus MCP connector (`PROMETHEUS_MCP_URL`, see
   `cloudrun/prometheus-mcp/` — queries Google Managed Prometheus, same
   bundled nginx-gate + Cloud Run pattern as Confluence); `diagnosis.py` also
   reads logs/traces/deploys live via Cloud Logging, Cloud Trace, and GitHub
   MCP servers. No more hardcoded `fetch_metrics()` stub — diagnosis's
   system prompt explicitly requires evidence to be checked against a
   provided `now` timestamp, since logging/trace tools can return
   arbitrarily old results if a query isn't time-scoped. Both prompts also
   guard against citing/validating a specific pod by name: remediation
   (rollback, restart, rescale) routinely replaces pods, so diagnosis must
   confirm a pod is part of the CURRENT live set before citing it as
   evidence, and validation must judge the service in aggregate, not a pod
   instance that may already be gone.
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

## Cost tracking and prompt caching

Every LLM call is logged to `llm_calls` (`agent_type`, `turn`, prompts,
response, token counts, `cost_usd`) — see `BaseAgent._log_llm_call()` in
`agents/base.py`. System prompts and tool definitions are identical on every
call a given agent type makes, so both get an ephemeral cache breakpoint
(`_CACHE_CONTROL`); within one incident's own multi-turn MCP tool loop, a
second breakpoint moves to the latest message each turn so the growing
history isn't re-priced as fresh input every round trip. `cost_usd` prices
cache writes and cache reads at Anthropic's published multipliers (1.25x and
0.10x of the base input rate) so it doesn't silently under-report once
caching is in effect — a plain `input_tokens * rate` calculation would miss
most of the actual cost.

A single agent's MCP tool-use loop is capped at `MAX_TOOL_TURNS` (default 8)
round trips before giving up with a `RuntimeError` rather than looping
forever — validation's stale-pod-aware prompts (above) are exploratory
enough to occasionally need most of that budget. If the loop is cut off
mid-tool-call, `agent_runs.error` records the real reason, not the
underlying MCP transport's own asyncio cleanup noise.
