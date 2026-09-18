async function request(path, options) {
  // no-store: this is a live-status dashboard polling every few seconds --
  // a cached response is always wrong, never merely stale-but-fine.
  const res = await fetch(path, { cache: "no-store", ...options });
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

  getIncidentsInProgress: () => request("/api/incidents/in_progress"),
  getIncidents: ({ start, end } = {}) => {
    const params = new URLSearchParams();
    if (start) params.set("start", start);
    if (end) params.set("end", end);
    const qs = params.toString();
    return request(`/api/incidents${qs ? `?${qs}` : ""}`);
  },
  getIncidentDetail: (incidentId) => request(`/api/incidents/${incidentId}`),
  getIncidentStats: ({ start, end } = {}) => {
    const params = new URLSearchParams();
    if (start) params.set("start", start);
    if (end) params.set("end", end);
    const qs = params.toString();
    return request(`/api/incidents/stats${qs ? `?${qs}` : ""}`);
  },

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
