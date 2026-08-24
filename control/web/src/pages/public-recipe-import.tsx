import {useEffect, useMemo, useRef, useState} from "react";
import type {CatalogApi, PublicRecipe, PublicRecipeCapability, PublicRecipeExecutionReadiness, PublicRecipePreview} from "../api/types";
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

type SparkFilter = "" | "1" | "2" | "3" | "4+";
type RecipeSort = "catalog" | "model" | "sparks" | "download";
type LocalFilter = "" | "not-imported" | "update-available" | "current" | "needs-review";
type ImportStep = "catalog" | "review" | "confirm";
type CatalogView = "cards" | "compact";
type Facet = "model" | "sourceOwner" | "repository" | "sparks" | "runtime" | "precision" | "topology" | "qualification" | "readiness" | "local" | "capability";

const CATALOG_VIEW_STORAGE_KEY = "vonk.public-recipe-catalog.view";

export type PublicRecipeFilters = {
  query: string;
  model: string;
  sourceOwner: string;
  repository: string;
  sparks: SparkFilter;
  runtime: string;
  precision: string;
  topology: string;
  qualification: "" | PublicRecipe["qualification"];
  readiness: "" | PublicRecipeExecutionReadiness;
  local: LocalFilter;
  sort: RecipeSort;
  capabilities: PublicRecipeCapability[];
};

const EMPTY_FILTERS: PublicRecipeFilters = {
  query: "", model: "", sourceOwner: "", repository: "", sparks: "", runtime: "", precision: "", topology: "",
  qualification: "", readiness: "", local: "", sort: "catalog", capabilities: [],
};

