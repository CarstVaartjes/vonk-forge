import type {TelemetryHistory, TelemetryHistoryPoint, TelemetryRollupPoint, VisualFleetNode} from "../api/types";
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

function throughput(receive: number | null, transmit: number | null): string {
  if (receive === null && transmit === null) return "Not reported";
  return `↓ ${receive === null ? "—" : `${formatBytes(receive)}/s`} · ↑ ${transmit === null ? "—" : `${formatBytes(transmit)}/s`}`;
}

function utilizationTone(percent: number): "danger" | "healthy" | "warning" {
  if (percent >= 90) return "danger";
  if (percent >= 75) return "warning";
  return "healthy";
}

type TrendMetric = "gpu_utilization_percent" | "memory_available_bytes" | "temperature_c";

function pointValue(point: TelemetryHistoryPoint, metric: TrendMetric): number | null {
  if (isRollupPoint(point)) return point.metrics[metric]?.mean ?? null;
  return finite(point[metric]);
}

function isRollupPoint(point: TelemetryHistoryPoint): point is TelemetryRollupPoint {
  return typeof point === "object" && point !== null && "resolution" in point && typeof point.resolution === "string";
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
  const unavailable = finiteValues.length === 0;
  return <figure className={`node-card-trend${unavailable ? " is-unavailable" : ""}`}>
    <figcaption><span>{label}</span><strong>{current === null ? "Not reported" : format(current)}</strong></figcaption>
    {unavailable
      ? <div className="node-card-trend-unavailable" role="img" aria-label={`${label} ${historyLabel} trend unavailable`}>Unavailable</div>
      : <svg role="img" aria-label={`${label} ${historyLabel} trend`} aria-description={description} viewBox="0 0 100 30" preserveAspectRatio="none">
        {path && <path aria-hidden="true" d={path} vectorEffect="non-scaling-stroke"/>}
      </svg>}
  </figure>;
}

function WorkloadSummary({node, name}: {node: VisualFleetNode; name: string}) {
  return <section className="node-workload-summary" aria-label={`Workloads on ${name}`}>
    <div>
      <header><span>Current work</span></header>
      {node.loaded.length > 0
        ? <ul>{node.loaded.map(run => <li key={run.run_id} className={run.healthy ? "is-healthy" : "is-degraded"}><span>{run.title}</span><small>{run.alias ? `${run.alias} · ` : ""}{run.healthy ? "healthy" : run.group_state}</small></li>)}</ul>
        : <small className="node-model-empty">No active model reported</small>}
    </div>
    <div>
      <header><span>Local recipes</span></header>
      {node.installed.length > 0
        ? <ul>{node.installed.map(item => <li key={item.installation_id} className={item.complete ? "is-healthy" : "is-degraded"}><span>{item.title}</span><small>{item.complete ? "ready" : item.group_state}</small></li>)}</ul>
        : <small className="node-model-empty">No local recipe reported</small>}
    </div>
  </section>;
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
  const networkReceive = finite(sample?.network_receive_bytes_per_second);
  const networkTransmit = finite(sample?.network_transmit_bytes_per_second);

  return <article className={`node-card state-${state}${selected ? " is-selected" : ""}`} aria-label={`${name} — ${label}`}>
    <header className="node-card-heading">
      <div>
        <div className="node-title-line"><h3>{name}</h3><span>{node.labels.role ?? node.lifecycle}</span></div>
        <p className="node-host">{secondaryName ?? node.lifecycle}{timestamp && <> · <time dateTime={timestamp.dateTime} title={timestamp.exact} aria-label={`${timestamp.relative}; exact time ${timestamp.exact}`}>{timestamp.relative}</time></>}</p>
      </div>
      <div className="node-card-heading-actions"><StatusPill tone={statusTone(state)}>{label}</StatusPill><button type="button" className="node-edit-button" aria-label={`Edit ${name}`} onClick={onEdit}>Edit</button></div>
    </header>
    {state === "offline" && <p className="node-offline-reason">{offlineReasonLabel(node.connection.offline_reason)}</p>}

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
      <div className="metric"><dt>CPU / load</dt><dd>{cpu === "Not reported" && load === "Not reported" ? "Not reported" : `${cpu} · load ${load}`}</dd></div>
      <div className="metric"><dt>Disk</dt><dd>{capacity(diskFree, diskTotal, "free")}</dd></div>
      <div className="metric"><dt>Network</dt><dd>{throughput(networkReceive, networkTransmit)}</dd></div>
      <div className="metric"><dt>Board power</dt><dd>{formatMetric(sample?.power_watts, value => `${value.toFixed(1)} W`)}</dd></div>
    </dl>

    <WorkloadSummary node={node} name={name}/>
    {warnings.length > 0 && <ul className="node-warnings" aria-label={`Warnings for ${name}: ${warnings.map(warning => warning.detail).join(" ")}`} title={warnings.map(warning => `${warning.severity}: ${warning.detail}`).join("\n")}>
      <li className={`severity-${warnings[0]!.severity}`}><strong>{warnings[0]!.severity}</strong> {warnings[0]!.detail}{warnings.length > 1 && <span className="node-warning-overflow">+{warnings.length - 1} more</span>}</li>
    </ul>}
    <button type="button" className="node-detail-trigger" aria-expanded={selected} onClick={onSelect}>View {name} details</button>
  </article>;
}
