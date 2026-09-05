import {useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {ControlApi, PublicRecipeCapability, VisualFleetSnapshot} from "../api/types";
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

function modelFamilyKey(record: LibraryRecipeRecord): string {
  const familyIdentity = record.modelVersion?.family?.identity;
  if (familyIdentity) return modelVersionKey(familyIdentity);
  if (record.model) return `${record.model.publisher}/${record.model.slug}`;
  return "unlinked";
}

function modelFamilyTitle(record: LibraryRecipeRecord | undefined): string {
  const title = record?.modelVersion?.family?.metadata.title?.trim()
    || record?.catalog?.model_title?.trim()
    || record?.modelTitle?.trim();
  if (title) return title;
  return record?.model ? humanizeIdentifier(`${record.model.publisher}/${record.model.slug}`) : "Unlinked model";
}

function variantKey(record: LibraryRecipeRecord): string {
  // A recipe revision is an execution contract. The Models view groups the
  // exact model payload by its content addressed identity, so two recipes
  // that point at the same model stay one model variant.
  return record.model ? modelVersionKey(record.model) : "unlinked";
}

function modelVersionTitle(record: LibraryRecipeRecord | undefined): string {
  const catalog = record?.catalog;
  const facts = record?.modelVersion;
  if (catalog?.model_version_title?.trim()) return catalog.model_version_title;
  if (facts?.metadata?.title?.trim()) return facts.metadata.title;
  if (facts?.version?.trim()) return facts.version;
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

function recipeSparkCount(record: LibraryRecipeRecord): number | null {
  const value = record.catalog?.node_count;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const topology = record.recipe?.topology_name?.toLocaleLowerCase() ?? "";
  if (topology.includes("dual") || topology.includes("pair")) return 2;
  if (record.recipe) return 1;
  return null;
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
        const requiredSparks = [...new Set(grouped.map(recipeSparkCount).filter((value): value is number => value !== null))];
        return {
          key,
          title: modelVariantTitle(first),
          modelTitle: first?.catalog?.model_title ?? first?.modelTitle ?? "Model metadata not reported",
          modelCapabilities,
          recipeCount: grouped.length,
          requiredSparks: requiredSparks.length === 1 ? requiredSparks[0] : requiredSparks.length > 1 ? Math.max(...requiredSparks) : null,
          recipeCapabilities,
          quantizations,
          runtime: first?.catalog?.runtime_distribution ?? null,
          expectedBytes: first?.catalog?.expected_download_bytes ?? null,
          records: grouped,
          modelKey: first?.modelKey ?? versionKey,
        } satisfies ModelVariant;
      }).sort((left, right) => left.title.localeCompare(right.title)),
    })).sort((left, right) => left.title.localeCompare(right.title)),
  })).sort((left, right) => left.title.localeCompare(right.title));
}

function capabilityText(capability: PublicRecipeCapability): string {
  return CAPABILITY_LABELS[capability] ?? capability;
}

function versionHref(modelKey: string): string {
  return modelKey === "unlinked" ? "/library/models/~unlinked" : modelLibraryPath(modelKey);
}

