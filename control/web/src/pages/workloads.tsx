import { useEffect } from "react";
import type { ControlApi } from "../api/types";
import { FleetFeedback } from "../components/fleet-feedback";
import { MetricsChart } from "../components/metrics-chart";
import { PageHeader } from "../components/page-header";
import { SparkDetail } from "../components/spark-detail";
import { SparksTable } from "../components/sparks-table";
import { type LiveStatus, liveStatus } from "../hooks/fleet-stream-state";
import { useFleetStream } from "../hooks/use-fleet-stream";
import { useSelectedSpark } from "../hooks/use-selected-spark";
import { nodeDisplayName } from "../lib/fleet";

/** Every Spark in one table, with the fleet's trends; the Spark you pick opens below. */
export function WorkloadsPage({
  api,
  onLive,
}: {
  api: ControlApi;
  onLive?(status: LiveStatus | null): void;
}) {
  const fleet = useFleetStream(api);
  const live = liveStatus(fleet);
  useEffect(() => onLive?.(live), [onLive, live]);
  useEffect(() => () => onLive?.(null), [onLive]);
  const [spark, selectSpark] = useSelectedSpark();
  const nodes = fleet.snapshot?.nodes ?? [];
  return (
    <div className="fleet-page">
      <PageHeader
        title="Workloads"
        description="Every Spark, its health, and what is running there."
      />
      <FleetFeedback fleet={fleet} />
      {nodes.length > 0 && (
        <SparksTable nodes={nodes} now={fleet.now} selected={spark} onSelect={selectSpark} />
      )}
      <MetricsChart
        api={api}
        names={Object.fromEntries(nodes.map((node) => [node.id, nodeDisplayName(node)]))}
      />
      {spark && <SparkDetail api={api} id={spark} onClose={() => selectSpark(null)} />}
    </div>
  );
}
