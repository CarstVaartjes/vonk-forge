import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi, LibraryModelDeletionPlan, LibraryPlacementPreview, LibrarySnapshot, ManagedCatalogSyncSummary, PublicRecipe, VisualFleetNode, VisualFleetSnapshot} from "../api/types";
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

function catalogSync(overrides: Partial<ManagedCatalogSyncSummary> = {}): ManagedCatalogSyncSummary {
  return {
    schema_version: 1,
    sync_id: "00000000-0000-4000-8000-000000000101",
    request_key: "00000000-0000-4000-8000-000000000102",
    trigger: "automatic",
    state: "current",
    repository: "CarstVaartjes/vonk-forge-recipes",
    commit: "c".repeat(40),
    expected_commit: "c".repeat(40),
    total_count: 3,
    processed_count: 3,
    imported_count: 0,
    updated_count: 0,
    unchanged_count: 3,
    skipped_count: 0,
    withdrawn_count: 0,
    withdrawn_recipes: [],
    stale_recipes: [],
    problems: [],
    created_at: "2026-09-01T12:00:00Z",
    completed_at: "2026-09-01T12:00:01Z",
    ...overrides,
  };
}

function placementPreview(overrides: Partial<LibraryPlacementPreview> = {}): LibraryPlacementPreview {
  return {
    schema_version: 1,
    generated_at: "2026-09-01T12:00:00Z",
    recipe_id: "recipe-chat",
    recipe_revision_id: "revision-chat",
    recipe_title: "Qwen Chat",
    topology_name: "pair",
    desired_state: "installed",
    alias: null,
    invocation: "button",
    selected_node_ids: ["node-alpha", "node-beta"],
    selected_nodes: [
      {node_id: "node-alpha", rank: 0, role: "leader", endpoint_owner: true, disk_free_bytes: 200 * GIB, disk_required_bytes: 60 * GIB, disk_free_after_bytes: 140 * GIB, memory_available_bytes: 100 * GIB, memory_required_bytes: 60 * GIB, memory_free_after_bytes: 40 * GIB},
      {node_id: "node-beta", rank: 1, role: "worker", endpoint_owner: false, disk_free_bytes: 200 * GIB, disk_required_bytes: 60 * GIB, disk_free_after_bytes: 140 * GIB, memory_available_bytes: 100 * GIB, memory_required_bytes: 60 * GIB, memory_free_after_bytes: 40 * GIB},
    ],
    allowed: true,
    steps: [{index: 0, kind: "install", label: "Install Qwen Chat", node_ids: ["node-alpha", "node-beta"]}],
    blockers: [],
    warnings: [],
    locations: {installation_ids: [], run_ids: [], installed: false, running: false},
    plan_digest: "d".repeat(64),
    ...overrides,
  };
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
  const managedRecipeCatalogSyncStatus = vi.fn().mockResolvedValue(catalogSync({imported_count: 1, updated_count: 2, unchanged_count: 0}));
  const syncManagedRecipeCatalog = vi.fn().mockResolvedValue(catalogSync({trigger: "manual", imported_count: 1, updated_count: 2, unchanged_count: 0}));
  const api = {librarySnapshot: async () => librarySnapshot, listPublicRecipes: async () => ({repository: "CarstVaartjes/vonk-forge-recipes", commit: "c".repeat(40), recipes: [update]}), managedRecipeCatalogSyncStatus, syncManagedRecipeCatalog} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  expect(await screen.findByText("Update available · v1.0.0 → v1.2.0")).toBeVisible();
  await waitFor(() => expect(managedRecipeCatalogSyncStatus).toHaveBeenCalledTimes(1));
  expect(syncManagedRecipeCatalog).not.toHaveBeenCalled();
  expect(await screen.findByText(/Last update: 1 imported · 2 updated · 0 unchanged/)).toBeVisible();
  expect(screen.queryByText(/installed recipes? (?:is|are) withdrawn upstream/i)).not.toBeInTheDocument();
  const sync = screen.getByRole("button", {name: "Update from Vonk Forge remote"});
  expect(sync).toBeEnabled();
  await user.click(sync);
  await waitFor(() => expect(syncManagedRecipeCatalog).toHaveBeenCalledWith({expected_commit: "c".repeat(40), request_key: expect.any(String)}, expect.any(AbortSignal)));

  await user.click(screen.getByText("Recipe filters"));
  await user.selectOptions(screen.getByRole("combobox", {name: "Local status"}), "custom");
  const workSurface = screen.getByRole("region", {name: "Recipe inventory"});
  expect(workSurface).toHaveTextContent("Custom Runtime");
  expect(workSurface).not.toHaveTextContent("Qwen Chat");
  expect(screen.getByRole("region", {name: "Models"})).toHaveTextContent("Unlinked");
});

