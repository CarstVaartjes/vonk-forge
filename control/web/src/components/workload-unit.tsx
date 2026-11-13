import "./workload-unit.css";
import type { MetricsSeriesResponse } from "../api/types";
import { nodeDisplayName, type WorkloadCluster } from "../lib/fleet";
import type { ReactNode } from "react";
import { WorkloadHero } from "./workload-hero";

/**
 * Workloads that share Sparks as one unit: each workload's header, hero and chart, then a bracket down
 * to the Spark cards (a "Runs on" line on a phone). `children` are the cards.
 */
export function WorkloadUnit({
  cluster,
  tokens,
  requests,
  live,
  children,
}: {
  cluster: WorkloadCluster;
  tokens: MetricsSeriesResponse | null;
  requests: MetricsSeriesResponse | null;
  live: boolean;
  children: ReactNode;
}) {
  const names = cluster.nodes.map(nodeDisplayName);
  return (
    <section
      className="workload-unit card"
      aria-label={cluster.units.map((unit) => unit.title).join(" and ")}
    >
      {cluster.units.map((unit) => (
        <WorkloadHero
          key={unit.runId}
          unit={unit}
          tokens={tokens}
          requests={requests}
          live={live}
        />
      ))}
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
