import { ClipboardList, LineChart, Server, ShieldCheck } from "lucide-react";
import { useState } from "react";

import FinOpsTab from "./FinOpsTab.jsx";
import FleetTab from "./FleetTab.jsx";
import TasksTab from "./TasksTab.jsx";

const TABS = [
  { id: "fleet", label: "Fleet", icon: Server, Component: FleetTab },
  { id: "finops", label: "FinOps", icon: LineChart, Component: FinOpsTab },
  { id: "tasks", label: "Tasks", icon: ClipboardList, Component: TasksTab },
];

export default function App() {
  const [tab, setTab] = useState("fleet");
  const active = TABS.find((t) => t.id === tab);

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <ShieldCheck size={20} />
          SRE Platform
        </div>
        <nav className="sidebar-nav">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={t.id === tab ? "sidebar-link active" : "sidebar-link"}
              onClick={() => setTab(t.id)}
            >
              <t.icon size={17} />
              {t.label}
            </button>
          ))}
        </nav>
      </aside>
      <main>{active && <active.Component />}</main>
    </div>
  );
}
