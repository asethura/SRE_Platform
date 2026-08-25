async function request(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${options?.method || "GET"} ${path} -> ${res.status}: ${body}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  getFleet: () => request("/api/fleet"),
  getFleetActive: (agentType) => request(`/api/fleet/${agentType}/active`),

  getFinOpsSummary: () => request("/api/finops/summary"),
  getFinOpsByAgent: () => request("/api/finops/by_agent"),
  getFinOpsTimeseries: (days = 14) => request(`/api/finops/timeseries?days=${days}`),

  getApprovals: () => request("/api/approvals"),
  approveIncident: (incidentId, decidedBy) =>
    request(`/api/incidents/${incidentId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decided_by: decidedBy }),
    }),
  rejectIncident: (incidentId, decidedBy, reason) =>
    request(`/api/incidents/${incidentId}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decided_by: decidedBy, reason }),
    }),
};
