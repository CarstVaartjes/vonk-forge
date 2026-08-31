import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi, LibraryInstallPlan, LibrarySnapshot, PublicRecipe, VisualFleetNode, VisualFleetSnapshot} from "../api/types";
import {App} from "../app";
import {fullLibraryDetail, libraryRecipeSummary, librarySnapshot} from "../test-fixtures/library";
import {
  buildLibraryRecipeRecords,
  deriveLibraryModels,
  EMPTY_LIBRARY_WORKCELL_FILTERS,
  filterLibraryRecipeRecords,
  sparkPlacementState,
} from "./library-workcell";

const GIB = 1024 ** 3;

function catalogRecipe(overrides: Partial<PublicRecipe> & Pick<PublicRecipe, "slug" | "title">): PublicRecipe {
  return {
    publisher: "vonk-forge", description: `${overrides.title} description`, tags: [], uri: `vonk://catalog/vonk-forge/${overrides.slug}@sha256:${"b".repeat(64)}`,
    content_sha256: "b".repeat(64), model_publisher: "qwen", model_slug: "qwen", model_title: "Qwen", model_version_publisher: "qwen", model_version_slug: "qwen-3", model_version_title: "Qwen 3",
    source_owner: "QwenLM", source_repository: "https://github.com/QwenLM/Qwen3", capabilities: ["chat"], qualification: "cataloged", qualification_basis: "explicit-accepted-metadata",
    qualification_detail: "Accepted by the catalog gate.", precision: "BF16", quantizations: ["BF16"], execution_readiness: "executable", execution_readiness_basis: "explicit-executable-metadata",
    execution_readiness_detail: "Complete executable contract.", execution_harness: "vllm-openai", runtime_distribution: "vllm-0-27-1", source_bundle_sha256: "9".repeat(64), artifact_count: 1,
    topology_name: "single", topology_mode: "single", node_count: 1, topology_roles: [{name: "entrypoint", count: 1, endpoint_owner: true}], fabric: {connectivity: "none", minimum_bandwidth_mbps: 0},
    expected_download_bytes: 20 * GIB, maximum_installed_bytes_per_node: 25 * GIB, maximum_runtime_memory_bytes_per_node: 48 * GIB, release_version: "1.0.0", release_released_at: "2026-08-30",
    local: {status: "current", recipe_id: null, revision_number: null, content_sha256: null, release_version: null},
    ...overrides,
  };
}

function fleetNode(overrides: Partial<VisualFleetNode> = {}): VisualFleetNode {
  return {
    id: "node-alpha", display_name: "MIA Alpha", hostname: "mia-alpha.internal", labels: {}, lifecycle: "ready",
    connection: {agent_state: "active", certificate_state: "valid", last_seen_age_seconds: 1, last_seen_at: "2026-08-31T12:00:00Z", offline_reason: null, online_state: "online"},
    installed: [], loaded: [], inventory: null, telemetry: null, reservations: {disk_bytes: 0, memory_bytes: 0}, warnings: [],
    ...overrides,
  } as VisualFleetNode;
}

function fleet(nodes: VisualFleetNode[]): VisualFleetSnapshot {
  return {schema_version: 1, generated_at: "2026-08-31T12:00:00Z", authority_revision: "f".repeat(64), event_cursor: 1, nodes};
}

afterEach(() => {
  history.replaceState(null, "", "/");
  localStorage.clear();
  vi.restoreAllMocks();
});

