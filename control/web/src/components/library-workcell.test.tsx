import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi, LibrarySnapshot, PublicRecipe, VisualFleetNode, VisualFleetSnapshot} from "../api/types";
import {App} from "../app";
import {librarySnapshot} from "../test-fixtures/library";
import {buildLibraryRecipeRecords, EMPTY_LIBRARY_WORKCELL_FILTERS, filterLibraryRecipeRecords, sparkPlacementState} from "./library-workcell";

const GIB = 1024 ** 3;

function catalogRecipe(overrides: Partial<PublicRecipe> = {}): PublicRecipe {
  return {
    publisher: "vonk-forge", slug: "qwen-chat", title: "Qwen Chat", description: "Repository recipe", tags: [],
    uri: `vonk://catalog/vonk-forge/qwen-chat@sha256:${"b".repeat(64)}`, content_sha256: "b".repeat(64),
    model_publisher: "qwen", model_slug: "qwen", model_title: "Qwen 3.5", model_version_publisher: "qwen", model_version_slug: "qwen-3-5-bf16", model_version_title: "Qwen 3.5 BF16",
    source_owner: "QwenLM", source_repository: "https://github.com/QwenLM/Qwen3", alignment: "standard", capabilities: ["chat"], qualification: "cataloged", qualification_basis: "explicit-accepted-metadata", qualification_detail: "Accepted.",
    precision: "BF16", quantizations: ["BF16"], execution_readiness: "executable", execution_readiness_basis: "explicit-executable-metadata", execution_readiness_detail: "Executable.", execution_harness: "vllm-openai", runtime_distribution: "vllm-0-27-1", source_bundle_sha256: "9".repeat(64), artifact_count: 1, artifact_identities: [], temporary_build_bytes_per_node: 0,
    topology_name: "single", topology_mode: "single", node_count: 1, topology_roles: [{name: "entrypoint", count: 1, endpoint_owner: true, disk: {image_bytes: 0, artifact_bytes: 0, staging_bytes: 0, cache_bytes: 0, rollback_bytes: 0, safety_margin_bytes: 0}}], fabric: {connectivity: "none", minimum_bandwidth_mbps: 0},
    expected_download_bytes: 20 * GIB, maximum_installed_bytes_per_node: 25 * GIB, maximum_runtime_memory_bytes_per_node: 48 * GIB, release_version: "1.0.0", release_released_at: "2026-08-30",
    local: {status: "current", recipe_id: "recipe-chat", revision_number: 1, content_sha256: "a".repeat(64), release_version: "1.0.0"},
    ...overrides,
  };
}

function node(id: string, displayName: string, recipeIds: string[] = []): VisualFleetNode {
  return {id, display_name: displayName, hostname: `${id}.internal`, labels: {}, lifecycle: "ready", connection: {agent_state: "active", certificate_state: "valid", last_seen_age_seconds: 1, last_seen_at: "2026-09-03T12:00:00Z", offline_reason: null, online_state: "online"}, installed: recipeIds.map(recipe_id => ({recipe_id, rank_state: "installed"})) as VisualFleetNode["installed"], loaded: [], inventory: null, telemetry: null, reservations: {disk_bytes: 0, gpu_memory_bytes: 0, host_memory_bytes: 0, port_count: 0, unified_memory_bytes: 0}, warnings: []} as unknown as VisualFleetNode;
}

function fleet(nodes: VisualFleetNode[]): VisualFleetSnapshot {
  return {schema_version: 1, generated_at: "2026-09-03T12:00:00Z", authority_revision: "f".repeat(64), event_cursor: 1, nodes};
}

afterEach(() => { history.replaceState(null, "", "/"); vi.restoreAllMocks(); });

