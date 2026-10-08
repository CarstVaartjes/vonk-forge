import { useCallback, useRef, useState } from "react";
import { ContractViolation } from "../api/errors";
import { LifecycleState } from "../api/vocabulary.generated";
import type { ControlApi, RecipeImageAvailabilityResponse } from "../api/types";
import { failureNotice } from "../lib/error-display";
import { CancelOperation } from "./cancel-operation";
import { useOperationObserver } from "../hooks/use-operation-observer";
import { ObservationNotice } from "./observation-notice";
import { useToast } from "./toast";
import { WaitingFor } from "./waiting-for";

const TERMINAL_STATES = new Set<string>([
  LifecycleState.SUCCEEDED,
  LifecycleState.FAILED,
  LifecycleState.CANCELLED,
]);

function failureText(response: RecipeImageAvailabilityResponse): string {
  const failure = response.failure;
  if (!failure) return "Recipe download did not complete";
  return `${failure.code}: ${failure.detail}`.slice(0, 256);
}

function progressLabel(response: RecipeImageAvailabilityResponse): string {
  if (!response.progress) return "Progress unavailable";
  const pending = (response.children ?? []).filter(
    (child) => child.state !== LifecycleState.SUCCEEDED,
  ).length;
  if (pending > 0)
    return `${response.progress.phase} · ${pending} model cache child${pending === 1 ? "" : "ren"}`;
  return response.progress.phase;
}

/**
 * Cache a recipe image together with the model artifacts it needs.
 *
 * The Controller download always includes the recipe's missing model
 * artifacts, exactly as `vonkctl recipe download` describes it ("Cache a recipe
 * and missing model"), so the offer is stated before the operator commits
 * rather than discovered afterwards.
 */
export function LibraryRecipeDownloadAction({
  api,
  selector,
  missingModels,
  onDownloaded,
}: {
  api: ControlApi;
  selector: string;
  missingModels: string[];
  onDownloaded(): void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const requestKey = useRef("");
  const toast = useToast();
  const observer = useOperationObserver<RecipeImageAvailabilityResponse>({
    isTerminal: (operation) => !operation.residue && TERMINAL_STATES.has(operation.state),
    // Terminal: refetch the library and cache state that depends on this download.
    onTerminal: (operation) => {
      if (operation.state === LifecycleState.SUCCEEDED) {
        toast.success("Recipe downloaded.");
        onDownloaded();
      } else if (operation.state !== LifecycleState.CANCELLED)
        toast.error(failureNotice(failureText(operation), requestKey.current));
    },
  });
  const operation = observer.value;
  const running = Boolean(operation && !TERMINAL_STATES.has(operation.state));
  const observing = Boolean(operation && (operation.residue || running));
  const busy = submitting || observing;
  const phase =
    operation && operation.state !== LifecycleState.SUCCEEDED
      ? progressLabel(operation)
      : submitting
        ? "queued"
        : "";
  const error =
    submitError ||
    observer.fatal ||
    (operation?.state === LifecycleState.CANCELLED
      ? "Download cancelled. Partial files are kept; download again to resume."
      : operation?.state === LifecycleState.FAILED
        ? failureText(operation)
        : "");

  const download = useCallback(async () => {
    observer.reset();
    setSubmitting(true);
    setSubmitError("");
    const key = crypto.randomUUID();
    requestKey.current = key;
    try {
      const accepted = await api.downloadRecipe(selector, key);
      toast.info("Recipe download queued.");
      observer.start(accepted, async (signal) => {
        const next = await api.recipeCacheOperation(accepted.id, signal);
        if (!("kind" in next) || next.kind !== accepted.kind || next.id !== accepted.id) {
          throw new ContractViolation("GET", `/api/recipe/operations/${accepted.id}`, 200);
        }
        return next;
      });
    } catch (value) {
      const failed =
        value instanceof Error ? value.message.slice(0, 256) : "Recipe download failed";
      setSubmitError(failed);
      toast.error(failureNotice(failed, key));
    } finally {
      setSubmitting(false);
    }
  }, [api, observer, selector, toast]);

  const label =
    missingModels.length > 0
      ? `Download recipe and ${missingModels.length} missing model${missingModels.length === 1 ? "" : "s"}`
      : "Download recipe";

  return (
    <div className="library-cache-action">
      <button
        type="button"
        className="button secondary"
        disabled={busy}
        onClick={() => void download()}
      >
        {operation?.residue ? "Observing…" : busy ? "Downloading…" : label}
      </button>
      {missingModels.length > 0 && (
        <span className="library-cache-missing">Also caches {missingModels.join(", ")}</span>
      )}
      {(busy || phase) && <span role="status">{phase}</span>}
      {operation?.residue && (
        <span role="status">
          Stored operation evidence is unavailable: {operation.residue.reason}.
        </span>
      )}
      {observing && (
        <ObservationNotice
          connection={observer.connection}
          lastSuccessAt={observer.lastSuccessAt}
          background={observer.background}
          onResume={observer.resume}
          subject="this download"
        />
      )}
      {running && operation && (
        <WaitingFor blockers={operation.blockers} nextAttemptAt={operation.next_attempt_at} />
      )}
      {running && operation && (
        <CancelOperation
          what="download"
          consequence="Stops this download. Partial files are kept and the download resumes if you start it again."
          command={`vonkctl recipe cancel ${operation.id}`}
          cancel={(key) => api.cancelRecipeOperation(operation.id, key)}
        />
      )}
      {error && (
        <span className="library-cache-error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
