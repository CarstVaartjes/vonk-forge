import type {VisualFleetNode} from "../api/types";
import {
  formatBytes,
  formatMetric,
  installationGroupLabel,
  nodeOperationalState,
  nodeWarningsAt,
  offlineReasonLabel,
  runGroupLabel,
} from "../lib/fleet";
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

function ageLabel(observedAt: string | null | undefined, now: Date): string {
  if (!observedAt) return "Update time not reported";
  const observed = Date.parse(observedAt);
  if (!Number.isFinite(observed)) return "Update time not reported";
  const seconds = Math.max(0, Math.round((now.getTime() - observed) / 1000));
  return `Updated ${seconds} ${seconds === 1 ? "second" : "seconds"} ago`;
}

function timestampLabel(value: string | null | undefined): string {
  if (!value) return "Timestamp not reported";
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return "Timestamp not reported";
  return `Observed ${parsed.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "UTC", timeZoneName: "short"})}`;
}

function RecipeGroups({node}: {node: VisualFleetNode}) {
  const installed = node.installed.filter(installation => installation.complete);
  const installationStates = node.installed.filter(installation => !installation.complete);
  const loaded = node.loaded.filter(run => run.healthy);
  const runStates = node.loaded.filter(run => !run.healthy);
  return <div className="node-recipe-groups">
    <section aria-label={`Loaded recipes on ${node.display_name}`}>
      <h4>Loaded now</h4>
      {loaded.length === 0
        ? <p className="empty-copy">Nothing is loaded now</p>
        : <ul className="recipe-presence-list">{loaded.map(run => <li key={`${run.run_id}:${run.rank}`} className="is-healthy">
          <strong>{run.title}</strong><span>{run.alias} · {run.role} rank {run.rank}</span><small>Group {run.group_state} · Run {run.run_state} · Rank {run.rank_state} · Route {run.route_state}</small><small>{runGroupLabel(run)}</small>
        </li>)}</ul>}
    </section>
    <section aria-label={`Installed recipes on ${node.display_name}`}>
      <h4>Installed</h4>
      {installed.length === 0
        ? <p className="empty-copy">No complete installations reported</p>
        : <ul className="recipe-presence-list">{installed.map(installation => <li key={`${installation.installation_id}:${installation.rank}`} className="is-healthy">
          <strong>{installation.title}</strong><span>{installation.topology_name} · {installation.role} rank {installation.rank}</span><small>Group {installation.group_state} · Rank {installation.rank_state}</small><small>{installationGroupLabel(installation)}</small>
        </li>)}</ul>}
    </section>
    <section aria-label={`Installation state on ${node.display_name}`}>
      <h4>Installation state</h4>
      {installationStates.length === 0
        ? <p className="empty-copy">No incomplete installation states</p>
        : <ul className="recipe-presence-list">{installationStates.map(installation => <li key={`${installation.installation_id}:${installation.rank}`} className="is-degraded">
          <strong>{installation.title}</strong><span>{installation.topology_name} · {installation.role} rank {installation.rank}</span><small>Group {installation.group_state} · Rank {installation.rank_state}</small><small>{installationGroupLabel(installation)}</small>
        </li>)}</ul>}
    </section>
    <section aria-label={`Run state on ${node.display_name}`}>
      <h4>Run state</h4>
      {runStates.length === 0
        ? <p className="empty-copy">No inactive or degraded run states</p>
        : <ul className="recipe-presence-list">{runStates.map(run => <li key={`${run.run_id}:${run.rank}`} className="is-degraded">
          <strong>{run.title}</strong><span>{run.alias} · {run.role} rank {run.rank}</span><small>Group {run.group_state} · Run {run.run_state} · Rank {run.rank_state} · Route {run.route_state}</small><small>{runGroupLabel(run)}</small>
        </li>)}</ul>}
    </section>
  </div>;
}

