import { NoReading } from "../ui/no-reading";
import {
  addWire,
  compareWire,
  formatWire,
  multiplyWire,
  type WireNumber,
} from "../../api/contract-numeric";
import { useEffect, useRef, useState } from "react";
import type { MouseEvent, ReactNode } from "react";
import type {
  ControlApi,
  LibrarySort,
  LibraryViewModel,
  LibraryViewRecipe,
  LibraryViewRecipeDetail,
  LibraryViewSnapshot,
  ModelDisplay,
  VisualFleetSnapshot,
  RecipeReadinessState,
} from "../../api/types";
import { LibraryPath, type LibraryRoute } from "../../lib/library-route";
import { modelKey, modelLibraryPath, recipeLibraryPath } from "../../lib/library-route";
import { formatBytes } from "../../lib/fleet";
import { EmptyState } from "../ui/empty-state";
import { LibraryRecipeActions } from "./library-recipe-actions";
import { useCapabilityWords } from "./library-vocabulary";
import {
  RecipeQualificationBadge,
  RecipeQualificationEvidence,
} from "./library-recipe-qualification";
import { LibraryRecipeUpdateAction } from "./library-recipe-update-action";
import { LibrarySetupStrip } from "./library-setup";

// Filter names mirror `vonkctl model library` and `vonkctl recipe library`
// one-for-one: --model --usage --family --version --quantization --publisher
// --alignment, plus --sparks for recipe topology size and --engine/--creator
// for the runtime engine and the upstream creator of a recipe. `fits` is
// --fits-fleet: the Controller's placement verdict against fresh fleet capacity.
// `publisher` is the MODEL publisher and is filtered on the Models tab. `sort` and `updated`
// mirror --sort and --updated-since; `updated` is the recency window that the
// request turns into a timestamp. Both ordering controls are applied by the
// projection to the whole library, so changing one restarts paging.
export type LibraryWorkcellFilters = {
  model: string;
  usage: string;
  family: string;
  version: string;
  quantization: string;
  publisher: string;
  alignment: string;
  sparks: string;
  engine: string;
  creator: string;
  cached: boolean;
  fits: boolean;
  sort: LibrarySort;
  updated: LibraryRecency;
};
export const EMPTY_LIBRARY_WORKCELL_FILTERS: LibraryWorkcellFilters = {
  model: "",
  usage: "",
  family: "",
  version: "",
  quantization: "",
  publisher: "",
  alignment: "",
  sparks: "",
  engine: "",
  creator: "",
  cached: false,
  fits: false,
  sort: "updated",
  updated: "any",
};

// The values here are checked against the generated `sort` union, so a server
// value rename or removal breaks the type gate instead of drifting silently.
export const LIBRARY_SORTS = ["updated", "name"] as const satisfies readonly LibrarySort[];
export const LIBRARY_RECENCY_VALUES = ["any", "24h", "7d", "31d"] as const;
export type LibraryRecency = (typeof LIBRARY_RECENCY_VALUES)[number];

const LIBRARY_RECENCY_HOURS: Record<LibraryRecency, number> = {
  any: 0,
  "24h": 24,
  "7d": 24 * 7,
  "31d": 24 * 31,
};
export const LIBRARY_RECENCY_LABELS: Record<LibraryRecency, string> = {
  any: "Any time",
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "31d": "Last 31 days",
};
export const LIBRARY_SORT_LABELS: Record<LibrarySort, string> = {
  updated: "Recently updated",
  name: "Name",
};

export function librarySortFromValue(value: string): LibrarySort {
  return LIBRARY_SORTS.find((candidate) => candidate === value) ?? "updated";
}
export function libraryRecencyFromValue(value: string): LibraryRecency {
  return LIBRARY_RECENCY_VALUES.find((candidate) => candidate === value) ?? "any";
}
export function libraryRecencySince(recency: LibraryRecency, now = new Date()): string | undefined {
  const hours = LIBRARY_RECENCY_HOURS[recency];
  return hours === 0 ? undefined : new Date(now.getTime() - hours * 3_600_000).toISOString();
}

