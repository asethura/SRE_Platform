import { useState } from "react";

import FinOpsTab from "./FinOpsTab.jsx";
import FleetTab from "./FleetTab.jsx";
import TasksTab from "./TasksTab.jsx";

const TABS = [
  { id: "fleet", label: "Fleet", Component: FleetTab },
  { id: "finops", label: "FinOps", Component: FinOpsTab },
  { id: "tasks", label: "Tasks", Component: TasksTab },
];

export default function App() {
  const [tab, setTab] = useState("fleet");
  const active = TABS.find((t) => t.id === tab);

  return (
    <div className="app">
      <header>
        <h1>SRE Platform</h1>
        <nav>
          {TABS.map((t) => (
            <button
              key={t.id}
              className={t.id === tab ? "tab active" : "tab"}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>
      <main>{active && <active.Component />}</main>
    </div>
  );
}
