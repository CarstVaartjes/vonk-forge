import {render, screen} from "@testing-library/react";
import {librarySnapshot} from "../test-fixtures/library";
import {LibraryCacheView, aggregateCacheEntries} from "./library-cache-view";

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