const FILTER_PARAMS = [
  "model",
  "usage",
  "family",
  "version",
  "quantization",
  "publisher",
  "alignment",
  "sparks",
  "engine",
  "creator",
] as const;

export function libraryFiltersFromSearch(params: URLSearchParams): LibraryWorkcellFilters {
  return {
    model: params.get("model") ?? "",
    usage: params.get("usage") ?? "",
    family: params.get("family") ?? "",
    version: params.get("version") ?? "",
    quantization: params.get("quantization") ?? "",
    publisher: params.get("publisher") ?? "",
    alignment: params.get("alignment") ?? "",
    sparks: params.get("sparks") ?? "",
    engine: params.get("engine") ?? "",
    creator: params.get("creator") ?? "",
    cached: params.get("cached") === "1",
    fits: params.get("fits") === "1",
    sort: librarySortFromValue(params.get("sort") ?? ""),
    updated: libraryRecencyFromValue(params.get("updated") ?? ""),
  };
}
export function libraryFiltersToSearch(
  filters: LibraryWorkcellFilters,
  params = new URLSearchParams(),
): URLSearchParams {
  for (const name of FILTER_PARAMS) {
    if (filters[name]) params.set(name, filters[name]);
    else params.delete(name);
  }
  if (filters.cached) params.set("cached", "1");
  else params.delete("cached");
  if (filters.fits) params.set("fits", "1");
  else params.delete("fits");
  if (filters.sort === "updated") params.delete("sort");
  else params.set("sort", filters.sort);
  if (filters.updated === "any") params.delete("updated");
  else params.set("updated", filters.updated);
  return params;
}

export type LibraryRecipeRecord = {
  key: string;
  title: string;
  recipe?: LibraryViewRecipe;
  model?: LibraryViewModel["model"];
  modelDocument?: LibraryViewModel["model_document"];
  modelDisplay?: LibraryViewModel["model_display"];
  modelCapabilities?: LibraryViewModel["usage"];
  modelKey: string;
  modelTitle: string;
  modelFileCount: WireNumber;
  modelBytes: WireNumber | null;
  cacheUsage?: LibraryViewModel["cache_usage"];
  modelCached: boolean;
  capabilities: string[];
};

/** Cached, downloading, or running: the same test `vonkctl ... --cached` applies. */
export function isModelCached(model: LibraryViewModel): boolean {
  return (
    model.local.controller === "cached" ||
    model.local.controller === "preparing" ||
    (model.local.running_on ?? []).length > 0
  );
}

/** What the operator filtered by, in words, for an empty result. */
export function activeFilterSummary(filters: LibraryWorkcellFilters, query: string): string[] {
  const parts = FILTER_PARAMS.filter((name) => filters[name]).map(
    (name) => `${name}: ${filters[name]}`,
  );
  if (filters.cached) parts.unshift("cached only");
  if (filters.fits) parts.unshift("fits fleet");
  if (filters.updated !== "any")
    parts.push(`updated: ${LIBRARY_RECENCY_LABELS[filters.updated].toLocaleLowerCase()}`);
  if (query.trim()) parts.push(`search: ${query.trim()}`);
  return parts;
}

/** The Model an exact variant belongs to: its display lineage `family/line/model`.
 * Variants (quantizations, derivatives) fold into it. A model with no lineage
 * (a helper, drafter or encoder, or a display not yet synced) is not a Model and
 * has no key: it is left out of every Model list and count, as on the public
 * site. Every "N models" figure counts this key, never the exact variant. */
