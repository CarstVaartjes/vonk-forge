import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {MouseEvent} from "react";
import {EMPTY_LIBRARY_WORKCELL_FILTERS, buildLibraryRecipeRecords} from "./library-workcell";
import {LibraryModelsView} from "./library-models-view";
import type {ControlApi, LibrarySnapshot, ModelCacheOperationResponse, PublicRecipe} from "../api/types";
import {libraryRecipeSummary, librarySnapshot} from "../test-fixtures/library";

const knownCatalogRecipe: PublicRecipe = {
  publisher: "vonk-forge", slug: "qwen-chat", title: "Qwen Chat", description: "A cataloged Qwen recipe.", tags: ["chat"],
  uri: `vonk://catalog/vonk-forge/qwen-chat@sha256:${"c".repeat(64)}`, content_sha256: "c".repeat(64),
  model_publisher: "qwen", model_slug: "3", model_title: "Qwen 3", model_version_publisher: "qwen", model_version_slug: "3-bf16", model_version_title: "Qwen 3 BF16",
  source_owner: "QwenLM", source_repository: "https://github.com/QwenLM/Qwen3", alignment: "standard", capabilities: ["chat"], qualification: "cataloged",
  qualification_basis: "explicit-accepted-metadata", qualification_detail: "The immutable recipe declares accepted qualification.", precision: "BF16", quantizations: ["BF16"],
  execution_readiness: "executable", execution_readiness_basis: "explicit-executable-metadata", execution_readiness_detail: "The recipe declares an executable contract.",
  execution_harness: "vllm-openai", runtime_distribution: "vllm-0-27-1", source_bundle_sha256: "d".repeat(64), artifact_count: 1, artifact_identities: [], temporary_build_bytes_per_node: 0,
  topology_name: "pair", topology_mode: "tensor_parallel", node_count: 2, topology_roles: [{name: "leader", count: 1, endpoint_owner: true, disk: {image_bytes: 0, artifact_bytes: 0, staging_bytes: 0, cache_bytes: 0, rollback_bytes: 0, safety_margin_bytes: 0}}, {name: "worker", count: 1, endpoint_owner: false, disk: {image_bytes: 0, artifact_bytes: 0, staging_bytes: 0, cache_bytes: 0, rollback_bytes: 0, safety_margin_bytes: 0}}],
  fabric: {connectivity: "connected", minimum_bandwidth_mbps: 25_000}, expected_download_bytes: 80 * 1024 ** 3, maximum_installed_bytes_per_node: 100 * 1024 ** 3, maximum_runtime_memory_bytes_per_node: 72 * 1024 ** 3,
  release_version: "1.2.0", release_released_at: "2026-08-24", local: {status: "current", recipe_id: "recipe-chat", revision_number: 3, content_sha256: "a".repeat(64), release_version: "1.2.0"},
};

function renderModels(snapshot: LibrarySnapshot = librarySnapshot, catalog: PublicRecipe[] = [knownCatalogRecipe], modelInventory: LibrarySnapshot["models"] = snapshot.models, api: Partial<ControlApi> = {}, onNavigate: (event: MouseEvent<HTMLAnchorElement>, path: string) => void = () => undefined) {
  const entries = buildLibraryRecipeRecords(snapshot, catalog);
  return render(<LibraryModelsView
    api={api as ControlApi}
    entries={entries}
    modelInventory={modelInventory}
    filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
    onFiltersChange={() => undefined}
    onNavigate={onNavigate}
    onQueryChange={() => undefined}
    query=""
  />);
}

