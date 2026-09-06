import {useEffect, useMemo, useRef, useState} from "react";
import type {MouseEvent} from "react";
import type {CacheEntryResponse, ControlApi, LibraryModel, ModelCacheOperationResponse, VisualFleetSnapshot} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {modelVersionKey} from "../lib/library-route";
import type {LibraryRecipeRecord} from "./library-workcell";
import {LibraryModelDeletionDialog} from "./library-model-deletion-dialog";
import {availabilityFailure, availabilityRetryable, LibraryAvailabilityFeedback} from "./library-availability-feedback";
import {availabilityProgress, LibraryAvailabilityProgress} from "./library-availability-progress";

export type LibraryCacheEntry = {key: string; model: LibraryModel; cache?: CacheEntryResponse; files: LibraryModel["model_document"]["files"]; recipeCount: number; status: string; expectedBytes: number; verifiedBytes: number; error?: string};

export async function loadModelCacheInventory(api: ControlApi, signal: AbortSignal): Promise<import("../api/types").ModelCacheInventoryResponse> {
  let cursor: string | undefined;
  let first: import("../api/types").ModelCacheInventoryResponse | undefined;
  const entries: CacheEntryResponse[] = [];
  const seen = new Set<string>();
  do {
    const page = await api.modelCacheInventory(cursor, signal);
    first ??= page;
    entries.push(...page.entries);
    cursor = page.next_cursor ?? undefined;
    if (cursor) { if (seen.has(cursor)) throw new Error("NAS cache pagination cursor repeated"); seen.add(cursor); }
  } while (cursor);
  if (!first) throw new Error("NAS cache returned no page");
  return {...first, entries, next_cursor: null, total: entries.length};
}

export function LibraryModelDownloadAction({api, model, modelAccessUrl, onComplete}: {api: ControlApi; model: LibraryModel; modelAccessUrl?: string; onComplete?(): void}) {
  const [operation, setOperation] = useState<ModelCacheOperationResponse>();
  const [error, setError] = useState("");
  const completedOperation = useRef<string | undefined>(undefined);
  const active = Boolean(operation && !terminal(operation.state));
  const retryable = operation?.state === "failed" && availabilityRetryable(operation);
  useEffect(() => { if (!operation) return; if (operation.state === "succeeded") { if (completedOperation.current !== operation.id) { completedOperation.current = operation.id; onComplete?.(); } return; } if (!active) return; const timer = window.setTimeout(() => void api.modelCacheOperation(operation.id).then(setOperation).catch(value => setError(value instanceof Error ? value.message : "Download progress unavailable")), 1200); return () => window.clearTimeout(timer); }, [active, api, onComplete, operation]);
  async function download(force = false) {
    setError("");
    try {
      if (operation && retryable && !force) {
        setOperation(await api.retryModelCacheOperation(operation.id, {schema_version: 2, request_key: crypto.randomUUID()}));
        return;
      }
      const plan = await api.previewModelCacheDownload({schema_version: 2, source_policy: "nas-first", model_version_sha256: model.model.content_sha256});
      if (plan.blockers.length) { setError(plan.blockers.join("; ")); return; }
      if (force) {
        const repairPlan = await api.previewModelCacheRepair({schema_version: 2, artifact_set_sha256: plan.artifact_set_sha256});
        setOperation(await api.repairModelCache({schema_version: 2, source_policy: "nas-first", artifact_set_sha256: repairPlan.artifact_set_sha256, plan_digest: repairPlan.plan_digest, request_key: crypto.randomUUID()}));
      } else {
        setOperation(await api.downloadModelCache({schema_version: 2, source_policy: "nas-first", model_version_sha256: model.model.content_sha256, artifact_set_sha256: plan.artifact_set_sha256, plan_digest: plan.plan_digest, request_key: crypto.randomUUID()}));
      }
    } catch (value) { setError(value instanceof Error ? value.message : "Download to NAS failed"); }
  }
  async function checkAccess() {
    if (!operation?.artifact_set_sha256 || !operation.plan_digest) {
      setError("The failed operation did not retain its exact cache identity; start a fresh download.");
      return;
    }
    setError("");
    try {
      setOperation(await api.checkModelCacheAccessAndResume(operation.id, {schema_version: 2, request_key: crypto.randomUUID(), artifact_set_sha256: operation.artifact_set_sha256, plan_digest: operation.plan_digest}));
    } catch (value) { setError(value instanceof Error ? value.message : "Model access check failed"); }
  }
  return <span className="library-model-download"><button type="button" className="button secondary" disabled={active} onClick={() => void download()}>{active ? "Downloading to NAS…" : operation?.state === "succeeded" ? "Available on NAS" : retryable ? "Retry download" : "Make available"}</button>{operation && <><small role="status">{operation.progress.completed_artifacts} of {operation.progress.total_artifacts || "?"} files · {formatBytes(operation.progress.downloaded_bytes)}</small><LibraryAvailabilityProgress progress={availabilityProgress(operation.progress)}/></>}{!active && <details><summary>More actions</summary><button type="button" className="button secondary" onClick={() => void download(true)}>Download again</button></details>}{error && <LibraryAvailabilityFeedback failure={availabilityFailure(error, "Download to NAS failed.")} onRetry={() => void download()} retryLabel="Retry download"/>}{operation?.state === "failed" && <LibraryAvailabilityFeedback failure={availabilityFailure(operation, "Download to NAS failed.")} modelAccessUrl={modelAccessUrl} onCheckAccessAndResume={() => void checkAccess()} onRetry={retryable ? () => void download() : undefined} retryLabel="Retry download"/>}</span>;
}

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
    const status = complete ? "cached" : candidates.length ? (cache?.state === "cached" ? "partial" : cache?.state ?? "partial") : "not cached";
    return {key: model.model.content_sha256, model, cache, files, recipeCount: model.recipes.length, status, expectedBytes, verifiedBytes, error: candidates.find(entry => entry.last_error)?.last_error ?? undefined};
  });
}