export function modelFoldKey(display: ModelDisplay | null | undefined): string | null {
  const lineage = display?.lineage;
  return lineage ? `${lineage.family}/${lineage.line}/${lineage.model}` : null;
}
/** Which variant stands for its Model: base first, then the shortest title, then the key. */
function representativeOrder(a: LibraryRecipeRecord, b: LibraryRecipeRecord): number {
  const base = (record: LibraryRecipeRecord) =>
    record.modelDisplay?.lineage?.variant_kind === "base" ? 0 : 1;
  return (
    base(a) - base(b) ||
    a.modelTitle.length - b.modelTitle.length ||
    (a.modelKey < b.modelKey ? -1 : a.modelKey > b.modelKey ? 1 : 0)
  );
}
export function recordModelKey(record: LibraryRecipeRecord): string | null {
  return modelFoldKey(record.modelDisplay);
}
/** Distinct Models among exact variants. */
export function countModels(models: LibraryViewModel[]): number {
  return new Set(models.flatMap((model) => modelFoldKey(model.model_display) ?? [])).size;
}
/** Distinct exact variants of the counted Models. */
export function countVariants(models: LibraryViewModel[]): number {
  return new Set(
    models.flatMap((model) => (modelFoldKey(model.model_display) ? [modelKey(model.model)] : [])),
  ).size;
}

function modelTitle(model: LibraryViewModel): string {
  return (
    model.model_display?.model_title ||
    model.model_display?.family_title ||
    `${model.model.publisher}/${model.model.slug}`
  );
}

function recipeRecordKey(modelKey: string, recipe: LibraryViewRecipe): string {
  return `${modelKey}:${recipe.recipe_id}:${recipe.publisher}/${recipe.slug}@${recipe.content_sha256}`;
}

/** The runtime engine, as the Controller projects it (same as `vonkctl --engine`). */
export function recipeEngine(recipe: LibraryViewRecipe): string {
  return recipe.engine ?? recipe.recipe_document.runtime.engine;
}

/** The upstream creator (same as `vonkctl --creator`), when one is known. */
export function recipeCreator(recipe: LibraryViewRecipe): string | undefined {
  return recipe.creator ?? undefined;
}

export function sparkLabel(count: WireNumber): string {
  return `${formatWire(count)} Spark${compareWire(count, 1) === 0 ? "" : "s"}`;
}

/** The Controller's fleet-fit verdict, or "unassessed" when the page carried none. */
export type RecipeFleetFit = RecipeReadinessState | "unassessed";
export function recipeFleetFit(recipe: LibraryViewRecipe): RecipeFleetFit {
  return recipe.assessment?.fleet_fit.state ?? "unassessed";
}
/** A proven fit ranks above unknown evidence, which ranks above a proven non-fit. */
export const FLEET_FIT_RANK: Record<RecipeFleetFit, 0 | 1 | 2> = {
  ready: 2,
  unavailable: 1,
  unassessed: 1,
  blocked: 0,
};
export function fleetFitRank(recipe: LibraryViewRecipe): 0 | 1 | 2 {
  return FLEET_FIT_RANK[recipeFleetFit(recipe)];
}
/** `--fits-fleet` keeps unknown evidence visible: only a proven non-fit is excluded. */
export function recipeMayFitFleet(recipe: LibraryViewRecipe): boolean {
  return fleetFitRank(recipe) > 0;
}
export const FLEET_FIT_WORDS: Record<RecipeFleetFit, string> = {
  ready: "fits fleet",
  blocked: "does not fit fleet",
  unavailable: "fit unknown",
  unassessed: "not assessed",
};

/** Engine, creator, Spark count, fleet fit and hardware status as compact badges. */
export function RecipeBadges({ recipe }: { recipe: LibraryViewRecipe }) {
  const creator = recipeCreator(recipe);
  const fit = recipeFleetFit(recipe);
  return (
    <span className="library-badges">
      <span className="library-badge" data-badge="engine" title="Engine">
        {recipeEngine(recipe)}
      </span>
      {creator && (
        <span className="library-badge" data-badge="creator" title="Creator">
          {creator}
        </span>
      )}
      <span className="library-badge" data-badge="sparks" title="Sparks">
        {sparkLabel(recipe.recipe_document.topology.node_count)}
      </span>
      {fit !== "unassessed" && (
        <span
          className="library-badge"
          data-badge="fit"
          data-fit={fit}
          title={
            recipe.assessment?.fleet_fit.reasons?.[0]?.detail ??
            "Placement against fresh fleet capacity"
          }
        >
          {FLEET_FIT_WORDS[fit]}
        </span>
      )}
      <RecipeQualificationBadge qualification={recipe.qualification} />
    </span>
  );
}

