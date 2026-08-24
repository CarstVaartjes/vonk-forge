import {useState} from "react";
import type {MouseEvent} from "react";
import type {LibraryRecipeDetail, LibrarySnapshot} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {actionName} from "./library-action-types";
import type {LibraryActionTarget, LibraryPlacementGroup} from "./library-action-types";
import {LibraryReasons} from "./library-reasons";
import {useLibraryNodeName} from "./library-node-names";
import {humanizeIdentifier, TechnicalDetails} from "./library-technical-details";

type Placement = LibraryRecipeDetail["placement"][number];
type Group = Placement["recommendations"][number];
type FreshnessPolicy = LibrarySnapshot["freshness_policy"];

function title(value: string): string {
  return value.replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase());
}

function inventoryFreshness(age: number, policy: FreshnessPolicy): string {
  return age < policy.inventory_fresh_seconds ? "fresh" : "stale";
}

function telemetryFreshness(age: number, policy: FreshnessPolicy): string {
  if (age <= policy.telemetry_live_seconds) return "live";
  if (age <= policy.telemetry_delayed_seconds) return "delayed";
  return "stale";
}

function groupKey(topologyName: string, group: Group): string {
  return `${topologyName}:${group.node_ids.join(":")}`;
}

function groupName(group: Group, nodeName: (nodeId: string) => string): string {
  return group.node_ids.map(nodeName).join(" and ");
}

function CapacityBar({after, label, required, reserved}: {after: number; label: string; required: number; reserved: number}) {
  const parts = [Math.max(0, required), Math.max(0, reserved), Math.max(0, after)];
  const total = Math.max(1, parts.reduce((sum, value) => sum + value, 0));
  return <div className="placement-capacity" role="img" aria-label={`${label}: ${formatBytes(required)} required, ${formatBytes(reserved)} already reserved, ${formatBytes(after)} free after`}>
    <div className="placement-capacity-heading"><strong>{label}</strong><span>{formatBytes(after)} free after</span></div>
    <div className="placement-capacity-track" aria-hidden="true">
      <span className="placement-capacity-required" style={{width: `${parts[0] / total * 100}%`}}/>
      <span className="placement-capacity-reserved" style={{width: `${parts[1] / total * 100}%`}}/>
      <span className="placement-capacity-after" style={{width: `${parts[2] / total * 100}%`}}/>
    </div>
    <div className="placement-capacity-legend" aria-hidden="true"><span>Required {formatBytes(required)}</span><span>Reserved {formatBytes(reserved)}</span><span>After {formatBytes(after)}</span></div>
  </div>;
}

function GroupEvidence({group, policy, selected}: {group: Group; policy: FreshnessPolicy; selected: boolean}) {
  const nodeName = useLibraryNodeName();
  return <div className={`placement-evidence${selected ? " is-selected" : ""}`}>
    <p className="placement-state">Complete group · {title(group.install_state === "complete" ? "installed" : group.install_state)} · {title(group.load_state === "not_loaded" ? "not loaded" : group.load_state)}</p>
    <ol className="placement-nodes">{group.nodes.map(node => <li key={`${node.node_id}:${node.rank}`}>
      <div className="placement-node-heading"><strong>{nodeName(node.node_id)}</strong><span>Rank {node.rank} · {humanizeIdentifier(node.role)}</span></div>
      <TechnicalDetails compact items={[{label: "Node ID", value: node.node_id}, {label: "Fabric address", value: node.fabric_address ?? ""}]}/>
      <dl>
        <div><dt>Admission inventory</dt><dd>Inventory {inventoryFreshness(node.inventory_age_seconds, policy)} · {node.inventory_age_seconds}s</dd></div>
        <div><dt>Live telemetry</dt><dd>Telemetry {telemetryFreshness(node.telemetry_age_seconds, policy)} · {node.telemetry_age_seconds}s</dd></div>
        <div><dt>Disk</dt><dd>{formatBytes(node.disk_required_bytes)} required · {formatBytes(node.disk_reserved_bytes)} disk reserved · {formatBytes(node.disk_free_after_bytes)} after</dd></div>
        <div><dt>{title(node.memory_kind)} memory</dt><dd>{formatBytes(node.memory_required_bytes)} required · {formatBytes(node.memory_reserved_bytes)} memory reserved · {formatBytes(node.memory_free_after_bytes)} after</dd></div>
        <div><dt>Exact artifact reuse</dt><dd>{formatBytes(node.artifact_reuse_bytes)}</dd></div>
        <div><dt>Fabric</dt><dd>{node.fabric_address ?? "Not reported"}{node.fabric_bandwidth_mbps == null ? "" : ` · ${node.fabric_bandwidth_mbps.toLocaleString()} Mbps`}</dd></div>
      </dl>
      <div className="placement-capacity-bars">
        <CapacityBar label="Disk capacity" required={node.disk_required_bytes} reserved={node.disk_reserved_bytes} after={node.disk_free_after_bytes}/>
        <CapacityBar label={`${title(node.memory_kind)} memory`} required={node.memory_required_bytes} reserved={node.memory_reserved_bytes} after={node.memory_free_after_bytes}/>
      </div>
    </li>)}</ol>
    <LibraryReasons reasons={group.reasons}/>
  </div>;
}

