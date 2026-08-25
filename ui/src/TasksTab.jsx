import { useEffect, useState } from "react";

import { api } from "./api.js";

const POLL_MS = 15000;
const DECIDED_BY = "ui"; // no auth/user identity exists yet -- see README follow-ups

export default function TasksTab() {
  const [approvals, setApprovals] = useState([]);
  const [rejecting, setRejecting] = useState(null); // approval_id currently showing the reason box
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(null); // incident_id currently submitting

  function load() {
    return api.getApprovals().then(setApprovals);
  }

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, []);

  async function handleApprove(incidentId) {
    setBusy(incidentId);
    try {
      await api.approveIncident(incidentId, DECIDED_BY);
      await load();
    } finally {
      setBusy(null);
    }
  }

  async function handleReject(incidentId) {
    if (!reason.trim()) return;
    setBusy(incidentId);
    try {
      await api.rejectIncident(incidentId, DECIDED_BY, reason.trim());
      setRejecting(null);
      setReason("");
      await load();
    } finally {
      setBusy(null);
    }
  }

  if (approvals.length === 0) {
    return <p className="muted">No pending approvals.</p>;
  }

  return (
    <div className="task-list">
      {approvals.map((apr) => (
        <div className="card task-card" key={apr.approval_id}>
          <div className="task-header">
            <span className={`badge badge-${apr.stage}`}>{apr.stage}</span>
            <span className="task-title">{apr.incident.title}</span>
            {apr.risk_tier && <span className={`badge badge-risk-${apr.risk_tier}`}>{apr.risk_tier}</span>}
          </div>
          <div className="muted small">
            {apr.incident.service || "unknown service"} · {apr.incident.severity} · {apr.incident_id}
          </div>
          <p>{apr.summary}</p>

          {apr.stage === "remediation" && Array.isArray(apr.proposed_plan) && (
            <ol className="plan-list">
              {apr.proposed_plan.map((step, i) => (
                <li key={i}>
                  {step.step}
                  {step.playbook_id && <span className="muted small"> — {step.playbook_id}</span>}
                </li>
              ))}
            </ol>
          )}

          <div className="task-actions">
            <button
              disabled={busy === apr.incident_id}
              onClick={() => handleApprove(apr.incident_id)}
            >
              Approve
            </button>
            <button
              className="secondary"
              disabled={busy === apr.incident_id}
              onClick={() => setRejecting(rejecting === apr.approval_id ? null : apr.approval_id)}
            >
              Reject
            </button>
          </div>

          {rejecting === apr.approval_id && (
            <div className="reject-box">
              <textarea
                placeholder="Reason for rejecting…"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <button
                disabled={busy === apr.incident_id || !reason.trim()}
                onClick={() => handleReject(apr.incident_id)}
              >
                Submit rejection
              </button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
