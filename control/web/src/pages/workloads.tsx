import { useEffect, useState } from "react";
import type { ControlApi, MetricRange } from "../api/types";
import { FleetFeedback } from "../components/fleet-feedback";
import { MetricsChart } from "../components/metrics-chart";
import { PageHeader } from "../components/page-header";
import { RangeSwitch } from "../components/range-switch";
import { SparksTable } from "../components/sparks-table";
import { type LiveStatus, liveStatus } from "../hooks/fleet-stream-state";
import { useFleetStream } from "../hooks/use-fleet-stream";
import { nodeDisplayName } from "../lib/fleet";

/** Every Spark in one table, with the fleet's trends; the Spark you pick opens on its own page. */
export function WorkloadsPage({
  api,
  onLive,
  onOpenSpark,
}: {
  api: ControlApi;
  onLive?(status: LiveStatus | null): void;
  onOpenSpark?(id: string): void;
}) {
  const fleet = useFleetStream(api);
  const live = liveStatus(fleet);
  useEffect(() => onLive?.(live), [onLive, live]);
  useEffect(() => () => onLive?.(null), [onLive]);
  const [range, setRange] = useState<MetricRange>("6h");
  const nodes = fleet.snapshot?.nodes ?? [];
  return (
    <div className="fleet-page">
      <PageHeader
        title="Workloads"
        description="Every Spark, its health, and what is running there."
      />
      <FleetFeedback fleet={fleet} />
      {nodes.length > 0 && <SparksTable nodes={nodes} now={fleet.now} onSelect={onOpenSpark} />}
      <RangeSwitch range={range} onChange={setRange} />
      <MetricsChart
        api={api}
        range={range}
        names={Object.fromEntries(nodes.map((node) => [node.id, nodeDisplayName(node)]))}
      />
    </div>
  );
}