function terminal(state: ModelCacheOperationResponse["state"]): boolean { return ["succeeded", "failed", "cancelled"].includes(state); }

export function LibraryCacheView({api, entries: _entries, modelInventory = [], onBusyChange, onNavigate, path: _path}: {api: ControlApi; entries?: LibraryRecipeRecord[]; modelInventory?: LibraryModel[]; fleet?: VisualFleetSnapshot; onBusyChange?(busy: boolean): void; onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void; path: string}) {
  const [inventory, setInventory] = useState<{entries: CacheEntryResponse[]} | undefined>();
  const [operation, setOperation] = useState<ModelCacheOperationResponse>();
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [operationModelDigest, setOperationModelDigest] = useState<string>();
  useEffect(() => { if (!api.modelCacheInventory) return; const controller = new AbortController(); void loadModelCacheInventory(api, controller.signal).then(value => { if (!controller.signal.aborted) setInventory(value); }).catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "Unable to read NAS cache"); }); return () => controller.abort(); }, [api, attempt]);
  useEffect(() => { const busy = Boolean(operation && !terminal(operation.state)); onBusyChange?.(busy); return () => onBusyChange?.(false); }, [onBusyChange, operation]);
  useEffect(() => { if (!operation || terminal(operation.state) || !api.modelCacheOperation) return; const timer = window.setTimeout(() => void api.modelCacheOperation!(operation.id).then(next => { setOperation(next); if (terminal(next.state) && next.state === "succeeded") setAttempt(value => value + 1); }).catch(value => setError(value instanceof Error ? value.message : "Cache operation progress is unavailable")), 1500); return () => window.clearTimeout(timer); }, [api, operation]);
  const entries = useMemo(() => aggregateCacheEntries(modelInventory, inventory), [inventory, modelInventory]);
  const visible = entries.filter(entry => !query.trim() || `${entry.model.model_document.metadata.description} ${entry.model.model.slug}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const retryable = operation?.state === "failed" && availabilityRetryable(operation);
  async function download(model: LibraryModel) {
    if (!api.previewModelCacheDownload || !api.downloadModelCache) return;
    setError("");
    try {
      if (operation && retryable && operationModelDigest === model.model.content_sha256) {
        setOperation(await api.retryModelCacheOperation(operation.id, {schema_version: 2, request_key: crypto.randomUUID()}));
        return;
      }
      const plan = await api.previewModelCacheDownload({schema_version: 2, source_policy: "nas-first", model_version_sha256: model.model.content_sha256});
      if (plan.blockers.length) { setError(plan.blockers.join("; ")); return; }
      const next = await api.downloadModelCache({schema_version: 2, source_policy: "nas-first", model_version_sha256: model.model.content_sha256, artifact_set_sha256: plan.artifact_set_sha256, plan_digest: plan.plan_digest, request_key: crypto.randomUUID()});
      setOperationModelDigest(model.model.content_sha256);
      setOperation(next);
    } catch (value) { setError(value instanceof Error ? value.message : "Download to NAS failed"); }
  }
  return <section className="library-cache-view" aria-labelledby="library-cache-heading">
    <header className="library-subview-heading">
      <div><h2 id="library-cache-heading">NAS cache</h2><p>Every Model file is tracked together. Download the complete manifest to NAS in one action.</p></div>
      <a className="button secondary" href="/library?view=models" onClick={event => onNavigate(event, "/library?view=models")}>Choose a Model</a>
    </header>
    <label className="library-cache-search">Find a Model<input type="search" aria-label="Search NAS cache" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search exact model"/></label>
    {error && <div className="library-cache-state is-error" role="alert"><span>{error}</span><button type="button" className="button secondary" onClick={() => { setError(""); setAttempt(value => value + 1); }}>Retry cache</button></div>}
    {operation && <section className={`library-cache-operation state-${operation.state}`} role="status" aria-live="polite">
      <strong>{operation.state === "succeeded" ? "Downloaded to NAS" : operation.state === "failed" ? "Cache operation failed" : "Cache operation in progress"}</strong>
      <span>{operation.progress.completed_artifacts} of {operation.progress.total_artifacts || "?"} files · {formatBytes(operation.progress.downloaded_bytes)}{operation.failure?.detail ? ` · ${operation.failure.detail}` : ""}</span>
      {operation.progress.total_artifacts > 0 && <div className="library-cache-operation-track" role="progressbar" aria-valuemin={0} aria-valuemax={operation.progress.total_artifacts} aria-valuenow={operation.progress.completed_artifacts}><span style={{width: `${Math.min(100, operation.progress.completed_artifacts / operation.progress.total_artifacts * 100)}%`}}/></div>}
      {operation.state === "failed" && <LibraryAvailabilityFeedback failure={availabilityFailure(operation, "Cache operation failed.")} onRetry={retryable && operationModelDigest ? () => { const model = modelInventory.find(item => item.model.content_sha256 === operationModelDigest); if (model) void download(model); } : undefined} retryLabel="Retry cache operation"/>}
    </section>}
    <div className="library-cache-list" aria-label="Complete model file cache">
      {visible.map(entry => {
        const activeForEntry = operationModelDigest === entry.key && Boolean(operation && !terminal(operation.state));
        const retryForEntry = operationModelDigest === entry.key && retryable;
        return <article className="library-cache-row" key={entry.key}>
          <div><h3>{entry.model.model_document.identity.model.title}</h3><p>{entry.model.model_document.identity.version} · {entry.model.model_document.identity.variant} · {entry.model.model.publisher}/{entry.model.model.slug} · {entry.recipeCount ? `${entry.recipeCount} Recipe${entry.recipeCount === 1 ? "" : "s"}` : "No Recipe linked"}</p><span className={`library-cache-status state-${entry.status}`}>{entry.status}</span></div>
          <dl><div><dt>Files</dt><dd>{entry.files.length}</dd></div><div><dt>Complete bytes</dt><dd>{formatBytes(entry.expectedBytes)}</dd></div><div><dt>Verified</dt><dd>{formatBytes(entry.verifiedBytes)}</dd></div></dl>
          {activeForEntry && operation && <LibraryAvailabilityProgress progress={availabilityProgress(operation.progress)}/>}<span className="library-cache-actions"><button type="button" className="button" disabled={activeForEntry || entry.status === "cached"} onClick={() => void download(entry.model)}>{entry.status === "cached" ? "Cached on NAS" : retryForEntry ? "Retry cache operation" : activeForEntry ? "Downloading to NAS…" : "Download to NAS"}</button>{entry.status === "cached" && <a className="button secondary" href={`/library?view=models&model=${encodeURIComponent(modelVersionKey(entry.model.model))}`} onClick={event => onNavigate(event, `/library?view=models&model=${encodeURIComponent(modelVersionKey(entry.model.model))}`)}>Review Model removal in Models</a>}</span>
          <details><summary>Show all files</summary><ul>{entry.files.map(file => <li key={file.id}><span>{file.path}</span><small>{formatBytes(file.size_bytes)} · sha256:{file.sha256.slice(0, 12)}…</small></li>)}</ul></details>
        </article>;
      })}
      {visible.length === 0 && <p className="library-empty-state">No cached Models match.</p>}
    </div>
  </section>;
}
