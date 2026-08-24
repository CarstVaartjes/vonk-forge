import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {useState} from "react";
import type {CatalogApi, PublicRecipe, PublicRecipePreview} from "../api/types";
import {PublicRecipeImportPage, parsePublicRecipeImportUrl, publicRecipeImportUrl, publicRecipeMatches, type PublicRecipeFilters} from "./public-recipe-import";

const EMPTY_FILTERS: PublicRecipeFilters = {
  query: "", model: "", creator: "", repository: "", sparks: "", runtime: "", precision: "", topology: "",
  qualification: "", local: "", sort: "catalog", capabilities: [],
};

function recipe(slug: string, overrides: Partial<PublicRecipe> = {}): PublicRecipe {
  return {
    publisher: "vonk-forge", slug, title: `${slug} recipe`, description: `Recipe for ${slug}`, tags: [slug],
    uri: `vonk://catalog/vonk-forge/${slug}@sha256:${slug.padEnd(64, "0").slice(0, 64)}`,
    content_sha256: slug.padEnd(64, "0").slice(0, 64), model_publisher: "models", model_slug: slug,
    model_title: slug, source_owner: "MiaLabs", source_repository: `https://github.com/MiaLabs/${slug}`,
    capabilities: ["chat"], qualification: "candidate", qualification_basis: "explicit-candidate-metadata",
    qualification_detail: "This immutable recipe explicitly declares candidate qualification.", precision: "BF16",
    execution_harness: "vllm-openai", runtime_distribution: "vllm-0-27-1", source_bundle_sha256: "9".repeat(64),
    artifact_count: 1, topology_name: "single-spark", topology_mode: "single", node_count: 1,
    expected_download_bytes: 1024, maximum_installed_bytes_per_node: 2048, maximum_runtime_memory_bytes_per_node: 1024,
    release_version: "1.1.0", release_released_at: "2026-08-24",
    local: {status: "not-imported", recipe_id: null, revision_number: null, content_sha256: null, release_version: null},
    ...overrides,
  };
}

function preview(value: PublicRecipe): PublicRecipePreview {
  return {...value, source: "recipe_library", changes_since_local: []};
}

function apiFor(recipes: PublicRecipe[], overrides: Partial<CatalogApi> = {}): CatalogApi {
  return {
    listPublicRecipes: async () => ({repository: "CarstVaartjes/vonk-forge-recipes", commit: "a".repeat(40), recipes}),
    previewPublicRecipe: async (uri: string) => preview(recipes.find(value => value.uri === uri) ?? recipe("manual", {uri})),
    importPublicRecipe: async () => ({recipe_id: "local", revision_number: 1, lifecycle: "draft", slug: "local"}),
    ...overrides,
  } as unknown as CatalogApi;
}

function Harness({api, initialUrl = "/library/import"}: {api: CatalogApi; initialUrl?: string}) {
  const [url, setUrl] = useState(initialUrl);
  return <PublicRecipeImportPage api={api} url={url} onNavigate={setUrl}/>;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => { resolve = onResolve; reject = onReject; });
  return {promise, resolve, reject};
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

it("round-trips validated URL state with repeated multi-select capabilities", () => {
  const parsed = parsePublicRecipeImportUrl("/library/import?q=glm&sparks=4%2B&qualification=candidate&local=bogus&sort=bogus&capability=chat&capability=vision&capability=chat&capability=bogus&more=1&recipe=immutable&step=confirm");
  expect(parsed).toMatchObject({more: true, recipe: "immutable", step: "confirm", filters: {query: "glm", sparks: "4+", qualification: "candidate", local: "", sort: "catalog", capabilities: ["chat", "vision"]}});
  expect(publicRecipeImportUrl(parsed.filters, {more: parsed.more, recipe: parsed.recipe, step: parsed.step})).toBe("/library/import?q=glm&sparks=4%2B&qualification=candidate&capability=chat&capability=vision&more=1&recipe=immutable&step=confirm");
  expect(parsePublicRecipeImportUrl("/library/import?step=confirm").step).toBe("catalog");
});

