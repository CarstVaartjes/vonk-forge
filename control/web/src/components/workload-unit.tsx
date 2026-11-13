import "./workload-unit.css";
import type { ReactNode } from "react";
import type { WorkloadGroup } from "../lib/fleet";
import { StatusPill } from "./status-pill";

/** One workload as a unit: its alias, title and state, with the Sparks that carry it below. */
export function WorkloadUnit({ unit, children }: { unit: WorkloadGroup; children: ReactNode }) {
  return (
    <section className="workload-unit card" aria-label={unit.title}>
      <header>
        {unit.alias && <code className="unit-alias">{unit.alias}</code>}
        <h2>{unit.title}</h2>
        <StatusPill tone={unit.tone}>{unit.state}</StatusPill>
        <span className="unit-facts">
          {unit.nodes.length} {unit.nodes.length === 1 ? "Spark" : "Sparks"}
        </span>
      </header>
      <div className="unit-sparks">{children}</div>
    </section>
  );
}
