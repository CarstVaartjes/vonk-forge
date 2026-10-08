import { useCallback, useEffect, useMemo, useState } from "react";
import type { MouseEvent, ReactNode } from "react";
import {
  addWire,
  compareWire,
  displayRatio,
  formatWire,
  isWireNumber,
  materialize,
  parseContractJson,
} from "../api/contract-numeric";
import type { WireNumber } from "../api/contract-numeric";
import { validateControlParameters } from "../api/contract-json";
import { ApiError } from "../api/client";
import { canonicalRecipeSelector, readableProfile } from "../api/types";
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
} from "../api/types";
import { formatBytes, nodeDisplayName } from "../lib/fleet";
import type { LibraryRecipeRecord } from "./library-workcell";
import { CancelOperation } from "./cancel-operation";
import { ConfirmDialog } from "./confirm-dialog";
import { StatusPill } from "./status-pill";
import { WaitingFor } from "./waiting-for";
import { ObservationNotice } from "./observation-notice";
import { useOperationObserver } from "../hooks/use-operation-observer";
import { EmptyState } from "./empty-state";
import { ProfileExport } from "./profile-export";
import { SkeletonRows } from "./skeleton";
import { useToast } from "./toast";
import { RecipeOptionSelects, effectiveChoices } from "./recipe-option-selects";
import type { OptionChoices, RecipeOption } from "./recipe-option-selects";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
type AssignmentInput = NonNullable<FleetProfileInput["assignments"]>[number];
type AssignmentDraft = {
  key: string;
  recipeSelector: string;
  assignmentName: string;
  modelVariant: string;
  desiredState: AssignmentInput["desired_state"];
  sparkIds: string[];
  optionChoices: OptionChoices;
};
type ProfileDraft = {
  number?: FleetProfileNumber;
  revision?: FleetProfile["revision"];
  name: string;
  description: string;
  favorite: boolean;
  installationPolicy: FleetProfileInput["installation_policy"];
  labels: FleetProfileInput["labels"];
  assignments: AssignmentDraft[];
};
type FleetEntry = { id: string; name: string; state: string };
type PendingProfileLoad = { requestKey: string; reviewedEffectsDigest: string };

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled", "superseded"]);
/** A superseded load that names its successor is not an end: the latest-load observation continues under the successor. */
const ended = (application: Pick<FleetProfileApplicationView, "state" | "superseded_by">) =>
  TERMINAL_STATES.has(application.state) &&
  !(application.state === "superseded" && application.superseded_by);

function stringField(value: unknown, key: string): string {
  if (!value || typeof value !== "object") return "";
  const field = (value as Record<string, unknown>)[key];
  return typeof field === "string" ? field : "";
}

function profileAssignments(profile: FleetProfile): AssignmentDraft[] {
  return (profile.definition.assignments ?? []).map((assignment, index) => ({
    key: `${assignment.recipe_selector}-${index}`,
    recipeSelector: assignment.recipe_selector,
    assignmentName: assignment.assignment_name ?? "",
    modelVariant: assignment.model_variant ?? "",
    desiredState: assignment.desired_state,
    sparkIds: [...assignment.spark_ids].sort(),
    optionChoices: { ...(assignment.option_choices ?? {}) },
  }));
}

function draftFromProfile(profile: FleetProfile): ProfileDraft {
  const definition = profile.definition;
  return {
    number: profile.number,
    revision: profile.revision,
    name: definition.name,
    description: definition.description,
    favorite: definition.favorite,
    installationPolicy: definition.installation_policy,
    labels: definition.labels,
    assignments: profileAssignments(profile),
  };
}

function blankDraft(): ProfileDraft {
  return {
    name: "New fleet profile",
    description: "A complete desired setup for the fleet.",
    favorite: false,
    installationPolicy: "keep-cached",
    labels: {},
    assignments: [],
  };
}

/**
 * Unsaved edits are kept per profile for the session, so selecting another
 * profile or navigating away never silently discards them.
 */
