import { NoReading } from "../ui/no-reading";
import { CacheOperationKind, LibraryCacheState } from "../../api/vocabulary.generated";
import { compareWire, formatWire } from "../../api/contract-numeric";
import type { MouseEvent } from "react";
import { useState } from "react";
import type {
  ControlApi,
  LibraryViewModel,
  LibraryViewSnapshot,
  VisualFleetSnapshot,
} from "../../api/types";
import { formatBytes } from "../../lib/fleet";
import { modelLibraryPath, modelKey, recipeLibraryPath } from "../../lib/library-route";
import { LibraryCacheAction } from "./library-cache-action";
import { LibrarySetupStrip } from "./library-setup";
import { useCapabilityWords } from "./library-vocabulary";
import type { LibraryRecipeRecord, LibraryWorkcellFilters } from "./library-workcell";
import {
  countModels,
  countVariants,
  modelFoldKey,
  EMPTY_LIBRARY_WORKCELL_FILTERS,
  EmptyLibrary,
  filterLibraryRecipeRecords,
  isModelCached,
  libraryFiltersToSearch,
  useShowMore,
} from "./library-workcell";

type LibraryModelsViewProps = {
  api: ControlApi;
  entries: LibraryRecipeRecord[];
  fleet?: VisualFleetSnapshot | undefined;
  modelInventory?: LibraryViewSnapshot["models"];
  filters: LibraryWorkcellFilters;
  onFiltersChange(filters: LibraryWorkcellFilters): void;
  onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void;
  onNavigatePath?: ((path: string, replace?: boolean) => void) | undefined;
  onQueryChange(value: string): void;
  onRefresh(signal: AbortSignal): Promise<void>;
  path: string;
  query: string;
};

export function LibraryModelsView({
  api,
  entries,
  fleet,
  filters,
  modelInventory,
  onFiltersChange,
  onNavigate,
  onNavigatePath,
  onQueryChange,
  onRefresh,
  path,
  query,
}: LibraryModelsViewProps) {
  const models = modelInventory ?? [];
  const filteredRecipes = filterLibraryRecipeRecords(entries, filters, query);
  const normalizedQuery = query.trim().toLowerCase();
  // Recipe-level filters (Sparks, engine, creator, fleet fit) have no model
  // field to test, so a model stays visible while one of its recipes passes.
  const recipeScoped = Boolean(filters.sparks || filters.engine || filters.creator || filters.fits);
  const visible = models
    .filter((model) => !filters.cached || isModelCached(model))
    .filter(
      (model) =>
        !recipeScoped ||
        filteredRecipes.some(
          (record) => record.recipe !== undefined && record.modelKey === modelKey(model.model),
        ),
    )
    .filter((model) => !filters.model || modelKey(model.model) === filters.model)
    .filter((model) => !filters.usage || model.usage.includes(filters.usage))
    .filter((model) => !filters.family || model.family === filters.family)
    .filter((model) => !filters.version || model.version === filters.version)
    .filter((model) => !filters.quantization || model.quantization === filters.quantization)
    .filter((model) => !filters.publisher || model.model.publisher === filters.publisher)
    .filter((model) => !filters.alignment || model.alignment.includes(filters.alignment))
    .filter(
      (model) =>
        !normalizedQuery ||
        filteredRecipes.some((record) => record.modelKey === modelKey(model.model)) ||
        modelTitle(model).toLowerCase().includes(normalizedQuery),
    );
  const others = visible.filter((model) => modelFoldKey(model.model_display) === null).length;
  const groups = groupModels(visible);
  const { limit, more } = useShowMore(JSON.stringify([filters, query]), groups.size, 0, "rows");
  const refresh = () => void onRefresh(new AbortController().signal);

  function updateFilters(patch: Partial<LibraryWorkcellFilters>) {
    const next = { ...filters, ...patch };
    onFiltersChange(next);
    if (!onNavigatePath) return;
    const url = new URL(path, location.origin);
    onNavigatePath(
      `${url.pathname}?${libraryFiltersToSearch(next, url.searchParams).toString()}`,
      true,
    );
  }

  return (
    <section className="library-models-view" aria-labelledby="library-models-heading">
      <header className="library-subview-heading">
        <div>
          <h2 id="library-models-heading">Models</h2>
          <p>
            Browse the verified model library, prepare the cache, and open the recipes that use each
            model.
          </p>
        </div>
        <span>
          {countModels(visible)} of {countModels(models)} models · {countVariants(visible)} variants
          {others > 0 && ` · ${others} other models`}
        </span>
      </header>
      <LibrarySetupStrip
        fleet={fleet}
        records={entries}
        filters={filters}
        query={query}
        noun="models"
        onFiltersChange={(next) => updateFilters(next)}
        onQueryChange={onQueryChange}
      />
      <section className="library-model-list" aria-label="Model library">
        {[...groups].slice(0, limit).map(([key, revisions]) => (
          <ModelRevisionsRow
            key={key}
            api={api}
            revisions={revisions}
            onNavigate={onNavigate}
            onPrepared={refresh}
          />
        ))}
        {more}
        {visible.length === 0 && (
          <EmptyLibrary
            filters={filters}
            query={query}
            onRefresh={refresh}
            onClear={() => {
              updateFilters(EMPTY_LIBRARY_WORKCELL_FILTERS);
              onQueryChange("");
            }}
          />
        )}
      </section>
    </section>
  );
}