export function LibraryModelsView({api: _api, entries, fleet: _fleet, filters, onFiltersChange, onNavigate, onQueryChange, query}: {
  api: ControlApi;
  entries: LibraryRecipeRecord[];
  fleet?: VisualFleetSnapshot;
  filters: LibraryWorkcellFilters;
  onFiltersChange(filters: LibraryWorkcellFilters): void;
  onNavigate: Navigate;
  onQueryChange(value: string): void;
  query: string;
}) {
  const [expandedFamilies, setExpandedFamilies] = useState<Set<string>>();
  const modelEntries = useMemo(() => entries.filter(record => record.modelKey !== "unlinked" && Boolean(record.model)), [entries]);
  const matching = useMemo(() => filterLibraryRecipeRecords(modelEntries, filters, query), [modelEntries, filters, query]);
  const families = useMemo(() => groupModels(matching), [matching]);
  const allFamilies = useMemo(() => groupModels(modelEntries), [modelEntries]);
  const expanded = expandedFamilies ?? new Set(allFamilies.slice(0, 2).map(family => family.key));
  const activeFilters = Object.entries(filters).reduce((count, [, value]) => count + (Array.isArray(value) ? value.length : value ? 1 : 0), 0);
  const hasDetachedRecipes = entries.some(record => record.modelKey === "unlinked" || !record.model);

  function toggleFamily(key: string) {
    const next = new Set(expanded);
    if (next.has(key)) next.delete(key); else next.add(key);
    setExpandedFamilies(next);
  }

  function clearFilters() {
    onFiltersChange({
      abliterated: "", capabilities: [], installedOn: "", local: "", model: "", modelFamily: "", qualification: "", quantization: "", readiness: "",
      repository: "", runtime: "", sourceOwner: "", sparks: "", updated: "",
    });
    onQueryChange("");
  }

  return <section className="library-models-view" aria-labelledby="library-models-heading">
    <header className="library-subview-heading">
      <div><h2 id="library-models-heading">Models</h2><p>Browse linked model versions and their recipe variants.</p></div>
    </header>
    <div className="library-models-toolbar">
      <label><span className="sr-only">Search models and recipes</span><input type="search" aria-label="Search models and recipes" value={query} onChange={event => onQueryChange(event.target.value)} placeholder="Search family, version, capability…"/></label>
      <div className="library-models-toolbar-actions"><span>{matching.length} linked recipe{matching.length === 1 ? "" : "s"}</span>{activeFilters > 0 && <button type="button" className="button secondary" onClick={clearFilters}>Clear filters</button>}</div>
    </div>
    {hasDetachedRecipes && <p className="library-models-detached-note">Unlinked and custom recipes live in <a href="/library" onClick={event => onNavigate(event, "/library")}>Recipes</a> until a model version is linked.</p>}
    {filters.capabilities.length > 0 && <div className="library-filter-chips" aria-label="Active model filters">{filters.capabilities.map(value => <button type="button" key={value} onClick={() => onFiltersChange({...filters, capabilities: filters.capabilities.filter(item => item !== value)})}>{capabilityText(value)} ×</button>)}</div>}
    {families.length === 0 && <div className="library-models-empty"><h3>No models match</h3><p>Change the search or filters. Cache coverage is shown separately in NAS cache.</p>{(query || activeFilters > 0) && <button type="button" className="button secondary" onClick={clearFilters}>Clear filters</button>}</div>}
    {families.length > 0 && <div className="library-model-family-list" aria-label="Model families">{families.map(family => <section className="library-model-family" key={family.key}>
      <button type="button" className="library-model-family-heading" aria-expanded={expanded.has(family.key)} onClick={() => toggleFamily(family.key)}><span><strong>{family.title}</strong>{!(family.versions.length === 1 && family.versions[0]!.variants.length === 1) && <small>{family.versions.length} version{family.versions.length === 1 ? "" : "s"} · {family.versions.reduce((count, version) => count + version.variants.length, 0)} exact variant{family.versions.reduce((count, version) => count + version.variants.length, 0) === 1 ? "" : "s"}</small>}</span><span>{expanded.has(family.key) ? "Collapse" : "Expand"}</span></button>
      {expanded.has(family.key) && <div className="library-model-version-list">{family.versions.map(version => {
        const flat = family.versions.length === 1 && version.variants.length === 1;
        return <section className={`library-model-version${flat ? " is-flat" : ""}`} key={version.key}>
          {!flat && <header><div><h3>{version.title}</h3><small>{version.variants.length} exact variant{version.variants.length === 1 ? "" : "s"}</small></div><a className="text-link" href={versionHref(version.modelKey)} onClick={event => onNavigate(event, versionHref(version.modelKey))}>Compare recipes</a></header>}
          {version.variants.map(variant => <article className="library-model-row" key={variant.key}><div className="library-model-identity"><strong>{variant.title}</strong><small>{flat ? `${version.title} · ` : ""}{variant.modelTitle}</small></div><div className="library-model-capabilities" aria-label="Model and recipe capabilities">{variant.modelCapabilities.slice(0, 3).map(capability => <span className="library-model-capability-model" key={`model-${capability}`}>Model: {humanizeIdentifier(capability)}</span>)}{variant.recipeCapabilities.slice(0, 3).map(capability => <span className="library-model-capability-recipe" key={`recipe-${capability}`}>Recipe: {capabilityText(capability)}</span>)}{variant.modelCapabilities.length === 0 && <details className="library-model-capability-unknown"><summary>Capabilities unavailable</summary><p>Model capability evidence is unavailable from the Controller. Recipe capabilities above describe the linked recipe interface.</p></details>}</div><div className="library-model-cache-state"><span className="state-dot is-unknown" aria-hidden="true"/><details><summary>Cache status unavailable</summary><p>Controller cache inventory has not reported this exact artifact set.</p></details></div><div className="library-model-requirements"><strong>{variant.requiredSparks === null ? "Spark count unknown" : `${variant.requiredSparks} Spark${variant.requiredSparks === 1 ? "" : "s"}`}</strong><small>{variant.expectedBytes !== null ? `${formatBytes(variant.expectedBytes)} download` : "Download size unknown"}</small></div><div className="library-model-actions"><a className="button secondary" href={`/library/cache?model=${encodeURIComponent(variant.modelKey)}&artifact=${encodeURIComponent(variant.key)}`} onClick={event => onNavigate(event, `/library/cache?model=${encodeURIComponent(variant.modelKey)}&artifact=${encodeURIComponent(variant.key)}`)}>Download to Library</a><details className="library-model-linked-recipes"><summary>Recipes · {variant.recipeCount}</summary><ul>{variant.records.map(record => <li key={record.key}>{record.recipe ? <a href={recipeLibraryPath(record.recipe.recipe_id)} onClick={event => onNavigate(event, recipeLibraryPath(record.recipe!.recipe_id))}>{record.title}</a> : <span>{record.title}</span>}</li>)}</ul></details></div></article>)}
        </section>;
      })}</div>}
    </section>)}</div>}
  </section>;
}
