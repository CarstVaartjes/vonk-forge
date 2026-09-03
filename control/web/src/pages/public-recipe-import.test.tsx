import {act, fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {useState} from "react";
import type {CatalogApi, PublicRecipe, PublicRecipePreview} from "../api/types";
import {PublicRecipeImportPage, parsePublicRecipeImportUrl, publicRecipeImportUrl, publicRecipeMatches, type PublicRecipeFilters} from "./public-recipe-import";

const EMPTY_FILTERS: PublicRecipeFilters = {
  query: "", modelFamily: "", model: "", abliterated: "", sourceOwner: "", repository: "", sparks: "", runtime: "", quantization: "", updated: "",
  qualification: "", readiness: "", local: "", sort: "catalog", capabilities: [],
};

function recipe(slug: string, overrides: Partial<PublicRecipe> = {}): PublicRecipe {
  return {
    publisher: "vonk-forge", slug, title: `${slug} recipe`, description: `Recipe for ${slug}`, tags: [slug],
    uri: `vonk://catalog/vonk-forge/${slug}@sha256:${slug.padEnd(64, "0").slice(0, 64)}`,
    content_sha256: slug.padEnd(64, "0").slice(0, 64), model_publisher: "models", model_slug: slug,
    model_title: slug, model_version_publisher: "models", model_version_slug: `${slug}-v1`, model_version_title: `${slug} v1`,
    source_owner: "MiaLabs", source_repository: `https://github.com/MiaLabs/${slug}`,
    alignment: "standard",
    capabilities: ["chat"], qualification: "candidate", qualification_basis: "explicit-candidate-metadata",
    qualification_detail: "This immutable recipe explicitly declares candidate qualification.", precision: "BF16", quantizations: ["BF16"],
    execution_readiness: "executable", execution_readiness_basis: "explicit-executable-metadata",
    execution_readiness_detail: "The immutable recipe declares a complete executable contract.",
    execution_harness: "vllm-openai", runtime_distribution: "vllm-0-27-1", source_bundle_sha256: "9".repeat(64),
    artifact_count: 1, topology_name: "single-spark", topology_mode: "single", node_count: 1,
    topology_roles: [{name: "entrypoint", count: 1, endpoint_owner: true}],
    fabric: {connectivity: "none", minimum_bandwidth_mbps: 0},
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
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function advanceRequestTime(milliseconds: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(milliseconds);
  });
}

it("round-trips the shared catalog filter vocabulary", () => {
  const parsed = parsePublicRecipeImportUrl("/library/import?q=glm&model_family=GLM+5.3+Flash&model=models%2Fglm-v1&abliterated=true&creator=MiaLabs&quantization=NVFP4&updated=30&sparks=4%2B&qualification=candidate&readiness=integration-required&local=bogus&sort=bogus&capability=chat&capability=vision&capability=chat&capability=bogus&more=1&recipe=immutable&step=confirm");
  expect(parsed).toMatchObject({more: true, recipe: "immutable", step: "confirm", filters: {query: "glm", modelFamily: "GLM 5.3 Flash", model: "models/glm-v1", abliterated: "true", sourceOwner: "MiaLabs", quantization: "NVFP4", updated: "30", sparks: "4+", qualification: "candidate", readiness: "integration-required", local: "", sort: "catalog", capabilities: ["chat", "vision"]}});
  expect(publicRecipeImportUrl(parsed.filters, {more: parsed.more, recipe: parsed.recipe, step: parsed.step})).toBe("/library/import?q=glm&model_family=GLM+5.3+Flash&model=models%2Fglm-v1&abliterated=true&creator=MiaLabs&sparks=4%2B&quantization=NVFP4&updated=30&qualification=candidate&readiness=integration-required&capability=chat&capability=vision&more=1&recipe=immutable&step=confirm");
  expect(parsePublicRecipeImportUrl("/library/import?abliterated=unknown").filters.abliterated).toBe("");
  expect(parsePublicRecipeImportUrl("/library/import?readiness=executable").filters.readiness).toBe("executable");
  expect(parsePublicRecipeImportUrl("/library/import?readiness=ready").filters.readiness).toBe("");
  expect(parsePublicRecipeImportUrl("/library/import?step=confirm").step).toBe("catalog");
});

it("uses exactly 1, 2, 3 and 4+ Spark facets and ANDs capability selections", async () => {
  const both = recipe("both", {node_count: 4, capabilities: ["chat", "vision"]});
  const chat = recipe("chat", {node_count: 3, capabilities: ["chat"]});
  render(<Harness api={apiFor([both, chat])}/>);
  expect(await screen.findByRole("combobox", {name: "Filter by model family"})).toBeVisible();
  expect(screen.getByRole("combobox", {name: "Filter by model"})).toBeVisible();
  expect(screen.getByRole("combobox", {name: "Filter by quantization"})).toBeVisible();
  expect(screen.getByRole("combobox", {name: "Filter by abliterated"})).toBeVisible();
  expect(screen.getByRole("combobox", {name: "Filter by recipe creator"})).toBeVisible();
  expect(screen.getByRole("combobox", {name: "Filter by updated date"})).toBeVisible();
  await userEvent.click(await screen.findByRole("button", {name: "More filters"}));
  const sparks = await screen.findByRole("combobox", {name: "Filter by required Sparks"});
  expect(within(sparks).getAllByRole("option").map(option => option.getAttribute("value"))).toEqual(["", "1", "2", "3", "4+"]);
  expect(within(sparks).queryByRole("option", {name: /^4 Sparks/})).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("checkbox", {name: /Chat/}));
  await userEvent.click(screen.getByRole("checkbox", {name: /Vision/}));
  expect(screen.getByRole("heading", {name: "both", level: 3})).toBeVisible();
  expect(screen.queryByRole("heading", {name: "chat", level: 3})).not.toBeInTheDocument();
});

