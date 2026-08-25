import { useEffect, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api } from "./api.js";

const AGENT_COLORS = {
  triage: "#7c9cff",
  diagnosis: "#f6a35c",
  remediation: "#e0679c",
  validation: "#5fc9a8",
};

function money(v) {
  return `$${(v ?? 0).toFixed(4)}`;
}

export default function FinOpsTab() {
  const [summary, setSummary] = useState(null);
  const [byAgent, setByAgent] = useState([]);
  const [timeseries, setTimeseries] = useState([]);

  useEffect(() => {
    api.getFinOpsSummary().then(setSummary);
    api.getFinOpsByAgent().then(setByAgent);
    api.getFinOpsTimeseries(14).then((rows) => {
      // Flatten by_agent into top-level keys so recharts can plot each
      // agent as its own stacked Area series.
      setTimeseries(
        rows.map((r) => ({ date: r.date, total_usd: r.total_usd, ...r.by_agent }))
      );
    });
  }, []);

  const agentTypes = Object.keys(AGENT_COLORS);

  return (
    <div>
      <div className="card-grid">
        <div className="card stat-card">
          <div className="stat-label">Total cost</div>
          <div className="stat-value">{summary ? money(summary.total_cost_usd) : "…"}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-label">Last 24h</div>
          <div className="stat-value">{summary ? money(summary.cost_last_24h_usd) : "…"}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-label">Last 7d</div>
          <div className="stat-value">{summary ? money(summary.cost_last_7d_usd) : "…"}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-label">LLM calls</div>
          <div className="stat-value">{summary ? summary.call_count : "…"}</div>
        </div>
      </div>

      <div className="panel">
        <h3>Cost trend (14 days, by agent)</h3>
        <ResponsiveContainer width="100%" height={280}>
          <AreaChart data={timeseries}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
            <XAxis dataKey="date" />
            <YAxis tickFormatter={(v) => `$${v}`} />
            <Tooltip formatter={(v) => money(v)} />
            <Legend />
            {agentTypes.map((agentType) => (
              <Area
                key={agentType}
                type="monotone"
                dataKey={agentType}
                stackId="cost"
                stroke={AGENT_COLORS[agentType]}
                fill={AGENT_COLORS[agentType]}
                fillOpacity={0.5}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>

      <div className="panel">
        <h3>Cost by agent (all time)</h3>
        <table>
          <thead>
            <tr>
              <th>Agent</th>
              <th>Calls</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {byAgent.map((row) => (
              <tr key={row.agent_type}>
                <td>{row.agent_type}</td>
                <td>{row.call_count}</td>
                <td>{money(row.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
