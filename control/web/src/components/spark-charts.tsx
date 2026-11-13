import { useState } from "react";
import type { ControlApi, Metric, MetricRange, MetricSpec } from "../api/types";
import { useMetricCatalogue } from "../hooks/use-metric-catalogue";
import { useMetricRead } from "../hooks/use-metric-series";
import { GPU_TEMP_WARN_C } from "../lib/gpu-thresholds";
import { MetricCard } from "./metric-card";

/**
 * The charts a Spark has, in order: its metric, the one summary the card's second line gives, and a
 * threshold line where there is one. A metric the catalogue does not list is not drawn; the grid grows
 * as the Controller serves more per-Spark metrics (power, clock, CPU, network).
 */
const CARDS: readonly {
  metric: Metric;
  line: "average" | "peak";
  threshold?: { value: number; label: string };
}[] = [
  { metric: "gpu_utilization", line: "average" },
  {
    metric: "gpu_temperature",
    line: "peak",
    threshold: { value: GPU_TEMP_WARN_C, label: `warns at ${GPU_TEMP_WARN_C} °C` },
  },
];

function SparkChartCard({
  api,
  nodeId,
  range,
  spec,
  card,
  color,
  crosshair,
}: {
  api: ControlApi;
  nodeId: string;
  range: MetricRange;
  spec: MetricSpec;
  card: (typeof CARDS)[number];
  color: string;
  crosshair: { at: number | null; onChange(at: number | null): void };
}) {
  const data = useMetricRead(api, card.metric, range, nodeId);
  return (
    <MetricCard
      spec={spec}
      range={range}
      data={data}
      line={card.line}
      threshold={card.threshold}
      color={color}
      crosshair={crosshair}
    />
  );
}

/** A Spark's chart grid: one range and one cursor across every chart. */
export function SparkCharts({
  api,
  nodeId,
  range,
  color,
}: {
  api: ControlApi;
  nodeId: string;
  range: MetricRange;
  color: string;
}) {
  const [retry, setRetry] = useState(0);
  const catalogue = useMetricCatalogue(api, retry);
  const [at, setAt] = useState<number | null>(null);
  const specs = Array.isArray(catalogue) ? catalogue : [];
  return (
    <>
      <div className="button-row">
        <button type="button" className="button secondary" onClick={() => setRetry((n) => n + 1)}>
          Refresh metrics
        </button>
      </div>
      {catalogue === undefined && <p role="status">Reading metrics…</p>}
      {typeof catalogue === "string" && <p role="status">Metrics unavailable: {catalogue}</p>}
      <div className="spark-chart-grid">
        {CARDS.flatMap((card) => {
          const spec = specs.find((entry) => entry.metric === card.metric);
          return spec
            ? [
                <SparkChartCard
                  key={card.metric}
                  api={api}
                  nodeId={nodeId}
                  range={range}
                  spec={spec}
                  card={card}
                  color={color}
                  crosshair={{ at, onChange: setAt }}
                />,
              ]
            : [];
        })}
      </div>
    </>
  );
}
