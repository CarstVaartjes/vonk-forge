import {useCallback, useEffect, useId, useRef, useState} from "react";
import type {MouseEvent as ReactMouseEvent} from "react";
import type {ControlApi, LibraryOperation, TelemetryHistory, TelemetryHistoryPoint, TelemetryResolution, VisualFleetNode} from "../api/types";
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

function RelativeTimestamp({label, now, value}: {label: string; now: Date; value: string | null | undefined}) {
  const timestamp = timestampPresentation(value, now, label);
  return timestamp
    ? <time dateTime={timestamp.dateTime} title={timestamp.exact} aria-label={`${timestamp.relative}; exact time ${timestamp.exact}`}>{timestamp.relative}</time>
    : <>Not reported</>;
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
  const [range, setRange] = useState<HistoryRange>("1h");
  const [history, setHistory] = useState<TelemetryHistory>();
  const [historyError, setHistoryError] = useState("");
  const [historyLoading, setHistoryLoading] = useState(true);
  const [retryRevision, setRetryRevision] = useState(0);
  const [reviewTarget, setReviewTarget] = useState<LibraryActionTarget>();
  const [operation, setOperation] = useState<LibraryOperation>();
  const [operationName, setOperationName] = useState<LibraryActionName>("Stop");
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

    <section aria-labelledby={`${headingId}-overview`}>
      <h4 id={`${headingId}-overview`}>Overview</h4>
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
      <div className="node-recipe-command"><div><h4 id={`${headingId}-recipes`}>Models and recipes</h4><p>Stop or remove what is here, or choose a model and exact recipe to install on this Spark.</p></div><a className="button" href={`/library?spark=${encodeURIComponent(node.id)}`}>Install model or recipe</a></div>
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
