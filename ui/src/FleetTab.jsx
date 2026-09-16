import { ListChecks } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "./api.js";

const POLL_MS = 15000;

const AGENT_LABELS = {
  triage: "Triage Agent",
  diagnosis: "Diagnosis Agent",
  remediation: "Remediation Agent",
  validation: "Validation Agent",
};

function formatAge(startedAt) {
  if (!startedAt) return "";
  const ms = Date.now() - new Date(startedAt).getTime();
  const mins = Math.max(0, Math.round(ms / 60000));
  if (mins < 60) return `${mins}m`;
  return `${Math.round(mins / 60)}h`;
}

export default function FleetTab({ onSelectIncident }) {
  const [fleet, setFleet] = useState([]);
  const [expanded, setExpanded] = useState(null);
  const [active, setActive] = useState([]);
  const [loadingActive, setLoadingActive] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = () => api.getFleet().then((rows) => !cancelled && setFleet(rows));
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  function toggle(agentType) {
    if (expanded === agentType) {
      setExpanded(null);
      setActive([]);
      return;
    }
    setExpanded(agentType);
    setLoadingActive(true);
    api
      .getFleetActive(agentType)
      .then(setActive)
      .finally(() => setLoadingActive(false));
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Fleet</h1>
          <p>How many agents of each type are deployed, and what they're working on right now.</p>
        </div>
      </div>

      <div className="card-grid">
        {fleet.map((row) => {
          const unavailable = Math.max(0, row.desired_replicas - row.available_replicas);
          const pct = row.desired_replicas
            ? Math.round((row.available_replicas / row.desired_replicas) * 100)
            : 0;
          return (
            <button
              key={row.agent_type}
              className={`card fleet-card ${expanded === row.agent_type ? "selected" : ""}`}
              onClick={() => toggle(row.agent_type)}
            >
              <div className="fleet-card-title">{AGENT_LABELS[row.agent_type] || row.agent_type}</div>
              <div className="fleet-card-count">
                {row.desired_replicas} <span className="of">total deployed</span>
              </div>
              <div className="fleet-breakdown">
                <div className="fleet-breakdown-row">
                  <span className="dot dot-green" /> Available <b>{row.available_replicas}</b>
                </div>
                {unavailable > 0 && (
                  <div className="fleet-breakdown-row">
                    <span className="dot dot-gray" /> Unavailable <b>{unavailable}</b>
                  </div>
                )}
              </div>
              <div className="bar-track">
                <div className="bar-segment" style={{ width: `${pct}%`, background: "var(--green)" }} />
                <div className="bar-segment" style={{ width: `${100 - pct}%`, background: "var(--gray-soft)" }} />
              </div>
            </button>
          );
        })}
      </div>

      {expanded && (
        <div className="card panel">
          <div className="panel-header">
            <ListChecks size={15} />
            {AGENT_LABELS[expanded] || expanded} — active incidents
          </div>
          {loadingActive && <p className="muted">Loading…</p>}
          {!loadingActive && active.length === 0 && (
            <p className="muted">No incidents currently being worked by this agent type.</p>
          )}
          {!loadingActive && active.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Incident</th>
                  <th>Service</th>
                  <th>Severity</th>
                  <th>Status</th>
                  <th>Running for</th>
                </tr>
              </thead>
              <tbody>
                {active.map((inc) => (
                  <tr
                    key={inc.run_id}
                    className="row-clickable"
                    onClick={() => onSelectIncident?.(inc.incident_id)}
                  >
                    <td>
                      <div>{inc.title}</div>
                      <div className="muted small">{inc.incident_id}</div>
                    </td>
                    <td>{inc.service || "—"}</td>
                    <td>
                      <span className={`badge badge-${inc.severity?.toLowerCase()}`}>{inc.severity}</span>
                    </td>
                    <td>{inc.status}</td>
                    <td>{formatAge(inc.started_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
