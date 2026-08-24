import type {TelemetryHistory, TelemetryHistoryPoint, VisualFleetNode} from "../api/types";
import {
  formatBytes,
  formatMetric,
  nodeDisplayName,
  nodeOperationalState,
  nodeSecondaryName,
  nodeUnifiedMemory,
  nodeWarningsAt,
  offlineReasonLabel,
  timestampPresentation,
} from "../lib/fleet";
import {Meter} from "./meter";
import {sparklinePath} from "./sparkline";
import {StatusPill} from "./status-pill";

function statusLabel(state: ReturnType<typeof nodeOperationalState>): string {
  return state.charAt(0).toUpperCase() + state.slice(1);
}

function statusTone(state: ReturnType<typeof nodeOperationalState>) {
  if (state === "live") return "healthy" as const;
  if (state === "delayed" || state === "stale") return "warning" as const;
  return "danger" as const;
}

function finite(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function capacity(free: number | null, total: number | null, freeLabel: string): string {
  if (free === null || total === null) return "Not reported";
  return `${formatBytes(free)} ${freeLabel} / ${formatBytes(total)}`;
}

function utilizationTone(percent: number): "danger" | "healthy" | "warning" {
  if (percent >= 90) return "danger";
  if (percent >= 75) return "warning";
  return "healthy";
}

type TrendMetric = "gpu_utilization_percent" | "memory_available_bytes" | "temperature_c";

function pointValue(point: TelemetryHistoryPoint, metric: TrendMetric): number | null {
  if ("resolution" in point) return point.metrics[metric]?.mean ?? null;
  return finite(point[metric]);
}

function CardTrend({
  current,
  domain,
  format,
  history,
  historyLabel,
  label,
  metric,
}: {
  current: number | null;
  domain?: readonly [number, number];
  format(value: number): string;
  history?: TelemetryHistory;
  historyLabel: string;
  label: string;
  metric: TrendMetric;
}) {
  const values = (history?.points ?? []).map(point => pointValue(point, metric));
  if (current !== null && values.at(-1) !== current) values.push(current);
  const finiteValues = values.filter((value): value is number => value !== null && Number.isFinite(value));
  const plotted = values.length === 1 ? [values[0], values[0]] : values;
  const path = sparklinePath(plotted, 100, 30, domain);
  const minimum = finiteValues.length > 0 ? Math.min(...finiteValues) : undefined;
  const maximum = finiteValues.length > 0 ? Math.max(...finiteValues) : undefined;
  const description = current === null || minimum === undefined || maximum === undefined
    ? "No reported samples."
    : `Latest ${format(current)}; range ${format(minimum)} to ${format(maximum)}; ${finiteValues.length} reported points.`;
  return <figure className="node-card-trend">
    <figcaption><span>{label}</span><strong>{current === null ? "Not reported" : format(current)}</strong></figcaption>
    <svg role="img" aria-label={`${label} ${historyLabel} trend`} aria-description={description} viewBox="0 0 100 30" preserveAspectRatio="none">
      {path && <path aria-hidden="true" d={path} vectorEffect="non-scaling-stroke"/>}
    </svg>
  </figure>;
}

function WorkloadSummary({node, name}: {node: VisualFleetNode; name: string}) {
  const loaded = node.loaded.filter(run => run.healthy);
  const installed = node.installed.filter(item => item.complete);
  const degraded = node.loaded.filter(run => !run.healthy).length + node.installed.filter(item => !item.complete).length;
  return <div className="node-workload-summary" aria-label={`Workloads on ${name}`}>
    <div><span>Loaded now</span><strong>{loaded.length}</strong><small>{loaded[0]?.title ?? "No active model"}</small></div>
    <div><span>Installed</span><strong>{installed.length}</strong><small>{installed[0]?.title ?? "No complete install"}</small></div>
    {degraded > 0 && <p><strong>{degraded}</strong> workload {degraded === 1 ? "state needs" : "states need"} attention</p>}
  </div>;
}

export function NodeCard({
  node,
  now,
  onSelect,
  onEdit,
  selected,
  history,
  historyLabel = "24h",
  historyLoading = false,
  historyError = "",
}: {
  node: VisualFleetNode;
  now: Date;
  onSelect(): void;
  onEdit(): void;
  selected: boolean;
  history?: TelemetryHistory;
  historyLabel?: string;
  historyLoading?: boolean;
  historyError?: string;
}) {
  const name = nodeDisplayName(node);
  const secondaryName = nodeSecondaryName(node);
  const state = nodeOperationalState(node, now);
  const label = statusLabel(state);
  const sample = node.telemetry?.sample;
  const unified = nodeUnifiedMemory(node);
  const diskFree = finite(sample?.disk_free_bytes ?? node.inventory?.disk_free_bytes);
  const diskTotal = finite(sample?.disk_total_bytes ?? node.inventory?.disk_total_bytes);
  const accelerator = sample?.details.accelerator_name
    ? `${sample.details.accelerator_name}${sample.details.accelerator_performance_state ? ` · ${sample.details.accelerator_performance_state}` : ""}`
    : "Not reported";
  const cpu = formatMetric(sample?.cpu_utilization_percent, value => `${value.toFixed(1)}%`);
  const load = formatMetric(sample?.load_average_1m, value => value.toFixed(2));
  const observedAt = sample?.observed_at;
  const timestamp = timestampPresentation(observedAt, now);
  const warnings = nodeWarningsAt(node, now);

  return <article className={`node-card state-${state}${selected ? " is-selected" : ""}`} aria-label={`${name} — ${label}`}>
    <header className="node-card-heading">
      <div>
        <p className="node-eyebrow">{node.labels.role ?? node.lifecycle}</p>
        <h3>{name}</h3>
        {secondaryName && <p className="node-host">{secondaryName}</p>}
      </div>
      <div className="node-card-heading-actions"><StatusPill tone={statusTone(state)}>{label}</StatusPill><button type="button" className="node-edit-button" aria-label={`Edit ${name}`} onClick={onEdit}>Edit</button></div>
    </header>
    <div className="node-freshness">
      {timestamp
        ? <time dateTime={timestamp.dateTime} title={timestamp.exact} aria-label={`${timestamp.relative}; exact time ${timestamp.exact}`}>{timestamp.relative}</time>
        : <span>Update time not reported</span>}
      {state === "offline" && <strong>{offlineReasonLabel(node.connection.offline_reason)}</strong>}
    </div>

    <div className="node-capacity-visual">
      {unified
        ? <Meter label="Unified memory in use" max={unified.total} value={unified.used} tone={utilizationTone(unified.utilizationPercent)} valueLabel={`${formatBytes(unified.available)} available of ${formatBytes(unified.total)}`}/>
        : <div className="meter meter-neutral"><div className="meter-heading"><span>Unified memory</span><strong>Not reported</strong></div><div className="meter-unknown" role="img" aria-label="Unified memory not reported"/></div>}
    </div>

    <section className="node-card-trends" aria-label={`${name} ${historyLabel} telemetry`}>
      <div className="node-card-trends-heading"><strong>{historyLabel} trend</strong>{historyLoading ? <span role="status">Loading history…</span> : historyError ? <span title={historyError}>Latest sample only</span> : <span>{history?.resolution === "fifteen-minute" ? "15-minute" : "Minute"} history</span>}</div>
      <div className="node-card-trend-grid">
        <CardTrend label="GPU" historyLabel={historyLabel} metric="gpu_utilization_percent" current={finite(sample?.gpu_utilization_percent)} history={history} domain={[0, 100]} format={value => `${Number(value.toFixed(1))}%`}/>
        <CardTrend label="Memory free" historyLabel={historyLabel} metric="memory_available_bytes" current={finite(sample?.memory_available_bytes)} history={history} format={formatBytes}/>
        <CardTrend label="Temperature" historyLabel={historyLabel} metric="temperature_c" current={finite(sample?.temperature_c)} history={history} domain={[0, 100]} format={value => `${Number(value.toFixed(1))} °C`}/>
      </div>
    </section>

    <dl className="node-metrics">
      <div className="metric metric-featured"><dt>Accelerator</dt><dd>{accelerator}</dd></div>
      <div className="metric"><dt>Disk</dt><dd>{capacity(diskFree, diskTotal, "free")}</dd></div>
      <div className="metric"><dt>CPU / load</dt><dd>{cpu === "Not reported" && load === "Not reported" ? "Not reported" : `${cpu} · load ${load}`}</dd></div>
      <div className="metric"><dt>Power</dt><dd>{formatMetric(sample?.power_watts, value => `${value.toFixed(1)} W`)}</dd></div>
    </dl>

    <WorkloadSummary node={node} name={name}/>
    {warnings.length > 0 && <ul className="node-warnings" aria-label={`Warnings for ${name}`}>
      {warnings.map((warning, index) => <li key={`${warning.code}:${index}`} className={`severity-${warning.severity}`}><strong>{warning.severity}</strong> {warning.detail}</li>)}
    </ul>}
    <button type="button" className="node-detail-trigger" aria-expanded={selected} onClick={onSelect}>View {name} details</button>
  </article>;
}
