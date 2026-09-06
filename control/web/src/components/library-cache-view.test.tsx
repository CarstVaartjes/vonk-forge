import {render, screen} from "@testing-library/react";
import {librarySnapshot} from "../test-fixtures/library";
import {LibraryCacheView, aggregateCacheEntries} from "./library-cache-view";

test("aggregates the complete canonical Model file set", () => {
  const entries = aggregateCacheEntries(librarySnapshot.models);
  expect(entries).toHaveLength(92);
  expect(entries[0]!.files).toHaveLength(2);
  expect(entries[0]!.expectedBytes).toBeGreaterThan(entries[0]!.verifiedBytes);
});
test("offers one direct Download to NAS action", () => {
  render(<LibraryCacheView api={{modelCacheInventory: async () => ({entries: [], schema_version: 2, source_policy: "nas-first", total: 0, next_cursor: null}), previewModelCacheDownload: async () => ({schema_version: 2, artifact_set_sha256: "a".repeat(64), plan_digest: "p", source_policy: "nas-first", artifact_count: 2, expected_bytes: 1, already_cached_bytes: 0, new_bytes: 1, blockers: [], warnings: []}), downloadModelCache: async () => { throw new Error("unused"); }} as never} modelInventory={librarySnapshot.models} onNavigate={() => undefined} path="/library/cache"/>);
  expect(screen.getAllByRole("button", {name: "Download to NAS"}).length).toBeGreaterThan(0);
});
