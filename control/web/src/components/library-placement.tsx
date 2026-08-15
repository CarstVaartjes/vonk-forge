import {useState} from "react";
import type {MouseEvent} from "react";
import type {LibraryRecipeDetail, LibrarySnapshot} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {actionName} from "./library-action-types";
import type {LibraryActionTarget} from "./library-action-types";
import {LibraryReasons} from "./library-reasons";

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

function groupKey(profileName: string, group: Group): string {
  return `${profileName}:${group.node_ids.join(":")}`;
}

function GroupEvidence({group, policy, selected}: {group: Group; policy: FreshnessPolicy; selected: boolean}) {
  return <div className={`placement-evidence${selected ? " is-selected" : ""}`}>
    <p className="placement-state">Complete group · {title(group.install_state === "complete" ? "installed" : group.install_state)} · {title(group.load_state === "not_loaded" ? "not loaded" : group.load_state)}</p>
    <ol className="placement-nodes">{group.nodes.map(node => <li key={`${node.node_id}:${node.rank}`}>
      <div className="placement-node-heading"><strong>{node.node_id}</strong><span>Rank {node.rank} · {node.role}</span></div>
      <dl>
        <div><dt>Admission inventory</dt><dd>Inventory {inventoryFreshness(node.inventory_age_seconds, policy)} · {node.inventory_age_seconds}s</dd></div>
        <div><dt>Live telemetry</dt><dd>Telemetry {telemetryFreshness(node.telemetry_age_seconds, policy)} · {node.telemetry_age_seconds}s</dd></div>
        <div><dt>Disk</dt><dd>{formatBytes(node.disk_required_bytes)} required · {formatBytes(node.disk_reserved_bytes)} disk reserved · {formatBytes(node.disk_free_after_bytes)} after</dd></div>
        <div><dt>{title(node.memory_kind)} memory</dt><dd>{formatBytes(node.memory_required_bytes)} required · {formatBytes(node.memory_reserved_bytes)} memory reserved · {formatBytes(node.memory_free_after_bytes)} after</dd></div>
        <div><dt>Exact artifact reuse</dt><dd>{formatBytes(node.artifact_reuse_bytes)}</dd></div>
        <div><dt>Fabric</dt><dd>{node.fabric_address ?? "Not reported"}{node.fabric_bandwidth_mbps == null ? "" : ` · ${node.fabric_bandwidth_mbps.toLocaleString()} Mbps`}</dd></div>
      </dl>
    </li>)}</ol>
    <LibraryReasons reasons={group.reasons}/>
  </div>;
}

function RejectedEvidence({group, policy}: {group: Group; policy: FreshnessPolicy}) {
  return <article className="placement-rejected">
    <h6>{group.node_ids.join(" + ")}</h6>
    <GroupEvidence group={group} policy={policy} selected={false}/>
  </article>;
}

export function LibraryPlacement({actionsDisabled = false, detail, onReview, policy}: {
  actionsDisabled?: boolean;
  detail: LibraryRecipeDetail;
  onReview?(target: LibraryActionTarget, trigger: HTMLButtonElement): void;
  policy: FreshnessPolicy;
}) {
  const [selectedGroup, setSelectedGroup] = useState("");
  if (detail.placement.length === 0) return <section className="library-section"><h4>Placement</h4><p className="library-placeholder">No valid complete placement profile is available.</p></section>;
  return <section className="library-section placement-section" aria-label="Complete placement groups">
    <div className="section-heading"><div><p className="fleet-kicker">One atomic group</p><h4>Complete placement groups</h4></div><small>Select all ranks together</small></div>
    {detail.placement.map(profile => <section key={profile.profile_name} className="placement-profile" aria-label={`${profile.profile_name} placement`}>
      <div className="placement-profile-heading"><h5>{profile.profile_name}</h5><span>{profile.node_count} nodes · {profile.recommendations.length} available</span></div>
      {!profile.search_complete && <div className="bounded-search-notice" role="note">
        <strong>Bounded search is incomplete</strong>
        <p>{profile.reasons.find(reason => reason.code.includes("truncated"))?.detail ?? `The bounded search evaluated ${profile.evaluated_group_count} complete groups.`} This is bounded advisory evidence, not a globally optimal placement.</p>
      </div>}
      <LibraryReasons reasons={profile.reasons}/>
      <div className="placement-groups">{profile.recommendations.map(group => {
        const key = groupKey(profile.profile_name, group);
        const selected = selectedGroup === key;
        return <article key={key} className={`placement-group${selected ? " is-selected" : ""}`}>
          <button type="button" className="placement-selector" aria-pressed={selected} onClick={() => setSelectedGroup(key)} aria-label={`Select complete group ${group.node_ids.join(" and ")}`}>
            <span>{group.node_ids.join(" + ")}</span><small>{group.nodes.length} ranks · complete group</small>
          </button>
          <GroupEvidence group={group} policy={policy} selected={selected}/>
          {selected && group.preview_targets.length > 0 && <div className="placement-actions" aria-label="Selected group actions">
            {group.preview_targets.map((target, index) => <button
              type="button"
              className="button"
              disabled={actionsDisabled}
              key={`${target.kind}:${index}`}
              onClick={(event: MouseEvent<HTMLButtonElement>) => onReview?.(target, event.currentTarget)}
            >Review {actionName(target)}</button>)}
          </div>}
        </article>;
      })}</div>
      {(profile.rejected_groups.length > 0 || profile.rejected_nodes.length > 0) && <details className="placement-rejections" open>
        <summary>Unavailable placement evidence</summary>
        {profile.rejected_groups.map(group => <RejectedEvidence key={groupKey(profile.profile_name, group)} group={group} policy={policy}/>)}
        {profile.rejected_nodes.map(node => <div key={node.node_id} className="rejected-node"><strong>{node.node_id}</strong><LibraryReasons reasons={node.reasons}/></div>)}
        {profile.rejected_evidence_truncated && <p className="bounded-copy">Rejected evidence is also truncated at the published server limit.</p>}
      </details>}
    </section>)}
  </section>;
}