const VALID_SPARKS = new Set<SparkFilter>(["", "1", "2", "3", "4+"]);
const VALID_SORTS = new Set<RecipeSort>(["catalog", "model", "sparks", "download"]);
const VALID_LOCAL = new Set<LocalFilter>(["", "not-imported", "update-available", "current", "needs-review"]);
const VALID_READINESS = new Set<PublicRecipeFilters["readiness"]>(["", "executable", "not-executable", "integration-required", "not-declared"]);
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
  const sort = search.get("sort") as RecipeSort | null;
  const local = search.get("local") as LocalFilter | null;
  const qualification = search.get("qualification");
  const readiness = search.get("readiness") as PublicRecipeFilters["readiness"] | null;
  const requestedStep = search.get("step");
  const recipe = search.get("recipe") ?? "";
  const step: ImportStep = recipe && requestedStep === "confirm" ? "confirm" : recipe ? "review" : "catalog";
  return {
    filters: {
      query: search.get("q") ?? "",
      model: search.get("model") ?? "",
      sourceOwner: search.get("source_owner") ?? search.get("creator") ?? "",
      repository: search.get("repository") ?? "",
      sparks: sparks && VALID_SPARKS.has(sparks) ? sparks : "",
      runtime: search.get("runtime") ?? "",
      precision: search.get("precision") ?? "",
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
  if (filters.model) search.set("model", filters.model);
  if (filters.sourceOwner) search.set("source_owner", filters.sourceOwner);
  if (filters.repository) search.set("repository", filters.repository);
  if (filters.sparks) search.set("sparks", filters.sparks);
  if (filters.runtime) search.set("runtime", filters.runtime);
  if (filters.precision) search.set("precision", filters.precision);
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

export function publicRecipeMatches(recipe: PublicRecipe, filters: PublicRecipeFilters, omitted?: Facet): boolean {
  const normalized = filters.query.trim().toLowerCase();
  const queryMatches = !normalized || [recipe.title, recipe.slug, recipe.description, recipe.model_title, recipe.model_slug, recipe.source_owner ?? "", recipe.source_repository ?? "", recipe.runtime_distribution, recipe.precision ?? "", ...recipe.capabilities, ...recipe.tags].some(value => value.toLowerCase().includes(normalized));
  return queryMatches
    && (omitted === "model" || !filters.model || `${recipe.model_publisher}/${recipe.model_slug}` === filters.model)
    && (omitted === "sourceOwner" || !filters.sourceOwner || recipe.source_owner === filters.sourceOwner)
    && (omitted === "repository" || !filters.repository || recipe.source_repository === filters.repository)
    && (omitted === "sparks" || sparkMatches(recipe, filters.sparks))
    && (omitted === "runtime" || !filters.runtime || recipe.runtime_distribution === filters.runtime)
    && (omitted === "precision" || !filters.precision || recipe.precision === filters.precision)
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

function localStatusLabel(recipe: PublicRecipe): string {
  if (recipe.local.status === "current") return "Installed · current";
  if (recipe.local.status === "update-available") return `Update from v${recipe.local.release_version ?? "?"}`;
  if (recipe.local.status === "local-ahead") return "Local version is newer";
  if (recipe.local.status === "different-revision") return "Different local revision";
  if (recipe.local.status === "conflict") return "Local identity conflict";
  return "Not installed";
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
          <tr><th scope="row">Precision</th>{recipes.map(recipe => <td key={recipe.uri}>{recipe.precision ?? "Not specified"}</td>)}</tr>
          <tr><th scope="row">Source owner</th>{recipes.map(recipe => <td key={recipe.uri}>{recipe.source_owner ?? "Not specified"}</td>)}</tr>
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
  if (filters.model) add("model", `Model: ${filters.model.split("/").at(-1)}`, {model: ""});
  if (filters.sourceOwner) add("sourceOwner", `Source owner: ${filters.sourceOwner}`, {sourceOwner: ""});
  if (filters.repository) add("repository", `Repository: ${repositoryLabel(filters.repository)}`, {repository: ""});
  if (filters.sparks) add("sparks", `Sparks: ${filters.sparks}`, {sparks: ""});
  if (filters.runtime) add("runtime", `Runtime: ${runtimeLabel(filters.runtime)}`, {runtime: ""});
  if (filters.precision) add("precision", `Precision: ${filters.precision}`, {precision: ""});
  if (filters.topology) add("topology", `Topology: ${runtimeLabel(filters.topology)}`, {topology: ""});
  if (filters.qualification) add("qualification", `Qualification: ${filters.qualification === "candidate" ? "Candidate" : "Accepted"}`, {qualification: ""});
  if (filters.readiness) add("readiness", `Execution readiness: ${readinessLabel(filters.readiness)}`, {readiness: ""});
  if (filters.local) add("local", `Local: ${filters.local.replaceAll("-", " ")}`, {local: ""});
  if (filters.sort !== "catalog") add("sort", `Sort: ${filters.sort}`, {sort: "catalog"});
  for (const capability of filters.capabilities) items.push({key: `capability:${capability}`, label: `Capability: ${PUBLIC_RECIPE_CAPABILITIES.find(option => option.value === capability)?.label ?? capability}`, remove: current => ({...current, capabilities: current.capabilities.filter(value => value !== capability)})});
  return items;
}

function Preview({preview, saving, status, onBack, onConfirm, onImport}: {
  preview: PublicRecipePreview;
  saving: boolean;
  status: string;
  onBack(): void;
  onConfirm(): void;
  onImport(): void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { queueMicrotask(() => heading.current?.focus()); }, [preview.uri, status]);
  const requiredEffect = strongestUpgradeEffect(preview.changes_since_local);
  return <section className={`public-import-preview${status === "confirm" ? " is-confirming" : ""}`} aria-labelledby="public-import-preview-title" aria-busy={saving}>
    <button type="button" className="button secondary public-import-mobile-back" disabled={saving} onClick={onBack}>Back to catalog</button>
    <header className="public-import-preview-header">
      <div><span className="public-import-kicker">{status === "confirm" ? "Confirm import" : "Review recipe"}</span><h2 id="public-import-preview-title" ref={heading} tabIndex={-1}>{preview.title}</h2><p>{preview.model_title}{preview.precision ? ` · ${preview.precision}` : ""}</p></div>
      <div className="public-import-preview-signals"><span className={`public-import-readiness readiness-${preview.execution_readiness}`}>{readinessLabel(preview.execution_readiness)}</span><span className={`public-import-qualification qualification-${preview.qualification}`}>{qualificationLabel(preview)}</span></div>
    </header>
    <div className={`public-import-trust qualification-${preview.qualification}`}><strong>{qualificationLabel(preview)} qualification</strong><p>{preview.qualification_detail}</p></div>
    <div className={`public-import-trust readiness-${preview.execution_readiness}`}><strong>{readinessLabel(preview.execution_readiness)}</strong><p>{preview.execution_readiness_detail}</p></div>
    <div className="public-import-version-summary" aria-label="Version summary">
      <div><span>Installed</span><strong>{preview.local.release_version ? `v${preview.local.release_version}` : preview.local.status === "not-imported" ? "Not installed" : "Unknown revision"}</strong></div>
      <span aria-hidden="true">→</span>
      <div><span>Available</span><strong>{preview.release_version ? `v${preview.release_version}` : "Immutable revision"}</strong></div>
      <div><span>Upgrade effect</span><strong>{requiredEffect ? upgradeEffectLabel(requiredEffect) : preview.local.status === "not-imported" ? "New installation" : "No runtime change listed"}</strong></div>
      <div><span>Changes</span><strong>{preview.changes_since_local.reduce((total, release) => total + release.changes.length, 0)}</strong></div>
    </div>
    {status === "confirm" && <div className={`public-import-confirmation-copy readiness-${preview.execution_readiness}`}><strong>{preview.execution_readiness === "not-executable" ? "Import this non-executable recipe metadata?" : preview.execution_readiness === "integration-required" ? "Import this integration-required recipe?" : preview.qualification === "candidate" ? "Import this candidate?" : "Import this recipe?"}</strong><p>This saves a new immutable local recipe revision. It does not prove the recipe can run, and it does not rebuild, reinstall, or restart running services.</p></div>}
    {status !== "confirm" && <>
      <p className="public-import-description">{preview.description || "No description provided."}</p>
      <div className="public-import-tags" aria-label="Recipe capabilities">{preview.capabilities.map(capability => <span key={capability}>{PUBLIC_RECIPE_CAPABILITIES.find(option => option.value === capability)?.label ?? capability}</span>)}</div>
      <dl className="public-import-primary-facts"><div><dt>Sparks</dt><dd>{sparkLabel(preview.node_count)}</dd></div><div><dt>Download</dt><dd>{formatBytes(preview.expected_download_bytes)}</dd></div><div><dt>Memory / Spark</dt><dd>{formatBytes(preview.maximum_runtime_memory_bytes_per_node)}</dd></div><div><dt>Source owner</dt><dd>{preview.source_owner ?? "Not specified"}</dd></div></dl>
      <RecipeTopology recipe={preview}/>
      <RecipeRequirements recipe={preview}/>
      {preview.local.status === "update-available" && <p className="public-import-note">Existing installations and running services remain pinned to their current revision until you rebuild or reinstall them.</p>}
      {["different-revision", "conflict", "local-ahead"].includes(preview.local.status) && <p className="public-import-warning" role="alert">{preview.local.status === "different-revision" ? "The local digest is not in catalog history, so an exact change list cannot be proven." : preview.local.status === "conflict" ? "A different local recipe owns this slug. Resolve the conflict before importing." : "The local release is newer than this catalog snapshot. Import is disabled to avoid a downgrade."}</p>}
      {preview.changes_since_local.length > 0 && <section className="public-import-changelog" aria-labelledby="public-import-changelog-title"><header><span className="public-import-kicker">Release notes</span><h3 id="public-import-changelog-title">{preview.local.status === "update-available" ? `Changes since local v${preview.local.release_version}` : "Catalog changelog"}</h3></header>{preview.changes_since_local.map((release, releaseIndex) => <details open={releaseIndex === 0} key={`${release.version}:${release.content_sha256}`}><summary><strong>v{release.version}</strong><span>{release.released_at} · {upgradeEffectLabel(release.upgrade_effect)}</span></summary><ul>{release.changes.map((change, index) => <li key={`${change.kind}:${index}`}><span>{change.kind}</span><strong>{change.summary}</strong>{change.details && <p>{change.details}</p>}{change.references.length > 0 && <div>{change.references.map((reference, referenceIndex) => <a href={reference} target="_blank" rel="noreferrer" key={reference}>Source {referenceIndex + 1}<span className="sr-only"> (opens in a new tab)</span></a>)}</div>}</li>)}</ul></details>)}</section>}
      <details className="public-import-technical"><summary>Technical details</summary><dl><div><dt>Catalog identity</dt><dd>{preview.publisher}/{preview.slug}</dd></div><div><dt>Qualification evidence</dt><dd>{preview.qualification_basis.replaceAll("-", " ")}</dd></div><div><dt>Readiness evidence</dt><dd>{preview.execution_readiness_basis.replaceAll("-", " ")}</dd></div><div><dt>Runtime</dt><dd>{runtimeLabel(preview.runtime_distribution)}</dd></div><div><dt>Execution</dt><dd>{runtimeLabel(preview.execution_harness)}</dd></div><div><dt>Topology</dt><dd>{preview.topology_mode}</dd></div><div><dt>Installed / Spark</dt><dd>{formatBytes(preview.maximum_installed_bytes_per_node)}</dd></div><div><dt>Artifacts</dt><dd>{preview.artifact_count}</dd></div>{preview.source_repository && <div><dt>Original repository</dt><dd><a href={preview.source_repository} target="_blank" rel="noreferrer">View source<span className="sr-only"> (opens in a new tab)</span></a></dd></div>}<div><dt>Immutable digest</dt><dd><code>sha256:{preview.content_sha256}</code></dd></div></dl></details>
    </>}
    <footer className="public-import-preview-actions">
      <button type="button" className="button secondary" disabled={saving} onClick={onBack}>{status === "confirm" ? "Back to review" : "Choose another recipe"}</button>
      {status === "confirm" ? <button type="button" className="button" disabled={saving || ["current", "conflict", "local-ahead"].includes(preview.local.status)} onClick={onImport}>{saving ? "Importing…" : preview.local.status === "update-available" || preview.local.status === "different-revision" ? `Import${preview.release_version ? ` v${preview.release_version}` : " catalog revision"}` : preview.qualification === "candidate" ? "Import candidate" : "Import recipe"}</button> : <button type="button" className="button" disabled={saving || ["current", "conflict", "local-ahead"].includes(preview.local.status)} onClick={onConfirm}>{preview.local.status === "current" ? "Already current" : "Continue to confirm"}</button>}
    </footer>
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
  const [catalogError, setCatalogError] = useState("");
  const [preview, setPreview] = useState<PublicRecipePreview>();
  const [previewError, setPreviewError] = useState("");
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [completion, setCompletion] = useState("");
  const [manualUri, setManualUri] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [catalogView, setCatalogView] = useState<CatalogView>(storedCatalogView);
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
    refresh ? setRefreshing(true) : setLoading(true);
    try {
      const result = await api.listPublicRecipes(controller.signal);
      if (controller.signal.aborted) return;
      setRecipes(result.recipes);
      setCommit(result.commit);
    } catch (value) {
      if (!controller.signal.aborted) setCatalogError(value instanceof Error ? value.message : "Unable to load the current recipe catalog");
    } finally {
      if (!controller.signal.aborted) { setLoading(false); setRefreshing(false); }
      if (catalogRequest.current === controller) catalogRequest.current = undefined;
    }
  }

  useEffect(() => {
    queueMicrotask(() => heading.current?.focus());
    void loadCatalog();
    return () => { catalogRequest.current?.abort(); previewRequest.current?.abort(); importRequest.current?.abort(); };
  }, [api]);

  useEffect(() => { onBusyChange?.(saving); }, [onBusyChange, saving]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);

  useEffect(() => {
    if (!selectedUri) { previewRequest.current?.abort(); setPreview(undefined); setPreviewError(""); setPreviewLoading(false); return; }
    const controller = new AbortController();
    previewRequest.current?.abort();
    previewRequest.current = controller;
    const sequence = ++requestSequence.current;
    setPreview(undefined);
    setPreviewError("");
    setCompletion("");
    setPreviewLoading(true);
    void api.previewPublicRecipe(selectedUri, controller.signal).then(value => {
      if (!controller.signal.aborted && sequence === requestSequence.current && value.uri === selectedUri) setPreview(value);
    }).catch(value => {
      if (!controller.signal.aborted && sequence === requestSequence.current) setPreviewError(value instanceof Error ? value.message : "Unable to preview import");
    }).finally(() => {
      if (!controller.signal.aborted && sequence === requestSequence.current) setPreviewLoading(false);
      if (previewRequest.current === controller) previewRequest.current = undefined;
    });
    return () => controller.abort();
  }, [api, selectedUri, previewAttempt]);

  useEffect(() => {
    if (!importRequest.current) return;
    importSequence.current += 1;
    importRequest.current.abort();
    importRequest.current = undefined;
    setSaving(false);
  }, [selectedUri, step]);

  const models = useMemo(() => Array.from(new Map(recipes.map(recipe => [`${recipe.model_publisher}/${recipe.model_slug}`, recipe.model_title])).entries()).sort((a, b) => a[1].localeCompare(b[1])), [recipes]);
  const sourceOwners = useMemo(() => Array.from(new Set(recipes.flatMap(recipe => recipe.source_owner ? [recipe.source_owner] : []))).sort(), [recipes]);
  const repositories = useMemo(() => Array.from(new Set(recipes.flatMap(recipe => recipe.source_repository ? [recipe.source_repository] : []))).sort(), [recipes]);
  const runtimes = useMemo(() => Array.from(new Set(recipes.map(recipe => recipe.runtime_distribution))).sort(), [recipes]);
  const precisions = useMemo(() => Array.from(new Set(recipes.flatMap(recipe => recipe.precision ? [recipe.precision] : []))).sort(), [recipes]);
  const topologies = useMemo(() => Array.from(new Set(recipes.map(recipe => recipe.topology_mode))).sort(), [recipes]);
  const filtered = useMemo(() => sortRecipes(recipes.filter(recipe => publicRecipeMatches(recipe, filters)), filters.sort), [filters, recipes]);
  const count = (facet: Facet, predicate: (recipe: PublicRecipe) => boolean) => recipes.filter(recipe => publicRecipeMatches(recipe, filters, facet) && predicate(recipe)).length;
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

  function chooseCatalogView(view: CatalogView) {
    setCatalogView(view);
    saveCatalogView(view);
    setAnnouncement(`${view === "cards" ? "Cards" : "Compact list"} view selected.`);
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

  async function importRecipe() {
    if (!preview) return;
    importRequest.current?.abort();
    const controller = new AbortController();
    importRequest.current = controller;
    const sequence = ++importSequence.current;
    const route = importRoute.current;
    setSaving(true);
    setPreviewError("");
    try {
      await api.importPublicRecipe(preview.uri, preview.content_sha256, controller.signal);
      if (controller.signal.aborted || sequence !== importSequence.current || route !== importRoute.current) return;
      const updated = preview.local.status === "update-available" || preview.local.status === "different-revision";
      setCompletion(updated && preview.release_version ? `Updated ${preview.title} to v${preview.release_version}` : `Imported ${preview.title}`);
      setPreview(current => current ? {...current, local: {...current.local, status: "current", content_sha256: current.content_sha256, release_version: current.release_version}} : current);
      await loadCatalog(true);
    } catch (value) {
      if (!controller.signal.aborted) setPreviewError(value instanceof Error ? value.message : "Unable to import recipe");
    } finally {
      if (!controller.signal.aborted && sequence === importSequence.current && route === importRoute.current) setSaving(false);
      if (importRequest.current === controller) importRequest.current = undefined;
    }
  }

  if (completion) return <section className="public-import-complete" aria-labelledby="public-import-complete-title" role="status" tabIndex={-1} ref={element => { if (element) queueMicrotask(() => element.focus()); }}><span className="public-import-kicker">Import complete</span><h1 id="public-import-complete-title">{completion}</h1><p>The immutable revision is saved in your local Library. Existing installations and running services were not changed.</p><div><a className="button" href="/library">View local Library</a><button type="button" className="button secondary" onClick={() => { setCompletion(""); returnToCatalog(); }}>Import another recipe</button></div></section>;

  return <div className={`public-import-page step-${step}`}>
    <header className="public-import-page-header">
      <div><a href="/library" className="public-import-back" aria-disabled={saving || undefined} tabIndex={saving ? -1 : undefined} onClick={saving ? event => event.preventDefault() : undefined}>← Library</a><span className="public-import-kicker">Digest-bound public catalog</span><h1 ref={heading} tabIndex={-1}>Import a public recipe</h1><p>Choose an immutable recipe, inspect its independent qualification and execution-readiness evidence, then confirm the local import.</p></div>
      <ol aria-label="Import progress"><li aria-current={step === "catalog" ? "step" : undefined}>1 <span>Catalog</span></li><li aria-current={step === "review" ? "step" : undefined}>2 <span>Review</span></li><li aria-current={step === "confirm" ? "step" : undefined}>3 <span>Confirm</span></li></ol>
    </header>
    <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
    <fieldset className="public-import-workspace public-import-interaction-lock" disabled={saving} aria-busy={saving}>
      <legend className="sr-only">Recipe catalog and review</legend>
      <aside className="public-import-filter-rail" aria-label="Recipe filters">
        <div className="public-import-filter-heading"><div><span className="public-import-kicker">Narrow the catalog</span><h2>Filters</h2></div>{applied.length > 0 && <button type="button" className="public-import-text-button" onClick={() => navigateFilters(EMPTY_FILTERS)}>Clear all</button>}</div>
        <label className="public-import-search"><span>Find a recipe</span><input type="search" value={filters.query} onChange={event => updateFilter("query", event.target.value)} placeholder="Model, modality, runtime…" /></label>
        <label><span>Model</span><select aria-label="Filter by model" value={filters.model} onChange={event => updateFilter("model", event.target.value)}><option value="">All models ({count("model", () => true)})</option>{models.map(([value, label]) => { const available = count("model", recipe => `${recipe.model_publisher}/${recipe.model_slug}` === value); return <option value={value} disabled={available === 0} key={value}>{label} ({available})</option>; })}</select></label>
        <label><span>Sparks</span><select aria-label="Filter by required Sparks" value={filters.sparks} onChange={event => updateFilter("sparks", event.target.value as SparkFilter)}><option value="">Any count ({count("sparks", () => true)})</option>{(["1", "2", "3", "4+"] as SparkFilter[]).map(value => { const available = count("sparks", recipe => sparkMatches(recipe, value)); return <option value={value} disabled={available === 0} key={value}>{value}{value === "1" ? " Spark" : " Sparks"} ({available})</option>; })}</select></label>
        <label><span>Qualification</span><select aria-label="Filter by qualification" value={filters.qualification} onChange={event => updateFilter("qualification", event.target.value as PublicRecipeFilters["qualification"])}><option value="">Any status ({count("qualification", () => true)})</option><option value="cataloged" disabled={count("qualification", recipe => recipe.qualification === "cataloged") === 0}>Accepted ({count("qualification", recipe => recipe.qualification === "cataloged")})</option><option value="candidate" disabled={count("qualification", recipe => recipe.qualification === "candidate") === 0}>Candidate ({count("qualification", recipe => recipe.qualification === "candidate")})</option></select></label>
        <label><span>Execution readiness</span><select aria-label="Filter by execution readiness" value={filters.readiness} onChange={event => updateFilter("readiness", event.target.value as PublicRecipeFilters["readiness"])}><option value="">Any declaration ({count("readiness", () => true)})</option>{(["executable", "integration-required", "not-executable", "not-declared"] as PublicRecipeExecutionReadiness[]).map(value => { const available = count("readiness", recipe => recipe.execution_readiness === value); return <option key={value} value={value} disabled={available === 0}>{readinessLabel(value)} ({available})</option>; })}</select></label>
        <label><span>Local status</span><select aria-label="Filter by local status" value={filters.local} onChange={event => updateFilter("local", event.target.value as LocalFilter)}><option value="">All ({count("local", () => true)})</option>{(["not-imported", "update-available", "current", "needs-review"] as LocalFilter[]).map(value => { const available = count("local", recipe => localMatches(recipe, value)); return <option value={value} disabled={available === 0} key={value}>{value === "not-imported" ? "Not installed" : value === "update-available" ? "Update available" : value === "current" ? "Installed current" : "Needs review"} ({available})</option>; })}</select></label>
        <button type="button" className="button secondary public-import-more-toggle" aria-expanded={more} aria-controls="public-import-more-filters" onClick={() => onNavigate(publicRecipeImportUrl(filters, {more: !more}), true)}>{more ? "Hide more filters" : "More filters"}</button>
        <div id="public-import-more-filters" hidden={!more} className="public-import-more-filters">
          <label><span>Source owner</span><select aria-label="Filter by source owner" value={filters.sourceOwner} onChange={event => updateFilter("sourceOwner", event.target.value)}><option value="">All source owners</option>{sourceOwners.map(value => <option key={value} value={value} disabled={count("sourceOwner", recipe => recipe.source_owner === value) === 0}>{value} ({count("sourceOwner", recipe => recipe.source_owner === value)})</option>)}</select></label>
          <label><span>Original repository</span><select aria-label="Filter by original repository" value={filters.repository} onChange={event => updateFilter("repository", event.target.value)}><option value="">All repositories</option>{repositories.map(value => <option key={value} value={value} disabled={count("repository", recipe => recipe.source_repository === value) === 0}>{repositoryLabel(value)} ({count("repository", recipe => recipe.source_repository === value)})</option>)}</select></label>
          <label><span>Runtime</span><select aria-label="Filter by runtime" value={filters.runtime} onChange={event => updateFilter("runtime", event.target.value)}><option value="">All runtimes</option>{runtimes.map(value => <option key={value} value={value} disabled={count("runtime", recipe => recipe.runtime_distribution === value) === 0}>{runtimeLabel(value)} ({count("runtime", recipe => recipe.runtime_distribution === value)})</option>)}</select></label>
          <label><span>Precision</span><select aria-label="Filter by precision" value={filters.precision} onChange={event => updateFilter("precision", event.target.value)}><option value="">Any precision</option>{precisions.map(value => <option key={value} value={value} disabled={count("precision", recipe => recipe.precision === value) === 0}>{value} ({count("precision", recipe => recipe.precision === value)})</option>)}</select></label>
          <label><span>Topology</span><select aria-label="Filter by topology" value={filters.topology} onChange={event => updateFilter("topology", event.target.value)}><option value="">Any topology</option>{topologies.map(value => <option key={value} value={value} disabled={count("topology", recipe => recipe.topology_mode === value) === 0}>{runtimeLabel(value)} ({count("topology", recipe => recipe.topology_mode === value)})</option>)}</select></label>
          <label><span>Sort</span><select aria-label="Sort recipes" value={filters.sort} onChange={event => updateFilter("sort", event.target.value as RecipeSort)}><option value="catalog">Catalog order</option><option value="model">Model A–Z</option><option value="sparks">Fewest Sparks</option><option value="download">Smallest download</option></select></label>
        </div>
        <fieldset className="public-import-capabilities"><legend>Capabilities <span>Must all match</span></legend>{PUBLIC_RECIPE_CAPABILITIES.map(option => { const selected = filters.capabilities.includes(option.value); const available = capabilityCount(option.value); return <label className={available === 0 && !selected ? "is-disabled" : ""} key={option.value}><input type="checkbox" checked={selected} disabled={available === 0 && !selected} onChange={() => updateFilter("capabilities", selected ? filters.capabilities.filter(value => value !== option.value) : [...filters.capabilities, option.value])}/><span>{option.label}</span><small>{available}</small></label>; })}</fieldset>
        <details className="public-import-manual"><summary>Advanced: import URI</summary><div><label><span>Public recipe URI</span><input value={manualUri} onChange={event => setManualUri(event.target.value)} placeholder="vonk://catalog/…@sha256:…" /></label><button type="button" className="button secondary" disabled={!manualUri} onClick={() => selectRecipe(manualUri)}>Review URI</button></div></details>
      </aside>
      <section className="public-import-results" aria-busy={loading || refreshing} aria-labelledby="public-recipe-results-heading">
        <header><div><span className="public-import-kicker">Public recipe library</span><h2 id="public-recipe-results-heading">Choose a recipe</h2></div><div><span>{refreshing ? "Refreshing…" : `${filtered.length} of ${recipes.length}`}</span><button type="button" className="public-import-icon-button" aria-label="Refresh public catalog" onClick={() => void loadCatalog(true)}>↻</button></div></header>
        <div className="public-import-results-tools">
          <div className="public-import-view-switch" role="group" aria-label="Catalog view">
            <button type="button" aria-pressed={catalogView === "cards"} onClick={() => chooseCatalogView("cards")}><span aria-hidden="true">▦</span> Cards</button>
            <button type="button" aria-pressed={catalogView === "compact"} onClick={() => chooseCatalogView("compact")}><span aria-hidden="true">☷</span> Compact</button>
          </div>
          {commit && <details className="public-import-snapshot"><summary>Catalog snapshot</summary><div><span>Immutable commit</span><code>{commit}</code></div></details>}
        </div>
        {loading && recipes.length === 0 && <div className="public-import-state" role="status"><span className="public-import-spinner" aria-hidden="true"/><div><strong>Loading the public catalog</strong><p>Resolving one immutable library snapshot…</p></div></div>}
        {catalogError && <div className="public-import-state is-error" role="alert"><div><strong>Catalog unavailable</strong><p>{catalogError}</p></div><button type="button" className="button secondary" onClick={() => void loadCatalog()}>Try again</button></div>}
        {!loading && !catalogError && recipes.length === 0 && <div className="public-import-state"><div><strong>The public catalog is empty</strong><p>No catalog recipes were returned. You can refresh or use an immutable URI.</p></div><button type="button" className="button secondary" onClick={() => void loadCatalog(true)}>Refresh catalog</button></div>}
        {applied.length > 0 && <div className="public-import-applied" aria-label="Applied filters">{applied.map(item => <button type="button" key={item.key} onClick={() => navigateFilters(item.remove(filters))}>{item.label}<span aria-hidden="true">×</span><span className="sr-only"> Remove filter</span></button>)}</div>}
        {!loading && recipes.length > 0 && filtered.length === 0 && <div className="public-import-state"><div><strong>No matching recipes</strong><p>Remove one or more filters to broaden the catalog.</p></div><button type="button" className="button secondary" onClick={() => navigateFilters(EMPTY_FILTERS)}>Clear filters</button></div>}
        <div className={`public-import-recipe-list is-${catalogView}`} role="list" aria-label="Public recipes">{filtered.map(recipe => { const compared = compareUris.includes(recipe.uri); return <article role="listitem" className={selectedUri === recipe.uri ? "is-selected" : ""} key={recipe.uri}>
          <header><div><span className={`public-import-readiness readiness-${recipe.execution_readiness}`}>{readinessLabel(recipe.execution_readiness)}</span><span className={`public-import-qualification qualification-${recipe.qualification}`}>{qualificationLabel(recipe)}{recipe.release_version ? ` · v${recipe.release_version}` : ""}</span><span className={`public-import-local status-${recipe.local.status}`}>{localStatusLabel(recipe)}</span></div><h3>{recipe.model_title}</h3><p>{recipe.title}{recipe.precision ? ` · ${recipe.precision}` : ""}</p></header>
          <dl><div><dt>Sparks</dt><dd>{sparkLabel(recipe.node_count)}</dd></div><div><dt>Download</dt><dd>{formatBytes(recipe.expected_download_bytes)}</dd></div><div><dt>Memory / Spark</dt><dd>{formatBytes(recipe.maximum_runtime_memory_bytes_per_node)}</dd></div></dl>
          <div className="public-import-tags" aria-label={`${recipe.title} capabilities`}>{recipe.capabilities.map(capability => <span key={capability}>{PUBLIC_RECIPE_CAPABILITIES.find(option => option.value === capability)?.label ?? capability}</span>)}</div>
          <footer><label className="public-import-compare-toggle"><input type="checkbox" checked={compared} disabled={!compared && compareUris.length >= 3} onChange={() => toggleComparison(recipe.uri)}/><span>Compare<span className="sr-only"> {recipe.title}</span></span></label>{recipe.source_owner && <span>Source: {recipe.source_owner}</span>}<button type="button" className="button secondary" data-recipe-uri={recipe.uri} aria-current={selectedUri === recipe.uri ? "true" : undefined} onClick={() => selectRecipe(recipe.uri)}>{catalogView === "compact" ? <>Review<span className="sr-only">{recipe.local.status === "update-available" ? ` update for ${recipe.title}` : ` ${recipe.title}`}</span></> : recipe.local.status === "update-available" ? `Review update for ${recipe.title}` : `Review ${recipe.title}`}</button></footer>
        </article>; })}</div>
        <RecipeComparisonTray recipes={comparedRecipes} onRemove={uri => setCompareUris(current => current.filter(value => value !== uri))} onClear={() => setCompareUris([])}/>
      </section>
      <aside className="public-import-review-pane" aria-label="Selected recipe review">
        {previewLoading && <div className="public-import-state" role="status"><span className="public-import-spinner" aria-hidden="true"/><div><strong>Loading recipe preview</strong><p>Verifying the selected immutable revision…</p></div></div>}
        {previewError && <div className="public-import-state is-error" role="alert"><div><strong>Preview unavailable</strong><p>{previewError}</p></div><button type="button" className="button secondary" onClick={() => setPreviewAttempt(value => value + 1)}>Try again</button></div>}
        {!selectedUri && !previewLoading && <div className="public-import-review-empty"><span aria-hidden="true">↗</span><strong>Select a recipe to review</strong><p>Execution readiness, qualification, version changes, resource needs and provenance will appear here.</p></div>}
        {preview && <Preview preview={preview} saving={saving} status={step} onBack={step === "confirm" ? () => onNavigate(publicRecipeImportUrl(filters, {more, recipe: selectedUri, step: "review"})) : returnToCatalog} onConfirm={() => onNavigate(publicRecipeImportUrl(filters, {more, recipe: selectedUri, step: "confirm"}))} onImport={() => void importRecipe()}/>}
      </aside>
    </fieldset>
  </div>;
}
