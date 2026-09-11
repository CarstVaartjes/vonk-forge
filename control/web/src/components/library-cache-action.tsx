import {useCallback, useEffect, useRef, useState} from "react";
import type {ControlApi, ModelCacheOperatorResponse} from "../api/types";

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled"]);
const POLL_INTERVAL_MS = 1_000;
const MAX_POLL_ATTEMPTS = 180;

export type LibraryCacheState = "cached" | "preparing" | "not_cached" | "failed" | "unknown";

function progressLabel(response: ModelCacheOperatorResponse): string {
  const total = response.total_bytes ?? 0;
  const transferred = response.transferred_bytes ?? 0;
  if (total > 0 && transferred > 0) {
    const percent = Math.min(100, Math.round((transferred / total) * 100));
    return `${response.phase} · ${percent}%`;
  }
  return response.phase;
}

function failureText(response: ModelCacheOperatorResponse): string {
  const failure = response.failure;
  if (!failure) return "Cache preparation did not complete";
  return `${failure.code}: ${failure.detail}`.slice(0, 256);
}

/**
 * Prepare the Controller cache entry for one exact model.
 *
 * This is the operator-facing half of the NAS cache contract: the cache is the
 * authority for what a profile may place, so an uncached model needs an
 * explicit, observable preparation action rather than a silent fetch later.
 */
export function LibraryCacheAction({api, selector, state, onPrepared}: {
  api: ControlApi;
  selector: string;
  state: LibraryCacheState;
  onPrepared(): void;
}) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [phase, setPhase] = useState("");
  const [error, setError] = useState("");
  const abort = useRef<AbortController | undefined>(undefined);

  useEffect(() => () => abort.current?.abort(), []);

  const prepare = useCallback(async () => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    setPhase("queued");
    try {
      let current = await api.prepareModelCache(selector, crypto.randomUUID(), controller.signal);
      let attempts = 0;
      while (!TERMINAL_STATES.has(current.state) && current.operation_id && attempts < MAX_POLL_ATTEMPTS) {
        await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
        if (controller.signal.aborted) return;
        current = await api.modelCacheOperation(current.operation_id, controller.signal);
        attempts += 1;
        setPhase(progressLabel(current));
      }
      if (controller.signal.aborted) return;
      setBusy(false);
      if (current.state === "succeeded") {
        setPhase("");
        onPrepared();
        return;
      }
      setError(failureText(current));
    } catch (value) {
      if (controller.signal.aborted) return;
      setBusy(false);
      setError(value instanceof Error ? value.message.slice(0, 256) : "Cache preparation failed");
    }
  }, [api, onPrepared, selector]);

  const remove = useCallback(async () => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    try {
      const result = await api.removeModelCache(selector, crypto.randomUUID(), controller.signal);
      if (controller.signal.aborted) return;
      setBusy(false);
      setConfirming(false);
      if (result.state === "failed" || result.state === "cancelled") {
        setError(failureText(result));
        return;
      }
      onPrepared();
    } catch (value) {
      if (controller.signal.aborted) return;
      setBusy(false);
      setError(value instanceof Error ? value.message.slice(0, 256) : "Cache removal failed");
    }
  }, [api, onPrepared, selector]);

  if (state === "cached") {
    // Removal is destructive, so it takes an explicit second action rather
    // than a single click, matching the CLI's mandatory --yes.
    return <div className="library-cache-action">
      <span className="library-cache-state is-ready">Cached</span>
      {confirming
        ? <>
            <button type="button" className="button secondary" disabled={busy} onClick={() => void remove()}>
              {busy ? "Removing…" : "Confirm remove"}
            </button>
            <button type="button" className="button secondary" disabled={busy} onClick={() => setConfirming(false)}>Cancel</button>
          </>
        : <button type="button" className="button secondary" onClick={() => setConfirming(true)}>Remove from cache</button>}
      {confirming && <span className="library-cache-missing">Referenced profiles and running workloads keep the entry.</span>}
      {error && <span className="library-cache-error" role="alert">{error}</span>}
    </div>;
  }
  return <div className="library-cache-action">
    <button type="button" className="button secondary" disabled={busy || state === "preparing"} onClick={() => void prepare()}>
      {busy || state === "preparing" ? "Downloading…" : state === "failed" ? "Retry download" : "Download model"}
    </button>
    {(busy || state === "preparing") && <span role="status">{phase || "queued"}</span>}
    {error && <span className="library-cache-error" role="alert">{error}</span>}
  </div>;
}
