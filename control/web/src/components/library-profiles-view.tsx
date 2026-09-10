import {useEffect, useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {ControlApi, FleetProfile, FleetProfileApplicationView, FleetProfileInput, FleetProfilePreview, VisualFleetSnapshot} from "../api/types";
import {nodeDisplayName} from "../lib/fleet";
import type {LibraryRecipeRecord} from "./library-workcell";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
type AssignmentInput = NonNullable<FleetProfileInput["assignments"]>[number];
type AssignmentDraft = {
  key: string;
  recipeSelector: string;
  assignmentName: string;
  modelVariant: string;
  desiredState: AssignmentInput["desired_state"];
  sparkIds: string[];
};
type ProfileDraft = {
  number?: number;
  revision?: number;
  name: string;
  description: string;
  favorite: boolean;
  installationPolicy: FleetProfileInput["installation_policy"];
  labels: FleetProfileInput["labels"];
  assignments: AssignmentDraft[];
};
type FleetEntry = {id: string; name: string; state: string};

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled"]);

function stringField(value: unknown, key: string): string {
  if (!value || typeof value !== "object") return "";
  const field = (value as Record<string, unknown>)[key];
  return typeof field === "string" ? field : "";
}

function profileAssignments(profile: FleetProfile): AssignmentDraft[] {
  return profile.assignments.map((assignment, index) => ({
    key: `${assignment.selector}-${index}`,
    recipeSelector: assignment.recipe_selector,
    assignmentName: assignment.selector,
    modelVariant: stringField(assignment.model, "variant"),
    desiredState: assignment.observed_state.toLocaleLowerCase().includes("installed") ? "installed" : "running",
    sparkIds: [...assignment.spark_ids].sort(),
  }));
}

function draftFromProfile(profile: FleetProfile): ProfileDraft {
  return {
    number: profile.number,
    revision: profile.revision,
    name: profile.name,
    description: profile.description,
    favorite: profile.favorite,
    installationPolicy: profile.installation_policy,
    labels: profile.labels,
    assignments: profileAssignments(profile),
  };
}

function blankDraft(): ProfileDraft {
  return {name: "New fleet profile", description: "A complete desired setup for the fleet.", favorite: false, installationPolicy: "keep-cached", labels: {}, assignments: []};
}

function fleetEntries(profile: FleetProfile | undefined, fleet: VisualFleetSnapshot | undefined): FleetEntry[] {
  if (profile) return (profile.fleet ?? []).flatMap(item => {
    const id = stringField(item, "selector");
    if (!id) return [];
    return [{id, name: stringField(item, "display_name") || id, state: stringField(item, "state") || "Unknown"}];
  });
  return fleet?.nodes.map(node => ({id: node.id, name: nodeDisplayName(node), state: "Observed"})) ?? [];
}

function inputFromDraft(draft: ProfileDraft): FleetProfileInput {
  const input: FleetProfileInput = {
    name: draft.name.trim(),
    description: draft.description.trim(),
    installation_policy: draft.installationPolicy,
    labels: draft.labels,
    favorite: draft.favorite,
    assignments: draft.assignments.map(assignment => ({
      recipe_selector: assignment.recipeSelector.trim(),
      spark_ids: [...new Set(assignment.sparkIds)].sort(),
      assignment_name: assignment.assignmentName.trim() || undefined,
      model_variant: assignment.modelVariant.trim() || undefined,
      desired_state: assignment.desiredState,
    })),
  };
  if (draft.revision !== undefined) input.expected_revision = draft.revision;
  return input;
}

function applicationProgressRecord(application: FleetProfileApplicationView): Record<string, unknown> {
  const progress = application.progress as Record<string, unknown>;
  const child = progress.child_progress;
  return child && typeof child === "object" ? child as Record<string, unknown> : progress;
}

function formatBytes(value: unknown): string | undefined {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return undefined;
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let unit = "B";
  for (const next of units) { amount /= 1024; unit = next; if (amount < 1024) break; }
  return `${amount.toFixed(amount >= 10 ? 0 : 1)} ${unit}`;
}

function ProfileProgress({application}: {application: FleetProfileApplicationView}) {
  const progress = applicationProgressRecord(application);
  const completed = formatBytes(progress.bytes);
  const total = formatBytes(progress.total_bytes);
  const value = typeof progress.bytes === "number" && typeof progress.total_bytes === "number" && progress.total_bytes > 0 ? Math.min(100, Math.max(0, progress.bytes / progress.total_bytes * 100)) : undefined;
  const phase = typeof progress.phase === "string" ? progress.phase.replaceAll("-", " ") : "Profile load";
  const nodeIds = Array.isArray(progress.node_ids) ? progress.node_ids.filter((id): id is string => typeof id === "string") : [];
  return <section className={`library-profile-application state-${application.state}`} aria-live="polite" aria-label="Profile load progress">
    <div className="library-profile-application-heading"><div><strong>{phase}</strong><span>{application.state.replaceAll("-", " ")}</span></div>{completed && <span>{completed}{total ? ` of ${total}` : ""}</span>}</div>
    <div className={`library-profile-application-progress${value === undefined ? " is-indeterminate" : ""}`} role="progressbar" aria-label="Profile load progress" aria-valuemin={0} aria-valuemax={100} {...(value === undefined ? {"aria-valuetext": "Progress total unavailable"} : {"aria-valuenow": value})}><span style={value === undefined ? undefined : {transform: `scaleX(${value / 100})`}}/></div>
    {nodeIds.length > 0 && <ul className="library-profile-application-members" aria-label="Profile load targets">{nodeIds.map(nodeId => <li key={nodeId}><span>{nodeId}</span><small>Participating</small></li>)}</ul>}
    {application.status_reason && <p>{application.status_reason}</p>}
  </section>;
}

export function LibraryProfilesView({api, entries, fleet, initialCreate = false, onBusyChange, onNavigate}: {api: ControlApi; entries: LibraryRecipeRecord[]; fleet?: VisualFleetSnapshot; initialCreate?: boolean; onBusyChange?(busy: boolean): void; onNavigate: Navigate}) {
  const [profiles, setProfiles] = useState<FleetProfile[]>([]);
  const [selectedNumber, setSelectedNumber] = useState<number>();
  const [draft, setDraft] = useState<ProfileDraft>();
  const [editing, setEditing] = useState(initialCreate);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadingProfile, setLoadingProfile] = useState(false);
  const [preview, setPreview] = useState<FleetProfilePreview>();
  const [application, setApplication] = useState<FleetProfileApplicationView>();
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const selectedProfile = profiles.find(profile => profile.number === selectedNumber);
  const availableFleet = useMemo(() => fleetEntries(selectedProfile, fleet), [fleet, selectedProfile]);
  const matchingEntries = useMemo(() => entries.filter(entry => entry.recipe), [entries]);
  const draftValid = Boolean(draft?.name.trim()) && (draft?.assignments.every(assignment => assignment.recipeSelector.trim() && assignment.sparkIds.length > 0) ?? false);
  const applicationRunning = Boolean(application && !TERMINAL_STATES.has(application.state));

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void api.profiles(controller.signal).then(result => {
      if (controller.signal.aborted) return;
      setProfiles([...result.profiles].sort((left, right) => left.number - right.number));
      setSelectedNumber(current => current && result.profiles.some(profile => profile.number === current) ? current : result.profiles[0]?.number);
      if (!initialCreate && result.profiles[0] && !draft) { setDraft(draftFromProfile(result.profiles[0])); setEditing(false); }
      setError("");
    }).catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Saved profiles are unavailable."); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, initialCreate]);

  useEffect(() => {
    if (selectedNumber === undefined || editing) { setPreview(undefined); return; }
    const controller = new AbortController();
    void api.previewProfile(selectedNumber, controller.signal).then(result => { if (!controller.signal.aborted) setPreview(result); }).catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "The profile preview is unavailable."); });
    return () => controller.abort();
  }, [api, editing, selectedNumber]);

  useEffect(() => {
    if (selectedNumber === undefined || !application || TERMINAL_STATES.has(application.state)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => { void api.profileProgress(selectedNumber, controller.signal).then(next => { if (!controller.signal.aborted) setApplication(next); }).catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Profile load progress is unavailable."); }); }, 1_000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [api, application, selectedNumber]);

  useEffect(() => { onBusyChange?.(saving || loadingProfile || applicationRunning); return () => onBusyChange?.(false); }, [applicationRunning, loadingProfile, onBusyChange, saving]);

  function selectProfile(profile: FleetProfile) {
    setSelectedNumber(profile.number); setDraft(draftFromProfile(profile)); setEditing(false); setPreview(undefined); setApplication(undefined); setNotice("");
  }

  function startNew() { setSelectedNumber(undefined); setDraft(blankDraft()); setEditing(true); setPreview(undefined); setApplication(undefined); setNotice("Draft profile created. Saving does not change the running fleet."); }
  function updateDraft(next: Partial<ProfileDraft>) { setDraft(current => current ? {...current, ...next} : current); setNotice(""); }
  function updateAssignment(key: string, next: Partial<AssignmentDraft>) { if (draft) updateDraft({assignments: draft.assignments.map(item => item.key === key ? {...item, ...next} : item)}); }
  function toggleSpark(key: string, sparkId: string) {
    const assignment = draft?.assignments.find(item => item.key === key);
    if (!assignment) return;
    const sparkIds = assignment.sparkIds.includes(sparkId) ? assignment.sparkIds.filter(id => id !== sparkId) : [...assignment.sparkIds, sparkId];
    updateAssignment(key, {sparkIds});
  }
  function addRecipe() {
    const record = matchingEntries[0];
    if (!draft || !record?.recipe) return;
    const count = record.recipe.recipe_document.topology.node_count;
    const sparkIds = availableFleet.slice(0, count).map(item => item.id);
    const assignment: AssignmentDraft = {key: `assignment-${crypto.randomUUID()}`, recipeSelector: record.recipe.slug, assignmentName: record.recipe.slug, modelVariant: record.modelDocument?.identity.variant ?? "", desiredState: "running", sparkIds};
    updateDraft({assignments: [...draft.assignments, assignment]});
  }

  async function saveDraft() {
    if (!draft || !draftValid || saving) return;
    setSaving(true); setError("");
    try {
      const number = draft.number ?? Math.max(0, ...profiles.map(profile => profile.number)) + 1;
      const result = await api.autosaveProfile(number, inputFromDraft(draft));
      setProfiles(current => [...current.filter(profile => profile.number !== result.number), result].sort((left, right) => left.number - right.number));
      setSelectedNumber(result.number); setDraft(draftFromProfile(result)); setEditing(false); setPreview(undefined); setNotice(`Profile ${result.number} · ${result.name} saved. Current runs remain unchanged until load.`);
    } catch (value) { setError(value instanceof Error ? value.message : "The profile could not be saved."); }
    finally { setSaving(false); }
  }

  async function load() {
    if (selectedNumber === undefined || !preview?.allowed || loadingProfile) return;
    setLoadingProfile(true); setError("");
    try { setApplication(await api.loadProfile(selectedNumber, {dry_run: false, request_key: crypto.randomUUID()})); }
    catch (value) { setError(value instanceof Error ? value.message : "The profile could not be loaded."); }
    finally { setLoadingProfile(false); }
  }

  const names = Object.fromEntries(availableFleet.map(item => [item.id, item.name]));
  return <section className="library-profile-view" aria-labelledby="library-profiles-heading">
    <header className="library-subview-heading"><div><h2 id="library-profiles-heading">Profiles</h2><p>Save recipe choices for the whole fleet, then load the selected numbered profile when ready. The latest compatible cached recipes are resolved on load.</p></div><div className="library-profile-header-actions"><button type="button" className="button secondary" onClick={startNew}>Create profile</button><a className="button secondary" href="/library?view=models" onClick={event => onNavigate(event, "/library?view=models")}>Choose a model</a></div></header>
    {error && <p className="library-profile-plain-error" role="alert">{error}</p>}
    {loading && <p className="library-cache-state" role="status">Loading saved profiles…</p>}
    {notice && <div className="library-profile-notice" role="status">{notice}</div>}
    {selectedProfile && !editing && <section className="library-profile-status state-read" aria-live="polite"><div className="library-profile-status-summary"><strong>Profile {selectedProfile.number} · {selectedProfile.name}</strong><span>{selectedProfile.status.replaceAll("-", " ")} · revision {selectedProfile.revision}</span></div><span className="library-profile-status-scope">{(selectedProfile.fleet ?? []).length} Sparks · loaded revision {selectedProfile.loaded_revision ?? "none"}</span>{(selectedProfile.warnings ?? []).length > 0 && <ul>{(selectedProfile.warnings ?? []).map(warning => <li key={warning}>{warning}</li>)}</ul>}<button type="button" className="button secondary" onClick={() => setEditing(true)}>Edit profile</button></section>}
    {application && <ProfileProgress application={application}/>}
    <div className="library-profile-layout">
      <aside className="library-profile-list" aria-label="Saved profiles"><div className="library-profile-list-heading"><strong>Saved profiles</strong><span>{profiles.length}</span></div>{profiles.map(profile => <button key={profile.number} type="button" className={profile.number === selectedNumber ? "is-selected" : undefined} aria-pressed={profile.number === selectedNumber} onClick={() => selectProfile(profile)}><span>Profile {profile.number} · {profile.name}</span><small>{profile.assignments.length} assignment{profile.assignments.length === 1 ? "" : "s"} · {profile.status.replaceAll("-", " ")}</small></button>)}{profiles.length === 0 && <div className="library-profile-list-empty"><strong>No saved profiles</strong><p>Create a numbered profile to describe the desired fleet setup.</p></div>}<button type="button" className="library-profile-create-link" onClick={startNew}>+ New profile</button></aside>
      {draft && editing && <div className="library-profile-editor"><header className="library-profile-editor-heading"><div><span>{draft.number ? `Edit profile ${draft.number}` : "New numbered profile"}</span><h3>{draft.name || "Unnamed profile"}</h3></div></header><div className="library-profile-fields"><label><span>Profile name</span><input value={draft.name} maxLength={120} onChange={event => updateDraft({name: event.target.value})}/></label><label><span>Retention</span><select value={draft.installationPolicy} onChange={event => updateDraft({installationPolicy: event.target.value as FleetProfileInput["installation_policy"]})}><option value="keep-cached">Keep cached artifacts</option><option value="exact">Exact desired state</option></select></label><label className="library-profile-wide"><span>Purpose</span><textarea value={draft.description} maxLength={1000} rows={2} onChange={event => updateDraft({description: event.target.value})}/></label><label className="library-profile-favorite"><input type="checkbox" checked={draft.favorite} onChange={event => updateDraft({favorite: event.target.checked})}/><span>Favorite profile</span></label></div>
        <section className="library-profile-scope" aria-labelledby="profile-fleet-heading"><div className="library-profile-section-heading"><div><h4 id="profile-fleet-heading">Fleet and assignments</h4><p>Every enrolled Spark is in the profile view. Sparks without an assignment become idle when the profile loads.</p></div><span>{availableFleet.length} Sparks</span></div><div className="library-profile-scope-list" role="list" aria-label="Enrolled Sparks">{availableFleet.map(item => <div key={item.id} role="listitem"><strong>{item.name}</strong><small>{item.state}</small></div>)}{availableFleet.length === 0 && <p>No enrolled Sparks are visible yet.</p>}</div></section>
        <section className="library-profile-assignments" aria-label="Profile assignments"><header><h4>Recipe assignments</h4><button type="button" className="button secondary" onClick={addRecipe} disabled={matchingEntries.length === 0 || availableFleet.length === 0}>Add available Recipe</button></header>{draft.assignments.map(assignment => <article key={assignment.key} className="library-profile-assignment"><label><span>Recipe selector</span><input value={assignment.recipeSelector} onChange={event => updateAssignment(assignment.key, {recipeSelector: event.target.value})}/></label><label><span>Assignment name</span><input value={assignment.assignmentName} onChange={event => updateAssignment(assignment.key, {assignmentName: event.target.value})}/></label><label><span>Model variant</span><input value={assignment.modelVariant} onChange={event => updateAssignment(assignment.key, {modelVariant: event.target.value})}/></label><label><span>Desired state</span><select value={assignment.desiredState} onChange={event => updateAssignment(assignment.key, {desiredState: event.target.value as AssignmentInput["desired_state"]})}><option value="running">Running</option><option value="installed">Installed</option></select></label><fieldset><legend>Assigned Sparks</legend>{availableFleet.map(item => <label key={item.id}><input type="checkbox" checked={assignment.sparkIds.includes(item.id)} onChange={() => toggleSpark(assignment.key, item.id)}/><span>{names[item.id] ?? item.id}</span></label>)}</fieldset><button type="button" className="button danger" onClick={() => updateDraft({assignments: draft.assignments.filter(item => item.key !== assignment.key)})}>Remove assignment</button></article>)}{draft.assignments.length === 0 && <p>All Sparks will be idle when this profile loads.</p>}</section>
        <footer className="library-profile-editor-footer"><button type="button" className="button secondary" onClick={() => { if (selectedProfile) { setDraft(draftFromProfile(selectedProfile)); setEditing(false); } else setDraft(undefined); }}>Cancel</button><button type="button" className="button" disabled={!draftValid || saving} onClick={() => void saveDraft()}>{saving ? "Saving profile…" : draft.number ? "Save profile" : "Create profile"}</button></footer></div>}
      {selectedProfile && !editing && <section className="library-profile-saved" aria-label={`Profile ${selectedProfile.number} saved profile`}><header><div><span>Saved profile</span><h3>{selectedProfile.name}</h3></div><div><button type="button" className="button secondary" onClick={() => setEditing(true)}>Edit profile</button><button type="button" className="button" disabled={loadingProfile || applicationRunning || !preview?.allowed} onClick={() => void load()}>{loadingProfile ? "Loading…" : "Load profile"}</button></div></header><dl><div><dt>Number</dt><dd>{selectedProfile.number}</dd></div><div><dt>Revision</dt><dd>{selectedProfile.revision}</dd></div><div><dt>Cache</dt><dd>{selectedProfile.cache_summary?.state ? String(selectedProfile.cache_summary.state) : "Latest compatible cached recipes"}</dd></div></dl><ul>{selectedProfile.assignments.map(assignment => <li key={assignment.selector}><strong>{assignment.display_name}</strong><span>{assignment.recipe_selector} · {assignment.spark_ids.map(id => names[id] ?? id).join(" + ")} · {assignment.observed_state}</span></li>)}{selectedProfile.assignments.length === 0 && <li><strong>Idle fleet</strong><span>No assignments; every Spark becomes idle on load.</span></li>}</ul>{preview && !preview.allowed && preview.reasons[0] && <p className="library-profile-plain-error" role="alert">{preview.reasons[0].detail}</p>}{(selectedProfile.next_actions ?? []).length > 0 && <p>{(selectedProfile.next_actions ?? []).join(" · ")}</p>}</section>}
      {!draft && !loading && <div className="library-profile-empty-editor"><h3>Select or create a profile</h3><button type="button" className="button" onClick={startNew}>Create profile</button></div>}
    </div>
  </section>;
}
