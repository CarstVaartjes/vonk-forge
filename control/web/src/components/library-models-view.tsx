import {useEffect, useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {ControlApi, LibraryModel, LibrarySnapshot, ModelCacheUpdateResponse, VisualFleetSnapshot} from "../api/types";
import {formatBytes, nodeDisplayName} from "../lib/fleet";
import {modelLibraryPath, modelVersionKey} from "../lib/library-route";
import type {LibraryRecipeRecord, LibraryWorkcellFilters} from "./library-workcell";
import {filterLibraryRecipeRecords} from "./library-workcell";
import {aggregateCacheEntries, LibraryModelDownloadAction, loadModelCacheInventory} from "./library-cache-view";
import {LibraryModelDeletionDialog} from "./library-model-deletion-dialog";

export function LibraryModelsView({api, entries, filters, fleet, modelInventory, onBusyChange, onFiltersChange, onNavigate, onNavigatePath, onQueryChange, onRefresh, path, query}: {api: ControlApi; entries: LibraryRecipeRecord[]; fleet?: VisualFleetSnapshot; filters: LibraryWorkcellFilters; modelInventory?: LibrarySnapshot["models"]; onBusyChange?(busy: boolean): void; onFiltersChange(filters: LibraryWorkcellFilters): void; onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void; onNavigatePath?(path: string, replace?: boolean): void; onQueryChange(value: string): void; onRefresh?: (signal: AbortSignal) => Promise<void>; path: string; query: string}) {
  const [deleteModel, setDeleteModel] = useState<LibraryModel>();
  const [cacheInventory, setCacheInventory] = useState<{entries: import("../api/types").CacheEntryResponse[]}>();
  const [cacheAttempt, setCacheAttempt] = useState(0);
  const [cacheLoading, setCacheLoading] = useState(true);
  const [cacheError, setCacheError] = useState("");
  const [updates, setUpdates] = useState<ModelCacheUpdateResponse[]>([]);
  const [updatesError, setUpdatesError] = useState("");
  const [updatesAttempt, setUpdatesAttempt] = useState(0);
  const models = modelInventory ?? [];
  const filtered = filterLibraryRecipeRecords(entries, filters, query);
  const visible = models.filter(model => !filters.model || modelVersionKey(model.model) === filters.model).filter(model => !query.trim() || filtered.some(record => record.modelKey === modelVersionKey(model.model)));
  useEffect(() => { if (!api.modelCacheInventory) { setCacheLoading(false); setCacheError("NAS cache API unavailable"); return; } const controller = new AbortController(); setCacheLoading(true); setCacheError(""); void loadModelCacheInventory(api, controller.signal).then(value => { if (!controller.signal.aborted) setCacheInventory(value); }).catch(value => { if (!controller.signal.aborted) setCacheError(value instanceof Error ? value.message : "NAS cache inventory unavailable"); }).finally(() => { if (!controller.signal.aborted) setCacheLoading(false); }); return () => controller.abort(); }, [api, cacheAttempt]);
  useEffect(() => { if (!api.modelCacheUpdates) return; const controller = new AbortController(); setUpdatesError(""); void api.modelCacheUpdates(controller.signal).then(value => { if (!controller.signal.aborted) setUpdates(value.updates); }).catch(value => { if (!controller.signal.aborted) setUpdatesError(value instanceof Error ? value.message : "Model update discovery unavailable"); }); return () => controller.abort(); }, [api, updatesAttempt]);
  const cacheByModel = useMemo(() => new Map(aggregateCacheEntries(models, cacheInventory).map(entry => [entry.key, entry])), [cacheInventory, models]);
  const nodeNames = Object.fromEntries((fleet?.nodes ?? []).map(node => [node.id, nodeDisplayName(node)]));
  function updateModel(value: string) {
    onFiltersChange({...filters, model: value});
    if (onNavigatePath) { const url = new URL(path, location.origin); if (value) url.searchParams.set("model", value); else url.searchParams.delete("model"); onNavigatePath(`${url.pathname}${url.search}`, true); }
  }
  const titleFor = (model: LibraryModel) => model.model_document.identity.model.title || model.model_document.identity.family.title || `${model.model.publisher}/${model.model.slug}`;
  return <section className="library-models-view" aria-labelledby="library-models-heading">
    <header className="library-subview-heading"><div><h2 id="library-models-heading">Models</h2><p>Exact model manifests own their files, bytes, and capability evidence.</p></div><span>{visible.length} of {models.length} Models</span><button type="button" className="button secondary" onClick={() => setUpdatesAttempt(value => value + 1)}>Refresh updates</button></header>{cacheError && <div className="library-error" role="alert"><span>NAS cache: {cacheError}</span><button type="button" className="button secondary" onClick={() => setCacheAttempt(value => value + 1)}>Retry cache</button></div>}{updatesError && <div className="library-error" role="alert"><span>Model updates: {updatesError}</span><button type="button" className="button secondary" onClick={() => setUpdatesAttempt(value => value + 1)}>Retry updates</button></div>}
    <div className="library-model-controls"><label>Search Models<input type="search" aria-label="Search Models" value={query} onChange={event => onQueryChange(event.target.value)} placeholder="Search model title or capability"/></label><label>Exact Model<select aria-label="Filter exact model" value={filters.model} onChange={event => updateModel(event.target.value)}><option value="">All Models</option>{models.map(model => <option key={modelVersionKey(model.model)} value={modelVersionKey(model.model)}>{titleFor(model)}</option>)}</select></label></div>
    <div className="library-model-list" aria-label="Exact model inventory">
      {visible.map(model => {
        const key = modelVersionKey(model.model);
        const modelRecords = entries.filter(record => record.modelKey === key && record.recipe);
        const bytes = model.model_document.files.reduce((sum, file) => sum + file.size_bytes, 0);
        const caps = (model.model_capabilities?.facts ?? []).filter(fact => fact.support === "supported").map(fact => fact.capability);
        const cache = cacheByModel.get(model.model.content_sha256);
        const update = updates.find(candidate => candidate.model_version_sha256 === model.model.content_sha256);
        const candidates = update?.model_update_candidates ?? [];
        const cacheLabel = cacheLoading ? "loading" : cacheError ? "unavailable" : cache?.status ?? "not cached";
        return <div key={key} className="library-model-row">
          <div><a href={modelLibraryPath(key)} onClick={event => onNavigate(event, modelLibraryPath(key))}><h3>{titleFor(model)}</h3><p>{model.model.publisher}/{model.model.slug} · {model.model_document.identity.variant}</p></a><div className="library-model-badges">{caps.length ? caps.map(capability => <span key={capability}>{capability}</span>) : <span>Capabilities unknown</span>}</div><span className={`library-cache-status state-${cacheLabel}`}>NAS cache: {cacheLabel}{!cacheLoading && !cacheError && ` · ${formatBytes(cache?.verifiedBytes ?? 0)} / ${formatBytes(bytes)} verified`}</span></div>
          {update?.model_update_ambiguous && <div className="library-model-update library-error" role="status"><strong>Model update needs a choice</strong><p>Multiple candidates share this exact Model lineage. Choose the intended publisher, slug, variant, and format before downloading; this row stays pinned to {model.model.content_sha256}.</p><ul>{candidates.map((candidate, index) => <li key={index}>{candidateLabel(candidate)}</li>)}</ul></div>}
          {update?.model_update_available && !update.model_update_ambiguous && <p className="library-model-update" role="status"><strong>Model update available.</strong> Review the exact candidate before changing this pinned Model.</p>}
          <dl><div><dt>Files</dt><dd>{model.model_document.files.length}</dd></div><div><dt>Bytes</dt><dd>{formatBytes(bytes)}</dd></div><div><dt>Recipes</dt><dd>{modelRecords.length || "No Recipe"}</dd></div></dl>
          <LibraryModelDownloadAction api={api} model={model} modelAccessUrl={model.model_document.provenance.source_url} onComplete={() => setCacheAttempt(value => value + 1)}/>
          <button type="button" className="button secondary" onClick={() => setDeleteModel(model)}>Review Model removal</button>
        </div>;
      })}
      {visible.length === 0 && <p className="library-empty-state">No Models match the current filters.</p>}
    </div>
    {deleteModel && <LibraryModelDeletionDialog api={api} modelTitle={titleFor(deleteModel)} modelVersionSha256={deleteModel.model.content_sha256} nodeNames={nodeNames} onBusyChange={onBusyChange} onClose={() => setDeleteModel(undefined)} onRefresh={onRefresh ?? (async () => undefined)}/>}</section>
}

function candidateLabel(candidate: Record<string, unknown>): string {
  const text = (key: string) => typeof candidate[key] === "string" ? candidate[key] as string : undefined;
  const digest = text("model_version_sha256") ?? text("content_sha256");
  const lineage = [text("publisher"), text("slug"), text("variant"), text("format")].filter(Boolean).join("/");
  return [lineage || "Exact candidate", digest ? `sha256:${digest}` : "identity pending"].join(" · ");
}