export function recipeAttribution(document: LibraryViewRecipe["recipe_document"]): string {
  const attribution = [
    ...new Set(document.provenance.attribution.map((value) => value.trim()).filter(Boolean)),
  ];
  return attribution.length > 0 ? `Credits: ${attribution.join(", ")}` : "Credits: not declared";
}

export function buildLibraryRecipeRecords(snapshot: LibraryViewSnapshot): LibraryRecipeRecord[] {
  return snapshot.models.flatMap((model) => {
    const key = modelKey(model.model);
    const title = modelTitle(model);
    const fileCount = model.model_file_count;
    const bytes = model.model_bytes;
    const capabilities = model.usage;
    const modelCached = isModelCached(model);
    if (model.recipes.length === 0)
      return [
        {
          key: `model:${key}`,
          title,
          model: model.model,
          modelDocument: model.model_document,
          modelDisplay: model.model_display,
          modelCapabilities: model.usage,
          modelKey: key,
          modelTitle: title,
          modelFileCount: fileCount,
          modelBytes: bytes,
          cacheUsage: model.cache_usage,
          modelCached,
          capabilities,
        },
      ];
    return model.recipes.map((recipe) => ({
      key: recipeRecordKey(key, recipe),
      title: recipe.title,
      recipe,
      model: model.model,
      modelDocument: model.model_document,
      modelDisplay: model.model_display,
      modelCapabilities: model.usage,
      modelKey: key,
      modelTitle: title,
      modelFileCount: fileCount,
      modelBytes: bytes,
      cacheUsage: model.cache_usage,
      modelCached,
      // The recipe's own capabilities: the Controller derives them from its API document.
      capabilities: recipe.capabilities,
    }));
  });
}

// The hierarchy is display only: the Family of the Model display's lineage.
export function recordFamily(record: LibraryRecipeRecord): string {
  return record.modelDisplay?.lineage?.family ?? "";
}

export function filterLibraryRecipeRecords(
  records: LibraryRecipeRecord[],
  filters: LibraryWorkcellFilters,
  query: string,
): LibraryRecipeRecord[] {
  const needle = query.trim().toLocaleLowerCase();
  return records.filter((record) => {
    const text = [
      record.title,
      record.modelTitle,
      record.recipe?.slug ?? "",
      ...record.capabilities,
    ]
      .join(" ")
      .toLocaleLowerCase();
    return (
      (!needle || text.includes(needle)) &&
      (!filters.cached || record.modelCached) &&
      (!filters.fits || (record.recipe !== undefined && recipeMayFitFleet(record.recipe))) &&
      (!filters.model || record.modelKey === filters.model) &&
      (!filters.usage || record.capabilities.includes(filters.usage)) &&
      (!filters.family || recordFamily(record) === filters.family) &&
      (!filters.version || record.modelDocument?.identity.version === filters.version) &&
      (!filters.quantization ||
        record.modelDocument?.format.quantization === filters.quantization) &&
      (!filters.publisher || record.model?.publisher === filters.publisher) &&
      (!filters.engine ||
        (record.recipe !== undefined &&
          recipeEngine(record.recipe).toLocaleLowerCase() ===
            filters.engine.toLocaleLowerCase())) &&
      (!filters.creator ||
        recipeCreator(record.recipe ?? ({} as LibraryViewRecipe))?.toLocaleLowerCase() ===
          filters.creator.toLocaleLowerCase()) &&
      (!filters.alignment || record.recipe?.recipe_display?.alignment === filters.alignment) &&
      // "N or fewer", like the Controller's `sparks` filter and the public site.
      (!filters.sparks ||
        Number(record.recipe?.recipe_document.topology.node_count ?? Infinity) <=
          Number(filters.sparks))
    );
  });
}