test("uses canonical model size and exact Controller cache coverage", async () => {
  const modelArtifacts = [
    {...librarySnapshot.models[0]!.model_version!.artifacts[0]!, id: "weights", path: "model.safetensors", download_bytes: 80},
    {...librarySnapshot.models[0]!.model_version!.artifacts[0]!, id: "config", path: "config.json", sha256: "2".repeat(64), download_bytes: 10, installed_bytes: 10},
  ];
  const model = {...librarySnapshot.models[0]!, model_version: {...librarySnapshot.models[0]!.model_version!, artifacts: modelArtifacts, sizes: {download_bytes: 90, installed_bytes: 100}}, recipes: []};
  const cacheArtifact = (artifact: typeof modelArtifacts[number], actualBytes: number) => ({schema_version: 2 as const, id: artifact.id, key: artifact.id, path: artifact.path, roles: artifact.roles, sha256: artifact.sha256, source: "nas", state: "verified" as const, actual_bytes: actualBytes, expected_bytes: artifact.download_bytes});
  const cacheEntry = (artifactSet: string, artifacts: ReturnType<typeof cacheArtifact>[]) => ({schema_version: 2 as const, artifact_set_sha256: artifactSet, model_version_sha256: model.model.content_sha256, recipe_revision_sha256: null, state: "cached" as const, coverage: "complete" as const, expected_bytes: artifacts.reduce((total, artifact) => total + artifact.expected_bytes, 0), verified_bytes: artifacts.reduce((total, artifact) => total + artifact.actual_bytes, 0), unique_bytes: artifacts.reduce((total, artifact) => total + artifact.actual_bytes, 0), artifacts, protected: false, protected_reasons: [], update_available: false, recipe_update_available: false, created_at: "2026-09-06T00:00:00Z", updated_at: "2026-09-06T00:00:00Z", verified_at: "2026-09-06T00:00:00Z", last_error: null});
  const subset = cacheEntry("a".repeat(64), [cacheArtifact(modelArtifacts[0]!, 80)]);
  const full = cacheEntry("b".repeat(64), modelArtifacts.map(artifact => cacheArtifact(artifact, artifact.download_bytes)));
  const api: Partial<ControlApi> = {
    modelCacheInventory: async () => ({schema_version: 2, source_policy: "nas-first", entries: [subset, full], storage: {schema_version: 2, total_bytes: 100, free_bytes: 50, reserve_bytes: 10, available_bytes: 40, unique_used_bytes: 50, in_flight_bytes: 0, protected_bytes: 10, reclaimable_bytes: 20}, total: 2, next_cursor: null}),
  };
  renderModels({...librarySnapshot, models: [model]}, [], [model], api);

  const row = await screen.findByRole("article", {name: /BF16 model version/});
  expect(row).toHaveTextContent("90 B download");
  expect(row).toHaveTextContent("Cache 100% verified");
  expect(row).not.toHaveTextContent("Spark count");
});

test("does not call a selected-file cache set a complete model", async () => {
  const modelArtifacts = [
    {...librarySnapshot.models[0]!.model_version!.artifacts[0]!, id: "weights", path: "model.safetensors", download_bytes: 80},
    {...librarySnapshot.models[0]!.model_version!.artifacts[0]!, id: "config", path: "config.json", sha256: "2".repeat(64), download_bytes: 10, installed_bytes: 10},
  ];
  const model = {...librarySnapshot.models[0]!, model_version: {...librarySnapshot.models[0]!.model_version!, artifacts: modelArtifacts, sizes: {download_bytes: 90, installed_bytes: 100}}, recipes: []};
  const artifact = {...modelArtifacts[0]!, schema_version: 2 as const, key: modelArtifacts[0]!.id, source: "nas", state: "verified" as const, actual_bytes: 80, expected_bytes: 80};
  const api: Partial<ControlApi> = {
    modelCacheInventory: async () => ({schema_version: 2, source_policy: "nas-first", entries: [{schema_version: 2, artifact_set_sha256: "a".repeat(64), model_version_sha256: model.model.content_sha256, recipe_revision_sha256: "r".repeat(64), state: "cached", coverage: "complete", expected_bytes: 80, verified_bytes: 80, unique_bytes: 80, artifacts: [artifact], protected: false, protected_reasons: [], update_available: false, recipe_update_available: false, created_at: "2026-09-06T00:00:00Z", updated_at: "2026-09-06T00:00:00Z", verified_at: "2026-09-06T00:00:00Z", last_error: null}], storage: {schema_version: 2, total_bytes: 100, free_bytes: 50, reserve_bytes: 10, available_bytes: 40, unique_used_bytes: 50, in_flight_bytes: 0, protected_bytes: 10, reclaimable_bytes: 20}, total: 1, next_cursor: null}),
  };
  renderModels({...librarySnapshot, models: [model]}, [], [model], api);

  const row = await screen.findByRole("article", {name: /BF16 model version/});
  expect(row).toHaveTextContent("Incomplete");
  expect(row).not.toHaveTextContent("Cache 100% verified");
});

test("uses the catalog family title and flattens a single variant", () => {
  renderModels({
    ...librarySnapshot,
    models: [{...librarySnapshot.models[0]!, recipes: [librarySnapshot.models[0]!.recipes[0]!]}],
  });

  const families = screen.getByLabelText("Model families");
  expect(within(families).getByRole("button", {name: /Qwen 3.*BF16/})).toBeVisible();
  expect(within(families).queryByRole("button", {name: /Unlinked/})).not.toBeInTheDocument();
  expect(within(families).getByRole("article")).toHaveTextContent("Qwen 3 BF16");
  expect(within(families).getByText("Model: Text Generation")).toBeVisible();
  expect(within(families).getByText("Recipe: Chat")).toBeVisible();
  expect(screen.queryByText(/unlinked|detached/i)).not.toBeInTheDocument();
});

