import "./workloads.css";
import { useEffect, useState } from "react";
import type { ControlApi, MetricRange } from "../api/types";
import { CertificateNotice, expiringCertificates } from "../components/certificate-notice";
import { EmptyState } from "../components/empty-state";
import { EnginePanel } from "../components/engine-panel";
import { ClusterUnit } from "../components/workload/workload-units";
import { FleetFeedback } from "../components/fleet-feedback";
import { MetricsChart } from "../components/metrics-chart";
import { PageHeader } from "../components/page-header";
import { SparksTable } from "../components/spark/sparks-table";
import { WorkloadRow } from "../components/workload/workload-row";
import { type LiveStatus, liveStatus } from "../hooks/fleet-stream-state";
import { useFleetStream } from "../hooks/use-fleet-stream";
import { useMetricRead, useMetricSeries } from "../hooks/use-metric-series";
import { useWorkloadReads } from "../hooks/use-workload-reads";
import { groupByWorkload, nodeDisplayName } from "../lib/fleet";

/**
 * Every workload on one line (open one for its full unit), then every Spark in one table with the fleet's
 * trends; the Spark you pick opens on its own page.
 */
export function WorkloadsPage({
  api,
  onLive,
  onOpenSpark,
  onNavigateUrl,
}: {
  api: ControlApi;
  onLive?(status: LiveStatus | null): void;
  onOpenSpark?(id: string): void;
  onNavigateUrl?(url: string): void;
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
  const certificates = useMetricRead(api, "certificate_expiry", range);
  const { clusters } = groupByWorkload(nodes);
  const order = new Map(nodes.map((node, index) => [node.id, index]));
  const names = Object.fromEntries(nodes.map((node) => [node.id, nodeDisplayName(node)]));
  return (
    <div className="fleet-page">
      <PageHeader
        title="Workloads"
        description="What is serving, and every Spark that carries it."
      />
      <FleetFeedback fleet={fleet} />
      <CertificateNotice lines={expiringCertificates(certificates, names)} />
      {fleet.snapshot && (
        <section className="workloads-section" aria-labelledby="workloads-heading">
          <h2 id="workloads-heading">By workload</h2>
          {clusters.length === 0 ? (
            <EmptyState
              title="Nothing is loaded"
              description="No Spark is serving a model yet. Pick a recipe in the Library to load one."
              action={{ label: "Open the Library", onClick: () => onNavigateUrl?.("/library") }}
            />
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
                  >
                    <ClusterUnit
                      api={api}
                      cluster={{ units: [unit], nodes: cluster.nodes }}
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
      <MetricsChart api={api} range={range} onRangeChange={setRange} names={names} />
    </div>
  );
}
