import {useEffect, useRef, useState} from "react";
import type {ControlApi, ModelCacheOperatorResponse, RecipeOperatorResponse} from "../api/types";

type RemovalReceipt = ModelCacheOperatorResponse | RecipeOperatorResponse;

/** An accepted removal remains an observable operation until its owner settles. */
export function CacheRemovalProgress({api, kind, initial, onComplete, onDismiss}: {
  api: ControlApi;
  kind: "model" | "recipe";
  initial: RemovalReceipt;
  onComplete(): void;
  onDismiss(): void;
}) {
  const [receipt, setReceipt] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const abort = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => abort.current?.abort(), []);

  async function refresh() {
    if (!initial.operation_id) return;
    const controller = new AbortController();
    abort.current?.abort();
    abort.current = controller;
    setBusy(true);
    setError("");
    try {
      const current = kind === "model"
        ? await api.modelCacheOperation(initial.operation_id, controller.signal)
        : await api.recipeCacheOperation(initial.operation_id, controller.signal);
      if (controller.signal.aborted) return;
      if (!("action" in current) || current.action !== "remove" || !("operation_id" in current) || current.operation_id !== initial.operation_id || current.request_key !== initial.request_key || current.review_digest !== initial.review_digest || current.selector !== initial.selector
        || ("with_model" in initial && (!("with_model" in current) || current.with_model !== initial.with_model || current.recipe_revision_id !== initial.recipe_revision_id))
        || ("model_content_sha256" in initial && (!("model_content_sha256" in current) || current.model_content_sha256 !== initial.model_content_sha256))) {
        throw new Error("Controller returned a different removal operation.");
      }
      setReceipt(current);
      if (current.state === "succeeded") onComplete();
    } catch (value) {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Removal status unavailable");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return <div className="library-cache-action" aria-label="Cache removal progress">
    <p role="status">Removal {receipt.state}: {receipt.operation_id}</p>
    {receipt.progress?.phase && <p>{receipt.progress.phase}</p>}
    {receipt.failure && <p role="alert">{receipt.failure.code}: {receipt.failure.detail}</p>}
    <button type="button" className="button secondary" disabled={busy} onClick={() => void refresh()}>Refresh removal status</button>
    {(receipt.state === "failed" || receipt.state === "cancelled") && <button type="button" className="button secondary" disabled={busy} onClick={onDismiss}>Dismiss result</button>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
