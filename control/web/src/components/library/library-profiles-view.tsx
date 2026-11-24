import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ConnectionExamples } from "../ui/connection-examples";
import type { MouseEvent } from "react";
import { addWire, compareWire, formatWire, isWireNumber } from "../../api/contract-numeric";
import type { WireNumber } from "../../api/contract-numeric";
import { validateControlParameters } from "../../api/contract-json";
import { ApiError } from "../../api/client";
import { ProfileLoadAction } from "../../api/vocabulary.generated";
import { canonicalRecipeSelector, readableProfile } from "../../api/types";
import type {
  ControlApi,
  FleetProfile,
  FleetProfileNumber,
  FleetProfileRead,
  FleetProfileApplicationView,
  FleetProfileEndpoints,
  FleetProfileInput,
  FleetProfilePreview,
  VisualFleetSnapshot,
} from "../../api/types";
import type { LibraryRecipeRecord } from "./library-workcell";
import { ConfirmDialog } from "../ui/confirm-dialog";
import { StatusPill } from "../ui/status-pill";
import { WaitingFor } from "../ui/waiting-for";
import { ObservationNotice } from "../ui/observation-notice";
import { useOperationObserver } from "../../hooks/use-operation-observer";
import { EmptyState } from "../ui/empty-state";
import { ProfileExport } from "../ui/profile-export";
import { SkeletonRows } from "../ui/skeleton";
import { useToast } from "../ui/toast";
import { RecipeOptionSelects } from "../ui/recipe-option-selects";
import type { RecipeOption } from "../ui/recipe-option-selects";
import {
  type AssignmentDraft,
  type AssignmentInput,
  type ProfileDraft,
  blankDraft,
  draftFromProfile,
  draftKey,
  fleetEntries,
  inputFromDraft,
  isDirty,
  unsavedDrafts,
} from "./library-profile-drafts";
import {
  loadedFirst,
  openingProfile,
  profileStateLabel,
  sameNumber,
} from "./library-profile-selection";
import {
  type PendingProfileLoad,
  ended,
  loadable,
  nothingToChange,
  stoppedAliases,
  submitProfileLoad,
} from "./library-profile-load";
import { ProfileProgress } from "./library-profile-progress";
import { ProfileReview } from "./library-profile-review";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
/** The server reports per-assignment cache counts: {cached, missing, unknown}. */
export function cacheSummaryText(summary: Record<string, unknown> | undefined): string {
  const count = (key: string): WireNumber => {
    const value = summary?.[key];
    return isWireNumber(value) ? value : 0;
  };
  const parts = (
    [
      ["cached", count("cached")],
      ["missing", count("missing")],
      ["unknown", count("unknown")],
    ] as const
  ).filter(([, value]) => compareWire(value, 0) > 0);
  return parts.length
    ? parts.map(([label, value]) => `${formatWire(value)} ${label}`).join(" · ")
    : "Latest compatible cached recipes";
}

