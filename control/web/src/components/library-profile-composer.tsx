import {useEffect, useMemo, useState} from "react";
import type {ControlApi, FleetProfile, FleetProfileInput, LibraryRecipeDetail} from "../api/types";
import {useLibraryNodeName} from "./library-node-names";

function nextProfileNumber(profiles: FleetProfile[]): number {
  return Math.max(0, ...profiles.map(profile => profile.number)) + 1;
}

function inputFromProfile(profile: FleetProfile): FleetProfileInput {
  const input: FleetProfileInput = {
    name: profile.name,
    description: profile.description,
    installation_policy: profile.installation_policy,
    labels: profile.labels,
    favorite: profile.favorite,
    assignments: profile.assignments.map(assignment => ({
      recipe_selector: assignment.recipe_selector,
      spark_ids: [...assignment.spark_ids].sort(),
      assignment_name: assignment.selector,
      model_variant: typeof assignment.model?.variant === "string" ? assignment.model.variant : undefined,
      desired_state: assignment.observed_state.toLocaleLowerCase().includes("installed") ? "installed" : "running",
    })),
  };
  if (profile.revision > 0) input.expected_revision = profile.revision;
  return input;
}

export function LibraryProfileComposer({api, detail, preferredNodeId}: {api: ControlApi; detail: LibraryRecipeDetail; preferredNodeId?: string}) {
  const nodeName = useLibraryNodeName();
  const [open, setOpen] = useState(false);
  const [profiles, setProfiles] = useState<FleetProfile[]>([]);
  const [target, setTarget] = useState("new");
  const [name, setName] = useState(`${detail.recipe.title} ready`);
  const [description, setDescription] = useState(`Keep ${detail.recipe.title} ready on its selected Spark group.`);
  const [desiredState, setDesiredState] = useState<"installed" | "running">("running");
  const [assignmentName, setAssignmentName] = useState(detail.recipe.slug);
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
  const modelVariant = detail.model_documents[0]?.model_document.identity.variant;

  useEffect(() => {
    if (!open || profiles.length > 0 || loadingProfiles) return;
    const controller = new AbortController();
    setLoadingProfiles(true);
    void api.profiles(controller.signal).then(result => {
      setProfiles(result.profiles);
      setError("");
    }).catch(value => {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Saved profiles are unavailable.");
    }).finally(() => { if (!controller.signal.aborted) setLoadingProfiles(false); });
    return () => controller.abort();
  }, [api, loadingProfiles, open, profiles.length]);

  const topology = detail.definition.topology;
  if (!topology) return null;

  async function save() {
    if (!group || saving || !assignmentName.trim()) return;
    setSaving(true); setError("");
    const assignment = {
      recipe_selector: detail.recipe.slug,
      spark_ids: group.nodes.map(node => node.node_id).sort(),
      assignment_name: assignmentName.trim(),
      model_variant: modelVariant,
      desired_state: desiredState,
    } satisfies NonNullable<FleetProfileInput["assignments"]>[number];
    try {
      const existing = target === "new" ? undefined : profiles.find(profile => profile.number === Number(target));
      if (target !== "new" && !existing) throw new Error("Choose a saved profile.");
      if (existing?.assignments.some(item => item.recipe_selector === assignment.recipe_selector && item.spark_ids.join(",") === assignment.spark_ids.join(","))) {
        throw new Error("This recipe and Spark group are already part of the selected profile.");
      }
      const input = existing ? inputFromProfile(existing) : {
        name,
        description,
        installation_policy: "keep-cached" as const,
        labels: {source: "library"},
        favorite: profiles.length === 0,
        assignments: [],
      } satisfies FleetProfileInput;
      input.assignments = [...(input.assignments ?? []), assignment];
      const result = await api.autosaveProfile(existing?.number ?? nextProfileNumber(profiles), input);
      setSaved(result);
    } catch (value) {
      setError(value instanceof Error ? value.message : "The recipe could not be added to a profile.");
    } finally { setSaving(false); }
  }

  if (!open) return <button type="button" className="button secondary profile-composer-open" disabled={eligibleGroups.length === 0} title={eligibleGroups.length === 0 ? "No eligible Spark group is available for this recipe" : undefined} onClick={() => setOpen(true)}>Add to Fleet Profile</button>;
  if (saved) return <section className="profile-composer-success" aria-live="polite"><div><strong>{saved.name} is ready</strong><p>{detail.recipe.title} is saved on profile {saved.number} across {group?.nodes.length ?? 0} Sparks.</p></div><a className="button" href="/fleet">Review in Fleet</a></section>;

  return <section className="library-profile-composer" aria-labelledby="profile-composer-title">
    <header><div><h4 id="profile-composer-title">Add recipe to a Fleet Profile</h4><p>Save a recipe choice and Spark group. The latest compatible cached revision is selected when the profile loads.</p></div><button type="button" className="secondary-button" onClick={() => setOpen(false)}>Close</button></header>
    <div className="profile-composer-grid">
      <label><span>Destination</span><select value={target} disabled={loadingProfiles} onChange={event => setTarget(event.target.value)}><option value="new">New Fleet Profile</option>{profiles.map(profile => <option key={profile.number} value={profile.number}>Profile {profile.number} · {profile.name} · {profile.assignments.length} workloads</option>)}</select></label>
      {target === "new" && <><label><span>Profile name</span><input value={name} maxLength={120} onChange={event => setName(event.target.value)}/></label><label className="profile-composer-wide"><span>Purpose</span><textarea value={description} maxLength={1000} rows={2} onChange={event => setDescription(event.target.value)}/></label></>}
      <label><span>Desired state</span><select value={desiredState} onChange={event => setDesiredState(event.target.value as "installed" | "running")}><option value="running">Running</option><option value="installed">Installed</option></select></label>
      <label><span>Assignment name</span><input value={assignmentName} maxLength={128} onChange={event => setAssignmentName(event.target.value)}/></label>
      <label className="profile-composer-wide"><span>Spark group</span><select value={groupIndex} onChange={event => setGroupIndex(Number(event.target.value))}>{eligibleGroups.map((candidate, index) => <option key={`${candidate.topology_name}:${candidate.node_ids.join(":")}`} value={index}>{candidate.nodes.map(node => nodeName(node.node_id)).join(" + ")} · {candidate.load_state === "loaded" ? "running" : candidate.install_state === "complete" ? "installed" : "ready"}</option>)}</select></label>
    </div>
    {group && <ol className="profile-rank-preview" aria-label="Selected Sparks">{group.nodes.map(node => <li key={node.node_id}><span>Spark</span><strong>{nodeName(node.node_id)}</strong><small>{node.node_id}</small></li>)}</ol>}
    {error && <p className="dialog-error" role="alert">{error}</p>}
    <footer><span>{group?.nodes.length ?? 0} {group?.nodes.length === 1 ? "Spark" : "Sparks"} · selector {detail.recipe.slug}</span><button type="button" disabled={!group || saving || !assignmentName.trim() || (target === "new" && !name.trim())} onClick={() => void save()}>{saving ? "Saving profile…" : target === "new" ? "Create Fleet Profile" : "Add workload"}</button></footer>
  </section>;
}
