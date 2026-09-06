import {act, fireEvent, render, screen} from "@testing-library/react";
import type {ModelCacheOperationResponse} from "../api/types";
import {librarySnapshot} from "../test-fixtures/library";
import {LibraryCacheView, LibraryModelDownloadAction, aggregateCacheEntries} from "./library-cache-view";

test("aggregates the complete canonical Model file set", () => {
  const entries = aggregateCacheEntries(librarySnapshot.models);
  expect(entries).toHaveLength(92);
  expect(entries[0]!.files).toHaveLength(librarySnapshot.models[0]!.model_document.files.length);
  expect(entries[0]!.expectedBytes).toBeGreaterThan(entries[0]!.verifiedBytes);
});
test("offers one direct Download to NAS action", () => {
  render(<LibraryCacheView api={{modelCacheInventory: async () => ({entries: [], schema_version: 2, source_policy: "nas-first", total: 0, next_cursor: null}), previewModelCacheDownload: async () => ({schema_version: 2, artifact_set_sha256: "a".repeat(64), plan_digest: "p", source_policy: "nas-first", artifact_count: 2, expected_bytes: 1, already_cached_bytes: 0, new_bytes: 1, blockers: [], warnings: []}), downloadModelCache: async () => { throw new Error("unused"); }} as never} modelInventory={librarySnapshot.models} onNavigate={() => undefined} path="/library/cache"/>);
  expect(screen.getAllByRole("button", {name: "Download to NAS"}).length).toBeGreaterThan(0);
});
test("unions multiple cache sets before reporting a complete no-Recipe Model", () => {
  const model = librarySnapshot.models[79]!;
  const [first, second] = model.model_document.files;
  const entry = (artifact: typeof first, set: string) => ({schema_version: 2, artifact_set_sha256: set.repeat(64).slice(0, 64), artifacts: [{schema_version: 2, id: artifact.id, key: artifact.id, path: artifact.path, roles: artifact.roles, sha256: artifact.sha256, expected_bytes: artifact.size_bytes, actual_bytes: artifact.size_bytes, source: "nas", state: "verified"}], coverage: "incomplete", created_at: "2026-09-06T12:00:00Z", expected_bytes: artifact.size_bytes, model_version_sha256: model.model.content_sha256, protected: false, protected_reasons: [], recipe_revision_sha256: null, recipe_update_available: false, state: "cached", unique_bytes: artifact.size_bytes, update_available: false, updated_at: "2026-09-06T12:00:00Z", verified_at: "2026-09-06T12:00:00Z", verified_bytes: artifact.size_bytes});
  expect(aggregateCacheEntries([model], {entries: [entry(first!, "a")] as never})[0]!.status).toBe("partial");
  const result = aggregateCacheEntries([model], {entries: [entry(first!, "a"), entry(second!, "b")] as never});
  expect(result[0]!.status).toBe("cached");
  expect(result[0]!.verifiedBytes).toBeGreaterThan(0);
});

function cacheOperation(overrides: Partial<ModelCacheOperationResponse> = {}): ModelCacheOperationResponse {
  return {
    schema_version: 2,
    id: "cache-operation-1",
    kind: "download",
    state: "failed",
    attempt: 1,
    request_key: "00000000-0000-4000-8000-000000000401",
    artifact_set_sha256: "a".repeat(64),
    plan_digest: "b".repeat(64),
    progress: {schema_version: 2, phase: "failed", completed_artifacts: 1, total_artifacts: 2, downloaded_bytes: 10, expected_bytes: 20, total_bytes_known: true, current_artifact_key: "second"},
    created_at: "2026-09-06T12:00:00Z",
    completed_at: "2026-09-06T12:01:00Z",
    failure: {code: "temporary_transfer_failure", detail: "temporary transfer failure", retryable: true, recovery_actions: ["retry"]},
    result: null,
    updated_at: "2026-09-06T12:01:00Z",
    ...overrides,
  };
}

test("retries a transient cache operation in place with retained progress", async () => {
  const model = librarySnapshot.models[0]!;
  const failed = cacheOperation({result: {retryable: true}});
  const replacement = cacheOperation({id: "cache-operation-2", state: "running", result: {retryable: false}, attempt: 1, progress: {...failed.progress, phase: "downloading", downloaded_bytes: 10}});
  const previewModelCacheDownload = vi.fn(async () => ({schema_version: 2 as const, artifact_set_sha256: "a".repeat(64), plan_digest: "b".repeat(64), source_policy: "nas-first" as const, artifact_count: 2, expected_bytes: 20, already_cached_bytes: 10, new_bytes: 10, blockers: [], warnings: []}));
  const downloadModelCache = vi.fn(async () => failed);
  const retryModelCacheOperation = vi.fn(async () => replacement);
  render(<LibraryModelDownloadAction api={{previewModelCacheDownload, downloadModelCache, retryModelCacheOperation} as never} model={model}/>);

  await act(async () => { fireEvent.click(screen.getByRole("button", {name: "Make available"})); });
  expect((await screen.findAllByRole("button", {name: "Retry download"}))[0]).toBeVisible();
  await act(async () => { fireEvent.click(screen.getAllByRole("button", {name: "Retry download"})[0]!); });
  expect(retryModelCacheOperation).toHaveBeenCalledWith(failed.id, {schema_version: 2, request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)});
  expect(previewModelCacheDownload).toHaveBeenCalledTimes(1);
  expect(downloadModelCache).toHaveBeenCalledTimes(1);
  expect(screen.getByText("1 of 2 files · 10 B")).toBeVisible();
});

test("does not offer cache retry for terminal integrity failures", async () => {
  const model = librarySnapshot.models[0]!;
  const failed = cacheOperation({failure: {code: "integrity_mismatch", detail: "artifact digest mismatch", retryable: false, recovery_actions: ["download_again"]}});
  render(<LibraryModelDownloadAction api={{previewModelCacheDownload: vi.fn(async () => ({schema_version: 2 as const, artifact_set_sha256: "a".repeat(64), plan_digest: "b".repeat(64), source_policy: "nas-first" as const, artifact_count: 2, expected_bytes: 20, already_cached_bytes: 10, new_bytes: 10, blockers: [], warnings: []})), downloadModelCache: vi.fn(async () => failed)} as never} model={model}/>);

  await act(async () => { fireEvent.click(screen.getByRole("button", {name: "Make available"})); });
  expect(screen.queryByRole("button", {name: "Retry download"})).not.toBeInTheDocument();
});
