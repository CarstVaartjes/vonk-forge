import type { LosslessNumber } from "lossless-json";
import { useEffect, useId, useState } from "react";
import { safeErrorText } from "../lib/error-display";
import { useMetricCatalogue } from "../hooks/use-metric-catalogue";
import { AreaChart, type ChartSeries } from "./chart/area-chart";
import { EstimateTag } from "./estimate";

import type {
  ControlApi,
  Metric,
  MetricRange,
  MetricSpec,
  MetricsSeriesResponse,
} from "../api/types";

const GIB = 1024 ** 3;
// How the Controller's unit names read on the page; titles and basis come from the catalogue.
const UNITS: Record<MetricSpec["unit"], { symbol: string; scale: number }> = {
  percent: { symbol: "%", scale: 1 },
  bytes: { symbol: "GiB", scale: GIB },
  celsius: { symbol: "°C", scale: 1 },
  watts: { symbol: "W", scale: 1 },
  watt_hours: { symbol: "Wh", scale: 1 },
  rpm: { symbol: "rpm", scale: 1 },
  per_second: { symbol: "/s", scale: 1 },
  tokens_per_second: { symbol: "tok/s", scale: 1 },
  seconds: { symbol: "s", scale: 1 },
  ratio: { symbol: "", scale: 1 },
  count: { symbol: "", scale: 1 },
};
// The trend tiles, in order; the catalogue describes each.
const TILES: Metric[] = [
  "gpu_utilization",
  "host_memory_used",
  "gpu_temperature",
  "request_rate",
  "output_token_rate",
];
const SERIES_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];

/**
 * A Spark keeps one colour in every tile: its position in `names`, which the Fleet page builds in
 * the snapshot's node order. Series that are not Sparks (models) take the colour of their order
 * in the tile.
 */
function seriesColor(labels: Record<string, string>, names: Record<string, string>, index: number) {
  const position = labels.node_id ? Object.keys(names).indexOf(labels.node_id) : -1;
  return SERIES_COLORS[(position >= 0 ? position : index) % SERIES_COLORS.length];
}

function formatValue(value: number, unit: string): string {
  const text = Math.abs(value) >= 100 ? value.toFixed(0) : value.toPrecision(3);
  return `${Number(text)} ${unit}`.trim();
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
  return labels.alias ?? "All Sparks";
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
  const chartSeries: ChartSeries[] = data.series.map((series, index) => ({
    key: JSON.stringify(series.labels),
    name: seriesName(series.labels, names),
    color: seriesColor(series.labels, names, index),
    points: series.points.map((point) => ({
      t: Number(point.timestamp),
      v: point.value === null ? null : Number(point.value) / scale,
    })),
  }));
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
  names = {},
}: {
  api: ControlApi;
  node?: string;
  names?: Record<string, string>;
}) {
  const id = useId();
  const [range, setRange] = useState<MetricRange>("6h");
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
        <label>
          Range{" "}
          <select
            value={range}
            onChange={(event) => setRange(event.currentTarget.value as MetricRange)}
          >
            {(["1h", "6h", "24h", "7d", "30d"] as const).map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
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
                {spec.title}
                <EstimateTag basis={spec.basis} />
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
