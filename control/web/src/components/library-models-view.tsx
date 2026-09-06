import {useEffect, useMemo, useRef, useState} from "react";
import type {MouseEvent} from "react";
import type {ControlApi, LibraryModel, ModelCacheDownloadInput, ModelCacheInventoryResponse, ModelCacheOperationResponse, ModelCacheState, PublicRecipeCapability, VisualFleetSnapshot} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {modelLibraryPath, modelVersionKey, recipeLibraryPath} from "../lib/library-route";
import {humanizeIdentifier} from "./library-technical-details";
import type {LibraryRecipeRecord, LibraryWorkcellFilters} from "./library-workcell";
import {filterLibraryRecipeRecords, recordCapabilities} from "./library-workcell";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;

const CAPABILITY_LABELS: Record<PublicRecipeCapability, string> = {
  audio: "Audio",
  chat: "Chat",
  "3d": "3D",
  "image-editing": "Image editing",
  "image-generation": "Image generation",
  reasoning: "Reasoning",
  video: "Video",
  vision: "Vision",
};
const TERMINAL_CACHE_OPERATION_STATES = new Set<ModelCacheOperationResponse["state"]>(["succeeded", "failed", "cancelled"]);

type ModelVariant = {
  key: string;
  title: string;
  modelTitle: string;
  modelCapabilities: string[];
  recipeCount: number;
  requiredSparks: number | null;
  recipeCapabilities: PublicRecipeCapability[];
  quantizations: string[];
  runtime: string | null;
  expectedBytes: number | null;
  records: LibraryRecipeRecord[];
  modelKey: string;
};

type ModelCacheSummary = {
  state: ModelCacheState | "unknown";
  coverage: "complete" | "incomplete" | "unknown";
  expectedBytes: number | null;
  verifiedBytes: number | null;
};

type CanonicalModelArtifact = NonNullable<NonNullable<LibraryModel["model_version"]>["artifacts"]>[number];

type ModelVersion = {
  key: string;
  title: string;
  variants: ModelVariant[];
  modelKey: string;
};

type ModelFamily = {
  key: string;
  title: string;
  versions: ModelVersion[];
};

function inventoryVariant(model: LibraryModel): ModelVariant {
  const facts = model.model_version;
  const format = facts?.format;
  const modelCapabilities = (model.model_capabilities?.facts ?? []).filter(fact => fact.support === "supported").map(fact => fact.capability);
  return {
    key: modelVersionKey(model.model),
    title: format?.quantization?.trim() || format?.precision?.trim() || "Exact model version",
    modelTitle: facts?.family?.metadata.title?.trim() || facts?.metadata?.title?.trim() || humanizeIdentifier(`${model.model.publisher}/${model.model.slug}`),
    modelCapabilities,
    recipeCount: model.recipes.length,
    requiredSparks: null,
    recipeCapabilities: [],
    quantizations: [format?.quantization, format?.precision].filter((value): value is string => Boolean(value?.trim())),
    runtime: null,
    expectedBytes: facts?.sizes?.download_bytes ?? null,
    records: [],
    modelKey: modelVersionKey(model.model),
  };
}