/** Says which filters emptied the list, or that the library itself is empty. */
export function EmptyLibrary({
  filters,
  query,
  onClear,
  onRefresh,
  noun = "models",
}: {
  filters: LibraryWorkcellFilters;
  query: string;
  onClear(): void;
  onRefresh?: (() => void) | undefined;
  noun?: string;
}) {
  const filtered = activeFilterSummary(filters, query).length > 0;
  return (
    <EmptyState
      title={`No ${noun}`}
      description={`${noun[0]!.toLocaleUpperCase()}${noun.slice(1)} come from the recipe library, which the Controller syncs automatically; its sync status is shown above.`}
      filtered={filtered}
      onClearFilters={onClear}
      action={onRefresh ? { label: "Refresh library", onClick: onRefresh } : undefined}
    />
  );
}

const SHOW_STEP = 40;
/** Bounds a long list: the first page, a "Show more" button, back to one page when `resetKey` changes. */
export function useShowMore(resetKey: string, total: number, atLeast = 0, noun = "rows") {
  const [pages, setPages] = useState(1);
  useEffect(() => setPages(1), [resetKey]);
  const limit = Math.max(pages * SHOW_STEP, atLeast);
  const more =
    total > limit ? (
      <button
        type="button"
        className="button secondary library-load-more"
        onClick={() => setPages(Math.ceil(limit / SHOW_STEP) + 1)}
      >
        Show more ({total - limit} more {noun})
      </button>
    ) : null;
  return { limit, more };
}