test("applies creator and capability filters to recipes first, then derives the model rail", async () => {
  history.replaceState(null, "", "/library");
  const vision = libraryRecipeSummary({recipe_id: "recipe-vision", slug: "deepseek-vision", title: "DeepSeek Vision", capabilities: ["vision"]});
  const snapshot: LibrarySnapshot = {
    ...librarySnapshot,
    models: [
      librarySnapshot.models[0],
      {model: {kind: "model-version", publisher: "deepseek-ai", slug: "v4-vision", content_sha256: "d".repeat(64)}, page_local: true, recipes: [vision]},
    ],
    unlinked_recipes: [],
  };
  const recipes = [
    catalogRecipe({slug: "qwen-chat", title: "Qwen Chat", local: {status: "current", recipe_id: "recipe-chat", revision_number: 3, content_sha256: "a".repeat(64), release_version: "1.0.0"}}),
    catalogRecipe({slug: "deepseek-vision", title: "DeepSeek Vision", model_publisher: "deepseek-ai", model_slug: "v4", model_title: "DeepSeek V4", model_version_publisher: "deepseek-ai", model_version_slug: "v4-vision", model_version_title: "DeepSeek V4 Vision", source_owner: "MiaAI-Lab", source_repository: "https://github.com/MiaAI-Lab/DSpark", capabilities: ["vision"], local: {status: "current", recipe_id: "recipe-vision", revision_number: 1, content_sha256: "d".repeat(64), release_version: "1.0.0"}}),
  ];
  const api = {librarySnapshot: async () => snapshot, listPublicRecipes: async () => ({repository: "CarstVaartjes/vonk-forge-recipes", commit: "c".repeat(40), recipes})} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  await user.click(await screen.findByText("Recipe filters"));
  await user.selectOptions(screen.getByRole("combobox", {name: "Recipe creator"}), "MiaAI-Lab");
  await user.click(screen.getByRole("checkbox", {name: "Vision"}));

  const models = screen.getByRole("region", {name: "Models"});
  expect(within(models).getByRole("link", {name: /Deepseek V4 Vision/i})).toBeVisible();
  expect(within(models).queryByRole("link", {name: /Qwen 3/})).not.toBeInTheDocument();
  expect(screen.getByRole("region", {name: "Recipe inventory"})).toHaveTextContent("DeepSeek Vision");
});

test("keeps custom recipes isolated from managed updates while exposing the automatic-sync contract", async () => {
  history.replaceState(null, "", "/library");
  const update = catalogRecipe({slug: "qwen-chat", title: "Qwen Chat", release_version: "1.2.0", local: {status: "update-available", recipe_id: "recipe-chat", revision_number: 3, content_sha256: "a".repeat(64), release_version: "1.0.0"}});
  const syncManagedRecipeCatalog = vi.fn().mockResolvedValue({state: "current", imported_count: 1, updated_count: 2, withdrawn_count: 3});
  const api = {librarySnapshot: async () => librarySnapshot, listPublicRecipes: async () => ({repository: "CarstVaartjes/vonk-forge-recipes", commit: "c".repeat(40), recipes: [update]}), syncManagedRecipeCatalog} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  expect(await screen.findByText("Update available · v1.0.0 → v1.2.0")).toBeVisible();
  await waitFor(() => expect(syncManagedRecipeCatalog).toHaveBeenCalledTimes(1));
  expect(await screen.findByText("Last sync: 1 imported · 2 updated · 3 withdrawn")).toBeVisible();
  const sync = screen.getByRole("button", {name: "Sync now"});
  expect(sync).toBeEnabled();

  await user.click(screen.getByText("Recipe filters"));
  await user.selectOptions(screen.getByRole("combobox", {name: "Local status"}), "custom");
  const workSurface = screen.getByRole("region", {name: "Recipe inventory"});
  expect(workSurface).toHaveTextContent("Custom Runtime");
  expect(workSurface).not.toHaveTextContent("Qwen Chat");
  expect(screen.getByRole("region", {name: "Models"})).toHaveTextContent("Unlinked");
});

