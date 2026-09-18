import { AlertTriangle, ArrowLeft, CheckCircle2, Clock, ListChecks, PieChart as PieIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { api } from "./api.js";

const POLL_MS = 15000;

const AGENT_COLORS = {
  triage: "#2563eb",
  diagnosis: "#d97706",
  remediation: "#7c3aed",
  validation: "#16a34a",
};

const TIME_RANGE_PRESETS = [
  { key: "1d", label: "Last 1 day" },
  { key: "7d", label: "Last 7 days" },
  { key: "1m", label: "Last 1 month" },
  { key: "1y", label: "Last 1 year" },
  { key: "custom", label: "Custom" },
];

function toDateInputValue(d) {
  return d.toISOString().slice(0, 10); // yyyy-mm-dd, for <input type="date">
}

// Preset -> [start, end) as Date objects; end is always "now" for presets.
function presetRange(key) {
  const end = new Date();
  const start = new Date(end);
  if (key === "1d") start.setDate(start.getDate() - 1);
  else if (key === "7d") start.setDate(start.getDate() - 7);
  else if (key === "1m") start.setMonth(start.getMonth() - 1);
  else if (key === "1y") start.setFullYear(start.getFullYear() - 1);
  else return null;
  return { start, end };
}

function TimeRangeFilter({ preset, onPresetChange, customStart, customEnd, onCustomStartChange, onCustomEndChange }) {
  return (
    <div className="timeframe-bar">
      {TIME_RANGE_PRESETS.map((p) => (
        <button
          key={p.key}
          className={`timeframe-btn${preset === p.key ? " active" : ""}`}
          onClick={() => onPresetChange(p.key)}
        >
          {p.label}
        </button>
      ))}
      {preset === "custom" && (
        <div className="timeframe-custom">
          <input
            type="date"
            className="timeframe-date"
            value={customStart}
            max={customEnd}
            onChange={(e) => onCustomStartChange(e.target.value)}
          />
          <span className="muted small">to</span>
          <input
            type="date"
            className="timeframe-date"
            value={customEnd}
            min={customStart}
            onChange={(e) => onCustomEndChange(e.target.value)}
          />
        </div>
      )}
    </div>
  );
}

const AGENT_LABELS = {
  triage: "Triage",
  diagnosis: "Diagnosis",
  remediation: "Remediation",
  validation: "Validation",
};

function StatsBar({ stats }) {
  if (!stats) return null;

  return (
    <>
      <div className="card">
        <div className="stat-label">
          <ListChecks size={14} /> Picked up by agents
        </div>
        <div className="stat-value">{stats.picked_up_by_agents}</div>
        <div className="stat-sub">of {stats.total} in range</div>
      </div>
      <div className="card">
        <div className="stat-label">
          <CheckCircle2 size={14} /> Remediated
        </div>
        <div className="stat-value">{stats.remediated}</div>
      </div>
      <div className="card">
        <div className="stat-label">
          <Clock size={14} /> Pending approval
        </div>
        <div className="stat-value">{stats.pending_approval}</div>
      </div>
      <div className="card">
        <div className="stat-label">
          <AlertTriangle size={14} /> Escalated
        </div>
        <div className="stat-value">{stats.escalated}</div>
      </div>
    </>
  );
}

function EscalatedByAgentPanel({ stats }) {
  if (!stats) return null;
  const data = Object.entries(stats.escalated_by_last_agent || {}).map(([agent, count]) => ({
    agent,
    label: AGENT_LABELS[agent] || agent,
    count,
  }));

  return (
    <div className="card">
      <div className="stat-label">
        <PieIcon size={14} /> Escalated by agent
      </div>
      {stats.escalated === 0 ? (
        <div className="stat-sub">No escalations in this time range.</div>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
          <div style={{ width: 110, minWidth: 90, flexShrink: 1 }}>
            <ResponsiveContainer width="100%" height={110}>
              <PieChart>
                <Pie data={data} dataKey="count" nameKey="label" innerRadius={26} outerRadius={45} paddingAngle={2}>
                  {data.map((row) => (
                    <Cell key={row.agent} fill={AGENT_COLORS[row.agent] || "#9ca3af"} />
                  ))}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          </div>
          <div className="fleet-breakdown">
            {data.map((row) => (
              <div className="fleet-breakdown-row" key={row.agent}>
                <span className="dot" style={{ background: AGENT_COLORS[row.agent] || "#9ca3af" }} />
                {row.label} <b>{row.count}</b>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

const STATUS_TONE = {
  // run status
  claimed: "gray",
  running: "amber",
  completed: "green",
  failed: "red",
  // incident status
  new: "gray",
  triaging: "accent",
  awaiting_diagnosis_approval: "amber",
  awaiting_remediation_approval: "amber",
  diagnosing: "accent",
  remediating: "accent",
  validating: "accent",
  resolved: "green",
  closed_non_issue: "green",
  escalated: "red",
  // validation result
  pass: "green",
  fail: "red",
  insufficient_observation: "amber",
};

function tone(status) {
  return STATUS_TONE[status] || "gray";
}

function timeAgo(iso) {
  if (!iso) return "—";
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  return `${Math.round(mins / 60)}h ago`;
}

function duration(startIso, endIso) {
  if (!startIso) return "";
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  const secs = Math.max(0, Math.round((end - new Date(startIso).getTime()) / 1000));
  if (secs < 60) return `${secs}s`;
  return `${Math.round(secs / 60)}m`;
}

function IncidentList({ onSelectIncident }) {
  const [incidents, setIncidents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [stats, setStats] = useState(null);
  const [preset, setPreset] = useState("7d");
  const today = useMemo(() => toDateInputValue(new Date()), []);
  const [customStart, setCustomStart] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() - 7);
    return toDateInputValue(d);
  });
  const [customEnd, setCustomEnd] = useState(today);

  const { startIso, endIso } = useMemo(() => {
    if (preset === "custom") {
      // customEnd is a day, not an instant -- include the whole day.
      const start = customStart ? new Date(`${customStart}T00:00:00`) : null;
      const end = customEnd ? new Date(`${customEnd}T23:59:59.999`) : null;
      return { startIso: start?.toISOString(), endIso: end?.toISOString() };
    }
    const range = presetRange(preset);
    return { startIso: range?.start.toISOString(), endIso: range?.end.toISOString() };
  }, [preset, customStart, customEnd]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const load = () =>
      Promise.all([
        api.getIncidents({ start: startIso, end: endIso }),
        api.getIncidentStats({ start: startIso, end: endIso }),
      ])
        .then(([rows, statsRow]) => {
          if (cancelled) return;
          setIncidents(rows);
          setStats(statsRow);
        })
        .finally(() => !cancelled && setLoading(false));
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [startIso, endIso]);

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Incidents</h1>
          <p>Every incident reported in the selected time range, and which agent (if any) is working it right now.</p>
        </div>
        {incidents.length > 0 && <span className="badge badge-tone-accent">{incidents.length} incidents</span>}
      </div>

      <TimeRangeFilter
        preset={preset}
        onPresetChange={setPreset}
        customStart={customStart}
        customEnd={customEnd}
        onCustomStartChange={setCustomStart}
        onCustomEndChange={setCustomEnd}
      />

      <div className="stats-row">
        <StatsBar stats={stats} />
        <EscalatedByAgentPanel stats={stats} />
      </div>

      {loading && <p className="muted">Loading…</p>}
      {!loading && incidents.length === 0 && <p className="muted">No incidents in this time range.</p>}

      {!loading && incidents.length > 0 && (
        <div className="card panel">
          <table>
            <thead>
              <tr>
                <th>Incident</th>
                <th>Service</th>
                <th>Severity</th>
                <th>Status</th>
                <th>Current agent</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {incidents.map((inc) => (
                <tr key={inc.incident_id} className="row-clickable" onClick={() => onSelectIncident(inc.incident_id)}>
                  <td>
                    <div>{inc.title}</div>
                    <div className="muted small">{inc.incident_id}</div>
                  </td>
                  <td>{inc.service || "—"}</td>
                  <td>
                    <span className={`badge badge-${inc.severity?.toLowerCase()}`}>{inc.severity}</span>
                  </td>
                  <td>
                    <span className={`badge badge-tone-${tone(inc.status)}`}>{inc.status.replaceAll("_", " ")}</span>
                  </td>
                  <td>{inc.current_agent ? AGENT_LABELS[inc.current_agent] || inc.current_agent : "—"}</td>
                  <td>{timeAgo(inc.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function RunBody({ run }) {
  const output = run.output;
  if (!output) return run.error ? null : <p className="muted">No output recorded.</p>;

  switch (run.agent_type) {
    case "triage":
      return (
        <>
          <p>
            <span className={`badge badge-tone-${tone(output.verdict)}`}>{output.verdict?.replaceAll("_", " ")}</span>{" "}
            {output.confidence != null && <span className="muted small">confidence {output.confidence}</span>}
          </p>
          {output.reasoning && <p>{output.reasoning}</p>}
        </>
      );
    case "diagnosis":
      return (
        <>
          <p>
            <span className={`badge badge-tone-${output.outcome === "diagnosed" ? "green" : "amber"}`}>
              {output.outcome?.replaceAll("_", " ")}
            </span>{" "}
            {output.confidence != null && <span className="muted small">confidence {output.confidence}</span>}
          </p>
          {output.root_cause && <p>{output.root_cause}</p>}
          {Array.isArray(output.evidence) && output.evidence.length > 0 && (
            <ol className="plan-list">
              {output.evidence.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ol>
          )}
        </>
      );
    case "remediation":
      return (
        <>
          {Array.isArray(output.execution_plan) && (
            <ol className="plan-list">
              {output.execution_plan.map((step, i) => (
                <li key={i}>
                  {step.step}
                  {step.playbook_id && <span className="muted small"> — {step.playbook_id}</span>}
                </li>
              ))}
            </ol>
          )}
          {output.notes && <p className="muted small">{output.notes}</p>}
        </>
      );
    case "validation":
      return (
        <>
          <p>
            <span className={`badge badge-tone-${tone(output.result)}`}>{output.result?.replaceAll("_", " ")}</span>
          </p>
          {output.summary && <p>{output.summary}</p>}
          {Array.isArray(output.checks) && output.checks.length > 0 && (
            <ul className="checks-list">
              {output.checks.map((c, i) => (
                <li key={i}>
                  {c.metric}: {String(c.value)} (threshold {String(c.threshold)}) — {c.ok ? "ok" : "failed"}
                </li>
              ))}
            </ul>
          )}
        </>
      );
    default:
      return <pre className="small">{JSON.stringify(output, null, 2)}</pre>;
  }
}

function IncidentDetail({ incidentId, onBack }) {
  const [detail, setDetail] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => api.getIncidentDetail(incidentId).then((d) => !cancelled && setDetail(d));
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [incidentId]);

  return (
    <div>
      <button className="back-link" onClick={onBack}>
        <ArrowLeft size={14} /> Back to incidents
      </button>

      {!detail && <p className="muted">Loading…</p>}

      {detail && (
        <>
          <div className="page-header">
            <div>
              <h1>{detail.title}</h1>
              <p className="incident-meta-line">
                {detail.incident_id} · {detail.service || "unknown service"} · from {detail.source}
              </p>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <span className={`badge badge-${detail.severity?.toLowerCase()}`}>{detail.severity}</span>
              <span className={`badge badge-tone-${tone(detail.status)}`}>{detail.status.replaceAll("_", " ")}</span>
            </div>
          </div>

          <div className="card">
            <p className="incident-description">{detail.description}</p>

            <div className="incident-fact-grid">
              <div>
                <div className="incident-fact-label">Reported</div>
                {timeAgo(detail.created_at)}
              </div>
              <div>
                <div className="incident-fact-label">Last updated</div>
                {timeAgo(detail.updated_at)}
              </div>
              {detail.resolved_at && (
                <div>
                  <div className="incident-fact-label">Resolved</div>
                  {timeAgo(detail.resolved_at)}
                </div>
              )}
            </div>

            {detail.root_cause && (
              <>
                <div className="incident-fact-label">Root cause</div>
                <p>{detail.root_cause}</p>
              </>
            )}
            {Array.isArray(detail.remediation_steps) && detail.remediation_steps.length > 0 && (
              <>
                <div className="incident-fact-label">Remediation steps</div>
                <ol className="plan-list">
                  {detail.remediation_steps.map((s, i) => (
                    <li key={i}>{s}</li>
                  ))}
                </ol>
              </>
            )}
          </div>

          <div className="panel" style={{ marginTop: 18 }}>
            <div className="panel-header">Agent timeline</div>
            <div className="timeline">
              {detail.runs.length === 0 && <p className="muted">No agent has picked this up yet.</p>}
              {detail.runs.map((run) => (
                <div className={`card timeline-item status-${run.status}`} key={run.run_id}>
                  <div className="timeline-head">
                    <span className="agent-name">{AGENT_LABELS[run.agent_type] || run.agent_type}</span>
                    <span className={`badge badge-tone-${tone(run.status)}`}>{run.status}</span>
                    <span className="timeline-when">
                      {timeAgo(run.started_at)} · ran {duration(run.started_at, run.completed_at)}
                    </span>
                  </div>
                  <div className="timeline-body">
                    <RunBody run={run} />
                    {run.error && <div className="error-box">{run.error}</div>}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {detail.approvals.length > 0 && (
            <div className="panel" style={{ marginTop: 18 }}>
              <div className="panel-header">Approvals</div>
              <div className="task-list">
                {detail.approvals.map((apr) => (
                  <div className="card task-card" key={apr.approval_id}>
                    <div className="task-top">
                      <span className="task-title">{apr.stage} review</span>
                      <span className={`badge badge-tone-${tone(apr.status)}`}>{apr.status}</span>
                      {apr.risk_tier && <span className={`badge badge-risk-${apr.risk_tier}`}>{apr.risk_tier} risk</span>}
                    </div>
                    <div className="task-meta small">
                      {apr.decided_by ? `${apr.status} by ${apr.decided_by} · ${timeAgo(apr.decided_at)}` : timeAgo(apr.created_at)}
                    </div>
                    <div className="task-summary">{apr.summary}</div>
                    {apr.reject_reason && <div className="error-box">{apr.reject_reason}</div>}
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function IncidentsTab({ selectedIncidentId, onSelectIncident, onClearSelectedIncident }) {
  if (selectedIncidentId) {
    return <IncidentDetail incidentId={selectedIncidentId} onBack={onClearSelectedIncident} />;
  }
  return <IncidentList onSelectIncident={onSelectIncident} />;
}
