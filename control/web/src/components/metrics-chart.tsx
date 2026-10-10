import { useEffect, useId, useState } from "react";
import type { ControlApi, Metric, MetricRange, MetricsSeriesResponse } from "../api/types";
import { safeErrorText } from "../lib/error-display";

const METRICS: Array<[Metric, string, string]> = [
  ["gpu_utilization", "GPU utilisation", "%"],
  ["gpu_memory_used", "GPU memory used", "bytes"],
  ["host_memory_used", "Host memory used", "bytes"],
  ["gpu_temperature", "GPU temperature", "°C"],
  ["request_rate", "Requests per model", "requests/s"],
  ["certificate_expiry", "Certificate time to expiry", "seconds"],
];
const COLORS = ["var(--mint)", "var(--text)", "var(--warning)"];

export function SeriesChart({ data, unit }: { data: MetricsSeriesResponse; unit: string }) {
  const values = data.series.flatMap((series) =>
    series.points.flatMap((point) => (point.value === null ? [] : [Number(point.value)])),
  );
  if (!values.length) return <p role="status">No samples reported in this range.</p>;
  const low = values.reduce((min, value) => Math.min(min, value), 0),
    high = values.reduce((max, value) => Math.max(max, value), 1);
  const x = (timestamp: number) =>
    50 + (530 * (timestamp - Number(data.start))) / (Number(data.end) - Number(data.start));
  const y = (value: number) => 160 - (140 * (value - low)) / (high - low);
  return (
    <>
      <svg
        viewBox="0 0 600 200"
        role="img"
        aria-label={`Time series in ${unit}`}
        style={{ width: "100%", maxHeight: 240 }}
      >
        <text x="0" y="18" fill="currentColor" fontSize="11">
          {high.toPrecision(3)}
        </text>
        <text x="0" y="163" fill="currentColor" fontSize="11">
          {low.toPrecision(3)}
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
              stroke={COLORS[index % COLORS.length]}
              strokeWidth="2"
            >
              <title>{Object.values(series.labels).join(" · ")}</title>
            </path>
          );
        })}
        <text x="50" y="185" fill="currentColor" fontSize="11">
          {new Date(Number(data.start) * 1000).toLocaleString()}
        </text>
        <text x="580" y="185" textAnchor="end" fill="currentColor" fontSize="11">
          {new Date(Number(data.end) * 1000).toLocaleString()}
        </text>
      </svg>
      <ul aria-label="Series legend">
        {data.series.map((series, index) => (
          <li key={JSON.stringify(series.labels)} style={{ color: COLORS[index % COLORS.length] }}>
            {Object.entries(series.labels)
              .filter(([key]) => !["__name__", "job", "instance"].includes(key))
              .map(([key, value]) => `${key}: ${value}`)
              .join(" · ") || "Fleet"}{" "}
            · {unit}
          </li>
        ))}
      </ul>
    </>
  );
}

export function MetricsChart({ api, node }: { api: ControlApi; node?: string }) {
  const id = useId();
  const [metric, setMetric] = useState<Metric>("gpu_utilization");
  const [range, setRange] = useState<MetricRange>("6h");
  const [data, setData] = useState<MetricsSeriesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError(null);
    api.metricsSeries(metric, range, node, controller.signal).then(
      (result) => {
        if (!controller.signal.aborted) setData(result);
      },
      (cause) => {
        if (!controller.signal.aborted)
          setError(safeErrorText(cause instanceof Error ? cause.message : "Metrics unavailable"));
      },
    );
    return () => controller.abort();
  }, [api, metric, range, node, retry]);
  return (
    <section aria-labelledby={id} className="fleet-metrics">
      <h2 id={id}>Metrics</h2>
      <div className="button-row">
        <label>
          Metric{" "}
          <select
            value={metric}
            onChange={(event) => setMetric(event.currentTarget.value as Metric)}
          >
            {METRICS.filter(([key]) => !node || key !== "request_rate").map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
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
      {error ? (
        <p role="status">Metrics unavailable: {error}</p>
      ) : data ? (
        <SeriesChart data={data} unit={METRICS.find(([key]) => key === metric)?.[2] ?? ""} />
      ) : (
        <p role="status">Reading metrics…</p>
      )}
    </section>
  );
}
