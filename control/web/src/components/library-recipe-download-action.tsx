import {useCallback, useEffect, useRef, useState} from "react";
import type {ControlApi, RecipeImageAvailabilityResponse} from "../api/types";

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
  const [error, setError] = useState("");
  const abort = useRef<AbortController | undefined>(undefined);

  useEffect(() => () => abort.current?.abort(), []);

  const download = useCallback(async () => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    setPhase("queued");
    try {
      const accepted = await api.downloadRecipe(selector, crypto.randomUUID(), controller.signal);
      setPhase(progressLabel(accepted));
      let current = accepted;
      let attempts = 0;
      while (!TERMINAL_STATES.has(current.state) && attempts < MAX_POLL_ATTEMPTS) {
        await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
        if (controller.signal.aborted) return;
        const next = await api.recipeCacheOperation(current.id, controller.signal);
        if (!("children" in next)) {
          setBusy(false);
          setError("Recipe download returned an unexpected operation shape");
          return;
        }
        current = next;
        attempts += 1;
        setPhase(progressLabel(current));
      }
      if (controller.signal.aborted) return;
      setBusy(false);
      if (current.state === "succeeded") {
        setPhase("");
        onDownloaded();
        return;
      }
      setError(failureText(current));
    } catch (value) {
      if (controller.signal.aborted) return;
      setBusy(false);
      setError(value instanceof Error ? value.message.slice(0, 256) : "Recipe download failed");
    }
  }, [api, onDownloaded, selector]);

  const label = missingModels.length > 0
    ? `Download recipe and ${missingModels.length} missing model${missingModels.length === 1 ? "" : "s"}`
    : "Download recipe";

  return <div className="library-cache-action">
    <button type="button" className="button secondary" disabled={busy} onClick={() => void download()}>
      {busy ? "Downloading…" : label}
    </button>
    {missingModels.length > 0 && <span className="library-cache-missing">Also caches {missingModels.join(", ")}</span>}
    {(busy || phase) && <span role="status">{phase}</span>}
    {error && <span className="library-cache-error" role="alert">{error}</span>}
  </div>;
}
