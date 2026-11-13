import { useEffect, useState, type MouseEvent } from "react";
import type { ControlApi, MetricRange } from "../api/types";
import { EnginePanel } from "../components/engine-panel";
import { ClusterUnit } from "../components/workload-units";
import { FleetFeedback } from "../components/fleet-feedback";
import { MetricsChart } from "../components/metrics-chart";
import { PageHeader } from "../components/page-header";
import { RangeSwitch } from "../components/range-switch";
import { SparksTable } from "../components/sparks-table";
import { WorkloadRow } from "../components/workload-row";
import { type LiveStatus, liveStatus } from "../hooks/fleet-stream-state";
import { useFleetStream } from "../hooks/use-fleet-stream";
import { useMetricSeries } from "../hooks/use-metric-series";
import { useWorkloadReads } from "../hooks/use-workload-reads";
import { groupByWorkload, nodeDisplayName } from "../lib/fleet";

/**
 * Every workload on one line (open one for its full unit), then every Spark in one table with the fleet's
 * trends; the Spark you pick opens on its own page.
 */
export function WorkloadsPage({
  api,
  onLive,
  onNavigate,
  onOpenSpark,
}: {
  api: ControlApi;
  onLive?(status: LiveStatus | null): void;
  onNavigate?(event: MouseEvent<HTMLAnchorElement>, path: string): void;
  onOpenSpark?(id: string): void;
}) {
  const fleet = useFleetStream(api);
  const live = liveStatus(fleet);
  useEffect(() => onLive?.(live), [onLive, live]);
  useEffect(() => () => onLive?.(null), [onLive]);
  const reads = useWorkloadReads(api);
  const day = useMetricSeries(api, "output_token_rate", "24h");
  const [range, setRange] = useState<MetricRange>("6h");
  const [openRun, setOpenRun] = useState<string | null>(null);
  const nodes = fleet.snapshot?.nodes ?? [];
  const { clusters } = groupByWorkload(nodes);
  const order = new Map(nodes.map((node, index) => [node.id, index]));
  return (
    <div className="fleet-page">
      <PageHeader
        title="Workloads"
        description="What is serving, and every Spark that carries it."
      />
      <FleetFeedback fleet={fleet} />
      {fleet.snapshot && (
        <section className="workloads-section" aria-labelledby="workloads-heading">
          <h2 id="workloads-heading">By workload</h2>
          {clusters.length === 0 ? (
            <p>Nothing is loaded on any Spark.</p>
          ) : (
            <ul className="workload-rows">
              {clusters.flatMap((cluster) =>
                cluster.units.map((unit) => (
                  <WorkloadRow
                    key={unit.runId}
                    unit={unit}
                    reads={reads}
                    day={day}
                    order={order}
                    open={openRun === unit.runId}
                    onToggle={() => setOpenRun(openRun === unit.runId ? null : unit.runId)}
                    onActivity={onNavigate && ((event) => onNavigate(event, "/activity"))}
                  >
                    <ClusterUnit
                      api={api}
                      cluster={cluster}
                      order={order}
                      reads={reads}
                      now={fleet.now}
                      live={live === "live"}
                      onOpen={onOpenSpark}
                    />
                    {unit.alias && <EnginePanel api={api} alias={unit.alias} title={unit.title} />}
                  </WorkloadRow>
                )),
              )}
            </ul>
          )}
        </section>
      )}
      {nodes.length > 0 && (
        <section className="workloads-section" aria-labelledby="all-sparks-heading">
          <h2 id="all-sparks-heading">All Sparks</h2>
          <SparksTable nodes={nodes} now={fleet.now} onSelect={onOpenSpark} />
        </section>
      )}
      <RangeSwitch range={range} onChange={setRange} />
      <MetricsChart
        api={api}
        range={range}
        names={Object.fromEntries(nodes.map((node) => [node.id, nodeDisplayName(node)]))}
      />
    </div>
  );
}
