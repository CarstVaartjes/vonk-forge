import {availabilityFailure, availabilityRetryable, LibraryAvailabilityFeedback} from "./library-availability-feedback";
import {availabilityProgress, LibraryAvailabilityProgress} from "./library-availability-progress";
import type {RecipeImageAvailabilityOperation} from "../api/types";

export type AvailabilityMemberPresentation = {
  key: "model-cache" | "runtime-image";
  id?: string;
  artifactSetSha256?: string;
  planDigest?: string;
  label?: string;
  state: string;
  progress: unknown;
  failure?: unknown;
};

/** UI-only view model. The generated Controller response is adapted at the API boundary. */
export type AvailabilityOperationPresentation = {
  id: string;
  requestId: string;
  recipeRevisionId: string;
  state: string;
  attempt: number;
  progress: unknown;
  members: readonly AvailabilityMemberPresentation[];
  failure?: unknown;
  result?: unknown;
  runtimeMode: "image" | "build";
  updatedAt?: string;
};

/** Adapt the generated aggregate response at the UI boundary. */
export function recipeAvailabilityPresentation(operation: RecipeImageAvailabilityOperation): AvailabilityOperationPresentation {
  const members = (operation.children ?? []).map(child => ({
    key: child.kind,
    id: child.id,
    artifactSetSha256: child.artifact_set_sha256 ?? undefined,
    planDigest: child.plan_digest ?? undefined,
    label: child.kind === "model-cache" ? "Model files" : "Runtime image",
    state: child.state,
    progress: child.progress,
    failure: child.failure ?? undefined,
  } satisfies AvailabilityMemberPresentation));
  const runtimeImage = members.find(member => member.key === "runtime-image");
  return {
    id: operation.id,
    requestId: operation.request_id,
    recipeRevisionId: operation.recipe_revision_id,
    state: operation.state,
    attempt: operation.attempt,
    progress: operation.progress,
    members,
    failure: operation.failure ?? undefined,
    result: operation.result ?? undefined,
    runtimeMode: operation.result?.build_id || runtimeImage?.progress.phase === "build" ? "build" : "image",
    updatedAt: operation.updated_at,
  };
}

const terminalStates = new Set(["succeeded", "failed", "cancelled"]);

export function selectAvailabilityOperation(operations: readonly AvailabilityOperationPresentation[], recipeRevisionId: string): AvailabilityOperationPresentation | undefined {
  return [...operations]
    .filter(operation => operation.recipeRevisionId === recipeRevisionId)
    .sort((left, right) => Number(terminalStates.has(left.state)) - Number(terminalStates.has(right.state)) || right.attempt - left.attempt || (right.updatedAt ?? "").localeCompare(left.updatedAt ?? ""))[0];
}

function stateLabel(state: string): string {
  return state.replace(/[-_]+/g, " ").replace(/\b\w/g, value => value.toUpperCase());
}

function digestSummary(result: unknown): string[] {
  if (typeof result !== "object" || result === null || Array.isArray(result)) return [];
  const record = result as Record<string, unknown>;
  return ["artifact_set_sha256", "model_digest", "image_digest", "oci_layout_sha256"]
    .flatMap(key => typeof record[key] === "string" ? [`${key}: ${record[key]}`] : []);
}

export function LibraryAvailabilityOperation({modelAccessUrl, onCheckAccessAndResume, onForce, onMakeAvailable, onRetry, operation}: {
  modelAccessUrl?: string;
  onCheckAccessAndResume?(member: AvailabilityMemberPresentation): void;
  onForce?(): void;
  onMakeAvailable?(): void;
  onRetry?(): void;
  operation: AvailabilityOperationPresentation;
}) {
  const active = !terminalStates.has(operation.state);
  const failure = operation.failure ? availabilityFailure(operation, "Recipe availability failed.") : undefined;
  const retryable = Boolean(failure && availabilityRetryable(operation));
  const forceLabel = operation.runtimeMode === "build" ? "Rebuild image" : "Download image again";
  const resultDetails = digestSummary(operation.result);
  return <section className="library-availability-operation" aria-label="Recipe availability operation">
    <header className="library-availability-operation-heading"><div><strong>{operation.state === "succeeded" ? "Available on NAS" : active ? "Preparing exact Recipe on NAS" : "Recipe availability needs attention"}</strong><small>Revision {operation.recipeRevisionId} · attempt {operation.attempt}</small></div><span>{stateLabel(operation.state)}</span></header>
    <LibraryAvailabilityProgress progress={availabilityProgress(operation.progress)}/>
    <ol className="library-availability-members" aria-label="Availability members">
      {operation.members.map(member => <li key={member.key} className={`library-availability-member state-${member.state}`} data-member-kind={member.key}><div><strong>{member.label ?? (member.key === "model-cache" ? "Model files" : "Runtime image")}</strong><span>{stateLabel(member.state)}</span></div><LibraryAvailabilityProgress progress={availabilityProgress(member.progress)}/>{member.failure !== undefined && <LibraryAvailabilityFeedback failure={availabilityFailure(member.failure, `${member.label ?? "Availability member"} failed.`)} modelAccessUrl={modelAccessUrl} onCheckAccessAndResume={onCheckAccessAndResume ? () => onCheckAccessAndResume(member) : undefined} onRetry={onRetry && availabilityRetryable(member.failure) ? onRetry : undefined} retryLabel="Retry member"/>}</li>)}
    </ol>
    {failure !== undefined && <LibraryAvailabilityFeedback failure={failure} modelAccessUrl={modelAccessUrl} onCheckAccessAndResume={onCheckAccessAndResume && operation.members.find(member => member.key === "model-cache") ? () => onCheckAccessAndResume(operation.members.find(member => member.key === "model-cache")!) : undefined} onRetry={retryable ? onRetry : undefined} retryLabel="Retry availability"/>}
    {resultDetails.length > 0 && <details className="library-availability-receipt"><summary>Verified receipt</summary><ul>{resultDetails.map(detail => <li key={detail}><code>{detail}</code></li>)}</ul></details>}
    <div className="library-availability-operation-actions">{onMakeAvailable && <button type="button" className="button secondary" disabled={active} onClick={onMakeAvailable}>{active ? "Preparing…" : operation.state === "succeeded" ? "Available on NAS" : "Make available"}</button>}{onForce && <details><summary>More actions</summary><p>Reuses the exact selected Model files; only the runtime image is refreshed.</p><button type="button" className="button secondary" onClick={onForce}>{forceLabel}</button></details>}</div>
  </section>;
}