test("surfaces upstream withdrawal only for managed recipes installed on a Spark", async () => {
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const managedRecipeCatalogSyncStatus = vi.fn().mockResolvedValue(catalogSync({
    withdrawn_count: 3,
    withdrawn_recipes: [
      {recipe_id: "recipe-chat", recipe_uri: `vonk://catalog/vonk-forge/qwen-chat@sha256:${"a".repeat(64)}`},
      {recipe_id: "recipe-code", recipe_uri: `vonk://catalog/vonk-forge/qwen-code@sha256:${"b".repeat(64)}`},
      {recipe_id: "catalog-only", recipe_uri: `vonk://catalog/vonk-forge/catalog-only@sha256:${"c".repeat(64)}`},
    ],
  }));
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: async () => fullLibraryDetail,
    listPublicRecipes: async () => ({repository: "CarstVaartjes/vonk-forge-recipes", commit: "c".repeat(40), recipes: []}),
    managedRecipeCatalogSyncStatus,
    visualFleet: async () => fleet([fleetNode({installed: [{recipe_id: "recipe-chat", rank_state: "installed"}] as VisualFleetNode["installed"]})]),
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  expect(await screen.findByText("1 installed recipe is withdrawn upstream. Installed content remains pinned until you review its removal.")).toBeVisible();
  const models = screen.getByRole("region", {name: "Models"});
  expect(within(models).getByText("Installed recipe withdrawn upstream")).toBeVisible();

  const chat = screen.getByRole("link", {name: /^Qwen ChatQwen Chat description/}).closest("article")!;
  const code = screen.getByRole("link", {name: /^Qwen CodeQwen Code description/}).closest("article")!;
  expect(within(chat).getByText("Withdrawn upstream · installed")).toBeVisible();
  expect(within(chat).getByText("Catalog updates unavailable")).toBeVisible();
  expect(within(code).getByText("Managed")).toBeVisible();
  expect(within(code).queryByText(/Withdrawn upstream/i)).not.toBeInTheDocument();

  const sparks = screen.getByRole("complementary", {name: "Sparks"});
  expect(within(sparks).getByText("Withdrawn upstream")).toBeVisible();
  expect(within(sparks).getByText("Installed content is withdrawn upstream")).toBeVisible();

  await user.click(screen.getByText("Recipe filters"));
  await user.selectOptions(screen.getByRole("combobox", {name: "Local status"}), "withdrawn");
  const recipes = screen.getByRole("region", {name: /Recipes for/});
  expect(recipes).toHaveTextContent("Qwen Chat");
  expect(recipes).not.toHaveTextContent("Qwen Code");
  expect(recipes).not.toHaveTextContent("Catalog Only");
});

