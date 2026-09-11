import {useCallback, useEffect, useRef, useState} from "react";
import type {ControlApi} from "../api/types";

/**
 * Refresh every cached recipe image.
 *
 * `vonkctl recipe update --all` requires an explicit scope, and the Controller
 * refuses a request that combines or omits the scope, so this action states
 * "all" rather than leaving the scope implicit.
 */
export function LibraryRecipeUpdateAction({api, onUpdated}: {api: ControlApi; onUpdated(): void}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const abort = useRef<AbortController | undefined>(undefined);

  useEffect(() => () => abort.current?.abort(), []);

  const update = useCallback(async () => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api.updateRecipes(true, [], crypto.randomUUID(), controller.signal);
      if (controller.signal.aborted) return;
      setBusy(false);
      const count = result.updates.length;
      setMessage(`Refreshing ${count} cached recipe${count === 1 ? "" : "s"}`);
      onUpdated();
    } catch (value) {
      if (controller.signal.aborted) return;
      setBusy(false);
      setError(value instanceof Error ? value.message.slice(0, 256) : "Recipe update failed");
    }
  }, [api, onUpdated]);

  return <div className="library-cache-action">
    <button type="button" className="button secondary" disabled={busy} onClick={() => void update()}>
      {busy ? "Updating…" : "Update cached recipes"}
    </button>
    {message && <span role="status">{message}</span>}
    {error && <span className="library-cache-error" role="alert">{error}</span>}
  </div>;
}