test("deduplicates recipes that reference one exact model variant and pairs both recipes after selection", async () => {
  const secondCatalogRecipe: PublicRecipe = {
    ...knownCatalogRecipe,
    slug: "qwen-code",
    title: "Qwen Code",
    uri: `vonk://catalog/vonk-forge/qwen-code@sha256:${"d".repeat(64)}`,
    content_sha256: "d".repeat(64),
    capabilities: ["reasoning"],
    local: {...knownCatalogRecipe.local, recipe_id: "recipe-code", content_sha256: "a".repeat(64)},
  };
  const user = userEvent.setup();
  renderModels(librarySnapshot, [knownCatalogRecipe, secondCatalogRecipe]);

  const row = screen.getByRole("article");
  expect(screen.getAllByRole("article")).toHaveLength(1);
  expect(within(row).getByText("Qwen 3", {selector: "strong"})).toBeVisible();
  await user.click(within(row).getByRole("button", {name: /Qwen 3.*BF16/}));
  const recipes = screen.getByLabelText("Recipes matching selected model");
  expect(within(recipes).getByText("Qwen 3 · BF16")).toBeVisible();
  expect(within(recipes).getByRole("link", {name: /Qwen Chat/})).toBeVisible();
  expect(within(recipes).getByRole("link", {name: /Qwen Code/})).toBeVisible();
});

test("ignores local records that are absent from the repository", () => {
  const customRecipe = libraryRecipeSummary({recipe_id: "recipe-custom-linked", slug: "custom-linked", title: "Custom linked runtime", selected_revision: null, topology_name: "solo"});
  renderModels({
    ...librarySnapshot,
    models: [{model: {kind: "model-version", publisher: "local", slug: "runtime", content_sha256: "f".repeat(64)}, page_local: true, recipes: [customRecipe]}],
    unlinked_recipes: [],
  }, [], []);

  expect(screen.getByRole("heading", {name: "No models match"})).toBeVisible();
  expect(screen.queryByText(/Local Runtime|Custom linked runtime/i)).not.toBeInTheDocument();
});

test("shows a canonical model with no matching repository recipe", async () => {
  const noRecipeModel = {...librarySnapshot.models[0]!, recipes: []};
  const user = userEvent.setup();
  renderModels({...librarySnapshot, models: [noRecipeModel]}, [], [noRecipeModel]);

  const row = screen.getByRole("article");
  await user.click(within(row).getByRole("button", {name: /Qwen 3.*BF16/}));
  const recipes = screen.getByLabelText("Recipes matching selected model");
  expect(within(recipes).getByText("No matching recipes")).toBeVisible();
  expect(recipes).toHaveTextContent("This exact model version has no repository recipe");
});

test("starts a no-recipe NAS download from one model-row action", async () => {
  const noRecipeModel = {...librarySnapshot.models[0]!, recipes: []};
  const previewModelCacheDownload = vi.fn(async () => ({schema_version: 2 as const, artifact_set_sha256: "f".repeat(64), plan_digest: "model-download-plan", source_policy: "nas-first" as const, artifact_count: 2, expected_bytes: 200, already_cached_bytes: 0, new_bytes: 200, blockers: [], warnings: []}));
  const running: ModelCacheOperationResponse = {
    schema_version: 2, id: "model-download-operation", attempt: 1, request_key: "00000000-0000-4000-8000-000000000801", kind: "download", state: "running", artifact_set_sha256: "f".repeat(64), plan_digest: "model-download-plan", progress: {schema_version: 2, phase: "downloading", completed_artifacts: 1, total_artifacts: 2, downloaded_bytes: 100, expected_bytes: 200, current_artifact_key: "weights"}, result: null, last_error: null, created_at: "2026-09-06T00:00:00Z", updated_at: "2026-09-06T00:00:01Z", completed_at: null,
  };
  const downloadModelCache = vi.fn(async () => running);
  const onNavigate = vi.fn();
  renderModels({...librarySnapshot, models: [noRecipeModel]}, [], [noRecipeModel], {
    modelCacheInventory: async () => ({schema_version: 2, source_policy: "nas-first", entries: [], storage: {schema_version: 2, total_bytes: 100, free_bytes: 50, reserve_bytes: 10, available_bytes: 40, unique_used_bytes: 50, in_flight_bytes: 0, protected_bytes: 10, reclaimable_bytes: 20}, total: 0, next_cursor: null}),
    previewModelCacheDownload,
    downloadModelCache,
    modelCacheOperation: async () => running,
  }, onNavigate);

  const row = await screen.findByRole("article", {name: /BF16 model version/});
  await userEvent.setup().click(within(row).getByRole("button", {name: "Download to NAS"}));
  expect(onNavigate).not.toHaveBeenCalled();
  expect(previewModelCacheDownload).toHaveBeenCalledWith({schema_version: 2, model_version_sha256: noRecipeModel.model.content_sha256, source_policy: "nas-first"});
  expect(downloadModelCache).toHaveBeenCalledWith(expect.objectContaining({plan_digest: "model-download-plan", artifact_set_sha256: "f".repeat(64)}));
  expect(await screen.findByText(/Downloading to NAS/)).toBeVisible();
});
