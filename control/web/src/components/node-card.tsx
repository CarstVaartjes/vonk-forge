import type {VisualFleetNode} from "../api/types";
import {
  formatBytes,
  formatMetric,
  installationGroupLabel,
  nodeDisplayName,
  nodeOperationalState,
  nodeSecondaryName,
  nodeUnifiedMemory,
  nodeWarningsAt,
  offlineReasonLabel,
  runGroupLabel,
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

function RecipeGroups({node, name}: {node: VisualFleetNode; name: string}) {
  const installed = node.installed.filter(installation => installation.complete);
  const installationStates = node.installed.filter(installation => !installation.complete);
  const loaded = node.loaded.filter(run => run.healthy);
  const runStates = node.loaded.filter(run => !run.healthy);
  return <div className="node-recipe-groups">
    <section aria-label={`Loaded recipes on ${name}`}>
      <h4>Loaded now</h4>
      {loaded.length === 0
        ? <p className="empty-copy">Nothing is loaded now</p>
        : <ul className="recipe-presence-list">{loaded.map(run => <li key={`${run.run_id}:${run.rank}`} className="is-healthy">
          <strong>{run.title}</strong><span>{run.alias} · {run.role} rank {run.rank}</span><small>Group {run.group_state} · Run {run.run_state} · Rank {run.rank_state} · Route {run.route_state}</small><small>{runGroupLabel(run)}</small>
        </li>)}</ul>}
    </section>
    <section aria-label={`Installed recipes on ${name}`}>
      <h4>Installed</h4>
      {installed.length === 0
        ? <p className="empty-copy">No complete installations reported</p>
        : <ul className="recipe-presence-list">{installed.map(installation => <li key={`${installation.installation_id}:${installation.rank}`} className="is-healthy">
          <strong>{installation.title}</strong><span>{installation.topology_name} · {installation.role} rank {installation.rank}</span><small>Group {installation.group_state} · Rank {installation.rank_state}</small><small>{installationGroupLabel(installation)}</small>
        </li>)}</ul>}
    </section>
    <section aria-label={`Installation state on ${name}`}>
      <h4>Installation state</h4>
      {installationStates.length === 0
        ? <p className="empty-copy">No incomplete installation states</p>
        : <ul className="recipe-presence-list">{installationStates.map(installation => <li key={`${installation.installation_id}:${installation.rank}`} className="is-degraded">
          <strong>{installation.title}</strong><span>{installation.topology_name} · {installation.role} rank {installation.rank}</span><small>Group {installation.group_state} · Rank {installation.rank_state}</small><small>{installationGroupLabel(installation)}</small>
        </li>)}</ul>}
    </section>
    <section aria-label={`Run state on ${name}`}>
      <h4>Run state</h4>
      {runStates.length === 0
        ? <p className="empty-copy">No inactive or degraded run states</p>
        : <ul className="recipe-presence-list">{runStates.map(run => <li key={`${run.run_id}:${run.rank}`} className="is-degraded">
          <strong>{run.title}</strong><span>{run.alias} · {run.role} rank {run.rank}</span><small>Group {run.group_state} · Run {run.run_state} · Rank {run.rank_state} · Route {run.route_state}</small><small>{runGroupLabel(run)}</small>
        </li>)}</ul>}
    </section>
  </div>;
}

function utilizationTone(percent: number): "danger" | "healthy" | "warning" {
  if (percent >= 90) return "danger";
  if (percent >= 75) return "warning";
  return "healthy";
}

function MiniTrend({label, values}: {label: string; values: readonly number[]}) {
  const path = sparklinePath(values);
  const latest = values.at(-1);
  const minimum = values.length > 0 ? Math.min(...values) : undefined;
  const maximum = values.length > 0 ? Math.max(...values) : undefined;
  const description = latest === undefined
    ? "No recent samples."
    : `Latest ${latest.toFixed(1)}%; range ${minimum?.toFixed(1)}% to ${maximum?.toFixed(1)}%; ${values.length} recent ${values.length === 1 ? "sample" : "samples"}.`;
  return <figure className="node-mini-trend">
    <svg role="img" aria-label={label} aria-description={description} viewBox="0 0 100 30" preserveAspectRatio="none">
      {path && <path aria-hidden="true" d={path} vectorEffect="non-scaling-stroke"/>}
    </svg>
    <figcaption>Recent GPU trend</figcaption>
  </figure>;
}

export function NodeCard({
  node,
  now,
  onSelect,
  selected,
  trend = [],
}: {
  node: VisualFleetNode;
  now: Date;
  onSelect(): void;
  selected: boolean;
  trend?: readonly number[];
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
  const receive = formatMetric(sample?.network_receive_bytes_per_second, value => `${formatBytes(value)}/s`);
  const transmit = formatMetric(sample?.network_transmit_bytes_per_second, value => `${formatBytes(value)}/s`);
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
      <StatusPill tone={statusTone(state)}>{label}</StatusPill>
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
      {trend.length > 0 && <MiniTrend label={`${name} recent GPU utilization`} values={trend}/>}
    </div>

    <dl className="node-metrics">
      <div className="metric metric-featured"><dt>Accelerator</dt><dd>{accelerator}</dd></div>
      <div className="metric metric-featured" data-metric="gpu"><dt>GPU utilization</dt><dd>{formatMetric(sample?.gpu_utilization_percent, value => `${value.toFixed(1)}%`)}</dd></div>
      <div className="metric"><dt>Disk</dt><dd>{capacity(diskFree, diskTotal, "free")}</dd></div>
      <div className="metric"><dt>CPU / load</dt><dd>{cpu === "Not reported" && load === "Not reported" ? "Not reported" : `${cpu} · load ${load}`}</dd></div>
      <div className="metric"><dt>Temperature</dt><dd>{formatMetric(sample?.temperature_c, value => `${value.toFixed(1)} °C`)}</dd></div>
      <div className="metric"><dt>Power</dt><dd>{formatMetric(sample?.power_watts, value => `${value.toFixed(1)} W`)}</dd></div>
      <div className="metric"><dt>Network</dt><dd>{receive === "Not reported" && transmit === "Not reported" ? "Not reported" : `↓ ${receive} · ↑ ${transmit}`}</dd></div>
    </dl>

    <RecipeGroups node={node} name={name}/>
    {warnings.length > 0 && <ul className="node-warnings" aria-label={`Warnings for ${name}`}>
      {warnings.map((warning, index) => <li key={`${warning.code}:${index}`} className={`severity-${warning.severity}`}><strong>{warning.severity}</strong> {warning.detail}</li>)}
    </ul>}
    <button type="button" className="node-detail-trigger" aria-expanded={selected} onClick={onSelect}>View {name} details</button>
  </article>;
}
