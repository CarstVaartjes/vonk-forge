import type { LosslessNumber } from "lossless-json";
import { useEffect, useId, useState } from "react";
import { safeErrorText } from "../lib/error-display";
import { useMetricCatalogue } from "../hooks/use-metric-catalogue";
import { AreaChart, type ChartSeries } from "./chart/area-chart";
import { sparkColor } from "./chart/series";
import { EstimateTag } from "./estimate";
import { UNITS, formatValue } from "../lib/metric-units";

import type {
  ControlApi,
  Metric,
  MetricRange,
  MetricSpec,
  MetricsSeriesResponse,
} from "../api/types";

// The trend tiles, in order; the catalogue describes each.
const TILES: Metric[] = [
  "gpu_utilization",
  "host_memory_used",
  "gpu_temperature",
  "request_rate",
  "output_token_rate",
];

/**
 * A Spark keeps one colour in every tile: its position in `names`, which the Fleet page builds in
 * the enrolment order. Series that are not Sparks (models) take the colour of their order
 * in the tile.
 */
function seriesColor(labels: Record<string, string>, names: Record<string, string>, index: number) {
  const position = labels.node_id ? Object.keys(names).indexOf(labels.node_id) : -1;
  return sparkColor(position >= 0 ? position : index);
}

/** "12 days", "5 hours", "20 minutes": the time left, in the largest whole unit. */
export function formatTimeLeft(seconds: number): string {
  const [size, name] =
    seconds >= 86400 ? [86400, "day"] : seconds >= 3600 ? [3600, "hour"] : [60, "minute"];
  const count = Math.max(1, Math.floor(seconds / size));
  return `${count} ${name}${count === 1 ? "" : "s"}`;
}

function seriesName(labels: Record<string, string>, names: Record<string, string>): string {
  const node = labels.node_id;
  if (node) return names[node] ?? node;
  // A series without a label is the Controller's fleet line.
  return labels.alias ?? "Fleet";
}

/** Latest, average, peak and total as the Controller computed them; an estimate says so. */
function summaryText(
  summary: MetricsSeriesResponse["series"][number]["summary"],
  spec: MetricSpec,
) {
  const { symbol, scale } = UNITS[spec.unit];
  const part = (label: string, value: number | LosslessNumber | null) =>
    value === null ? [] : [`${label} ${formatValue(Number(value) / scale, symbol)}`];
  const total =
    summary.total === null ? [] : [`total ${Number(Number(summary.total).toPrecision(3))}`];
  const parts = [
    ...part("now", summary.latest),
    ...part("average", summary.average),
    ...part("peak", summary.peak),
    ...total,
  ];
  const text = parts.length ? parts.join(" · ") : "No data in this range";
  return spec.basis === "estimated" ? `${text} (estimate)` : text;
}

/** The chart's series for a response: display units, a Spark's own colour, gaps kept as gaps. */
export function toChartSeries(
  data: MetricsSeriesResponse,
  names: Record<string, string> = {},
): ChartSeries[] {
  const { scale } = UNITS[data.spec.unit];
  return data.series.map((series, index) => ({
    key: JSON.stringify(series.labels),
    name: seriesName(series.labels, names),
    color: seriesColor(series.labels, names, index),
    points: series.points.map((point) => ({
      t: Number(point.timestamp),
      v: point.value === null ? null : Number(point.value) / scale,
    })),
  }));
}