export function NodeCard({
  node,
  now,
  onSelect,
  selected,
}: {
  node: VisualFleetNode;
  now: Date;
  onSelect(): void;
  selected: boolean;
}) {
  const state = nodeOperationalState(node, now);
  const label = statusLabel(state);
  const sample = node.telemetry?.sample;
  const hostFree = finite(sample?.memory_available_bytes ?? node.inventory?.host_memory_free_bytes);
  const hostTotal = finite(sample?.memory_total_bytes ?? node.inventory?.host_memory_total_bytes);
  const gpuFree = finite(sample?.gpu_memory_free_bytes ?? node.inventory?.gpu_memory_free_bytes);
  const gpuTotal = finite(sample?.gpu_memory_total_bytes ?? node.inventory?.gpu_memory_total_bytes);
  const unifiedFree = hostFree !== null && gpuFree !== null ? Math.min(hostFree, gpuFree) : null;
  const unifiedTotal = hostTotal !== null && gpuTotal !== null ? Math.min(hostTotal, gpuTotal) : null;
  const diskFree = finite(sample?.disk_free_bytes ?? node.inventory?.disk_free_bytes);
  const diskTotal = finite(sample?.disk_total_bytes ?? node.inventory?.disk_total_bytes);
  const accelerator = sample?.details.accelerator_name
    ? `${sample.details.accelerator_name}${sample.details.accelerator_performance_state ? ` · ${sample.details.accelerator_performance_state}` : ""}`
    : "Not reported";
  const cpu = formatMetric(sample?.cpu_utilization_percent, value => `${value.toFixed(1)}%`);
  const load = formatMetric(sample?.load_average_1m, value => value.toFixed(2));
  const receive = formatMetric(sample?.network_receive_bytes_per_second, value => `${formatBytes(value)}/s`);
  const transmit = formatMetric(sample?.network_transmit_bytes_per_second, value => `${formatBytes(value)}/s`);
  const observedAt = sample?.observed_at;
  const warnings = nodeWarningsAt(node, now);

  return <article className={`node-card state-${state}${selected ? " is-selected" : ""}`} aria-label={`${node.display_name} — ${label}`}>
    <header className="node-card-heading">
      <div>
        <p className="node-eyebrow">{node.labels.role ?? node.lifecycle}</p>
        <h3>{node.display_name}</h3>
        <p className="node-host">{node.hostname}</p>
      </div>
      <StatusPill tone={statusTone(state)}>{label}</StatusPill>
    </header>
    <div className="node-freshness">
      <time dateTime={observedAt}>{ageLabel(observedAt, now)}</time>
      <span>{timestampLabel(observedAt)}</span>
      {state === "offline" && <strong>{offlineReasonLabel(node.connection.offline_reason)}</strong>}
    </div>

    <dl className="node-metrics">
      <div className="metric metric-featured"><dt>Accelerator</dt><dd>{accelerator}</dd></div>
      <div className="metric metric-featured" data-metric="gpu"><dt>GPU utilization</dt><dd>{formatMetric(sample?.gpu_utilization_percent, value => `${value.toFixed(1)}%`)}</dd></div>
      <div className="metric"><dt>Unified memory</dt><dd>{capacity(unifiedFree, unifiedTotal, "available")}</dd></div>
      <div className="metric"><dt>Host memory</dt><dd>{capacity(hostFree, hostTotal, "available")}</dd></div>
      <div className="metric"><dt>GPU memory</dt><dd>{capacity(gpuFree, gpuTotal, "available")}</dd></div>
      <div className="metric"><dt>Disk</dt><dd>{capacity(diskFree, diskTotal, "free")}</dd></div>
      <div className="metric"><dt>CPU / load</dt><dd>{cpu === "Not reported" && load === "Not reported" ? "Not reported" : `${cpu} · load ${load}`}</dd></div>
      <div className="metric"><dt>Temperature</dt><dd>{formatMetric(sample?.temperature_c, value => `${value.toFixed(1)} °C`)}</dd></div>
      <div className="metric"><dt>Power</dt><dd>{formatMetric(sample?.power_watts, value => `${value.toFixed(1)} W`)}</dd></div>
      <div className="metric"><dt>Network</dt><dd>{receive === "Not reported" && transmit === "Not reported" ? "Not reported" : `↓ ${receive} · ↑ ${transmit}`}</dd></div>
    </dl>

    <RecipeGroups node={node}/>
    {warnings.length > 0 && <ul className="node-warnings" aria-label={`Warnings for ${node.display_name}`}>
      {warnings.map((warning, index) => <li key={`${warning.code}:${index}`} className={`severity-${warning.severity}`}><strong>{warning.severity}</strong> {warning.detail}</li>)}
    </ul>}
    <button type="button" className="node-detail-trigger" aria-expanded={selected} onClick={onSelect}>View {node.display_name} details</button>
  </article>;
}