export function LibraryProfilesView({
  api,
  entries,
  fleet,
  initialCreate = false,
  onBusyChange,
  onNavigate,
}: {
  api: ControlApi;
  entries: LibraryRecipeRecord[];
  fleet?: VisualFleetSnapshot | undefined;
  initialCreate?: boolean;
  onBusyChange?: ((busy: boolean) => void) | undefined;
  onNavigate: Navigate;
}) {
  const [profiles, setProfiles] = useState<FleetProfileRead[]>([]);
  const [selectedNumber, setSelectedNumber] = useState<FleetProfileNumber>();
  const [draft, setDraft] = useState<ProfileDraft>();
  // The opening load reads the draft as it is when the profiles arrive, not as it was at render.
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const [editing, setEditing] = useState(initialCreate);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadingProfile, setLoadingProfile] = useState(false);
  const [preview, setPreview] = useState<FleetProfilePreview>();
  const [endpoints, setEndpoints] = useState<FleetProfileEndpoints>();
  const [pendingLoad, setPendingLoad] = useState<PendingProfileLoad>();
  const [error, setError] = useState("");
  const [optionsBySelector, setOptionsBySelector] = useState<Record<string, RecipeOption[]>>({});
  const [confirmingStops, setConfirmingStops] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const toast = useToast();
  const setNotice = (message: string) => {
    if (message) toast.info(message);
  };

  const listedProfile = profiles
    .filter(readableProfile)
    .find((profile) => sameNumber(profile.number, selectedNumber));
  // The list is the cheap overview without cache truth; the selected profile's
  // detail is read on its own and replaces the list entry once it arrives.
  // The detail is kept with the list entry it was read for, so a refreshed
  // entry (status changes without a revision bump) is never hidden by it.
  const [detail, setDetail] = useState<{ source: FleetProfile; view: FleetProfile }>();
  useEffect(() => {
    if (listedProfile === undefined) {
      setDetail(undefined);
      return;
    }
    const controller = new AbortController();
    api.profile(listedProfile.number, controller.signal).then(
      (result) => {
        if (!controller.signal.aborted && readableProfile(result))
          setDetail({ source: listedProfile, view: result });
      },
      () => undefined,
    );
    return () => controller.abort();
  }, [api, listedProfile]);
  const selectedProfile = detail?.source === listedProfile ? detail?.view : listedProfile;
  const availableFleet = useMemo(
    () => fleetEntries(selectedProfile, fleet),
    [fleet, selectedProfile],
  );
  const matchingEntries = useMemo(() => entries.filter((entry) => entry.recipe), [entries]);
  const recipeOptions = useMemo(() => {
    const options = new Map<string, string>();
    for (const entry of matchingEntries) {
      if (entry.recipe) options.set(canonicalRecipeSelector(entry.recipe), entry.title);
    }
    for (const assignment of draft?.assignments ?? []) {
      if (!options.has(assignment.recipeSelector))
        options.set(assignment.recipeSelector, assignment.recipeSelector);
    }
    return [...options.entries()].sort((left, right) => left[1].localeCompare(right[1]));
  }, [draft?.assignments, matchingEntries]);
  const draftValid =
    Boolean(draft?.name.trim()) &&
    (draft?.assignments.every(
      (assignment) => assignment.recipeSelector.trim() && assignment.sparkIds.length > 0,
    ) ??
      false);
  const draftSelectors = (draft?.assignments ?? [])
    .map((assignment) => assignment.recipeSelector.trim())
    .filter(Boolean)
    .join("\n");
  useEffect(() => {
    const controller = new AbortController();
    for (const selector of new Set(draftSelectors.split("\n").filter(Boolean))) {
      // Options are optional decoration: a failed read leaves the stored choices untouched.
      void Promise.resolve()
        .then(() => api.recipeDetail(selector, controller.signal))
        .then((detail) => {
          if (!controller.signal.aborted)
            setOptionsBySelector((current) => ({
              ...current,
              [selector]: detail.document.options ?? [],
            }));
        })
        .catch(() => undefined);
    }
    return () => controller.abort();
  }, [api, draftSelectors]);
  // The saved-profile list and the endpoint depend on the load's outcome, so a
  // terminal state refetches them instead of waiting for a page reload.
  const refreshProfiles = useCallback(
    async (signal?: AbortSignal) => {
      const result = await api.profiles(signal);
      if (signal?.aborted) return;
      setProfiles([...result.profiles].sort(loadedFirst));
      const opening = openingProfile(result.profiles);
      setSelectedNumber((current) =>
        current !== undefined &&
        result.profiles
          .filter(readableProfile)
          .some((profile) => sameNumber(profile.number, current))
          ? current
          : opening?.number,
      );
      return result;
    },
    [api],
  );
  const observer = useOperationObserver<FleetProfileApplicationView>({
    isTerminal: ended,
    onTerminal: () => {
      void refreshProfiles().catch(() => undefined);
    },
  });
  const application = observer.value;
  const setApplication = (next: FleetProfileApplicationView) => {
    if (selectedNumber === undefined) return;
    const number = selectedNumber;
    observer.start(next, (signal) => api.profileProgress(number, signal));
  };
  const selectedProfileId = selectedProfile?.id;
  const dirty = Boolean(draft && isDirty(draft, selectedProfile));
  useEffect(() => {
    if (!draft) return;
    if (dirty) unsavedDrafts.set(draftKey(draft), draft);
    else unsavedDrafts.delete(draftKey(draft));
  }, [dirty, draft]);
  const applicationRunning = Boolean(application && !ended(application));

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void refreshProfiles(controller.signal)
      .then((result) => {
        if (!result) return;
        const first = openingProfile(result.profiles);
        if (!initialCreate && first && !draftRef.current) {
          const kept = unsavedDrafts.get(formatWire(first.number));
          setDraft(kept ?? draftFromProfile(first));
          setEditing(Boolean(kept));
        }
        setError("");
      })
      .catch((value) => {
        if (!controller.signal.aborted)
          setError(value instanceof Error ? value.message : "Saved profiles are unavailable.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [refreshProfiles, initialCreate]);

  useEffect(() => {
    if (selectedNumber === undefined || selectedProfileId === undefined || editing) {
      setPreview(undefined);
      return;
    }
    const controller = new AbortController();
    void api
      .previewProfile(selectedNumber, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setPreview(result);
      })
      .catch((value) => {
        if (!controller.signal.aborted)
          setError(value instanceof Error ? value.message : "The profile preview is unavailable.");
      });
    return () => controller.abort();
  }, [api, editing, selectedNumber, selectedProfileId]);

  const loadedNumber =
    selectedProfile?.status === "loaded" && !editing ? selectedProfile.number : undefined;
  const applicationState = application?.state;
  useEffect(() => {
    setEndpoints(undefined);
    if (loadedNumber === undefined) return;
    const controller = new AbortController();
    // The Controller names the client-facing inference gateway; the Spark
    // backend address is diagnostic only and never shown as the endpoint.
    void api
      .profileEndpoints(loadedNumber, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setEndpoints(result);
      })
      .catch(() => {
        if (!controller.signal.aborted) setEndpoints(undefined);
      });
    return () => controller.abort();
  }, [api, loadedNumber, applicationState]);

  useEffect(() => {
    onBusyChange?.(saving || loadingProfile);
    return () => onBusyChange?.(false);
  }, [loadingProfile, onBusyChange, saving]);

  function selectProfile(profile: FleetProfile) {
    const kept = unsavedDrafts.get(formatWire(profile.number));
    setSelectedNumber(profile.number);
    setDraft(kept ?? draftFromProfile(profile));
    setEditing(Boolean(kept));
    setPreview(undefined);
    observer.reset();
    setPendingLoad(undefined);
    setNotice("");
  }

  function startNew() {
    setSelectedNumber(undefined);
    setDraft(unsavedDrafts.get("new") ?? blankDraft());
    setEditing(true);
    setPreview(undefined);
    observer.reset();
    setPendingLoad(undefined);
    setNotice("New profile started. Saving does not change the running fleet.");
  }
  function updateDraft(next: Partial<ProfileDraft>) {
    setDraft((current) => (current ? { ...current, ...next } : current));
    setNotice("");
  }
  function updateAssignment(key: string, next: Partial<AssignmentDraft>) {
    if (draft)
      updateDraft({
        assignments: draft.assignments.map((item) =>
          item.key === key ? { ...item, ...next } : item,
        ),
      });
  }
  function toggleSpark(key: string, sparkId: string) {
    const assignment = draft?.assignments.find((item) => item.key === key);
    if (!assignment) return;
    const sparkIds = assignment.sparkIds.includes(sparkId)
      ? assignment.sparkIds.filter((id) => id !== sparkId)
      : [...assignment.sparkIds, sparkId];
    updateAssignment(key, { sparkIds });
  }
  function addRecipe() {
    const record = matchingEntries[0];
    if (!draft || !record?.recipe) return;
    const count = record.recipe.recipe_document.topology.node_count;
    const sparkIds = availableFleet
      .filter((_item, index) => compareWire(index, count) < 0)
      .map((item) => item.id);
    const selector = canonicalRecipeSelector(record.recipe);
    const assignment: AssignmentDraft = {
      key: `assignment-${crypto.randomUUID()}`,
      recipeSelector: selector,
      assignmentName: record.recipe.slug,
      modelVariant: record.modelDocument?.identity.variant ?? "",
      desiredState: "running",
      sparkIds,
      optionChoices: {},
    };
    updateDraft({ assignments: [...draft.assignments, assignment] });
  }

  async function saveDraft() {
    if (!draft || !draftValid || saving) return;
    setSaving(true);
    setError("");
    try {
      const savedKey = draftKey(draft);
      const candidate =
        draft.number ??
        addWire(
          profiles.reduce<WireNumber>(
            (largest, profile) =>
              compareWire(profile.number, largest) > 0 ? profile.number : largest,
            0,
          ),
          1,
        );
      validateControlParameters("PUT", `/api/profile/${formatWire(candidate)}`, {
        path: { number: candidate },
      });
      if (typeof candidate !== "number")
        throw new Error("The profile number cannot be represented by the profile contract.");
      const number: FleetProfileNumber = candidate;
      const result = await api.autosaveProfile(number, inputFromDraft(draft, optionsBySelector));
      setProfiles((current) =>
        [
          ...current.filter((profile) => compareWire(profile.number, result.number) !== 0),
          result,
        ].sort(loadedFirst),
      );
      unsavedDrafts.delete(savedKey);
      setSelectedNumber(result.number);
      setDraft(draftFromProfile(result));
      setEditing(false);
      setPreview(undefined);
      toast.success(
        `Profile ${formatWire(result.number)} · ${result.name} saved. Current runs remain unchanged until load.`,
      );
    } catch (value) {
      toast.error(value instanceof Error ? value.message : "The profile could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  async function load() {
    if (selectedNumber === undefined || (!pendingLoad && !loadable(preview)) || loadingProfile)
      return;
    setLoadingProfile(true);
    setError("");
    let pending = pendingLoad;
    try {
      if (pending) {
        try {
          const existing = await api.profileApplicationByRequest(
            selectedNumber,
            pending.requestKey,
          );
          setApplication(existing);
          setPendingLoad(undefined);
          return;
        } catch (value) {
          if (!(value instanceof ApiError && value.status === 404)) throw value;
        }
      } else {
        if (!preview) {
          setPreview(await api.previewProfile(selectedNumber));
          return;
        }
        pending = {
          requestKey: crypto.randomUUID(),
          reviewedEffectsDigest: preview.effects_digest,
        };
        setPendingLoad(pending);
      }
      setApplication(await submitProfileLoad(api, selectedNumber, pending));
      setPendingLoad(undefined);
    } catch (value) {
      if (value instanceof ApiError && value.status === 409) {
        setPendingLoad(undefined);
        // The plan may have changed since it was reviewed: show the current one before another attempt.
        api.previewProfile(selectedNumber).then(setPreview, () => undefined);
      }
      toast.error(value instanceof Error ? value.message : "The profile could not be loaded.");
    } finally {
      setLoadingProfile(false);
    }
  }

  const recipeUpdates = (selectedProfile?.assignments ?? []).flatMap((assignment) =>
    assignment.recipe_update
      ? [{ selector: assignment.selector, detail: assignment.recipe_update.detail }]
      : [],
  );
  /** One rule for Load and Reload: confirm only when the plan stops a workload, else apply at once. */
  function loadOrConfirm() {
    if (preview?.next_action === ProfileLoadAction.LOAD_STOPPING) setConfirmingStops(true);
    else void load();
  }
  async function confirmedLoad() {
    await load();
    setConfirmingStops(false);
  }

  async function deleteSelected() {
    if (!selectedProfile || deleting) return;
    const { number, revision } = selectedProfile;
    setDeleting(true);
    try {
      const result = await api.deleteProfile(number, revision);
      setConfirmingDelete(false);
      if (result.outcome === "cancelling") {
        toast.info(
          "The profile's running load is being cancelled. Delete the profile again once it has ended.",
        );
        return;
      }
      unsavedDrafts.delete(formatWire(number));
      observer.reset();
      setPendingLoad(undefined);
      setPreview(undefined);
      const remaining = (await refreshProfiles())?.profiles.find(readableProfile);
      if (remaining) selectProfile(remaining);
      else {
        setSelectedNumber(undefined);
        setDraft(undefined);
        setEditing(false);
      }
      toast.success(`Profile ${formatWire(number)} deleted. Running workloads were not changed.`);
    } catch (value) {
      toast.error(value instanceof Error ? value.message : "The profile could not be deleted.");
    } finally {
      setDeleting(false);
    }
  }

  const names = Object.fromEntries(availableFleet.map((item) => [item.id, item.name]));
  return (
    <section className="library-profile-view" aria-labelledby="library-profiles-heading">
      <header className="library-subview-heading">
        <div>
          <h2 id="library-profiles-heading">Profiles</h2>
          <p>
            Save recipe choices for the whole fleet, then load the selected numbered profile when
            ready. The latest compatible cached recipes are resolved on load.
          </p>
        </div>
        <div className="library-profile-header-actions">
          <a
            className="button secondary"
            href="/library?view=models"
            onClick={(event) => onNavigate(event, "/library?view=models")}
          >
            Choose a model
          </a>
        </div>
      </header>
      {error && (
        <p className="library-profile-plain-error" role="alert">
          {error}
        </p>
      )}
      {loading && <SkeletonRows columns={2} rows={3} label="Loading saved profiles" />}
      {!loading && profiles.length === 0 && !draft && (
        <EmptyState
          title="No profiles"
          description="A profile saves which recipes run on which Sparks, so you can load them together."
          action={{ label: "New profile", onClick: startNew }}
        />
      )}
      {selectedProfile && !editing && (
        <section className="library-profile-status state-read" aria-live="polite">
          <div className="library-profile-status-summary">
            <strong>
              Profile {formatWire(selectedProfile.number)} · {selectedProfile.name}
            </strong>
            <span>
              {profileStateLabel(selectedProfile.status)} · revision{" "}
              {formatWire(selectedProfile.revision)}
            </span>
          </div>
          <span className="library-profile-status-scope">
            {(selectedProfile.fleet ?? []).length} Sparks · loaded revision{" "}
            {selectedProfile.loaded_revision == null
              ? "none"
              : formatWire(selectedProfile.loaded_revision)}
          </span>
          {(selectedProfile.warnings ?? []).length > 0 && (
            <ul>
              {(selectedProfile.warnings ?? []).map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
          {selectedProfile.maintenance_wait && (
            <span>Recovery reason: {selectedProfile.maintenance_wait.reason}</span>
          )}
          <WaitingFor
            blockers={selectedProfile.maintenance_wait?.blockers}
            nextAttemptAt={selectedProfile.maintenance_wait?.next_check}
            dependencyDeadline={selectedProfile.maintenance_wait?.dependency_deadline}
            resumeCondition={selectedProfile.maintenance_wait?.resume_condition}
          />
        </section>
      )}
      {application && (
        <ProfileProgress
          application={application}
          notice={
            applicationRunning ? (
              <ObservationNotice
                connection={observer.connection}
                lastSuccessAt={observer.lastSuccessAt}
                background={observer.background}
                onResume={observer.resume}
                subject="this profile load"
              />
            ) : undefined
          }
          cancel={
            applicationRunning && selectedNumber !== undefined
              ? async (key) =>
                  setApplication(
                    await api.cancelProfileApplication(application.id, selectedNumber, key),
                  )
              : undefined
          }
        />
      )}
      <div className="library-profile-layout">
        <aside className="library-profile-list" aria-label="Saved profiles">
          <div className="library-profile-list-heading">
            <strong>Saved profiles</strong>
            <span>{profiles.length}</span>
          </div>
          {profiles.map((profile) =>
            !readableProfile(profile) ? (
              <div
                key={formatWire(profile.number)}
                className="library-profile-list-empty"
                role="status"
              >
                <strong>Profile {formatWire(profile.number)} · Definition unavailable</strong>
                <p>{profile.projection_issue.detail}</p>
                <p>{profile.projection_issue.next_action}</p>
              </div>
            ) : (
              <button
                key={formatWire(profile.number)}
                type="button"
                className={sameNumber(profile.number, selectedNumber) ? "is-selected" : undefined}
                aria-pressed={sameNumber(profile.number, selectedNumber)}
                onClick={() => selectProfile(profile)}
              >
                <span>
                  Profile {formatWire(profile.number)} · {profile.name}
                </span>
                <small>
                  {`${profile.assignments.length} assignment${profile.assignments.length === 1 ? "" : "s"} · ${profileStateLabel(profile.status)}`}
                </small>
              </button>
            ),
          )}
          {profiles.length === 0 && (
            <div className="library-profile-list-empty">
              <strong>No saved profiles</strong>
              <p>Create a numbered profile to describe the desired fleet setup.</p>
            </div>
          )}
          <button type="button" className="library-profile-create-link" onClick={startNew}>
            + New profile
          </button>
        </aside>
        {draft && editing && (
          <div className="library-profile-editor">
            <header className="library-profile-editor-heading">
              <div>
                <span>
                  {draft.number !== undefined
                    ? `Edit profile ${formatWire(draft.number)}`
                    : "New numbered profile"}
                  {dirty && " · Unsaved changes kept"}
                </span>
                <h3>{draft.name || "Unnamed profile"}</h3>
              </div>
            </header>
            <div className="library-profile-fields">
              <label>
                <span>Profile name</span>
                <input
                  value={draft.name}
                  maxLength={120}
                  onChange={(event) => updateDraft({ name: event.target.value })}
                />
              </label>
              <label>
                <span>Retention</span>
                <select
                  value={draft.installationPolicy}
                  onChange={(event) =>
                    updateDraft({
                      installationPolicy: event.target
                        .value as FleetProfileInput["installation_policy"],
                    })
                  }
                >
                  <option value="keep-cached">Keep cached artifacts</option>
                  <option value="exact">Exact desired state</option>
                </select>
              </label>
              <label className="library-profile-wide">
                <span>Purpose</span>
                <textarea
                  value={draft.description}
                  maxLength={1000}
                  rows={2}
                  onChange={(event) => updateDraft({ description: event.target.value })}
                />
              </label>
              <label className="library-profile-favorite">
                <input
                  type="checkbox"
                  checked={draft.favorite}
                  onChange={(event) => updateDraft({ favorite: event.target.checked })}
                />
                <span>Favorite profile</span>
              </label>
            </div>
            <section className="library-profile-scope" aria-labelledby="profile-fleet-heading">
              <div className="library-profile-section-heading">
                <div>
                  <h4 id="profile-fleet-heading">Fleet and assignments</h4>
                  <p>
                    Every enrolled Spark is in the profile view. Sparks without an assignment become
                    idle when the profile loads.
                  </p>
                </div>
                <span>{availableFleet.length} Sparks</span>
              </div>
              {availableFleet.length === 0 ? (
                <p>No enrolled Sparks are visible yet.</p>
              ) : (
                <ul className="library-profile-scope-list" aria-label="Enrolled Sparks">
                  {availableFleet.map((item) => (
                    <li key={item.id}>
                      <strong>{item.name}</strong>
                      <small>{item.state}</small>
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <section className="library-profile-assignments" aria-label="Profile assignments">
              <header>
                <h4>Recipe assignments</h4>
                <button
                  type="button"
                  className="button secondary"
                  onClick={addRecipe}
                  disabled={matchingEntries.length === 0 || availableFleet.length === 0}
                >
                  Add available Recipe
                </button>
              </header>
              {draft.assignments.map((assignment) => (
                <article key={assignment.key} className="library-profile-assignment">
                  <label>
                    <span>Recipe</span>
                    <select
                      value={assignment.recipeSelector}
                      onChange={(event) =>
                        updateAssignment(assignment.key, { recipeSelector: event.target.value })
                      }
                    >
                      {recipeOptions.map(([selector, title]) => (
                        <option key={selector} value={selector}>
                          {title} · {selector}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>Assignment name</span>
                    <input
                      value={assignment.assignmentName}
                      onChange={(event) =>
                        updateAssignment(assignment.key, { assignmentName: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    <span>Model variant</span>
                    <input
                      value={assignment.modelVariant}
                      onChange={(event) =>
                        updateAssignment(assignment.key, { modelVariant: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    <span>Desired state</span>
                    <select
                      value={assignment.desiredState}
                      onChange={(event) =>
                        updateAssignment(assignment.key, {
                          desiredState: event.target.value as AssignmentInput["desired_state"],
                        })
                      }
                    >
                      <option value="running">Running</option>
                      <option value="installed">Installed</option>
                    </select>
                  </label>
                  {assignment.gpuClock && (
                    <p>
                      GPU range {formatWire(assignment.gpuClock.min_mhz)}–
                      {formatWire(assignment.gpuClock.max_mhz)} MHz
                    </p>
                  )}
                  <RecipeOptionSelects
                    idPrefix={`option-${assignment.key}`}
                    options={optionsBySelector[assignment.recipeSelector.trim()] ?? []}
                    value={assignment.optionChoices}
                    onChange={(optionChoices) => {
                      updateAssignment(assignment.key, { optionChoices });
                      if (selectedProfile?.loaded_revision)
                        setNotice(
                          "Changing recipe options saves a new profile revision; reload the profile to apply it.",
                        );
                    }}
                  />
                  <fieldset>
                    <legend>Assigned Sparks</legend>
                    {availableFleet.map((item) => (
                      <label key={item.id}>
                        <input
                          type="checkbox"
                          checked={assignment.sparkIds.includes(item.id)}
                          onChange={() => toggleSpark(assignment.key, item.id)}
                        />
                        <span>{names[item.id] ?? item.id}</span>
                      </label>
                    ))}
                  </fieldset>
                  <button
                    type="button"
                    className="button danger"
                    onClick={() =>
                      updateDraft({
                        assignments: draft.assignments.filter(
                          (item) => item.key !== assignment.key,
                        ),
                      })
                    }
                  >
                    Remove assignment
                  </button>
                </article>
              ))}
              {draft.assignments.length === 0 && (
                <p>All Sparks will be idle when this profile loads.</p>
              )}
            </section>
            <footer className="library-profile-editor-footer">
              <button
                type="button"
                className="button secondary"
                onClick={() => {
                  if (selectedProfile) {
                    setDraft(draftFromProfile(selectedProfile));
                    setEditing(false);
                  } else setDraft(undefined);
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                className="button"
                disabled={!draftValid || saving}
                onClick={() => void saveDraft()}
              >
                {saving ? "Saving profile…" : "Save profile"}
              </button>
            </footer>
          </div>
        )}
        {selectedProfile && !editing && recipeUpdates.length > 0 && (
          <section className="library-profile-update" aria-label="Recipe updates available">
            <ul>
              {recipeUpdates.map((update) => (
                <li key={update.selector}>
                  <StatusPill tone="info">update available</StatusPill> {update.detail}
                </li>
              ))}
            </ul>
            <button
              type="button"
              className="button secondary"
              disabled={
                loadingProfile || applicationRunning || (!loadable(preview) && !pendingLoad)
              }
              onClick={loadOrConfirm}
            >
              Reload profile
            </button>
          </section>
        )}
        {confirmingStops && selectedProfile && (
          <ConfirmDialog
            title="Load this profile?"
            consequence={`Loading stops ${stoppedAliases(preview).join(", ") || "running workloads"}; requests to ${stoppedAliases(preview).length === 1 ? "it" : "them"} are interrupted. Nothing stops until you confirm.`}
            confirmLabel="Load profile"
            command={`vonkctl --profile ${formatWire(selectedProfile.number)} profile load`}
            busy={loadingProfile}
            onConfirm={() => void confirmedLoad()}
            onCancel={() => setConfirmingStops(false)}
          />
        )}
        {confirmingDelete && selectedProfile && (
          <ConfirmDialog
            title="Delete this profile?"
            consequence="Removes the saved profile and its load history. Running workloads are not stopped or changed. The number can be reused."
            confirmLabel="Delete profile"
            command={`vonkctl --profile ${formatWire(selectedProfile.number)} profile delete`}
            busy={deleting}
            onConfirm={() => void deleteSelected()}
            onCancel={() => setConfirmingDelete(false)}
          />
        )}
        {selectedProfile && !editing && (
          <section
            className="library-profile-saved"
            aria-label={`Profile ${formatWire(selectedProfile.number)} saved profile`}
          >
            <header>
              <h3>{selectedProfile.name}</h3>
              <div className="library-profile-actions">
                {nothingToChange(preview) && !pendingLoad ? (
                  <small className="library-profile-stops">Loaded, nothing to change</small>
                ) : (
                  <button
                    type="button"
                    className="button compact"
                    disabled={
                      loadingProfile || applicationRunning || (!loadable(preview) && !pendingLoad)
                    }
                    onClick={loadOrConfirm}
                  >
                    {loadingProfile ? "Loading…" : "Load profile"}
                  </button>
                )}
                <button
                  type="button"
                  className="button compact secondary"
                  onClick={() => setEditing(true)}
                >
                  Edit profile
                </button>
                <ProfileExport api={api} number={selectedProfile.number} />
                <button
                  type="button"
                  className="button compact quiet"
                  onClick={() => setConfirmingDelete(true)}
                >
                  Delete profile
                </button>
                {preview && !nothingToChange(preview) && (
                  <small className="library-profile-stops">
                    {stoppedAliases(preview).length > 0
                      ? `Will stop: ${stoppedAliases(preview).join(", ")}`
                      : "Will stop: nothing"}
                  </small>
                )}
              </div>
            </header>
            <dl>
              <div>
                <dt>Number</dt>
                <dd>{formatWire(selectedProfile.number)}</dd>
              </div>
              <div>
                <dt>Revision</dt>
                <dd>{formatWire(selectedProfile.revision)}</dd>
              </div>
              <div>
                <dt>Cache</dt>
                <dd>{cacheSummaryText(selectedProfile.cache_summary)}</dd>
              </div>
            </dl>
            <ul>
              {selectedProfile.assignments.map((assignment) => (
                <li key={assignment.selector}>
                  <strong>{assignment.display_name}</strong>{" "}
                  <span>
                    {assignment.recipe_selector} ·{" "}
                    {assignment.spark_ids.map((id) => names[id] ?? id).join(" + ")} ·{" "}
                    {assignment.observed_state}
                  </span>
                </li>
              ))}
              {selectedProfile.assignments.length === 0 && (
                <li>
                  <strong>Idle fleet</strong> <span aria-hidden="true">·</span>{" "}
                  <span>No assignments; every Spark becomes idle on load.</span>
                </li>
              )}
            </ul>
            {(endpoints?.assignments ?? [])
              .filter((item) => item.state === "published" && item.endpoint)
              .map((item) => (
                <section
                  key={item.assignment_id}
                  className="library-profile-endpoint"
                  aria-label={`${item.recipe_title} client endpoint`}
                >
                  <h4>Client endpoint</h4>
                  <dl>
                    <div>
                      <dt>Base URL</dt>
                      <dd>
                        <code>{item.endpoint?.api_base}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>Model</dt>
                      <dd>
                        <code>{item.alias}</code>
                      </dd>
                    </div>
                  </dl>
                  {item.endpoint?.api_base && item.alias && (
                    <ConnectionExamples apiBase={item.endpoint.api_base} alias={item.alias} />
                  )}
                  <details>
                    <summary>Spark backend (diagnostic)</summary>
                    <p>{item.endpoint?.backend_api_base}</p>
                  </details>
                </section>
              ))}
            {preview && <ProfileReview preview={preview} />}
            {(selectedProfile.next_actions ?? []).length > 0 && (
              <p>{(selectedProfile.next_actions ?? []).join(" · ")}</p>
            )}
          </section>
        )}
        {!draft && !loading && profiles.length > 0 && (
          <div className="library-profile-empty-editor">
            <h3>Select or create a profile</h3>
            <button type="button" className="button" onClick={startNew}>
              New profile
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
