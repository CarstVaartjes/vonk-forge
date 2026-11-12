import { useEffect, useId, useState } from "react";
import type { ControlApi, Metric, MetricRange, MetricsSeriesResponse } from "../api/types";
import { safeErrorText } from "../lib/error-display";

type Unit = "%" | "GiB" | "°C" | "requests/s" | "tok/s";
const GIB = 1024 ** 3;
const TILES: Array<{
  metric: Metric;
  title: string;
  unit: Unit;
  scale: number;
  perModel?: boolean;
}> = [
  { metric: "gpu_utilization", title: "GPU utilisation", unit: "%", scale: 1 },
  { metric: "host_memory_used", title: "Unified memory used", unit: "GiB", scale: GIB },
  { metric: "gpu_temperature", title: "GPU temperature", unit: "°C", scale: 1 },
  {
    metric: "request_rate",
    title: "Requests per model",
    unit: "requests/s",
    scale: 1,
    perModel: true,
  },
  {
    metric: "output_token_rate",
    title: "Generated tokens per model",
    unit: "tok/s",
    scale: 1,
    perModel: true,
  },
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

function formatValue(value: number, unit: Unit): string {
  const text = Math.abs(value) >= 100 ? value.toFixed(0) : value.toPrecision(3);
  return `${Number(text)} ${unit}`;
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
  return labels.requested_model ?? "All Sparks";
}

export function SeriesChart({
  data,
  unit,
  scale = 1,
  names = {},
}: {
  data: MetricsSeriesResponse;
  unit: Unit;
  scale?: number;
  names?: Record<string, string>;
}) {
  const values = data.series.flatMap((series) =>
    series.points.flatMap((point) => (point.value === null ? [] : [Number(point.value) / scale])),
  );
  if (!values.length) return <p role="status">No samples in this range</p>;
  const low = values.reduce((min, value) => Math.min(min, value), 0),
    high = values.reduce((max, value) => Math.max(max, value), 1);
  const clock: Intl.DateTimeFormatOptions =
    Number(data.end) - Number(data.start) > 86400 * 2
      ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
      : { hour: "2-digit", minute: "2-digit" };
  const x = (timestamp: number) =>
    50 + (530 * (timestamp - Number(data.start))) / (Number(data.end) - Number(data.start));
  const y = (value: number) => 160 - (140 * (value / scale - low)) / (high - low);
  return (
    <>
      <svg
        viewBox="0 0 600 200"
        role="img"
        aria-label={`Time series in ${unit}`}
        style={{ width: "100%", maxHeight: 240 }}
      >
        <text x="0" y="18" fill="currentColor" fontSize="11">
          {formatValue(high, unit)}
        </text>
        <text x="0" y="163" fill="currentColor" fontSize="11">
          {formatValue(low, unit)}
        </text>
        <path d="M50 20V160H580" fillOpacity={0} stroke="currentColor" opacity="0.3" />
        {data.series.map((series, index) => {
          let connected = false;
          const path = series.points
            .map((point) => {
              if (point.value === null) {
                connected = false;
                return "";
              }
              const command = connected ? "L" : "M";
              connected = true;
              return `${command}${x(Number(point.timestamp))},${y(Number(point.value))}`;
            })
            .join(" ");
          return (
            <path
              key={JSON.stringify(series.labels)}
              d={path}
              fillOpacity={0}
              stroke={seriesColor(series.labels, names, index)}
              strokeWidth="2"
            >
              <title>{Object.values(series.labels).join(" · ")}</title>
            </path>
          );
        })}
        <text x="50" y="185" fill="currentColor" fontSize="11">
          {new Date(Number(data.start) * 1000).toLocaleString([], clock)}
        </text>
        <text x="580" y="185" textAnchor="end" fill="currentColor" fontSize="11">
          {new Date(Number(data.end) * 1000).toLocaleString([], clock)}
        </text>
      </svg>
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
  const wanted = [
    ...TILES.filter((tile) => !node || !tile.perModel).map((tile) => tile.metric),
    "certificate_expiry" as Metric,
  ];
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
            {(["1h", "6h", "24h", "7d"] as const).map((value) => (
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
      <div className="metric-tiles">
        {TILES.filter((tile) => !node || !tile.perModel).map((tile) => {
          const state = tiles[tile.metric];
          return (
            <section key={tile.metric} className="metric-tile" aria-label={tile.title}>
              <h3>{tile.title}</h3>
              {typeof state === "string" ? (
                <p role="status">Metrics unavailable: {state}</p>
              ) : state ? (
                <SeriesChart data={state} unit={tile.unit} scale={tile.scale} names={names} />
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
