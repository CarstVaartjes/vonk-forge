import {useEffect, useRef} from "react";
import type {MouseEvent, ReactNode} from "react";
import type {ControlApi, LibraryViewModel, LibraryViewRecipe, LibraryViewRecipeDetail, LibraryViewSnapshot, VisualFleetSnapshot} from "../api/types";
import {canonicalRecipeSelector} from "../api/types";
import type {LibraryRoute} from "../lib/library-route";
import {modelKey, modelLibraryPath, recipeLibraryPath} from "../lib/library-route";
import {formatBytes} from "../lib/fleet";
import {LibraryRecipeDownloadAction} from "./library-recipe-download-action";

// Filter names mirror `vonkctl model library` and `vonkctl recipe library`
// one-for-one: --model --usage --family --version --quantization.
export type LibraryWorkcellFilters = {model: string; usage: string; family: string; version: string; quantization: string};
export const EMPTY_LIBRARY_WORKCELL_FILTERS: LibraryWorkcellFilters = {model: "", usage: "", family: "", version: "", quantization: ""};

const FILTER_PARAMS = ["model", "usage", "family", "version", "quantization"] as const;

export function libraryFiltersFromSearch(params: URLSearchParams): LibraryWorkcellFilters {
  return {
    model: params.get("model") ?? "",
    usage: params.get("usage") ?? "",
    family: params.get("family") ?? "",
    version: params.get("version") ?? "",
    quantization: params.get("quantization") ?? "",
  };
}
export function libraryFiltersToSearch(filters: LibraryWorkcellFilters, params = new URLSearchParams()): URLSearchParams {
  for (const name of FILTER_PARAMS) {
    if (filters[name]) params.set(name, filters[name]); else params.delete(name);
  }
  return params;
}

export type LibraryRecipeRecord = {
  key: string;
  title: string;
  recipe?: LibraryViewRecipe;
  model?: LibraryViewModel["model"];
  modelDocument?: LibraryViewModel["model_document"];
  modelCapabilities?: LibraryViewModel["model_capabilities"];
  modelKey: string;
  modelTitle: string;
  modelFiles: LibraryViewModel["model_document"]["files"];
  modelBytes: number;
  capabilities: string[];
};

function modelTitle(model: LibraryViewModel): string {
  return model.model_document.identity.model.title || model.model_document.identity.family.title || `${model.model.publisher}/${model.model.slug}`;
}

function recipeRecordKey(modelKey: string, recipe: LibraryViewRecipe): string {
  return `${modelKey}:${recipe.recipe_id}:${recipe.publisher}/${recipe.slug}@${recipe.content_sha256}`;
}

export function recipeAttribution(document: LibraryViewRecipe["recipe_document"]): string {
  const attribution = [...new Set(document.provenance.attribution.map(value => value.trim()).filter(Boolean))];
  return attribution.length > 0 ? `Creator: ${attribution.join(", ")}` : "Creator: not declared";
}

export function buildLibraryRecipeRecords(snapshot: LibraryViewSnapshot): LibraryRecipeRecord[] {
  return snapshot.models.flatMap(model => {
    const key = modelKey(model.model);
    const title = modelTitle(model);
    const files = model.model_document.files;
    const bytes = files.reduce((total, file) => total + file.size_bytes, 0);
    const capabilities = (model.model_capabilities?.facts ?? []).filter(fact => fact.support === "supported").map(fact => fact.capability);
    if (model.recipes.length === 0) return [{key: `model:${key}`, title, model: model.model, modelDocument: model.model_document, modelCapabilities: model.model_capabilities, modelKey: key, modelTitle: title, modelFiles: files, modelBytes: bytes, capabilities}];
    return model.recipes.map(recipe => ({key: recipeRecordKey(key, recipe), title: recipe.title, recipe, model: model.model, modelDocument: model.model_document, modelCapabilities: model.model_capabilities, modelKey: key, modelTitle: title, modelFiles: files, modelBytes: bytes, capabilities: [...new Set([...capabilities, ...recipe.capabilities])]}));
  });
}

export function recordFamily(record: LibraryRecipeRecord): string {
  const family = record.modelDocument?.identity.family;
  return family ? `${family.publisher}/${family.slug}` : "";
}

