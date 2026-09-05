import {useEffect, useMemo, useState} from "react";
import type {ControlApi, FleetProfile, FleetProfileInput, LibraryRecipeDetail} from "../api/types";
import {useLibraryNodeName} from "./library-node-names";

function profileInput(profile: FleetProfile): FleetProfileInput {
  return {
    name: profile.name,
    description: profile.description,
    installation_policy: profile.installation_policy,
    labels: profile.labels,
    favorite: profile.favorite,
    scope: {node_ids: [...new Set(profile.assignments.flatMap(assignment => assignment.nodes.map(node => node.node_id)))].sort()},
    assignments: profile.assignments.map(assignment => ({
      recipe_revision_id: assignment.recipe_revision_id,
      topology_name: assignment.topology_name,
      desired_state: assignment.desired_state,
      alias: assignment.alias,
      nodes: assignment.nodes,
    })),
  };
}

export function LibraryProfileComposer({api, detail, preferredNodeId}: {api: ControlApi; detail: LibraryRecipeDetail; preferredNodeId?: string}) {
  const nodeName = useLibraryNodeName();
  const profileApi = api;
  const [open, setOpen] = useState(false);
  const [profiles, setProfiles] = useState<FleetProfile[]>([]);
  const [target, setTarget] = useState("new");
  const [name, setName] = useState(`${detail.recipe.title} ready`);
  const [description, setDescription] = useState(`Keep ${detail.recipe.title} ready on its selected Spark group.`);
  const [desiredState, setDesiredState] = useState<"installed" | "running">("running");
  const [alias, setAlias] = useState(detail.recipe.slug);
  const [groupIndex, setGroupIndex] = useState(0);
  const [loadingProfiles, setLoadingProfiles] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState<FleetProfile>();
  const eligibleGroups = useMemo(
    () => detail.placement.flatMap(placement => placement.recommendations.filter(group => group.eligible))
      .sort((left, right) => Number(right.node_ids.includes(preferredNodeId ?? "")) - Number(left.node_ids.includes(preferredNodeId ?? ""))),
    [detail.placement, preferredNodeId],
  );
  const group = eligibleGroups[groupIndex];
  useEffect(() => {
    if (!open || profiles.length > 0 || loadingProfiles) return;
    const controller = new AbortController();
    setLoadingProfiles(true);
    profileApi.fleetProfiles(controller.signal).then(result => {
      setProfiles(result.profiles);
      setError("");
    }).catch(value => {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Saved Fleet Profiles are unavailable.");
    }).finally(() => {
      if (!controller.signal.aborted) setLoadingProfiles(false);
    });
    return () => controller.abort();
  }, [loadingProfiles, open, profileApi, profiles.length]);

  if (!detail.selected_revision || detail.selected_revision.lifecycle !== "resolved" || !detail.topology) return null;

  async function save() {
    if (!group || saving) return;
    setSaving(true);
    setError("");
    const assignment = {
      recipe_revision_id: detail.selected_revision!.id,
      topology_name: detail.topology!.name,
      desired_state: desiredState,
      alias: desiredState === "running" ? alias : null,
      nodes: group.nodes.map(node => ({
        node_id: node.node_id,
        rank: node.rank,
        role: node.role,
        endpoint_owner: node.endpoint_owner,
      })),
    } satisfies NonNullable<FleetProfileInput["assignments"]>[number];
    const scope = {node_ids: [...new Set(assignment.nodes.map(node => node.node_id))].sort()};
    try {
      const result = target === "new"
        ? await profileApi.createFleetProfile({
            name,
            description,
            installation_policy: "keep-cached",
            labels: {source: "library"},
            favorite: profiles.length === 0,
            scope,
            assignments: [assignment],
          })
        : await (async () => {
            const existing = profiles.find(profile => profile.id === target);
            if (!existing) throw new Error("Choose a saved Fleet Profile.");
            if (existing.assignments.some(item => item.recipe_revision_id === assignment.recipe_revision_id)) {
              throw new Error("This recipe revision is already part of the selected Fleet Profile.");
            }
            const input = profileInput(existing);
            input.scope = {node_ids: [...new Set([...(input.scope?.node_ids ?? []), ...scope.node_ids])].sort()};
            input.assignments = [...(input.assignments ?? []), assignment];
            return profileApi.updateFleetProfile(existing.id, input);
          })();
      setSaved(result);
    } catch (value) {
      setError(value instanceof Error ? value.message : "The recipe could not be added to a Fleet Profile.");
    } finally {
      setSaving(false);
    }
  }

  if (!open) return <button type="button" className="button secondary profile-composer-open" disabled={eligibleGroups.length === 0} title={eligibleGroups.length === 0 ? "No eligible complete Spark group is available for this recipe" : undefined} onClick={() => setOpen(true)}>Add to Fleet Profile</button>;

  if (saved) return <section className="profile-composer-success" aria-live="polite"><div><strong>{saved.name} is ready</strong><p>{detail.recipe.title} now has a saved {desiredState} placement across {group?.nodes.length ?? 0} Sparks.</p></div><a className="button" href="/fleet">Review in Fleet</a></section>;

  return <section className="library-profile-composer" aria-labelledby="profile-composer-title">
    <header><div><h4 id="profile-composer-title">Add recipe to a Fleet Profile</h4><p>Save this exact revision, topology, rank order, and Spark group as repeatable desired state.</p></div><button type="button" className="secondary-button" onClick={() => setOpen(false)}>Close</button></header>
    <div className="profile-composer-grid">
      <label><span>Destination</span><select value={target} disabled={loadingProfiles} onChange={event => setTarget(event.target.value)}><option value="new">New Fleet Profile</option>{profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name} · {profile.assignments.length} workloads</option>)}</select></label>
      {target === "new" && <><label><span>Profile name</span><input value={name} maxLength={80} onChange={event => setName(event.target.value)}/></label><label className="profile-composer-wide"><span>Purpose</span><textarea value={description} maxLength={512} rows={2} onChange={event => setDescription(event.target.value)}/></label></>}
      <label><span>Desired state</span><select value={desiredState} onChange={event => setDesiredState(event.target.value as "installed" | "running")}><option value="running">Running with endpoint</option><option value="installed">Installed and ready to load</option></select></label>
      {desiredState === "running" && <label><span>Endpoint alias</span><input value={alias} maxLength={64} onChange={event => setAlias(event.target.value)}/></label>}
      <label className="profile-composer-wide"><span>Spark group</span><select value={groupIndex} onChange={event => setGroupIndex(Number(event.target.value))}>{eligibleGroups.map((candidate, index) => <option key={`${candidate.topology_name}:${candidate.node_ids.join(":")}`} value={index}>{candidate.nodes.map(node => nodeName(node.node_id)).join(" + ")} · {candidate.load_state === "loaded" ? "running" : candidate.install_state === "complete" ? "installed" : "ready to place"}</option>)}</select></label>
    </div>
    {group && <ol className="profile-rank-preview" aria-label="Saved Spark rank order">{group.nodes.map(node => <li key={node.node_id}><span>Rank {node.rank}</span><strong>{nodeName(node.node_id)}</strong><small>{node.role}{node.endpoint_owner ? " · endpoint owner" : ""}</small></li>)}</ol>}
    {error && <p className="dialog-error" role="alert">{error}</p>}
    <footer><span>{group?.nodes.length ?? 0} {group?.nodes.length === 1 ? "Spark" : "Sparks"} · immutable revision {detail.selected_revision.revision_number}</span><button type="button" disabled={!group || saving || (target === "new" && !name.trim()) || (desiredState === "running" && !alias.trim())} onClick={() => void save()}>{saving ? "Saving profile…" : target === "new" ? "Create Fleet Profile" : "Add workload"}</button></footer>
  </section>;
}
