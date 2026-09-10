import type {MouseEvent} from "react";
import type {ControlApi, LibraryViewModel, LibraryViewSnapshot} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {modelLibraryPath, modelKey, recipeLibraryPath} from "../lib/library-route";
import {LibraryCacheAction} from "./library-cache-action";
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
  onRefresh(signal: AbortSignal): Promise<void>;
  path: string;
  query: string;
};

export function LibraryModelsView({api, entries, filters, modelInventory, onFiltersChange, onNavigate, onNavigatePath, onQueryChange, onRefresh, path, query}: LibraryModelsViewProps) {
  const models = modelInventory ?? [];
  const filteredRecipes = filterLibraryRecipeRecords(entries, filters, query);
  const normalizedQuery = query.trim().toLowerCase();
  const visible = models
    .filter(model => !filters.model || modelKey(model.model) === filters.model)
    .filter(model => !normalizedQuery || filteredRecipes.some(record => record.modelKey === modelKey(model.model)) || modelTitle(model).toLowerCase().includes(normalizedQuery));
  const refresh = () => void onRefresh(new AbortController().signal);

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
      <div><h2 id="library-models-heading">Models</h2><p>Browse the verified model library, prepare the cache, and open the recipes that use each model.</p></div>
      <span>{visible.length} of {models.length} models</span>
    </header>
    <div className="library-model-controls">
      <label>Search models<input type="search" aria-label="Search models" value={query} onChange={event => onQueryChange(event.target.value)} placeholder="Search model title or capability" /></label>
      <label>Exact model<select aria-label="Filter exact model" value={filters.model} onChange={event => updateModel(event.target.value)}><option value="">All models</option>{models.map(model => <option key={modelKey(model.model)} value={modelKey(model.model)}>{modelTitle(model)}</option>)}</select></label>
    </div>
    <div className="library-model-list" aria-label="Model library">
      {visible.map(model => <ModelRow key={modelKey(model.model)} api={api} model={model} onNavigate={onNavigate} onPrepared={refresh} />)}
      {visible.length === 0 && <p className="library-empty-state">No models match the current filters.</p>}
    </div>
  </section>;
}

function ModelRow({api, model, onNavigate, onPrepared}: {api: ControlApi; model: LibraryViewModel; onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void; onPrepared(): void}) {
  const key = modelKey(model.model);
  const bytes = model.model_document.files.reduce((sum, file) => sum + file.size_bytes, 0);
  const capabilities = (model.model_capabilities?.facts ?? [])
    .filter(fact => fact.support === "supported")
    .map(fact => fact.capability);
  const running = (model.local.running_on ?? []).length;
  const preparation = model.local.preparation;
  // A recipe may reference the same model in more than one selection with
  // different roles, so list each recipe once per model.
  const recipes = [...new Map(model.recipes.map(recipe => [recipe.recipe_revision_id, recipe])).values()];

  return <article className="library-model-row">
    <div>
      <a href={modelLibraryPath(key)} onClick={event => onNavigate(event, modelLibraryPath(key))}>
        <h3>{modelTitle(model)}</h3>
        <p>{model.model.publisher}/{model.model.slug} · {model.model_document.identity.variant}</p>
      </a>
      <div className="library-model-badges">{capabilities.length ? capabilities.map(capability => <span key={capability}>{capability}</span>) : <span>Capabilities unknown</span>}</div>
      <LibraryCacheAction api={api} onPrepared={onPrepared} selector={`${model.model.publisher}/${model.model.slug}`} state={model.local.controller} />
      {model.local.controller === "preparing" && preparation && <span role="status">{preparation.phase ?? preparation.state}</span>}
    </div>
    <dl>
      <div><dt>Files</dt><dd>{model.model_document.files.length}</dd></div>
      <div><dt>Size</dt><dd>{formatBytes(bytes)}</dd></div>
      <div><dt>Recipes</dt><dd>{recipes.length || "None"}</dd></div>
      {running > 0 && <div><dt>Running on</dt><dd>{running}</dd></div>}
    </dl>
    {recipes.length > 0 && <ul className="library-model-recipes" aria-label={`Recipes using ${modelTitle(model)}`}>
      {recipes.map(recipe => <li key={recipe.recipe_revision_id}>
        <a href={recipeLibraryPath(recipe.recipe_id)} onClick={event => onNavigate(event, recipeLibraryPath(recipe.recipe_id))}>{recipe.title}</a>
        <span> · {recipe.topology_name} · {recipe.recipe_document.topology.node_count} {recipe.recipe_document.topology.node_count === 1 ? "Spark" : "Sparks"}</span>
      </li>)}
    </ul>}
  </article>;
}

function modelTitle(model: LibraryViewModel): string {
  return model.model_document.identity.model.title || model.model_document.identity.family.title || `${model.model.publisher}/${model.model.slug}`;
}