function mergeModelInventory(families: ModelFamily[], inventory: readonly LibraryModel[]): ModelFamily[] {
  const merged = families.map(family => ({...family, versions: family.versions.map(version => ({...version, variants: [...version.variants]}))}));
  for (const model of inventory) {
    const familyKey = model.model_version?.family?.identity ? modelVersionKey(model.model_version.family.identity) : `${model.model.publisher}/${model.model.slug}`;
    const familyTitle = model.model_version?.family?.metadata.title?.trim() || model.model_version?.metadata?.title?.trim() || humanizeIdentifier(`${model.model.publisher}/${model.model.slug}`);
    const versionKey = modelVersionKey(model.model);
    const variant = inventoryVariant(model);
    let family = merged.find(item => item.key === familyKey);
    if (!family) {
      family = {key: familyKey, title: familyTitle, versions: []};
      merged.push(family);
    }
    let version = family.versions.find(item => item.key === versionKey);
    if (!version) {
      version = {key: versionKey, title: model.model_version?.metadata?.title?.trim() || humanizeIdentifier(`${model.model.publisher}/${model.model.slug}`), modelKey: versionKey, variants: []};
      family.versions.push(version);
    }
    const existingVariant = version.variants.find(item => item.key === variant.key);
    if (!existingVariant) {
      version.variants.push(variant);
    } else {
      // The canonical Controller inventory owns model facts. Preserve the
      // recipe rows paired with this variant while replacing any copied facts
      // that came from a recipe projection.
      Object.assign(existingVariant, {
        modelCapabilities: variant.modelCapabilities,
        modelTitle: variant.modelTitle,
        expectedBytes: variant.expectedBytes,
        quantizations: variant.quantizations,
        title: variant.title,
      });
    }
  }
  return merged.map(family => ({...family, versions: family.versions.map(version => ({...version, variants: [...version.variants].sort((left, right) => left.title.localeCompare(right.title))})).sort((left, right) => left.title.localeCompare(right.title))})).sort((left, right) => left.title.localeCompare(right.title));
}

function modelFamilyKey(record: LibraryRecipeRecord): string {
  const familyIdentity = record.modelVersion?.family?.identity;
  if (familyIdentity) return modelVersionKey(familyIdentity);
  if (record.model) return `${record.model.publisher}/${record.model.slug}`;
  return "unresolved-model";
}

function modelFamilyTitle(record: LibraryRecipeRecord | undefined): string {
  const title = record?.modelVersion?.family?.metadata.title?.trim()
    || record?.modelVersion?.family?.family?.trim()
    || record?.catalog?.model_title?.trim()
    || record?.modelTitle?.trim();
  if (title) return title;
  return record?.model ? humanizeIdentifier(`${record.model.publisher}/${record.model.slug}`) : "Model linkage unavailable";
}

function variantKey(record: LibraryRecipeRecord): string {
  // A recipe revision is an execution contract. The Models view groups the
  // exact model payload by its content addressed identity, so two recipes
  // that point at the same model stay one model variant.
  return record.model ? modelVersionKey(record.model) : "unresolved-model";
}

function modelVersionTitle(record: LibraryRecipeRecord | undefined): string {
  const catalog = record?.catalog;
  const facts = record?.modelVersion;
  if (facts?.metadata?.title?.trim()) return facts.metadata.title;
  if (facts?.version?.trim()) return facts.version;
  if (catalog?.model_version_title?.trim()) return catalog.model_version_title;
  if (record?.model) return humanizeIdentifier(`${record.model.publisher}/${record.model.slug}`);
  return "Model version not reported";
}

function modelVariantTitle(record: LibraryRecipeRecord | undefined): string {
  const format = record?.modelVersion?.format;
  if (format?.quantization?.trim()) return format.quantization;
  if (format?.precision?.trim()) return format.precision;
  if (record?.catalog?.precision?.trim()) return record.catalog.precision;
  const quantization = record?.catalog?.quantizations.find(value => value.trim());
  return quantization ?? "Exact model version";
}

function firstRecord(versions: Map<string, Map<string, LibraryRecipeRecord[]>>): LibraryRecipeRecord | undefined {
  for (const variants of versions.values()) {
    for (const records of variants.values()) {
      if (records[0]) return records[0];
    }
  }
  return undefined;
}