test("renders the website recipe columns with Installed on first and precise Spark filters", async () => {
  history.replaceState(null, "", "/library");
  const recipe = catalogRecipe();
  const api = {librarySnapshot: async () => librarySnapshot, listPublicRecipes: async () => ({repository: "CarstVaartjes/vonk-forge-recipes", commit: "c".repeat(40), recipes: [recipe]}), visualFleet: async () => fleet([node("spark-1", "Spark One", ["recipe-chat"])])} as unknown as ControlApi;
  render(<App api={api}/>);

  const table = await screen.findByRole("table", {name: /Recipes synchronized from the repository/});
  const headers = within(table).getAllByRole("columnheader");
  expect(headers.slice(0, 8).map(header => header.textContent)).toEqual(expect.arrayContaining([expect.stringMatching(/^Installed on/), expect.stringMatching(/^Recipe/), expect.stringMatching(/^Model family/), expect.stringMatching(/^Model/), expect.stringMatching(/^Format/), expect.stringMatching(/^Runtime/), expect.stringMatching(/^Abliterated/), expect.stringMatching(/^Sparks/)]));
  expect(headers[0]).toHaveTextContent("Installed on");
  expect(headers[4]).toHaveTextContent("Format");
  expect(headers[5]).toHaveTextContent("Runtime");
  expect(within(screen.getByRole("combobox", {name: "Sparks"})).getAllByRole("option").map(option => option.textContent)).toEqual(["Any count", "1 Spark", "2 Sparks", "3 Sparks", "4+ Sparks"]);
  expect(within(screen.getByRole("combobox", {name: "Abliterated"})).getAllByRole("option").map(option => option.textContent)).toEqual(["True or False", "True", "False"]);
  expect(screen.queryByText(/import/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/recipe review/i)).not.toBeInTheDocument();
});

test("filters repository recipes by installation location", async () => {
  history.replaceState(null, "", "/library");
  const notInstalled = catalogRecipe({slug: "glm", title: "GLM 5.3 Flash", uri: `vonk://catalog/vonk-forge/glm@sha256:${"d".repeat(64)}`, local: {status: "not-imported", recipe_id: null, revision_number: null, content_sha256: null, release_version: null}});
  const api = {librarySnapshot: async () => librarySnapshot, listPublicRecipes: async () => ({repository: "CarstVaartjes/vonk-forge-recipes", commit: "c".repeat(40), recipes: [catalogRecipe(), notInstalled]}), visualFleet: async () => fleet([node("spark-1", "Spark One", ["recipe-chat"]), node("spark-2", "Spark Two")])} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);
  const installedOn = await screen.findByRole("combobox", {name: "Installed on"});

  await user.selectOptions(installedOn, "spark-1");
  expect(screen.getByRole("row", {name: /Qwen Chat/})).toHaveTextContent("Spark One");
  expect(screen.queryByRole("row", {name: /GLM 5.3 Flash/})).not.toBeInTheDocument();

  await user.selectOptions(installedOn, "not-installed");
  expect(screen.getByRole("row", {name: /GLM 5.3 Flash/})).toHaveTextContent("Not installed");
  expect(screen.queryByRole("row", {name: /Qwen Chat/})).not.toBeInTheDocument();
});

test("includes repository-only recipes while automatic synchronization catches up", () => {
  const empty: LibrarySnapshot = {...librarySnapshot, models: [], unlinked_recipes: []};
  const recipe = catalogRecipe({local: {status: "not-imported", recipe_id: null, revision_number: null, content_sha256: null, release_version: null}});
  const records = buildLibraryRecipeRecords(empty, [recipe]);
  expect(records).toHaveLength(1);
  expect(records[0]).toMatchObject({catalog: recipe, title: "Qwen Chat"});
  expect(records[0].recipe).toBeUndefined();
  expect(filterLibraryRecipeRecords(records, {...EMPTY_LIBRARY_WORKCELL_FILTERS, sparks: "1"}, "qwen")).toHaveLength(1);
});

test("surfaces unresolved model linkage for a repository recipe", () => {
  const unresolvedSnapshot: LibrarySnapshot = {
    ...librarySnapshot,
    models: [{...librarySnapshot.models[0]!, model_version: {...librarySnapshot.models[0]!.model_version!, state: "unknown"}, recipes: [librarySnapshot.models[0]!.recipes[0]!]}],
    unlinked_recipes: [],
  };
  const record = buildLibraryRecipeRecords(unresolvedSnapshot, [catalogRecipe()])[0]!;
  expect(record.modelLinkageError).toMatch(/could not resolve this model version/i);
});

test("preserves live Spark state projection for operational placement", () => {
  const record = buildLibraryRecipeRecords(librarySnapshot, [catalogRecipe({local: {status: "update-available", recipe_id: "recipe-chat", revision_number: 1, content_sha256: "a".repeat(64), release_version: "0.9.0"}})])[0]!;
  expect(sparkPlacementState(node("spark-1", "Spark One", ["recipe-chat"]), [record])).toBe("update");
  expect(sparkPlacementState({...node("spark-1", "Spark One"), connection: {...node("spark-1", "Spark One").connection, online_state: "offline", offline_reason: "stale"}}, [record])).toBe("offline");
});
