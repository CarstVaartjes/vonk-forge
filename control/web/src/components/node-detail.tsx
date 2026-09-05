import {useCallback, useEffect, useId, useRef, useState} from "react";
import type {MouseEvent as ReactMouseEvent} from "react";
import type {ControlApi, LibraryOperation, TelemetryCapabilitiesResponse, TelemetryCurrentResponse, TelemetryHistory, TelemetryHistoryPoint, TelemetryResolution, TelemetryRuntime, TelemetrySeries, TelemetryWorkload, TelemetryWorkloadsResponse, VisualFleetNode} from "../api/types";
import {formatBytes, installationGroupLabel, nodeDisplayName, nodeSecondaryName, nodeUnifiedMemory, nodeWarningsAt, runGroupLabel, timestampPresentation} from "../lib/fleet";
import {CopyButton} from "./copy-button";
import {LibraryActionDialog} from "./library-action-dialog";
import type {LibraryActionName, LibraryActionTarget} from "./library-action-types";
import {LibraryNodeNamesProvider} from "./library-node-names";
import {LibraryOperationProgress, operationSettled} from "./library-operation-progress";
import {Meter} from "./meter";
import {Sparkline, type SparklineDomain, type SparklineSeriesPoint} from "./sparkline";
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

const LIFECYCLE_PREVIEW_POLICY = {
  inventory_fresh_seconds: 300,
  telemetry_delayed_seconds: 20,
  telemetry_live_seconds: 6,
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

function availableMemoryDomain(points: readonly TelemetryHistoryPoint[], node: VisualFleetNode): SparklineDomain | undefined {
  const totals = points.flatMap(point => {
    if (isRollupPoint(point)) {
      const metric = point.metrics.memory_total_bytes;
      return metric && Number.isFinite(metric.maximum) && metric.maximum > 0 ? [metric.maximum] : [];
    }
    return typeof point.memory_total_bytes === "number"
      && Number.isFinite(point.memory_total_bytes)
      && point.memory_total_bytes > 0
      ? [point.memory_total_bytes]
      : [];
  });
  const currentTotal = node.telemetry?.sample.memory_total_bytes ?? node.inventory?.host_memory_total_bytes;
  if (typeof currentTotal === "number" && Number.isFinite(currentTotal) && currentTotal > 0) totals.push(currentTotal);
  return totals.length > 0 ? [0, Math.max(...totals)] : undefined;
}

function boundedError(value: unknown): string {
  const message = value instanceof Error ? value.message : "Telemetry history is unavailable";
  return message.length > 512 ? `${message.slice(0, 512)}…` : message;
}

function humanMetricKey(key: string): string {
  return key.replaceAll("_", " ").replaceAll(".", " · ").replace(/\b\w/g, value => value.toUpperCase());
}

function metricContext(series: TelemetrySeries): string {
  return [series.device_id, series.interface_name, series.process_name, series.run_id].filter(Boolean).join(" · ") || "Node aggregate";
}

function metricValue(series: TelemetrySeries): string {
  if (series.value === null) return "Unavailable";
  if (typeof series.value === "number") return Number.isFinite(series.value) ? series.value.toLocaleString(undefined, {maximumFractionDigits: 3}) : "Unavailable";
  return String(series.value);
}

function freshnessLabel(value: TelemetrySeries["freshness"] | TelemetryCurrentResponse["freshness"]): string {
  if (value === "fresh") return "Fresh";
  if (value === "live") return "Live";
  return value === "delayed" ? "Delayed" : "Stale";
}

function runtimePlacement(runtime: TelemetryRuntime): string {
  const nodes = runtime.serving_node_ids.length > 0 ? runtime.serving_node_ids.join(", ") : "Placement unavailable";
  return runtime.ranks.length > 0 ? `${nodes} · ranks ${runtime.ranks.join(", ")}` : nodes;
}

function workloadIdentity(workload: TelemetryWorkload): string {
  return workload.title || workload.job_id || workload.request_id || workload.run_id;
}

function RelativeTimestamp({label, now, value}: {label: string; now: Date; value: string | null | undefined}) {
  const timestamp = timestampPresentation(value, now, label);
  return timestamp
    ? <time dateTime={timestamp.dateTime} title={timestamp.exact} aria-label={`${timestamp.relative}; exact time ${timestamp.exact}`}>{timestamp.relative}</time>
    : <>Not reported</>;
}

type DetailTab = "overview" | "metrics" | "workloads" | "services" | "events";

type RichTelemetryPanelProps = {
  tab: Exclude<DetailTab, "overview">;
  current?: TelemetryCurrentResponse;
  capabilities?: TelemetryCapabilitiesResponse;
  workloads?: TelemetryWorkloadsResponse;
  loading: boolean;
  error: string;
  paused: boolean;
  onRetry(): void;
  onPause(): void;
  onExport(): void;
};

function RichTelemetryPanel({tab, current, capabilities, workloads, loading, error, paused, onRetry, onPause, onExport}: RichTelemetryPanelProps) {
  const metrics = current?.sample.metrics;
  const series = metrics?.series ?? [];
  const capabilityRows = (capabilities?.capabilities ?? metrics?.capabilities ?? [])
    .filter(capability => !series.some(item => item.key === capability.key && item.scope === capability.scope && item.device_id === capability.device_id && item.interface_name === capability.interface_name && item.run_id === capability.run_id));
  const metricRows = [...series, ...capabilityRows.map(capability => ({
    key: capability.key,
    scope: capability.scope,
    device_id: capability.device_id,
    process_id: null,
    process_name: null,
    interface_name: capability.interface_name,
    run_id: capability.run_id,
    value: capability.supported ? null : null,
    unit: capability.unit,
    source: capability.source,
    measurement_kind: capability.measurement_kind,
    observed_at: current?.observed_at ?? capabilities?.observed_at ?? "",
    received_at: current?.received_at ?? capabilities?.received_at ?? null,
    freshness: current?.freshness === "live" ? "fresh" as const : current?.freshness ?? "stale" as const,
    freshness_threshold_seconds: capability.freshness_threshold_seconds,
    support_status: capability.supported ? "unavailable" as const : "unsupported" as const,
    reason: capability.reason ?? (capability.supported ? "No sample was reported." : "This metric is not supported by the collector."),
    aggregation: "current",
  } satisfies TelemetrySeries))];

  if (loading) return <section className="node-detail-deep-surface" aria-live="polite"><p role="status">Loading Spark telemetry…</p></section>;
  if (error) return <section className="node-detail-deep-surface node-detail-deep-error" aria-live="polite"><p role="alert">{error}</p><button type="button" className="button secondary" onClick={onRetry}>Retry telemetry</button></section>;

  if (tab === "metrics") return <section className="node-detail-deep-surface" aria-labelledby="rich-metrics-heading">
    <header className="node-detail-deep-heading"><div><h4 id="rich-metrics-heading">Metrics</h4><p>Controller-projected values keep their unit, source, scope and freshness. Missing sensors stay visible as unavailable.</p></div><div className="node-detail-deep-actions"><button type="button" className="button secondary" onClick={onPause}>{paused ? "Resume refresh" : "Pause refresh"}</button><button type="button" className="button secondary" disabled={!current && !capabilities} onClick={onExport}>Export JSON</button></div></header>
    <div className="node-detail-telemetry-meta"><span>Sample: {current ? freshnessLabel(current.freshness) : "Unavailable"}</span>{current && <time dateTime={current.observed_at}>Observed {new Date(current.observed_at).toLocaleString()}</time>}{metrics?.provenance && <span>Source: {metrics.provenance.collector} {metrics.provenance.collector_version}</span>}{metrics?.provenance?.host_uptime_seconds !== null && metrics?.provenance?.host_uptime_seconds !== undefined && <span>Host uptime: {Math.round(metrics.provenance.host_uptime_seconds / 3600)} h</span>}</div>
    {metricRows.length === 0 ? <p role="status">No rich metric series or capability declarations are available for this Spark.</p> : <div className="node-detail-metric-table-wrap"><table className="node-detail-metric-table"><caption className="visually-hidden">Rich Spark metrics with source and freshness evidence</caption><thead><tr><th scope="col">Metric</th><th scope="col">Scope</th><th scope="col">Value</th><th scope="col">Evidence</th></tr></thead><tbody>{metricRows.map((item, index) => <tr key={`${item.key}:${item.scope}:${item.device_id ?? ""}:${item.interface_name ?? ""}:${item.run_id ?? ""}:${index}`}><th scope="row"><span>{humanMetricKey(item.key)}</span><small>{metricContext(item)}</small></th><td>{item.scope}</td><td><strong className={`telemetry-value status-${item.support_status}`}>{item.support_status === "available" ? metricValue(item) : item.support_status === "unsupported" ? "Unsupported" : "Unavailable"}</strong><small>{item.unit}</small></td><td><span className={`telemetry-freshness freshness-${item.freshness}`}>{freshnessLabel(item.freshness)}</span><small>{item.source} · {item.measurement_kind} · {item.aggregation}</small>{item.reason && <small>{item.reason}</small>}</td></tr>)}</tbody></table></div>}
  </section>;

  if (tab === "workloads") return <section className="node-detail-deep-surface" aria-labelledby="rich-workloads-heading">
    <header className="node-detail-deep-heading"><div><h4 id="rich-workloads-heading">Workloads</h4><p>Runtime identity and request placement come from the Controller telemetry projection.</p></div><button type="button" className="button secondary" onClick={onRetry}>Refresh workloads</button></header>
    {workloads?.runtimes.length ? <div className="node-detail-runtime-grid">{workloads.runtimes.map(runtime => <article key={`${runtime.run_id}:${runtime.engine_id}`}><header><strong>{runtime.model || runtime.run_id}</strong><span className={`telemetry-freshness readiness-${runtime.readiness}`}>{runtime.readiness}</span></header><dl><div><dt>Engine</dt><dd>{runtime.engine_id} · {runtime.backend}{runtime.version ? ` · ${runtime.version}` : ""}</dd></div><div><dt>Placement</dt><dd>{runtimePlacement(runtime)}</dd></div><div><dt>Endpoint</dt><dd>{runtime.endpoint || "Not reported"}</dd></div><div><dt>Recipe</dt><dd>{runtime.recipe_revision || "Not reported"}</dd></div><div><dt>Adapter</dt><dd>{runtime.adapter}{runtime.adapter_version ? ` · ${runtime.adapter_version}` : ""} · {runtime.adapter_supported ? "supported" : `unsupported: ${runtime.adapter_reason || "reason unavailable"}`}</dd></div></dl>{runtime.error && <p className="is-error">{runtime.error}</p>}</article>)}</div> : <p role="status">No runtime placement is reported for this Spark.</p>}
    {workloads?.workloads.length ? <div className="node-detail-workload-table-wrap"><table className="node-detail-workload-table"><caption className="visually-hidden">Reported request and job workloads</caption><thead><tr><th scope="col">Workload</th><th scope="col">State</th><th scope="col">Placement</th><th scope="col">Timing</th></tr></thead><tbody>{workloads.workloads.map((workload, index) => <tr key={`${workload.run_id}:${workload.request_id ?? workload.job_id ?? index}`}><th scope="row"><span>{workloadIdentity(workload)}</span><small>{workload.model || workload.recipe_revision || workload.run_id}</small></th><td><span className={`telemetry-freshness workload-${workload.state}`}>{workload.state}</span>{workload.failure && <small className="is-error">{workload.failure}</small>}</td><td>{workload.executor_node_ids.length > 0 ? workload.executor_node_ids.join(", ") : "Not reported"}</td><td>{workload.elapsed_seconds !== null && workload.elapsed_seconds !== undefined ? `${workload.elapsed_seconds.toFixed(1)} s elapsed` : workload.started_at ? `Started ${new Date(workload.started_at).toLocaleString()}` : "Not reported"}</td></tr>)}</tbody></table></div> : <p role="status">No request or job workloads are reported for this Spark.</p>}
  </section>;

  if (tab === "services") return <section className="node-detail-deep-surface" aria-labelledby="rich-services-heading">
    <header className="node-detail-deep-heading"><div><h4 id="rich-services-heading">Services</h4><p>Runtime adapters and endpoint ownership are shown with their reported support state.</p></div><button type="button" className="button secondary" onClick={onRetry}>Refresh services</button></header>
    {workloads?.runtimes.length ? <div className="node-detail-services-list">{workloads.runtimes.map(runtime => <article key={`${runtime.run_id}:${runtime.adapter}`}><div><strong>{runtime.adapter}</strong><span>{runtime.adapter_version || "Version not reported"}</span></div><dl><div><dt>Run</dt><dd>{runtime.run_id}</dd></div><div><dt>Backend</dt><dd>{runtime.backend}</dd></div><div><dt>Readiness</dt><dd>{runtime.readiness}</dd></div><div><dt>Support</dt><dd>{runtime.adapter_supported ? "Supported" : `Unsupported · ${runtime.adapter_reason || "reason unavailable"}`}</dd></div></dl></article>)}</div> : <p role="status">No runtime services are reported for this Spark.</p>}
  </section>;

  return <section className="node-detail-deep-surface" aria-labelledby="rich-events-heading"><header className="node-detail-deep-heading"><div><h4 id="rich-events-heading">Events</h4><p>Telemetry delivery and capability exceptions reported by this Spark.</p></div><button type="button" className="button secondary" onClick={onRetry}>Refresh events</button></header><div className="node-detail-event-summary"><span>Telemetry: {current ? freshnessLabel(current.freshness) : capabilities ? freshnessLabel(capabilities.freshness) : "Unavailable"}</span><span>{capabilities?.capabilities.filter(item => !item.supported).length ?? 0} unsupported capability declarations</span><span>{current?.sample.gap_samples ?? "No gap count reported"} gap samples in latest sample</span></div></section>;
}

export function NodeDetail({
  api,
  node,
  now,
  onBusyChange,
  onClose,
  onLifecycleRefresh,
  onReenroll,
  onUpgrade,
}: {
  api: ControlApi;
  node: VisualFleetNode;
  now: Date;
  onBusyChange?(busy: boolean): void;
  onClose(): void;
  onLifecycleRefresh?(signal: AbortSignal): Promise<void>;
  onReenroll?(event: ReactMouseEvent<HTMLButtonElement>): void;
  onUpgrade?(event: ReactMouseEvent<HTMLButtonElement>): void;
}) {
  const headingId = useId();
  const closeButton = useRef<HTMLButtonElement>(null);
  const [detailTab, setDetailTab] = useState<DetailTab>("overview");
  const [range, setRange] = useState<HistoryRange>("1h");
  const [history, setHistory] = useState<TelemetryHistory>();
  const [historyError, setHistoryError] = useState("");
  const [historyLoading, setHistoryLoading] = useState(true);
  const [retryRevision, setRetryRevision] = useState(0);
  const [reviewTarget, setReviewTarget] = useState<LibraryActionTarget>();
  const [operation, setOperation] = useState<LibraryOperation>();
  const [operationName, setOperationName] = useState<LibraryActionName>("Stop");
  const [richCurrent, setRichCurrent] = useState<TelemetryCurrentResponse>();
  const [richCapabilities, setRichCapabilities] = useState<TelemetryCapabilitiesResponse>();
  const [richWorkloads, setRichWorkloads] = useState<TelemetryWorkloadsResponse>();
  const [richLoading, setRichLoading] = useState(false);
  const [richError, setRichError] = useState("");
  const [richRetry, setRichRetry] = useState(0);
  const [telemetryPaused, setTelemetryPaused] = useState(false);
  const lifecycleTrigger = useRef<HTMLButtonElement | null>(null);

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

  useEffect(() => {
    if (detailTab === "overview") return;
    const controller = new AbortController();
    setRichLoading(true);
    setRichError("");
    const currentRequest = detailTab === "metrics" || detailTab === "events"
      ? api.nodeTelemetryCurrent(node.id, controller.signal)
      : Promise.resolve(undefined);
    const capabilitiesRequest = detailTab === "metrics" || detailTab === "events"
      ? api.nodeTelemetryCapabilities(node.id, controller.signal)
      : Promise.resolve(undefined);
    const workloadsRequest = detailTab === "workloads" || detailTab === "services"
      ? api.nodeTelemetryWorkloads(node.id, undefined, undefined, controller.signal)
      : Promise.resolve(undefined);
    void Promise.all([currentRequest, capabilitiesRequest, workloadsRequest]).then(([current, capabilities, workloads]) => {
      if (controller.signal.aborted) return;
      if (current) setRichCurrent(current);
      if (capabilities) setRichCapabilities(capabilities);
      if (workloads) setRichWorkloads(workloads);
    }).catch(value => {
      if (!controller.signal.aborted) setRichError(boundedError(value));
    }).finally(() => {
      if (!controller.signal.aborted) setRichLoading(false);
    });
    const refreshTimer = detailTab === "metrics" && !telemetryPaused
      ? window.setInterval(() => setRichRetry(value => value + 1), 15_000)
      : undefined;
    return () => {
      controller.abort();
      if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
    };
  }, [api, detailTab, node.id, richRetry, telemetryPaused]);

  const points = history?.points ?? [];
  const installed = node.installed.filter(item => item.complete);
  const installationStates = node.installed.filter(item => !item.complete);
  const loaded = node.loaded.filter(run => run.healthy);
  const runStates = node.loaded.filter(run => !run.healthy);
  const warnings = nodeWarningsAt(node, now);
  const name = nodeDisplayName(node);
  const secondaryName = nodeSecondaryName(node);
  const memory = nodeUnifiedMemory(node);
  const memoryHistoryDomain = availableMemoryDomain(points, node);
  const lifecycleBlocked = operation !== undefined && (!operationSettled(operation.state) || ["partial", "failed", "cancelled", "canceled", "lost"].includes(operation.state));
  const refreshLifecycle = useCallback(async (signal: AbortSignal) => {
    try {
      await onLifecycleRefresh?.(signal);
    } catch {
      // The durable operation remains authoritative; the Fleet stream also reconciles it.
    }
  }, [onLifecycleRefresh]);
  const openLifecycleReview = useCallback((target: LibraryActionTarget, trigger: HTMLButtonElement) => {
    lifecycleTrigger.current = trigger;
    setReviewTarget(target);
  }, []);
  const closeLifecycleReview = useCallback(() => {
    setReviewTarget(undefined);
    const returnTo = lifecycleTrigger.current;
    queueMicrotask(() => returnTo?.focus());
  }, []);
  const onLifecycleApplied = useCallback((next: LibraryOperation, action: LibraryActionName) => {
    setOperationName(action);
    setOperation(next);
  }, []);
  const blockingRuns = (installationId: string) => node.loaded.filter(run => run.installation_id === installationId && run.run_state !== "stopped");

  function exportTelemetry() {
    const payload = {node_id: node.id, current: richCurrent ?? null, capabilities: richCapabilities?.capabilities ?? [], workloads: richWorkloads ?? null};
    const blob = new Blob([JSON.stringify(payload, null, 2)], {type: "application/json"});
    const urlFactory = URL as typeof URL & {createObjectURL?: (value: Blob) => string; revokeObjectURL?: (value: string) => void};
    if (!urlFactory.createObjectURL) {
      setRichError("Telemetry export is unavailable in this browser.");
      return;
    }
    const link = document.createElement("a");
    const objectUrl = urlFactory.createObjectURL(blob);
    link.href = objectUrl;
    link.download = `${node.id}-telemetry.json`;
    link.click();
    urlFactory.revokeObjectURL?.(objectUrl);
  }

  function stopButton(run: VisualFleetNode["loaded"][number]) {
    if (run.run_state === "stopped") return null;
    const destination = run.member_node_ids.length === 1 ? "this Spark" : `${run.member_node_ids.length} Sparks`;
    return <button type="button" className="button secondary node-lifecycle-button" aria-label={`Stop ${run.title} on ${destination}`} disabled={lifecycleBlocked} onClick={event => openLifecycleReview({kind: "stop", runId: run.run_id}, event.currentTarget)}>Stop on {destination}</button>;
  }

  function removeControl(installation: VisualFleetNode["installed"][number]) {
    const runs = blockingRuns(installation.installation_id);
    if (runs.length > 0) return <small className="node-lifecycle-note">Stop {runs.length === 1 ? "the active run" : `all ${runs.length} active runs`} before removing this recipe from {installation.member_node_ids.length === 1 ? "this Spark" : `all ${installation.member_node_ids.length} Sparks`}.</small>;
    const destination = installation.member_node_ids.length === 1 ? "this Spark" : `${installation.member_node_ids.length} Sparks`;
    return <button type="button" className="button secondary node-lifecycle-button" aria-label={`Remove ${installation.title} from ${destination}`} disabled={lifecycleBlocked} onClick={event => openLifecycleReview({kind: "uninstall", installationId: installation.installation_id}, event.currentTarget)}>Remove from {destination}</button>;
  }

  return <LibraryNodeNamesProvider names={{[node.id]: name}}><aside className="node-detail" role="complementary" aria-labelledby={headingId}>
    <header className="node-detail-heading">
      <div>
        <h3 id={headingId}>{name} details</h3>
        {secondaryName && <p>{secondaryName}</p>}
      </div>
      <div>{onUpgrade && <><button type="button" className="secondary-button" onClick={onUpgrade}>Upgrade agent</button>{" "}</>}{onReenroll && <><button type="button" className="secondary-button" onClick={onReenroll}>Re-enroll</button>{" "}</>}<button ref={closeButton} type="button" className="secondary-button" aria-label={`Close ${name} details`} onClick={onClose}>Close</button></div>
    </header>

    <nav className="node-detail-tabs" aria-label={`${name} detail sections`} role="tablist">
      {(["overview", "metrics", "workloads", "services", "events"] as DetailTab[]).map(tab => <button key={tab} type="button" role="tab" aria-selected={detailTab === tab} aria-controls={detailTab === tab ? `${headingId}-${tab}-panel` : undefined} id={`${headingId}-${tab}-tab`} className={detailTab === tab ? "is-active" : undefined} onClick={() => setDetailTab(tab)}>{tab[0].toUpperCase() + tab.slice(1)}</button>)}
    </nav>
    {detailTab !== "overview" && <div id={`${headingId}-${detailTab}-panel`} role="tabpanel" aria-labelledby={`${headingId}-${detailTab}-tab`}><RichTelemetryPanel tab={detailTab} current={richCurrent} capabilities={richCapabilities} workloads={richWorkloads} loading={richLoading} error={richError} paused={telemetryPaused} onRetry={() => setRichRetry(value => value + 1)} onPause={() => setTelemetryPaused(value => !value)} onExport={exportTelemetry}/></div>}

    <section id={`${headingId}-overview-panel`} role="tabpanel" aria-labelledby={`${headingId}-overview-tab`}>
      <h4 id={`${headingId}-overview-heading`}>Overview</h4>
      <dl className="detail-facts">
        <div><dt>Agent</dt><dd><StatusPill tone={node.connection.online_state === "online" ? "healthy" : "danger"}>{node.connection.online_state}</StatusPill> {node.connection.agent_state}</dd></div>
        <div><dt>Lifecycle</dt><dd>{node.lifecycle}</dd></div>
        <div><dt>Last agent presence</dt><dd><RelativeTimestamp label="Seen" now={now} value={node.connection.last_seen_at}/></dd></div>
        <div><dt>Latest telemetry</dt><dd><RelativeTimestamp label="Updated" now={now} value={node.telemetry?.sample.observed_at}/></dd></div>
        <div><dt>Inventory</dt><dd><RelativeTimestamp label="Observed" now={now} value={node.inventory?.observed_at}/></dd></div>
        <div><dt>Reservations</dt><dd>{formatBytes(node.reservations.unified_memory_bytes)} unified · {formatBytes(node.reservations.disk_bytes)} disk · {node.reservations.port_count} ports</dd></div>
      </dl>
      {memory && <div className="detail-memory-meter"><Meter label="Unified memory in use" max={memory.total} value={memory.used} tone={memory.utilizationPercent >= 90 ? "danger" : memory.utilizationPercent >= 75 ? "warning" : "healthy"} valueLabel={`${formatBytes(memory.available)} available of ${formatBytes(memory.total)}`}/></div>}
    </section>

    <section aria-labelledby={`${headingId}-recipes`}>
      <div className="node-recipe-command"><div><h4 id={`${headingId}-recipes`}>Models and recipes</h4><p>Stop or remove what is here, or choose a model and exact recipe to download to the Controller.</p></div><a className="button" href={`/library?spark=${encodeURIComponent(node.id)}`}>Download to Library</a></div>
      {operation && <LibraryOperationProgress api={api} name={operationName} onChange={setOperation} onRefresh={refreshLifecycle} operation={operation}/>}
      <div className="detail-recipe-columns">
        <section aria-label={`Loaded recipes in ${name} details`}><h5>Loaded now</h5>{loaded.length === 0 ? <p>Nothing is loaded now</p> : <ul>{loaded.map(run => <li key={`${run.run_id}:${run.rank}`}><strong>{run.title}</strong><small>{run.alias} · {run.role} rank {run.rank}</small><small>Group {run.group_state} · Run {run.run_state} · Rank {run.rank_state} · Route {run.route_state}</small><span>{runGroupLabel(run)}</span>{stopButton(run)}</li>)}</ul>}</section>
        <section aria-label={`Installed recipes in ${name} details`}><h5>Installed</h5>{installed.length === 0 ? <p>No complete installations reported</p> : <ul>{installed.map(item => <li key={`${item.installation_id}:${item.rank}`}><strong>{item.title}</strong><small>{item.topology_name} · {item.role} rank {item.rank}</small><small>Group {item.group_state} · Rank {item.rank_state}</small><span>{installationGroupLabel(item)}</span>{removeControl(item)}</li>)}</ul>}</section>
        <section aria-label={`Installation state in ${name} details`}><h5>Installation state</h5>{installationStates.length === 0 ? <p>No incomplete installation states</p> : <ul>{installationStates.map(item => <li key={`${item.installation_id}:${item.rank}`}><strong>{item.title}</strong><small>{item.topology_name} · {item.role} rank {item.rank}</small><small>Group {item.group_state} · Rank {item.rank_state}</small><span>{installationGroupLabel(item)}</span>{item.group_state !== "uninstalled" && removeControl(item)}</li>)}</ul>}</section>
        <section aria-label={`Run state in ${name} details`}><h5>Run state</h5>{runStates.length === 0 ? <p>No inactive or degraded run states</p> : <ul>{runStates.map(run => <li key={`${run.run_id}:${run.rank}`}><strong>{run.title}</strong><small>{run.alias} · {run.role} rank {run.rank}</small><small>Group {run.group_state} · Run {run.run_state} · Rank {run.rank_state} · Route {run.route_state}</small><span>{runGroupLabel(run)}</span>{stopButton(run)}</li>)}</ul>}</section>
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
        <Sparkline metricName="GPU utilization" label={`${name} GPU utilization history`} domain={[0, 100]} values={[]} series={metricSeries(points, "gpu_utilization_percent")} sampleLabel={history.resolution === "raw" ? "samples" : "buckets"} formatValue={value => `${Math.round(value)}%`}/>
        <Sparkline metricName="Available memory" label={`${name} available memory history`} domain={memoryHistoryDomain} values={[]} series={metricSeries(points, "memory_available_bytes")} sampleLabel={history.resolution === "raw" ? "samples" : "buckets"} formatValue={formatBytes}/>
        <Sparkline metricName="Temperature" label={`${name} temperature history`} domain={[0, 100]} values={[]} series={metricSeries(points, "temperature_c")} sampleLabel={history.resolution === "raw" ? "samples" : "buckets"} formatValue={value => `${Number(value.toFixed(1))} °C`}/>
      </div>}
    </section>

    <section aria-labelledby={`${headingId}-events`}>
      <h4 id={`${headingId}-events`}>Events</h4>
      {warnings.length === 0 ? <p>No active Fleet warnings.</p> : <ul>{warnings.map((warning, index) => <li key={`${warning.code}:${index}`}><strong>{warning.severity}</strong> {warning.detail}</li>)}</ul>}
    </section>

    <details className="technical-details">
      <summary>Technical details</summary>
      <dl className="detail-facts">
        <div><dt>Node ID</dt><dd><div className="technical-identifier"><code>{node.id}</code><CopyButton label="node ID" value={node.id}/></div></dd></div>
        <div><dt>Hostname</dt><dd><code>{node.hostname || "Not reported"}</code></dd></div>
        <div><dt>Management IP</dt><dd><code>{node.ip_address || "Not reported"}</code></dd></div>
        <div><dt>Certificate</dt><dd>{node.connection.certificate_state}</dd></div>
        <div><dt>Telemetry sample</dt><dd><code>{node.telemetry?.sample.id ?? "Not reported"}</code></dd></div>
        <div><dt>Boot ID</dt><dd><code>{node.telemetry?.sample.boot_id ?? "Not reported"}</code></dd></div>
        <div><dt>Inventory runtime</dt><dd>{node.inventory?.container_runtime_version ?? "Not reported"}</dd></div>
        <div><dt>NVIDIA driver</dt><dd>{node.inventory?.nvidia_driver_version ?? "Not reported"}</dd></div>
        <div><dt>Reported host memory</dt><dd>{formatBytes(node.telemetry?.sample.memory_available_bytes ?? node.inventory?.host_memory_free_bytes)} available / {formatBytes(node.telemetry?.sample.memory_total_bytes ?? node.inventory?.host_memory_total_bytes)}</dd></div>
        <div><dt>Reported GPU memory</dt><dd>{formatBytes(node.telemetry?.sample.gpu_memory_free_bytes ?? node.inventory?.gpu_memory_free_bytes)} available / {formatBytes(node.telemetry?.sample.gpu_memory_total_bytes ?? node.inventory?.gpu_memory_total_bytes)}</dd></div>
      </dl>
    </details>
    {reviewTarget && <LibraryActionDialog
      alias={node.loaded.find(run => run.run_id === ("runId" in reviewTarget ? reviewTarget.runId : ""))?.alias ?? "model"}
      api={api}
      onApplied={onLifecycleApplied}
      onBusyChange={onBusyChange}
      onClose={closeLifecycleReview}
      onRefresh={refreshLifecycle}
      policy={LIFECYCLE_PREVIEW_POLICY}
      target={reviewTarget}
    />}
  </aside></LibraryNodeNamesProvider>;
}