function RejectedEvidence({group, policy}: {group: Group; policy: FreshnessPolicy}) {
  const nodeName = useLibraryNodeName();
  return <article className="placement-rejected">
    <h6>{group.node_ids.map(nodeName).join(" + ")}</h6>
    <GroupEvidence group={group} policy={policy} selected={false}/>
  </article>;
}

export function LibraryPlacement({actionsDisabled = false, detail, onReview, policy}: {
  actionsDisabled?: boolean;
  detail: LibraryRecipeDetail;
  onReview?(target: LibraryActionTarget, trigger: HTMLButtonElement, evidence?: LibraryPlacementGroup): void;
  policy: FreshnessPolicy;
}) {
  const [selectedGroup, setSelectedGroup] = useState("");
  const nodeName = useLibraryNodeName();
  if (detail.placement.length === 0) return <section className="library-section"><h4>Placement</h4><p className="library-placeholder">No valid complete topology placement is available.</p></section>;
  return <section className="library-section placement-section" aria-label="Complete placement groups">
    <div className="section-heading"><div><p className="fleet-kicker">One atomic group</p><h4>Complete placement groups</h4></div><small>Select all ranks together</small></div>
    {detail.placement.map(topology => <section key={topology.topology_name} className="placement-profile" aria-label={`${topology.topology_name} placement`}>
      <div className="placement-profile-heading"><h5>{humanizeIdentifier(topology.topology_name)}</h5><span>{topology.node_count} Sparks · {topology.recommendations.length} available</span></div>
      {!topology.search_complete && <div className="bounded-search-notice" role="note">
        <strong>Bounded search is incomplete</strong>
        <p>{topology.reasons.find(reason => reason.code.includes("truncated"))?.detail ?? `The bounded search evaluated ${topology.evaluated_group_count} complete groups.`} This is bounded advisory evidence, not a globally optimal placement.</p>
      </div>}
      <LibraryReasons reasons={topology.reasons}/>
      <div className="placement-groups">{topology.recommendations.filter(group => group.eligible).map(group => {
        const key = groupKey(topology.topology_name, group);
        const selected = selectedGroup === key;
        return <article key={key} className={`placement-group${selected ? " is-selected" : ""}`}>
          <button type="button" className="placement-selector" aria-pressed={selected} onClick={() => setSelectedGroup(key)} aria-label={`Select complete group ${groupName(group, nodeName)}`}>
            <span>{group.node_ids.map(nodeName).join(" + ")}</span><small>{group.nodes.length} ranks · eligible complete group</small>
          </button>
          {selected && group.preview_targets.length > 0 && <div className="placement-actions" role="region" aria-label="Selected group actions">
            {group.preview_targets.map((target, index) => <button
              type="button"
              className="button"
              disabled={actionsDisabled}
              key={`${target.kind}:${index}`}
              onClick={(event: MouseEvent<HTMLButtonElement>) => onReview?.(target, event.currentTarget, group)}
            >Review {actionName(target)}</button>)}
          </div>}
          <GroupEvidence group={group} policy={policy} selected={selected}/>
        </article>;
      })}</div>
      {topology.recommendations.every(group => !group.eligible) && <p className="library-placeholder">No eligible Spark group is available for this topology. Review unavailable placement evidence below.</p>}
      {(topology.rejected_groups.length > 0 || topology.rejected_nodes.length > 0 || topology.recommendations.some(group => !group.eligible)) && <details className="placement-rejections">
        <summary>Unavailable placement evidence</summary>
        {topology.recommendations.filter(group => !group.eligible).map(group => <RejectedEvidence key={groupKey(topology.topology_name, group)} group={group} policy={policy}/>) }
        {topology.rejected_groups.map(group => <RejectedEvidence key={groupKey(topology.topology_name, group)} group={group} policy={policy}/>) }
        {topology.rejected_nodes.map(node => <div key={node.node_id} className="rejected-node"><strong>{nodeName(node.node_id)}</strong><TechnicalDetails compact items={[{label: "Node ID", value: node.node_id}]}/><LibraryReasons reasons={node.reasons}/></div>)}
        {topology.rejected_evidence_truncated && <p className="bounded-copy">Rejected evidence is also truncated at the published server limit.</p>}
      </details>}
    </section>)}</section>;
}