export function filterLibraryRecipeRecords(records: LibraryRecipeRecord[], filters: LibraryWorkcellFilters, query: string): LibraryRecipeRecord[] {
  const needle = query.trim().toLocaleLowerCase();
  return records.filter(record => {
    const text = [record.title, record.modelTitle, record.recipe?.slug ?? "", ...record.capabilities].join(" ").toLocaleLowerCase();
    return (!needle || text.includes(needle))
      && (!filters.model || record.modelKey === filters.model)
      && (!filters.usage || record.capabilities.includes(filters.usage))
      && (!filters.family || recordFamily(record) === filters.family)
      && (!filters.version || record.modelDocument?.identity.version === filters.version)
      && (!filters.quantization || record.modelDocument?.format.quantization === filters.quantization);
  });
}

function NavigateLink({current, href, onNavigate, children}: {current?: boolean; href: string; onNavigate: (event: MouseEvent<HTMLAnchorElement>, path: string) => void; children: ReactNode}) {
  return <a href={href} aria-current={current ? "page" : undefined} onClick={event => onNavigate(event, href)}>{children}</a>;
}

export function LibraryWorkcell({api, detail: _detail, fleet: _fleet, filters, onFiltersChange, onNavigate, onQueryChange, onRefresh, query, route, snapshot}: {
  api: ControlApi;
  detail?: LibraryViewRecipeDetail;
  detailError?: string;
  detailLoading?: boolean;
  fleet?: VisualFleetSnapshot;
  fleetError?: string;
  filters: LibraryWorkcellFilters;
  onFiltersChange(filters: LibraryWorkcellFilters): void;
  onNavigate: (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
  onQueryChange(value: string): void;
  onRefresh?: (signal: AbortSignal) => Promise<void>;
  onRetryDetail?: () => void;
  onRetryFleet?: () => void;
  query: string;
  route: LibraryRoute;
  snapshot: LibraryViewSnapshot;
  [key: string]: unknown;
}) {
  const records = buildLibraryRecipeRecords(snapshot);
  const matching = filterLibraryRecipeRecords(records, filters, query);
  const models = [...new Map(records.map(record => [record.modelKey, record])).values()]
    .filter(record => !query.trim() || matching.some(item => item.modelKey === record.modelKey));
  const modelOptions = [...new Map(records.map(record => [record.modelKey, record.modelTitle])).entries()];
  const selectedModelKey = route.kind === "model" ? route.modelKey : filters.model || models[0]?.modelKey;
  const selectedModel = models.find(record => record.modelKey === selectedModelKey);
  const selectedRecipes = matching.filter(record => record.modelKey === selectedModelKey && record.recipe);
  const recipePaneRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if ((!filters.model && route.kind !== "model") || typeof window === "undefined" || window.innerWidth > 760) return;
    if (recipePaneRef.current && typeof recipePaneRef.current.scrollIntoView === "function") recipePaneRef.current.scrollIntoView({block: "start"});
    recipePaneRef.current?.focus({preventScroll: true});
  }, [filters.model, route.kind, selectedModelKey]);
  // A recipe download also caches the model artifacts it needs, so name the
  // models that are not cached yet before the operator commits.
  const controllerByModel = new Map(snapshot.models.map(model => [`${model.model.publisher}/${model.model.slug}`, model.local.controller]));
  const missingModelsFor = (recipe: LibraryViewRecipe): string[] => (recipe.model_selectors ?? []).filter(selector => controllerByModel.get(selector) !== "cached");
  const refresh = () => void onRefresh?.(new AbortController().signal);
  return <section className="library-workcell" aria-label="Models and recipes">
    <div className="library-workcell-toolbar">
      <label>Find models or recipes<input type="search" aria-label="Search Library" value={query} onChange={event => onQueryChange(event.target.value)} placeholder="Search names, slugs, capabilities" /></label>
      <label>Exact model<select aria-label="Filter exact model" value={filters.model} onChange={event => onFiltersChange({...filters, model: event.target.value})}><option value="">All models</option>{modelOptions.map(([key, title]) => <option key={key} value={key}>{title}</option>)}</select></label>
      <label>Usage<select aria-label="Filter usage" value={filters.usage} onChange={event => onFiltersChange({...filters, usage: event.target.value})}><option value="">All usage</option>{[...new Set(records.flatMap(record => record.capabilities))].sort().map(value => <option key={value} value={value}>{value}</option>)}</select></label>
      <label>Family<select aria-label="Filter family" value={filters.family} onChange={event => onFiltersChange({...filters, family: event.target.value})}><option value="">All families</option>{[...new Set(records.map(recordFamily).filter(Boolean))].sort().map(value => <option key={value} value={value}>{value}</option>)}</select></label>
      <label>Version<select aria-label="Filter version" value={filters.version} onChange={event => onFiltersChange({...filters, version: event.target.value})}><option value="">All versions</option>{[...new Set(records.map(record => record.modelDocument?.identity.version).filter((value): value is string => Boolean(value)))].sort().map(value => <option key={value} value={value}>{value}</option>)}</select></label>
      <label>Quantization<select aria-label="Filter quantization" value={filters.quantization} onChange={event => onFiltersChange({...filters, quantization: event.target.value})}><option value="">All quantization</option>{[...new Set(records.map(record => record.modelDocument?.format.quantization).filter((value): value is string => Boolean(value)))].sort().map(value => <option key={value} value={value}>{value}</option>)}</select></label>
    </div>
    <div className="library-paired-list" aria-label="Model and recipe list">
      <div className="library-paired-heading"><span>Models · {models.length} of {new Set(records.map(record => record.modelKey)).size}</span><span>Recipes for selected Model · {selectedRecipes.length}</span></div>
      <div className="library-paired-panes">
        <div className="library-model-pane" aria-label="Models"><ul>{models.map(model => <li key={model.modelKey}><NavigateLink current={model.modelKey === selectedModelKey} href={modelLibraryPath(model.modelKey)} onNavigate={onNavigate}><span className={model.modelKey === selectedModelKey ? "is-selected" : undefined}><strong>{model.modelTitle}</strong><small>{model.model?.publisher}/{model.model?.slug}</small><small>{model.modelFiles.length} files · {formatBytes(model.modelBytes)}</small><em>{model.capabilities.join(" · ") || "Capabilities not declared"}</em></span></NavigateLink></li>)}</ul>{models.length === 0 && <p className="library-empty-state">No Models match these filters.</p>}</div>
        <div className="library-recipe-pane" aria-label="Recipes matching selected Model" ref={recipePaneRef} tabIndex={-1}>{selectedModel && <div className="library-selected-model-context"><strong>{selectedModel.modelTitle}</strong><span>{selectedModel.modelFiles.length} files · {formatBytes(selectedModel.modelBytes)} · {selectedModel.capabilities.join(" · ") || "Capabilities not declared"}</span></div>}<ul>{selectedRecipes.map(record => { const document = record.recipe!.recipe_document; const roleBytes = document.topology.roles.reduce((sum, role) => sum + role.count * role.resources.disk.image_bytes + role.count * role.resources.disk.artifact_bytes, 0); return <li key={record.key}><NavigateLink href={recipeLibraryPath(record.recipe!.recipe_id)} onNavigate={onNavigate}><strong>{record.title}</strong><small>{recipeAttribution(document)} · {document.runtime.engine} · release {document.release.version} · {document.topology.node_count} Spark{document.topology.node_count === 1 ? "" : "s"} · {document.topology.mode}</small><small>{document.topology.roles.map(role => `${role.name}: ${formatBytes(role.resources.memory.startup_peak_bytes)} startup / ${formatBytes(role.resources.memory.steady_state_bytes)} steady`).join(" · ")}</small><small>{formatBytes(roleBytes)} image + artifact envelope</small></NavigateLink><LibraryRecipeDownloadAction api={api} missingModels={missingModelsFor(record.recipe!)} onDownloaded={refresh} selector={canonicalRecipeSelector(record.recipe!)} /></li>; })}</ul>{selectedModel && selectedRecipes.length === 0 && <div className="library-empty-recipe"><strong>No Recipe linked</strong><span>This exact Model is available for cache management but has no runnable Recipe.</span></div>}{!selectedModel && <p className="library-empty-state">Select a Model to see matching Recipes.</p>}</div>
      </div>
    </div>
  </section>;
}

export function recordCapabilities(record: LibraryRecipeRecord): string[] { return record.capabilities; }
export function applyManagedCatalogWithdrawals(records: LibraryRecipeRecord[]): LibraryRecipeRecord[] { return records; }
