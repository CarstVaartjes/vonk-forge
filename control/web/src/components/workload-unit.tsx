import "./workload-unit.css";
import type { ReactNode } from "react";
import { RunState } from "../api/vocabulary.generated";
import type { WorkloadGroup } from "../lib/fleet";
import { StatusPill, type StatusTone } from "./status-pill";

/** The run's state word (a RunState, or `degraded` / `state unknown` from `runStateWord`) as a tone. */
const STATE_TONES: Record<string, StatusTone> = {
  [RunState.RUNNING]: "ok",
  [RunState.FAILED]: "bad",
  [RunState.LOST]: "bad",
  degraded: "warn",
  "state unknown": "neutral",
};

/** One workload as a unit: its alias, title and state, with the Sparks that carry it below. */
export function WorkloadUnit({ unit, children }: { unit: WorkloadGroup; children: ReactNode }) {
  return (
    <section className="workload-unit card" aria-label={unit.title}>
      <header>
        {unit.alias && <code className="alias">{unit.alias}</code>}
        <h2>{unit.title}</h2>
        <StatusPill tone={STATE_TONES[unit.state] ?? "info"}>{unit.state}</StatusPill>
        <span className="unit-facts">
          {unit.nodes.length} {unit.nodes.length === 1 ? "Spark" : "Sparks"}
        </span>
      </header>
      <div className="unit-sparks">{children}</div>
    </section>
  );
}
