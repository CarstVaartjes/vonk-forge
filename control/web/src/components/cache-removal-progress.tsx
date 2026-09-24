import {useEffect, useRef, useState} from "react";
import type {ControlApi} from "../api/types";
import {
  findRemovalRequest,
  isDefiniteRemovalRefusal,
  retryReviewedRemoval,
  validateRemovalReceipt,
  type CacheRemovalIntent,
  type CacheRemovalReceipt,
} from "./cache-removal-operations";
import {CacheRemovalReviewDetails} from "./cache-removal-review";

/** An accepted removal remains an observable operation until its owner settles. */
export function CacheRemovalProgress({api, intent, initial, initialError, onComplete, onDismiss, onRejected}: {
  api: ControlApi;
  intent: CacheRemovalIntent;
  initial: CacheRemovalReceipt | null;
  initialError?: string;
  onComplete(): void;
  onDismiss(): void;
  onRejected(message: string): void;
}) {
  const [receipt, setReceipt] = useState<CacheRemovalReceipt | null>(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError ?? "");
  const [requestMissing, setRequestMissing] = useState(initial === null);
  const abort = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => abort.current?.abort(), []);

  function accept(current: CacheRemovalReceipt) {
    setReceipt(current);
    setRequestMissing(false);
    setError("");
    if (current.state === "succeeded") onComplete();
  }

  async function refresh() {
    const controller = new AbortController();
    abort.current?.abort();
    abort.current = controller;
    setBusy(true);
    setError("");
    try {
      if (!receipt) {
        const found = await findRemovalRequest(api, intent, controller.signal);
        if (controller.signal.aborted) return;
        if (found === null) {
          setRequestMissing(true);
          setError("");
          return;
        }
        accept(found);
        return;
      }
      const operationId = receipt.operation_id;
      if (!operationId) throw new Error("Removal receipt has no operation identity.");
      const current = intent.kind === "model"
        ? await api.modelCacheOperation(operationId, controller.signal)
        : await api.recipeCacheOperation(operationId, controller.signal);
      if (controller.signal.aborted) return;
      accept(validateRemovalReceipt(intent, current, operationId));
    } catch (value) {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Removal status unavailable");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function retrySameRequest() {
    const controller = new AbortController();
    abort.current?.abort();
    abort.current = controller;
    setBusy(true);
    setError("");
    try {
      const current = await retryReviewedRemoval(api, intent, controller.signal);
      if (controller.signal.aborted) return;
      accept(current);
    } catch (value) {
      if (controller.signal.aborted) return;
      if (isDefiniteRemovalRefusal(value)) {
        onRejected(value instanceof Error ? value.message : "Controller refused the reviewed removal.");
      } else {
        setError(value instanceof Error ? value.message : "Removal outcome remains unknown; check the request again.");
        setRequestMissing(false);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return <div className="library-cache-action" aria-label="Cache removal progress">
    <CacheRemovalReviewDetails review={intent.review}/>
    {receipt
      ? <p role="status">Removal {receipt.state}: {receipt.operation_id}</p>
      : <p role="status">Removal receipt not yet confirmed: {intent.requestKey}</p>}
    {receipt?.progress?.phase && <p>{receipt.progress.phase}</p>}
    {receipt?.failure && <p role="alert">{receipt.failure.code}: {receipt.failure.detail}</p>}
    <button type="button" className="button secondary" disabled={busy} onClick={() => void refresh()}>{receipt ? "Refresh removal status" : "Check request status"}</button>
    {!receipt && requestMissing && <button type="button" className="button secondary" disabled={busy} onClick={() => void retrySameRequest()}>Retry same reviewed request</button>}
    {receipt && (receipt.state === "failed" || receipt.state === "cancelled") && <button type="button" className="button secondary" disabled={busy} onClick={onDismiss}>Dismiss result</button>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
