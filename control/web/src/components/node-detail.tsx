import {useEffect, useId, useRef, useState} from "react";
import type {ControlApi, TelemetryHistory, TelemetryHistoryPoint, TelemetryResolution, VisualFleetNode} from "../api/types";
import {formatBytes, installationGroupLabel, nodeWarningsAt, runGroupLabel} from "../lib/fleet";
import {Sparkline, type SparklineSeriesPoint} from "./sparkline";
import {StatusPill} from "./status-pill";

type HistoryRange = "1h" | "6h" | "24h" | "7d" | "30d" | "90d" | "1y";

const HISTORY_RANGES: Record<HistoryRange, {hours: number; label: string; maximumPoints: number; resolution: TelemetryResolution; resolutionLabel: string}> = {
  "1h": {hours: 1, label: "1 hour", maximumPoints: 60, resolution: "minute", resolutionLabel: "minute buckets across the full 1-hour window"},
  "6h": {hours: 6, label: "6 hours", maximumPoints: 360, resolution: "minute", resolutionLabel: "minute buckets across the full 6-hour window"},
  "24h": {hours: 24, label: "24 hours", maximumPoints: 1440, resolution: "minute", resolutionLabel: "minute buckets across the full 24-hour window"},
  "7d": {hours: 24 * 7, label: "7 days", maximumPoints: 672, resolution: "fifteen-minute", resolutionLabel: "15-minute buckets across the full 7-day window"},
  "30d": {hours: 24 * 30, label: "30 days", maximumPoints: 1500, resolution: "fifteen-minute", resolutionLabel: "newest 1,500 15-minute buckets within 30 days"},
  "90d": {hours: 24 * 90, label: "90 days", maximumPoints: 1500, resolution: "fifteen-minute", resolutionLabel: "newest 1,500 15-minute buckets within 90 days"},
  "1y": {hours: 24 * 365, label: "1 year", maximumPoints: 1500, resolution: "fifteen-minute", resolutionLabel: "newest 1,500 15-minute buckets within 1 year"},
};

type RawMetricName = "gpu_utilization_percent" | "memory_available_bytes" | "temperature_c";

function isRollupPoint(point: TelemetryHistoryPoint): point is Extract<TelemetryHistoryPoint, {resolution: string}> {
  return "resolution" in point;
}

function metricSeries(points: readonly TelemetryHistoryPoint[], name: RawMetricName): (SparklineSeriesPoint | null)[] {
  return points.map(point => {
    if (isRollupPoint(point)) {
      const metric = point.metrics[name];
      return metric ? {count: metric.count, minimum: metric.minimum, mean: metric.mean, maximum: metric.maximum} : null;
    }
    const value = point[name];
    return typeof value === "number" && Number.isFinite(value)
      ? {count: 1, minimum: value, mean: value, maximum: value}
      : null;
  });
}

function boundedError(value: unknown): string {
  const message = value instanceof Error ? value.message : "Telemetry history is unavailable";
  return message.length > 512 ? `${message.slice(0, 512)}…` : message;
}

