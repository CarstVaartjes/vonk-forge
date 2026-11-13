import type { ControlApi, VisualFleetNode } from "../api/types";
import type { WorkloadReads } from "../hooks/use-workload-reads";
import { groupByWorkload, type WorkloadCluster } from "../lib/fleet";
import { SparkCard } from "./spark-card";
import { WorkloadUnit } from "./workload-unit";

/** One cluster of workloads as a unit, with a Spark card for each Spark that carries it. */
export function ClusterUnit({
  api,
  cluster,
  order,
  reads,
  now,
  live,
  onOpen,
}: {
  api: Pick<ControlApi, "metricsSeries">;
  cluster: WorkloadCluster;
  /** Each Spark's place in the enrolment order, which fixes its colour. */
  order: ReadonlyMap<string, number>;
  reads: WorkloadReads;
  now: Date;
  live: boolean;
  onOpen?(id: string): void;
}) {
  // A rank means something only when one workload spans several Sparks.
  const [only] = cluster.units;
  const ranked = cluster.units.length === 1 && cluster.nodes.length > 1 ? only : undefined;
  return (
    <WorkloadUnit cluster={cluster} reads={reads} live={live}>
      {cluster.nodes.map((node) => (
        <SparkCard
          key={node.id}
          api={api}
          node={node}
          now={now}
          index={order.get(node.id) ?? 0}
          rank={ranked?.ranks[node.id]}
          onOpen={onOpen}
        />
      ))}
    </WorkloadUnit>
  );
}

/** The fleet's workloads as units with their Spark cards, then the Sparks that run nothing. */
export function WorkloadUnits({
  api,
  nodes,
  reads,
  now,
  live,
  onOpen,
}: {
  api: Pick<ControlApi, "metricsSeries">;
  nodes: VisualFleetNode[];
  /** The page's reads: the units and the page's own figures share them. */
  reads: WorkloadReads;
  now: Date;
  live: boolean;
  onOpen?(id: string): void;
}) {
  const { clusters, idle } = groupByWorkload(nodes);
  const order = new Map(nodes.map((node, index) => [node.id, index]));
  return (
    <>
      {clusters.map((cluster) => (
        <ClusterUnit
          key={cluster.units.map((unit) => unit.runId).join("+")}
          api={api}
          cluster={cluster}
          order={order}
          reads={reads}
          now={now}
          live={live}
          onOpen={onOpen}
        />
      ))}
      {idle.length > 0 && (
        <section aria-label="Idle Sparks">
          <h2>Idle</h2>
          <div className="idle-sparks">
            {idle.map((node) => (
              <div className="card" key={node.id}>
                <SparkCard
                  api={api}
                  node={node}
                  now={now}
                  index={order.get(node.id) ?? 0}
                  onOpen={onOpen}
                />
              </div>
            ))}
          </div>
        </section>
      )}
    </>
  );
}
