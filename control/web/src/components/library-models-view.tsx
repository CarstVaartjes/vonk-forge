import type {MouseEvent} from "react";
import type {ControlApi, LibraryViewModel, LibraryViewSnapshot} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {modelLibraryPath, modelKey} from "../lib/library-route";
import type {LibraryRecipeRecord, LibraryWorkcellFilters} from "./library-workcell";
import {filterLibraryRecipeRecords} from "./library-workcell";

type LibraryModelsViewProps = {
  api: ControlApi;
  entries: LibraryRecipeRecord[];
  fleet?: unknown;
  modelInventory?: LibraryViewSnapshot["models"];
  filters: LibraryWorkcellFilters;
  onFiltersChange(filters: LibraryWorkcellFilters): void;
  onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void;
  onNavigatePath?(path: string, replace?: boolean): void;
  onQueryChange(value: string): void;
  path: string;
  query: string;
};

/** The web library is read-only for now; vonkctl owns cache mutations. */
export function LibraryModelsView({entries, filters, modelInventory, onFiltersChange, onNavigate, onNavigatePath, onQueryChange, path, query}: LibraryModelsViewProps) {
  const models = modelInventory ?? [];
  const filteredRecipes = filterLibraryRecipeRecords(entries, filters, query);
  const normalizedQuery = query.trim().toLowerCase();
  const visible = models
    .filter(model => !filters.model || modelKey(model.model) === filters.model)
    .filter(model => !normalizedQuery || filteredRecipes.some(record => record.modelKey === modelKey(model.model)) || modelTitle(model).toLowerCase().includes(normalizedQuery));

  function updateModel(value: string) {
    onFiltersChange({...filters, model: value});
    if (!onNavigatePath) return;
    const url = new URL(path, location.origin);
    if (value) url.searchParams.set("model", value);
    else url.searchParams.delete("model");
    onNavigatePath(`${url.pathname}${url.search}`, true);
  }

  return <section className="library-models-view" aria-labelledby="library-models-heading">
    <header className="library-subview-heading">
      <div><h2 id="library-models-heading">Models</h2><p>Browse the verified model library and see the recipes that use each model.</p></div>
      <span>{visible.length} of {models.length} models</span>
    </header>
    <div className="library-model-controls">
      <label>Search models<input type="search" aria-label="Search models" value={query} onChange={event => onQueryChange(event.target.value)} placeholder="Search model title or capability" /></label>
      <label>Exact model<select aria-label="Filter exact model" value={filters.model} onChange={event => updateModel(event.target.value)}><option value="">All models</option>{models.map(model => <option key={modelKey(model.model)} value={modelKey(model.model)}>{modelTitle(model)}</option>)}</select></label>
    </div>
    <div className="library-model-list" aria-label="Model library">
      {visible.map(model => <ModelRow key={modelKey(model.model)} model={model} recipeCount={entries.filter(record => record.modelKey === modelKey(model.model) && record.recipe).length} onNavigate={onNavigate} />)}
      {visible.length === 0 && <p className="library-empty-state">No models match the current filters.</p>}
    </div>
  </section>;
}

function ModelRow({model, recipeCount, onNavigate}: {model: LibraryViewModel; recipeCount: number; onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void}) {
  const key = modelKey(model.model);
  const bytes = model.model_document.files.reduce((sum, file) => sum + file.size_bytes, 0);
  const capabilities = (model.model_capabilities?.facts ?? [])
    .filter(fact => fact.support === "supported")
    .map(fact => fact.capability);

  return <article className="library-model-row">
    <div>
      <a href={modelLibraryPath(key)} onClick={event => onNavigate(event, modelLibraryPath(key))}>
        <h3>{modelTitle(model)}</h3>
        <p>{model.model.publisher}/{model.model.slug} · {model.model_document.identity.variant}</p>
      </a>
      <div className="library-model-badges">{capabilities.length ? capabilities.map(capability => <span key={capability}>{capability}</span>) : <span>Capabilities unknown</span>}</div>
    </div>
    <dl><div><dt>Files</dt><dd>{model.model_document.files.length}</dd></div><div><dt>Size</dt><dd>{formatBytes(bytes)}</dd></div><div><dt>Recipes</dt><dd>{recipeCount || "None"}</dd></div></dl>
  </article>;
}

function modelTitle(model: LibraryViewModel): string {
  return model.model_document.identity.model.title || model.model_document.identity.family.title || `${model.model.publisher}/${model.model.slug}`;
}
