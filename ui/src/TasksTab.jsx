import { CheckCircle2, Clock, XCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "./api.js";

const POLL_MS = 15000;
const DECIDED_BY = "ui"; // no auth/user identity exists yet -- see README follow-ups

const STAGE_TITLE = {
  diagnosis: "Review root cause",
  remediation: "Approve remediation plan",
};

function timeAgo(iso) {
  if (!iso) return "";
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  return `${Math.round(mins / 60)}h ago`;
}

export default function TasksTab() {
  const [approvals, setApprovals] = useState([]);
  const [rejecting, setRejecting] = useState(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(null);

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

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Tasks</h1>
          <p>Actions awaiting your approval before an agent can proceed.</p>
        </div>
        {approvals.length > 0 && (
          <span className="badge badge-risk-medium">{approvals.length} Pending Approvals</span>
        )}
      </div>

      {approvals.length === 0 && <p className="muted">No pending approvals.</p>}

      <div className="task-list">
        {approvals.map((apr) => (
          <div className="card task-card" key={apr.approval_id}>
            <div className="task-top">
              <Clock size={17} />
              <span className="task-title">
                {STAGE_TITLE[apr.stage] || "Review"} — {apr.incident.title}
              </span>
              <span className={`badge badge-${apr.incident.severity?.toLowerCase()}`}>
                {apr.incident.severity}
              </span>
              {apr.risk_tier && (
                <span className={`badge badge-risk-${apr.risk_tier}`}>{apr.risk_tier} risk</span>
              )}
              <div style={{ flex: 1 }} />
              <button className="btn-approve" disabled={busy === apr.incident_id} onClick={() => handleApprove(apr.incident_id)}>
                <CheckCircle2 size={14} /> Approve
              </button>
              <button
                className="btn-reject"
                disabled={busy === apr.incident_id}
                onClick={() => setRejecting(rejecting === apr.approval_id ? null : apr.approval_id)}
              >
                <XCircle size={14} /> Reject
              </button>
            </div>

            <div className="task-meta small">
              {apr.approval_id} · Incident #{apr.incident_id} · {apr.incident.service || "unknown service"} ·{" "}
              {timeAgo(apr.created_at)}
            </div>

            <div className="task-summary">{apr.summary}</div>

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

            {rejecting === apr.approval_id && (
              <div className="reject-box">
                <textarea
                  placeholder="Reason for rejecting…"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
                <button
                  className="btn-reject"
                  style={{ alignSelf: "flex-start" }}
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
    </div>
  );
}
