import { useCallback, useEffect, useRef, useState } from "react";
import type { CacheRemovalReview, ControlApi } from "../../api/types";
import { failureNotice } from "../../lib/error-display";
import { useToast } from "../ui/toast";
import { CacheRemovalProgress } from "../cache-removal/cache-removal-progress";
import {
  CacheRemovalOutcomeUnknown,
  submitReviewedRemoval,
  validateRemovalReview,
  type CacheRemovalIntent,
  type CacheRemovalReceipt,
} from "../cache-removal/cache-removal-operations";
import { CacheRemovalReviewDetails } from "../cache-removal/cache-removal-review";

/** One click opens the Controller's review; Confirm accepts exactly that review, like a model's. */
export function LibraryRecipeRemoveAction({
  api,
  selector,
  onRemoved,
}: {
  api: ControlApi;
  selector: string;
  onRemoved(): void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [removal, setRemoval] = useState<{
    intent: Extract<CacheRemovalIntent, { kind: "recipe" }>;
    initial: CacheRemovalReceipt | null;
    initialError?: string;
  } | null>(null);
  const [review, setReview] = useState<CacheRemovalReview | null>(null);
  const [busy, setBusy] = useState(false);
  const [withModel, setWithModel] = useState(false);
  const [error, setError] = useState("");
  const abort = useRef<AbortController | undefined>(undefined);
  const toast = useToast();

  useEffect(() => {
    setReview(null);
    setRemoval(null);
    setConfirming(false);
    setWithModel(false);
    setBusy(false);
    setError("");
    return () => abort.current?.abort();
  }, [api, selector]);

  const reviewRemoval = useCallback(
    async (withModel: boolean) => {
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;
      setBusy(true);
      setReview(null);
      setError("");
      try {
        const current = await api.recipeRemovalReview(selector, withModel, controller.signal);
        if (controller.signal.aborted) return;
        validateRemovalReview(current, { kind: "recipe", selector, withModel });
        setReview(current);
      } catch (value) {
        if (!controller.signal.aborted)
          setError(value instanceof Error ? value.message : "Recipe removal review failed");
      } finally {
        if (!controller.signal.aborted) setBusy(false);
      }
    },
    [api, selector],
  );

  const remove = useCallback(async () => {
    if (!review?.submittable || typeof review.with_model !== "boolean") return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    const intent: Extract<CacheRemovalIntent, { kind: "recipe" }> = {
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
        toast.success("Recipe removed from the cache.");
        onRemoved();
        return;
      }
      toast.info("Recipe removal queued.");
      setRemoval({ intent, initial: result });
    } catch (value) {
      if (controller.signal.aborted) return;
      setBusy(false);
      setReview(null);
      if (value instanceof CacheRemovalOutcomeUnknown) {
        setConfirming(false);
        setRemoval({ intent, initial: null, initialError: value.message });
      } else {
        const failed =
          value instanceof Error
            ? value.message
            : "Recipe removal failed; review again before retrying.";
        setError(failed);
        toast.error(failureNotice(failed, intent.requestKey));
      }
    }
  }, [api, onRemoved, review, selector, toast]);

  if (removal)
    return (
      <CacheRemovalProgress
        api={api}
        intent={removal.intent}
        initial={removal.initial}
        initialError={removal.initialError}
        onComplete={() => {
          setRemoval(null);
          onRemoved();
        }}
        onDismiss={() => setRemoval(null)}
        onRejected={(message) => {
          setRemoval(null);
          setError(message);
        }}
      />
    );

  if (!confirming) {
    return (
      <div className="library-cache-action">
        <button
          type="button"
          className="button secondary compact"
          disabled={busy}
          onClick={() => {
            setConfirming(true);
            void reviewRemoval(false);
          }}
        >
          Remove recipe
        </button>
        {error && (
          <span className="library-cache-error" role="alert">
            {error}
          </span>
        )}
      </div>
    );
  }
  return (
    <div className="library-cache-action">
      <label>
        <input
          type="checkbox"
          checked={withModel}
          disabled={busy}
          onChange={(event) => {
            setWithModel(event.target.checked);
            void reviewRemoval(event.target.checked);
          }}
        />{" "}
        Also remove unused models
      </label>
      {busy && <span role="status">Waiting for Controller…</span>}
      {!busy && !review && (
        <button
          type="button"
          className="button secondary compact"
          onClick={() => void reviewRemoval(withModel)}
        >
          Review again
        </button>
      )}
      {review && (
        <>
          <CacheRemovalReviewDetails review={review} />
          <button
            type="button"
            className="button secondary compact"
            disabled={busy || !review.submittable}
            onClick={() => void remove()}
          >
            Confirm remove
          </button>
        </>
      )}
      <button
        type="button"
        className="button secondary compact"
        disabled={busy}
        onClick={() => {
          setConfirming(false);
          setReview(null);
          setWithModel(false);
        }}
      >
        Cancel
      </button>
      {error && (
        <span className="library-cache-error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
