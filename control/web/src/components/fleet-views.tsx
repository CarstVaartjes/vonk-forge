import type {VisualFleetNode} from "../api/types";
import {
  formatBytes,
  formatMetric,
  nodeDisplayName,
  nodeOperationalState,
  nodeSecondaryName,
  nodeUnifiedMemory,
  nodeWarningsAt,
  timestampPresentation,
} from "../lib/fleet";
import {Meter} from "./meter";
import {StatusPill, type StatusTone} from "./status-pill";

type FleetViewProps = {
  nodes: readonly VisualFleetNode[];
  now: Date;
  onSelect(nodeId: string): void;
  selectedNodeId?: string;
};

function statusPresentation(node: VisualFleetNode, now: Date): {label: string; tone: StatusTone} {
  const state = nodeOperationalState(node, now);
  return {
    label: state.charAt(0).toUpperCase() + state.slice(1),
    tone: state === "live" ? "healthy" : state === "offline" ? "danger" : "warning",
  };
}

function utilizationTone(percent: number): StatusTone {
  if (percent >= 90) return "danger";
  if (percent >= 75) return "warning";
  return "healthy";
}

function MemoryMeter({node}: {node: VisualFleetNode}) {
  const memory = nodeUnifiedMemory(node);
  if (!memory) return <span className="compact-not-reported">Not reported</span>;
  return <Meter
    label="Unified memory in use"
    max={memory.total}
    value={memory.used}
    tone={utilizationTone(memory.utilizationPercent)}
    valueLabel={`${formatBytes(memory.available)} available`}
  />;
}

export function FleetCompactView({nodes, now, onSelect, selectedNodeId}: FleetViewProps) {
  return <section className="fleet-compact" aria-label="Fleet nodes compact table">
    <div className="fleet-table-scroll">
      <table>
        <caption className="sr-only">Fleet nodes, health, capacity, workloads, and latest update</caption>
        <thead><tr><th scope="col">Spark</th><th scope="col">Health</th><th scope="col">Unified memory</th><th scope="col">GPU</th><th scope="col">Workloads</th><th scope="col">Latest update</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>{nodes.map(node => {
          const name = nodeDisplayName(node);
          const secondary = nodeSecondaryName(node);
          const status = statusPresentation(node, now);
          const timestamp = timestampPresentation(node.telemetry?.sample.observed_at, now);
          const loaded = node.loaded.filter(run => run.healthy).length;
          const installed = node.installed.filter(item => item.complete).length;
          const warnings = nodeWarningsAt(node, now).length;
          return <tr key={node.id} className={selectedNodeId === node.id ? "is-selected" : undefined}>
            <th scope="row" data-label="Spark"><strong>{name}</strong>{secondary && <small>{secondary}</small>}</th>
            <td data-label="Health"><StatusPill tone={status.tone}>{status.label}</StatusPill>{warnings > 0 && <small>{warnings} {warnings === 1 ? "warning" : "warnings"}</small>}</td>
            <td data-label="Unified memory"><MemoryMeter node={node}/></td>
            <td data-label="GPU"><strong>{formatMetric(node.telemetry?.sample.gpu_utilization_percent, value => `${value.toFixed(1)}%`)}</strong><small>{node.telemetry?.sample.details.accelerator_name ?? "Accelerator not reported"}</small></td>
            <td data-label="Workloads"><strong>{loaded} loaded</strong><small>{installed} installed</small></td>
            <td data-label="Latest update">{timestamp
              ? <time dateTime={timestamp.dateTime} title={timestamp.exact} aria-label={`${timestamp.relative}; exact time ${timestamp.exact}`}>{timestamp.relative.replace(/^Updated /, "")}</time>
              : <span>Not reported</span>}</td>
            <td data-label="Action"><button type="button" className="secondary-button compact-detail-button" aria-expanded={selectedNodeId === node.id} onClick={() => onSelect(node.id)}>View details<span className="sr-only"> for {name}</span></button></td>
          </tr>;
        })}</tbody>
      </table>
    </div>
  </section>;
}

