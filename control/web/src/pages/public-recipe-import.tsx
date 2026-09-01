import {useEffect, useMemo, useRef, useState} from "react";
import type {CatalogApi, PublicRecipe, PublicRecipeAlignment, PublicRecipeCapability, PublicRecipeExecutionReadiness, PublicRecipePreview} from "../api/types";
import {formatBytes} from "../lib/fleet";
import "./public-recipe-import.css";

const PUBLIC_RECIPE_CAPABILITIES: Array<{value: PublicRecipeCapability; label: string}> = [
  {value: "chat", label: "Chat"},
  {value: "reasoning", label: "Reasoning"},
  {value: "vision", label: "Vision"},
  {value: "image-generation", label: "Image generation"},
  {value: "image-editing", label: "Image editing"},
  {value: "video", label: "Video"},
  {value: "audio", label: "Audio"},
  {value: "3d", label: "3D"},
];

const PUBLIC_RECIPE_READINESS: Array<{value: PublicRecipeExecutionReadiness; label: string}> = [
  {value: "executable", label: "Executable contract"},
  {value: "integration-required", label: "Integration required"},
  {value: "not-executable", label: "Not executable"},
  {value: "not-declared", label: "Readiness not declared"},
];

const PUBLIC_RECIPE_ALIGNMENTS: Array<{value: PublicRecipeAlignment; label: string}> = [
  {value: "standard", label: "Standard"},
  {value: "abliterated", label: "Abliterated"},
  {value: "derisked", label: "Derisked"},
  {value: "other-modified", label: "Other modified"},
  {value: "unspecified", label: "Unspecified"},
];

type SparkFilter = "" | "1" | "2" | "3" | "4+";
type RecipeSort = "catalog" | "model" | "sparks" | "download";
type LocalFilter = "" | "not-imported" | "update-available" | "current" | "needs-review";
type ModelType = "" | "language" | "vision" | "image" | "video" | "audio" | "3d";
type UpdatedFilter = "" | "7" | "30" | "90" | "365";
type ImportStep = "catalog" | "review" | "confirm";
type CatalogView = "cards" | "compact";
type Facet = "modelType" | "model" | "modelVersion" | "alignment" | "sourceOwner" | "repository" | "sparks" | "runtime" | "quantization" | "updated" | "topology" | "qualification" | "readiness" | "local" | "capability";
type ImportCompletion = {recipeId: string; title: string};

const MODEL_TYPE_OPTIONS: Array<{value: Exclude<ModelType, "">; label: string}> = [
  {value: "language", label: "Language / chat"},
  {value: "vision", label: "Vision / multimodal"},
  {value: "image", label: "Image"},
  {value: "video", label: "Video"},
  {value: "audio", label: "Audio"},
  {value: "3d", label: "3D"},
];

const CATALOG_VIEW_STORAGE_KEY = "vonk.public-recipe-catalog.view";
const PUBLIC_RECIPE_URI_PATTERN = /^vonk:\/\/catalog\/[a-z0-9][a-z0-9-]{1,62}\/[a-z0-9][a-z0-9-]{1,62}@sha256:[0-9a-f]{64}$/;
const REQUEST_SLOW_MS = 5_000;
const REQUEST_TIMEOUT_MS = 30_000;

class RequestTimeoutError extends Error {}

function runBoundedRequest<T>(controller: AbortController, onSlow: () => void, timeoutMessage: string, request: () => Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(slowTimer);
      window.clearTimeout(deadlineTimer);
      controller.signal.removeEventListener("abort", onAbort);
      callback();
    };
    const onAbort = () => finish(() => reject(new DOMException("The request was canceled", "AbortError")));
    const slowTimer = window.setTimeout(() => { if (!settled) onSlow(); }, REQUEST_SLOW_MS);
    const deadlineTimer = window.setTimeout(() => {
      finish(() => reject(new RequestTimeoutError(timeoutMessage)));
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
    controller.signal.addEventListener("abort", onAbort, {once: true});
    void Promise.resolve().then(request).then(
      value => finish(() => resolve(value)),
      error => finish(() => reject(error)),
    );
  });
}

export type PublicRecipeFilters = {
  query: string;
  modelType: ModelType;
  model: string;
  modelVersion: string;
  alignment: "" | PublicRecipeAlignment;
  sourceOwner: string;
  repository: string;
  sparks: SparkFilter;
  runtime: string;
  quantization: string;
  updated: UpdatedFilter;
  topology: string;
  qualification: "" | PublicRecipe["qualification"];
  readiness: "" | PublicRecipeExecutionReadiness;
  local: LocalFilter;
  sort: RecipeSort;
  capabilities: PublicRecipeCapability[];
};

const EMPTY_FILTERS: PublicRecipeFilters = {
  query: "", modelType: "", model: "", modelVersion: "", alignment: "", sourceOwner: "", repository: "", sparks: "", runtime: "", quantization: "", updated: "", topology: "",
  qualification: "", readiness: "", local: "", sort: "catalog", capabilities: [],
};

const VALID_SPARKS = new Set<SparkFilter>(["", "1", "2", "3", "4+"]);
const VALID_MODEL_TYPES = new Set<ModelType>(["", ...MODEL_TYPE_OPTIONS.map(option => option.value)]);
const VALID_SORTS = new Set<RecipeSort>(["catalog", "model", "sparks", "download"]);
const VALID_LOCAL = new Set<LocalFilter>(["", "not-imported", "update-available", "current", "needs-review"]);
const VALID_UPDATED = new Set<UpdatedFilter>(["", "7", "30", "90", "365"]);
const VALID_READINESS = new Set<PublicRecipeFilters["readiness"]>(["", ...PUBLIC_RECIPE_READINESS.map(option => option.value)]);
const VALID_ALIGNMENTS = new Set<PublicRecipeFilters["alignment"]>(["", ...PUBLIC_RECIPE_ALIGNMENTS.map(option => option.value)]);
const VALID_CAPABILITIES = new Set(PUBLIC_RECIPE_CAPABILITIES.map(option => option.value));

function storedCatalogView(): CatalogView {
  try {
    return window.localStorage.getItem(CATALOG_VIEW_STORAGE_KEY) === "compact" ? "compact" : "cards";
  } catch {
    return "cards";
  }
}

function saveCatalogView(view: CatalogView) {
  try {
    window.localStorage.setItem(CATALOG_VIEW_STORAGE_KEY, view);
  } catch {
    // A blocked storage preference must not block the catalog itself.
  }
}

export function parsePublicRecipeImportUrl(url: string): {filters: PublicRecipeFilters; more: boolean; recipe: string; step: ImportStep} {
  const search = new URL(url, "https://vonk.invalid").searchParams;
  const sparks = search.get("sparks") as SparkFilter | null;
  const modelType = search.get("model_type") as ModelType | null;
  const sort = search.get("sort") as RecipeSort | null;
  const local = search.get("local") as LocalFilter | null;
  const updated = search.get("updated") as UpdatedFilter | null;
  const qualification = search.get("qualification");
  const readiness = search.get("readiness") as PublicRecipeFilters["readiness"] | null;
  const alignment = search.get("alignment") as PublicRecipeFilters["alignment"] | null;
  const requestedStep = search.get("step");
  const recipe = search.get("recipe") ?? "";
  const step: ImportStep = recipe && requestedStep === "confirm" ? "confirm" : recipe ? "review" : "catalog";
  return {
    filters: {
      query: search.get("q") ?? "",
      modelType: modelType && VALID_MODEL_TYPES.has(modelType) ? modelType : "",
      model: search.get("model") ?? "",
      modelVersion: search.get("model_version") ?? "",
      alignment: alignment && VALID_ALIGNMENTS.has(alignment) ? alignment : "",
      sourceOwner: search.get("creator") ?? "",
      repository: search.get("repository") ?? "",
      sparks: sparks && VALID_SPARKS.has(sparks) ? sparks : "",
      runtime: search.get("runtime") ?? "",
      quantization: search.get("quantization") ?? "",
      updated: updated && VALID_UPDATED.has(updated) ? updated : "",
      topology: search.get("topology") ?? "",
      qualification: qualification === "candidate" || qualification === "cataloged" ? qualification : "",
      readiness: readiness && VALID_READINESS.has(readiness) ? readiness : "",
      local: local && VALID_LOCAL.has(local) ? local : "",
      sort: sort && VALID_SORTS.has(sort) ? sort : "catalog",
      capabilities: Array.from(new Set(search.getAll("capability").filter((value): value is PublicRecipeCapability => VALID_CAPABILITIES.has(value as PublicRecipeCapability)))),
    },
    more: search.get("more") === "1",
    recipe,
    step,
  };
}