function groupModels(records: LibraryRecipeRecord[]): ModelFamily[] {
  const families = new Map<string, Map<string, Map<string, LibraryRecipeRecord[]>>>();
  for (const record of records) {
    const family = modelFamilyKey(record);
    const version = record.modelKey || `${family}/unknown`;
    const variant = variantKey(record);
    const versions = families.get(family) ?? new Map<string, Map<string, LibraryRecipeRecord[]>>();
    const variants = versions.get(version) ?? new Map<string, LibraryRecipeRecord[]>();
    const group = variants.get(variant) ?? [];
    group.push(record);
    variants.set(variant, group);
    versions.set(version, variants);
    families.set(family, versions);
  }
  return [...families.entries()].map(([familyKey, versions]) => ({
    key: familyKey,
    title: modelFamilyTitle(firstRecord(versions)),
    versions: [...versions.entries()].map(([versionKey, variants]) => ({
      key: versionKey,
      title: modelVersionTitle(variants.values().next().value?.[0]),
      modelKey: variants.values().next().value?.[0]?.modelKey ?? versionKey,
      variants: [...variants.entries()].map(([key, grouped]) => {
        const first = grouped[0];
        const recipeCapabilities = [...new Set(grouped.flatMap(recordCapabilities))];
        const quantizations = [...new Set(grouped.flatMap(record => {
          const format = record.modelVersion?.format;
          return [format?.quantization, format?.precision, record.catalog?.precision, ...(record.catalog?.quantizations ?? [])].filter((value): value is string => Boolean(value?.trim()));
        }))];
        const modelCapabilities = [...new Set(grouped.flatMap(record => (record.modelCapabilities?.facts ?? []).filter(fact => fact.support === "supported").map(fact => fact.capability)))];
        const modelFacts = first?.modelVersion;
        const modelFormat = modelFacts?.format;
        return {
          key,
          title: modelVariantTitle(first),
          modelTitle: first?.catalog?.model_title ?? first?.modelTitle ?? "Model metadata not reported",
          modelCapabilities,
          recipeCount: grouped.length,
          // Spark count and runtime are properties of a selected recipe. Keep
          // those facts in the paired Recipes panel rather than projecting one
          // recipe's execution contract onto the canonical model row.
          requiredSparks: null,
          recipeCapabilities,
          quantizations: [modelFormat?.quantization, modelFormat?.precision].filter((value): value is string => Boolean(value?.trim())).length > 0
            ? [modelFormat?.quantization, modelFormat?.precision].filter((value): value is string => Boolean(value?.trim()))
            : quantizations,
          runtime: null,
          expectedBytes: modelFacts?.sizes?.download_bytes ?? null,
          records: grouped,
          modelKey: first?.modelKey ?? versionKey,
        } satisfies ModelVariant;
      }).sort((left, right) => left.title.localeCompare(right.title)),
    })).sort((left, right) => left.title.localeCompare(right.title)),
  })).sort((left, right) => left.title.localeCompare(right.title));
}

function canonicalArtifactKey(artifact: Pick<CanonicalModelArtifact, "sha256" | "path">): string {
  return artifact.sha256 || artifact.path;
}

function aggregateModelCache(model: LibraryModel, entries: ModelCacheInventoryResponse["entries"]): ModelCacheSummary | undefined {
  if (entries.length === 0) return undefined;
  const canonicalArtifacts = model.model_version?.artifacts ?? [];
  const canonicalBySha = new Map(canonicalArtifacts.map(artifact => [artifact.sha256, artifact]));
  const canonicalByPath = new Map(canonicalArtifacts.map(artifact => [artifact.path, artifact]));
  const verifiedBytesByArtifact = new Map<string, number>();
  for (const entry of entries) {
    for (const artifact of entry.artifacts) {
      if (artifact.state !== "verified") continue;
      const canonical = canonicalBySha.get(artifact.sha256) ?? canonicalByPath.get(artifact.path);
      if (!canonical) continue;
      const key = canonicalArtifactKey(canonical);
      const verifiedBytes = Math.min(artifact.actual_bytes, canonical.download_bytes);
      verifiedBytesByArtifact.set(key, Math.max(verifiedBytesByArtifact.get(key) ?? 0, verifiedBytes));
    }
  }
  const expectedBytes = model.model_version?.sizes?.download_bytes
    ?? (canonicalArtifacts.length > 0 ? canonicalArtifacts.reduce((total, artifact) => total + artifact.download_bytes, 0) : null);
  const verifiedBytes = verifiedBytesByArtifact.size > 0 ? [...verifiedBytesByArtifact.values()].reduce((total, value) => total + value, 0) : 0;
  const closureComplete = canonicalArtifacts.length > 0
    ? canonicalArtifacts.every(artifact => verifiedBytesByArtifact.has(canonicalArtifactKey(artifact))) && (expectedBytes === null || verifiedBytes >= expectedBytes)
    : false;
  const active = entries.find(entry => entry.state === "downloading" || entry.state === "verifying");
  const repair = entries.find(entry => entry.state === "needs-repair");
  const hasUsableEvidence = entries.some(entry => entry.state !== "failed");
  return {
    state: closureComplete ? "cached" : active?.state ?? repair?.state ?? (hasUsableEvidence ? "incomplete" : "unknown"),
    coverage: closureComplete ? "complete" : hasUsableEvidence ? "incomplete" : "unknown",
    expectedBytes,
    verifiedBytes,
  };
}