export function NodeDetail({
  api,
  node,
  now,
  onClose,
}: {
  api: ControlApi;
  node: VisualFleetNode;
  now: Date;
  onClose(): void;
}) {
  const headingId = useId();
  const closeButton = useRef<HTMLButtonElement>(null);
  const [range, setRange] = useState<HistoryRange>("1h");
  const [history, setHistory] = useState<TelemetryHistory>();
  const [historyError, setHistoryError] = useState("");
  const [historyLoading, setHistoryLoading] = useState(true);
  const [retryRevision, setRetryRevision] = useState(0);

  useEffect(() => { closeButton.current?.focus(); }, []);

  useEffect(() => {
    const controller = new AbortController();
    const selection = HISTORY_RANGES[range];
    const end = new Date(now);
    const start = new Date(end.getTime() - selection.hours * 60 * 60 * 1000);
    setHistoryLoading(true);
    setHistory(undefined);
    setHistoryError("");
    void api.nodeTelemetryHistory(
      node.id,
      start.toISOString(),
      end.toISOString(),
      selection.resolution,
      selection.maximumPoints,
      controller.signal,
    ).then(result => {
      if (!controller.signal.aborted) setHistory(result);
    }).catch(value => {
      if (!controller.signal.aborted) {
        setHistory(undefined);
        setHistoryError(boundedError(value));
      }
    }).finally(() => {
      if (!controller.signal.aborted) setHistoryLoading(false);
    });
    return () => controller.abort();
  }, [api, node.id, node.telemetry?.sample.id, range, retryRevision]);

  const points = history?.points ?? [];
  const installed = node.installed.filter(item => item.complete);
  const installationStates = node.installed.filter(item => !item.complete);
  const loaded = node.loaded.filter(run => run.healthy);
  const runStates = node.loaded.filter(run => !run.healthy);
  const warnings = nodeWarningsAt(node, now);

  return <aside className="node-detail" role="complementary" aria-labelledby={headingId}>
    <header className="node-detail-heading">
      <div>
        <p className="node-eyebrow">Node detail</p>
        <h3 id={headingId}>{node.display_name} details</h3>
        <p>{node.hostname}</p>
      </div>
      <button ref={closeButton} type="button" className="secondary-button" aria-label={`Close ${node.display_name} details`} onClick={onClose}>Close</button>
    </header>

    <section aria-labelledby={`${headingId}-overview`}>
      <h4 id={`${headingId}-overview`}>Overview</h4>
      <dl className="detail-facts">
        <div><dt>Agent</dt><dd><StatusPill tone={node.connection.online_state === "online" ? "healthy" : "danger"}>{node.connection.online_state}</StatusPill> {node.connection.agent_state}</dd></div>
        <div><dt>Lifecycle</dt><dd>{node.lifecycle}</dd></div>
        <div><dt>Last agent presence</dt><dd>{node.connection.last_seen_at ?? "Not reported"}</dd></div>
        <div><dt>Latest telemetry</dt><dd>{node.telemetry?.sample.observed_at ?? "Not reported"}</dd></div>
        <div><dt>Inventory</dt><dd>{node.inventory?.observed_at ?? "Not reported"}</dd></div>
        <div><dt>Reservations</dt><dd>{formatBytes(node.reservations.unified_memory_bytes)} unified · {formatBytes(node.reservations.disk_bytes)} disk · {node.reservations.port_count} ports</dd></div>
      </dl>
    </section>

    <section aria-labelledby={`${headingId}-recipes`}>
      <h4 id={`${headingId}-recipes`}>Recipes</h4>
      <div className="detail-recipe-columns">
        <section aria-label={`Loaded recipes in ${node.display_name} details`}><h5>Loaded now</h5>{loaded.length === 0 ? <p>Nothing is loaded now</p> : <ul>{loaded.map(run => <li key={`${run.run_id}:${run.rank}`}><strong>{run.title}</strong><small>{run.alias} · {run.role} rank {run.rank}</small><small>Group {run.group_state} · Run {run.run_state} · Rank {run.rank_state} · Route {run.route_state}</small><span>{runGroupLabel(run)}</span></li>)}</ul>}</section>
        <section aria-label={`Installed recipes in ${node.display_name} details`}><h5>Installed</h5>{installed.length === 0 ? <p>No complete installations reported</p> : <ul>{installed.map(item => <li key={`${item.installation_id}:${item.rank}`}><strong>{item.title}</strong><small>{item.profile_name} · {item.role} rank {item.rank}</small><small>Group {item.group_state} · Rank {item.rank_state}</small><span>{installationGroupLabel(item)}</span></li>)}</ul>}</section>
        <section aria-label={`Installation state in ${node.display_name} details`}><h5>Installation state</h5>{installationStates.length === 0 ? <p>No incomplete installation states</p> : <ul>{installationStates.map(item => <li key={`${item.installation_id}:${item.rank}`}><strong>{item.title}</strong><small>{item.profile_name} · {item.role} rank {item.rank}</small><small>Group {item.group_state} · Rank {item.rank_state}</small><span>{installationGroupLabel(item)}</span></li>)}</ul>}</section>
        <section aria-label={`Run state in ${node.display_name} details`}><h5>Run state</h5>{runStates.length === 0 ? <p>No inactive or degraded run states</p> : <ul>{runStates.map(run => <li key={`${run.run_id}:${run.rank}`}><strong>{run.title}</strong><small>{run.alias} · {run.role} rank {run.rank}</small><small>Group {run.group_state} · Run {run.run_state} · Rank {run.rank_state} · Route {run.route_state}</small><span>{runGroupLabel(run)}</span></li>)}</ul>}</section>
      </div>
    </section>

    <section aria-labelledby={`${headingId}-performance`}>
      <div className="performance-heading">
        <h4 id={`${headingId}-performance`}>Performance</h4>
        <div className="history-ranges" role="group" aria-label="Telemetry history range">
          {(Object.entries(HISTORY_RANGES) as [HistoryRange, typeof HISTORY_RANGES[HistoryRange]][]).map(([value, option]) => <button key={value} type="button" aria-pressed={range === value} onClick={() => setRange(value)}>{option.label}</button>)}
        </div>
      </div>
      <p className="history-resolution" role="status">Showing {HISTORY_RANGES[range].resolutionLabel} for the selected window. Ranges are reported values; no samples are filled in.</p>
      {warnings.some(warning => warning.code === "telemetry.delayed" || warning.code === "telemetry.stale") && <p className="history-stale" role="status">Telemetry delivery is delayed; this history may be behind the live node.</p>}
      {historyLoading && <p role="status">Loading bounded telemetry history…</p>}
      {historyError && <div className="history-error"><p role="alert">{historyError}</p><button type="button" onClick={() => setRetryRevision(value => value + 1)}>Retry history</button></div>}
      {history && points.length === 0 && <p role="status">No telemetry samples are available in this window.</p>}
      {history && points.length > 0 && <div className="history-grid">
        <Sparkline label={`${node.display_name} GPU utilization history`} values={[]} series={metricSeries(points, "gpu_utilization_percent")} sampleLabel={history.resolution === "raw" ? "samples" : "buckets"} formatValue={value => `${Math.round(value)}%`}/>
        <Sparkline label={`${node.display_name} available memory history`} values={[]} series={metricSeries(points, "memory_available_bytes")} sampleLabel={history.resolution === "raw" ? "samples" : "buckets"} formatValue={formatBytes}/>
        <Sparkline label={`${node.display_name} temperature history`} values={[]} series={metricSeries(points, "temperature_c")} sampleLabel={history.resolution === "raw" ? "samples" : "buckets"} formatValue={value => `${Number(value.toFixed(1))} °C`}/>
      </div>}
    </section>

    <section aria-labelledby={`${headingId}-events`}>
      <h4 id={`${headingId}-events`}>Events</h4>
      {warnings.length === 0 ? <p>No active Fleet warnings.</p> : <ul>{warnings.map((warning, index) => <li key={`${warning.code}:${index}`}><strong>{warning.severity}</strong> {warning.detail}</li>)}</ul>}
    </section>

    <details className="technical-details">
      <summary>Technical details</summary>
      <dl className="detail-facts">
        <div><dt>Node ID</dt><dd><code>{node.id}</code></dd></div>
        <div><dt>Certificate</dt><dd>{node.connection.certificate_state}</dd></div>
        <div><dt>Telemetry sample</dt><dd><code>{node.telemetry?.sample.id ?? "Not reported"}</code></dd></div>
        <div><dt>Boot ID</dt><dd><code>{node.telemetry?.sample.boot_id ?? "Not reported"}</code></dd></div>
        <div><dt>Inventory runtime</dt><dd>{node.inventory?.container_runtime_version ?? "Not reported"}</dd></div>
        <div><dt>NVIDIA driver</dt><dd>{node.inventory?.nvidia_driver_version ?? "Not reported"}</dd></div>
      </dl>
    </details>
  </aside>;
}