test("opens one durable atomic placement preview from button, keyboard, and drag-and-drop", async () => {
  history.replaceState(null, "", "/library");
  const detail = structuredClone(fullLibraryDetail);
  detail.placement[0].recommendations[0].preview_targets = [{kind: "install", input: {mapping_id: "mapping-chat", recipe_build_id: "build-chat"}}];
  const previewLibraryPlacement = vi.fn(async (input: Parameters<ControlApi["previewLibraryPlacement"]>[0]) => placementPreview({desired_state: input.desired_state, invocation: input.invocation, selected_node_ids: input.node_ids}));
  const libraryRecipe = vi.fn(async () => detail);
  const api = {librarySnapshot: async () => librarySnapshot, libraryRecipe, visualFleet: async () => fleet([fleetNode(), fleetNode({id: "node-beta", display_name: "MIA Beta", hostname: "mia-beta.internal"})]), previewLibraryPlacement} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const recipe = (await screen.findByRole("link", {name: /^Qwen ChatQwen Chat description/})).closest("article")!;
  await user.click(within(recipe).getByRole("button", {name: "Place on Spark"}));
  await waitFor(() => expect(libraryRecipe).toHaveBeenCalledWith("recipe-chat"));
  expect(await screen.findByText("Choose a compatible Spark for Qwen Chat.")).toBeInTheDocument();
  expect(await screen.findAllByText("Atomic 2-Spark placement")).toHaveLength(2);
  await user.click(await screen.findByRole("button", {name: "Review placement on MIA Alpha"}));
  expect(await screen.findByRole("heading", {name: "Place Qwen Chat"})).toBeVisible();
  await waitFor(() => expect(previewLibraryPlacement).toHaveBeenCalledWith({alias: null, desired_state: "installed", invocation: "button", node_ids: ["node-alpha", "node-beta"], recipe_id: "recipe-chat"}, expect.any(AbortSignal)));
  await user.click(screen.getByRole("button", {name: "Close"}));

  const keyboardTarget = screen.getByRole("button", {name: "Review placement on MIA Alpha"});
  keyboardTarget.focus();
  await user.keyboard("{Enter}");
  expect(await screen.findByRole("heading", {name: "Place Qwen Chat"})).toBeVisible();
  await waitFor(() => expect(previewLibraryPlacement).toHaveBeenLastCalledWith({alias: null, desired_state: "installed", invocation: "keyboard", node_ids: ["node-alpha", "node-beta"], recipe_id: "recipe-chat"}, expect.any(AbortSignal)));
  await user.click(screen.getByRole("button", {name: "Close"}));

  const dataTransfer = {effectAllowed: "none", dropEffect: "none", setData: vi.fn(), getData: vi.fn()} as unknown as DataTransfer;
  fireEvent.dragStart(recipe, {dataTransfer});
  const spark = screen.getByText("MIA Alpha").closest("article")!;
  fireEvent.dragOver(spark, {dataTransfer});
  fireEvent.drop(spark, {dataTransfer});
  expect(await screen.findByRole("heading", {name: "Place Qwen Chat"})).toBeVisible();
  await waitFor(() => expect(previewLibraryPlacement).toHaveBeenLastCalledWith({alias: null, desired_state: "installed", invocation: "drag-drop", node_ids: ["node-alpha", "node-beta"], recipe_id: "recipe-chat"}, expect.any(AbortSignal)));
});

test("projects running, update, incompatible, and offline Spark states without flattening multi-Spark placement", () => {
  const record = buildLibraryRecipeRecords(librarySnapshot, [catalogRecipe({slug: "qwen-chat", title: "Qwen Chat", local: {status: "update-available", recipe_id: "recipe-chat", revision_number: 3, content_sha256: "a".repeat(64), release_version: "1.0.0"}})])[0]!;
  const installed = {recipe_id: "recipe-chat", rank_state: "installed"};
  const loaded = {recipe_id: "recipe-chat"};
  expect(sparkPlacementState(fleetNode({installed: [installed] as VisualFleetNode["installed"]}), [record])).toBe("update");
  expect(sparkPlacementState(fleetNode({installed: [installed] as VisualFleetNode["installed"]}), [{...record, withdrawnInstalled: true}])).toBe("withdrawn");
  expect(sparkPlacementState(fleetNode({loaded: [loaded] as VisualFleetNode["loaded"]}), [record])).toBe("running");
  expect(sparkPlacementState(fleetNode({loaded: [{...loaded, healthy: false}] as VisualFleetNode["loaded"]}), [record])).toBe("running-attention");
  expect(sparkPlacementState(fleetNode({id: "node-outside"}), [record], fullLibraryDetail)).toBe("incompatible");
  expect(sparkPlacementState(fleetNode({connection: {...fleetNode().connection, online_state: "offline", offline_reason: "stale"}}), [record], fullLibraryDetail)).toBe("offline");

  const visionOnly = filterLibraryRecipeRecords([record], {...EMPTY_LIBRARY_WORKCELL_FILTERS, capabilities: ["vision"]}, "");
  expect(deriveLibraryModels(visionOnly)).toEqual([]);
});