it("uses exactly 1, 2, 3 and 4+ Spark facets and ANDs capability selections", async () => {
  const both = recipe("both", {node_count: 4, capabilities: ["chat", "vision"]});
  const chat = recipe("chat", {node_count: 3, capabilities: ["chat"]});
  render(<Harness api={apiFor([both, chat])}/>);
  const sparks = await screen.findByRole("combobox", {name: "Filter by required Sparks"});
  expect(within(sparks).getAllByRole("option").map(option => option.getAttribute("value"))).toEqual(["", "1", "2", "3", "4+"]);
  expect(within(sparks).queryByRole("option", {name: /^4 Sparks/})).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("checkbox", {name: /Chat/}));
  await userEvent.click(screen.getByRole("checkbox", {name: /Vision/}));
  expect(screen.getByRole("heading", {name: "both", level: 3})).toBeVisible();
  expect(screen.queryByRole("heading", {name: "chat", level: 3})).not.toBeInTheDocument();
});

it("persists the compact catalog preference independently from URL state", async () => {
  const alpha = recipe("Alpha");
  const first = render(<Harness api={apiFor([alpha])}/>);
  const compact = await screen.findByRole("button", {name: "Compact"});
  await userEvent.click(compact);
  expect(compact).toHaveAttribute("aria-pressed", "true");
  expect(localStorage.getItem("vonk.public-recipe-catalog.view")).toBe("compact");
  first.unmount();

  render(<Harness api={apiFor([alpha])}/>);
  expect(await screen.findByRole("button", {name: "Compact"})).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", {name: "Cards"})).toHaveAttribute("aria-pressed", "false");
});

it("shows friendly topology and honest requirement graphics while keeping immutable IDs collapsed", async () => {
  const distributed = recipe("Distributed", {node_count: 3, topology_mode: "tensor_parallel"});
  render(<Harness api={apiFor([distributed])}/>);
  await userEvent.click(await screen.findByRole("button", {name: `Review ${distributed.title}`}));

  const topology = await screen.findByRole("region", {name: "3 Sparks · Tensor parallel"});
  expect(within(topology).getByRole("img", {name: /One leader Spark serving the endpoint and 2 worker Sparks connected over shared fabric/})).toBeVisible();
  expect(within(topology).getByText("Leader")).toBeVisible();
  expect(within(topology).getAllByText("Worker")).toHaveLength(2);
  const requirements = screen.getByRole("region", {name: "Largest per-Spark envelope"});
  expect(within(requirements).getAllByRole("meter")).toHaveLength(3);
  expect(within(requirements).getByText(/do not claim a fit against your current fleet/)).toBeVisible();

  const digest = screen.getByText(`sha256:${distributed.content_sha256}`);
  expect(digest).not.toBeVisible();
  await userEvent.click(screen.getByText("Technical details"));
  expect(digest).toBeVisible();
});

it("compares up to three recipes in an accessible human-readable table", async () => {
  const alpha = recipe("Alpha");
  const beta = recipe("Beta", {node_count: 2, precision: "FP8"});
  render(<Harness api={apiFor([alpha, beta])}/>);
  await userEvent.click(await screen.findByRole("checkbox", {name: /Compare.*Alpha/}));
  await userEvent.click(screen.getByRole("checkbox", {name: /Compare.*Beta/}));
  const compareButton = screen.getByRole("button", {name: "Compare 2 recipes"});
  expect(compareButton).toBeEnabled();
  await userEvent.click(compareButton);
  const table = screen.getByRole("table", {name: "Selected public recipe comparison"});
  expect(within(table).getByRole("columnheader", {name: "Alpha"})).toBeVisible();
  expect(within(table).getByRole("columnheader", {name: "Beta"})).toBeVisible();
  expect(within(table).getByRole("row", {name: /Sparks 1 Spark 2 Sparks/})).toBeVisible();
});