export function SeriesChart({
  data,
  names = {},
}: {
  data: MetricsSeriesResponse;
  names?: Record<string, string>;
}) {
  const { symbol: unit, scale } = UNITS[data.spec.unit];
  if (!data.series.some((series) => series.points.some((point) => point.value !== null)))
    return <p role="status">No samples in this range</p>;
  const chartSeries = toChartSeries(data, names);
  const sentence = `${data.spec.title}, ${data.range}: ${data.series
    .map(
      (series) => `${seriesName(series.labels, names)} ${summaryText(series.summary, data.spec)}`,
    )
    .join("; ")}`;
  return (
    <>
      <AreaChart
        title={data.spec.title}
        label={sentence}
        series={chartSeries}
        start={Number(data.start)}
        end={Number(data.end)}
        format={(value) => formatValue(value, unit)}
      />
      <ul aria-label="Series legend">
        {data.series.map((series, index) => (
          <li
            key={JSON.stringify(series.labels)}
            style={{ color: seriesColor(series.labels, names, index) }}
          >
            {seriesName(series.labels, names)} · {unit}
          </li>
        ))}
      </ul>
      <ul aria-label="Series summary">
        {data.series.map((series) => (
          <li key={JSON.stringify(series.labels)}>
            {seriesName(series.labels, names)}: {summaryText(series.summary, data.spec)}
          </li>
        ))}
      </ul>
      {data.spec.basis === "estimated" && <p>{data.spec.hint}</p>}
    </>
  );
}

// A tile holds its series, or the message of the read that failed.
type Tile = MetricsSeriesResponse | string;

function timeLeft(data: MetricsSeriesResponse, names: Record<string, string>): string[] {
  return data.series.flatMap((series) => {
    const last = [...series.points].reverse().find((point) => point.value !== null);
    if (!last) return [];
    const left = Number(last.value);
    const name = seriesName(series.labels, names);
    return [
      left <= 0
        ? `${name}: certificate expired`
        : `${name}: certificate expires in ${formatTimeLeft(left)}`,
    ];
  });
}

export function MetricsChart({
  api,
  node,
  range,
  names = {},
}: {
  api: ControlApi;
  node?: string;
  range: MetricRange;
  names?: Record<string, string>;
}) {
  const id = useId();
  const [tiles, setTiles] = useState<Record<string, Tile | undefined>>({});
  const [retry, setRetry] = useState(0);
  const catalogue = useMetricCatalogue(api, retry);
  const wanted: Metric[] = [...TILES, "certificate_expiry"];
  useEffect(() => {
    const controller = new AbortController();
    setTiles({});
    // Each tile reads on its own, so one failed read leaves the others standing.
    for (const metric of wanted) {
      api.metricsSeries(metric, range, node, controller.signal).then(
        (data) => {
          if (!controller.signal.aborted) setTiles((current) => ({ ...current, [metric]: data }));
        },
        (cause) => {
          if (!controller.signal.aborted)
            setTiles((current) => ({
              ...current,
              [metric]: safeErrorText(
                cause instanceof Error ? cause.message : "Metrics unavailable",
              ),
            }));
        },
      );
    }
    return () => controller.abort();
  }, [api, range, node, retry]);
  const expiryRead = tiles.certificate_expiry;
  const expiry = typeof expiryRead === "object" ? expiryRead : undefined;
  return (
    <section aria-labelledby={id} className="fleet-metrics">
      <h2 id={id}>Trends</h2>
      <div className="button-row">
        <button
          type="button"
          className="button secondary"
          onClick={() => setRetry((value) => value + 1)}
        >
          Refresh metrics
        </button>
      </div>
      {typeof catalogue === "string" && <p role="status">Metrics unavailable: {catalogue}</p>}
      <div className="metric-tiles">
        {(Array.isArray(catalogue) ? TILES : []).map((metric) => {
          const spec = (catalogue as MetricSpec[]).find((entry) => entry.metric === metric);
          const state = tiles[metric];
          if (!spec) return null;
          return (
            <section key={metric} className="metric-tile card" aria-label={spec.title}>
              <h3>
                {spec.title} <EstimateTag basis={spec.basis} />
              </h3>
              {typeof state === "string" ? (
                <p role="status">Metrics unavailable: {state}</p>
              ) : state ? (
                <SeriesChart data={state} names={names} />
              ) : (
                <p role="status">Reading metrics…</p>
              )}
            </section>
          );
        })}
      </div>
      {expiry && timeLeft(expiry, names).map((line) => <p key={line}>{line}</p>)}
    </section>
  );
}