const unsavedDrafts = new Map<string, ProfileDraft>();
/** Test seam: forget every kept unsaved draft. */
export const forgetUnsavedProfileDrafts = () => unsavedDrafts.clear();
const draftKey = (draft: Pick<ProfileDraft, "number">) =>
  draft.number === undefined ? "new" : formatWire(draft.number);
const comparable = (draft: ProfileDraft) =>
  JSON.stringify({
    ...draft,
    number: draft.number === undefined ? null : formatWire(draft.number),
    revision: draft.revision === undefined ? null : formatWire(draft.revision),
    assignments: draft.assignments.map(({ key: _key, ...rest }) => rest),
  });
function isDirty(draft: ProfileDraft, profile: FleetProfile | undefined): boolean {
  if (draft.number === undefined)
    return draft.assignments.length > 0 || comparable(draft) !== comparable(blankDraft());
  return !profile || comparable(draft) !== comparable(draftFromProfile(profile));
}
const requestedProfile = (): FleetProfileNumber | undefined => {
  const token = new URLSearchParams(location.search).get("profile");
  if (!token || !/^[1-9][0-9]*$/.test(token)) return undefined;
  const value = materialize(parseContractJson(token));
  validateControlParameters("GET", `/api/profile/${token}`, { path: { number: value } });
  if (typeof value !== "number")
    throw new Error("The profile number cannot be represented by the profile contract.");
  return value;
};
const sameNumber = (left: WireNumber, right: WireNumber | undefined) =>
  right !== undefined && compareWire(left, right) === 0;

function fleetEntries(
  profile: FleetProfile | undefined,
  fleet: VisualFleetSnapshot | undefined,
): FleetEntry[] {
  if (profile)
    return (profile.fleet ?? []).flatMap((item) => {
      const id = stringField(item, "selector");
      if (!id) return [];
      return [
        {
          id,
          name: stringField(item, "display_name") || id,
          state: stringField(item, "state") || "Unknown",
        },
      ];
    });
  return (
    fleet?.nodes.map((node) => ({ id: node.id, name: nodeDisplayName(node), state: "Observed" })) ??
    []
  );
}

function inputFromDraft(
  draft: ProfileDraft,
  optionsBySelector: Record<string, RecipeOption[]> = {},
): FleetProfileInput {
  const input: FleetProfileInput = {
    name: draft.name,
    description: draft.description,
    expected_revision: draft.revision ?? 0,
    installation_policy: draft.installationPolicy,
    labels: draft.labels,
    favorite: draft.favorite,
    assignments: draft.assignments.map((assignment) => ({
      recipe_selector: assignment.recipeSelector.trim(),
      spark_ids: [...new Set(assignment.sparkIds)].sort(),
      assignment_name: assignment.assignmentName.trim() || undefined,
      model_variant: assignment.modelVariant.trim() || undefined,
      desired_state: assignment.desiredState,
      // Every option the recipe declares is sent explicitly, defaults included.
      option_choices: optionsBySelector[assignment.recipeSelector.trim()]
        ? effectiveChoices(
            optionsBySelector[assignment.recipeSelector.trim()],
            assignment.optionChoices,
          )
        : assignment.optionChoices,
    })),
  };
  return input;
}

function applicationProgressRecord(
  application: FleetProfileApplicationView,
): Record<string, unknown> {
  const progress = application.progress as Record<string, unknown>;
  const child = progress.child_progress;
  return child && typeof child === "object" ? (child as Record<string, unknown>) : progress;
}

function isAmbiguousLoadFailure(error: unknown): boolean {
  return error instanceof TypeError || (error instanceof ApiError && error.status >= 500);
}

async function submitProfileLoad(
  api: ControlApi,
  number: FleetProfileNumber,
  pending: PendingProfileLoad,
): Promise<FleetProfileApplicationView> {
  // The load names the effects the operator reviewed, so a plan that changed since
  // is refused (409) instead of silently accepted. A retry sends the identical body.
  const input = {
    request_key: pending.requestKey,
    review: { effects_digest: pending.reviewedEffectsDigest },
  };
  try {
    return await api.loadProfile(number, input);
  } catch (error) {
    if (!isAmbiguousLoadFailure(error)) throw error;
    try {
      return await api.profileApplicationByRequest(number, pending.requestKey);
    } catch (lookupError) {
      if (!(lookupError instanceof ApiError && lookupError.status === 404)) throw error;
    }
    return api.loadProfile(number, input);
  }
}

