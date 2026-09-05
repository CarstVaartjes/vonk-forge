import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi, ModelCacheInventoryResponse, ModelCacheOperationResponse} from "../api/types";
import {librarySnapshot} from "../test-fixtures/library";
import {buildLibraryRecipeRecords} from "./library-workcell";
import {LibraryCacheView} from "./library-cache-view";

const storage = {
  schema_version: 2 as const,
  total_bytes: 1000,
  free_bytes: 700,
  reserve_bytes: 100,
  available_bytes: 600,
  unique_used_bytes: 300,
  in_flight_bytes: 0,
  protected_bytes: 100,
  reclaimable_bytes: 200,
};

function operation(kind: "download" | "repair"): ModelCacheOperationResponse {
  return {
    schema_version: 2,
    id: `${kind}-operation`,
    request_key: "00000000-0000-4000-8000-000000000001",
    kind,
    state: "succeeded",
    artifact_set_sha256: "c".repeat(64),
    plan_digest: `${kind}-plan`,
    progress: {schema_version: 2, phase: "completed", completed_artifacts: 2, total_artifacts: 2, downloaded_bytes: 200, expected_bytes: 200, current_artifact_key: null},
    result: null,
    last_error: null,
    created_at: "2026-09-05T00:00:00Z",
    updated_at: "2026-09-05T00:00:01Z",
    completed_at: "2026-09-05T00:00:01Z",
  };
}

function api(overrides: Partial<ControlApi> = {}): ControlApi {
  return {
    modelCacheInventory: async () => ({schema_version: 2, source_policy: "nas-first", entries: [], storage, total: 0, next_cursor: null}),
    modelCacheEntry: async () => { throw new Error("unused"); },
    previewModelCacheDownload: async () => ({schema_version: 2, artifact_set_sha256: "c".repeat(64), plan_digest: "download-plan", source_policy: "nas-first", artifact_count: 2, expected_bytes: 200, already_cached_bytes: 0, new_bytes: 200, blockers: [], warnings: []}),
    downloadModelCache: async () => operation("download"),
    previewModelCacheRepair: async () => ({schema_version: 2, artifact_set_sha256: "c".repeat(64), plan_digest: "repair-plan", source_policy: "nas-first", artifact_count: 2, current_state: "incomplete", expected_bytes: 200, verified_bytes: 100}),
    repairModelCache: async () => operation("repair"),
    previewModelCacheEviction: async () => { throw new Error("unused"); },
    evictModelCache: async () => { throw new Error("unused"); },
    modelCacheUpdates: async () => ({schema_version: 2, source_policy: "nas-first", updates: []}),
    modelCacheOperations: async () => ({schema_version: 2, operations: [], total: 0, next_cursor: null}),
    modelCacheOperation: async () => operation("download"),
    ...overrides,
  } as unknown as ControlApi;
}

afterEach(() => vi.restoreAllMocks());

test("downloads a candidate by previewing and applying in one action", async () => {
  const user = userEvent.setup();
  const previewModelCacheDownload = vi.fn(async () => ({schema_version: 2 as const, artifact_set_sha256: "c".repeat(64), plan_digest: "download-plan", source_policy: "nas-first" as const, artifact_count: 2, expected_bytes: 200, already_cached_bytes: 0, new_bytes: 200, blockers: [], warnings: []}));
  const downloadModelCache = vi.fn(async () => operation("download"));
  const records = buildLibraryRecipeRecords(librarySnapshot, []);
  render(<LibraryCacheView api={api({previewModelCacheDownload, downloadModelCache})} entries={records} onNavigate={vi.fn()} />);

  const row = (await screen.findAllByRole("article", {name: /Qwen 3 cache entry/}))[0]!;
  await user.click(within(row).getByRole("button", {name: "Download to Library"}));

  expect(previewModelCacheDownload).toHaveBeenCalledWith(expect.objectContaining({schema_version: 2, recipe_revision_sha256: "a".repeat(64)}));
  expect(downloadModelCache).toHaveBeenCalledWith(expect.objectContaining({schema_version: 2, plan_digest: "download-plan", request_key: expect.stringMatching(/^[0-9a-f-]{36}$/), source_policy: "nas-first"}));
  expect(screen.queryByText("Ready to apply")).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", {name: "Review download to NAS"})).not.toBeInTheDocument();
  expect(screen.getByText("Cache operation complete")).toBeVisible();
});

test("repairs an incomplete artifact set through the same automatic action", async () => {
  const user = userEvent.setup();
  const artifactSet = "c".repeat(64);
  const inventory: ModelCacheInventoryResponse = {
    schema_version: 2,
    source_policy: "nas-first",
    entries: [{schema_version: 2, artifact_set_sha256: artifactSet, model_version_sha256: "e".repeat(64), recipe_revision_sha256: "a".repeat(64), state: "incomplete", coverage: "incomplete", expected_bytes: 200, verified_bytes: 100, unique_bytes: 200, artifacts: [], protected: false, protected_reasons: [], update_available: false, recipe_update_available: false, created_at: "2026-09-05T00:00:00Z", updated_at: "2026-09-05T00:00:00Z", verified_at: null, last_error: "one file is incomplete"}],
    storage,
    total: 1,
    next_cursor: null,
  };
  const previewModelCacheRepair = vi.fn(async () => ({schema_version: 2 as const, artifact_set_sha256: artifactSet, plan_digest: "repair-plan", source_policy: "nas-first" as const, artifact_count: 2, current_state: "incomplete" as const, expected_bytes: 200, verified_bytes: 100}));
  const repairModelCache = vi.fn(async () => operation("repair"));
  const records = buildLibraryRecipeRecords(librarySnapshot, []);
  render(<LibraryCacheView api={api({modelCacheInventory: async () => inventory, previewModelCacheRepair, repairModelCache})} entries={records} onNavigate={vi.fn()} />);

  const status = await screen.findByText("Incomplete on Controller");
  const row = status.closest("article");
  expect(row).not.toBeNull();
  const cacheRow = row!;
  await user.click(within(cacheRow).getByRole("button", {name: "Repair payload"}));

  expect(previewModelCacheRepair).toHaveBeenCalledWith({schema_version: 2, artifact_set_sha256: artifactSet});
  expect(repairModelCache).toHaveBeenCalledWith(expect.objectContaining({schema_version: 2, artifact_set_sha256: artifactSet, plan_digest: "repair-plan", request_key: expect.stringMatching(/^[0-9a-f-]{36}$/), source_policy: "nas-first"}));
  expect(screen.queryByRole("heading", {name: "Review cache repair"})).not.toBeInTheDocument();
  expect(screen.getByText("Cache operation complete")).toBeVisible();
});
