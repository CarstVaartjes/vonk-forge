import {useEffect, useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {CacheEntryResponse, ControlApi, LibraryModel, ModelCacheOperationResponse, VisualFleetSnapshot} from "../api/types";
import {formatBytes} from "../lib/fleet";
import type {LibraryRecipeRecord} from "./library-workcell";

export type LibraryCacheEntry = {key: string; model: LibraryModel; cache?: CacheEntryResponse; files: LibraryModel["model_document"]["files"]; recipeCount: number; status: string; expectedBytes: number; verifiedBytes: number; error?: string};

export function aggregateCacheEntries(models: readonly LibraryModel[], inventory?: {entries: CacheEntryResponse[]}): LibraryCacheEntry[] {
  return models.map(model => {
    const files = model.model_document.files;
    const candidates = (inventory?.entries ?? []).filter(entry => entry.model_version_sha256 === model.model.content_sha256);
    const artifacts = candidates.flatMap(entry => entry.artifacts);
    const verified = new Set(artifacts.filter(file => file.state === "verified").map(file => `${file.path}|${file.sha256}|${file.actual_bytes}`));
    const complete = files.every(file => verified.has(`${file.path}|${file.sha256}|${file.size_bytes}`));
    const expectedBytes = files.reduce((sum, file) => sum + file.size_bytes, 0);
    const verifiedBytes = files.filter(file => verified.has(`${file.path}|${file.sha256}|${file.size_bytes}`)).reduce((sum, file) => sum + file.size_bytes, 0);
    const cache = candidates.find(entry => entry.coverage === "complete" && complete) ?? candidates[0];
    return {key: model.model.content_sha256, model, cache, files, recipeCount: model.recipes.length, status: complete ? "cached" : cache?.state ?? "not cached", expectedBytes, verifiedBytes, error: candidates.find(entry => entry.last_error)?.last_error ?? undefined};
  });
}

function terminal(state: ModelCacheOperationResponse["state"]): boolean { return ["succeeded", "failed", "cancelled"].includes(state); }

export function LibraryCacheView({api, entries: _entries, modelInventory = [], onBusyChange, onNavigate, path: _path}: {api: ControlApi; entries?: LibraryRecipeRecord[]; modelInventory?: LibraryModel[]; fleet?: VisualFleetSnapshot; onBusyChange?(busy: boolean): void; onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void; path: string}) {
  const [inventory, setInventory] = useState<{entries: CacheEntryResponse[]} | undefined>();
  const [operation, setOperation] = useState<ModelCacheOperationResponse>();
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => { if (!api.modelCacheInventory) return; const controller = new AbortController(); void api.modelCacheInventory(undefined, controller.signal).then(value => { if (!controller.signal.aborted) setInventory(value); }).catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Unable to read NAS cache"); }); return () => controller.abort(); }, [api, attempt]);
  useEffect(() => { const busy = Boolean(operation && !terminal(operation.state)); onBusyChange?.(busy); return () => onBusyChange?.(false); }, [onBusyChange, operation]);
  useEffect(() => { if (!operation || terminal(operation.state) || !api.modelCacheOperation) return; const timer = window.setTimeout(() => void api.modelCacheOperation!(operation.id).then(setOperation).catch(() => undefined), 1500); return () => window.clearTimeout(timer); }, [api, operation]);
  const entries = useMemo(() => aggregateCacheEntries(modelInventory, inventory), [inventory, modelInventory]);
  const visible = entries.filter(entry => !query.trim() || `${entry.model.model_document.metadata.description} ${entry.model.model.slug}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  async function download(model: LibraryModel) {
    if (!api.previewModelCacheDownload || !api.downloadModelCache) return;
    setError("");
    try { const plan = await api.previewModelCacheDownload({schema_version: 2, source_policy: "nas-first", model_version_sha256: model.model.content_sha256}); if (plan.blockers.length) { setError(plan.blockers.join("; ")); return; } const next = await api.downloadModelCache({schema_version: 2, source_policy: "nas-first", model_version_sha256: model.model.content_sha256, artifact_set_sha256: plan.artifact_set_sha256, plan_digest: plan.plan_digest, request_key: crypto.randomUUID()}); setOperation(next); } catch (value) { setError(value instanceof Error ? value.message : "Download to NAS failed"); }
  }
  return <section className="library-cache-view" aria-labelledby="library-cache-heading"><header className="library-subview-heading"><div><h2 id="library-cache-heading">NAS cache</h2><p>Every Model file is tracked together. Download the complete manifest to NAS in one action.</p></div><a className="button secondary" href="/library?view=models" onClick={event => onNavigate(event, "/library?view=models")}>Choose a Model</a></header><label className="library-cache-search">Find a Model<input type="search" aria-label="Search NAS cache" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search exact model"/></label>{error && <div className="library-cache-state is-error" role="alert"><span>{error}</span><button type="button" className="button secondary" onClick={() => { setError(""); setAttempt(value => value + 1); }}>Retry cache</button></div>}{operation && <section className={`library-cache-operation state-${operation.state}`} role="status" aria-live="polite"><strong>{operation.state === "succeeded" ? "Downloaded to NAS" : operation.state === "failed" ? "Download failed" : "Downloading to NAS"}</strong><span>{operation.progress.completed_artifacts} of {operation.progress.total_artifacts || "?"} files · {formatBytes(operation.progress.downloaded_bytes)}{operation.last_error ? ` · ${operation.last_error}` : ""}</span>{operation.progress.total_artifacts > 0 && <div className="library-cache-operation-track" role="progressbar" aria-valuemin={0} aria-valuemax={operation.progress.total_artifacts} aria-valuenow={operation.progress.completed_artifacts}><span style={{width: `${Math.min(100, operation.progress.completed_artifacts / operation.progress.total_artifacts * 100)}%`}}/></div>}</section>}<div className="library-cache-list" aria-label="Complete model file cache">{visible.map(entry => <article className="library-cache-row" key={entry.key}><div><h3>{entry.model.model_document.identity.model.title}</h3><p>{entry.model.model_document.identity.version} · {entry.model.model_document.identity.variant} · {entry.model.model.publisher}/{entry.model.model.slug} · {entry.recipeCount ? `${entry.recipeCount} Recipe${entry.recipeCount === 1 ? "" : "s"}` : "No Recipe linked"}</p><span className={`library-cache-status state-${entry.status}`}>{entry.status}</span></div><dl><div><dt>Files</dt><dd>{entry.files.length}</dd></div><div><dt>Complete bytes</dt><dd>{formatBytes(entry.expectedBytes)}</dd></div><div><dt>Verified</dt><dd>{formatBytes(entry.verifiedBytes)}</dd></div></dl><button type="button" className="button" disabled={Boolean(operation && !terminal(operation.state)) || entry.status === "cached"} onClick={() => void download(entry.model)}>{entry.status === "cached" ? "Cached on NAS" : "Download to NAS"}</button><details><summary>Show all files</summary><ul>{entry.files.map(file => <li key={file.id}><span>{file.path}</span><small>{formatBytes(file.size_bytes)} · sha256:{file.sha256.slice(0, 12)}…</small></li>)}</ul></details></article>)}{visible.length === 0 && <p className="library-empty-state">No cached Models match.</p>}</div></section>;
}
