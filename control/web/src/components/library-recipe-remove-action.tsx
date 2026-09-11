import {useCallback, useEffect, useRef, useState} from "react";
import type {ControlApi} from "../api/types";

/**
 * Remove a recipe from the Controller cache.
 *
 * The model decision is explicit, mirroring the CLI's mandatory --keep-model or
 * --with-model. The Controller refuses a removal without that choice rather
 * than guessing, so the web asks the same question.
 */
export function LibraryRecipeRemoveAction({api, selector, onRemoved}: {
  api: ControlApi;
  selector: string;
  onRemoved(): void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const abort = useRef<AbortController | undefined>(undefined);

  useEffect(() => () => abort.current?.abort(), []);

  const remove = useCallback(async (withModel: boolean) => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    try {
      const result = await api.removeRecipe(selector, crypto.randomUUID(), withModel, controller.signal);
      if (controller.signal.aborted) return;
      setBusy(false);
      setConfirming(false);
      if (result.state === "failed" || result.state === "cancelled") {
        setError(`${result.selector}: recipe removal did not complete`.slice(0, 256));
        return;
      }
      onRemoved();
    } catch (value) {
      if (controller.signal.aborted) return;
      setBusy(false);
      setError(value instanceof Error ? value.message.slice(0, 256) : "Recipe removal failed");
    }
  }, [api, onRemoved, selector]);

  if (!confirming) {
    return <div className="library-cache-action">
      <button type="button" className="button secondary" onClick={() => setConfirming(true)}>Remove recipe</button>
    </div>;
  }
  return <div className="library-cache-action">
    <button type="button" className="button secondary" disabled={busy} onClick={() => void remove(false)}>
      {busy ? "Removing…" : "Keep the model"}
    </button>
    <button type="button" className="button secondary" disabled={busy} onClick={() => void remove(true)}>
      {busy ? "Removing…" : "Remove the model too"}
    </button>
    <button type="button" className="button secondary" disabled={busy} onClick={() => setConfirming(false)}>Cancel</button>
    <span className="library-cache-missing">A model shared with another cached recipe or saved profile is kept.</span>
    {error && <span className="library-cache-error" role="alert">{error}</span>}
  </div>;
}
