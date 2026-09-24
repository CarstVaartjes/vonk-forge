import {useCallback, useEffect, useRef, useState} from "react";
import type {CacheRemovalReview, ControlApi, ModelCacheOperatorResponse} from "../api/types";

import {CacheRemovalProgress} from "./cache-removal-progress";
import {
  CacheRemovalOutcomeUnknown,
  submitReviewedRemoval,
  validateRemovalReview,
  type CacheRemovalIntent,
  type CacheRemovalReceipt,
} from "./cache-removal-operations";
import {CacheRemovalReviewDetails} from "./cache-removal-review";

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
export function LibraryCacheAction({api, selector, modelContentSha256, state, onPrepared}: {
  api: ControlApi;
  selector: string;
  modelContentSha256: string;
  state: LibraryCacheState;
  onPrepared(): void;
}) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [removal, setRemoval] = useState<{
    intent: Extract<CacheRemovalIntent, {kind: "model"}>;
    initial: CacheRemovalReceipt | null;
    initialError?: string;
  } | null>(null);
  const [review, setReview] = useState<CacheRemovalReview | null>(null);
  const [phase, setPhase] = useState("");
  const [error, setError] = useState("");
  const abort = useRef<AbortController | undefined>(undefined);

  useEffect(() => {
    setReview(null);
    setRemoval(null);
    setConfirming(false);
    setBusy(false);
    setError("");
    return () => abort.current?.abort();
  }, [api, selector, modelContentSha256]);

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

  const reviewRemoval = useCallback(async () => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setConfirming(true);
    setReview(null);
    setBusy(true);
    setError("");
    try {
      const current = await api.modelRemovalReview(selector, controller.signal);
      if (controller.signal.aborted) return;
      validateRemovalReview(current, {kind: "model", selector, modelContentSha256});
      setReview(current);
    } catch (value) {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Cache removal review failed");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }, [api, modelContentSha256, selector]);

  const remove = useCallback(async () => {
    if (!review || review.blockers.length || review.target_identity !== modelContentSha256) return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    const intent: Extract<CacheRemovalIntent, {kind: "model"}> = {
      kind: "model",
      selector,
      requestKey: crypto.randomUUID(),
      review,
    };
    try {
      const result = await submitReviewedRemoval(api, intent, controller.signal);
      if (controller.signal.aborted) return;
      setBusy(false);
      setConfirming(false);
      setReview(null);
      if (result.state === "succeeded") {
        onPrepared();
        return;
      }
      setRemoval({intent, initial: result});
    } catch (value) {
      if (controller.signal.aborted) return;
      setBusy(false);
      setReview(null);
      if (value instanceof CacheRemovalOutcomeUnknown) {
        setConfirming(false);
        setRemoval({intent, initial: null, initialError: value.message});
      } else {
        setError(value instanceof Error ? value.message : "Cache removal failed; review again before retrying.");
      }
    }
  }, [api, modelContentSha256, onPrepared, review, selector]);

  if (removal) return <CacheRemovalProgress api={api} intent={removal.intent} initial={removal.initial}
    initialError={removal.initialError}
    onComplete={() => {setRemoval(null); onPrepared();}}
    onDismiss={() => setRemoval(null)}
    onRejected={message => {setRemoval(null); setError(message);}}/>;

  if (state === "cached") {
    // Confirmation accepts exactly the Controller review shown here.
    return <div className="library-cache-action">
      <span className="library-cache-state is-ready">Cached</span>
      {confirming
        ? <>
            <button type="button" className="button secondary" disabled={busy || !review || review.blockers.length > 0} onClick={() => void remove()}>
              Confirm remove
            </button>
            <button type="button" className="button secondary" disabled={busy} onClick={() => setConfirming(false)}>Cancel</button>
          </>
        : <button type="button" className="button secondary" disabled={busy} onClick={() => void reviewRemoval()}>Remove from cache</button>}
      {confirming && busy && <span role="status">Waiting for Controller…</span>}
      {confirming && review && <CacheRemovalReviewDetails review={review}/> }
      {confirming && !review && !busy && <button type="button" className="button secondary" onClick={() => void reviewRemoval()}>Review again</button>}
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
