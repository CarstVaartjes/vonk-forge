import type {
  LibraryBuildPlan,
  LibraryImageDistributionPlan,
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
import {useLibraryNodeName} from "./library-node-names";
import {humanizeIdentifier, TechnicalDetails} from "./library-technical-details";

export type LibraryActionPlan = LibraryBuildPlan | LibraryMappingPlan | LibraryImageDistributionPlan | LibraryInstallPlan | LibraryLoadPlan | LibraryStopPlan | LibraryUninstallPlan;

export function BuildPreview({plan}: {plan: LibraryBuildPlan}) {
  const nodeName = useLibraryNodeName();
  return <div className="action-preview">
    <p><strong>Build the recipe image on {nodeName(plan.builder_node_id)}</strong></p>
    <p>The immutable source bundle and recipe revision below are the complete build inputs.</p>
    <TechnicalDetails items={[
      {label: "Builder node ID", value: plan.builder_node_id},
      {label: "Recipe revision ID", value: plan.recipe_revision_id},
      {label: "Recipe content SHA-256", value: plan.recipe_content_sha256},
      {label: "Source bundle SHA-256", value: plan.source_bundle_sha256},
      {label: "Build input SHA-256", value: plan.build_input_sha256},
    ]}/>
  </div>;
}

export function ImageDistributionPreview({plan}: {plan: LibraryImageDistributionPlan}) {
  const nodeName = useLibraryNodeName();
  return <div className="action-preview">
    <p><strong>Copy the exact built image to {plan.node_ids.length} mapped Spark{plan.node_ids.length === 1 ? "" : "s"}</strong></p>
    <p>Mapping generation {plan.mapping_generation}</p>
    <ol className="action-node-plans">{plan.node_ids.map(nodeId => <li key={nodeId}>
      <strong>{nodeName(nodeId)}</strong>
      <TechnicalDetails compact items={[{label: "Node ID", value: nodeId}]}/>
    </li>)}</ol>
    <TechnicalDetails items={[
      {label: "Image digest", value: plan.image_digest},
      {label: "Recipe build ID", value: plan.recipe_build_id},
      {label: "Mapping ID", value: plan.mapping_id},
    ]}/>
  </div>;
}

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
  const nodeName = useLibraryNodeName();
  return <div className="action-preview">
    <p><strong>{plan.topology_name}</strong> · generation {plan.generation}</p>
    <ol className="action-node-plans">{plan.nodes.map(node => {
      const authority = evidence?.nodes.find(candidate => candidate.node_id === node.node_id && candidate.rank === node.rank);
      return <li key={node.node_id}>
        <strong>Rank {node.rank} · {humanizeIdentifier(node.role)}{node.endpoint_owner ? " · endpoint owner" : ""} · {nodeName(node.node_id)}</strong>
        <TechnicalDetails compact items={[{label: "Node ID", value: node.node_id}]}/>
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
  const nodeName = useLibraryNodeName();
  const reasons = reasonSets(plan.nodes);
  return <div className="action-preview">
    <p>Selected mapping · generation {plan.mapping_generation}</p>
    <TechnicalDetails compact items={[{label: "Mapping ID", value: plan.mapping_id}]}/>
    <ol className="action-node-plans">{plan.nodes.map(node => <li key={node.node_id}>
      <strong>Rank {node.rank} · {humanizeIdentifier(node.role)} · {nodeName(node.node_id)}</strong>
      <TechnicalDetails compact items={[{label: "Node ID", value: node.node_id}]}/>
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
  const nodeName = useLibraryNodeName();
  const reasons = reasonSets(plan.nodes);
  const coexistence = reasons.warnings.filter(reason => reason.code.toLowerCase().includes("coexist"));
  return <div className="action-preview">
    <p className="authority-copy">Existing recipes remain loaded. Forge will not unload anything automatically.</p>
    <p>Endpoint alias {plan.alias}</p>
    <p>Selected installation · mapping generation {plan.mapping_generation}</p>
    <TechnicalDetails compact items={[{label: "Installation ID", value: plan.installation_id}, {label: "Mapping ID", value: plan.mapping_id}]}/>
    <ol className="action-node-plans">{plan.nodes.map(node => <li key={node.node_id}>
      <strong>Rank {node.rank} · {humanizeIdentifier(node.role)}{node.endpoint_owner ? " · endpoint owner" : ""} · {nodeName(node.node_id)}</strong>
      <TechnicalDetails compact items={[{label: "Node ID", value: node.node_id}]}/>
      <span>{formatBytes(node.required_memory_bytes)} required · {node.available_memory_bytes == null ? "Memory inventory unavailable" : `${formatBytes(node.available_memory_bytes)} available`}{node.free_after_bytes == null ? "" : ` · ${formatBytes(node.free_after_bytes)} after`}</span>
      <span>{formatBytes(node.active_reserved_bytes)} already reserved · {node.memory_kind} memory · port {node.port}</span>
    </li>)}</ol>
    {coexistence.map(reason => <p className="coexistence-copy" key={reason.code}>{reason.detail}</p>)}
    <LibraryPlanReasons heading="Load blockers" reasons={reasons.blockers}/>
    <LibraryPlanReasons heading="Load warnings" reasons={reasons.warnings.filter(reason => !coexistence.includes(reason))}/>
  </div>;
}

export function StopPreview({plan}: {plan: LibraryStopPlan}) {
  const nodeName = useLibraryNodeName();
  return <div className="action-preview">
    <p>{plan.route_withdrawal ? "Published route will be withdrawn." : `Route remains ${plan.route_state}.`}</p>
    <ol className="action-node-plans">{plan.nodes.map(node => <li key={node.node_id}>
      <strong>Rank {node.rank} · {humanizeIdentifier(node.role)} · {humanizeIdentifier(node.state)} · {nodeName(node.node_id)}</strong><span>{formatBytes(node.active_memory_reservation_bytes)} active reservation</span><TechnicalDetails compact items={[{label: "Node ID", value: node.node_id}]}/>
    </li>)}</ol>
    <p>{formatBytes(plan.total_active_memory_reservation_bytes)} is actively reserved now.</p>
    <p className="authority-copy">Capacity remains reserved unless every rank stops successfully.</p>
    <LibraryPlanReasons heading="Stop blockers" reasons={plan.blockers}/>
    <LibraryPlanReasons heading="Stop warnings" reasons={plan.warnings}/>
  </div>;
}

export function UninstallPreview({plan}: {plan: LibraryUninstallPlan}) {
  const nodeName = useLibraryNodeName();
  return <div className="action-preview">
    <p>{plan.bytes_removed == null ? "Exact removable bytes are unknown." : `${formatBytes(plan.bytes_removed)} will be removed.`}</p>
    <ol className="action-node-plans">{plan.nodes.map(node => <li key={node.node_id}>
      <strong>Rank {node.rank} · {humanizeIdentifier(node.role)} · {humanizeIdentifier(node.state)} · {nodeName(node.node_id)}</strong><span>{node.installed_bytes == null ? "Installed bytes unknown" : `${formatBytes(node.installed_bytes)} installed`}</span><TechnicalDetails compact items={[{label: "Node ID", value: node.node_id}]}/>
    </li>)}</ol>
    {plan.active_runs.length > 0 && <section aria-label="Active runs"><h4>{plan.active_run_count} active run{plan.active_run_count === 1 ? "" : "s"}</h4><ul>{plan.active_runs.map((run, index) => <li key={run.run_id}><span>Run {index + 1} · {humanizeIdentifier(run.state)} · route {humanizeIdentifier(run.route_state)}</span><TechnicalDetails compact items={[{label: "Run ID", value: run.run_id}]}/></li>)}</ul></section>}
    {!plan.consequences.automatic_stop && <p className="authority-copy">Forge will not stop active runs automatically.</p>}
    {plan.consequences.catalog_retained && <p>The local catalog recipe is retained.</p>}
    {plan.consequences.reinstall_required && <p>Reinstall is required to restore removed content.</p>}
    <LibraryPlanReasons heading="Remove blockers" reasons={plan.blockers}/>
    <LibraryPlanReasons heading="Remove warnings" reasons={plan.warnings}/>
  </div>;
}