function modelCacheMap(inventory: ModelCacheInventoryResponse | undefined, modelInventory: readonly LibraryModel[]): Map<string, ModelCacheSummary> {
  const entriesByModel = new Map<string, ModelCacheInventoryResponse["entries"]>();
  for (const entry of inventory?.entries ?? []) {
    if (!entry.model_version_sha256) continue;
    const entries = entriesByModel.get(entry.model_version_sha256) ?? [];
    entries.push(entry);
    entriesByModel.set(entry.model_version_sha256, entries);
  }
  return new Map(modelInventory.flatMap(model => {
    const digest = model.model.content_sha256;
    const summary = aggregateModelCache(model, entriesByModel.get(digest) ?? []);
    return summary ? [[digest, summary] as const] : [];
  }));
}

function cacheStatusLabel(summary: ModelCacheSummary | undefined, loading: boolean, error: boolean): string {
  if (loading) return "Reading NAS cache…";
  if (error || !summary || summary.state === "failed") return "Cache coverage unknown";
  if (summary.state === "cached" && summary.coverage === "complete") return "Cache 100% verified";
  if (summary.state === "cached" && summary.coverage === "incomplete") return "Cache incomplete";
  if (summary.state === "needs-repair") return "Cache needs repair";
  if (summary.state === "downloading" || summary.state === "verifying" || summary.state === "incomplete") {
    const percent = summary.expectedBytes && summary.expectedBytes > 0 && summary.verifiedBytes !== null
      ? ` · ${Math.round(Math.min(100, summary.verifiedBytes / summary.expectedBytes * 100))}%`
      : "";
    return `${summary.state === "downloading" ? "Downloading" : summary.state === "verifying" ? "Verifying" : "Incomplete"}${percent}`;
  }
  return "Cache coverage unknown";
}

function downloadOperationLabel(operation: ModelCacheOperationResponse): string {
  if (operation.state === "succeeded") return "NAS download complete";
  if (operation.state === "failed" || operation.state === "cancelled") return "NAS download failed";
  const expected = operation.progress.expected_bytes;
  const percent = expected && expected > 0 ? ` · ${Math.round(Math.min(100, operation.progress.downloaded_bytes / expected * 100))}%` : "";
  if (operation.progress.phase === "verifying") return `Verifying NAS payload${percent}`;
  return `Downloading to NAS${percent}`;
}

function modelDigestForKey(modelKey: string): string {
  return modelKey.slice(modelKey.lastIndexOf("@") + 1);
}

function capabilityText(capability: PublicRecipeCapability): string {
  return CAPABILITY_LABELS[capability] ?? capability;
}

function versionHref(modelKey: string): string {
  return modelLibraryPath(modelKey);
}

