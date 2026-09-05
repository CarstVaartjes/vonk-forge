import {useEffect, useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {
  ControlApi,
  FleetProfile,
  FleetProfileApplyInput,
  FleetProfileCaptureInput,
  FleetProfileDuplicateInput,
  FleetProfileInput,
  FleetProfileApplication,
  FleetProfilePreview,
  FleetProfileStatus,
  VisualFleetNode,
  VisualFleetSnapshot,
} from "../api/types";
import {nodeDisplayName} from "../lib/fleet";
import type {LibraryRecipeRecord} from "./library-workcell";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
type AssignmentInput = NonNullable<FleetProfileInput["assignments"]>[number];
type AssignmentDraft = {
  key: string;
  recipeId: string;
  recipeTitle: string;
  modelTitle: string;
  recipeRevisionId: string;
  topologyName: string;
  desiredState: AssignmentInput["desired_state"];
  alias: string;
  nodes: AssignmentInput["nodes"];
};
type ProfileDraft = {
  id?: string;
  name: string;
  description: string;
  favorite: boolean;
  installationPolicy: FleetProfileInput["installation_policy"];
  scopeNodeIds: string[];
  assignments: AssignmentDraft[];
};

const TERMINAL_APPLICATION_STATES = new Set(["succeeded", "failed", "cancelled"]);

function copyAssignment(assignment: FleetProfile["assignments"][number], index: number): AssignmentDraft {
  return {
    key: `assignment-${assignment.id}-${index}`,
    recipeId: assignment.recipe_id,
    recipeTitle: assignment.recipe_title,
    modelTitle: assignment.model_title ?? "",
    recipeRevisionId: assignment.recipe_revision_id,
    topologyName: assignment.topology_name,
    desiredState: assignment.desired_state,
    alias: assignment.alias ?? "",
    nodes: assignment.nodes.map(node => ({...node})),
  };
}

function draftFromProfile(profile: FleetProfile): ProfileDraft {
  const scopeNodeIds = [...profile.scope.node_ids];
  return {
    id: profile.id,
    name: profile.name,
    description: profile.description,
    favorite: profile.favorite,
    installationPolicy: profile.installation_policy,
    scopeNodeIds,
    assignments: profile.assignments.map(copyAssignment),
  };
}

function blankDraft(fleet?: VisualFleetSnapshot): ProfileDraft {
  return {
    name: "New fleet profile",
    description: "A complete desired setup for the selected Sparks.",
    favorite: false,
    installationPolicy: "keep-cached",
    scopeNodeIds: fleet?.nodes.map(node => node.id) ?? [],
    assignments: [],
  };
}

function profileInput(draft: ProfileDraft): FleetProfileInput {
  return {
    name: draft.name.trim(),
    description: draft.description.trim(),
    installation_policy: draft.installationPolicy,
    favorite: draft.favorite,
    labels: {},
    scope: {node_ids: [...new Set(draft.scopeNodeIds)].sort()},
    assignments: draft.assignments.map(assignment => ({
      recipe_revision_id: assignment.recipeRevisionId,
      topology_name: assignment.topologyName,
      desired_state: assignment.desiredState,
      alias: assignment.desiredState === "running" ? assignment.alias.trim() || null : null,
      nodes: assignment.nodes,
    })),
  };
}

function roleList(record: LibraryRecipeRecord): string[] {
  const roles = record.catalog?.topology_roles;
  if (roles && roles.length > 0) return roles.flatMap(role => Array.from({length: role.count}, () => role.name));
  const count = record.catalog?.node_count ?? (record.recipe?.topology_name?.includes("dual") || record.recipe?.topology_name?.includes("pair") ? 2 : 1);
  return Array.from({length: count}, (_, index) => index === 0 ? "leader" : "worker");
}

function addAssignment(record: LibraryRecipeRecord, draft: ProfileDraft, fleet?: VisualFleetSnapshot): ProfileDraft {
  const revision = record.recipe?.selected_revision;
  if (!record.recipe || !revision || revision.lifecycle !== "resolved") return draft;
  const roles = roleList(record);
  const available = draft.scopeNodeIds.length > 0 ? draft.scopeNodeIds : (fleet?.nodes.map(node => node.id) ?? []);
  const nodeIds = available.slice(0, roles.length);
  const nodes = nodeIds.map((nodeId, index) => ({node_id: nodeId, rank: index, role: roles[index] ?? `rank-${index}`, endpoint_owner: index === 0}));
  const assignment: AssignmentDraft = {
    key: `assignment-new-${crypto.randomUUID()}`,
    recipeId: record.recipe.recipe_id,
    recipeTitle: record.title,
    modelTitle: record.modelTitle,
    recipeRevisionId: revision.id,
    topologyName: record.recipe.topology_name ?? record.catalog?.topology_name ?? "unknown",
    desiredState: "running",
    alias: record.recipe.slug,
    nodes,
  };
  return {...draft, assignments: [...draft.assignments, assignment]};
}

function currentWork(node: VisualFleetNode): string {
  const running = (node.loaded ?? []).map(run => run.title || run.alias);
  if (running.length > 0) return running.join(" · ");
  const installed = (node.installed ?? []).map(item => item.title);
  return installed.length > 0 ? `${installed.join(" · ")} · installed` : "Idle";
}

function desiredWork(nodeId: string, draft: ProfileDraft): string {
  const assignments = draft.assignments.filter(assignment => assignment.nodes.some(node => node.node_id === nodeId));
  if (assignments.length === 0) return "Idle by intent";
  return assignments.map(assignment => assignment.desiredState === "running" ? assignment.recipeTitle : `${assignment.recipeTitle} · installed`).join(" · ");
}

function applicationLabel(application: FleetProfileApplication): string {
  if (application.state === "succeeded") return "Profile matched";
  if (application.state === "failed" || application.state === "cancelled") return "Switch needs attention";
  if (application.state === "waiting-for-operator") return "Switch waiting for operator";
  return "Switch in progress";
}

function applicationProgressLabel(application: FleetProfileApplication, nodeNames: Record<string, string>): string {
  const progress = profileProgressRecord(application);
  const subphase = typeof progress.subphase === "string" ? progress.subphase : "";
  const phase = typeof progress.phase === "string" ? progress.phase : "";
  const members = Array.isArray(progress.members) ? progress.members : [];
  const activeMember = members.find(member => member && typeof member === "object" && (member as Record<string, unknown>).state === "running")
    ?? members.find(member => member && typeof member === "object" && (member as Record<string, unknown>).state === "pending");
  const nodeId = activeMember && typeof activeMember === "object" && typeof (activeMember as Record<string, unknown>).node_id === "string"
    ? String((activeMember as Record<string, unknown>).node_id)
    : undefined;
  const nodeName = nodeId ? nodeNames[nodeId] ?? nodeId : undefined;
  if (subphase === "container-build" || phase === "build" || phase === "prepare") return "Building container";
  if (subphase === "model-download" || subphase === "model-copy" || phase === "transfer" && progress.asset === "model") return nodeName ? `Copying model to ${nodeName}` : "Downloading model";
  if (subphase === "runtime-install" || subphase === "container-copy" || phase === "transfer" && progress.asset === "container") return nodeName ? `Copying container to ${nodeName}` : "Copying container to NAS";
  if (phase === "start") return "Starting";
  if (phase === "final_verify" || phase === "running") return "Running";
  if (phase === "switch") return "Switching profile";
  if (application.status_reason && application.state === "failed") return application.status_reason;
  return application.state === "queued" ? "Checking current setup" : applicationLabel(application);
}

function profileProgressRecord(application: FleetProfileApplication): Record<string, unknown> {
  return application.progress && typeof application.progress === "object" ? application.progress : {};
}

function profileBytes(value: unknown): string | undefined {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return undefined;
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let unit = "B";
  for (const next of units) {
    amount /= 1024;
    unit = next;
    if (amount < 1024) break;
  }
  return `${amount.toFixed(amount >= 10 ? 0 : 1)} ${unit}`;
}

function ProfileApplicationProgress({application, nodeNames, onRetry}: {application: FleetProfileApplication; nodeNames: Record<string, string>; onRetry(): void}) {
  const progress = profileProgressRecord(application);
  const completed = profileBytes(progress.completed_bytes);
  const total = profileBytes(progress.total_bytes);
  const totalKnown = typeof progress.total_bytes === "number" && Number.isFinite(progress.total_bytes);
  const value = totalKnown && typeof progress.completed_bytes === "number" ? Math.min(100, Math.max(0, progress.completed_bytes / Number(progress.total_bytes) * 100)) : undefined;
  const members = Array.isArray(progress.members) ? progress.members.flatMap(member => {
    if (!member || typeof member !== "object") return [];
    const item = member as Record<string, unknown>;
    if (typeof item.node_id !== "string") return [];
    return [{nodeId: item.node_id, state: typeof item.state === "string" ? item.state : "pending", completed: profileBytes(item.completed_bytes), total: profileBytes(item.total_bytes)}];
  }) : [];
  const memberLabel = (state: string) => state === "succeeded" ? "Complete" : state === "failed" ? "Failed" : state === "running" ? "In progress" : state === "unknown" ? "Status unavailable" : "Waiting";
  return <section className={`library-profile-application state-${application.state}`} aria-live="polite" aria-label="Profile switch progress">
    <div className="library-profile-application-heading"><div><strong>{applicationProgressLabel(application, nodeNames)}</strong><span>{application.state.replaceAll("-", " ")}</span></div>{completed && <span>{completed}{total ? ` of ${total}` : ""}</span>}</div>
    <div className={`library-profile-application-progress${value === undefined ? " is-indeterminate" : ""}`} role="progressbar" aria-label="Profile switch progress" aria-valuemin={0} aria-valuemax={100} {...(value === undefined ? {"aria-valuetext": "Progress total unavailable"} : {"aria-valuenow": value})}><span style={value === undefined ? undefined : {width: `${value}%`}}/></div>
    {members.length > 0 && <ul className="library-profile-application-members" aria-label="Profile switch targets">{members.map(member => <li key={member.nodeId}><span>{nodeNames[member.nodeId] ?? member.nodeId}</span><small>{memberLabel(member.state)}{member.completed ? ` · ${member.completed}${member.total ? ` of ${member.total}` : ""}` : ""}</small></li>)}</ul>}
    {application.status_reason && <p>{application.status_reason}</p>}
    {TERMINAL_APPLICATION_STATES.has(application.state) && application.state !== "succeeded" && <button type="button" className="button secondary" onClick={onRetry}>Recheck and retry</button>}
  </section>;
}

const PROFILE_CONFIRMATION_REASON_CODES = new Set([
  "profile.scope_changed",
  "profile.distributed_cross_scope",
  "profile.shared_installation_scope",
  "profile.preparation_scope_mismatch",
  "profile.unresolved_conflict",
  "profile.choice_required",
]);

function planNeedsConfirmation(preview: FleetProfilePreview): boolean {
  if (!preview.allowed) return true;
  if (preview.summary.uninstalls > 0) return true;
  return preview.reasons.some(reason => PROFILE_CONFIRMATION_REASON_CODES.has(reason.code));
}

function reasonLabel(code: string): string {
  return code.split(".").at(-1)?.replaceAll("_", " ") ?? code;
}

function profileStatusLabel(status: FleetProfileStatus): string {
  if (status.matched) return "Up to date";
  if (status.drifted) return "Needs update";
  if (status.state === "blocked") return "Needs attention";
  if (status.state === "partially-applied") return "Partially applied";
  return "Not active everywhere";
}

export function LibraryProfilesView({api, entries, fleet, initialCreate = false, initialProfileId, onBusyChange, onNavigate}: {
  api: ControlApi;
  entries: LibraryRecipeRecord[];
  fleet?: VisualFleetSnapshot;
  initialCreate?: boolean;
  initialProfileId?: string;
  onBusyChange?(busy: boolean): void;
  onNavigate: Navigate;
}) {
  const profileApi = api;
  const [profiles, setProfiles] = useState<FleetProfile[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState<string>();
  const [draft, setDraft] = useState<ProfileDraft>();
  const [editing, setEditing] = useState(initialCreate);
  const [savedDraftKey, setSavedDraftKey] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [preview, setPreview] = useState<FleetProfilePreview>();
  const [previewing, setPreviewing] = useState(false);
  const [profilesAttempt, setProfilesAttempt] = useState(0);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [confirmationRequired, setConfirmationRequired] = useState(false);
  const [application, setApplication] = useState<FleetProfileApplication>();
  const [applying, setApplying] = useState(false);
  const [recipeToAdd, setRecipeToAdd] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [scopeContractAvailable, setScopeContractAvailable] = useState(true);
  const [profileStatus, setProfileStatus] = useState<FleetProfileStatus>();
  const [statusAttempt, setStatusAttempt] = useState(0);
  const [requestKey, setRequestKey] = useState<string>();

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void profileApi.fleetProfiles(controller.signal)
      .then(result => {
        if (controller.signal.aborted) return;
        const resultHasScope = result.profiles.length === 0
          ? true
          : result.profiles.every(profile => Array.isArray(profile.scope.node_ids));
        setScopeContractAvailable(resultHasScope);
        setProfiles(result.profiles);
        const requested = initialProfileId && result.profiles.some(profile => profile.id === initialProfileId) ? initialProfileId : result.profiles[0]?.id;
        if (initialCreate) {
          setSelectedProfileId(undefined);
          setDraft(blankDraft(fleet));
          setSavedDraftKey("");
          setEditing(true);
        } else if (requested) {
          const selected = result.profiles.find(profile => profile.id === requested);
          setSelectedProfileId(requested);
          if (selected) {
            const next = draftFromProfile(selected);
            setDraft(next);
            setSavedDraftKey(JSON.stringify(next));
            setEditing(false);
          }
        } else {
          setSelectedProfileId(undefined);
          setDraft(undefined);
          setSavedDraftKey("");
          setEditing(false);
        }
      })
      .catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message.slice(0, 256) : "Saved profiles are unavailable."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, fleet, initialCreate, initialProfileId, profilesAttempt]);

  const matchingEntries = useMemo(() => entries.filter(entry => entry.recipe?.selected_revision?.lifecycle === "resolved" && entry.recipe.selected_revision.id && entry.recipe.topology_name), [entries]);
  const draftDirty = Boolean(draft && JSON.stringify(draft) !== savedDraftKey);
  const selectedProfile = profiles.find(profile => profile.id === selectedProfileId);
  const assignedNodeIds = new Set(draft?.assignments.flatMap(assignment => assignment.nodes.map(node => node.node_id)) ?? []);
  const applicationRunning = Boolean(application && !TERMINAL_APPLICATION_STATES.has(application.state));

  useEffect(() => {
    if (!selectedProfileId || !scopeContractAvailable) {
      setProfileStatus(undefined);
      return;
    }
    const controller = new AbortController();
    void profileApi.fleetProfileStatus(selectedProfileId, controller.signal)
      .then(value => { if (!controller.signal.aborted) setProfileStatus(value); })
      .catch(() => { if (!controller.signal.aborted) setProfileStatus(undefined); });
    return () => controller.abort();
  }, [api, scopeContractAvailable, selectedProfileId, statusAttempt]);

  useEffect(() => {
    onBusyChange?.(applicationRunning || applying);
    return () => onBusyChange?.(false);
  }, [applicationRunning, applying, onBusyChange]);

  useEffect(() => {
    if (!selectedProfileId || draftDirty) {
      setPreview(undefined);
      return;
    }
    const controller = new AbortController();
    setPreviewing(true);
    setError("");
    void profileApi.previewFleetProfile(selectedProfileId, controller.signal)
        .then(result => { if (!controller.signal.aborted) { setPreview(result); setConfirmationRequired(false); } })
      .catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message.slice(0, 256) : "The profile preview is unavailable."); })
      .finally(() => { if (!controller.signal.aborted) setPreviewing(false); });
    return () => controller.abort();
  }, [api, draftDirty, previewAttempt, selectedProfileId]);

  useEffect(() => {
    if (!application || TERMINAL_APPLICATION_STATES.has(application.state)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void profileApi.fleetProfileApplication(application.id, controller.signal).then(next => {
        if (controller.signal.aborted) return;
        setApplication(next);
      }).catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message.slice(0, 256) : "Switch progress is unavailable."); });
    }, 1000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [api, application]);

  function selectProfile(profile: FleetProfile) {
    const next = draftFromProfile(profile);
    setSelectedProfileId(profile.id);
    setDraft(next);
    setEditing(false);
    setSavedDraftKey(JSON.stringify(next));
    setPreview(undefined);
    setConfirmationRequired(false);
    setApplication(undefined);
    setRequestKey(undefined);
    setProfileStatus(undefined);
    setRecipeToAdd("");
    setConfirmDelete(false);
    setNotice("");
  }

  function startNew() {
    setSelectedProfileId(undefined);
    setDraft(blankDraft(fleet));
    setEditing(true);
    setSavedDraftKey("");
    setPreview(undefined);
    setApplication(undefined);
    setRequestKey(undefined);
    setProfileStatus(undefined);
    setConfirmationRequired(false);
    setRecipeToAdd("");
    setNotice("Draft profile created. Saving does not change the running fleet.");
  }

  async function duplicate() {
    if (!draft) return;
    setError("");
    if (draft.id) {
      setApplying(true);
      try {
        const result = await profileApi.duplicateFleetProfile(draft.id, {name: `${draft.name} copy`});
        setProfiles(current => [...current.filter(profile => profile.id !== result.id), result].sort((left, right) => Number(right.favorite) - Number(left.favorite) || left.name.localeCompare(right.name)));
        setSelectedProfileId(result.id);
        const next = draftFromProfile(result);
        setDraft(next);
        setEditing(true);
        setSavedDraftKey(JSON.stringify(next));
        setPreview(undefined);
        setApplication(undefined);
        setRequestKey(undefined);
        setProfileStatus(undefined);
        setConfirmationRequired(false);
        setNotice("Profile duplicated. Review the Sparks and placements before switching.");
      } catch (value) {
        setError(value instanceof Error ? value.message.slice(0, 256) : "The profile could not be duplicated.");
      } finally {
        setApplying(false);
      }
      return;
    }
    setSelectedProfileId(undefined);
    setDraft({...draft, id: undefined, name: `${draft.name} copy`, favorite: false, assignments: draft.assignments.map((assignment, index) => ({...assignment, key: `duplicate-${index}-${assignment.key}`}))});
    setEditing(true);
    setSavedDraftKey("");
    setPreview(undefined);
    setApplication(undefined);
    setRequestKey(undefined);
    setConfirmationRequired(false);
    setNotice("Duplicate draft ready. Review the exact scope and assignments before saving.");
  }

  function updateDraft(next: Partial<ProfileDraft>) {
    setDraft(current => current ? {...current, ...next} : current);
    setNotice("");
  }

  function toggleScope(nodeId: string) {
    if (!draft) return;
    const next = draft.scopeNodeIds.includes(nodeId)
      ? draft.scopeNodeIds.filter(id => id !== nodeId)
      : [...draft.scopeNodeIds, nodeId];
    updateDraft({scopeNodeIds: next});
  }

  function updateAssignment(key: string, next: Partial<AssignmentDraft>) {
    if (!draft) return;
    updateDraft({assignments: draft.assignments.map(assignment => assignment.key === key ? {...assignment, ...next} : assignment)});
  }

  function updateAssignmentNodes(key: string, nodeIds: string[]) {
    if (!draft) return;
    const assignment = draft.assignments.find(item => item.key === key);
    if (!assignment) return;
    const nodes = nodeIds.map((nodeId, index) => {
      const previous = assignment.nodes[index];
      return {node_id: nodeId, rank: index, role: previous?.role ?? (index === 0 ? "leader" : "worker"), endpoint_owner: index === 0};
    });
    updateAssignment(key, {nodes});
  }

  function addSelectedRecipe() {
    if (!draft || !recipeToAdd) return;
    const record = matchingEntries.find(entry => entry.key === recipeToAdd);
    if (!record) return;
    setDraft(addAssignment(record, draft, fleet));
    setRecipeToAdd("");
    setNotice("Placement added. Save the profile to check the complete Spark group.");
  }

  async function captureCurrent() {
    setError("");
    setApplying(true);
    try {
      const result = await profileApi.captureCurrentFleetProfile({
        name: draft?.name.trim() || "Captured Fleet setup",
        description: draft?.description.trim() || "Captured current Fleet setup",
        installation_policy: draft?.installationPolicy ?? "keep-cached",
        favorite: draft?.favorite ?? false,
      });
      setProfiles(current => [...current.filter(profile => profile.id !== result.id), result].sort((left, right) => Number(right.favorite) - Number(left.favorite) || left.name.localeCompare(right.name)));
      setSelectedProfileId(result.id);
      const next = draftFromProfile(result);
      setDraft(next);
      setEditing(false);
      setSavedDraftKey(JSON.stringify(next));
      setPreview(undefined);
      setConfirmationRequired(false);
      setRequestKey(undefined);
      setProfileStatus(undefined);
      setNotice("The current setup was saved as a profile. Switch to it when you are ready.");
    } catch (value) {
      setError(value instanceof Error ? value.message.slice(0, 256) : "The current setup could not be captured.");
    } finally {
      setApplying(false);
    }
  }

  async function saveDraft() {
    if (!draft || !draft.name.trim()) return;
    setError("");
    setNotice("");
    setApplying(true);
    try {
      const input = profileInput(draft);
      const result = draft.id
        ? await profileApi.updateFleetProfile(draft.id, input)
        : await profileApi.createFleetProfile(input);
      setProfiles(current => [...current.filter(profile => profile.id !== result.id), result].sort((left, right) => Number(right.favorite) - Number(left.favorite) || left.name.localeCompare(right.name)));
      setSelectedProfileId(result.id);
      const next = draftFromProfile(result);
      setDraft(next);
      setEditing(false);
      setSavedDraftKey(JSON.stringify(next));
      setPreview(undefined);
      setPreviewAttempt(value => value + 1);
      setRequestKey(undefined);
      setProfileStatus(undefined);
      setNotice(`${result.name} saved. Current runs remain unchanged until you switch the profile.`);
    } catch (value) {
      setError(value instanceof Error ? value.message.slice(0, 256) : "The profile could not be saved.");
    } finally {
      setApplying(false);
    }
  }

  async function deleteSelected() {
    if (!selectedProfileId) return;
    setError("");
    setApplying(true);
    try {
      await profileApi.deleteFleetProfile(selectedProfileId);
      const remaining = profiles.filter(profile => profile.id !== selectedProfileId);
      setProfiles(remaining);
      setConfirmDelete(false);
      setSelectedProfileId(remaining[0]?.id);
      const next = remaining[0] ? draftFromProfile(remaining[0]) : blankDraft(fleet);
      setDraft(remaining[0] ? next : undefined);
      setEditing(false);
      setSavedDraftKey(remaining[0] ? JSON.stringify(next) : "");
      setPreview(undefined);
      setRequestKey(undefined);
      setProfileStatus(undefined);
      setNotice("Profile deleted. Cached artifacts and Spark installations were not removed.");
    } catch (value) {
      setError(value instanceof Error ? value.message.slice(0, 256) : "The profile could not be deleted.");
    } finally {
      setApplying(false);
    }
  }

  async function applySwitch(plan: FleetProfilePreview = preview as FleetProfilePreview) {
    if (!selectedProfileId || !plan || !plan.allowed || !plan.steps.length || applying) return;
    setApplying(true);
    setError("");
    try {
      const key = requestKey ?? crypto.randomUUID();
      setRequestKey(key);
      const applyInput: FleetProfileApplyInput = {plan_digest: plan.plan_digest, request_key: key};
      const next = await profileApi.applyFleetProfile(selectedProfileId, applyInput);
      setApplication(next);
      if (next.state === "succeeded") setRequestKey(undefined);
      if (TERMINAL_APPLICATION_STATES.has(next.state)) setPreviewAttempt(value => value + 1);
    } catch (value) {
      setError(value instanceof Error ? value.message.slice(0, 256) : "The profile switch could not be started.");
      setNotice("The switch request may have been accepted. Recheck profile status before retrying; the same request key will be reused.");
    } finally {
      setApplying(false);
    }
  }

  async function switchProfile() {
    if (!selectedProfileId || draftDirty || applying) return;
    setError("");
    let plan = preview;
    if (!plan) {
      const controller = new AbortController();
      setPreviewing(true);
      try {
        plan = await profileApi.previewFleetProfile(selectedProfileId, controller.signal);
        setPreview(plan);
      } catch (value) {
        setError(value instanceof Error ? value.message.slice(0, 256) : "The current setup could not be checked.");
      } finally {
        setPreviewing(false);
      }
    }
    if (!plan) return;
    if (!plan.allowed || planNeedsConfirmation(plan)) {
      setConfirmationRequired(true);
      return;
    }
    if (plan.steps.length === 0) {
      setNotice("This profile already matches the observed fleet. No switch was dispatched.");
      return;
    }
    await applySwitch(plan);
  }

  if (!scopeContractAvailable) return <section className="library-profile-view library-unavailable" aria-labelledby="library-profiles-heading"><header className="library-subview-heading"><div><h2 id="library-profiles-heading">Profiles</h2><p>Create and review complete setups across the enrolled Sparks.</p></div><a className="button secondary" href="/library?view=models" onClick={event => onNavigate(event, "/library?view=models")}>Choose a model</a></header><div className="library-cache-notice" role="status"><div><strong>Complete Spark scope is unavailable</strong><p>This Controller did not report which Sparks belong to each profile. Refresh after the profile service is updated.</p></div><span>Profile details unavailable</span></div>{error && <div className="library-cache-state is-error" role="alert"><span>{error}</span><button type="button" className="button secondary" onClick={() => setProfilesAttempt(value => value + 1)}>Retry profiles</button></div>}</section>;

  return <section className="library-profile-view" aria-labelledby="library-profiles-heading">
    <>
      <header className="library-subview-heading">
        <div><h2 id="library-profiles-heading">Profiles</h2><p>Save a complete setup, then switch the selected Sparks with one action. Missing model and runtime assets are fetched and verified as needed.</p></div>
        <div className="library-profile-header-actions"><button type="button" className="button secondary" onClick={startNew}>Create profile</button><a className="button secondary" href="/library?view=models" onClick={event => onNavigate(event, "/library?view=models")}>Choose a model</a></div>
      </header>
      {loading && <p className="library-cache-state" role="status">Loading saved profiles…</p>}
      {notice && <div className="library-profile-notice" role="status">{notice}</div>}
      {profileStatus && <section className={`library-profile-status state-${profileStatus.state}`} aria-live="polite">
        <div className="library-profile-status-summary"><strong>Profile status: {profileStatusLabel(profileStatus)}</strong><span>{profileStatus.matched ? "The Sparks match this profile." : profileStatus.drifted ? "Some Sparks differ from this profile." : "This profile is not active on every Spark."}</span></div>
        <span className="library-profile-status-scope">{profileStatus.scope.idle_node_ids?.length ?? 0} idle by choice · {profileStatus.scope.node_ids.length} Sparks in scope</span>
        {profileStatus.reasons.length > 0 && <details className="library-profile-status-details"><summary>Attention details</summary><ul>{profileStatus.reasons.map(reason => <li key={`${reason.code}:${reason.detail}`}><strong>{reasonLabel(reason.code)}</strong><span>{reason.detail}</span></li>)}</ul></details>}
        <button type="button" className="button secondary" onClick={() => setStatusAttempt(value => value + 1)}>Refresh status</button>
      </section>}
      {application && <ProfileApplicationProgress application={application} nodeNames={Object.fromEntries(fleet?.nodes.map(node => [node.id, nodeDisplayName(node)]) ?? [])} onRetry={() => { setApplication(undefined); setPreviewAttempt(value => value + 1); }}/>}
      <div className="library-profile-layout">
        <aside className="library-profile-list" aria-label="Saved profiles">
          <div className="library-profile-list-heading"><strong>Saved profiles</strong><span>{profiles.length}</span></div>
          {profiles.map(profile => <button key={profile.id} type="button" className={profile.id === selectedProfileId ? "is-selected" : undefined} aria-pressed={profile.id === selectedProfileId} onClick={() => selectProfile(profile)}><span>{profile.name}</span><small>{profile.assignments.length} placement{profile.assignments.length === 1 ? "" : "s"} · {profile.installation_policy === "exact" ? "Exact" : "Keep cached"}</small></button>)}
          {profiles.length === 0 && <div className="library-profile-list-empty"><strong>No saved profiles</strong><p>Create one here or choose a model and save its exact placement.</p></div>}
          <button type="button" className="library-profile-create-link" onClick={startNew}>+ New profile</button>
        </aside>
        {draft && editing && <div className="library-profile-editor">
          <header className="library-profile-editor-heading"><div><span>{draft.id ? "Edit saved profile" : "New profile draft"}</span><h3>{draft.name || "Unnamed profile"}</h3></div><div className="library-profile-editor-actions">{draft.id && <button type="button" className="button secondary" onClick={duplicate}>Duplicate</button>}{draft.id && <button type="button" className="button secondary" onClick={() => setConfirmDelete(true)} disabled={applying}>Delete</button>}</div></header>
          {confirmDelete && draft.id && <div className="library-profile-delete-confirm" role="alert"><div><strong>Delete {draft.name}?</strong><p>This removes the saved desired state. Cached NAS artifacts and Spark installations remain untouched.</p></div><div><button type="button" className="button secondary" onClick={() => setConfirmDelete(false)}>Keep profile</button><button type="button" className="button danger" onClick={() => void deleteSelected()}>Delete profile</button></div></div>}
          <div className="library-profile-fields"><label><span>Profile name</span><input value={draft.name} maxLength={80} onChange={event => updateDraft({name: event.target.value})}/></label><label><span>Retention</span><select value={draft.installationPolicy} onChange={event => updateDraft({installationPolicy: event.target.value as FleetProfileInput["installation_policy"]})}><option value="keep-cached">Keep cached artifacts</option><option value="exact">Remove unlisted installations</option></select></label><label className="library-profile-wide"><span>Purpose</span><textarea value={draft.description} maxLength={512} rows={2} onChange={event => updateDraft({description: event.target.value})}/></label><label className="library-profile-favorite"><input type="checkbox" checked={draft.favorite} onChange={event => updateDraft({favorite: event.target.checked})}/><span>Pin this profile for quick switching</span></label></div>
          <section className="library-profile-scope" aria-labelledby="profile-scope-heading"><div className="library-profile-section-heading"><div><h4 id="profile-scope-heading">Fleet scope</h4><p>Every selected Spark gets an explicit outcome. A selected Spark without a placement stays idle by intent.</p></div><span>{draft.scopeNodeIds.length} of {fleet?.nodes.length ?? 0} selected</span></div><div className="library-profile-scope-actions"><button type="button" className="button secondary" onClick={() => updateDraft({scopeNodeIds: fleet?.nodes.map(node => node.id) ?? []})}>Select all Sparks</button><button type="button" className="button secondary" onClick={() => updateDraft({scopeNodeIds: []})}>Clear scope</button><button type="button" className="button secondary" onClick={captureCurrent}>Capture current setup</button></div><div className="library-profile-scope-list">{fleet?.nodes.map(node => <label key={node.id} className={draft.scopeNodeIds.includes(node.id) ? "is-selected" : undefined}><input type="checkbox" checked={draft.scopeNodeIds.includes(node.id)} onChange={() => toggleScope(node.id)}/><span><strong>{nodeDisplayName(node)}</strong><small>{assignedNodeIds.has(node.id) ? "Assigned workload" : draft.scopeNodeIds.includes(node.id) ? "Idle by intent" : "Outside profile scope"}</small></span></label>)}{!fleet && <p role="status">Spark membership is not available yet. You can draft assignments and recheck scope before saving.</p>}{fleet && fleet.nodes.length === 0 && <p>No Sparks are enrolled. Save a draft and apply after enrollment.</p>}</div></section>
          <section className="library-profile-assignments" aria-labelledby="profile-assignments-heading"><div className="library-profile-section-heading"><div><h4 id="profile-assignments-heading">Desired placements</h4><p>Recipe revisions stay pinned. The same revision may appear on more than one complete Spark group.</p></div><span>{draft.assignments.length} placement{draft.assignments.length === 1 ? "" : "s"}</span></div><div className="library-profile-add-row"><label><span>Add exact recipe</span><select aria-label="Add exact recipe" value={recipeToAdd} onChange={event => setRecipeToAdd(event.target.value)}><option value="">Choose a resolved recipe…</option>{matchingEntries.map(entry => <option key={entry.key} value={entry.key}>{entry.title} · {entry.modelTitle}</option>)}</select></label><button type="button" className="button secondary" disabled={!recipeToAdd} onClick={addSelectedRecipe}>Add placement</button></div>{draft.assignments.length === 0 && <div className="library-profile-empty-assignments"><strong>All scoped Sparks are idle</strong><p>Add an exact recipe or capture the current setup. An empty profile is a deliberate desired state.</p></div>}<div className="library-profile-assignment-list">{draft.assignments.map((assignment, index) => <article key={assignment.key} className="library-profile-assignment"><header><div><strong>{assignment.recipeTitle}</strong><small>{assignment.modelTitle || "Model metadata not reported"} · {assignment.topologyName} · revision {assignment.recipeRevisionId.slice(0, 12)}…</small></div><button type="button" className="button secondary" onClick={() => updateDraft({assignments: draft.assignments.filter(item => item.key !== assignment.key)})}>Remove placement</button></header><div className="library-profile-assignment-fields"><label><span>Desired state</span><select value={assignment.desiredState} onChange={event => updateAssignment(assignment.key, {desiredState: event.target.value as AssignmentInput["desired_state"]})}><option value="running">Running</option><option value="installed">Installed and ready</option></select></label><label><span>Endpoint alias</span><input value={assignment.alias} disabled={assignment.desiredState !== "running"} onChange={event => updateAssignment(assignment.key, {alias: event.target.value})}/></label><fieldset><legend>Spark ranks</legend>{fleet?.nodes.map(node => <label key={node.id}><input type="checkbox" checked={assignment.nodes.some(item => item.node_id === node.id)} onChange={event => { const next = assignment.nodes.map(item => item.node_id); const nodeIds = event.target.checked ? [...next, node.id] : next.filter(id => id !== node.id); updateAssignmentNodes(assignment.key, nodeIds); }}/><span>{nodeDisplayName(node)}</span></label>)}{assignment.nodes.length === 0 && <p className="is-error">No Spark ranks selected. Add a complete group before preview.</p>}</fieldset></div><p className="library-profile-assignment-note">Rank order: {assignment.nodes.length ? assignment.nodes.map(node => `${node.rank} ${node.role} · ${fleet?.nodes.find(item => item.id === node.node_id) ? nodeDisplayName(fleet.nodes.find(item => item.id === node.node_id)!) : node.node_id}`).join(" → ") : "No ranks"}</p><small className="library-profile-assignment-index">Placement {index + 1}</small></article>)}</div></section>
          <footer className="library-profile-editor-footer"><span>{draftDirty ? "Unsaved draft" : draft.id ? "Saved profile" : "Draft only"} · changes never stop current runs while editing</span><button type="button" className="button" disabled={!draft.name.trim() || applying || !draftDirty} onClick={() => void saveDraft()}>{applying ? "Saving…" : draft.id ? "Save profile" : "Create profile"}</button></footer>
          {selectedProfile && <section className="library-profile-switch-panel" aria-label="Profile switch"><header><div><span>{previewing ? "Checking current setup" : applicationRunning ? "Switch in progress" : "Switch profile"}</span><h4>{preview ? (preview.allowed ? (preview.steps.length > 0 ? "Ready to switch" : "Already up to date") : "Switch needs attention") : draftDirty ? "Save changes before switching" : "Checking current setup"}</h4></div><div className="library-profile-switch-actions"><button type="button" className="button secondary" disabled={draftDirty || previewing || applying} onClick={() => setPreviewAttempt(value => value + 1)}>Refresh status</button><button type="button" className="button" disabled={draftDirty || previewing || applying || applicationRunning} onClick={() => void switchProfile()}>{applying ? "Starting switch…" : applicationRunning ? "Switching…" : preview && !preview.allowed ? "Switch unavailable" : preview && planNeedsConfirmation(preview) && !confirmationRequired ? "Review effects" : confirmationRequired ? "Confirm switch" : "Switch profile"}</button></div></header>{preview && <div className="library-profile-preview-summary"><span>{preview.allowed ? (preview.steps.length > 0 ? "Missing files are fetched and verified as needed" : "No changes required") : "Some Sparks need attention before switching"}</span>{preview.steps.length > 0 && <span>{preview.summary.stops} stop · {preview.summary.starts} start · {preview.summary.installs} install</span>}</div>}{preview && preview.reasons.length > 0 && <details className="library-profile-inline-findings" open={confirmationRequired || !preview.allowed}><summary>{preview.allowed && planNeedsConfirmation(preview) ? "Effects need your choice" : "Details"}</summary><ul className="library-profile-preview-reasons">{preview.reasons.map(reason => <li key={`${reason.code}:${reason.detail}`} className={`reason-${reason.severity}`}><strong>{reasonLabel(reason.code)}</strong><span>{reason.detail}</span></li>)}</ul>{confirmationRequired && preview.allowed && <button type="button" className="button" disabled={applying || applicationRunning} onClick={() => void applySwitch(preview)}>Confirm switch</button>}</details>}</section>}
        </div>}
        {draft && !editing && selectedProfile && <section className="library-profile-saved" aria-label={`${selectedProfile.name} saved profile`}>
          <header><div><span>Saved profile</span><h3>{selectedProfile.name}</h3><p>{selectedProfile.description || "No description"}</p></div><button type="button" className="button secondary" onClick={() => setEditing(true)}>Edit profile</button></header>
          <dl><div><dt>Sparks in scope</dt><dd>{selectedProfile.scope.node_ids.length}</dd></div><div><dt>Placements</dt><dd>{selectedProfile.assignments.length}</dd></div><div><dt>Cache policy</dt><dd>{selectedProfile.installation_policy === "exact" ? "Exact" : "Keep cached"}</dd></div></dl>
          <div className="library-profile-saved-work"><strong>Saved setup</strong><ul>{selectedProfile.assignments.map(assignment => <li key={assignment.id}><span>{assignment.model_title || assignment.recipe_title}</span><small>{assignment.recipe_title} · {assignment.nodes.length} {assignment.nodes.length === 1 ? "Spark" : "Sparks"} · {assignment.desired_state}</small></li>)}{selectedProfile.assignments.length === 0 && <li><span>Idle profile</span><small>Every Spark in scope stays idle.</small></li>}</ul></div>
          <div className="library-profile-saved-switch"><div><span>{previewing ? "Checking current setup" : applicationRunning ? "Switch in progress" : "Switch profile"}</span><strong>{applicationRunning ? "Applying this setup" : preview ? (preview.allowed ? (preview.steps.length ? "Ready to switch" : "Already up to date") : "Switch needs attention") : "Checking current setup"}</strong></div><button type="button" className="button" disabled={previewing || applying || applicationRunning || !preview?.allowed || !preview.steps.length} onClick={() => void switchProfile()}>{applying ? "Switching…" : applicationRunning ? "Switching…" : "Switch profile"}</button></div>
          {preview && !preview.allowed && preview.reasons[0] && <p className="library-profile-plain-error" role="alert">{preview.reasons[0].detail}</p>}
        </section>}
      </div>
      {!draft && !loading && <div className="library-profile-empty-editor"><h3>Select a saved profile</h3><p>Or create a new profile to describe the complete desired fleet setup.</p><button type="button" className="button" onClick={startNew}>Create profile</button></div>}
    </>
  </section>;
}
