import { useState } from "react";
import type { ControlApi, Metric, MetricRange, MetricSpec } from "../api/types";
import { useMetricCatalogue } from "../hooks/use-metric-catalogue";
import { useMetricRead } from "../hooks/use-metric-series";
import { aliasSeries } from "../lib/metric-units";
import { MetricCard } from "./metric-card";
import { RangeSwitch } from "./range-switch";

/**
 * The charts of one workload, one per catalogue metric, in reading order: speed, latency, queue, caches,
 * trouble. A metric the catalogue does not list, or that has no samples for this alias (the engine does
 * not report it), is not drawn; gateway estimates carry "est." from the catalogue's `basis`.
 */
const CARDS: readonly { metric: Metric; line: "average" | "peak" }[] = [
  { metric: "decode_throughput", line: "average" },
  { metric: "prefill_throughput", line: "average" },
  { metric: "ttft_mean", line: "average" },
  { metric: "gateway_ttft_p95", line: "peak" },
  { metric: "time_per_token_mean", line: "average" },
  { metric: "e2e_latency_mean", line: "average" },
  { metric: "requests_running", line: "peak" },
  { metric: "requests_waiting", line: "peak" },
  { metric: "kv_cache_usage", line: "peak" },
  { metric: "prefix_cache_hit_rate", line: "average" },
  { metric: "spec_decode_acceptance", line: "average" },
  { metric: "preemption_rate", line: "peak" },
  { metric: "error_rate", line: "peak" },
];

function EngineChart({
  api,
  alias,
  range,
  card,
  spec,
  crosshair,
}: {
  api: Pick<ControlApi, "metricsSeries">;
  alias: string;
  range: MetricRange;
  card: (typeof CARDS)[number];
  spec: MetricSpec;
  crosshair: { at: number | null; onChange(at: number | null): void };
}) {
  const data = useMetricRead(api, card.metric, range);
  if (data === undefined) return null;
  const reading = aliasSeries(data, alias);
  // A read that failed is Not reported; a read that succeeded without samples for this alias is not drawn.
  if (data !== null && !reading?.series.points.some((point) => point.value !== null)) return null;
  return (
    <MetricCard
      spec={spec}
      range={range}
      data={reading && { ...reading.data, series: [reading.series] }}
      line={card.line}
      color="var(--model-ink)"
      crosshair={crosshair}
    />
  );
}

/** A workload's engine charts under its unit: one range and one cursor across all of them. */
export function EnginePanel({
  api,
  alias,
  title,
}: {
  api: Pick<ControlApi, "metricsSeries" | "metricsCatalogue">;
  alias: string;
  title: string;
}) {
  const [range, setRange] = useState<MetricRange>("1h");
  const [at, setAt] = useState<number | null>(null);
  const catalogue = useMetricCatalogue(api);
  const specs = Array.isArray(catalogue) ? catalogue : [];
  return (
    <section className="engine-panel" aria-label={`${title} engine`}>
      <header>
        <h3>Engine</h3>
        <RangeSwitch range={range} onChange={setRange} />
      </header>
      {typeof catalogue === "string" && <p role="status">Metrics unavailable: {catalogue}</p>}
      <div className="spark-chart-grid">
        {CARDS.flatMap((card) => {
          const spec = specs.find((entry) => entry.metric === card.metric);
          return spec
            ? [
                <EngineChart
                  key={card.metric}
                  api={api}
                  alias={alias}
                  range={range}
                  card={card}
                  spec={spec}
                  crosshair={{ at, onChange: setAt }}
                />,
              ]
            : [];
        })}
      </div>
    </section>
  );
}