test("opens the exact install preview from the keyboard placement path and drag-and-drop", async () => {
  history.replaceState(null, "", "/library");
  const detail = structuredClone(fullLibraryDetail);
  detail.placement[0].recommendations[0].preview_targets = [{kind: "install", input: {mapping_id: "mapping-chat", recipe_build_id: "build-chat"}}];
  const installPlan: LibraryInstallPlan = {
    allowed: true, image_digest: `sha256:${"d".repeat(64)}`, mapping_generation: 4, mapping_id: "mapping-chat", nodes: [], plan_digest: "install-plan", recipe_build_id: "build-chat", recipe_content_sha256: "a".repeat(64), recipe_revision_id: "revision-chat",
  };
  const previewLibraryInstall = vi.fn(async () => installPlan);
  const libraryRecipe = vi.fn(async () => detail);
  const api = {librarySnapshot: async () => librarySnapshot, libraryRecipe, visualFleet: async () => fleet([fleetNode(), fleetNode({id: "node-beta", display_name: "MIA Beta", hostname: "mia-beta.internal"})]), previewLibraryInstall} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const recipe = (await screen.findByRole("link", {name: /^Qwen ChatQwen Chat description/})).closest("article")!;
  await user.click(within(recipe).getByRole("button", {name: "Place on Spark"}));
  await waitFor(() => expect(libraryRecipe).toHaveBeenCalledWith("recipe-chat"));
  expect(await screen.findByText("Choose a compatible Spark for Qwen Chat.")).toBeInTheDocument();
  expect(await screen.findAllByText("Atomic 2-Spark placement")).toHaveLength(2);
  await user.click(await screen.findByRole("button", {name: "Review placement on MIA Alpha"}));
  expect(await screen.findByRole("heading", {name: "Review Install"})).toBeVisible();
  await waitFor(() => expect(previewLibraryInstall).toHaveBeenCalledTimes(1));
  await user.click(screen.getByRole("button", {name: "Close review"}));

  const dataTransfer = {effectAllowed: "none", dropEffect: "none", setData: vi.fn(), getData: vi.fn()} as unknown as DataTransfer;
  fireEvent.dragStart(recipe, {dataTransfer});
  const spark = screen.getByText("MIA Alpha").closest("article")!;
  fireEvent.dragOver(spark, {dataTransfer});
  fireEvent.drop(spark, {dataTransfer});
  expect(await screen.findByRole("heading", {name: "Review Install"})).toBeVisible();
  await waitFor(() => expect(previewLibraryInstall).toHaveBeenCalledTimes(2));
});

test("projects running, update, incompatible, and offline Spark states without flattening multi-Spark placement", () => {
  const record = buildLibraryRecipeRecords(librarySnapshot, [catalogRecipe({slug: "qwen-chat", title: "Qwen Chat", local: {status: "update-available", recipe_id: "recipe-chat", revision_number: 3, content_sha256: "a".repeat(64), release_version: "1.0.0"}})])[0]!;
  const installed = {recipe_id: "recipe-chat", rank_state: "installed"};
  const loaded = {recipe_id: "recipe-chat"};
  expect(sparkPlacementState(fleetNode({installed: [installed] as VisualFleetNode["installed"]}), [record])).toBe("update");
  expect(sparkPlacementState(fleetNode({loaded: [loaded] as VisualFleetNode["loaded"]}), [record])).toBe("running");
  expect(sparkPlacementState(fleetNode({id: "node-outside"}), [record], fullLibraryDetail)).toBe("incompatible");
  expect(sparkPlacementState(fleetNode({connection: {...fleetNode().connection, online_state: "offline", offline_reason: "stale"}}), [record], fullLibraryDetail)).toBe("offline");

  const visionOnly = filterLibraryRecipeRecords([record], {...EMPTY_LIBRARY_WORKCELL_FILTERS, capabilities: ["vision"]}, "");
  expect(deriveLibraryModels(visionOnly)).toEqual([]);
});

test("makes active-run removal impact explicit and does not invent a model-dependency mutation", async () => {
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const node = fleetNode({
    installed: [{recipe_id: "recipe-chat", rank_state: "installed"}] as VisualFleetNode["installed"],
    loaded: [{recipe_id: "recipe-chat"}] as VisualFleetNode["loaded"],
  });
  const api = {librarySnapshot: async () => librarySnapshot, libraryRecipe: async () => fullLibraryDetail, visualFleet: async () => fleet([node])} as unknown as ControlApi;
  render(<App api={api}/>);

  const sparks = await screen.findByRole("complementary", {name: "Sparks"});
  expect(within(sparks).getByText("Running")).toBeVisible();
  expect(within(sparks).getByText("Active runs must stop before uninstall.")).toBeVisible();
  const modelRemoval = within(sparks).getByRole("button", {name: "Review model removal"});
  expect(modelRemoval).toBeDisabled();
  expect(modelRemoval).toHaveAttribute("title", "Model dependency preview is not available from the Controller yet.");
});