export function publicRecipeImportUrl(filters: PublicRecipeFilters, options: {more?: boolean; recipe?: string; step?: ImportStep} = {}): string {
  const search = new URLSearchParams();
  if (filters.query) search.set("q", filters.query);
  if (filters.modelType) search.set("model_type", filters.modelType);
  if (filters.model) search.set("model", filters.model);
  if (filters.modelVersion) search.set("model_version", filters.modelVersion);
  if (filters.alignment) search.set("alignment", filters.alignment);
  if (filters.sourceOwner) search.set("creator", filters.sourceOwner);
  if (filters.repository) search.set("repository", filters.repository);
  if (filters.sparks) search.set("sparks", filters.sparks);
  if (filters.runtime) search.set("runtime", filters.runtime);
  if (filters.quantization) search.set("quantization", filters.quantization);
  if (filters.updated) search.set("updated", filters.updated);
  if (filters.topology) search.set("topology", filters.topology);
  if (filters.qualification) search.set("qualification", filters.qualification);
  if (filters.readiness) search.set("readiness", filters.readiness);
  if (filters.local) search.set("local", filters.local);
  if (filters.sort !== "catalog") search.set("sort", filters.sort);
  for (const capability of filters.capabilities) search.append("capability", capability);
  if (options.more) search.set("more", "1");
  if (options.recipe) search.set("recipe", options.recipe);
  if (options.recipe && options.step === "confirm") search.set("step", "confirm");
  const suffix = search.toString();
  return `/library/import${suffix ? `?${suffix}` : ""}`;
}

function sparkMatches(recipe: PublicRecipe, sparks: SparkFilter): boolean {
  if (!sparks) return true;
  if (sparks === "4+") return recipe.node_count >= 4;
  return recipe.node_count === Number(sparks);
}

function localMatches(recipe: PublicRecipe, local: LocalFilter): boolean {
  if (!local) return true;
  if (local === "needs-review") return ["different-revision", "local-ahead", "conflict"].includes(recipe.local.status);
  return recipe.local.status === local;
}

function modelTypeMatches(recipe: PublicRecipe, modelType: ModelType): boolean {
  if (!modelType) return true;
  if (modelType === "language") return recipe.capabilities.includes("chat") || recipe.capabilities.includes("reasoning");
  if (modelType === "vision") return recipe.capabilities.includes("vision");
  if (modelType === "image") return recipe.capabilities.includes("image-generation") || recipe.capabilities.includes("image-editing");
  return recipe.capabilities.includes(modelType);
}

function modelTypeLabel(modelType: Exclude<ModelType, "">): string {
  return MODEL_TYPE_OPTIONS.find(option => option.value === modelType)?.label ?? modelType;
}

function alignmentLabel(alignment: PublicRecipeAlignment | undefined): string {
  return PUBLIC_RECIPE_ALIGNMENTS.find(option => option.value === alignment)?.label ?? "Unspecified";
}

function modelVersionKey(recipe: PublicRecipe): string {
  return `${recipe.model_version_publisher}/${recipe.model_version_slug}`;
}

function modelVersionTitle(recipe: PublicRecipe): string {
  return recipe.model_version_title;
}

function recipeQuantizations(recipe: PublicRecipe): string[] {
  return recipe.quantizations;
}

function updatedMatches(recipe: PublicRecipe, updated: UpdatedFilter, now = new Date()): boolean {
  if (!updated) return true;
  if (!recipe.release_released_at) return false;
  const released = new Date(`${recipe.release_released_at}T00:00:00Z`);
  if (Number.isNaN(released.getTime())) return false;
  return released.getTime() >= now.getTime() - Number(updated) * 86_400_000;
}

export function publicRecipeMatches(recipe: PublicRecipe, filters: PublicRecipeFilters, omitted?: Facet): boolean {
  const normalized = filters.query.trim().toLowerCase();
  const queryMatches = !normalized || [recipe.title, recipe.slug, recipe.description, recipe.model_title, recipe.model_slug, modelVersionTitle(recipe), recipe.model_version_slug, recipe.source_owner ?? "", recipe.source_repository ?? "", recipe.runtime_distribution, alignmentLabel(recipe.alignment), ...recipeQuantizations(recipe), ...recipe.capabilities, ...recipe.tags].some(value => value.toLowerCase().includes(normalized));
  return queryMatches
    && (omitted === "modelType" || modelTypeMatches(recipe, filters.modelType))
    && (omitted === "model" || !filters.model || `${recipe.model_publisher}/${recipe.model_slug}` === filters.model)
    && (omitted === "modelVersion" || !filters.modelVersion || modelVersionKey(recipe) === filters.modelVersion)
    && (omitted === "alignment" || !filters.alignment || recipe.alignment === filters.alignment)
    && (omitted === "sourceOwner" || !filters.sourceOwner || recipe.source_owner === filters.sourceOwner)
    && (omitted === "repository" || !filters.repository || recipe.source_repository === filters.repository)
    && (omitted === "sparks" || sparkMatches(recipe, filters.sparks))
    && (omitted === "runtime" || !filters.runtime || recipe.runtime_distribution === filters.runtime)
    && (omitted === "quantization" || !filters.quantization || recipeQuantizations(recipe).includes(filters.quantization))
    && (omitted === "updated" || updatedMatches(recipe, filters.updated))
    && (omitted === "topology" || !filters.topology || recipe.topology_mode === filters.topology)
    && (omitted === "qualification" || !filters.qualification || recipe.qualification === filters.qualification)
    && (omitted === "readiness" || !filters.readiness || recipe.execution_readiness === filters.readiness)
    && (omitted === "local" || localMatches(recipe, filters.local))
    && (omitted === "capability" || filters.capabilities.every(capability => recipe.capabilities.includes(capability)));
}

function runtimeLabel(value: string): string {
  if (value.startsWith("vllm-")) return `vLLM ${value.slice(5).replaceAll("-", ".")}`;
  if (value.startsWith("diffusers-")) return `Diffusers ${value.slice(10).replaceAll("-", ".")}`;
  if (value.startsWith("pytorch-")) return `PyTorch ${value.slice(8).replaceAll("-", ".")}`;
  return value.replaceAll("-", " ");
}

