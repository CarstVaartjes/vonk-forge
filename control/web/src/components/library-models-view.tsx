import type {MouseEvent} from "react";
import type {ControlApi, LibraryModel, LibrarySnapshot, VisualFleetSnapshot} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {modelLibraryPath, modelVersionKey, recipeLibraryPath} from "../lib/library-route";
import type {LibraryRecipeRecord, LibraryWorkcellFilters} from "./library-workcell";
import {filterLibraryRecipeRecords} from "./library-workcell";

export function LibraryModelsView({entries, filters, modelInventory, onFiltersChange, onNavigate, onNavigatePath, onQueryChange, path, query}: {api: ControlApi; entries: LibraryRecipeRecord[]; fleet?: VisualFleetSnapshot; filters: LibraryWorkcellFilters; modelInventory?: LibrarySnapshot["models"]; onFiltersChange(filters: LibraryWorkcellFilters): void; onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void; onNavigatePath?(path: string, replace?: boolean): void; onQueryChange(value: string): void; path: string; query: string}) {
  const models = modelInventory ?? [];
  const filtered = filterLibraryRecipeRecords(entries, filters, query);
  const visible = models.filter(model => !filters.model || modelVersionKey(model.model) === filters.model).filter(model => !query.trim() || filtered.some(record => record.modelKey === modelVersionKey(model.model)));
  function updateModel(value: string) {
    onFiltersChange({...filters, model: value});
    if (onNavigatePath) { const url = new URL(path, location.origin); if (value) url.searchParams.set("model", value); else url.searchParams.delete("model"); onNavigatePath(`${url.pathname}${url.search}`, true); }
  }
  const titleFor = (model: LibraryModel) => model.model_document.identity.model.title || model.model_document.identity.family.title || `${model.model.publisher}/${model.model.slug}`;
  return <section className="library-models-view" aria-labelledby="library-models-heading">
    <header className="library-subview-heading"><div><h2 id="library-models-heading">Models</h2><p>Exact model manifests own their files, bytes, and capability evidence.</p></div><span>{visible.length} of {models.length} Models</span></header>
    <div className="library-model-controls"><label>Search Models<input type="search" aria-label="Search Models" value={query} onChange={event => onQueryChange(event.target.value)} placeholder="Search model title or capability"/></label><label>Exact Model<select aria-label="Filter exact model" value={filters.model} onChange={event => updateModel(event.target.value)}><option value="">All Models</option>{models.map(model => <option key={modelVersionKey(model.model)} value={modelVersionKey(model.model)}>{titleFor(model)}</option>)}</select></label></div>
    <div className="library-model-list" aria-label="Exact model inventory">{visible.map(model => { const key = modelVersionKey(model.model); const modelRecords = entries.filter(record => record.modelKey === key && record.recipe); const bytes = model.model_document.files.reduce((sum, file) => sum + file.size_bytes, 0); const caps = (model.model_capabilities?.facts ?? []).filter(fact => fact.support === "supported").map(fact => fact.capability); return <div key={key} className="library-model-row"><div><a href={modelLibraryPath(key)} onClick={event => onNavigate(event, modelLibraryPath(key))}><h3>{titleFor(model)}</h3><p>{model.model.publisher}/{model.model.slug} · {model.model_document.identity.variant}</p></a><div className="library-model-badges">{caps.length ? caps.map(capability => <span key={capability}>{capability}</span>) : <span>Capabilities unknown</span>}</div></div><dl><div><dt>Files</dt><dd>{model.model_document.files.length}</dd></div><div><dt>Bytes</dt><dd>{formatBytes(bytes)}</dd></div><div><dt>Recipes</dt><dd>{modelRecords.length || "No Recipe"}</dd></div></dl></div>; })}{visible.length === 0 && <p className="library-empty-state">No Models match the current filters.</p>}</div>
  </section>;
}