it("hydrates creator and original-repository filters with conditional zero counts", async () => {
  const alpha = recipe("Alpha", {source_owner: "MiaLabs", capabilities: ["chat"]});
  const beta = recipe("Beta", {source_owner: "MiaLabs", capabilities: ["vision"], qualification: "cataloged", qualification_basis: "explicit-accepted-metadata", qualification_detail: "Explicitly accepted."});
  render(<Harness api={apiFor([alpha, beta])} initialUrl={`/library/import?creator=MiaLabs&repository=${encodeURIComponent(alpha.source_repository!)}&more=1`}/>);
  expect(await screen.findByRole("heading", {name: "Alpha", level: 3})).toBeVisible();
  expect(screen.queryByRole("heading", {name: "Beta", level: 3})).not.toBeInTheDocument();
  expect(screen.getByRole("combobox", {name: "Filter by creator"})).toHaveValue("MiaLabs");
  expect(screen.getByRole("combobox", {name: "Filter by original repository"})).toHaveValue(alpha.source_repository);
  expect(screen.getByRole("checkbox", {name: /Vision0/})).toBeDisabled();
  expect(screen.getByRole("button", {name: /Creator: MiaLabs/})).toBeVisible();
  expect(screen.getByRole("button", {name: /Repository: MiaLabs\/Alpha/})).toBeVisible();
});

it("aborts and ignores a stale preview when the user selects another recipe", async () => {
  const alpha = recipe("Alpha");
  const beta = recipe("Beta");
  const alphaResult = deferred<PublicRecipePreview>();
  const betaResult = deferred<PublicRecipePreview>();
  const signals: AbortSignal[] = [];
  const previewPublicRecipe = vi.fn((uri: string, signal?: AbortSignal) => {
    if (signal) signals.push(signal);
    return uri === alpha.uri ? alphaResult.promise : betaResult.promise;
  });
  render(<Harness api={apiFor([alpha, beta], {previewPublicRecipe})}/>);
  await screen.findByRole("heading", {name: "Alpha", level: 3});
  await userEvent.click(screen.getByRole("button", {name: `Review ${alpha.title}`}));
  await waitFor(() => expect(previewPublicRecipe).toHaveBeenCalledTimes(1));
  await userEvent.click(screen.getByRole("button", {name: `Review ${beta.title}`}));
  await waitFor(() => expect(previewPublicRecipe).toHaveBeenCalledTimes(2));
  expect(signals[0]?.aborted).toBe(true);
  betaResult.resolve(preview(beta));
  expect(await screen.findByRole("heading", {name: beta.title, level: 2})).toHaveFocus();
  alphaResult.resolve(preview(alpha));
  await waitFor(() => expect(screen.queryByRole("heading", {name: alpha.title, level: 2})).not.toBeInTheDocument());
});

it("shows explicit candidate evidence and requires a separate confirmation step", async () => {
  const candidate = recipe("Candidate");
  render(<Harness api={apiFor([candidate])}/>);
  await userEvent.click(await screen.findByRole("button", {name: `Review ${candidate.title}`}));
  expect(await screen.findByText(candidate.qualification_detail)).toBeVisible();
  expect(screen.queryByRole("button", {name: "Import candidate"})).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", {name: "Continue to confirm"}));
  expect(await screen.findByRole("button", {name: "Import candidate"})).toBeVisible();
  expect(screen.getByText("Import this candidate?")).toBeVisible();
});

