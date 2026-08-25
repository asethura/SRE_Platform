import { Activity, CalendarDays, Clock, DollarSign, PieChart as PieIcon, TrendingUp } from "lucide-react";
import { useEffect, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api } from "./api.js";

const AGENT_COLORS = {
  triage: "#2563eb",
  diagnosis: "#d97706",
  remediation: "#7c3aed",
  validation: "#16a34a",
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
      setTimeseries(
        rows.map((r) => ({ date: r.date, total_usd: r.total_usd, ...r.by_agent }))
      );
    });
  }, []);

  const agentTypes = Object.keys(AGENT_COLORS);
  const pieData = byAgent.filter((r) => r.cost_usd > 0);

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>FinOps</h1>
          <p>LLM cost consumed by this platform — trend over time and breakdown by agent.</p>
        </div>
      </div>

      <div className="card-grid">
        <div className="card">
          <div className="stat-label"><DollarSign size={14} /> Total cost</div>
          <div className="stat-value">{summary ? money(summary.total_cost_usd) : "…"}</div>
        </div>
        <div className="card">
          <div className="stat-label"><Clock size={14} /> Last 24h</div>
          <div className="stat-value">{summary ? money(summary.cost_last_24h_usd) : "…"}</div>
        </div>
        <div className="card">
          <div className="stat-label"><CalendarDays size={14} /> Last 7d</div>
          <div className="stat-value">{summary ? money(summary.cost_last_7d_usd) : "…"}</div>
        </div>
        <div className="card">
          <div className="stat-label"><Activity size={14} /> LLM calls</div>
          <div className="stat-value">{summary ? summary.call_count : "…"}</div>
        </div>
      </div>

      <div className="card-grid" style={{ gridTemplateColumns: "2fr 1fr" }}>
        <div className="card panel">
          <div className="panel-header"><TrendingUp size={15} /> Cost trend (14 days, by agent)</div>
          <ResponsiveContainer width="100%" height={260}>
            <AreaChart data={timeseries}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis dataKey="date" stroke="var(--muted)" fontSize={12} />
              <YAxis tickFormatter={(v) => `$${v}`} stroke="var(--muted)" fontSize={12} />
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
                  fillOpacity={0.45}
                />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        </div>

        <div className="card panel">
          <div className="panel-header"><PieIcon size={15} /> Cost by agent</div>
          {pieData.length === 0 ? (
            <p className="muted">No cost recorded yet.</p>
          ) : (
            <>
              <ResponsiveContainer width="100%" height={160}>
                <PieChart>
                  <Pie
                    data={pieData}
                    dataKey="cost_usd"
                    nameKey="agent_type"
                    innerRadius={45}
                    outerRadius={70}
                    paddingAngle={2}
                  >
                    {pieData.map((row) => (
                      <Cell key={row.agent_type} fill={AGENT_COLORS[row.agent_type] || "#9ca3af"} />
                    ))}
                  </Pie>
                  <Tooltip formatter={(v) => money(v)} />
                </PieChart>
              </ResponsiveContainer>
              <div className="fleet-breakdown" style={{ marginTop: 4 }}>
                {pieData.map((row) => (
                  <div className="fleet-breakdown-row" key={row.agent_type}>
                    <span className="dot" style={{ background: AGENT_COLORS[row.agent_type] }} />
                    {row.agent_type} <b>{money(row.cost_usd)}</b>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      <div className="card panel">
        <div className="panel-header">Cost by agent — detail</div>
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
                <td style={{ textTransform: "capitalize" }}>{row.agent_type}</td>
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