it("treats abliterated as a recipe-owned boolean", () => {
  const standard = recipe("standard", {alignment: "standard"});
  const abliterated = recipe("abliterated", {alignment: "abliterated"});
  expect(publicRecipeMatches(standard, {...EMPTY_FILTERS, abliterated: "false"})).toBe(true);
  expect(publicRecipeMatches(abliterated, {...EMPTY_FILTERS, abliterated: "false"})).toBe(false);
});

it("groups downloadable models beneath a human model family", async () => {
  const nvfp4 = recipe("glm-nvfp4", {model_title: "GLM 5.3 Flash NVFP4", model_version_publisher: "libertai", model_version_slug: "glm-nvfp4-caca", model_version_title: "GLM 5.3 Flash NVFP4 caca4e6a"});
  const exl3 = recipe("glm-exl3", {model_title: "GLM 5.3 Flash EXL3 TR3 with DFlash2", model_version_publisher: "mia", model_version_slug: "glm-exl3-024d", model_version_title: "GLM 5.3 Flash EXL3 TR3 4bpw plus DFlash2 024db9f7"});
  const qwen = recipe("qwen", {model_title: "Qwen3.5 9B", model_version_publisher: "qwen", model_version_slug: "qwen3-5-9b", model_version_title: "Qwen3.5 9B 71e3ab2c"});
  render(<Harness api={apiFor([nvfp4, exl3, qwen])}/>);

  const family = await screen.findByRole("combobox", {name: "Filter by model family"});
  const model = screen.getByRole("combobox", {name: "Filter by model"});
  expect(family.compareDocumentPosition(model) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(within(family).getByRole("option", {name: "GLM 5.3 Flash (2)"})).toBeEnabled();
  await userEvent.selectOptions(family, "GLM 5.3 Flash");
  expect(within(model).getByRole("option", {name: /GLM 5.3 Flash NVFP4 caca4e6a/})).toBeVisible();
  expect(within(model).getByRole("option", {name: /GLM 5.3 Flash EXL3/})).toBeVisible();
  expect(within(model).queryByRole("option", {name: /Qwen3.5/})).not.toBeInTheDocument();
});

it("keeps sorting with the result controls instead of treating it as an applied filter", async () => {
  render(<Harness api={apiFor([recipe("Zulu"), recipe("Alpha")])}/>);
  const sort = await screen.findByRole("combobox", {name: "Sort recipes"});
  expect(sort.closest(".public-import-results-tools")).not.toBeNull();
  await userEvent.selectOptions(sort, "model");
  expect(screen.queryByRole("button", {name: /Sort:/})).not.toBeInTheDocument();
  expect(screen.getAllByRole("heading", {level: 3}).map(element => element.textContent)).toEqual(["Alpha", "Zulu"]);
});

it("disambiguates distinct downloadable models that share a display title", async () => {
  const mova360 = recipe("mova-360p-recipe", {model_title: "MOVA", model_version_publisher: "openmoss", model_version_slug: "mova-360p", model_version_title: "MOVA checkpoint"});
  const mova720 = recipe("mova-720p-recipe", {model_title: "MOVA", model_version_publisher: "openmoss", model_version_slug: "mova-720p", model_version_title: "MOVA checkpoint"});
  render(<Harness api={apiFor([mova360, mova720])}/>);

  const models = await screen.findByRole("combobox", {name: "Filter by model"});
  expect(within(models).getByRole("option", {name: "MOVA checkpoint · openmoss/mova-360p (1)"})).toBeVisible();
  expect(within(models).getByRole("option", {name: "MOVA checkpoint · openmoss/mova-720p (1)"})).toBeVisible();

  await userEvent.selectOptions(models, "openmoss/mova-360p");
  expect(screen.getByRole("heading", {name: "MOVA", level: 3})).toBeVisible();
  expect(screen.getByText(mova360.title)).toBeVisible();
  expect(screen.queryByText(mova720.title)).not.toBeInTheDocument();
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
  expect(screen.getByRole("button", {name: "Detailed"})).toHaveAttribute("aria-pressed", "false");
});

it("shows friendly topology and honest requirement graphics while keeping immutable IDs collapsed", async () => {
  const distributed = recipe("Distributed", {node_count: 3, topology_mode: "tensor_parallel", topology_roles: [{name: "entrypoint", count: 1, endpoint_owner: true}, {name: "shard", count: 2, endpoint_owner: false}], fabric: {connectivity: "switch", minimum_bandwidth_mbps: 200_000}});
  render(<Harness api={apiFor([distributed])}/>);
  await userEvent.click(await screen.findByRole("button", {name: `Review ${distributed.title}`}));

  const topology = await screen.findByRole("region", {name: "3 Sparks · Tensor parallel"});
  expect(within(topology).getByRole("img", {name: /1 entrypoint Spark owning an endpoint, 2 shard Sparks.*Fabric switch · 200 Gbps minimum/})).toBeVisible();
  expect(within(topology).getByText("entrypoint")).toBeVisible();
  expect(within(topology).getByText("shard")).toBeVisible();
  expect(within(topology).getByText("2× Spark")).toBeVisible();
  expect(within(topology).queryByText("Leader")).not.toBeInTheDocument();
  expect(within(topology).queryByText("Worker")).not.toBeInTheDocument();
  const requirements = screen.getByRole("region", {name: "Largest per-Spark envelope"});
  expect(within(requirements).getAllByRole("meter")).toHaveLength(3);
  expect(within(requirements).getByText(/do not claim a fit against your current fleet/)).toBeVisible();

  const digest = screen.getByText(`sha256:${distributed.content_sha256}`);
  expect(digest).not.toBeVisible();
  await userEvent.click(screen.getByText("Technical details"));
  expect(digest).toBeVisible();
});

it("falls back to declared count and mode when projected topology details disagree", async () => {
  const malformed = recipe("Fallback", {node_count: 3, topology_mode: "distributed", topology_roles: [{name: "entrypoint", count: 1, endpoint_owner: true}], fabric: {connectivity: "none", minimum_bandwidth_mbps: 0}});
  render(<Harness api={apiFor([malformed])}/>);
  await userEvent.click(await screen.findByRole("button", {name: `Review ${malformed.title}`}));

  const topology = await screen.findByRole("region", {name: "3 Sparks · Distributed"});
  expect(within(topology).getByRole("img", {name: /Role and fabric details are unavailable/})).toBeVisible();
  expect(within(topology).getByText("Topology details unavailable")).toBeVisible();
  expect(within(topology).queryByText("Declared endpoint")).not.toBeInTheDocument();
  expect(within(topology).queryByText(/Fabric:/)).not.toBeInTheDocument();
});

it("compares up to three recipes in an accessible human-readable table", async () => {
  const alpha = recipe("Alpha");
  const beta = recipe("Beta", {node_count: 2, precision: "FP8", quantizations: ["FP8"]});
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

it("combines recipe creator and original repository filters", async () => {
  const alpha = recipe("Alpha", {source_owner: "MiaLabs", capabilities: ["chat"]});
  const beta = recipe("Beta", {source_owner: "MiaLabs", capabilities: ["vision"], qualification: "cataloged", qualification_basis: "explicit-accepted-metadata", qualification_detail: "Explicitly accepted."});
  render(<Harness api={apiFor([alpha, beta])} initialUrl={`/library/import?creator=MiaLabs&repository=${encodeURIComponent(alpha.source_repository!)}&more=1`}/>);
  expect(await screen.findByRole("heading", {name: "Alpha", level: 3})).toBeVisible();
  expect(screen.queryByRole("heading", {name: "Beta", level: 3})).not.toBeInTheDocument();
  expect(screen.getByRole("combobox", {name: "Filter by recipe creator"})).toHaveValue("MiaLabs");
  expect(screen.getByRole("combobox", {name: "Filter by original repository"})).toHaveValue(alpha.source_repository);
  expect(screen.getByRole("checkbox", {name: /Vision0/})).toBeDisabled();
  expect(screen.getByRole("button", {name: /Creator: MiaLabs/})).toBeVisible();
  expect(screen.getByRole("button", {name: /Repository: MiaLabs\/Alpha/})).toBeVisible();
});

it("lists every readiness status and blocks unsupported imports while preserving evidence", async () => {
  const executableCandidate = recipe("Executable", {execution_readiness: "executable", execution_readiness_basis: "explicit-executable-metadata", execution_readiness_detail: "Executable contract declared."});
  const blockedAccepted = recipe("Metadata", {qualification: "cataloged", qualification_basis: "explicit-accepted-metadata", qualification_detail: "Accepted review evidence.", execution_readiness: "not-executable", execution_readiness_basis: "explicit-non-executable-metadata", execution_readiness_detail: "No executable runtime contract."});
  const importPublicRecipe = vi.fn();
  const api = apiFor([executableCandidate, blockedAccepted], {importPublicRecipe});
  const view = render(<Harness api={api} initialUrl="/library/import?qualification=cataloged&readiness=not-executable"/>);

  expect(await screen.findByRole("heading", {name: "Metadata", level: 3})).toBeVisible();
  expect(screen.queryByRole("heading", {name: "Executable", level: 3})).not.toBeInTheDocument();
  const readiness = screen.getByRole("combobox", {name: "Filter by execution readiness"});
  expect(within(readiness).getAllByRole("option").map(option => option.getAttribute("value"))).toEqual(["", "executable", "integration-required", "not-executable", "not-declared"]);
  expect(screen.getByText("Not executable")).toBeVisible();
  expect(screen.getByText("Accepted · v1.1.0")).toBeVisible();
  await userEvent.click(screen.getByRole("button", {name: `Review ${blockedAccepted.title}`}));
  expect(await screen.findByText("No executable runtime contract.")).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("Import blocked: executable contract required");
  expect(screen.queryByRole("button", {name: "Continue to confirm"})).not.toBeInTheDocument();
  expect(screen.queryByRole("button", {name: /Import/})).not.toBeInTheDocument();
  expect(importPublicRecipe).not.toHaveBeenCalled();

  view.unmount();
  render(<Harness api={api} initialUrl={publicRecipeImportUrl(EMPTY_FILTERS, {recipe: blockedAccepted.uri, step: "confirm"})}/>);
  expect(await screen.findByRole("heading", {name: blockedAccepted.title, level: 2})).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("Import blocked: executable contract required");
  expect(screen.getByText("No executable runtime contract.")).toBeVisible();
  expect(screen.queryByRole("button", {name: /^Import/})).not.toBeInTheDocument();
  expect(importPublicRecipe).not.toHaveBeenCalled();
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
  const betaHeading = await screen.findByRole("heading", {name: beta.title, level: 2});
  await waitFor(() => expect(betaHeading).toHaveFocus());
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

it("hands an already imported recipe off to its local build and install controls", async () => {
  const current = recipe("Current", {local: {status: "current", recipe_id: "local/recipe", revision_number: 1, content_sha256: "1".repeat(64), release_version: "1.1.0"}});
  render(<Harness api={apiFor([current])} initialUrl={publicRecipeImportUrl(EMPTY_FILTERS, {recipe: current.uri, step: "review"})}/>);

  expect(await screen.findByText("Already in your local Library")).toBeVisible();
  expect(screen.getByText("Imported · current")).toBeVisible();
  const versionSummary = screen.getByLabelText("Version summary");
  expect(within(versionSummary).getByText("Local recipe")).toBeVisible();
  expect(within(versionSummary).getByText("Catalog release")).toBeVisible();
  expect(screen.getByText(/Build, map, install, and run/)).toBeVisible();
  expect(screen.getByRole("link", {name: "Open build & install controls"})).toHaveAttribute("href", "/library/recipes/local%2Frecipe");
  expect(screen.queryByRole("button", {name: "Already current"})).not.toBeInTheDocument();
});

it("keeps confirmation context after an import failure and retries the import operation", async () => {
  const candidate = recipe("Candidate");
  const firstImport = deferred<Awaited<ReturnType<CatalogApi["importPublicRecipe"]>>>();
  const previewPublicRecipe = vi.fn(async () => preview(candidate));
  const importPublicRecipe = vi.fn()
    .mockImplementationOnce(() => firstImport.promise)
    .mockResolvedValueOnce({recipe_id: "candidate-local", revision_number: 1, lifecycle: "draft", slug: "candidate"});
  render(<Harness api={apiFor([candidate], {previewPublicRecipe, importPublicRecipe})}/>);

  await userEvent.click(await screen.findByRole("button", {name: `Review ${candidate.title}`}));
  await userEvent.click(await screen.findByRole("button", {name: "Continue to confirm"}));
  await userEvent.click(await screen.findByRole("button", {name: "Import candidate"}));

  expect(await screen.findByText(`Importing ${candidate.title}`)).toBeVisible();
  expect(within(screen.getByRole("list", {name: "Import progress"})).getByText("Importing")).toBeVisible();
  firstImport.reject(new Error("local database is temporarily unavailable"));

  const failure = await screen.findByRole("alert");
  expect(failure).toHaveTextContent("Import failed");
  expect(failure).toHaveTextContent("local database is temporarily unavailable");
  expect(screen.queryByText("Preview unavailable")).not.toBeInTheDocument();
  expect(screen.getByRole("heading", {name: candidate.title, level: 2})).toBeVisible();
  expect(screen.getByText("Import this candidate?")).toBeVisible();

  await userEvent.click(within(failure).getByRole("button", {name: "Retry import"}));
  await waitFor(() => expect(importPublicRecipe).toHaveBeenCalledTimes(2));
  expect(previewPublicRecipe).toHaveBeenCalledTimes(1);
  expect(await screen.findByText(`Imported ${candidate.title}`)).toBeVisible();
});

it("keeps preview failures retryable without invoking import", async () => {
  const candidate = recipe("Candidate");
  const previewPublicRecipe = vi.fn()
    .mockRejectedValueOnce(new Error("preview verification failed"))
    .mockResolvedValueOnce(preview(candidate));
  const importPublicRecipe = vi.fn();
  render(<Harness api={apiFor([candidate], {previewPublicRecipe, importPublicRecipe})}/>);

  await userEvent.click(await screen.findByRole("button", {name: `Review ${candidate.title}`}));
  const failure = await screen.findByRole("alert");
  expect(failure).toHaveTextContent("Preview unavailable");
  expect(failure).toHaveTextContent("preview verification failed");
  await userEvent.click(within(failure).getByRole("button", {name: "Try again"}));

  expect(await screen.findByRole("heading", {name: candidate.title, level: 2})).toBeVisible();
  expect(previewPublicRecipe).toHaveBeenCalledTimes(2);
  expect(importPublicRecipe).not.toHaveBeenCalled();
});

it("validates and trims a manually entered immutable URI before preview", async () => {
  const manual = recipe("manual", {execution_readiness: "not-declared", execution_readiness_basis: "missing-readiness-metadata", execution_readiness_detail: "The immutable recipe does not declare execution readiness."});
  const previewPublicRecipe = vi.fn(async (uri: string) => preview({...manual, uri}));
  render(<Harness api={apiFor([], {previewPublicRecipe})}/>);
  await screen.findByText("The public catalog is empty");
  await userEvent.click(screen.getByText("Advanced: import URI"));
  const input = screen.getByRole("textbox", {name: "Public recipe URI"});

  await userEvent.type(input, "  not-an-immutable-uri  ");
  await userEvent.click(screen.getByRole("button", {name: "Review URI"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("vonk://catalog/publisher/slug@sha256:digest");
  expect(input).toHaveAttribute("aria-invalid", "true");
  expect(previewPublicRecipe).not.toHaveBeenCalled();

  const validUri = `vonk://catalog/vonk-forge/manual@sha256:${"a".repeat(64)}`;
  await userEvent.clear(input);
  await userEvent.type(input, `  ${validUri}  `);
  await userEvent.click(screen.getByRole("button", {name: "Review URI"}));
  await waitFor(() => expect(previewPublicRecipe).toHaveBeenCalledWith(validUri, expect.any(AbortSignal)));
  expect(input).toHaveValue(validUri);
  expect(screen.queryByText("Enter an immutable URI")).not.toBeInTheDocument();
  expect(await screen.findByText("The immutable recipe does not declare execution readiness.")).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("Import blocked: executable contract required");
  expect(screen.queryByRole("button", {name: "Continue to confirm"})).not.toBeInTheDocument();
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
  expect(screen.getByText(/saved in your local Library/)).toBeVisible();
  expect(screen.getByRole("link", {name: "Open build & install controls"})).toHaveAttribute("href", "/library/recipes/alpha-local");
  expect(screen.queryByText(/revision is ready/)).not.toBeInTheDocument();
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

it("shows a slow catalog refresh, times it out, and keeps the last good snapshot", async () => {
  const alpha = recipe("Alpha");
  const hungRefresh = deferred<Awaited<ReturnType<CatalogApi["listPublicRecipes"]>>>();
  let refreshSignal: AbortSignal | undefined;
  const listPublicRecipes = vi.fn((signal?: AbortSignal) => {
    if (listPublicRecipes.mock.calls.length === 1) return Promise.resolve({repository: "CarstVaartjes/vonk-forge-recipes", commit: "a".repeat(40), recipes: [alpha]});
    refreshSignal = signal;
    return hungRefresh.promise;
  });
  render(<Harness api={apiFor([alpha], {listPublicRecipes})}/>);
  expect(await screen.findByRole("heading", {name: "Alpha", level: 3})).toBeVisible();

  vi.useFakeTimers();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", {name: "Refresh public catalog"}));
    await Promise.resolve();
  });
  await advanceRequestTime(5_000);
  expect(screen.getByText("Catalog refresh is taking longer than expected")).toBeVisible();
  expect(screen.getByRole("button", {name: "Cancel refresh"})).toBeVisible();
  expect(screen.getByRole("heading", {name: "Alpha", level: 3})).toBeVisible();

  await advanceRequestTime(25_000);
  expect(screen.getByRole("alert")).toHaveTextContent("catalog refresh did not respond within 30 seconds");
  expect(screen.getByRole("alert")).toHaveTextContent("previous catalog snapshot is still shown");
  expect(screen.getByRole("heading", {name: "Alpha", level: 3})).toBeVisible();
  expect(refreshSignal?.aborted).toBe(true);
});

it("cancels a hung preview without an error, then bounds a retry and allows another retry", async () => {
  const alpha = recipe("Alpha");
  const firstPreview = deferred<PublicRecipePreview>();
  const secondPreview = deferred<PublicRecipePreview>();
  const signals: AbortSignal[] = [];
  const previewPublicRecipe = vi.fn((_uri: string, signal?: AbortSignal) => {
    if (signal) signals.push(signal);
    if (previewPublicRecipe.mock.calls.length === 1) return firstPreview.promise;
    if (previewPublicRecipe.mock.calls.length === 2) return secondPreview.promise;
    return Promise.resolve(preview(alpha));
  });
  render(<Harness api={apiFor([alpha], {previewPublicRecipe})}/>);
  expect(await screen.findByRole("heading", {name: "Alpha", level: 3})).toBeVisible();

  vi.useFakeTimers();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", {name: `Review ${alpha.title}`}));
    await Promise.resolve();
  });
  await advanceRequestTime(5_000);
  expect(screen.getByText("Recipe preview is taking longer than expected")).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "Cancel preview"}));
  expect(screen.getByText("Preview canceled")).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(signals[0]?.aborted).toBe(true);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", {name: "Try again"}));
    await Promise.resolve();
  });
  await advanceRequestTime(30_000);
  expect(screen.getByRole("alert")).toHaveTextContent("recipe preview did not respond within 30 seconds");
  expect(signals[1]?.aborted).toBe(true);

  await act(async () => {
    fireEvent.click(within(screen.getByRole("alert")).getByRole("button", {name: "Try again"}));
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByRole("heading", {name: alpha.title, level: 2})).toBeVisible();
  expect(previewPublicRecipe).toHaveBeenCalledTimes(3);
});

it("treats a stopped or timed-out import as unknown until status is rechecked", async () => {
  const alpha = recipe("Alpha");
  const hungImport = deferred<Awaited<ReturnType<CatalogApi["importPublicRecipe"]>>>();
  const initialPreview = preview(alpha);
  const previewPublicRecipe = vi.fn()
    .mockResolvedValueOnce(initialPreview)
    .mockResolvedValueOnce({...initialPreview, local: {...initialPreview.local, status: "current", recipe_id: "alpha-local"}});
  let importSignal: AbortSignal | undefined;
  const importPublicRecipe = vi.fn((_uri: string, _digest: string, signal?: AbortSignal) => {
    importSignal = signal;
    return hungImport.promise;
  });
  render(<Harness api={apiFor([alpha], {importPublicRecipe, previewPublicRecipe})}/>);
  await userEvent.click(await screen.findByRole("button", {name: `Review ${alpha.title}`}));
  await userEvent.click(await screen.findByRole("button", {name: "Continue to confirm"}));

  vi.useFakeTimers();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", {name: "Import candidate"}));
    await Promise.resolve();
  });
  await advanceRequestTime(5_000);
  expect(screen.getByText("Import is taking longer than expected")).toBeVisible();
  expect(screen.getByRole("button", {name: "Stop waiting"})).toBeEnabled();

  await advanceRequestTime(25_000);
  expect(screen.getByText("Stopped waiting — import outcome unknown")).toBeVisible();
  expect(screen.getByText(/server may still have completed the import/i)).toBeVisible();
  expect(importSignal?.aborted).toBe(true);
  expect(screen.queryByRole("button", {name: "Retry import"})).not.toBeInTheDocument();
  expect(screen.getByRole("link", {name: "Check local Library"})).toHaveAttribute("href", "/library");
  expect(screen.getByRole("button", {name: "Back to review"})).toBeEnabled();

  await act(async () => {
    fireEvent.click(screen.getByRole("button", {name: "Recheck status"}));
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(previewPublicRecipe).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("link", {name: "Open build & install controls"})).toHaveAttribute("href", "/library/recipes/alpha-local");
  expect(importPublicRecipe).toHaveBeenCalledTimes(1);
});

it("keeps filtering correct at the API maximum of 256 recipes", async () => {
  const recipes = Array.from({length: 256}, (_, index) => recipe(`Model ${index}`, {capabilities: index === 255 ? ["audio"] : ["chat"]}));
  render(<Harness api={apiFor(recipes)} initialUrl="/library/import?capability=audio"/>);
  expect(await screen.findByRole("heading", {name: "Model 255", level: 3})).toBeVisible();
  expect(screen.getAllByRole("listitem").filter(element => element.closest("[aria-label='Public recipes']"))).toHaveLength(1);
  expect(publicRecipeMatches(recipes[255], {...EMPTY_FILTERS, capabilities: ["audio"]})).toBe(true);
});