test("makes active-run removal impact explicit and exposes one server-previewed recipe removal", async () => {
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const node = fleetNode({
    installed: [{recipe_id: "recipe-chat", rank_state: "installed"}] as VisualFleetNode["installed"],
    loaded: [{recipe_id: "recipe-chat"}] as VisualFleetNode["loaded"],
  });
  const api = {librarySnapshot: async () => librarySnapshot, libraryRecipe: async () => fullLibraryDetail, visualFleet: async () => fleet([node])} as unknown as ControlApi;
  render(<App api={api}/>);

  const sparks = await screen.findByRole("complementary", {name: "Sparks"});
  expect(within(sparks).getAllByText("Running")).toHaveLength(2);
  expect(within(sparks).getByRole("group", {name: "Selected content on MIA Alpha"})).toHaveTextContent("RunningQwen Chat");
  expect(within(sparks).getByText("Active runs must stop before removal. You can still review the exact blocked plan.")).toBeVisible();
  expect(within(sparks).getByText("The catalog recipe remains available. The Controller preview decides whether shared model files stay on each Spark.")).toBeVisible();
  expect(within(sparks).getByRole("button", {name: "Review recipe removal"})).toBeEnabled();
  expect(within(sparks).queryByRole("button", {name: "Review model removal"})).not.toBeInTheDocument();
});

test("offers fleet-wide model deletion only for an installed exact model and opens its cascade preview", async () => {
  const modelDigest = "e".repeat(64);
  history.replaceState(null, "", `/library/models/${encodeURIComponent(`qwen/3@${modelDigest}`)}`);
  const nodes = [
    fleetNode({installed: [{recipe_id: "recipe-chat", rank_state: "installed"}] as VisualFleetNode["installed"]}),
    fleetNode({id: "node-beta", display_name: "MIA Beta", hostname: "mia-beta.internal", installed: [{recipe_id: "recipe-chat", rank_state: "installed"}] as VisualFleetNode["installed"]}),
  ];
  const deletionPlan: LibraryModelDeletionPlan = {
    active_run_count: 1,
    active_runs: [{alias: "chat", route_state: "published", run_id: "run-chat", state: "running"}],
    allowed: false,
    blockers: [{code: "model_delete.active_runs", detail: "Stop the complete run before deletion."}],
    bytes_removed: 120 * GIB,
    installations: [{installation_id: "installation-chat", installed_bytes: 120 * GIB, node_ids: ["node-alpha", "node-beta"], recipe_content_sha256: "a".repeat(64), recipe_id: "recipe-chat", recipe_revision_id: "revision-chat"}],
    model_title: "Qwen 3",
    model_version_sha256: modelDigest,
    nodes: [{installation_ids: ["installation-chat"], installed_bytes: 60 * GIB, node_id: "node-alpha", recipe_ids: ["recipe-chat"]}, {installation_ids: ["installation-chat"], installed_bytes: 60 * GIB, node_id: "node-beta", recipe_ids: ["recipe-chat"]}],
    plan_digest: "model-delete-plan",
    shared_cache_policy: "Unrelated immutable caches stay installed.",
    warnings: [],
  };
  const previewLibraryModelDeletion = vi.fn(async () => deletionPlan);
  const api = {librarySnapshot: async () => librarySnapshot, visualFleet: async () => fleet(nodes), previewLibraryModelDeletion} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const sparks = await screen.findByRole("complementary", {name: "Sparks"});
  expect(within(sparks).getByText("Qwen 3 is used by 1 installed recipe across 2 Sparks.")).toBeVisible();
  await user.click(within(sparks).getByRole("button", {name: "Review model deletion"}));
  const dialog = await screen.findByRole("dialog", {name: "Delete Qwen 3 from Sparks"});
  expect(within(dialog).getByRole("heading", {name: "1 active run blocks deletion"})).toBeVisible();
  expect(within(dialog).getByText(/Shared cache policy:.*Unrelated immutable caches stay installed\./)).toBeVisible();
  expect(previewLibraryModelDeletion).toHaveBeenCalledWith(modelDigest, expect.any(AbortSignal));
});
