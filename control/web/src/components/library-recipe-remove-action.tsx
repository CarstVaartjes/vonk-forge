import {useCallback, useEffect, useRef, useState} from "react";
import type {CacheRemovalReview, ControlApi} from "../api/types";
import {CacheRemovalProgress} from "./cache-removal-progress";
import {
  CacheRemovalOutcomeUnknown,
  submitReviewedRemoval,
  validateRemovalReview,
  type CacheRemovalIntent,
  type CacheRemovalReceipt,
} from "./cache-removal-operations";
import {CacheRemovalReviewDetails} from "./cache-removal-review";

/** Choose retention, inspect the Controller decision, then accept that review. */
export function LibraryRecipeRemoveAction({api, selector, onRemoved}: {
  api: ControlApi;
  selector: string;
  onRemoved(): void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [removal, setRemoval] = useState<{
    intent: Extract<CacheRemovalIntent, {kind: "recipe"}>;
    initial: CacheRemovalReceipt | null;
    initialError?: string;
  } | null>(null);
  const [review, setReview] = useState<CacheRemovalReview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const abort = useRef<AbortController | undefined>(undefined);

  useEffect(() => {
    setReview(null);
    setRemoval(null);
    setConfirming(false);
    setBusy(false);
    setError("");
    return () => abort.current?.abort();
  }, [api, selector]);

  const reviewRemoval = useCallback(async (withModel: boolean) => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setReview(null);
    setError("");
    try {
      const current = await api.recipeRemovalReview(selector, withModel, controller.signal);
      if (controller.signal.aborted) return;
      validateRemovalReview(current, {kind: "recipe", selector, withModel});
      setReview(current);
    } catch (value) {
      if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Recipe removal review failed");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }, [api, selector]);

  const remove = useCallback(async () => {
    if (!review || review.blockers.length || typeof review.with_model !== "boolean") return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    const intent: Extract<CacheRemovalIntent, {kind: "recipe"}> = {
      kind: "recipe",
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
        onRemoved();
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
        setError(value instanceof Error ? value.message : "Recipe removal failed; review again before retrying.");
      }
    }
  }, [api, onRemoved, review, selector]);

  if (removal) return <CacheRemovalProgress api={api} intent={removal.intent} initial={removal.initial}
    initialError={removal.initialError}
    onComplete={() => {setRemoval(null); onRemoved();}}
    onDismiss={() => setRemoval(null)}
    onRejected={message => {setRemoval(null); setError(message);}}/>;

  if (!confirming) {
    return <div className="library-cache-action">
      <button type="button" className="button secondary" onClick={() => setConfirming(true)}>Remove recipe</button>
      {error && <span className="library-cache-error" role="alert">{error}</span>}
    </div>;
  }
  return <div className="library-cache-action">
    <button type="button" className="button secondary" disabled={busy} onClick={() => void reviewRemoval(false)}>Keep the model</button>
    <button type="button" className="button secondary" disabled={busy} onClick={() => void reviewRemoval(true)}>Remove the model too</button>
    {busy && <span role="status">Waiting for Controller…</span>}
    {review && <>
      <CacheRemovalReviewDetails review={review}/>
      <button type="button" className="button secondary" disabled={busy || review.blockers.length > 0} onClick={() => void remove()}>Confirm remove</button>
    </>}
    <button type="button" className="button secondary" disabled={busy} onClick={() => {setConfirming(false); setReview(null);}}>Cancel</button>
    {error && <span className="library-cache-error" role="alert">{error}</span>}
  </div>;
}