function repositoryLabel(value: string): string {
  try {
    const parsed = new URL(value);
    return parsed.pathname.replace(/^\//, "").replace(/\.git$/, "") || parsed.hostname;
  } catch {
    return value;
  }
}

function sparkLabel(count: number): string {
  return `${count} Spark${count === 1 ? "" : "s"}`;
}

function localRecipePath(recipeId: string): string {
  return `/library/recipes/${encodeURIComponent(recipeId)}`;
}

function localStatusLabel(recipe: PublicRecipe): string {
  if (recipe.local.status === "current") return "Imported · current";
  if (recipe.local.status === "update-available") return `Update from v${recipe.local.release_version ?? "?"}`;
  if (recipe.local.status === "local-ahead") return "Local version is newer";
  if (recipe.local.status === "different-revision") return "Different local revision";
  if (recipe.local.status === "conflict") return "Local identity conflict";
  return "Not imported";
}

function upgradeEffectLabel(value: "metadata-only" | "restart" | "reinstall" | "rebuild"): string {
  if (value === "metadata-only") return "No runtime action";
  if (value === "restart") return "Restart required";
  if (value === "reinstall") return "Reinstall required";
  return "Rebuild required";
}

function strongestUpgradeEffect(releases: PublicRecipePreview["changes_since_local"]): "metadata-only" | "restart" | "reinstall" | "rebuild" | undefined {
  const priority = {"metadata-only": 0, restart: 1, reinstall: 2, rebuild: 3} as const;
  return releases.reduce<"metadata-only" | "restart" | "reinstall" | "rebuild" | undefined>((strongest, release) => !strongest || priority[release.upgrade_effect] > priority[strongest] ? release.upgrade_effect : strongest, undefined);
}

function qualificationLabel(recipe: PublicRecipe): string {
  return recipe.qualification === "candidate" ? "Candidate" : "Accepted";
}

function readinessLabel(value: PublicRecipeExecutionReadiness): string {
  if (value === "executable") return "Executable contract";
  if (value === "not-executable") return "Not executable";
  if (value === "integration-required") return "Integration required";
  return "Readiness not declared";
}

function fabricLabel(recipe: PublicRecipe): string {
  const connectivity = recipe.fabric.connectivity.replaceAll("_", " ");
  if (recipe.fabric.minimum_bandwidth_mbps === 0) return connectivity;
  const bandwidth = recipe.fabric.minimum_bandwidth_mbps >= 1000
    ? `${recipe.fabric.minimum_bandwidth_mbps / 1000} Gbps minimum`
    : `${recipe.fabric.minimum_bandwidth_mbps} Mbps minimum`;
  return `${connectivity} · ${bandwidth}`;
}

function topologyModeLabel(value: string): string {
  const normalized = value.replaceAll("-", "_");
  if (normalized === "single") return "Single Spark";
  if (normalized === "tensor_parallel") return "Tensor parallel";
  if (normalized === "pipeline_parallel") return "Pipeline parallel";
  if (normalized === "data_parallel") return "Data parallel";
  const label = runtimeLabel(value);
  return `${label.charAt(0).toUpperCase()}${label.slice(1)}`;
}

function RecipeTopology({recipe}: {recipe: PublicRecipe}) {
  const roleCount = recipe.topology_roles.reduce((total, role) => total + role.count, 0);
  const endpointOwners = recipe.topology_roles.filter(role => role.endpoint_owner);
  const fabricMatchesCount = recipe.node_count === 1 ? recipe.fabric.connectivity === "none" : recipe.fabric.connectivity !== "none";
  const detailsValid = roleCount === recipe.node_count && endpointOwners.length === 1 && endpointOwners[0]?.count === 1 && fabricMatchesCount;
  const hasEndpointOwner = detailsValid;
  const hasFabric = recipe.fabric.connectivity !== "none";
  const roleSummary = recipe.topology_roles.map(role => `${role.count} ${runtimeLabel(role.name)} ${role.count === 1 ? "Spark" : "Sparks"}${role.endpoint_owner ? " owning an endpoint" : ""}`).join(", ");
  const topologyDescription = detailsValid
    ? `Recipe declares ${sparkLabel(recipe.node_count)} using ${topologyModeLabel(recipe.topology_mode)} topology: ${roleSummary}.${hasFabric ? ` Fabric ${fabricLabel(recipe)}.` : " No inter-Spark fabric is declared."}`
    : `Recipe declares ${sparkLabel(recipe.node_count)} using ${topologyModeLabel(recipe.topology_mode)} topology. Role and fabric details are unavailable.`;
  return <section className="public-import-topology" aria-labelledby="public-import-topology-title">
    <header>
      <div><span className="public-import-kicker">How it runs</span><h3 id="public-import-topology-title">{sparkLabel(recipe.node_count)} · {topologyModeLabel(recipe.topology_mode)}</h3></div>
      {detailsValid && hasFabric && <span className="public-import-fabric-label">Fabric: {fabricLabel(recipe)}</span>}
    </header>
    <div className={`public-import-topology-map${hasEndpointOwner ? " has-endpoint" : ""}`} role="img" aria-label={topologyDescription}>
      {hasEndpointOwner && <><div className="public-import-endpoint" aria-hidden="true"><span>API</span><strong>Declared endpoint</strong></div><span className="public-import-topology-connector" aria-hidden="true" /></>}
      <div className="public-import-spark-nodes" aria-hidden="true">
        {detailsValid ? recipe.topology_roles.map(role => <div className={role.endpoint_owner ? "is-endpoint-owner" : ""} key={role.name}>
          <span>{role.count}× Spark</span>
          <strong>{runtimeLabel(role.name)}</strong>
          <small>{role.endpoint_owner ? "Endpoint owner" : "Declared role group"}</small>
        </div>) : <div><span>{recipe.node_count}× Spark</span><strong>Topology details unavailable</strong><small>Review the immutable recipe</small></div>}
      </div>
    </div>
    <p>{detailsValid ? "Roles, endpoint ownership, and fabric shown here come from the immutable recipe contract." : "Role and fabric details are unavailable; review the immutable recipe."}</p>
  </section>;
}

function RecipeRequirements({recipe}: {recipe: PublicRecipe}) {
  const values = [
    {label: "Download total", value: recipe.expected_download_bytes},
    {label: "Storage / Spark", value: recipe.maximum_installed_bytes_per_node},
    {label: "Memory / Spark", value: recipe.maximum_runtime_memory_bytes_per_node},
  ];
  const maximum = Math.max(...values.map(item => item.value));
  return <section className="public-import-requirements" aria-labelledby="public-import-requirements-title">
    <header><span className="public-import-kicker">Resource requirements</span><h3 id="public-import-requirements-title">Largest per-Spark envelope</h3></header>
    <div>{values.map(item => <div className="public-import-requirement" key={item.label}>
      <div><span>{item.label}</span><strong>{formatBytes(item.value)}</strong></div>
      <meter min={0} max={maximum} value={item.value} aria-label={`${item.label}: ${formatBytes(item.value)}`}>{formatBytes(item.value)}</meter>
    </div>)}</div>
    <p>Bars compare byte requirements within this recipe; they do not claim a fit against your current fleet.</p>
  </section>;
}

function RecipeComparisonTray({recipes, onRemove, onClear}: {recipes: PublicRecipe[]; onRemove(uri: string): void; onClear(): void}) {
  const [expanded, setExpanded] = useState(false);
  useEffect(() => { if (recipes.length < 2) setExpanded(false); }, [recipes.length]);
  if (recipes.length === 0) return null;
  return <section className="public-import-compare" aria-labelledby="public-import-compare-title">
    <header>
      <div><span className="public-import-kicker">Compare tray</span><h3 id="public-import-compare-title">{recipes.length} of 3 recipes</h3></div>
      <button type="button" className="public-import-text-button" onClick={onClear}>Clear</button>
    </header>
    <div className="public-import-compare-chips" aria-label="Recipes selected for comparison">{recipes.map(recipe => <span key={recipe.uri}>{recipe.model_title}<button type="button" onClick={() => onRemove(recipe.uri)} aria-label={`Remove ${recipe.title} from comparison`}>×</button></span>)}</div>
    <button type="button" className="button secondary" disabled={recipes.length < 2} aria-expanded={expanded} aria-controls="public-import-comparison-table" onClick={() => setExpanded(value => !value)}>{expanded ? "Hide comparison" : `Compare ${recipes.length} recipes`}</button>
    {recipes.length < 2 && <p>Select one more recipe to compare.</p>}
    {expanded && <div className="public-import-comparison-table-wrap" id="public-import-comparison-table" tabIndex={0}>
      <table>
        <caption>Selected public recipe comparison</caption>
        <thead><tr><th scope="col">Attribute</th>{recipes.map(recipe => <th scope="col" key={recipe.uri}>{recipe.model_title}</th>)}</tr></thead>
        <tbody>
          <tr><th scope="row">Recipe</th>{recipes.map(recipe => <td key={recipe.uri}>{recipe.title}</td>)}</tr>
          <tr><th scope="row">Execution readiness</th>{recipes.map(recipe => <td key={recipe.uri}>{readinessLabel(recipe.execution_readiness)}</td>)}</tr>
          <tr><th scope="row">Qualification</th>{recipes.map(recipe => <td key={recipe.uri}>{qualificationLabel(recipe)}</td>)}</tr>
          <tr><th scope="row">Sparks</th>{recipes.map(recipe => <td key={recipe.uri}>{sparkLabel(recipe.node_count)}</td>)}</tr>
          <tr><th scope="row">Topology</th>{recipes.map(recipe => <td key={recipe.uri}>{topologyModeLabel(recipe.topology_mode)}</td>)}</tr>
          <tr><th scope="row">Memory / Spark</th>{recipes.map(recipe => <td key={recipe.uri}>{formatBytes(recipe.maximum_runtime_memory_bytes_per_node)}</td>)}</tr>
          <tr><th scope="row">Download</th>{recipes.map(recipe => <td key={recipe.uri}>{formatBytes(recipe.expected_download_bytes)}</td>)}</tr>
      <tr><th scope="row">Quantization / format</th>{recipes.map(recipe => <td key={recipe.uri}>{recipeQuantizations(recipe).join(" · ") || "Not specified"}</td>)}</tr><tr><th scope="row">Alignment</th>{recipes.map(recipe => <td key={recipe.uri}>{alignmentLabel(recipe.alignment)}</td>)}</tr>
          <tr><th scope="row">Recipe creator</th>{recipes.map(recipe => <td key={recipe.uri}>{recipe.source_owner ?? "Not specified"}</td>)}</tr>
        </tbody>
      </table>
    </div>}
  </section>;
}

function sortRecipes(recipes: PublicRecipe[], sort: RecipeSort): PublicRecipe[] {
  if (sort === "catalog") return recipes;
  return [...recipes].sort((left, right) => {
    if (sort === "model") return left.model_title.localeCompare(right.model_title) || left.title.localeCompare(right.title);
    if (sort === "sparks") return left.node_count - right.node_count || left.title.localeCompare(right.title);
    return left.expected_download_bytes - right.expected_download_bytes || left.title.localeCompare(right.title);
  });
}

type ActiveFilter = {key: string; label: string; remove(filters: PublicRecipeFilters): PublicRecipeFilters};

function activeFilters(filters: PublicRecipeFilters): ActiveFilter[] {
  const items: ActiveFilter[] = [];
  const add = (key: string, label: string, patch: Partial<PublicRecipeFilters>) => items.push({key, label, remove: current => ({...current, ...patch})});
  if (filters.query) add("query", `Search: ${filters.query}`, {query: ""});
  if (filters.modelType) add("modelType", `Model type: ${modelTypeLabel(filters.modelType)}`, {modelType: ""});
  if (filters.model) add("model", `Model: ${filters.model.split("/").at(-1)}`, {model: ""});
  if (filters.modelVersion) add("modelVersion", `Model version: ${filters.modelVersion.split("/").at(-1)}`, {modelVersion: ""});
  if (filters.alignment) add("alignment", `Alignment: ${PUBLIC_RECIPE_ALIGNMENTS.find(option => option.value === filters.alignment)?.label ?? filters.alignment}`, {alignment: ""});
  if (filters.sourceOwner) add("sourceOwner", `Creator: ${filters.sourceOwner}`, {sourceOwner: ""});
  if (filters.repository) add("repository", `Repository: ${repositoryLabel(filters.repository)}`, {repository: ""});
  if (filters.sparks) add("sparks", `Sparks: ${filters.sparks}`, {sparks: ""});
  if (filters.runtime) add("runtime", `Runtime: ${runtimeLabel(filters.runtime)}`, {runtime: ""});
  if (filters.quantization) add("quantization", `Quantization: ${filters.quantization}`, {quantization: ""});
  if (filters.updated) add("updated", `Updated: last ${filters.updated} days`, {updated: ""});
  if (filters.topology) add("topology", `Topology: ${runtimeLabel(filters.topology)}`, {topology: ""});
  if (filters.qualification) add("qualification", `Qualification: ${filters.qualification === "candidate" ? "Candidate" : "Accepted"}`, {qualification: ""});
  if (filters.readiness) add("readiness", `Execution readiness: ${readinessLabel(filters.readiness)}`, {readiness: ""});
  if (filters.local) add("local", `Local: ${filters.local.replaceAll("-", " ")}`, {local: ""});
  for (const capability of filters.capabilities) items.push({key: `capability:${capability}`, label: `Capability: ${PUBLIC_RECIPE_CAPABILITIES.find(option => option.value === capability)?.label ?? capability}`, remove: current => ({...current, capabilities: current.capabilities.filter(value => value !== capability)})});
  return items;
}

function Preview({preview, saving, importError, importOutcomeUnknown, status, onBack, onConfirm, onImport, onOpenLocal, onRecheckImport}: {
  preview: PublicRecipePreview;
  saving: boolean;
  importError: string;
  importOutcomeUnknown: boolean;
  status: ImportStep;
  onBack(): void;
  onConfirm(): void;
  onImport(): void;
  onOpenLocal(): void;
  onRecheckImport(): void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { queueMicrotask(() => heading.current?.focus()); }, [preview.uri, status]);
  const requiredEffect = strongestUpgradeEffect(preview.changes_since_local);
  const executable = preview.execution_readiness === "executable";
  const localPath = preview.local.recipe_id ? localRecipePath(preview.local.recipe_id) : "";
  const actions = <footer className="public-import-preview-actions">
    <button type="button" className="button secondary" disabled={saving} onClick={onBack}>{status === "confirm" ? "Back to review" : "Choose another recipe"}</button>
    {preview.local.status === "current" && localPath ? <a className="button" href={localPath} onClick={event => { event.preventDefault(); onOpenLocal(); }}>Open build &amp; install controls</a> : executable && (status === "confirm" && !importError && !importOutcomeUnknown ? <button type="button" className="button" disabled={saving || ["current", "conflict", "local-ahead"].includes(preview.local.status)} onClick={onImport}>{saving ? "Importing…" : preview.local.status === "update-available" || preview.local.status === "different-revision" ? `Import${preview.release_version ? ` v${preview.release_version}` : " catalog revision"}` : preview.qualification === "candidate" ? "Import candidate" : "Import recipe"}</button> : status !== "confirm" && <button type="button" className="button" disabled={saving || ["current", "conflict", "local-ahead"].includes(preview.local.status)} onClick={onConfirm}>{preview.local.status === "current" ? "Already current" : "Continue to confirm"}</button>)}
  </footer>;
  return <section className={`public-import-preview${status === "confirm" ? " is-confirming" : ""}`} aria-labelledby="public-import-preview-title" aria-busy={saving}>
    <button type="button" className="button secondary public-import-mobile-back" disabled={saving} onClick={onBack}>Back to catalog</button>
    <header className="public-import-preview-header">
      <div><span className="public-import-kicker">{status === "confirm" ? "Confirm import" : "Review recipe"}</span><h2 id="public-import-preview-title" ref={heading} tabIndex={-1}>{preview.title}</h2><p>{preview.model_title}{recipeQuantizations(preview).length ? ` · ${recipeQuantizations(preview).join(" · ")}` : ""}</p></div>
      <div className="public-import-preview-signals"><span className={`public-import-readiness readiness-${preview.execution_readiness}`}>{readinessLabel(preview.execution_readiness)}</span><span className={`public-import-qualification qualification-${preview.qualification}`}>{qualificationLabel(preview)}</span></div>
    </header>
    <div className={`public-import-trust qualification-${preview.qualification}`}><strong>{qualificationLabel(preview)} qualification</strong><p>{preview.qualification_detail}</p></div>
    <div className={`public-import-trust readiness-${preview.execution_readiness}`}><strong>{readinessLabel(preview.execution_readiness)}</strong><p>{preview.execution_readiness_detail}</p></div>
    <div className="public-import-version-summary" aria-label="Version summary">
      <div><span>Local recipe</span><strong>{preview.local.release_version ? `v${preview.local.release_version}` : preview.local.status === "not-imported" ? "Not imported" : "Unknown revision"}</strong></div>
      <span aria-hidden="true">→</span>
      <div><span>Catalog release</span><strong>{preview.release_version ? `v${preview.release_version}` : "Immutable revision"}</strong></div>
      <div><span>Runtime impact</span><strong>{requiredEffect ? upgradeEffectLabel(requiredEffect) : preview.local.status === "not-imported" ? "Install after import" : "No runtime change listed"}</strong></div>
      <div><span>Changes</span><strong>{preview.changes_since_local.reduce((total, release) => total + release.changes.length, 0)}</strong></div>
    </div>
    {preview.local.status === "current" && <div className="public-import-local-handoff" role="region" aria-label="Local recipe handoff"><div><strong>Already in your local Library</strong><p>Build, map, install, and run this immutable revision from its local recipe page.</p></div></div>}
    {status !== "confirm" && actions}
    {!executable && <div className="public-import-state is-error" role="alert"><div><strong>Import blocked: executable contract required</strong><p>This immutable recipe remains available for inspection, but Vonk Forge only imports recipes that declare a complete executable contract.</p></div></div>}
    {status === "confirm" && executable && preview.local.status !== "current" && <div className={`public-import-confirmation-copy readiness-${preview.execution_readiness}`}><strong>{preview.qualification === "candidate" ? "Import this candidate?" : "Import this recipe?"}</strong><p>This saves a new immutable local recipe revision. It does not rebuild, reinstall, or restart running services.</p></div>}
    {status === "confirm" && executable && importError && <div className="public-import-state is-error" role="alert"><div><strong>Import failed</strong><p>{importError}</p></div><button type="button" className="button secondary" disabled={saving} onClick={onImport}>Retry import</button></div>}
    {status === "confirm" && executable && importOutcomeUnknown && <div className="public-import-state is-slow" role="status"><div><strong>Stopped waiting — import outcome unknown</strong><p>The server may still have completed the import. Check or recheck the local Library status before starting another import.</p></div><div><a className="button secondary" href="/library">Check local Library</a><button type="button" className="button secondary" onClick={onRecheckImport}>Recheck status</button></div></div>}
    {status === "confirm" && actions}
    {status !== "confirm" && <>
      <p className="public-import-description">{preview.description || "No description provided."}</p>
      <div className="public-import-tags" aria-label="Recipe capabilities">{preview.capabilities.map(capability => <span key={capability}>{PUBLIC_RECIPE_CAPABILITIES.find(option => option.value === capability)?.label ?? capability}</span>)}</div>
      <dl className="public-import-primary-facts"><div><dt>Sparks</dt><dd>{sparkLabel(preview.node_count)}</dd></div><div><dt>Download</dt><dd>{formatBytes(preview.expected_download_bytes)}</dd></div><div><dt>Memory / Spark</dt><dd>{formatBytes(preview.maximum_runtime_memory_bytes_per_node)}</dd></div><div><dt>Recipe creator</dt><dd>{preview.source_owner ?? "Not specified"}</dd></div></dl>
      <RecipeTopology recipe={preview}/>
      <RecipeRequirements recipe={preview}/>
      {preview.local.status === "update-available" && <p className="public-import-note">Existing installations and running services remain pinned to their current revision until you rebuild or reinstall them.</p>}
      {["different-revision", "conflict", "local-ahead"].includes(preview.local.status) && <p className="public-import-warning" role="alert">{preview.local.status === "different-revision" ? "The local digest is not in catalog history, so an exact change list cannot be proven." : preview.local.status === "conflict" ? "A different local recipe owns this slug. Resolve the conflict before importing." : "The local release is newer than this catalog snapshot. Import is disabled to avoid a downgrade."}</p>}
      {preview.changes_since_local.length > 0 && <section className="public-import-changelog" aria-labelledby="public-import-changelog-title"><header><span className="public-import-kicker">Release notes</span><h3 id="public-import-changelog-title">{preview.local.status === "update-available" ? `Changes since local v${preview.local.release_version}` : "Catalog changelog"}</h3></header>{preview.changes_since_local.map((release, releaseIndex) => <details open={releaseIndex === 0} key={`${release.version}:${release.content_sha256}`}><summary><strong>v{release.version}</strong><span>{release.released_at} · {upgradeEffectLabel(release.upgrade_effect)}</span></summary><ul>{release.changes.map((change, index) => <li key={`${change.kind}:${index}`}><span>{change.kind}</span><strong>{change.summary}</strong>{change.details && <p>{change.details}</p>}{change.references.length > 0 && <div>{change.references.map((reference, referenceIndex) => <a href={reference} target="_blank" rel="noreferrer" key={reference}>Source {referenceIndex + 1}<span className="sr-only"> (opens in a new tab)</span></a>)}</div>}</li>)}</ul></details>)}</section>}
      <details className="public-import-technical"><summary>Technical details</summary><dl><div><dt>Catalog identity</dt><dd>{preview.publisher}/{preview.slug}</dd></div><div><dt>Qualification evidence</dt><dd>{preview.qualification_basis.replaceAll("-", " ")}</dd></div><div><dt>Readiness evidence</dt><dd>{preview.execution_readiness_basis.replaceAll("-", " ")}</dd></div><div><dt>Runtime</dt><dd>{runtimeLabel(preview.runtime_distribution)}</dd></div><div><dt>Execution</dt><dd>{runtimeLabel(preview.execution_harness)}</dd></div><div><dt>Topology</dt><dd>{preview.topology_mode}</dd></div><div><dt>Installed / Spark</dt><dd>{formatBytes(preview.maximum_installed_bytes_per_node)}</dd></div><div><dt>Artifacts</dt><dd>{preview.artifact_count}</dd></div>{preview.source_repository && <div><dt>Original repository</dt><dd><a href={preview.source_repository} target="_blank" rel="noreferrer">View source<span className="sr-only"> (opens in a new tab)</span></a></dd></div>}<div><dt>Immutable digest</dt><dd><code>sha256:{preview.content_sha256}</code></dd></div></dl></details>
    </>}
    {status === "confirm" && <span className="sr-only">Confirm the selected immutable recipe before importing.</span>}
  </section>;
}

export function PublicRecipeImportPage({api, url, onNavigate, onBusyChange}: {api: CatalogApi; url: string; onNavigate(url: string, replace?: boolean): void; onBusyChange?(busy: boolean): void}) {
  const parsed = parsePublicRecipeImportUrl(url);
  const {filters, more, recipe: selectedUri, step} = parsed;
  const [recipes, setRecipes] = useState<PublicRecipe[]>([]);
  const [commit, setCommit] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [catalogSlow, setCatalogSlow] = useState(false);
  const [catalogCancelled, setCatalogCancelled] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [preview, setPreview] = useState<PublicRecipePreview>();
  const [previewError, setPreviewError] = useState("");
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewSlow, setPreviewSlow] = useState(false);
  const [previewCancelled, setPreviewCancelled] = useState(false);
  const [saving, setSaving] = useState(false);
  const [importSlow, setImportSlow] = useState(false);
  const [importOutcomeUnknown, setImportOutcomeUnknown] = useState(false);
  const [importError, setImportError] = useState("");
  const [completion, setCompletion] = useState<ImportCompletion>();
  const [manualUri, setManualUri] = useState("");
  const [manualUriError, setManualUriError] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [catalogView, setCatalogView] = useState<CatalogView>(storedCatalogView);
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false);
  const [compareUris, setCompareUris] = useState<string[]>([]);
  const heading = useRef<HTMLHeadingElement>(null);
  const catalogRequest = useRef<AbortController | undefined>(undefined);
  const previewRequest = useRef<AbortController | undefined>(undefined);
  const importRequest = useRef<AbortController | undefined>(undefined);
  const requestSequence = useRef(0);
  const importSequence = useRef(0);
  const importRoute = useRef(`${selectedUri}\u0000${step}`);
  importRoute.current = `${selectedUri}\u0000${step}`;

  const navigateFilters = (next: PublicRecipeFilters, nextMore = more) => onNavigate(publicRecipeImportUrl(next, {more: nextMore}), true);
  const updateFilter = <K extends keyof PublicRecipeFilters>(key: K, value: PublicRecipeFilters[K]) => navigateFilters({...filters, [key]: value});

  async function loadCatalog(refresh = false) {
    catalogRequest.current?.abort();
    const controller = new AbortController();
    catalogRequest.current = controller;
    setCatalogError("");
    setCatalogCancelled(false);
    setCatalogSlow(false);
    refresh ? setRefreshing(true) : setLoading(true);
    try {
      const timeoutMessage = refresh
        ? "The catalog refresh did not respond within 30 seconds. The previous catalog snapshot is still shown; try again."
        : "The public catalog did not respond within 30 seconds. Try again.";
      const result = await runBoundedRequest(controller, () => setCatalogSlow(true), timeoutMessage, () => api.listPublicRecipes(controller.signal));
      if (controller.signal.aborted) return;
      setRecipes(result.recipes);
      setCommit(result.commit);
    } catch (value) {
      if (value instanceof RequestTimeoutError || !controller.signal.aborted) setCatalogError(value instanceof Error ? value.message : "Unable to load the current recipe catalog");
    } finally {
      if (catalogRequest.current === controller) {
        setLoading(false);
        setRefreshing(false);
        setCatalogSlow(false);
        catalogRequest.current = undefined;
      }
    }
  }

  function cancelCatalogRequest() {
    const controller = catalogRequest.current;
    if (!controller) return;
    catalogRequest.current = undefined;
    controller.abort();
    setLoading(false);
    setRefreshing(false);
    setCatalogSlow(false);
    setCatalogError("");
    setCatalogCancelled(true);
  }

  useEffect(() => {
    queueMicrotask(() => heading.current?.focus());
    void loadCatalog();
    return () => { catalogRequest.current?.abort(); previewRequest.current?.abort(); importRequest.current?.abort(); };
  }, [api]);

  useEffect(() => { onBusyChange?.(saving); }, [onBusyChange, saving]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);

  useEffect(() => {
    if (!selectedUri) { previewRequest.current?.abort(); setPreview(undefined); setPreviewError(""); setImportError(""); setPreviewLoading(false); setPreviewSlow(false); setPreviewCancelled(false); return; }
    const controller = new AbortController();
    previewRequest.current?.abort();
    previewRequest.current = controller;
    const sequence = ++requestSequence.current;
    setPreview(undefined);
    setPreviewError("");
    setImportError("");
    setCompletion(undefined);
    setPreviewLoading(true);
    setPreviewSlow(false);
    setPreviewCancelled(false);
    void runBoundedRequest(controller, () => setPreviewSlow(true), "The recipe preview did not respond within 30 seconds. Try again.", () => api.previewPublicRecipe(selectedUri, controller.signal)).then(value => {
      if (!controller.signal.aborted && sequence === requestSequence.current && value.uri === selectedUri) setPreview(value);
    }).catch(value => {
      if (sequence === requestSequence.current && (value instanceof RequestTimeoutError || !controller.signal.aborted)) setPreviewError(value instanceof Error ? value.message : "Unable to preview import");
    }).finally(() => {
      if (previewRequest.current === controller && sequence === requestSequence.current) {
        setPreviewLoading(false);
        setPreviewSlow(false);
        previewRequest.current = undefined;
      }
    });
    return () => controller.abort();
  }, [api, selectedUri, previewAttempt]);

  useEffect(() => {
    setImportError("");
    setImportOutcomeUnknown(false);
    setImportSlow(false);
    if (!importRequest.current) return;
    importSequence.current += 1;
    importRequest.current.abort();
    importRequest.current = undefined;
    setSaving(false);
  }, [selectedUri, step]);

  const models = useMemo(() => {
    const identities = Array.from(new Map(recipes.filter(recipe => modelTypeMatches(recipe, filters.modelType)).map(recipe => [`${recipe.model_publisher}/${recipe.model_slug}`, recipe.model_title])).entries());
    const titleCounts = identities.reduce((counts, [, title]) => counts.set(title, (counts.get(title) ?? 0) + 1), new Map<string, number>());
    return identities
      .map(([value, title]) => [value, titleCounts.get(title) === 1 ? title : `${title} · ${value}`] as const)
      .sort((a, b) => a[1].localeCompare(b[1]));
  }, [filters.modelType, recipes]);
  const modelVersions = useMemo(() => {
    const identities = Array.from(new Map(recipes
      .filter(recipe => modelTypeMatches(recipe, filters.modelType) && (!filters.model || `${recipe.model_publisher}/${recipe.model_slug}` === filters.model))
      .map(recipe => [modelVersionKey(recipe), modelVersionTitle(recipe)])).entries());
    const titleCounts = identities.reduce((counts, [, title]) => counts.set(title, (counts.get(title) ?? 0) + 1), new Map<string, number>());
    return identities.map(([value, title]) => [value, titleCounts.get(title) === 1 ? title : `${title} · ${value}`] as const).sort((a, b) => a[1].localeCompare(b[1]));
  }, [filters.model, filters.modelType, recipes]);
  const sourceOwners = useMemo(() => Array.from(new Set(recipes.flatMap(recipe => recipe.source_owner ? [recipe.source_owner] : []))).sort(), [recipes]);
  const repositories = useMemo(() => Array.from(new Set(recipes.flatMap(recipe => recipe.source_repository ? [recipe.source_repository] : []))).sort(), [recipes]);
  const runtimes = useMemo(() => Array.from(new Set(recipes.map(recipe => recipe.runtime_distribution))).sort(), [recipes]);
  const quantizations = useMemo(() => Array.from(new Set(recipes.flatMap(recipeQuantizations))).sort(), [recipes]);
  const alignments = useMemo(() => PUBLIC_RECIPE_ALIGNMENTS.filter(option => recipes.some(recipe => recipe.alignment === option.value)), [recipes]);
  const topologies = useMemo(() => Array.from(new Set(recipes.map(recipe => recipe.topology_mode))).sort(), [recipes]);
  const filtered = useMemo(() => sortRecipes(recipes.filter(recipe => publicRecipeMatches(recipe, filters)), filters.sort), [filters, recipes]);
  const count = (facet: Facet, predicate: (recipe: PublicRecipe) => boolean) => recipes.filter(recipe => publicRecipeMatches(recipe, filters, facet) && predicate(recipe)).length;
  const modelTypeCount = (modelType: ModelType) => recipes.filter(recipe => publicRecipeMatches(recipe, {...filters, model: ""}, "modelType") && modelTypeMatches(recipe, modelType)).length;
  const capabilityCount = (capability: PublicRecipeCapability) => recipes.filter(recipe => publicRecipeMatches(recipe, filters, "capability") && filters.capabilities.filter(value => value !== capability).every(value => recipe.capabilities.includes(value)) && recipe.capabilities.includes(capability)).length;
  const applied = activeFilters(filters);
  const comparedRecipes = compareUris.flatMap(uri => recipes.find(recipe => recipe.uri === uri) ?? []);

  useEffect(() => {
    const timeout = window.setTimeout(() => setAnnouncement(`Showing ${filtered.length} of ${recipes.length} recipes.`), 250);
    return () => window.clearTimeout(timeout);
  }, [filtered.length, recipes.length]);

  function selectRecipe(uri: string) {
    onNavigate(publicRecipeImportUrl(filters, {more, recipe: uri, step: "review"}));
  }

  function updateModelType(modelType: ModelType) {
    const selectedModelMatches = !filters.model || recipes.some(recipe => `${recipe.model_publisher}/${recipe.model_slug}` === filters.model && modelTypeMatches(recipe, modelType));
    navigateFilters({...filters, modelType, model: selectedModelMatches ? filters.model : "", modelVersion: selectedModelMatches ? filters.modelVersion : ""});
  }

  function updateModel(model: string) {
    const selectedVersionMatches = !filters.modelVersion || recipes.some(recipe => `${recipe.model_publisher}/${recipe.model_slug}` === model && modelVersionKey(recipe) === filters.modelVersion);
    navigateFilters({...filters, model, modelVersion: selectedVersionMatches ? filters.modelVersion : ""});
  }

  function reviewManualUri() {
    const uri = manualUri.trim();
    setManualUri(uri);
    if (!PUBLIC_RECIPE_URI_PATTERN.test(uri)) {
      setManualUriError("Enter an immutable URI in the form vonk://catalog/publisher/slug@sha256:digest.");
      return;
    }
    setManualUriError("");
    selectRecipe(uri);
  }

  function chooseCatalogView(view: CatalogView) {
    setCatalogView(view);
    saveCatalogView(view);
    setAnnouncement(`${view === "cards" ? "Detailed" : "Compact"} view selected.`);
  }

  function toggleComparison(uri: string) {
    setCompareUris(current => {
      if (current.includes(uri)) return current.filter(value => value !== uri);
      if (current.length >= 3) return current;
      return [...current, uri];
    });
  }

  function returnToCatalog() {
    onNavigate(publicRecipeImportUrl(filters, {more}));
    queueMicrotask(() => Array.from(document.querySelectorAll<HTMLButtonElement>("[data-recipe-uri]"))
      .find(element => element.dataset.recipeUri === selectedUri)?.focus());
  }

  function cancelPreviewRequest() {
    const controller = previewRequest.current;
    if (!controller) return;
    previewRequest.current = undefined;
    requestSequence.current += 1;
    controller.abort();
    setPreviewLoading(false);
    setPreviewSlow(false);
    setPreviewError("");
    setPreviewCancelled(true);
  }

  function retryPreview() {
    setPreviewCancelled(false);
    setPreviewAttempt(value => value + 1);
  }

  function stopWaitingForImport() {
    const controller = importRequest.current;
    if (!controller) return;
    importRequest.current = undefined;
    importSequence.current += 1;
    controller.abort();
    setSaving(false);
    setImportSlow(false);
    setImportError("");
    setImportOutcomeUnknown(true);
  }

  function recheckImportOutcome() {
    setImportOutcomeUnknown(false);
    setImportError("");
    setPreviewAttempt(value => value + 1);
    onNavigate(publicRecipeImportUrl(filters, {more, recipe: selectedUri, step: "review"}));
  }

  async function importRecipe() {
    if (!preview || preview.execution_readiness !== "executable") return;
    importRequest.current?.abort();
    const controller = new AbortController();
    importRequest.current = controller;
    const sequence = ++importSequence.current;
    const route = importRoute.current;
    setSaving(true);
    setImportError("");
    setImportOutcomeUnknown(false);
    setImportSlow(false);
    try {
      const imported = await runBoundedRequest(controller, () => setImportSlow(true), "The import did not finish within 30 seconds. It may not have been saved; check the local Library before retrying.", () => api.importPublicRecipe(preview.uri, preview.content_sha256, controller.signal));
      if (controller.signal.aborted || sequence !== importSequence.current || route !== importRoute.current) return;
      const updated = preview.local.status === "update-available" || preview.local.status === "different-revision";
      setCompletion({recipeId: imported.recipe_id, title: updated && preview.release_version ? `Updated ${preview.title} to v${preview.release_version}` : `Imported ${preview.title}`});
      setPreview(current => current ? {...current, local: {...current.local, status: "current", content_sha256: current.content_sha256, release_version: current.release_version}} : current);
      await loadCatalog(true);
    } catch (value) {
      if (sequence === importSequence.current && route === importRoute.current && value instanceof RequestTimeoutError) {
        setImportError("");
        setImportOutcomeUnknown(true);
      } else if (sequence === importSequence.current && route === importRoute.current && !controller.signal.aborted) {
        setImportError(value instanceof Error ? value.message : "Unable to import recipe");
      }
    } finally {
      if (importRequest.current === controller && sequence === importSequence.current && route === importRoute.current) {
        setSaving(false);
        setImportSlow(false);
        importRequest.current = undefined;
      }
    }
  }

  if (completion) { const path = localRecipePath(completion.recipeId); return <section className="public-import-complete" aria-labelledby="public-import-complete-title" role="status" tabIndex={-1} ref={element => { if (element) queueMicrotask(() => element.focus()); }}><span className="public-import-kicker">Import complete</span><h1 id="public-import-complete-title">{completion.title}</h1><p>The immutable revision is saved in your local Library. To install it, continue to its local recipe page and complete Build → Map → Install. Existing installations and running services were not changed.</p><div><a className="button" href={path} onClick={event => { event.preventDefault(); onNavigate(path); }}>Open build &amp; install controls</a><button type="button" className="button secondary" onClick={() => { setCompletion(undefined); returnToCatalog(); }}>Import another recipe</button></div></section>; }

  return <div className={`public-import-page step-${step}`}>
    <header className="public-import-page-header">
      <div><a href="/library" className="public-import-back" aria-disabled={saving || undefined} tabIndex={saving ? -1 : undefined} onClick={saving ? event => event.preventDefault() : undefined}>← Library</a><h1 ref={heading} tabIndex={-1}>Import a public recipe</h1><p>Choose an immutable recipe, inspect its independent qualification and execution-readiness evidence, then confirm the local import.</p></div>
      <ol aria-label="Import progress"><li aria-current={step === "catalog" ? "step" : undefined}>1 <span>Catalog</span></li><li aria-current={step === "review" ? "step" : undefined}>2 <span>Review</span></li><li aria-current={step === "confirm" ? "step" : undefined}>3 <span>{saving ? "Importing" : "Confirm"}</span></li></ol>
    </header>
    <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
    {saving && preview && <div className={`public-import-state public-import-active-request${importSlow ? " is-slow" : ""}`} role="status" aria-live="polite"><span className="public-import-spinner" aria-hidden="true"/><div><strong>{importSlow ? "Import is taking longer than expected" : `Importing ${preview.title}`}</strong><p>{importSlow ? "The verified revision is still being saved. You can stop waiting, but the server may still complete it." : "Saving the verified immutable revision to your local Library…"}</p></div><button type="button" className="button secondary" onClick={stopWaitingForImport}>Stop waiting</button></div>}
    <fieldset className="public-import-workspace public-import-interaction-lock" disabled={saving} aria-busy={saving}>
      <legend className="sr-only">Recipe catalog and review</legend>
      <button type="button" className="button secondary public-import-mobile-filter-toggle" aria-expanded={mobileFiltersOpen} aria-controls="public-import-filter-rail" onClick={() => setMobileFiltersOpen(open => !open)}>
        <span>{mobileFiltersOpen ? "Hide filters" : "Show filters"}</span>
        {applied.length > 0 && <span className="public-import-mobile-filter-count">{applied.length} applied</span>}
      </button>
      <aside id="public-import-filter-rail" className={`public-import-filter-rail${mobileFiltersOpen ? " is-mobile-open" : ""}`} aria-label="Recipe filters">
        <div className="public-import-filter-heading"><div><span className="public-import-kicker">Narrow the catalog</span><h2>Filters</h2></div>{applied.length > 0 && <button type="button" className="public-import-text-button" onClick={() => navigateFilters(EMPTY_FILTERS)}>Clear all</button>}</div>
        <label className="public-import-search"><span>Find a recipe</span><input type="search" value={filters.query} onChange={event => updateFilter("query", event.target.value)} placeholder="Model, modality, runtime…" /></label>
        <label><span>Model type</span><select aria-label="Filter by model type" value={filters.modelType} onChange={event => updateModelType(event.target.value as ModelType)}><option value="">All types ({modelTypeCount("")})</option>{MODEL_TYPE_OPTIONS.map(option => { const available = modelTypeCount(option.value); return <option value={option.value} disabled={available === 0} key={option.value}>{option.label} ({available})</option>; })}</select></label>
        <label><span>Model</span><select aria-label="Filter by model" value={filters.model} onChange={event => updateModel(event.target.value)}><option value="">All models ({count("model", () => true)})</option>{models.map(([value, label]) => { const available = count("model", recipe => `${recipe.model_publisher}/${recipe.model_slug}` === value); return <option value={value} disabled={available === 0} key={value}>{label} ({available})</option>; })}</select></label>
        <label><span>Model version</span><select aria-label="Filter by model version" value={filters.modelVersion} onChange={event => updateFilter("modelVersion", event.target.value)}><option value="">All versions ({count("modelVersion", () => true)})</option>{modelVersions.map(([value, label]) => { const available = count("modelVersion", recipe => modelVersionKey(recipe) === value); return <option value={value} disabled={available === 0} key={value}>{label} ({available})</option>; })}</select></label>
        <label><span>Quantization / format</span><select aria-label="Filter by quantization" value={filters.quantization} onChange={event => updateFilter("quantization", event.target.value)}><option value="">Any format</option>{quantizations.map(value => <option key={value} value={value} disabled={count("quantization", recipe => recipeQuantizations(recipe).includes(value)) === 0}>{value} ({count("quantization", recipe => recipeQuantizations(recipe).includes(value))})</option>)}</select></label>
        <label><span>Alignment</span><select aria-label="Filter by alignment" value={filters.alignment} onChange={event => updateFilter("alignment", event.target.value as PublicRecipeFilters["alignment"])}><option value="">Any alignment ({count("alignment", () => true)})</option>{alignments.map(option => <option key={option.value} value={option.value} disabled={count("alignment", recipe => recipe.alignment === option.value) === 0}>{option.label} ({count("alignment", recipe => recipe.alignment === option.value)})</option>)}</select></label>
        <label><span>Required Sparks</span><select aria-label="Filter by required Sparks" value={filters.sparks} onChange={event => updateFilter("sparks", event.target.value as SparkFilter)}><option value="">Any count ({count("sparks", () => true)})</option>{(["1", "2", "3", "4+"] as SparkFilter[]).map(value => { const available = count("sparks", recipe => sparkMatches(recipe, value)); return <option value={value} disabled={available === 0} key={value}>{value}{value === "1" ? " Spark" : " Sparks"} ({available})</option>; })}</select></label>
        <label><span>Recipe creator</span><select aria-label="Filter by recipe creator" value={filters.sourceOwner} onChange={event => updateFilter("sourceOwner", event.target.value)}><option value="">All creators</option>{sourceOwners.map(value => <option key={value} value={value} disabled={count("sourceOwner", recipe => recipe.source_owner === value) === 0}>{value} ({count("sourceOwner", recipe => recipe.source_owner === value)})</option>)}</select></label>
        <label><span>Updated</span><select aria-label="Filter by updated date" value={filters.updated} onChange={event => updateFilter("updated", event.target.value as UpdatedFilter)}><option value="">Any time ({count("updated", () => true)})</option>{(["7", "30", "90", "365"] as UpdatedFilter[]).map(value => <option key={value} value={value} disabled={count("updated", recipe => updatedMatches(recipe, value)) === 0}>Last {value} days ({count("updated", recipe => updatedMatches(recipe, value))})</option>)}</select></label>
        <label><span>Local recipe status</span><select aria-label="Filter by local recipe status" value={filters.local} onChange={event => updateFilter("local", event.target.value as LocalFilter)}><option value="">All ({count("local", () => true)})</option>{(["not-imported", "update-available", "current", "needs-review"] as LocalFilter[]).map(value => { const available = count("local", recipe => localMatches(recipe, value)); return <option value={value} disabled={available === 0} key={value}>{value === "not-imported" ? "Not imported" : value === "update-available" ? "Update available" : value === "current" ? "Imported current" : "Needs review"} ({available})</option>; })}</select></label>
        <label><span>Execution readiness</span><select aria-label="Filter by execution readiness" value={filters.readiness} onChange={event => updateFilter("readiness", event.target.value as PublicRecipeFilters["readiness"])}><option value="">Any readiness ({count("readiness", () => true)})</option>{PUBLIC_RECIPE_READINESS.map(option => { const available = count("readiness", recipe => recipe.execution_readiness === option.value); return <option value={option.value} disabled={available === 0} key={option.value}>{option.label} ({available})</option>; })}</select></label>
        <fieldset className="public-import-capabilities"><legend>Capabilities <span>Must all match</span></legend>{PUBLIC_RECIPE_CAPABILITIES.map(option => { const selected = filters.capabilities.includes(option.value); const available = capabilityCount(option.value); return <label className={available === 0 && !selected ? "is-disabled" : ""} key={option.value}><input type="checkbox" checked={selected} disabled={available === 0 && !selected} onChange={() => updateFilter("capabilities", selected ? filters.capabilities.filter(value => value !== option.value) : [...filters.capabilities, option.value])}/><span>{option.label}</span><small>{available}</small></label>; })}</fieldset>
        <button type="button" className="button secondary public-import-more-toggle" aria-expanded={more} aria-controls="public-import-more-filters" onClick={() => onNavigate(publicRecipeImportUrl(filters, {more: !more}), true)}>{more ? "Hide more filters" : "More filters"}</button>
        <div id="public-import-more-filters" hidden={!more} className="public-import-more-filters">
          <label><span>Qualification</span><select aria-label="Filter by qualification" value={filters.qualification} onChange={event => updateFilter("qualification", event.target.value as PublicRecipeFilters["qualification"])}><option value="">Any status ({count("qualification", () => true)})</option><option value="cataloged" disabled={count("qualification", recipe => recipe.qualification === "cataloged") === 0}>Accepted ({count("qualification", recipe => recipe.qualification === "cataloged")})</option><option value="candidate" disabled={count("qualification", recipe => recipe.qualification === "candidate") === 0}>Candidate ({count("qualification", recipe => recipe.qualification === "candidate")})</option></select></label>
          <label><span>Original repository</span><select aria-label="Filter by original repository" value={filters.repository} onChange={event => updateFilter("repository", event.target.value)}><option value="">All repositories</option>{repositories.map(value => <option key={value} value={value} disabled={count("repository", recipe => recipe.source_repository === value) === 0}>{repositoryLabel(value)} ({count("repository", recipe => recipe.source_repository === value)})</option>)}</select></label>
          <label><span>Runtime</span><select aria-label="Filter by runtime" value={filters.runtime} onChange={event => updateFilter("runtime", event.target.value)}><option value="">All runtimes</option>{runtimes.map(value => <option key={value} value={value} disabled={count("runtime", recipe => recipe.runtime_distribution === value) === 0}>{runtimeLabel(value)} ({count("runtime", recipe => recipe.runtime_distribution === value)})</option>)}</select></label>
          <label><span>Topology</span><select aria-label="Filter by topology" value={filters.topology} onChange={event => updateFilter("topology", event.target.value)}><option value="">Any topology</option>{topologies.map(value => <option key={value} value={value} disabled={count("topology", recipe => recipe.topology_mode === value) === 0}>{runtimeLabel(value)} ({count("topology", recipe => recipe.topology_mode === value)})</option>)}</select></label>
        </div>
        <details className="public-import-manual"><summary>Advanced: import URI</summary><div><label><span>Public recipe URI</span><input value={manualUri} aria-invalid={manualUriError ? "true" : undefined} aria-describedby={manualUriError ? "public-recipe-uri-error" : undefined} onChange={event => { setManualUri(event.target.value); setManualUriError(""); }} placeholder="vonk://catalog/…@sha256:…" /></label>{manualUriError && <small id="public-recipe-uri-error" role="alert">{manualUriError}</small>}<button type="button" className="button secondary" disabled={!manualUri.trim()} onClick={reviewManualUri}>Review URI</button></div></details>
      </aside>
      <section className="public-import-results" aria-busy={loading || refreshing} aria-labelledby="public-recipe-results-heading">
        <header><div><span className="public-import-kicker">Public recipe library</span><h2 id="public-recipe-results-heading">Choose a recipe</h2></div><div><span>{refreshing ? "Refreshing…" : `${filtered.length} of ${recipes.length}`}</span><button type="button" className="public-import-icon-button" aria-label="Refresh public catalog" onClick={() => void loadCatalog(true)}>↻</button></div></header>
        <div className="public-import-results-tools">
          <div className="public-import-results-controls">
            <div className="public-import-view-switch" role="group" aria-label="Catalog view">
              <button type="button" aria-pressed={catalogView === "cards"} onClick={() => chooseCatalogView("cards")}><span aria-hidden="true">▦</span> Detailed</button>
              <button type="button" aria-pressed={catalogView === "compact"} onClick={() => chooseCatalogView("compact")}><span aria-hidden="true">☷</span> Compact</button>
            </div>
            <label className="public-import-sort"><span>Sort by</span><select aria-label="Sort recipes" value={filters.sort} onChange={event => updateFilter("sort", event.target.value as RecipeSort)}><option value="catalog">Catalog order</option><option value="model">Model A–Z</option><option value="sparks">Fewest Sparks</option><option value="download">Smallest download</option></select></label>
          </div>
          {commit && <details className="public-import-snapshot"><summary>Catalog snapshot</summary><div><span>Immutable commit</span><code>{commit}</code></div></details>}
        </div>
        {loading && recipes.length === 0 && <div className={`public-import-state${catalogSlow ? " is-slow" : ""}`} role="status"><span className="public-import-spinner" aria-hidden="true"/><div><strong>{catalogSlow ? "Catalog load is taking longer than expected" : "Loading the public catalog"}</strong><p>{catalogSlow ? "The catalog is still responding. You can cancel instead of waiting for the 30-second deadline." : "Resolving one immutable library snapshot…"}</p></div><button type="button" className="button secondary" onClick={cancelCatalogRequest}>Cancel load</button></div>}
        {refreshing && catalogSlow && <div className="public-import-state is-slow" role="status"><span className="public-import-spinner" aria-hidden="true"/><div><strong>Catalog refresh is taking longer than expected</strong><p>The previous snapshot remains available. You can cancel instead of waiting for the 30-second deadline.</p></div><button type="button" className="button secondary" onClick={cancelCatalogRequest}>Cancel refresh</button></div>}
        {catalogError && <div className="public-import-state is-error" role="alert"><div><strong>Catalog unavailable</strong><p>{catalogError}</p></div><button type="button" className="button secondary" onClick={() => void loadCatalog(recipes.length > 0)}>Try again</button></div>}
        {catalogCancelled && <div className="public-import-state" role="status"><div><strong>{recipes.length > 0 ? "Catalog refresh canceled" : "Catalog load canceled"}</strong><p>{recipes.length > 0 ? "The previous catalog snapshot remains available." : "No error was reported. You can try loading the catalog again."}</p></div><button type="button" className="button secondary" onClick={() => void loadCatalog(recipes.length > 0)}>{recipes.length > 0 ? "Retry refresh" : "Try again"}</button></div>}
        {!loading && !catalogError && !catalogCancelled && recipes.length === 0 && <div className="public-import-state"><div><strong>The public catalog is empty</strong><p>No catalog recipes were returned. You can refresh or use an immutable URI.</p></div><button type="button" className="button secondary" onClick={() => void loadCatalog(true)}>Refresh catalog</button></div>}
        {applied.length > 0 && <div className="public-import-applied" aria-label="Applied filters">{applied.map(item => <button type="button" key={item.key} onClick={() => navigateFilters(item.remove(filters))}>{item.label}<span aria-hidden="true">×</span><span className="sr-only"> Remove filter</span></button>)}</div>}
        {!loading && recipes.length > 0 && filtered.length === 0 && <div className="public-import-state"><div><strong>No matching recipes</strong><p>Remove one or more filters to broaden the catalog.</p></div><button type="button" className="button secondary" onClick={() => navigateFilters(EMPTY_FILTERS)}>Clear filters</button></div>}
        <div className={`public-import-recipe-list is-${catalogView}`} role="list" aria-label="Public recipes">{filtered.map(recipe => { const compared = compareUris.includes(recipe.uri); return <article role="listitem" className={selectedUri === recipe.uri ? "is-selected" : ""} key={recipe.uri}>
          <header><div><span className={`public-import-readiness readiness-${recipe.execution_readiness}`}>{readinessLabel(recipe.execution_readiness)}</span><span className={`public-import-qualification qualification-${recipe.qualification}`}>{qualificationLabel(recipe)}{recipe.release_version ? ` · v${recipe.release_version}` : ""}</span><span className={`public-import-local status-${recipe.local.status}`}>{localStatusLabel(recipe)}</span></div><h3>{recipe.model_title}</h3><p>{recipe.title}{recipeQuantizations(recipe).length ? ` · ${recipeQuantizations(recipe).join(" · ")}` : ""} · {alignmentLabel(recipe.alignment)}</p></header>
          <dl><div><dt>Sparks</dt><dd>{sparkLabel(recipe.node_count)}</dd></div><div><dt>Download</dt><dd>{formatBytes(recipe.expected_download_bytes)}</dd></div><div><dt>Memory / Spark</dt><dd>{formatBytes(recipe.maximum_runtime_memory_bytes_per_node)}</dd></div></dl>
          <div className="public-import-tags" aria-label={`${recipe.title} capabilities`}>{recipe.capabilities.map(capability => <span key={capability}>{PUBLIC_RECIPE_CAPABILITIES.find(option => option.value === capability)?.label ?? capability}</span>)}</div>
          <footer><label className="public-import-compare-toggle"><input type="checkbox" checked={compared} disabled={!compared && compareUris.length >= 3} onChange={() => toggleComparison(recipe.uri)}/><span>Compare<span className="sr-only"> {recipe.title}</span></span></label>{recipe.source_owner && <span>Source: {recipe.source_owner}</span>}<button type="button" className="button secondary" data-recipe-uri={recipe.uri} aria-current={selectedUri === recipe.uri ? "true" : undefined} onClick={() => selectRecipe(recipe.uri)}>{catalogView === "compact" ? <>Review<span className="sr-only">{recipe.local.status === "update-available" ? ` update for ${recipe.title}` : ` ${recipe.title}`}</span></> : recipe.local.status === "update-available" ? `Review update for ${recipe.title}` : `Review ${recipe.title}`}</button></footer>
        </article>; })}</div>
        <RecipeComparisonTray recipes={comparedRecipes} onRemove={uri => setCompareUris(current => current.filter(value => value !== uri))} onClear={() => setCompareUris([])}/>
      </section>
      <aside className="public-import-review-pane" aria-label="Selected recipe review">
        {previewLoading && <div className={`public-import-state${previewSlow ? " is-slow" : ""}`} role="status"><span className="public-import-spinner" aria-hidden="true"/><div><strong>{previewSlow ? "Recipe preview is taking longer than expected" : "Loading recipe preview"}</strong><p>{previewSlow ? "Verification is still running. You can cancel instead of waiting for the 30-second deadline." : "Verifying the selected immutable revision…"}</p></div><button type="button" className="button secondary" onClick={cancelPreviewRequest}>Cancel preview</button></div>}
        {previewError && <div className="public-import-state is-error" role="alert"><div><strong>Preview unavailable</strong><p>{previewError}</p></div><button type="button" className="button secondary" onClick={retryPreview}>Try again</button></div>}
        {previewCancelled && <div className="public-import-state" role="status"><div><strong>Preview canceled</strong><p>No error was reported. You can retry verification when you are ready.</p></div><button type="button" className="button secondary" onClick={retryPreview}>Try again</button></div>}
        {!selectedUri && !previewLoading && <div className="public-import-review-empty"><span aria-hidden="true">↗</span><strong>Select a recipe to review</strong><p>Execution readiness, qualification, version changes, resource needs and provenance will appear here.</p></div>}
        {preview && <Preview
          preview={preview}
          saving={saving}
          importError={importError}
          importOutcomeUnknown={importOutcomeUnknown}
          status={step}
          onBack={step === "confirm" ? () => onNavigate(publicRecipeImportUrl(filters, {more, recipe: selectedUri, step: "review"})) : returnToCatalog}
          onConfirm={() => onNavigate(publicRecipeImportUrl(filters, {more, recipe: selectedUri, step: "confirm"}))}
          onImport={() => void importRecipe()}
          onOpenLocal={() => { if (preview.local.recipe_id) onNavigate(localRecipePath(preview.local.recipe_id)); }}
          onRecheckImport={recheckImportOutcome}
        />}
      </aside>
    </fieldset>
  </div>;
}
