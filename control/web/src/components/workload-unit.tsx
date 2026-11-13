import "./workload-unit.css";
import type { WorkloadReads } from "../hooks/use-workload-reads";
import { nodeDisplayName, type WorkloadCluster } from "../lib/fleet";
import type { ReactNode } from "react";
import { WorkloadHeader, WorkloadHero } from "./workload-hero";

/**
 * Workloads that share Sparks as one unit: the first workload's header, hero and chart (the others a
 * one-line strip each), then a bracket down
 * to the Spark cards (a "Runs on" line on a phone). `children` are the cards.
 */
export function WorkloadUnit({
  cluster,
  reads,
  live,
  children,
}: {
  cluster: WorkloadCluster;
  reads: WorkloadReads;
  live: boolean;
  children: ReactNode;
}) {
  const names = cluster.nodes.map(nodeDisplayName);
  return (
    <section
      className="workload-unit card"
      aria-label={cluster.units.map((unit) => unit.title).join(" and ")}
    >
      {cluster.units.map((unit, index) =>
        index === 0 ? (
          <WorkloadHero key={unit.runId} unit={unit} reads={reads} live={live} />
        ) : (
          // A second workload on the same Sparks is one line under the first.
          <div className="workload-strip" key={unit.runId}>
            <WorkloadHeader unit={unit} />
          </div>
        ),
      )}
      {names.length > 1 && (
        <>
          <div className="unit-bracket" aria-hidden="true" />
          <p className="unit-runs-on">Runs on {names.join(" and ")}</p>
        </>
      )}
      <div className="unit-sparks">{children}</div>
    </section>
  );
}