it("summarizes the strongest required action across every release since local", async () => {
  const upgrade = recipe("Upgrade", {local: {status: "update-available", recipe_id: "local", revision_number: 1, content_sha256: "1".repeat(64), release_version: "1.0.0"}});
  const upgradePreview = {...preview(upgrade), changes_since_local: [
    {version: "1.1.0", released_at: "2026-08-23", content_sha256: "2".repeat(64), upgrade_effect: "restart" as const, changes: [{kind: "performance" as const, summary: "Faster decode", details: null, references: []}]},
    {version: "1.2.0", released_at: "2026-08-24", content_sha256: "3".repeat(64), upgrade_effect: "rebuild" as const, changes: [{kind: "compatibility" as const, summary: "New runtime image", details: null, references: []}]},
  ]};
  render(<Harness api={apiFor([upgrade], {previewPublicRecipe: async () => upgradePreview})}/>);
  await userEvent.click(await screen.findByRole("button", {name: `Review update for ${upgrade.title}`}));
  expect(await within(screen.getByLabelText("Version summary")).findByText("Rebuild required")).toBeVisible();
});

it("locks catalog navigation while an import is pending", async () => {
  const alpha = recipe("Alpha");
  const beta = recipe("Beta");
  const importResult = deferred<Awaited<ReturnType<CatalogApi["importPublicRecipe"]>>>();
  const importPublicRecipe = vi.fn(() => importResult.promise);
  render(<Harness api={apiFor([alpha, beta], {importPublicRecipe})}/>);
  await userEvent.click(await screen.findByRole("button", {name: `Review ${alpha.title}`}));
  await userEvent.click(await screen.findByRole("button", {name: "Continue to confirm"}));
  await userEvent.click(await screen.findByRole("button", {name: "Import candidate"}));
  await waitFor(() => expect(importPublicRecipe).toHaveBeenCalledTimes(1));
  expect(screen.getByRole("button", {name: "Back to review"})).toBeDisabled();
  expect(screen.getByRole("button", {name: `Review ${beta.title}`})).toBeDisabled();
  expect(screen.getByRole("button", {name: "Refresh public catalog"})).toBeDisabled();
  expect(screen.getByRole("combobox", {name: "Filter by model"})).toBeDisabled();
  expect(screen.getByRole("link", {name: "← Library"})).toHaveAttribute("aria-disabled", "true");
  importResult.resolve({recipe_id: "alpha-local", revision_number: 1, lifecycle: "draft", slug: "alpha"} as Awaited<ReturnType<CatalogApi["importPublicRecipe"]>>);
  expect(await screen.findByText("Import complete")).toBeVisible();
});

it("distinguishes an empty catalog, filtered zero state, and a retryable load error", async () => {
  const emptyApi = apiFor([]);
  const {unmount} = render(<Harness api={emptyApi}/>);
  expect(await screen.findByText("The public catalog is empty")).toBeVisible();
  unmount();
  const one = recipe("Only");
  const filteredApi = apiFor([one]);
  const filteredView = render(<Harness api={filteredApi} initialUrl="/library/import?q=missing"/>);
  expect(await screen.findByText("No matching recipes")).toBeVisible();
  filteredView.unmount();
  const failing = apiFor([], {listPublicRecipes: vi.fn().mockRejectedValue(new Error("network down"))});
  render(<Harness api={failing}/>);
  expect(await screen.findByRole("alert")).toHaveTextContent("network down");
  expect(screen.getByRole("button", {name: "Try again"})).toBeVisible();
});

it("keeps filtering correct at the API maximum of 256 recipes", async () => {
  const recipes = Array.from({length: 256}, (_, index) => recipe(`Model ${index}`, {capabilities: index === 255 ? ["audio"] : ["chat"]}));
  render(<Harness api={apiFor(recipes)} initialUrl="/library/import?capability=audio"/>);
  expect(await screen.findByRole("heading", {name: "Model 255", level: 3})).toBeVisible();
  expect(screen.getAllByRole("listitem").filter(element => element.closest("[aria-label='Public recipes']"))).toHaveLength(1);
  expect(publicRecipeMatches(recipes[255], {...EMPTY_FILTERS, capabilities: ["audio"]})).toBe(true);
});
