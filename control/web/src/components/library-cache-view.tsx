import {useEffect, useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {
  CacheArtifactResponse,
  CacheEntryResponse,
  ControlApi,
  ModelCacheDownloadInput,
  ModelCacheDownloadPreviewResponse,
  ModelCacheEvictInput,
  ModelCacheEvictionPreviewResponse,
  ModelCacheInventoryResponse,
  ModelCacheOperationResponse,
  ModelCacheRepairInput,
  ModelCacheRepairPreviewResponse,
  ModelCacheState,
  VisualFleetNode,
  VisualFleetSnapshot,
} from "../api/types";
import {formatBytes, nodeDisplayName} from "../lib/fleet";
import type {LibraryRecipeRecord} from "./library-workcell";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
type CacheState = ModelCacheState | "unknown";
type CacheTargetState = "running" | "staged" | "unavailable";

export type LibraryCacheEntry = {
  rowKey: string;
  artifact_set_sha256: string | null;
  model_version_sha256: string | null;
  recipe_revision_sha256: string | null;
  model_title: string;
  variant_title: string;
  recipe_ids: string[];
  recipe_titles: string[];
  status: CacheState;
  coverage: "complete" | "incomplete" | "unknown";
  expected_bytes: number | null;
  verified_bytes: number | null;
  unique_bytes: number | null;
  artifact_count: number | null;
  artifacts: CacheArtifactResponse[];
  protected: boolean;
  protected_reasons: string[];
  update_available: boolean;
  recipe_update_available: boolean;
  verified_at: string | null;
  last_error: string | null;
  target_states: Array<{node_id: string; label: string; state: CacheTargetState; detail: string}>;
  note?: string;
};

type CacheFilter = "all" | "attention" | "updates" | "referenced";
type CacheMutationAction = "download" | "repair" | "evict";
type CacheReview =
  | {action: "download"; plan: ModelCacheDownloadPreviewResponse}
  | {action: "repair"; plan: ModelCacheRepairPreviewResponse}
  | {action: "evict"; plan: ModelCacheEvictionPreviewResponse};

const CACHE_FILTERS: Array<{label: string; value: CacheFilter}> = [
  {label: "All entries", value: "all"},
  {label: "Needs attention", value: "attention"},
  {label: "Updates available", value: "updates"},
  {label: "Referenced by Sparks", value: "referenced"},
];
const TERMINAL_OPERATION_STATES = new Set<ModelCacheOperationResponse["state"]>(["succeeded", "failed", "cancelled"]);

function statusLabel(status: CacheState): string {
  switch (status) {
    case "cached": return "Cached on Controller";
    case "downloading": return "Downloading to Controller";
    case "verifying": return "Verifying on Controller";
    case "incomplete": return "Incomplete on Controller";
    case "needs-repair": return "Needs repair";
    case "failed": return "Cache operation failed";
    case "unknown": return "Controller cache state unknown";
  }
}

function statusTone(status: CacheState): "healthy" | "warning" | "danger" | "neutral" {
  if (status === "cached") return "healthy";
  if (["needs-repair", "incomplete", "failed"].includes(status)) return "danger";
  if (["downloading", "verifying"].includes(status)) return "warning";
  return "neutral";
}

function entryMatches(entry: LibraryCacheEntry, filter: CacheFilter, query: string): boolean {
  const normalized = query.trim().toLocaleLowerCase();
  if (normalized && ![entry.model_title, entry.variant_title, ...entry.recipe_titles, entry.artifact_set_sha256 ?? ""]
    .join(" ").toLocaleLowerCase().includes(normalized)) return false;
  if (filter === "attention") return ["unknown", "incomplete", "needs-repair", "failed"].includes(entry.status);
  if (filter === "updates") return entry.update_available || entry.recipe_update_available;
  if (filter === "referenced") return entry.target_states.some(target => target.state !== "unavailable");
  return true;
}

function formatVerifiedAt(value: string | null): string {
  if (!value) return "Verification time not reported";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Verification time not reported" : `Verified ${date.toLocaleString()}`;
}

function exactRecipeMatches(entry: CacheEntryResponse, records: LibraryRecipeRecord[]): LibraryRecipeRecord[] {
  return records.filter(record => {
    const revision = record.recipe?.selected_revision;
    return Boolean(entry.recipe_revision_sha256 && (revision?.content_sha256 === entry.recipe_revision_sha256 || record.catalog?.content_sha256 === entry.recipe_revision_sha256));
  });
}

function targetStates(recipeIds: string[], fleet?: VisualFleetSnapshot): LibraryCacheEntry["target_states"] {
  if (!fleet) return [];
  return fleet.nodes.map((node: VisualFleetNode) => {
    const running = recipeIds.some(recipeId => (node.loaded ?? []).some(run => run.recipe_id === recipeId && run.healthy));
    const staged = recipeIds.some(recipeId => (node.installed ?? []).some(installation => installation.recipe_id === recipeId && installation.complete));
    return {
      node_id: node.id,
      label: nodeDisplayName(node),
      state: running ? "running" : staged ? "staged" : "unavailable",
      detail: running ? "Running from the observed Fleet state" : staged ? "Installed and complete in the observed Fleet state" : "Target artifact state not reported",
    };
  });
}

function entryFromResponse(response: CacheEntryResponse, records: LibraryRecipeRecord[], fleet?: VisualFleetSnapshot): LibraryCacheEntry {
  const matches = exactRecipeMatches(response, records);
  const recipeIds = matches.flatMap(record => record.recipe?.recipe_id ? [record.recipe.recipe_id] : []);
  const first = matches[0];
  return {
    rowKey: response.artifact_set_sha256,
    artifact_set_sha256: response.artifact_set_sha256,
    model_version_sha256: response.model_version_sha256,
    recipe_revision_sha256: response.recipe_revision_sha256,
    model_title: first?.modelTitle ?? "Model identity not reported",
    variant_title: first?.catalog?.model_version_title ?? first?.modelTitle ?? `Artifact set ${response.artifact_set_sha256.slice(0, 12)}…`,
    recipe_ids: recipeIds,
    recipe_titles: matches.map(record => record.title),
    status: response.state,
    coverage: response.coverage,
    expected_bytes: response.expected_bytes,
    verified_bytes: response.verified_bytes,
    unique_bytes: response.unique_bytes,
    artifact_count: response.artifacts.length,
    artifacts: response.artifacts,
    protected: response.protected,
    protected_reasons: response.protected_reasons,
    update_available: response.update_available,
    recipe_update_available: response.recipe_update_available,
    verified_at: response.verified_at,
    last_error: response.last_error ?? null,
    target_states: targetStates(recipeIds, fleet),
    note: response.last_error ?? undefined,
  };
}

function candidateFromRecord(record: LibraryRecipeRecord, fleet?: VisualFleetSnapshot): LibraryCacheEntry {
  const recipeId = record.recipe?.recipe_id;
  return {
    rowKey: `candidate:${record.key}`,
    artifact_set_sha256: null,
    model_version_sha256: null,
    recipe_revision_sha256: record.recipe?.selected_revision?.content_sha256 ?? record.catalog?.content_sha256 ?? null,
    model_title: record.modelTitle,
    variant_title: record.catalog?.model_version_title ?? record.modelTitle,
    recipe_ids: recipeId ? [recipeId] : [],
    recipe_titles: [record.title],
    status: "unknown",
    coverage: "unknown",
    expected_bytes: record.catalog?.expected_download_bytes ?? null,
    verified_bytes: null,
    unique_bytes: null,
    artifact_count: record.catalog?.artifact_count ?? null,
    artifacts: [],
    protected: false,
    protected_reasons: [],
    update_available: record.catalog?.local.status === "update-available",
    recipe_update_available: false,
    verified_at: null,
    last_error: null,
    target_states: targetStates(recipeId ? [recipeId] : [], fleet),
    note: "The Controller has not identified an exact artifact_set_sha256 for this recipe. Catalog metadata is not cache evidence.",
  };
}

function mergeInventory(inventory: ModelCacheInventoryResponse | undefined, records: LibraryRecipeRecord[], fleet?: VisualFleetSnapshot): LibraryCacheEntry[] {
  if (!inventory) return records.map(record => candidateFromRecord(record, fleet));
  const exact = inventory.entries.map(entry => entryFromResponse(entry, records, fleet));
  const representedRevisionPins = new Set(exact.map(entry => entry.recipe_revision_sha256).filter((value): value is string => Boolean(value)));
  const candidates = records
    .filter(record => {
      const pin = record.recipe?.selected_revision?.content_sha256 ?? record.catalog?.content_sha256;
      return !pin || !representedRevisionPins.has(pin);
    })
    .map(record => candidateFromRecord(record, fleet));
  return [...exact, ...candidates].sort((left, right) => left.variant_title.localeCompare(right.variant_title));
}

function actionFor(entry: LibraryCacheEntry): CacheMutationAction | undefined {
  if (!entry.artifact_set_sha256 && !entry.recipe_revision_sha256) return undefined;
  if (["incomplete", "needs-repair", "failed"].includes(entry.status)) return entry.artifact_set_sha256 ? "repair" : "download";
  if (entry.status === "cached" && (entry.update_available || entry.recipe_update_available)) return "download";
  if (entry.status === "cached") return "evict";
  if (entry.status === "unknown") return "download";
  return undefined;
}

function actionLabel(entry: LibraryCacheEntry, action: CacheMutationAction | undefined): string {
  if (!action) return "Controller action unavailable";
  if (action === "repair") return "Repair payload";
  if (action === "evict") return "Review eviction";
  return entry.update_available || entry.recipe_update_available ? "Update in Library" : "Download to Library";
}

function operationLabel(operation: ModelCacheOperationResponse): string {
  if (operation.state === "succeeded") return "Cache operation complete";
  if (operation.state === "failed" || operation.state === "cancelled") return "Cache operation needs attention";
  return `Cache ${operation.kind} in progress`;
}

function operationPhaseLabel(operation: ModelCacheOperationResponse): string {
  if (operation.state === "succeeded") return "Complete";
  if (operation.state === "failed" || operation.state === "cancelled") return "Needs attention";
  if (operation.kind === "download") {
    if (operation.progress.phase === "downloading") return "Downloading model";
    if (operation.progress.phase === "verifying") return "Verifying model";
  }
  if (operation.kind === "repair") {
    if (operation.progress.phase === "downloading") return "Repairing model cache";
    if (operation.progress.phase === "verifying") return "Verifying repaired model";
  }
  if (operation.kind === "evict" || operation.progress.phase === "reclaiming") return "Removing unreferenced cache";
  return operation.progress.phase.replaceAll("-", " ");
}

function reviewBlockers(review: CacheReview): string[] {
  if (review.action === "download") return review.plan.blockers;
  if (review.action === "evict") return review.plan.blockers;
  return [];
}

export function LibraryCacheView({api, entries: recordEntries, fleet, onBusyChange, onNavigate}: {
  api: ControlApi;
  entries: LibraryRecipeRecord[];
  fleet?: VisualFleetSnapshot;
  onBusyChange?(busy: boolean): void;
  onNavigate: Navigate;
}) {
  const cacheApi = api;
  const [inventory, setInventory] = useState<ModelCacheInventoryResponse>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<CacheFilter>("all");
  const [selected, setSelected] = useState<LibraryCacheEntry>();
  const [review, setReview] = useState<CacheReview>();
  const [operation, setOperation] = useState<ModelCacheOperationResponse>();
  const [operationError, setOperationError] = useState("");
  const [requestKey, setRequestKey] = useState<string>();
  const [requestScope, setRequestScope] = useState<{rowKey: string; action: CacheMutationAction}>();
  const [refreshAttempt, setRefreshAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void cacheApi.modelCacheInventory(undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) setInventory(value); })
      .catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message.slice(0, 256) : "The NAS cache inventory is unavailable."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, refreshAttempt]);

  const entries = useMemo(() => mergeInventory(inventory, recordEntries, fleet), [fleet, inventory, recordEntries]);
  const visibleEntries = useMemo(() => entries.filter(entry => entryMatches(entry, filter, query)), [entries, filter, query]);
  const cachedCount = entries.filter(entry => entry.status === "cached").length;
  const updateCount = entries.filter(entry => entry.update_available || entry.recipe_update_available).length;
  const referencedCount = entries.filter(entry => entry.target_states.some(target => target.state !== "unavailable") || entry.protected).length;
  const operationRunning = Boolean(operation && !TERMINAL_OPERATION_STATES.has(operation.state));

  useEffect(() => {
    onBusyChange?.(operationRunning);
    return () => onBusyChange?.(false);
  }, [onBusyChange, operationRunning]);

  useEffect(() => {
    if (!operation || TERMINAL_OPERATION_STATES.has(operation.state)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void cacheApi.modelCacheOperation(operation.id, controller.signal)
        .then(next => {
          if (controller.signal.aborted) return;
          setOperation(next);
          if (TERMINAL_OPERATION_STATES.has(next.state)) {
            if (next.state === "succeeded") {
              setRequestKey(undefined);
              setRequestScope(undefined);
            }
            setRefreshAttempt(value => value + 1);
          }
        })
        .catch(value => { if (!controller.signal.aborted) setOperationError(value instanceof Error ? value.message.slice(0, 256) : "Cache operation progress is unavailable."); });
    }, 1_000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [api, operation]);

  function openEntry(entry: LibraryCacheEntry) {
    setSelected(current => current?.rowKey === entry.rowKey ? undefined : entry);
    setReview(undefined);
    setOperationError("");
  }

  async function previewAction(entry: LibraryCacheEntry, action = actionFor(entry)) {
    if (!action || (!entry.artifact_set_sha256 && !entry.recipe_revision_sha256)) {
      setSelected(entry);
      setOperationError("The Controller has not reported an exact recipe or artifact identity for this entry. No cache mutation was assumed.");
      return;
    }
    setSelected(entry);
    if (!requestScope || requestScope.rowKey !== entry.rowKey || requestScope.action !== action) {
      setRequestKey(undefined);
      setRequestScope(undefined);
    }
    setReview(undefined);
    setOperationError("");
    try {
      if (action === "download") {
        const plan = await cacheApi.previewModelCacheDownload({schema_version: 2, source_policy: "nas-first", ...(entry.artifact_set_sha256 ? {artifact_set_sha256: entry.artifact_set_sha256} : {recipe_revision_sha256: entry.recipe_revision_sha256!})});
        const nextReview: CacheReview = {action, plan};
        if (plan.blockers.length > 0) setReview(nextReview);
        else await applyReview(nextReview, entry);
      } else if (action === "repair") {
        if (!entry.artifact_set_sha256) {
          setOperationError("The Controller must report an exact artifact set before repair can run.");
          return;
        }
        const plan = await cacheApi.previewModelCacheRepair({schema_version: 2, artifact_set_sha256: entry.artifact_set_sha256});
        await applyReview({action, plan}, entry);
      } else if (action === "evict") {
        if (!entry.artifact_set_sha256) {
          setOperationError("The Controller must report an exact artifact set before eviction can run.");
          return;
        }
        const targetBytes = Math.max(1, entry.unique_bytes ?? entry.verified_bytes ?? entry.expected_bytes ?? 1);
        const plan = await cacheApi.previewModelCacheEviction({schema_version: 2, target_bytes: targetBytes});
        setReview({action, plan});
      } else {
        setOperationError("This cache action is not exposed by the Controller.");
      }
    } catch (value) {
      setOperationError(value instanceof Error ? value.message.slice(0, 256) : "The NAS cache action could not be previewed.");
    }
  }

  async function applyReview(reviewToApply: CacheReview, selectedEntry: LibraryCacheEntry) {
    if (reviewBlockers(reviewToApply).length > 0) return;
    const key = requestScope?.rowKey === selectedEntry.rowKey && requestScope.action === reviewToApply.action && requestKey
      ? requestKey
      : crypto.randomUUID();
    setRequestKey(key);
    setRequestScope({rowKey: selectedEntry.rowKey, action: reviewToApply.action});
    setOperationError("");
    try {
      let next: ModelCacheOperationResponse;
      if (reviewToApply.action === "download") {
        const input: ModelCacheDownloadInput = {schema_version: 2, request_key: key, plan_digest: reviewToApply.plan.plan_digest, artifact_set_sha256: reviewToApply.plan.artifact_set_sha256, source_policy: "nas-first"};
        next = await cacheApi.downloadModelCache(input);
      } else if (reviewToApply.action === "repair" && selectedEntry.artifact_set_sha256) {
        const input: ModelCacheRepairInput = {schema_version: 2, request_key: key, plan_digest: reviewToApply.plan.plan_digest, artifact_set_sha256: selectedEntry.artifact_set_sha256, source_policy: "nas-first"};
        next = await cacheApi.repairModelCache(input);
      } else if (reviewToApply.action === "evict") {
        const input: ModelCacheEvictInput = {schema_version: 2, request_key: key, plan_digest: reviewToApply.plan.plan_digest, target_bytes: reviewToApply.plan.target_bytes};
        next = await cacheApi.evictModelCache(input);
      } else {
        setOperationError("This cache action is not exposed by the Controller.");
        return;
      }
      setOperation(next);
      setReview(undefined);
      if (next.state === "succeeded") {
        setRequestKey(undefined);
        setRequestScope(undefined);
      }
    } catch (value) {
      setOperationError(value instanceof Error ? value.message.slice(0, 256) : "The NAS cache action may have been accepted. Recheck before retrying; the same request key is retained.");
    }
  }

  async function applyAction() {
    if (!selected || !review) return;
    await applyReview(review, selected);
  }

  const closeReview = () => setReview(undefined);
  const operationExpectedBytes = operation?.progress.expected_bytes ?? null;
  const operationHasExpectedBytes = typeof operationExpectedBytes === "number" && operationExpectedBytes > 0;

  return <section className="library-cache-view" aria-labelledby="library-cache-heading">
    <header className="library-subview-heading">
      <div><h2 id="library-cache-heading">NAS cache</h2><p>Controller-owned model payloads are downloaded once, verified, and reused by the enrolled Sparks.</p></div>
      <a className="button secondary" href="/library?view=models" onClick={event => onNavigate(event, "/library?view=models")}>Choose a model</a>
    </header>
    {loading && <p className="library-cache-state" role="status">Reading Controller artifact coverage…</p>}
    {error && <div className="library-cache-state is-error" role="alert"><span>{error}</span><button type="button" className="button secondary" onClick={() => setRefreshAttempt(value => value + 1)}>Retry cache inventory</button></div>}
    <dl className="library-cache-summary" aria-label="NAS cache summary">
      <div><dt>Verified entries</dt><dd>{cachedCount}</dd><small>{`${cachedCount} complete Controller artifact set${cachedCount === 1 ? "" : "s"}`}</small></div>
      <div><dt>Referenced by Sparks</dt><dd>{referencedCount}</dd><small>Observed staged or running placements</small></div>
      <div><dt>Updates available</dt><dd>{updateCount}</dd><small>Model and recipe pins stay separate</small></div>
      <div><dt>Storage</dt><dd>{inventory ? formatBytes(inventory.storage.free_bytes) : "—"}</dd><small>{inventory ? `${formatBytes(inventory.storage.reclaimable_bytes)} reclaimable · ${formatBytes(inventory.storage.protected_bytes)} protected` : "Storage evidence unavailable"}</small></div>
    </dl>
    <div className="library-cache-controls">
      <label><span>Find an artifact set</span><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search model, variant, recipe, digest…"/></label>
      <div className="library-cache-filters" role="group" aria-label="Filter NAS cache">{CACHE_FILTERS.map(option => <button key={option.value} type="button" aria-pressed={filter === option.value} onClick={() => setFilter(option.value)}>{option.label}{option.value === "updates" && updateCount > 0 ? ` · ${updateCount}` : ""}</button>)}</div>
    </div>
    {operation && <section className={`library-cache-operation state-${operation.state}`} aria-live="polite"><div><strong>{operationLabel(operation)}</strong><span>{operationPhaseLabel(operation)} · {operation.progress.completed_artifacts} of {operation.progress.total_artifacts || "?"} artifacts · {formatBytes(operation.progress.downloaded_bytes)} / {operationHasExpectedBytes ? formatBytes(operationExpectedBytes) : "total unavailable"}</span></div>{operationHasExpectedBytes ? <div className="library-cache-operation-track" role="progressbar" aria-label="NAS cache bytes transferred" aria-valuemin={0} aria-valuemax={operationExpectedBytes} aria-valuenow={operation.progress.downloaded_bytes}><span style={{width: `${Math.min(100, operation.progress.downloaded_bytes / operationExpectedBytes * 100)}%`}}/></div> : operation.progress.total_artifacts > 0 ? <div className="library-cache-operation-track" role="progressbar" aria-label="NAS cache artifacts completed" aria-valuemin={0} aria-valuemax={operation.progress.total_artifacts} aria-valuenow={operation.progress.completed_artifacts}><span style={{width: `${Math.min(100, operation.progress.completed_artifacts / operation.progress.total_artifacts * 100)}%`}}/></div> : <div className="library-cache-operation-track is-indeterminate" role="progressbar" aria-label="NAS cache operation progress" aria-valuetext="Total bytes and artifact count unavailable"><span/></div>}{operation.last_error && <p>{operation.last_error}</p>}{requestKey && <details className="library-cache-operation-advanced"><summary>Operation details</summary><code>Request key {requestKey}</code>{operation.plan_digest && <code>Plan digest {operation.plan_digest}</code>}</details>}{TERMINAL_OPERATION_STATES.has(operation.state) && operation.state !== "succeeded" && <button type="button" className="button secondary" onClick={() => { setOperation(undefined); setOperationError(""); }}>Recheck before retrying</button>}</section>}
    {operationError && <div className="library-cache-state is-error" role="alert"><span>{operationError}</span><button type="button" className="button secondary" onClick={() => setOperationError("")}>Dismiss</button></div>}
    <section className="library-cache-list" aria-label="NAS artifact sets">
      <div className="library-cache-list-heading"><span>{visibleEntries.length} of {entries.length} artifact set{entries.length === 1 ? "" : "s"}</span><small>Controller cache, Spark staging, and running state are shown separately.</small></div>
      {visibleEntries.map(entry => {
        const action = actionFor(entry);
        const tone = statusTone(entry.status);
        return <article key={entry.rowKey} aria-label={`${entry.variant_title} cache entry`} className={`library-cache-row tone-${tone}${selected?.rowKey === entry.rowKey ? " is-selected" : ""}`}>
          <button type="button" className="library-cache-row-main" onClick={() => openEntry(entry)} aria-expanded={selected?.rowKey === entry.rowKey}>
            <span><strong>{entry.variant_title}</strong><small>{entry.model_title} · {entry.recipe_titles.length} recipe{entry.recipe_titles.length === 1 ? "" : "s"}</small></span>
            <span className="library-cache-state-label"><i aria-hidden="true"/>{statusLabel(entry.status)}{(entry.update_available || entry.recipe_update_available) && <small>New immutable revision available</small>}</span>
          </button>
          <div className="library-cache-row-facts"><span>{entry.unique_bytes !== null ? formatBytes(entry.unique_bytes) : entry.expected_bytes !== null ? `${formatBytes(entry.expected_bytes)} expected` : "Size unknown"}</span><small>{entry.artifact_count === null ? "Artifact count unknown" : `${entry.artifact_count} artifact${entry.artifact_count === 1 ? "" : "s"}`} · {entry.protected ? "Protected by Controller references" : "Protection not reported"}</small>{entry.coverage !== "unknown" && entry.expected_bytes && <div className="library-cache-progress" role="progressbar" aria-label={`${entry.variant_title} cache coverage`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round((entry.verified_bytes ?? 0) / entry.expected_bytes * 100)}><span style={{width: `${Math.max(0, Math.min(100, (entry.verified_bytes ?? 0) / entry.expected_bytes * 100))}%`}}/></div>}</div>
          {action ? <button type="button" className="button secondary" disabled={operationRunning} onClick={() => void previewAction(entry)}>{actionLabel(entry, action)}</button> : <span className="library-cache-action-unavailable">{entry.artifact_set_sha256 ? "Controller action unavailable" : "Exact cache identity unavailable"}</span>}
          {selected?.rowKey === entry.rowKey && <div className="library-cache-detail" role="region" aria-label={`${entry.variant_title} cache details`}>
            <div><strong>Controller cache details</strong><p>{entry.note ?? "The Controller supplied this cache state."}</p></div>
            <dl><div><dt>Artifact set</dt><dd><code>{entry.artifact_set_sha256 ? `sha256:${entry.artifact_set_sha256}` : "Not reported by Controller"}</code></dd></div><div><dt>Model pin</dt><dd><code>{entry.model_version_sha256 ? `sha256:${entry.model_version_sha256}` : "Not reported"}</code></dd></div><div><dt>Recipe pin</dt><dd><code>{entry.recipe_revision_sha256 ? `sha256:${entry.recipe_revision_sha256}` : "Not reported"}</code></dd></div><div><dt>Last verification</dt><dd>{formatVerifiedAt(entry.verified_at)}</dd></div><div><dt>Errors</dt><dd>{entry.last_error ?? "None reported"}</dd></div></dl>
            <div className="library-cache-targets"><strong>Per-Spark outcome</strong>{entry.target_states.length > 0 ? <ul>{entry.target_states.map(target => <li key={target.node_id} className={`target-${target.state}`}><span>{target.label}</span><small>{target.state === "running" ? "Running" : target.state === "staged" ? "Staged / installed" : "Target artifact state unavailable"} · {target.detail}</small></li>)}</ul> : <p>Fleet target state is unavailable.</p>}</div>
            {entry.artifacts.length > 0 && <details className="library-cache-artifacts"><summary>Show {entry.artifacts.length} verified payload file{entry.artifacts.length === 1 ? "" : "s"}</summary><ul>{entry.artifacts.map(artifact => <li key={artifact.sha256}><span>{artifact.path}</span><small>{artifact.state} · {formatBytes(artifact.actual_bytes)} / {formatBytes(artifact.expected_bytes)} · <code>{artifact.sha256.slice(0, 12)}…</code></small></li>)}</ul></details>}
            <div className="library-cache-detail-actions">{action ? <button type="button" className="button secondary" disabled={operationRunning} onClick={() => void previewAction(entry, action)}>{actionLabel(entry, action)}</button> : <span className="library-cache-action-unavailable">No mutation until the Controller reports an exact artifact set</span>}{entry.status === "cached" && <button type="button" className="button secondary" disabled={operationRunning} onClick={() => void previewAction(entry, "evict")}>Review eviction</button>}</div>
          </div>}
        </article>;
      })}
      {!loading && visibleEntries.length === 0 && <div className="library-cache-empty"><h3>No artifact sets match</h3><p>Change the search or filter. A missing Controller response is kept as unknown rather than treated as empty.</p>{(query || filter !== "all") && <button type="button" className="button secondary" onClick={() => { setQuery(""); setFilter("all"); }}>Clear cache filters</button>}</div>}
    </section>
    {review && selected && <section className="library-review-surface" aria-labelledby="cache-review-heading"><div><h3 id="cache-review-heading">Review {review.action === "repair" ? "cache repair" : review.action === "evict" ? "NAS eviction" : "download to NAS"}</h3><p>{selected.variant_title} · the Controller preview determines storage, verification, and protected references before any bytes change.</p></div><div className="library-review-facts"><span>{reviewBlockers(review).length > 0 ? "Blocked" : "Confirmation required"}</span><details><summary>Plan details</summary><code>{review.plan.plan_digest}</code></details></div>{reviewBlockers(review).length > 0 && <ul className="library-review-reasons">{reviewBlockers(review).map(reason => <li key={reason} className="reason-error"><strong>Controller blocker</strong><span>{reason}</span></li>)}</ul>}{review.action === "download" && review.plan.warnings.length > 0 && <ul className="library-review-reasons">{review.plan.warnings.map(reason => <li key={reason} className="reason-warning"><strong>Controller warning</strong><span>{reason}</span></li>)}</ul>}{review.action === "evict" && <div className="library-review-facts"><span>{formatBytes(review.plan.selected_bytes)} selected</span><span>{formatBytes(review.plan.reclaimable_bytes)} reclaimable</span><span>{review.plan.protected_entries.length} protected entries remain</span></div>}{review.action === "repair" && <div className="library-review-facts"><span>{review.plan.current_state.replaceAll("-", " ")}</span><span>{review.plan.artifact_count} artifacts</span><span>{formatBytes(review.plan.verified_bytes)} verified / {formatBytes(review.plan.expected_bytes)}</span></div>}<footer><button type="button" className="button secondary" onClick={closeReview}>Cancel</button><button type="button" className="button" disabled={reviewBlockers(review).length > 0 || operationRunning} onClick={() => void applyAction()}>{review.action === "evict" ? "Evict selected bytes" : review.action === "repair" ? "Repair exact payload" : "Download and verify"}</button></footer></section>}
  </section>;
}

export function cacheEntriesFromRecords(records: LibraryRecipeRecord[], fleet?: VisualFleetSnapshot): LibraryCacheEntry[] {
  return records.map(record => candidateFromRecord(record, fleet));
}

export function cacheEntrySparkNames(entry: LibraryCacheEntry): string[] {
  return entry.target_states.filter(target => target.state !== "unavailable").map(target => target.label);
}
