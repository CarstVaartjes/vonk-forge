import type {
  LibraryInstallPlan,
  LibraryLoadPlan,
  LibraryMappingPlan,
  LibrarySnapshot,
  LibraryStopPlan,
  LibraryUninstallPlan,
} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {LibraryPlanReasons} from "./library-plan-reasons";
import {LibraryReasons} from "./library-reasons";
import type {LibraryPlacementGroup} from "./library-action-types";

export type LibraryActionPlan = LibraryMappingPlan | LibraryInstallPlan | LibraryLoadPlan | LibraryStopPlan | LibraryUninstallPlan;

function reasonSets(nodes: Array<{blockers: {code: string; detail: string}[]; warnings: {code: string; detail: string}[]}>) {
  return {
    blockers: nodes.flatMap(node => node.blockers),
    warnings: nodes.flatMap(node => node.warnings),
  };
}

type InstallNodePlan = LibraryInstallPlan["nodes"][number];

function typedInventoryState(node: InstallNodePlan): "stale" | "unavailable" | undefined {
  const codes = node.blockers.concat(node.warnings).map(reason => reason.code.toLowerCase());
  if (codes.some(code => code.includes("inventory") && /missing|unavailable|not[_-]reported/.test(code))) return "unavailable";
  if (codes.some(code => code.includes("inventory") && code.includes("stale"))) return "stale";
  return undefined;
}

function inventoryFreshness(node: InstallNodePlan, previewReceivedAt: number, policy: LibrarySnapshot["freshness_policy"]): string {
  const typedState = typedInventoryState(node);
  if (typedState === "unavailable") return "Inventory unavailable · server preview evidence";
  if (!node.inventory_observed_at) return "Inventory unavailable · observation not reported";
  const observed = Date.parse(node.inventory_observed_at);
  if (!Number.isFinite(observed) || !Number.isFinite(previewReceivedAt)) return "Inventory unavailable · invalid observation time";
  const age = Math.max(0, Math.round((previewReceivedAt - observed) / 1000));
  return `Inventory ${typedState === "stale" || age >= policy.inventory_fresh_seconds ? "stale" : "fresh"} · ${age}s`;
}

export function MappingPreview({evidence, plan, policy}: {
  evidence?: LibraryPlacementGroup;
  plan: LibraryMappingPlan;
  policy: LibrarySnapshot["freshness_policy"];
}) {
  return <div className="action-preview">
    <p><strong>{plan.topology_name}</strong> · generation {plan.generation}</p>
    <ol className="action-node-plans">{plan.nodes.map(node => {
      const authority = evidence?.nodes.find(candidate => candidate.node_id === node.node_id && candidate.rank === node.rank);
      return <li key={node.node_id}>
        <strong>Rank {node.rank} · {node.role}{node.endpoint_owner ? " · endpoint owner" : ""} · {node.node_id}</strong>
        {authority ? <>
          <span>{formatBytes(authority.disk_required_bytes)} disk required · {formatBytes(authority.disk_reserved_bytes)} reserved · {formatBytes(authority.disk_free_after_bytes)} after</span>
          <span>{formatBytes(authority.artifact_reuse_bytes)} exact artifacts reused</span>
          <span>Inventory {authority.inventory_age_seconds < policy.inventory_fresh_seconds ? "fresh" : "stale"} · {authority.inventory_age_seconds}s</span>
        </> : <span>Placement capacity evidence unavailable for this node.</span>}
      </li>;
    })}</ol>
    {evidence && <LibraryReasons reasons={evidence.reasons}/>}
  </div>;
}

export function InstallPreview({plan, policy, previewReceivedAt}: {
  plan: LibraryInstallPlan;
  policy: LibrarySnapshot["freshness_policy"];
  previewReceivedAt: number;
}) {
  const reasons = reasonSets(plan.nodes);
  return <div className="action-preview">
    <p>Exact mapping <strong>{plan.mapping_id}</strong> · generation {plan.mapping_generation}</p>
    <ol className="action-node-plans">{plan.nodes.map(node => <li key={node.node_id}>
      <strong>Rank {node.rank} · {node.role} · {node.node_id}</strong>
      <span>{formatBytes(node.required_bytes)} disk required · {formatBytes(node.required_download_bytes)} download · {formatBytes(node.reused_bytes)} reused</span>
      <span>{formatBytes(node.active_reserved_bytes)} reserved · {node.free_bytes == null ? "Disk inventory unavailable" : `${formatBytes(node.free_bytes)} free`}{node.free_after_bytes == null ? "" : ` · ${formatBytes(node.free_after_bytes)} after`}</span>
      <span>{inventoryFreshness(node, previewReceivedAt, policy)}</span>
      <span>Observed {node.inventory_observed_at ?? "not reported"}</span>
    </li>)}</ol>
    <LibraryPlanReasons heading="Install blockers" reasons={reasons.blockers}/>
    <LibraryPlanReasons heading="Install warnings" reasons={reasons.warnings}/>
  </div>;
}