export function LibraryModelsView({api, entries, fleet: _fleet, filters, modelInventory = [], onFiltersChange, onNavigate, onNavigatePath, onQueryChange, path, query}: {
  api: ControlApi;
  entries: LibraryRecipeRecord[];
  fleet?: VisualFleetSnapshot;
  filters: LibraryWorkcellFilters;
  modelInventory?: readonly LibraryModel[];
  onFiltersChange(filters: LibraryWorkcellFilters): void;
  onNavigate: Navigate;
  onNavigatePath?(path: string, replace?: boolean): void;
  onQueryChange(value: string): void;
  path?: string;
  query: string;
}) {
  const [expandedFamilies, setExpandedFamilies] = useState<Set<string>>();
  const [cacheInventory, setCacheInventory] = useState<ModelCacheInventoryResponse>();
  const [cacheLoading, setCacheLoading] = useState(true);
  const [cacheError, setCacheError] = useState(false);
  const [cacheRefreshAttempt, setCacheRefreshAttempt] = useState(0);
  const [downloadDigest, setDownloadDigest] = useState<string>();
  const [downloadOperation, setDownloadOperation] = useState<ModelCacheOperationResponse>();
  const [downloadError, setDownloadError] = useState("");
  useEffect(() => {
    const cacheApi = api as Partial<Pick<ControlApi, "modelCacheInventory">>;
    if (!cacheApi.modelCacheInventory) {
      setCacheLoading(false);
      setCacheError(true);
      return;
    }
    const controller = new AbortController();
    setCacheLoading(true);
    setCacheError(false);
    void cacheApi.modelCacheInventory(undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) setCacheInventory(value); })
      .catch(() => { if (!controller.signal.aborted) setCacheError(true); })
      .finally(() => { if (!controller.signal.aborted) setCacheLoading(false); });
    return () => controller.abort();
  }, [api, cacheRefreshAttempt]);
  useEffect(() => {
    if (!downloadOperation || TERMINAL_CACHE_OPERATION_STATES.has(downloadOperation.state)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void api.modelCacheOperation(downloadOperation.id, controller.signal)
        .then(next => {
          if (controller.signal.aborted) return;
          setDownloadOperation(next);
          if (TERMINAL_CACHE_OPERATION_STATES.has(next.state)) setCacheRefreshAttempt(value => value + 1);
        })
        .catch(value => { if (!controller.signal.aborted) setDownloadError(value instanceof Error ? value.message.slice(0, 256) : "NAS download progress is unavailable."); });
    }, 1_000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [api, downloadOperation]);
  const cacheByModel = useMemo(() => modelCacheMap(cacheInventory, modelInventory), [cacheInventory, modelInventory]);
  const modelEntries = useMemo(() => entries.filter(record => Boolean(record.model) && !record.modelLinkageError), [entries]);
  const matching = useMemo(() => filterLibraryRecipeRecords(modelEntries, filters, query), [modelEntries, filters, query]);
  const matchingInventory = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return modelInventory.filter(model => {
      const facts = model.model_version;
      const family = facts?.family?.identity ? modelVersionKey(facts.family.identity) : `${model.model.publisher}/${model.model.slug}`;
      const title = [facts?.family?.metadata.title, facts?.metadata?.title, model.model.publisher, model.model.slug, modelVersionKey(model.model)].filter(Boolean).join(" ").toLocaleLowerCase();
      if (normalized && !title.includes(normalized)) return false;
      if (filters.model && modelVersionKey(model.model) !== filters.model) return false;
      if (filters.modelFamily && family !== filters.modelFamily) return false;
      if (filters.capabilities.length > 0 && !filters.capabilities.every(capability => (model.model_capabilities?.facts ?? []).some(fact => fact.support === "supported" && fact.capability === capability))) return false;
      return true;
    });
  }, [filters.capabilities, filters.model, filters.modelFamily, modelInventory, query]);
  const families = useMemo(() => mergeModelInventory(groupModels(matching), matchingInventory), [matching, matchingInventory]);
  const allFamilies = useMemo(() => mergeModelInventory(groupModels(modelEntries), modelInventory), [modelEntries, modelInventory]);
  const expanded = expandedFamilies ?? new Set(allFamilies.slice(0, 2).map(family => family.key));
  const [selectedVariantKey, setSelectedVariantKey] = useState<string>(() => {
    const params = new URL(path ?? "/library?view=models", location.origin).searchParams;
    const model = params.get("model_selection");
    const variant = params.get("variant_selection");
    return model && variant ? `${model}:${variant}` : "";
  });
  const selectedVariant = useMemo(() => families.flatMap(family => family.versions.flatMap(version => version.variants)).find(variant => `${variant.modelKey}:${variant.key}` === selectedVariantKey), [families, selectedVariantKey]);
  const recipeSelectionRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!path) return;
    const params = new URL(path ?? "/library?view=models", location.origin).searchParams;
    const model = params.get("model_selection");
    const variant = params.get("variant_selection");
    const fromUrl = model && variant ? `${model}:${variant}` : "";
    if (fromUrl !== selectedVariantKey) setSelectedVariantKey(fromUrl);
  }, [path, selectedVariantKey]);
  useEffect(() => {
    if (selectedVariantKey && !selectedVariant) {
      setSelectedVariantKey("");
      if (path && onNavigatePath) {
        const next = new URL(path, location.origin);
        next.searchParams.delete("model_selection");
        next.searchParams.delete("variant_selection");
        onNavigatePath(`${next.pathname}${next.search}`, true);
      }
      return;
    }
    if (selectedVariant && typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(max-width: 1000px)").matches) {
      queueMicrotask(() => recipeSelectionRef.current?.scrollIntoView({block: "start"}));
    }
  }, [onNavigatePath, path, selectedVariant, selectedVariantKey]);

  function selectVariant(selectionKey: string) {
    setSelectedVariantKey(selectionKey);
    if (onNavigatePath) {
      const next = new URL(path ?? "/library?view=models", location.origin);
      if (!selectionKey) {
        next.searchParams.delete("model_selection");
        next.searchParams.delete("variant_selection");
        onNavigatePath(`${next.pathname}${next.search}`, true);
        return;
      }
      const separator = selectionKey.lastIndexOf(":");
      const model = selectionKey.slice(0, separator);
      const variant = selectionKey.slice(separator + 1);
      next.searchParams.set("model_selection", model);
      next.searchParams.set("variant_selection", variant);
      onNavigatePath(`${next.pathname}${next.search}`, true);
    }
  }
  const activeFilters = Object.entries(filters).reduce((count, [, value]) => count + (Array.isArray(value) ? value.length : value ? 1 : 0), 0);
  const unresolvedRecipeCount = entries.filter(record => record.modelLinkageError).length;

  function toggleFamily(key: string) {
    const next = new Set(expanded);
    if (next.has(key)) next.delete(key); else next.add(key);
    setExpandedFamilies(next);
  }

  function clearFilters() {
    onFiltersChange({
      abliterated: "", capabilities: [], installedOn: "", local: "", model: "", modelFamily: "", quantization: "",
      repository: "", runtime: "", sourceOwner: "", sparks: "", updated: "",
    });
    onQueryChange("");
    selectVariant("");
  }

  async function downloadModel(variant: ModelVariant) {
    const digest = modelDigestForKey(variant.modelKey);
    setDownloadDigest(digest);
    setDownloadOperation(undefined);
    setDownloadError("");
    try {
      const plan = await api.previewModelCacheDownload({schema_version: 2, model_version_sha256: digest, source_policy: "nas-first"});
      if (plan.blockers.length > 0) {
        setDownloadError(plan.blockers.join(" "));
        return;
      }
      const requestKey = crypto.randomUUID();
      const input: ModelCacheDownloadInput = {schema_version: 2, request_key: requestKey, plan_digest: plan.plan_digest, artifact_set_sha256: plan.artifact_set_sha256, source_policy: "nas-first"};
      setDownloadOperation(await api.downloadModelCache(input));
    } catch (value) {
      setDownloadError(value instanceof Error ? value.message.slice(0, 256) : "The NAS download could not be started.");
    }
  }

  function renderModelVariant(version: ModelVersion, flat: boolean, variant: ModelVariant) {
    const selectionKey = `${variant.modelKey}:${variant.key}`;
    const modelDigest = modelDigestForKey(variant.modelKey);
    const selectedDownload = downloadDigest === modelDigest ? downloadOperation : undefined;
    const selectedDownloadRunning = Boolean(selectedDownload && !TERMINAL_CACHE_OPERATION_STATES.has(selectedDownload.state));
    const cacheLabel = selectedDownload ? downloadOperationLabel(selectedDownload) : cacheStatusLabel(cacheByModel.get(modelDigest), cacheLoading, cacheError);
    return <article className={`library-model-row${selectedVariantKey === selectionKey ? " is-selected" : ""}`} key={variant.key} aria-label={`${variant.title} model version`}>
      <div className="library-model-primary">
        <button type="button" className="library-model-select" onClick={() => selectVariant(selectionKey)} aria-pressed={selectedVariantKey === selectionKey}>
          <strong>{variant.modelTitle}</strong>
          <small>{flat ? version.title : `${version.title} · ${variant.title}`}</small>
        </button>
        <div className="library-model-summary" aria-label="Model summary">
          {variant.modelCapabilities.slice(0, 3).map(capability => <span className="library-model-summary-capability" key={`model-${capability}`}>Model: {humanizeIdentifier(capability)}</span>)}
          {variant.recipeCapabilities.slice(0, 3).map(capability => <span className="library-model-summary-capability" key={`recipe-${capability}`}>Recipe: {capabilityText(capability)}</span>)}
          {variant.modelCapabilities.length === 0 && <span className="is-unknown">Capabilities unavailable</span>}
          <span>{variant.expectedBytes !== null ? `${formatBytes(variant.expectedBytes)} download` : "Download size unknown"}</span>
          <span className="library-model-cache-state" title={selectedDownload?.artifact_set_sha256 ? `Artifact set sha256:${selectedDownload.artifact_set_sha256}` : "Controller cache inventory for this exact model version"}>{cacheLabel}</span>
          {downloadDigest === modelDigest && downloadError && <span className="library-model-cache-state is-error" role="alert">{downloadError}</span>}
        </div>
      </div>
      <div className="library-model-actions">
        <button type="button" className="button secondary" disabled={selectedDownloadRunning} onClick={() => void downloadModel(variant)}>{selectedDownloadRunning ? "Downloading…" : "Download to NAS"}</button>
        <span className="library-model-select-hint">{selectedVariantKey === selectionKey ? "Selected" : "Select model"}</span>
      </div>
    </article>;
  }

  return <section className="library-models-view" aria-labelledby="library-models-heading">
    <header className="library-subview-heading">
      <div><h2 id="library-models-heading">Models</h2><p>Browse linked model versions and their recipe variants.</p></div>
    </header>
    <div className="library-models-toolbar">
      <label><span className="sr-only">Search models and recipes</span><input type="search" aria-label="Search models and recipes" value={query} onChange={event => onQueryChange(event.target.value)} placeholder="Search family, version, capability…"/></label>
      <div className="library-models-toolbar-actions"><span>{matching.length} linked recipe{matching.length === 1 ? "" : "s"}</span>{activeFilters > 0 && <button type="button" className="button secondary" onClick={clearFilters}>Clear filters</button>}</div>
    </div>
    {unresolvedRecipeCount > 0 && <p className="library-models-linkage-note is-error" role="status">{unresolvedRecipeCount} repository recipe{unresolvedRecipeCount === 1 ? " has" : "s have"} unresolved model linkage. Refresh Library and retry.</p>}
    {filters.capabilities.length > 0 && <div className="library-filter-chips" aria-label="Active model filters">{filters.capabilities.map(value => <button type="button" key={value} onClick={() => onFiltersChange({...filters, capabilities: filters.capabilities.filter(item => item !== value)})}>{capabilityText(value)} ×</button>)}</div>}
    {families.length === 0 && <div className="library-models-empty"><h3>No models match</h3><p>Change the search or filters. Cache coverage is shown separately in NAS cache.</p>{(query || activeFilters > 0) && <button type="button" className="button secondary" onClick={clearFilters}>Clear filters</button>}</div>}
    {families.length > 0 && <div className="library-model-selection-layout">
      <div className="library-model-family-list" aria-label="Model families">{families.map(family => <section className="library-model-family" key={family.key}>
      <button type="button" className="library-model-family-heading" aria-expanded={expanded.has(family.key)} onClick={() => toggleFamily(family.key)}><span><strong>{family.title}</strong>{!(family.versions.length === 1 && family.versions[0]!.variants.length === 1) && <small>{family.versions.length} version{family.versions.length === 1 ? "" : "s"} · {family.versions.reduce((count, version) => count + version.variants.length, 0)} exact variant{family.versions.reduce((count, version) => count + version.variants.length, 0) === 1 ? "" : "s"}</small>}</span><span>{expanded.has(family.key) ? "Collapse" : "Expand"}</span></button>
      {expanded.has(family.key) && <div className="library-model-version-list">{family.versions.map(version => {
        const flat = family.versions.length === 1 && version.variants.length === 1;
        return <section className={`library-model-version${flat ? " is-flat" : ""}`} key={version.key}>
          {!flat && <header><div><h3>{version.title}</h3><small>{version.variants.length} exact variant{version.variants.length === 1 ? "" : "s"}</small></div><a className="text-link" href={versionHref(version.modelKey)} onClick={event => onNavigate(event, versionHref(version.modelKey))}>Compare recipes</a></header>}
          {version.variants.map(variant => renderModelVariant(version, flat, variant))}
        </section>;
      })}</div>}</section>)}</div>
      <aside ref={recipeSelectionRef} className="library-model-recipe-selection" aria-label="Recipes matching selected model">
        <header><div><span>Installation selection</span><h3>Recipes</h3><p>{selectedVariant ? `${selectedVariant.modelTitle} · ${selectedVariant.title}` : "Select an exact model version to see compatible recipes."}</p></div>{selectedVariant && <button type="button" className="button secondary" onClick={() => selectVariant("")}>Clear model</button>}</header>
        {selectedVariant ? <ul>{selectedVariant.records.map(record => { const catalog = record.catalog; return <li key={record.key}>{record.recipe ? <a href={recipeLibraryPath(record.recipe.recipe_id)} onClick={event => onNavigate(event, recipeLibraryPath(record.recipe!.recipe_id))}><strong>{record.title}</strong><small>{catalog?.model_version_title ?? selectedVariant.title} · {catalog?.runtime_distribution ? humanizeIdentifier(catalog.runtime_distribution) : "Engine not reported"}{catalog?.execution_harness ? ` · ${humanizeIdentifier(catalog.execution_harness)}` : ""}</small><small>{catalog?.topology_name ?? "Topology not reported"} · {catalog?.node_count === undefined ? "Spark count unknown" : `${catalog.node_count} Spark${catalog.node_count === 1 ? "" : "s"}`} · {catalog?.quantizations.join(" · ") || "Format not reported"}</small><small>{catalog?.expected_download_bytes === undefined ? "Download size unknown" : `${formatBytes(catalog.expected_download_bytes)} download`} · {catalog?.maximum_runtime_memory_bytes_per_node === undefined ? "Runtime memory unknown" : `${formatBytes(catalog.maximum_runtime_memory_bytes_per_node)} / Spark`}</small><span>Select recipe to choose Sparks and run</span></a> : <span>{record.title}</span>}</li>; })}</ul> : <div className="library-model-recipe-empty"><strong>No model selected</strong><p>Choose a model or exact version in the list to continue.</p></div>}
        {selectedVariant && selectedVariant.records.length === 0 && <div className="library-model-recipe-empty"><strong>No matching recipes</strong><p>This exact model version has no repository recipe. Clear the model or adjust the filters.</p></div>}
      </aside>
    </div>}
  </section>;
}