/** A load is offered when it can be admitted now, or once the Controller has prepared what it lacks. */
const loadable = (preview: FleetProfilePreview | undefined) =>
  preview?.allowed === true || preview?.waits_for_preparation === true;

const bytes = (value: unknown) =>
  isWireNumber(value) && compareWire(value, 0) >= 0 ? formatBytes(value) : undefined;

function ProfileProgress({
  application,
  cancel,
  notice,
}: {
  application: FleetProfileApplicationView;
  cancel?(requestKey: string): Promise<unknown>;
  notice?: ReactNode;
}) {
  const progress = applicationProgressRecord(application);
  const completed = bytes(progress.bytes);
  const total = bytes(progress.total_bytes);
  const value =
    isWireNumber(progress.bytes) &&
    isWireNumber(progress.total_bytes) &&
    compareWire(progress.total_bytes, 0) > 0
      ? Math.min(100, Math.max(0, displayRatio(progress.bytes, progress.total_bytes) * 100))
      : undefined;
  const phase =
    typeof progress.phase === "string" ? progress.phase.replaceAll("-", " ") : "Profile load";
  const nodeIds = Array.isArray(progress.node_ids)
    ? progress.node_ids.filter((id): id is string => typeof id === "string")
    : [];
  return (
    <section
      className={`library-profile-application state-${application.state}`}
      aria-live="polite"
      aria-label="Profile load progress"
    >
      <div className="library-profile-application-heading">
        <div>
          <strong>{phase}</strong>
          <span>{application.state.replaceAll("-", " ")}</span>
        </div>
        {completed && (
          <span>
            {completed}
            {total ? ` of ${total}` : ""}
          </span>
        )}
      </div>
      <div
        className={`library-profile-application-progress${value === undefined ? " is-indeterminate" : ""}`}
        role="progressbar"
        aria-label="Profile load progress"
        aria-valuemin={0}
        aria-valuemax={100}
        {...(value === undefined
          ? { "aria-valuetext": "Progress total unavailable" }
          : { "aria-valuenow": value })}
      >
        <span style={value === undefined ? undefined : { transform: `scaleX(${value / 100})` }} />
      </div>
      {nodeIds.length > 0 && (
        <ul className="library-profile-application-members" aria-label="Profile load targets">
          {nodeIds.map((nodeId) => (
            <li key={nodeId}>
              <span>{nodeId}</span>
              <small>Participating</small>
            </li>
          ))}
        </ul>
      )}
      {application.status_reason && <p>{application.status_reason}</p>}
      {application.state === "superseded" && (
        <p className="library-profile-application-superseded">
          Not a failure:{" "}
          {application.superseded_by ? (
            <>
              this load continues as application <code>{application.superseded_by}</code>.
            </>
          ) : (
            "a newer profile intent replaced this load; review it again."
          )}
        </p>
      )}
      <WaitingFor blockers={application.blockers} nextAttemptAt={application.next_attempt_at} />
      {notice}
      {cancel && (
        <CancelOperation
          what="load"
          consequence="Stops this profile load and reconciles what it already changed. Sparks may be left partly changed until you load again."
          command={`vonkctl profile cancel ${application.id}`}
          cancel={cancel}
        />
      )}
    </section>
  );
}

const SUMMARY_LABELS: [keyof FleetProfilePreview["summary"], string][] = [
  ["starts", "start"],
  ["stops", "stop"],
  ["installs", "install"],
  ["uninstalls", "uninstall"],
  ["builds", "build"],
  ["distributions", "distribute"],
  ["placements", "place"],
];

