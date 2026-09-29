import {useCallback, useEffect, useRef, useState} from "react";
import type {ControlApi, RecipeImageAvailabilityResponse} from "../api/types";
import {failureNotice} from "../lib/error-display";
import {CancelOperation} from "./cancel-operation";
import {useToast} from "./toast";
import {WaitingFor} from "./waiting-for";

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled"]);
const POLL_INTERVAL_MS = 1_000;
const MAX_POLL_ATTEMPTS = 180;

function failureText(response: RecipeImageAvailabilityResponse): string {
  const failure = response.failure;
  if (!failure) return "Recipe download did not complete";
  return `${failure.code}: ${failure.detail}`.slice(0, 256);
}

function progressLabel(response: RecipeImageAvailabilityResponse): string {
  const pending = (response.children ?? []).filter(child => child.state !== "succeeded").length;
  if (pending > 0) return `${response.progress.phase} · ${pending} model cache child${pending === 1 ? "" : "ren"}`;
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
export function LibraryRecipeDownloadAction({api, selector, missingModels, onDownloaded}: {
  api: ControlApi;
  selector: string;
  missingModels: string[];
  onDownloaded(): void;
}) {
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState("");
  const [operationId, setOperationId] = useState("");
  const [error, setError] = useState("");
  const [waiting, setWaiting] = useState<Pick<RecipeImageAvailabilityResponse, "blockers" | "next_attempt_at">>({});
  const abort = useRef<AbortController | undefined>(undefined);
  const toast = useToast();

  useEffect(() => () => abort.current?.abort(), []);

  const download = useCallback(async () => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    setPhase("queued");
    const requestKey = crypto.randomUUID();
    try {
      const accepted = await api.downloadRecipe(selector, requestKey, controller.signal);
      toast.info("Recipe download queued.");
      setPhase(progressLabel(accepted));
      setWaiting(accepted);
      setOperationId(accepted.id);
      let current = accepted;
      let attempts = 0;
      while (!TERMINAL_STATES.has(current.state) && attempts < MAX_POLL_ATTEMPTS) {
        await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
        if (controller.signal.aborted) return;
        const next = await api.recipeCacheOperation(current.id, controller.signal);
        if (!("kind" in next) || next.kind !== "recipe.image.availability.v2" || next.id !== current.id) {
          setBusy(false);
          setError("Recipe download returned an unexpected operation shape");
          toast.error(failureNotice("Recipe download returned an unexpected operation shape", requestKey));
          return;
        }
        current = next;
        attempts += 1;
        setPhase(progressLabel(current));
        setWaiting(current);
      }
      if (controller.signal.aborted) return;
      setBusy(false);
      if (current.state === "succeeded") {
        setPhase("");
        toast.success("Recipe downloaded.");
        onDownloaded();
        return;
      }
      const failed = current.state === "cancelled" ? "Download cancelled. Partial files are kept; download again to resume." : failureText(current);
      setError(failed);
      if (current.state !== "cancelled") toast.error(failureNotice(failed, requestKey));
    } catch (value) {
      if (controller.signal.aborted) return;
      setBusy(false);
      const failed = value instanceof Error ? value.message.slice(0, 256) : "Recipe download failed";
      setError(failed);
      toast.error(failureNotice(failed, requestKey));
    }
  }, [api, onDownloaded, selector, toast]);

  const label = missingModels.length > 0
    ? `Download recipe and ${missingModels.length} missing model${missingModels.length === 1 ? "" : "s"}`
    : "Download recipe";

  return <div className="library-cache-action">
    <button type="button" className="button secondary" disabled={busy} onClick={() => void download()}>
      {busy ? "Downloading…" : label}
    </button>
    {missingModels.length > 0 && <span className="library-cache-missing">Also caches {missingModels.join(", ")}</span>}
    {(busy || phase) && <span role="status">{phase}</span>}
    {busy && <WaitingFor blockers={waiting.blockers} nextAttemptAt={waiting.next_attempt_at}/>}
    {busy && operationId && <CancelOperation what="download" consequence="Stops this download. Partial files are kept and the download resumes if you start it again." command={`vonkctl recipe cancel ${operationId}`} cancel={key => api.cancelRecipeOperation(operationId, key)}/>}
    {error && <span className="library-cache-error" role="alert">{error}</span>}
  </div>;
}