type TopologyWorkload = {
  key: string;
  kind: "Installed" | "Running";
  nodeIds: readonly string[];
  state: string;
  title: string;
  tone: StatusTone;
};

function topologyWorkloads(nodes: readonly VisualFleetNode[]): TopologyWorkload[] {
  const workloads = new Map<string, TopologyWorkload>();
  for (const node of nodes) {
    for (const run of node.loaded) {
      if (!workloads.has(`run:${run.run_id}`)) workloads.set(`run:${run.run_id}`, {
        key: `run:${run.run_id}`,
        kind: "Running",
        nodeIds: run.member_node_ids,
        state: run.healthy ? "Healthy route" : "Degraded route",
        title: run.title,
        tone: run.healthy ? "healthy" : "warning",
      });
    }
    for (const installation of node.installed) {
      if (!workloads.has(`install:${installation.installation_id}`)) workloads.set(`install:${installation.installation_id}`, {
        key: `install:${installation.installation_id}`,
        kind: "Installed",
        nodeIds: installation.member_node_ids,
        state: installation.complete ? "Complete placement" : "Partial placement",
        title: installation.title,
        tone: installation.complete ? "info" : "warning",
      });
    }
  }
  return [...workloads.values()].sort((left, right) => left.title.localeCompare(right.title));
}

export function FleetTopologyView({nodes, now, onSelect, selectedNodeId}: FleetViewProps) {
  const names = new Map(nodes.map(node => [node.id, nodeDisplayName(node)]));
  const workloads = topologyWorkloads(nodes);
  return <section className="fleet-topology" aria-labelledby="fleet-topology-heading">
    <header><div><p className="fleet-kicker">Live relationship map</p><h3 id="fleet-topology-heading">Fleet topology</h3></div><p>Controller connectivity and recipe placement, using reported Fleet state.</p></header>
    <div className="topology-map">
      <div className="topology-controller" aria-label="Vonk Forge controller"><span className="topology-controller-mark" aria-hidden="true">VF</span><div><strong>Vonk Forge</strong><small>Control plane</small></div></div>
      <div className="topology-trunk" aria-hidden="true"><span/></div>
      <ul className="topology-node-list" aria-label="Connected Sparks">{nodes.map(node => {
        const name = nodeDisplayName(node);
        const status = statusPresentation(node, now);
        const memory = nodeUnifiedMemory(node);
        return <li key={node.id} className={`topology-node topology-node-${status.tone}`}>
          <button type="button" aria-pressed={selectedNodeId === node.id} aria-label={`View ${name} details, ${status.label}`} onClick={() => onSelect(node.id)}>
            <span className="topology-node-heading"><strong>{name}</strong><StatusPill tone={status.tone}>{status.label}</StatusPill></span>
            <span className="topology-node-role">{node.labels.role ?? node.lifecycle}</span>
            <span className="topology-node-memory" role="img" aria-label={memory ? `${Math.round(memory.utilizationPercent)}% of unified memory in use` : "Unified memory not reported"}>
              <span aria-hidden="true" style={{width: `${memory?.utilizationPercent ?? 0}%`}}/>
            </span>
            <span className="topology-node-caption">{memory ? `${formatBytes(memory.available)} memory available` : "Memory not reported"}</span>
          </button>
        </li>;
      })}</ul>
    </div>
    <section className="topology-workloads" aria-labelledby="topology-workloads-heading">
      <div className="topology-workloads-heading"><h4 id="topology-workloads-heading">Recipe routes and placements</h4><span>{workloads.length} {workloads.length === 1 ? "relationship" : "relationships"}</span></div>
      {workloads.length === 0
        ? <p className="topology-empty">No installed or running recipes are reported yet.</p>
        : <ul>{workloads.map(workload => <li key={workload.key}>
          <span className="topology-workload-line" aria-hidden="true"/>
          <div><span className="topology-workload-heading"><strong>{workload.title}</strong><StatusPill tone={workload.tone}>{workload.state}</StatusPill></span><small>{workload.kind} on {workload.nodeIds.map(nodeId => names.get(nodeId) ?? "Unavailable Spark").join(" · ")}</small></div>
        </li>)}</ul>}
    </section>
  </section>;
}
