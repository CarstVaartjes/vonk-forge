import "./metric-card.css";
import type { MetricRange, MetricSpec, MetricsSeriesResponse } from "../api/types";
import { NOT_REPORTED_TEXT } from "../lib/fleet";
import { UNITS, formatValue } from "../lib/metric-units";
import { AreaChart } from "./chart/area-chart";
import { EstimateTag } from "./estimate";
import { toChartSeries } from "./metrics-chart";

/** "24h" reads "24 h". */
const rangeWords = (range: MetricRange) => range.replace(/^(\d+)/, "$1 ");

/**
 * One metric as a card: its title, the Controller's latest value, one summary line (average or peak), and
 * the chart with an optional threshold line. `data` is the series read: undefined while it loads, null
 * after a failed read (Not reported). The card shows the first series of `data`.
 */
export function MetricCard({
  spec,
  range,
  data,
  line,
  threshold,
  color,
  crosshair,
}: {
  spec: MetricSpec;
  range: MetricRange;
  data: MetricsSeriesResponse | null | undefined;
  line: "average" | "peak";
  threshold?: { value: number; label: string };
  color: string;
  crosshair?: { at: number | null; onChange(at: number | null): void };
}) {
  const { symbol, scale } = UNITS[spec.unit];
  const summary = data?.series[0]?.summary;
  const show = (value: unknown) =>
    value === null || value === undefined ? null : formatValue(Number(value) / scale, symbol);
  const now = show(summary?.latest);
  const extra = show(line === "average" ? summary?.average : summary?.peak);
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
          {rangeWords(range)} {line} {extra}
        </p>
      )}
      {data === undefined && <p role="status">Reading metrics…</p>}
      {data === null && <p role="status">{NOT_REPORTED_TEXT}</p>}
      {data && (
        <AreaChart
          title={spec.title}
          label={`${spec.title}, last ${rangeWords(range)}${now ? `, now ${now}` : ""}${extra ? `, ${line} ${extra}` : ""}`}
          series={toChartSeries(data).map((series) => ({ ...series, color }))}
          start={Number(data.start)}
          end={Number(data.end)}
          format={(value) => formatValue(value, symbol)}
          threshold={threshold}
          crosshair={crosshair}
        />
      )}
    </section>
  );
}
