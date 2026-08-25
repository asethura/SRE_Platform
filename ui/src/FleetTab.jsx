import { useEffect, useState } from "react";

import { api } from "./api.js";

const POLL_MS = 15000;

const AGENT_LABELS = {
  triage: "Triage",
  diagnosis: "Diagnosis",
  remediation: "Remediation",
  validation: "Validation",
};

function formatAge(startedAt) {
  if (!startedAt) return "";
  const ms = Date.now() - new Date(startedAt).getTime();
  const mins = Math.max(0, Math.round(ms / 60000));
  if (mins < 60) return `${mins}m`;
  return `${Math.round(mins / 60)}h`;
}

export default function FleetTab() {
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
      <div className="card-grid">
        {fleet.map((row) => (
          <button
            key={row.agent_type}
            className={`card fleet-card ${expanded === row.agent_type ? "selected" : ""}`}
            onClick={() => toggle(row.agent_type)}
          >
            <div className="fleet-card-title">{AGENT_LABELS[row.agent_type] || row.agent_type}</div>
            <div className="fleet-card-count">
              {row.available_replicas}/{row.desired_replicas}
            </div>
            <div className="fleet-card-sub">available / desired</div>
          </button>
        ))}
      </div>

      {expanded && (
        <div className="panel">
          <h3>{AGENT_LABELS[expanded] || expanded} — active incidents</h3>
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
                  <tr key={inc.run_id}>
                    <td>
                      <div>{inc.title}</div>
                      <div className="muted small">{inc.incident_id}</div>
                    </td>
                    <td>{inc.service || "—"}</td>
                    <td>{inc.severity}</td>
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