function NavigateLink({
  current,
  href,
  onNavigate,
  children,
}: {
  current?: boolean;
  href: string;
  onNavigate: (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
  children: ReactNode;
}) {
  return (
    <a
      href={href}
      aria-current={current ? "page" : undefined}
      onClick={(event) => onNavigate(event, href)}
    >
      {children}
    </a>
  );
}

export function LibraryWorkcell({
  api,
  detail: _detail,
  fleet,
  filters,
  onFiltersChange,
  onNavigate,
  onQueryChange,
  onRefresh,
  query,
  route,
  snapshot,
}: {
  api: ControlApi;
  detail?: LibraryViewRecipeDetail | undefined;
  detailError?: string;
  detailLoading?: boolean;
  fleet?: VisualFleetSnapshot | undefined;
  fleetError?: string;
  filters: LibraryWorkcellFilters;
  onFiltersChange(filters: LibraryWorkcellFilters): void;
  onNavigate: (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
  onQueryChange(value: string): void;
  onRefresh?: (signal: AbortSignal) => Promise<void>;
  onRetryDetail?: () => void;
  onRetryFleet?: () => void;
  query: string;
  route: LibraryRoute;
  snapshot: LibraryViewSnapshot;
  [key: string]: unknown;
}) {
  const words = useCapabilityWords();
  const records = buildLibraryRecipeRecords(snapshot);
  const matching = filterLibraryRecipeRecords(records, filters, query);
  // One entry per Model: variants fold into it (see `modelFoldKey`). The list,
  // its "x of y" count and the Spark/Any counts all use this one predicate. The
  // row stands for the Model, so its representative comes from the whole library
  // (the base variant when there is one), never from what a filter leaves.
  const requestedKey = route.kind === LibraryPath.MODELS ? route.modelKey : filters.model;
  const variantKeys = new Map<string, Set<string>>();
  const reps = new Map<string, LibraryRecipeRecord>();
  for (const record of records) {
    const key = recordModelKey(record);
    if (key === null) continue;
    variantKeys.set(key, (variantKeys.get(key) ?? new Set()).add(record.modelKey));
    const rep = reps.get(key);
    if (!rep || representativeOrder(record, rep) < 0) reps.set(key, record);
  }
  const shown = new Set(matching.flatMap((record) => recordModelKey(record) ?? []));
  const models = [...reps].filter(([key]) => shown.has(key)).map(([, record]) => record);
  const totalModels = reps.size;
  const requested = records.find((record) => record.modelKey === requestedKey);
  const requestedFold = requested ? recordModelKey(requested) : null;
  const selectedModel =
    models.find((record) => recordModelKey(record) === requestedFold) ??
    (requested ? undefined : models[0]);
  const selectedFold = selectedModel ? recordModelKey(selectedModel) : undefined;
  // A recipe is listed once, however many variants of the Model it serves.
  const selectedRecipes = [
    ...new Map(
      matching
        .filter((record) => recordModelKey(record) === selectedFold && record.recipe)
        .map((record) => [record.recipe!.recipe_id, record]),
    ).values(),
  ];
  const selectedIndex = models.findIndex((record) => recordModelKey(record) === selectedFold);
  const selectedVariants = selectedFold ? (variantKeys.get(selectedFold)?.size ?? 0) : 0;
  const { limit, more } = useShowMore(
    JSON.stringify([filters, query]),
    models.length,
    selectedIndex + 1,
    "models",
  );
  const recipePaneRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (
      (!filters.model && route.kind !== LibraryPath.MODELS) ||
      typeof window === "undefined" ||
      window.innerWidth > 760
    )
      return;
    if (recipePaneRef.current && typeof recipePaneRef.current.scrollIntoView === "function")
      recipePaneRef.current.scrollIntoView({ block: "start" });
    recipePaneRef.current?.focus({ preventScroll: true });
  }, [filters.model, route.kind, selectedFold]);
  // A recipe download also caches the model artifacts it needs, so name the
  // models that are not cached yet before the operator commits.
  const controllerByModel = new Map(
    snapshot.models.map((model) => [
      `${model.model.publisher}/${model.model.slug}`,
      model.local.controller,
    ]),
  );
  const missingModelsFor = (recipe: LibraryViewRecipe): string[] =>
    (recipe.model_selectors ?? []).filter(
      (selector) => controllerByModel.get(selector) !== "cached",
    );
  const refresh = () => void onRefresh?.(new AbortController().signal);
  return (
    <section className="library-workcell" aria-label="Models and recipes">
      <LibrarySetupStrip
        fleet={fleet}
        records={records}
        filters={filters}
        query={query}
        noun="recipes"
        onFiltersChange={onFiltersChange}
        onQueryChange={onQueryChange}
      />
      <div className="library-workcell-toolbar">
        <LibraryRecipeUpdateAction api={api} onUpdated={refresh} />
      </div>
      <div className="library-paired-list">
        <div className="library-paired-heading">
          <span>
            Models · {models.length} of {totalModels}
          </span>
          <span>Recipes for selected Model · {selectedRecipes.length}</span>
        </div>
        <div className="library-paired-panes">
          <div className="library-model-pane">
            <ul aria-label="Models">
              {models.slice(0, limit).map((model) => (
                <li key={recordModelKey(model)!}>
                  <NavigateLink
                    current={recordModelKey(model) === selectedFold}
                    href={modelLibraryPath(model.modelKey)}
                    onNavigate={onNavigate}
                  >
                    <span
                      className={recordModelKey(model) === selectedFold ? "is-selected" : undefined}
                    >
                      <strong>{model.modelTitle}</strong>
                      <small>
                        {(variantKeys.get(recordModelKey(model)!)?.size ?? 0) > 1
                          ? `${variantKeys.get(recordModelKey(model)!)!.size} variants`
                          : `${model.model?.publisher}/${model.model?.slug}`}
                      </small>
                      <em>
                        {(model.modelCapabilities ?? model.capabilities)
                          .map(words.label)
                          .join(" · ") || <NoReading />}
                      </em>
                    </span>
                  </NavigateLink>
                </li>
              ))}
            </ul>
            {more}
            {models.length === 0 && (
              <EmptyLibrary
                noun="recipes"
                filters={filters}
                query={query}
                onRefresh={refresh}
                onClear={() => {
                  onFiltersChange(EMPTY_LIBRARY_WORKCELL_FILTERS);
                  onQueryChange("");
                }}
              />
            )}
          </div>
          <section
            className="library-recipe-pane"
            aria-label="Recipes matching selected Model"
            ref={recipePaneRef}
            tabIndex={-1}
          >
            {selectedModel && (
              <div className="library-selected-model-context">
                <strong>{selectedModel.modelTitle}</strong>
                <span>
                  {selectedVariants <= 1 && (
                    <>
                      {formatWire(selectedModel.modelFileCount)} files ·{" "}
                      {formatBytes(selectedModel.modelBytes)} ·{" "}
                    </>
                  )}
                  {(selectedModel.modelCapabilities ?? selectedModel.capabilities)
                    .map(words.label)
                    .join(" · ") || <NoReading />}
                </span>
                {selectedVariants <= 1 && selectedModel.cacheUsage && (
                  <span>
                    {formatBytes(selectedModel.cacheUsage.unique_bytes)} unique ·{" "}
                    {formatBytes(selectedModel.cacheUsage.shared_bytes)} shared
                    {(selectedModel.cacheUsage.retained_revisions ?? []).map((revision) => (
                      <span key={revision.content_sha256}>
                        Previous revision retained because {revision.owners.join(", ")};{" "}
                        {formatBytes(revision.unique_bytes)} unique
                      </span>
                    ))}
                  </span>
                )}
              </div>
            )}
            <ul>
              {selectedRecipes.map((record) => {
                const document = record.recipe!.recipe_document;
                const roleBytes = document.topology.roles.reduce<WireNumber>(
                  (sum, role) =>
                    addWire(
                      sum,
                      multiplyWire(
                        role.count,
                        addWire(
                          role.resources.disk.image_bytes,
                          role.resources.disk.artifact_bytes,
                        ),
                      ),
                    ),
                  0,
                );
                return (
                  <li key={record.key}>
                    <NavigateLink href={recipeLibraryPath(record.recipe!)} onNavigate={onNavigate}>
                      <strong>{record.title}</strong>
                      <RecipeBadges recipe={record.recipe!} />
                      <RecipeQualificationEvidence qualification={record.recipe!.qualification} />
                      <small>
                        {recipeAttribution(document)} · release {document.release.version}
                      </small>
                      <small>
                        {document.topology.roles
                          .map(
                            (role) =>
                              `${role.name}: ${formatBytes(role.resources.memory.peak_bytes)} peak${
                                role.resources.memory.load_peak_bytes == null
                                  ? ""
                                  : ` · ${formatBytes(role.resources.memory.load_peak_bytes)} load peak`
                              }${
                                role.resources.memory.minimum_swap_bytes
                                  ? ` · ${formatBytes(role.resources.memory.minimum_swap_bytes)} min swap`
                                  : ""
                              }`,
                          )
                          .join(" · ")}
                      </small>
                      <small>{formatBytes(roleBytes)} image + artifact envelope</small>
                    </NavigateLink>
                    <LibraryRecipeActions
                      api={api}
                      recipe={record.recipe!}
                      missingModels={missingModelsFor(record.recipe!)}
                      onChanged={refresh}
                    />
                  </li>
                );
              })}
            </ul>
            {selectedModel && selectedRecipes.length === 0 && (
              <div className="library-empty-recipe">
                <strong>No Recipe linked</strong>
                <span>
                  This exact Model is available for cache management but has no runnable Recipe.
                </span>
              </div>
            )}
            {!selectedModel && (
              <p className="library-empty-state">Select a Model to see matching Recipes.</p>
            )}
          </section>
        </div>
      </div>
    </section>
  );
}

export function recordCapabilities(record: LibraryRecipeRecord): string[] {
  return record.capabilities;
}
export function applyManagedCatalogWithdrawals(
  records: LibraryRecipeRecord[],
): LibraryRecipeRecord[] {
  return records;
}
