import { useState } from "react";
import type { ControlApi, Metric, MetricRange, MetricSpec } from "../api/types";
import { useMetricCatalogue } from "../hooks/use-metric-catalogue";
import { useMetricRead } from "../hooks/use-metric-series";
import { NOT_REPORTED_TEXT } from "../lib/fleet";
import { GPU_TEMP_WARN_C } from "../lib/gpu-thresholds";
import { UNITS, formatValue } from "../lib/metric-units";
import { AreaChart } from "./chart/area-chart";
import { EstimateTag } from "./estimate";
import { toChartSeries } from "./metrics-chart";

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

/** "24h" reads "24 h". */
const rangeWords = (range: MetricRange) => range.replace(/^(\d+)/, "$1 ");

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
  const { symbol, scale } = UNITS[spec.unit];
  const summary = data?.series[0]?.summary;
  const show = (value: unknown) =>
    value === null || value === undefined ? null : formatValue(Number(value) / scale, symbol);
  const now = show(summary?.latest);
  const extra = show(card.line === "average" ? summary?.average : summary?.peak);
  return (
    <section className="card spark-chart-card" aria-label={spec.title}>
      <header>
        <h3>
          {spec.title} <EstimateTag basis={spec.basis} />
        </h3>
        <strong>{now ?? NOT_REPORTED_TEXT}</strong>
      </header>
      {extra && (
        <p className="spark-chart-line">
          {rangeWords(range)} {card.line} {extra}
        </p>
      )}
      {data === undefined && <p role="status">Reading metrics…</p>}
      {data === null && <p role="status">{NOT_REPORTED_TEXT}</p>}
      {data && (
        <AreaChart
          title={spec.title}
          label={`${spec.title}, last ${rangeWords(range)}${now ? `, now ${now}` : ""}${extra ? `, ${card.line} ${extra}` : ""}`}
          series={toChartSeries(data).map((series) => ({ ...series, color }))}
          start={Number(data.start)}
          end={Number(data.end)}
          format={(value) => formatValue(value, symbol)}
          threshold={card.threshold}
          crosshair={crosshair}
        />
      )}
    </section>
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
  const catalogue = useMetricCatalogue(api);
  const [at, setAt] = useState<number | null>(null);
  if (typeof catalogue === "string") return <p role="status">Metrics unavailable: {catalogue}</p>;
  return (
    <div className="spark-chart-grid">
      {CARDS.flatMap((card) => {
        const spec = catalogue?.find((entry) => entry.metric === card.metric);
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
  );
}
