import type {CacheRemovalReview} from "../api/types";

export function cacheRemovalReview(overrides: Partial<CacheRemovalReview> = {}): CacheRemovalReview {
  return {
    schema_version: 2,
    action: "remove",
    resource_kind: "recipe",
    selector: "vonk-forge/test-recipe",
    target_identity: "reviewed-recipe-revision",
    with_model: false,
    assets: [{kind: "runtime-image", sha256: "a".repeat(64), expected_bytes: 42, availability: "verified", available_bytes: 42, disposition: "remove"}],
    references: [],
    active_work: [],
    blockers: [],
    observed_at: "2026-09-24T12:00:00Z",
    review_digest: "b".repeat(64),
    ...overrides,
  };
}
