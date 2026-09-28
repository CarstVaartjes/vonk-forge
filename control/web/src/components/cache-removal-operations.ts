import {ApiError} from "../api/client";
import type {
  CacheRemovalReview,
  ControlApi,
  ModelCacheOperatorResponse,
  RecipeOperatorResponse,
} from "../api/types";

export type CacheRemovalIntent =
  | {kind: "model"; selector: string; requestKey: string; review: CacheRemovalReview}
  | {kind: "recipe"; selector: string; requestKey: string; review: CacheRemovalReview};
export type CacheRemovalReviewTarget =
  | {kind: "model"; selector: string; modelContentSha256: string}
  | {kind: "recipe"; selector: string; withModel: boolean};

export type CacheRemovalReceipt = ModelCacheOperatorResponse | RecipeOperatorResponse;

export class CacheRemovalOutcomeUnknown extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CacheRemovalOutcomeUnknown";
  }
}

export function sameSelector(left: string, right: string): boolean {
  return left.trim().toLowerCase() === right.trim().toLowerCase();
}

export function validateRemovalReview(
  review: CacheRemovalReview,
  target: CacheRemovalReviewTarget,
): void {
  if (
    review.resource_kind !== target.kind
    || !sameSelector(review.selector, target.selector)
    || review.action !== "remove"
  ) {
    throw new Error("Cache removal review identifies another selector or resource.");
  }
  if (target.kind === "model") {
    if (
      review.with_model !== null
      || review.target_identity !== target.modelContentSha256
      || !/^[0-9a-f]{64}$/.test(review.target_identity)
    ) {
      throw new Error("Model removal review does not match the selected content identity.");
    }
    return;
  }
  if (
    review.with_model !== target.withModel
    || !review.target_identity
  ) {
    throw new Error("Recipe removal review does not match the selected retention choice.");
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function validateRemovalReceipt(
  intent: CacheRemovalIntent,
  value: unknown,
  expectedOperationId?: string,
): CacheRemovalReceipt {
  if (!isRecord(value)) {
    throw new Error("Controller returned a malformed removal receipt.");
  }
  if (expectedOperationId !== undefined && value.operation_id !== expectedOperationId) {
    throw new Error("Controller returned a different removal operation.");
  }
  const commonMatches =
    value.action === "remove"
    && typeof value.selector === "string"
    && sameSelector(value.selector, intent.selector)
    && value.request_key === intent.requestKey
    && typeof value.operation_id === "string"
    && value.operation_id.length > 0;
  const targetMatches = intent.kind === "model"
    ? value.model_content_sha256 === intent.review.target_identity
    : value.recipe_revision_id === intent.review.target_identity
      && value.with_model === intent.review.with_model;
  if (!commonMatches || !targetMatches) {
    throw new Error("Controller returned a removal receipt for a different reviewed intent.");
  }
  return value as unknown as CacheRemovalReceipt;
}

export function isDefiniteRemovalRefusal(error: unknown): boolean {
  // A timed-out response cannot establish whether the owner accepted the POST.
  return error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408;
}

export async function findRemovalRequest(
  api: ControlApi,
  intent: CacheRemovalIntent,
  signal?: AbortSignal,
): Promise<CacheRemovalReceipt | null> {
  try {
    const result = intent.kind === "model"
      ? await api.modelCacheRequest(intent.requestKey, signal)
      : await api.recipeCacheRequest(intent.requestKey, signal);
    return validateRemovalReceipt(intent, result);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export async function submitReviewedRemoval(
  api: ControlApi,
  intent: CacheRemovalIntent,
  signal?: AbortSignal,
): Promise<CacheRemovalReceipt> {
  try {
    const result = await postRemoval(api, intent, signal);
    return validateRemovalReceipt(intent, result);
  } catch (error) {
    if (signal?.aborted) throw error;
    if (error instanceof ApiError && error.status === 409) {
      const accepted = await findRemovalRequest(api, intent, signal);
      if (accepted) return accepted;
      const current = intent.kind === "model"
        ? await api.modelRemovalReview(intent.selector, signal)
        : await api.recipeRemovalReview(
            intent.selector,
            intent.review.with_model as boolean,
            signal,
          );
      validateRemovalReview(current, intent.kind === "model"
        ? {kind: "model", selector: intent.selector, modelContentSha256: intent.review.target_identity}
        : {kind: "recipe", selector: intent.selector, withModel: intent.review.with_model as boolean});
      const resumed = {...intent, review: current};
      return validateRemovalReceipt(resumed, await postRemoval(api, resumed, signal));
    }
    if (signal?.aborted || isDefiniteRemovalRefusal(error)) throw error;
    try {
      const accepted = await findRemovalRequest(api, intent, signal);
      if (accepted) return accepted;
    } catch {
      // A failed or mismatched lookup cannot prove that the POST was refused.
    }
    throw new CacheRemovalOutcomeUnknown(
      "The removal receipt could not be confirmed. Keep this request and check its status before starting another removal.",
    );
  }
}

function postRemoval(
  api: ControlApi,
  intent: CacheRemovalIntent,
  signal?: AbortSignal,
): Promise<CacheRemovalReceipt> {
  return intent.kind === "model"
    ? api.removeModelCache(
        intent.selector,
        intent.review.target_identity,
        intent.requestKey,
        intent.review.review_digest,
        signal,
      )
    : api.removeRecipe(
        intent.selector,
        intent.requestKey,
        intent.review.with_model as boolean,
        intent.review.review_digest,
        signal,
      );
}

export async function retryReviewedRemoval(
  api: ControlApi,
  intent: CacheRemovalIntent,
  signal?: AbortSignal,
): Promise<CacheRemovalReceipt> {
  const existing = await findRemovalRequest(api, intent, signal);
  if (existing) return existing;
  // The request-key read above found no receipt. Replaying the identical body
  // and key preserves the original consent and lets the owner deduplicate it.
  return submitReviewedRemoval(api, intent, signal);
}
