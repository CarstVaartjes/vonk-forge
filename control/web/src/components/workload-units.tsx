import type { ControlApi, VisualFleetNode } from "../api/types";
import { useMetricSeries } from "../hooks/use-metric-series";
import { groupByWorkload } from "../lib/fleet";
import { SparkCard } from "./spark-card";
import { WorkloadUnit } from "./workload-unit";

/** The fleet's workloads as units with their Spark cards, then the Sparks that run nothing. */
export function WorkloadUnits({
  api,
  nodes,
  now,
  live,
  onOpen,
}: {
  api: Pick<ControlApi, "metricsSeries">;
  nodes: VisualFleetNode[];
  now: Date;
  live: boolean;
  onOpen?(id: string): void;
}) {
  const tokens = useMetricSeries(api, "output_token_rate", "1h");
  const requests = useMetricSeries(api, "request_rate", "1h");
  const { clusters, idle } = groupByWorkload(nodes);
  const order = new Map(nodes.map((node, index) => [node.id, index]));
  const card = (node: VisualFleetNode, rank?: number) => (
    <SparkCard
      key={node.id}
      api={api}
      node={node}
      now={now}
      index={order.get(node.id) ?? 0}
      rank={rank}
      onOpen={onOpen}
    />
  );
  return (
    <>
      {clusters.map((cluster) => {
        // A rank means something only when one workload spans several Sparks.
        const [only] = cluster.units;
        const ranked = cluster.units.length === 1 && cluster.nodes.length > 1 ? only : undefined;
        return (
          <WorkloadUnit
            key={cluster.units.map((unit) => unit.runId).join("+")}
            cluster={cluster}
            tokens={tokens}
            requests={requests}
            live={live}
          >
            {cluster.nodes.map((node) => card(node, ranked?.ranks[node.id]))}
          </WorkloadUnit>
        );
      })}
      {idle.length > 0 && (
        <section aria-label="Idle Sparks">
          <h2>Idle</h2>
          <div className="idle-sparks">
            {idle.map((node) => (
              <div className="card" key={node.id}>
                {card(node)}
              </div>
            ))}
          </div>
        </section>
      )}
    </>
  );
}