export function LoadPreview({plan}: {plan: LibraryLoadPlan}) {
  const reasons = reasonSets(plan.nodes);
  const coexistence = reasons.warnings.filter(reason => reason.code.toLowerCase().includes("coexist"));
  return <div className="action-preview">
    <p className="authority-copy">Existing recipes remain loaded. Forge will not unload anything automatically.</p>
    <p>Endpoint alias {plan.alias}</p>
    <p>Selected installation <strong>{plan.installation_id}</strong> · mapping generation {plan.mapping_generation}</p>
    <ol className="action-node-plans">{plan.nodes.map(node => <li key={node.node_id}>
      <strong>Rank {node.rank} · {node.role}{node.endpoint_owner ? " · endpoint owner" : ""}</strong>
      <span>{formatBytes(node.required_memory_bytes)} required · {node.available_memory_bytes == null ? "Memory inventory unavailable" : `${formatBytes(node.available_memory_bytes)} available`}{node.free_after_bytes == null ? "" : ` · ${formatBytes(node.free_after_bytes)} after`}</span>
      <span>{formatBytes(node.active_reserved_bytes)} already reserved · {node.memory_kind} memory · port {node.port}</span>
    </li>)}</ol>
    {coexistence.map(reason => <p className="coexistence-copy" key={reason.code}>{reason.detail}</p>)}
    <LibraryPlanReasons heading="Load blockers" reasons={reasons.blockers}/>
    <LibraryPlanReasons heading="Load warnings" reasons={reasons.warnings.filter(reason => !coexistence.includes(reason))}/>
  </div>;
}

export function StopPreview({plan}: {plan: LibraryStopPlan}) {
  return <div className="action-preview">
    <p>{plan.route_withdrawal ? "Published route will be withdrawn." : `Route remains ${plan.route_state}.`}</p>
    <ol className="action-node-plans">{plan.nodes.map(node => <li key={node.node_id}>
      <strong>Rank {node.rank} · {node.role} · {node.state}</strong><span>{node.node_id} · {formatBytes(node.active_memory_reservation_bytes)} active reservation</span>
    </li>)}</ol>
    <p>{formatBytes(plan.total_active_memory_reservation_bytes)} is actively reserved now.</p>
    <p className="authority-copy">Capacity remains reserved unless every rank stops successfully.</p>
    <LibraryPlanReasons heading="Stop blockers" reasons={plan.blockers}/>
    <LibraryPlanReasons heading="Stop warnings" reasons={plan.warnings}/>
  </div>;
}

export function UninstallPreview({plan}: {plan: LibraryUninstallPlan}) {
  return <div className="action-preview">
    <p>{plan.bytes_removed == null ? "Exact removable bytes are unknown." : `${formatBytes(plan.bytes_removed)} will be removed.`}</p>
    <ol className="action-node-plans">{plan.nodes.map(node => <li key={node.node_id}>
      <strong>Rank {node.rank} · {node.role} · {node.state}</strong><span>{node.node_id} · {node.installed_bytes == null ? "installed bytes unknown" : `${formatBytes(node.installed_bytes)} installed`}</span>
    </li>)}</ol>
    {plan.active_runs.length > 0 && <section aria-label="Active runs"><h4>{plan.active_run_count} active run{plan.active_run_count === 1 ? "" : "s"}</h4><ul>{plan.active_runs.map(run => <li key={run.run_id}>{run.run_id} · {run.state} · route {run.route_state}</li>)}</ul></section>}
    {!plan.consequences.automatic_stop && <p className="authority-copy">Forge will not stop active runs automatically.</p>}
    {plan.consequences.catalog_retained && <p>The local catalog recipe is retained.</p>}
    {plan.consequences.reinstall_required && <p>Reinstall is required to restore removed content.</p>}
    <LibraryPlanReasons heading="Remove blockers" reasons={plan.blockers}/>
    <LibraryPlanReasons heading="Remove warnings" reasons={plan.warnings}/>
  </div>;
}
