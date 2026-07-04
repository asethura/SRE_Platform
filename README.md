# SRE Platform — L2 Incident Automation

Four stateless agents (Triage → Diagnosis → Remediation → Validation) with a
single human approval gate at the runbook level. **There is no orchestrator**:
each agent independently polls the shared session database and picks up
incidents matching its entry criteria (`agent_criteria` table). The session
DB + criteria table IS the coordination layer.

## Structure

```
sre-platform/
├── db/
│   ├── models.py        # Shared session DB — the spine (no runbooks/KB tables — see below)
│   └── seed.py          # Criteria table + playbooks only
├── agents/
│   ├── base.py          # Stateless pattern + polling loop; MCP connector plumbing
│   ├── triage.py        # 3 exits: non-issue / known / unknown
│   ├── diagnosis.py     # 3 outcomes; observability stubs -> MCP later
│   ├── remediation.py   # eligible() = approval hard gate; locks; risk-tier rollback
│   └── validation.py    # Pass -> resolve; fail -> back to diagnosis
├── hitl.py              # approve()/reject() — call from your UI/API
├── run_agent.py         # Deployment: one polling worker process per agent
├── main.py              # Demo: three incidents, one per triage exit
├── smoke_test.py        # Full pipeline test with mocked LLM (no API key)
├── Dockerfile           # One image, agent type picked via CMD arg
└── k8s/base/            # Kustomize base: Deployment per agent + seed Job
```

## Runbooks & KB articles live in Confluence, not this DB

Triage, diagnosis, and remediation each declare `mcp_servers()` ->
`confluence_mcp_server()` (`agents/base.py`). Claude gets real Confluence
search/fetch tools via the Anthropic **MCP connector**
(`client.beta.messages.create(..., mcp_servers=..., tools=[{"type":
"mcp_toolset", ...}])`, beta `mcp-client-2025-11-20`) — the model decides
when and how to search, agentically, within `BaseAgent.run_llm()`. Set:

```bash
export CONFLUENCE_MCP_URL=https://your-mcp-server/...
export CONFLUENCE_MCP_TOKEN=...   # optional, if the server needs a bearer token
```

Only what a matched page states — its Confluence page id, title, and risk
tier — is persisted, on `Incident.matched_runbook_*` / `Approval.runbook_id`,
so remediation and rollback logic don't have to re-fetch and re-judge the
page every time. `Playbook` is still a local table: it's an executable
artifact (API/script), not a knowledge document, so it isn't Confluence
content.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export CONFLUENCE_MCP_URL=...      # required for triage/diagnosis/remediation
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
`requirements.txt` already has `psycopg2-binary`).

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

**Before this reaches real traffic:** `SRE_ITSM_CLIENT` currently only
supports `stub` (replays 3 fixed demo tickets forever) or `null` (no
intake at all — incidents only arrive via whatever creates `Incident` rows
directly, e.g. a webhook handler you add). The ConfigMap defaults to `null`
so a fresh cluster doesn't spin on fake tickets. Wire up a real PagerDuty/
Datadog client against the `ITSMClient` contract in `integrations/itsm.py`,
register it in `run_agent.py`'s `ITSM_CLIENTS` map, and point
`SRE_ITSM_CLIENT` at it.

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
4. Remediation additionally requires an APPROVED `approvals` row via its
   `eligible()` override — the human gate is enforced at dispatch, not just
   inside the agent.

## How it maps to the design

| Design decision | Where in code |
|---|---|
| Shared session state | `db/models.py` — `incidents` + `agent_runs` (handoff medium) |
| Entry/exit criteria table (to-do #13) | `agent_criteria` table + `BaseAgent.find_work()` |
| Criteria editable via HITL (to-do #14) | `AgentCriteria.updated_by` — expose via your UI |
| Runbook vs playbook split | Runbooks = Confluence pages (fetched live via MCP); `playbooks` table = executor + endpoint |
| Playbooks invoked over API (to-do #4) | `remediation.execute_playbook()` — stub, swap for real calls |
| Pre-approved changes (to-do #12) | Not yet re-added post-Confluence-migration — was `Runbook.preapproved_change_id`; needs a home once a runbook page format for it is settled |
| Single approval gate | `approvals` table; hard gate in `RemediationAgent.eligible()` |
| Triage closes non-issue tickets | `triage.apply_output()` → `CLOSED_NON_ISSUE` |
| Validation fail → re-diagnose | `validation.apply_output()` → `DIAGNOSING` |
| Unable to diagnose → escalate | `diagnosis.apply_output()` → `ESCALATED` |
| Resource locks (pool safety) | `resource_locks` table in `remediation.py` |
| Risk-tier failure behavior | `remediation._handle_failure()` — LOW auto-rollback |
| Feedback loop | `feedback` table with `destination` routing + review flag |
| Idempotent instance pools | `AgentRun.active_claim` unique index |

## Swapping stubs for production

1. **Observability** — replace `fetch_metrics/logs/traces/deploys` in
   `diagnosis.py` and `fetch_post_fix_metrics` in `validation.py` with
   Prometheus / ELK / Tempo / GitHub MCP clients.
2. **Playbook executor** — replace `execute_playbook()` with real Kubernetes
   API / HTTP calls per `Playbook.executor`.
3. **KB retrieval** — done: triage/diagnosis/remediation search and fetch
   Confluence directly via the MCP connector (`CONFLUENCE_MCP_URL`) instead
   of keyword matching a local table.
4. **Database** — swap SQLite URL in `get_engine()` for Postgres. Under
   concurrent pools, SQLite serializes writers; Postgres is the real target.
5. **Polling** — `run_forever()` is deliberate simple polling; tune
   `SRE_POLL_INTERVAL`, or replace the wake-up with a Redis queue /
   LISTEN-NOTIFY later. Agents and the criteria table stay unchanged.
6. **Models** — set `SRE_MODEL` env var; consider `claude-haiku-4-5-20251001`
   for triage/validation once stable, Sonnet for diagnosis/remediation.
