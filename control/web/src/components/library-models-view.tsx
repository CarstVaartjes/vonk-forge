import {useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {ControlApi, PublicRecipeCapability, VisualFleetSnapshot} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {modelLibraryPath} from "../lib/library-route";
import {humanizeIdentifier} from "./library-technical-details";
import type {LibraryRecipeRecord, LibraryWorkcellFilters} from "./library-workcell";
import {filterLibraryRecipeRecords} from "./library-workcell";

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
  const catalog = record.catalog;
  if (catalog) return `${catalog.model_publisher}/${catalog.model_slug}`;
  if (record.model) return `${record.model.publisher}/${record.model.slug}`;
  return record.modelTitle || "unlinked";
}

function modelFamilyTitle(record: LibraryRecipeRecord | undefined): string {
  // The catalog's model_title is the authoritative family label. Do not
  // infer a family by stripping digits or variant tokens from a display name:
  // version numbers are meaningful model identity (for example, Qwen 3).
  const title = record?.catalog?.model_title?.trim() || record?.modelTitle?.trim();
  if (title) return title;
  return record?.model ? humanizeIdentifier(`${record.model.publisher}/${record.model.slug}`) : "Unlinked model";
}

function variantKey(record: LibraryRecipeRecord): string {
  return record.catalog?.content_sha256
    ?? record.recipe?.selected_revision?.content_sha256
    ?? record.recipe?.selected_revision?.id
    ?? record.key;
}

function modelVersionTitle(record: LibraryRecipeRecord | undefined): string {
  const catalog = record?.catalog;
  if (catalog?.model_version_title) return catalog.model_version_title;
  if (record?.modelTitle) return record.modelTitle;
  return record?.title ?? "Model version not reported";
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
        const recipeCapabilities = [...new Set(grouped.flatMap(record => record.catalog?.capabilities ?? []))];
        const quantizations = [...new Set(grouped.flatMap(record => record.catalog?.quantizations ?? []))];
        const requiredSparks = [...new Set(grouped.map(recipeSparkCount).filter((value): value is number => value !== null))];
        return {
          key,
          title: first ? (first.catalog?.title ?? first.title) : key,
          modelTitle: first?.catalog?.model_title ?? first?.modelTitle ?? "Model metadata not reported",
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
          {version.variants.map(variant => <article className="library-model-row" key={variant.key}><div className="library-model-identity"><strong>{variant.title}</strong><small>{flat ? `${version.title} · ` : ""}{variant.modelTitle}{variant.quantizations.length ? ` · ${variant.quantizations.join(" · ")}` : ""}</small></div><div className="library-model-capabilities" aria-label="Model and recipe capabilities">{variant.recipeCapabilities.length > 0 && variant.recipeCapabilities.slice(0, 3).map(capability => <span key={capability}>Recipe: {capabilityText(capability)}</span>)}<details className="library-model-capability-unknown"><summary>Capabilities unavailable</summary><p>Model capability evidence is not declared. Recipe capabilities above come from the linked recipe record.</p></details></div><div className="library-model-cache-state"><span className="state-dot is-unknown" aria-hidden="true"/><details><summary>Cache status unavailable</summary><p>Controller cache inventory has not reported this exact artifact set.</p></details></div><div className="library-model-requirements"><strong>{variant.requiredSparks === null ? "Spark count unknown" : `${variant.requiredSparks} Spark${variant.requiredSparks === 1 ? "" : "s"}`}</strong><small>{variant.recipeCount} recipe{variant.recipeCount === 1 ? "" : "s"}{variant.expectedBytes !== null ? ` · ${formatBytes(variant.expectedBytes)} download` : ""}</small></div><div className="library-model-actions"><a className="button secondary" href={`/library/cache?model=${encodeURIComponent(variant.modelKey)}&artifact=${encodeURIComponent(variant.key)}`} onClick={event => onNavigate(event, `/library/cache?model=${encodeURIComponent(variant.modelKey)}&artifact=${encodeURIComponent(variant.key)}`)}>Download to Library</a><a className="text-link" href={versionHref(variant.modelKey)} onClick={event => onNavigate(event, versionHref(variant.modelKey))}>Recipes</a></div></article>)}
        </section>;
      })}</div>}
    </section>)}</div>}
  </section>;
}
