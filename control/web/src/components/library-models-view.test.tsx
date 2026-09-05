import {render, screen, within} from "@testing-library/react";
import {EMPTY_LIBRARY_WORKCELL_FILTERS, buildLibraryRecipeRecords} from "./library-workcell";
import {LibraryModelsView} from "./library-models-view";
import type {LibrarySnapshot, PublicRecipe} from "../api/types";
import {libraryRecipeSummary, librarySnapshot} from "../test-fixtures/library";

const knownCatalogRecipe: PublicRecipe = {
  publisher: "vonk-forge", slug: "qwen-chat", title: "Qwen Chat", description: "A cataloged Qwen recipe.", tags: ["chat"],
  uri: `vonk://catalog/vonk-forge/qwen-chat@sha256:${"c".repeat(64)}`, content_sha256: "c".repeat(64),
  model_publisher: "qwen", model_slug: "3", model_title: "Qwen 3", model_version_publisher: "qwen", model_version_slug: "3-bf16", model_version_title: "Qwen 3 BF16",
  source_owner: "QwenLM", source_repository: "https://github.com/QwenLM/Qwen3", alignment: "standard", capabilities: ["chat"], qualification: "cataloged",
  qualification_basis: "explicit-accepted-metadata", qualification_detail: "The immutable recipe declares accepted qualification.", precision: "BF16", quantizations: ["BF16"],
  execution_readiness: "executable", execution_readiness_basis: "explicit-executable-metadata", execution_readiness_detail: "The recipe declares an executable contract.",
  execution_harness: "vllm-openai", runtime_distribution: "vllm-0-27-1", source_bundle_sha256: "d".repeat(64), artifact_count: 1,
  topology_name: "pair", topology_mode: "tensor_parallel", node_count: 2, topology_roles: [{name: "leader", count: 1, endpoint_owner: true}, {name: "worker", count: 1, endpoint_owner: false}],
  fabric: {connectivity: "connected", minimum_bandwidth_mbps: 25_000}, expected_download_bytes: 80 * 1024 ** 3, maximum_installed_bytes_per_node: 100 * 1024 ** 3, maximum_runtime_memory_bytes_per_node: 72 * 1024 ** 3,
  release_version: "1.2.0", release_released_at: "2026-08-24", local: {status: "current", recipe_id: "recipe-chat", revision_number: 3, content_sha256: "a".repeat(64), release_version: "1.2.0"},
};

function renderModels(snapshot: LibrarySnapshot = librarySnapshot, catalog: PublicRecipe[] = [knownCatalogRecipe]) {
  const entries = buildLibraryRecipeRecords(snapshot, catalog);
  render(<LibraryModelsView
    api={{} as never}
    entries={entries}
    filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
    onFiltersChange={() => undefined}
    onNavigate={() => undefined}
    onQueryChange={() => undefined}
    query=""
  />);
}

test("uses the catalog family title, flattens a single variant, and keeps detached recipes in Recipes", () => {
  renderModels({
    ...librarySnapshot,
    models: [{...librarySnapshot.models[0]!, recipes: [librarySnapshot.models[0]!.recipes[0]!]}],
  });

  const families = screen.getByLabelText("Model families");
  expect(within(families).getByRole("button", {name: /Qwen 3/})).toBeVisible();
  expect(within(families).queryByRole("button", {name: /Unlinked/})).not.toBeInTheDocument();
  expect(within(families).getByRole("article")).toHaveTextContent("Qwen 3 BF16 · Qwen 3");
  expect(within(families).getByText("Recipe: Chat")).toBeVisible();
  expect(within(families).getByText("Capabilities unavailable")).toBeVisible();
  expect(screen.getByText(/Unlinked and custom recipes live in/)).toBeVisible();
});

test("shows an unknown linked model as an explicit evidence state", () => {
  const customRecipe = libraryRecipeSummary({recipe_id: "recipe-custom-linked", slug: "custom-linked", title: "Custom linked runtime", selected_revision: null, topology_name: "solo"});
  renderModels({
    ...librarySnapshot,
    models: [{model: {kind: "model-version", publisher: "local", slug: "runtime", content_sha256: "f".repeat(64)}, page_local: true, recipes: [customRecipe]}],
    unlinked_recipes: [],
  }, []);

  expect(screen.getByRole("button", {name: /Local Runtime/})).toBeVisible();
  expect(screen.getByText("Capabilities unavailable")).toBeVisible();
  expect(screen.getByText("Cache status unavailable")).toBeVisible();
  expect(screen.queryByText(/Model support not declared|Exact artifact evidence required/)).not.toBeInTheDocument();
});