/** Review changes: what applying will do, blockers first; selectors and digests stay in the detail. */
function ProfileReview({ preview }: { preview: FleetProfilePreview }) {
  const counts = SUMMARY_LABELS.filter(
    ([key]) => compareWire(preview.summary?.[key] ?? 0, 0) > 0,
  ).map(([key, label]) => `${formatWire(preview.summary[key])} ${label}`);
  return (
    <section className="library-profile-review" aria-label="Review changes">
      <h4>Review changes</h4>
      {preview.waits_for_preparation && !preview.allowed && (
        <p className="library-profile-plain-note">
          {[
            "The Controller prepares this first, then loads:",
            ...(preview.preparation_steps ?? []).map((step) => step.label),
          ].join(" · ")}
        </p>
      )}
      {!loadable(preview) && preview.reasons[0] && (
        <p className="library-profile-plain-error" role="alert">
          {preview.reasons[0].detail}
        </p>
      )}
      {(preview.reasons ?? []).slice(loadable(preview) ? 0 : 1).map((reason) => (
        <p key={`${reason.code}:${reason.detail}`} className="library-profile-plain-note">
          {reason.detail}
        </p>
      ))}
      {(preview.effects.adopted ?? []).map((effect) => (
        <p key={effect.application_id} className="library-profile-plain-note">
          Existing work on {effect.node_ids.length}{" "}
          {effect.node_ids.length === 1 ? "Spark" : "Sparks"} continues; its progress is preserved.
        </p>
      ))}
      {(preview.steps ?? []).length > 0 ? (
        <ol className="library-profile-review-steps">
          {preview.steps.map((step) => (
            <li key={formatWire(step.index)}>{step.label}</li>
          ))}
        </ol>
      ) : (preview.effects.adopted ?? []).length > 0 ? (
        <p>The load completes when continuing work finishes.</p>
      ) : (
        <p>Nothing changes: every Spark already matches this profile.</p>
      )}
      {counts.length > 0 && <p className="library-profile-review-summary">{counts.join(" · ")}</p>}
      <details>
        <summary>Technical detail</summary>
        <dl>
          <div>
            <dt>Plan digest</dt>
            <dd>
              <code>{preview.plan_digest}</code>
            </dd>
          </div>
          <div>
            <dt>Profile digest</dt>
            <dd>
              <code>{preview.profile_digest}</code>
            </dd>
          </div>
        </dl>
        <ul>
          {(preview.resolved_assignments ?? []).map((assignment, index) => (
            <li key={index}>
              <strong>{assignment.recipe_title}</strong> ·{" "}
              <code>
                {assignment.recipe_id}@{assignment.recipe_revision_id}
              </code>{" "}
              on {(assignment.nodes ?? []).map((node) => node.node_id).join(", ")}
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

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
  fleet?: VisualFleetSnapshot;
  initialCreate?: boolean;
  onBusyChange?(busy: boolean): void;
  onNavigate: Navigate;
}) {
  const [profiles, setProfiles] = useState<FleetProfileRead[]>([]);
  const [selectedNumber, setSelectedNumber] = useState<FleetProfileNumber>();
  const [draft, setDraft] = useState<ProfileDraft>();
  const [editing, setEditing] = useState(initialCreate);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadingProfile, setLoadingProfile] = useState(false);
  const [preview, setPreview] = useState<FleetProfilePreview>();
  const [endpoints, setEndpoints] = useState<FleetProfileEndpoints>();
  const [pendingLoad, setPendingLoad] = useState<PendingProfileLoad>();
  const [error, setError] = useState("");
  const [optionsBySelector, setOptionsBySelector] = useState<Record<string, RecipeOption[]>>({});
  const [confirmingReload, setConfirmingReload] = useState(false);
  const toast = useToast();
  const setNotice = (message: string) => {
    if (message) toast.info(message);
  };

  const selectedProfile = profiles
    .filter(readableProfile)
    .find((profile) => sameNumber(profile.number, selectedNumber));
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
      setProfiles(
        [...result.profiles].sort((left, right) => compareWire(left.number, right.number)),
      );
      const preferred = requestedProfile();
      setSelectedNumber((current) =>
        current !== undefined &&
        result.profiles
          .filter(readableProfile)
          .some((profile) => sameNumber(profile.number, current))
          ? current
          : (result.profiles
              .filter(readableProfile)
              .find((profile) => sameNumber(profile.number, preferred))?.number ??
            result.profiles.find(readableProfile)?.number),
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
        const first =
          result.profiles
            .filter(readableProfile)
            .find((profile) => sameNumber(profile.number, requestedProfile())) ??
          result.profiles.find(readableProfile);
        if (!initialCreate && first && !draft) {
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
    if (selectedNumber === undefined || !selectedProfile || editing) {
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
  }, [api, editing, selectedNumber, selectedProfile?.id]);

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
    setNotice("Draft profile created. Saving does not change the running fleet.");
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
        ].sort((left, right) => compareWire(left.number, right.number)),
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
  async function reload() {
    await load();
    setConfirmingReload(false);
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
          <button type="button" className="button secondary" onClick={startNew}>
            Create profile
          </button>
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
          action={{ label: "Create profile", onClick: startNew }}
        />
      )}
      {selectedProfile && !editing && (
        <section className="library-profile-status state-read" aria-live="polite">
          <div className="library-profile-status-summary">
            <strong>
              Profile {formatWire(selectedProfile.number)} · {selectedProfile.name}
            </strong>
            <span>
              {selectedProfile.status.replaceAll("-", " ")} · revision{" "}
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
                  {profile.assignments.length} assignment
                  {profile.assignments.length === 1 ? "" : "s"} ·{" "}
                  {profile.status.replaceAll("-", " ")}
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
              <div className="library-profile-scope-list" role="list" aria-label="Enrolled Sparks">
                {availableFleet.map((item) => (
                  <div key={item.id} role="listitem">
                    <strong>{item.name}</strong>
                    <small>{item.state}</small>
                  </div>
                ))}
                {availableFleet.length === 0 && <p>No enrolled Sparks are visible yet.</p>}
              </div>
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
                {saving
                  ? "Saving profile…"
                  : draft.number !== undefined
                    ? "Save profile"
                    : "Create profile"}
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
              onClick={() => setConfirmingReload(true)}
            >
              Reload profile
            </button>
          </section>
        )}
        {confirmingReload && selectedProfile && (
          <ConfirmDialog
            title="Reload this profile?"
            consequence="Reloads the profile with the newest recipe revisions. Running workloads that use an older revision restart to apply it; nothing restarts until you confirm."
            confirmLabel="Reload profile"
            command={`vonkctl --profile ${formatWire(selectedProfile.number)} profile load`}
            busy={loadingProfile}
            onConfirm={() => void reload()}
            onCancel={() => setConfirmingReload(false)}
          />
        )}
        {selectedProfile && !editing && (
          <section
            className="library-profile-saved"
            aria-label={`Profile ${formatWire(selectedProfile.number)} saved profile`}
          >
            <header>
              <div>
                <span>Saved profile</span>
                <h3>{selectedProfile.name}</h3>
              </div>
              <div>
                <ProfileExport api={api} number={selectedProfile.number} />
                <button type="button" className="button secondary" onClick={() => setEditing(true)}>
                  Edit profile
                </button>
                <button
                  type="button"
                  className="button"
                  disabled={
                    loadingProfile || applicationRunning || (!loadable(preview) && !pendingLoad)
                  }
                  onClick={() => void load()}
                >
                  {loadingProfile ? "Applying…" : "Apply profile"}
                </button>
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
                  <strong>{assignment.display_name}</strong>
                  <span>
                    {assignment.recipe_selector} ·{" "}
                    {assignment.spark_ids.map((id) => names[id] ?? id).join(" + ")} ·{" "}
                    {assignment.observed_state}
                  </span>
                </li>
              ))}
              {selectedProfile.assignments.length === 0 && (
                <li>
                  <strong>Idle fleet</strong>
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
                    <div>
                      <dt>Spark backend (diagnostic)</dt>
                      <dd>{item.endpoint?.backend_api_base}</dd>
                    </div>
                  </dl>
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
              Create profile
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