function groupModels(models: LibraryViewModel[]): Map<string, LibraryViewModel[]> {
  const groups = new Map<string, LibraryViewModel[]>();
  for (const model of models) {
    const logical = model.model_document.identity.model;
    const key = JSON.stringify([
      model.model.publisher,
      logical.publisher,
      logical.slug,
      model.quantization,
    ]);
    groups.set(key, [...(groups.get(key) ?? []), model]);
  }
  return groups;
}

function ModelRevisionsRow({
  api,
  revisions,
  onNavigate,
  onPrepared,
}: {
  api: ControlApi;
  revisions: LibraryViewModel[];
  onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void;
  onPrepared(): void;
}) {
  const [selected, setSelected] = useState<string>();
  // Preserve the server's order, preferring an exact revision with a recipe.
  // Filtering still happens per revision before grouping, so exact links and
  // version/usage filters cannot silently select a different revision.
  const model =
    revisions.find((item) => modelKey(item.model) === selected) ??
    revisions.find((item) => item.recipes.length > 0) ??
    revisions[0]!;
  const key = modelKey(model.model);
  const bytes = model.model_bytes;
  const words = useCapabilityWords();
  const capabilities = model.usage;
  const running = (model.local.running_on ?? []).length;
  const preparation = model.local.preparation;
  // One rule, decided by the Controller: download or remove, never a button that cannot apply.
  const actions = model.actions;
  const downloadable = actions.includes(CacheOperationKind.DOWNLOAD);
  // A live preparation the Controller reports: its progress and Cancel come from here, not from this page's memory.
  const cancellable = actions.includes(CacheOperationKind.CANCEL);
  // An idle partial set reads as preparing but can be resumed; only a live preparation cannot.
  const downloadState =
    downloadable && model.local.controller === LibraryCacheState.PREPARING
      ? LibraryCacheState.NOT_CACHED
      : model.local.controller;
  // A recipe may reference the same model in more than one selection with
  // different roles, so list each recipe once per model.
  const recipes = [
    ...new Map(model.recipes.map((recipe) => [recipe.recipe_revision_id, recipe])).values(),
  ];

  return (
    <article className="library-model-row">
      <div>
        {modelFoldKey(model.model_display) === null ? (
          // Not a Model (helper, drafter, encoder): no Model page to open; its recipes keep their links.
          <div>
            <h3>{modelTitle(model)}</h3>
            <p>
              {model.model.publisher}/{model.model.slug} · {model.model_document.identity.variant}
            </p>
          </div>
        ) : (
          <a
            href={modelLibraryPath(key)}
            onClick={(event) => onNavigate(event, modelLibraryPath(key))}
          >
            <h3>{modelTitle(model)}</h3>
            <p>
              {model.model.publisher}/{model.model.slug} · {model.model_document.identity.variant}
            </p>
          </a>
        )}
        <div className="library-model-badges">
          {capabilities.length ? (
            capabilities.map((capability) => (
              <span key={capability} title={words.title(capability)}>
                {words.label(capability)}
              </span>
            ))
          ) : (
            <span>
              <NoReading />
            </span>
          )}
        </div>
        {revisions.length > 1 && (
          <label className="library-model-revision">
            Revision
            <select
              aria-label={`Revision for ${modelTitle(model)}`}
              value={key}
              onChange={(event) => setSelected(event.target.value)}
            >
              {revisions.map((revision) => (
                <option key={modelKey(revision.model)} value={modelKey(revision.model)}>
                  {revision.version} · {revision.model.content_sha256.slice(0, 8)}
                  {revision.recipes.length ? " · Recipes available" : ""}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className="library-model-actions">
          {(downloadable ||
            cancellable ||
            model.local.controller === LibraryCacheState.PREPARING) && (
            <LibraryCacheAction
              key={key}
              api={api}
              onPrepared={onPrepared}
              selector={`${model.model.publisher}/${model.model.slug}`}
              modelContentSha256={model.model.content_sha256}
              state={downloadState}
              preparation={cancellable ? preparation : undefined}
            />
          )}
          {actions.includes(CacheOperationKind.REMOVE) && (
            <LibraryCacheAction
              fleetOnly
              key={`${key}-fleet`}
              api={api}
              onPrepared={onPrepared}
              selector={`${model.model.publisher}/${model.model.slug}`}
              modelContentSha256={model.model.content_sha256}
              state={model.local.controller}
            />
          )}
        </div>
      </div>
      <dl>
        <div>
          <dt>Files</dt>
          <dd>{formatWire(model.model_file_count)}</dd>
        </div>
        <div>
          <dt>Size</dt>
          <dd>{formatBytes(bytes)}</dd>
        </div>
        {model.cache_usage && (
          <>
            <div>
              <dt>Unique</dt>
              <dd>{formatBytes(model.cache_usage.unique_bytes)}</dd>
            </div>
            <div>
              <dt>Shared</dt>
              <dd>{formatBytes(model.cache_usage.shared_bytes)}</dd>
            </div>
          </>
        )}
        <div>
          <dt>Recipes</dt>
          <dd>{recipes.length || "None"}</dd>
        </div>
        {running > 0 && (
          <div>
            <dt>Running on</dt>
            <dd>{running}</dd>
          </div>
        )}
      </dl>
      {(model.cache_usage?.retained_revisions ?? []).map((revision) => (
        <p key={revision.content_sha256}>
          Previous revision retained because {revision.owners.join(", ")};{" "}
          {formatBytes(revision.unique_bytes)} unique
        </p>
      ))}
      {recipes.length > 0 && (
        <ul className="library-model-recipes" aria-label={`Recipes using ${modelTitle(model)}`}>
          {recipes.map((recipe) => (
            <li key={recipe.recipe_revision_id}>
              <a
                href={recipeLibraryPath(recipe)}
                onClick={(event) => onNavigate(event, recipeLibraryPath(recipe))}
              >
                {recipe.title}
              </a>
              <span>
                {" "}
                · {recipe.topology_name} · {formatWire(recipe.recipe_document.topology.node_count)}{" "}
                {compareWire(recipe.recipe_document.topology.node_count, 1) === 0
                  ? "Spark"
                  : "Sparks"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

function modelTitle(model: LibraryViewModel): string {
  return (
    model.model_display?.model_title ||
    model.model_display?.family_title ||
    `${model.model.publisher}/${model.model.slug}`
  );
}
