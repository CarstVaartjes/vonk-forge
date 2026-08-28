import {useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {LibraryApi, LibraryRecipeDetail, LibraryRecipeSummary, LibrarySnapshot, PublicRecipe} from "../api/types";
import {formatBytes} from "../lib/fleet";
import type {LibraryRoute} from "../lib/library-route";
import {modelLibraryPath, modelVersionKey, recipeLibraryPath, unlinkedLibraryPath} from "../lib/library-route";
import {LibraryComparison} from "./library-comparison";
import {LibraryRecipeAuthority} from "./library-recipe-detail";
import {friendlyModelName, humanizeIdentifier, TechnicalDetails} from "./library-technical-details";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;
type LibraryViewMode = "browse" | "compact" | "compare";
type RecipeWithModel = {recipe: LibraryRecipeSummary; model?: LibrarySnapshot["models"][number]};

const VIEW_MODE_KEY = "vonk-forge.library.view-mode";
const VIEW_MODES: Array<{mode: LibraryViewMode; label: string; hint: string}> = [
  {mode: "browse", label: "Browse", hint: "Step through models and recipes"},
  {mode: "compact", label: "Compact", hint: "Scan all loaded recipes"},
  {mode: "compare", label: "Compare", hint: "Compare up to three recipes"},
];

function initialViewMode(): LibraryViewMode {
  try {
    const saved = localStorage.getItem(VIEW_MODE_KEY);
    if (saved === "browse" || saved === "compact" || saved === "compare") return saved;
  } catch {
    // Storage can be unavailable in locked-down browsers. Browse remains safe.
  }
  return "browse";
}

function countLabel(count: number, windowed: boolean): string {
  return `${count} recipe${count === 1 ? "" : "s"}${windowed ? " shown" : ""}`;
}

function modelLabel(model: LibrarySnapshot["models"][number]): string {
  return friendlyModelName(model.model);
}

function recipeStatus(recipe: LibraryRecipeSummary): string {
  if (recipe.runs.some(run => run.state === "running" && run.healthy)) return "Running";
  if (recipe.runs.some(run => run.state === "running")) return "Running · attention";
  if (recipe.installations.some(installation => installation.state === "installed")) return "Installed";
  if (recipe.selected_revision?.lifecycle === "resolved") return "Ready";
  return "Needs review";
}

function releaseContextLabel(recipe: PublicRecipe): string {
  const localVersion = recipe.local.release_version ? `v${recipe.local.release_version}` : "local revision";
  const catalogVersion = recipe.release_version ? `v${recipe.release_version}` : "catalog revision";
  if (recipe.local.status === "update-available") return `Update available · ${localVersion} → ${catalogVersion}`;
  if (recipe.local.status === "current") return `${catalogVersion} · catalog current`;
  if (recipe.local.status === "local-ahead") return `${localVersion} · newer than catalog`;
  if (recipe.local.status === "different-revision") return "Catalog revision differs";
  if (recipe.local.status === "conflict") return "Catalog identity conflict";
  return "Not linked to this local recipe";
}

function qualificationLabel(recipe: PublicRecipe): string {
  return recipe.qualification === "candidate" ? "Candidate" : "Accepted";
}

function QualificationBadge({recipe}: {recipe: PublicRecipe}) {
  const label = qualificationLabel(recipe);
  return <span className={`library-qualification qualification-${recipe.qualification}`} title={recipe.qualification_detail}>{label}<span className="visually-hidden"> catalog qualification</span></span>;
}

function releaseReviewPath(recipe: PublicRecipe): string {
  const search = new URLSearchParams({recipe: recipe.uri});
  return `/library/import?${search.toString()}`;
}

function CompareControl({disabled, recipe, selected, onToggle}: {
  disabled: boolean;
  recipe: LibraryRecipeSummary;
  selected: boolean;
  onToggle(recipeId: string): void;
}) {
  return <label className="library-compare-control">
    <input aria-label={`Compare ${recipe.title}`} type="checkbox" checked={selected} disabled={disabled && !selected} onChange={() => onToggle(recipe.recipe_id)}/>
    <span>{selected ? "Selected" : "Compare"}</span>
    <span className="visually-hidden"> {recipe.title}</span>
  </label>;
}

function RecipeEntry({active, catalogRecipe, onNavigate, onToggle, recipe, selected, selectionFull}: {
  active: boolean;
  catalogRecipe?: PublicRecipe;
  onNavigate: Navigate;
  onToggle(recipeId: string): void;
  recipe: LibraryRecipeSummary;
  selected: boolean;
  selectionFull: boolean;
}) {
  return <article className="library-row-shell">
    <a
      href={recipeLibraryPath(recipe.recipe_id)}
      className="library-row"
      aria-current={active ? "page" : undefined}
      onClick={event => onNavigate(event, recipeLibraryPath(recipe.recipe_id))}
    >
      <strong>{recipe.title}</strong>
      <span>{recipe.description}</span>
      <small>{recipe.topology_name ? `${humanizeIdentifier(recipe.topology_name)} topology` : "No valid topology"} · {recipeStatus(recipe)}</small>
      {catalogRecipe && <small className="library-catalog-scan"><QualificationBadge recipe={catalogRecipe}/><span className={`library-release-label status-${catalogRecipe.local.status}`}>{releaseContextLabel(catalogRecipe)}</span></small>}
    </a>
    <div className="library-row-tools">
      {catalogRecipe?.local.status === "update-available" && <a aria-label={`Review update for ${recipe.title}`} className="library-release-link" href={releaseReviewPath(catalogRecipe)} onClick={event => onNavigate(event, releaseReviewPath(catalogRecipe))}>Review update</a>}
      <CompareControl disabled={selectionFull} recipe={recipe} selected={selected} onToggle={onToggle}/>
      <TechnicalDetails compact items={[
        {label: "Recipe ID", value: recipe.recipe_id},
        {label: "Recipe slug", value: recipe.slug},
        {label: "Revision ID", value: recipe.selected_revision?.id ?? ""},
        {label: "Content digest", value: recipe.selected_revision?.content_sha256 ?? ""},
      ]}/>
    </div>
  </article>;
}

function RecipeReleaseContext({onNavigate, recipe}: {onNavigate: Navigate; recipe: PublicRecipe}) {
  const update = recipe.local.status === "update-available";
  const needsReview = ["different-revision", "local-ahead", "conflict"].includes(recipe.local.status);
  return <section className={`library-release-context status-${recipe.local.status}`} aria-label="Catalog release status">
    <div className="library-release-summary"><div><span>Catalog release</span><strong>{releaseContextLabel(recipe)}</strong></div><QualificationBadge recipe={recipe}/></div>
    <p className="library-qualification-detail"><strong>{qualificationLabel(recipe)} qualification.</strong> {recipe.qualification_detail}</p>
    {update && <p>The catalog has a newer immutable revision. Review every release note and required runtime action before replacing the local revision.</p>}
    {needsReview && <p>The local and catalog histories do not form a straightforward update. Review their immutable identities before taking action.</p>}
    {recipe.local.status === "current" && <p>This local digest matches the current catalog release.</p>}
    {(update || needsReview) && <a className="button secondary" href={releaseReviewPath(recipe)} onClick={event => onNavigate(event, releaseReviewPath(recipe))}>{update ? "Review changelog and update" : "Review catalog relationship"}</a>}
  </section>;
}

function selectedModel(snapshot: LibrarySnapshot, route: LibraryRoute) {
  if (route.kind === "model" && !route.unlinked) return snapshot.models.find(model => modelVersionKey(model.model) === route.modelKey);
  if (route.kind !== "recipe") return undefined;
  return snapshot.models.find(model => model.recipes.some(recipe => recipe.recipe_id === route.recipeId));
}

function selectedRecipe(snapshot: LibrarySnapshot, route: LibraryRoute): LibraryRecipeSummary | undefined {
  if (route.kind !== "recipe") return undefined;
  return snapshot.models.flatMap(model => model.recipes).concat(snapshot.unlinked_recipes)
    .find(recipe => recipe.recipe_id === route.recipeId);
}

function searchableModel(model: LibrarySnapshot["models"][number]): string {
  return `${friendlyModelName(model.model)} ${modelVersionKey(model.model)} ${model.model.publisher} ${model.model.slug}`.toLocaleLowerCase();
}

function searchableRecipe(recipe: LibraryRecipeSummary): string {
  return `${recipe.title} ${recipe.slug} ${recipe.description} ${recipe.topology_name ?? ""} ${recipe.capabilities.join(" ")}`.toLocaleLowerCase();
}

function filteredSnapshot(snapshot: LibrarySnapshot, query: string, route: LibraryRoute): LibrarySnapshot {
  const normalized = query.trim().toLocaleLowerCase();
  if (!normalized) return snapshot;
  const recipeId = route.kind === "recipe" ? route.recipeId : undefined;
  const modelKey = route.kind === "model" && !route.unlinked ? route.modelKey : undefined;
  const filterRecipes = (recipes: LibraryRecipeSummary[], modelMatches: boolean) => {
    if (modelMatches) return recipes;
    const matches = recipes.filter(recipe => searchableRecipe(recipe).includes(normalized));
    const selected = recipeId ? recipes.find(recipe => recipe.recipe_id === recipeId) : undefined;
    return selected && !matches.some(recipe => recipe.recipe_id === selected.recipe_id) ? [selected, ...matches] : matches;
  };
  const models = snapshot.models.flatMap(model => {
    const modelMatches = searchableModel(model).includes(normalized);
    const recipes = filterRecipes(model.recipes, modelMatches);
    const selected = modelKey === modelVersionKey(model.model);
    return modelMatches || recipes.length > 0 || selected ? [{...model, recipes}] : [];
  });
  const unlinked = filterRecipes(snapshot.unlinked_recipes, false);
  return {...snapshot, models, unlinked_recipes: unlinked};
}

function flattenedRecipes(snapshot: LibrarySnapshot): RecipeWithModel[] {
  return [
    ...snapshot.models.flatMap(model => model.recipes.map(recipe => ({model, recipe}))),
    ...snapshot.unlinked_recipes.map(recipe => ({recipe})),
  ];
}

export function LibraryBrowser({api, catalogError, catalogLoading, detail, detailError, detailLoading, onBusyChange, onClearSearch, onNavigate, onRefresh, onRetryCatalog, onRetryDetail, preferredNodeId, publicRecipes, query, route, snapshot, windowed}: {
  api: LibraryApi;
  catalogError: string;
  catalogLoading: boolean;
  detail?: LibraryRecipeDetail;
  detailError: string;
  detailLoading: boolean;
  onBusyChange?(busy: boolean): void;
  onClearSearch(): void;
  onNavigate: Navigate;
  onRefresh(signal: AbortSignal): Promise<void>;
  onRetryCatalog(): void;
  onRetryDetail(): void;
  preferredNodeId?: string;
  publicRecipes: PublicRecipe[];
  query: string;
  route: LibraryRoute;
  snapshot: LibrarySnapshot;
  windowed: boolean;
}) {
  const [viewMode, setViewMode] = useState<LibraryViewMode>(initialViewMode);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const visibleSnapshot = filteredSnapshot(snapshot, query, route);
  const allRecipes = useMemo(() => flattenedRecipes(snapshot), [snapshot]);
  const visibleFlatRecipes = useMemo(() => flattenedRecipes(visibleSnapshot), [visibleSnapshot]);
  const model = selectedModel(visibleSnapshot, route);
  const recipe = selectedRecipe(visibleSnapshot, route);
  const unlinked = route.kind === "model"
    ? route.unlinked
    : route.kind === "recipe" && !model && visibleSnapshot.unlinked_recipes.some(item => item.recipe_id === route.recipeId);
  const visibleRecipes = unlinked
    ? visibleSnapshot.unlinked_recipes
    : model?.recipes ?? visibleFlatRecipes.map(item => item.recipe);
  const modelPath = model ? modelLibraryPath(modelVersionKey(model.model)) : unlinked ? unlinkedLibraryPath() : "/library";
  const selectionFull = compareIds.length >= 3;
  const publicByLocalRecipe = useMemo(() => new Map(publicRecipes.flatMap(item => item.local.recipe_id ? [[item.local.recipe_id, item] as const] : [])), [publicRecipes]);
  const catalogUpdateCount = new Set(publicRecipes.flatMap(item => item.local.status === "update-available" && item.local.recipe_id ? [item.local.recipe_id] : [])).size;
  const hasSearch = query.trim().length > 0;
  const hasVisibleResults = visibleSnapshot.models.length > 0 || visibleSnapshot.unlinked_recipes.length > 0;

  function selectViewMode(mode: LibraryViewMode) {
    setViewMode(mode);
    try { localStorage.setItem(VIEW_MODE_KEY, mode); } catch { /* Keep the in-memory preference. */ }
  }

  function toggleCompare(recipeId: string) {
    setCompareIds(current => current.includes(recipeId)
      ? current.filter(id => id !== recipeId)
      : current.length < 3 ? [...current, recipeId] : current);
  }

  const compareRecipeSummaries = allRecipes.map(item => item.recipe);

  return <div className="library-browser-shell">
    <div className="library-view-toolbar">
      <div><span className="library-view-label">View</span><div className="library-view-switcher" role="group" aria-label="Library view mode">
        {VIEW_MODES.map(item => <button key={item.mode} type="button" aria-pressed={viewMode === item.mode} title={item.hint} onClick={() => selectViewMode(item.mode)}>{item.label}{item.mode === "compare" && compareIds.length > 0 ? ` (${compareIds.length})` : ""}</button>)}
      </div></div>
      <p role="status" aria-live="polite">{viewMode === "browse" ? "Guided model and recipe navigation" : viewMode === "compact" ? `${visibleFlatRecipes.length} recipes in a compact list` : `${compareIds.length} of 3 recipes selected`}</p>
    </div>
    {catalogLoading && <p className="library-catalog-state" role="status">Checking catalog release versions…</p>}
    {catalogError && <div className="library-catalog-state is-error" role="alert"><span>Catalog update check failed: {catalogError}</span><button type="button" className="button secondary" onClick={onRetryCatalog}>Retry update check</button></div>}
    {!catalogLoading && !catalogError && catalogUpdateCount > 0 && <aside className="library-update-summary" aria-label="Available catalog updates"><div><strong>{catalogUpdateCount} catalog update{catalogUpdateCount === 1 ? "" : "s"} available</strong><span>Across all local catalog links, including recipes outside this loaded window.</span></div><a className="button secondary" href="/library/import?local=update-available" onClick={event => onNavigate(event, "/library/import?local=update-available")}>Review all updates</a></aside>}

    {viewMode === "browse" && hasSearch && !hasVisibleResults && <section className="library-filter-empty" aria-label="No Library search results">
      <div className="library-empty-visual" aria-hidden="true"><span/><span/><span/></div>
      <h3>No matching models or recipes</h3>
      <p>Nothing in the loaded Library window matches “{query.trim()}”. Clear the search to browse every available local recipe.</p>
      <button type="button" className="button secondary" onClick={onClearSearch}>Clear Library search</button>
    </section>}

    {viewMode === "browse" && (!hasSearch || hasVisibleResults) && <div className={`library-browser route-${route.kind}`}>
      <section className="library-pane library-models" aria-label="Models">
        <div className="library-pane-heading"><div><p className="library-step">1</p><h3>Models</h3></div><small>Derived from recipes</small></div>
        <div className="library-list">
          {visibleSnapshot.models.map(item => <article className="library-row-shell library-model-row" key={modelVersionKey(item.model)}>
            <a
              href={modelLibraryPath(modelVersionKey(item.model))}
              className="library-row"
              aria-current={model && modelVersionKey(model.model) === modelVersionKey(item.model) ? "page" : undefined}
              onClick={event => onNavigate(event, modelLibraryPath(modelVersionKey(item.model)))}
            ><strong>{modelLabel(item)}</strong><span>Model family</span><small>{countLabel(item.recipes.length, windowed)}</small></a>
            <div className="library-row-tools"><TechnicalDetails compact items={[
              {label: "Publisher", value: item.model.publisher},
              {label: "Model slug", value: item.model.slug},
              {label: "Model digest", value: item.model.content_sha256},
            ]}/></div>
          </article>)}
          {visibleSnapshot.unlinked_recipes.length > 0 && <a
            href={unlinkedLibraryPath()}
            className="library-row library-unlinked"
            aria-current={unlinked ? "page" : undefined}
            onClick={event => onNavigate(event, unlinkedLibraryPath())
          }><strong>Unlinked</strong><span>Recipes without a valid exact model version</span><small>{countLabel(snapshot.unlinked_recipes.length, windowed)}</small></a>}
        </div>
      </section>

      <section className="library-pane library-recipes" aria-label={model ? `Recipes for ${modelLabel(model)}` : unlinked ? "Unlinked recipes" : "Recipe inventory"}>
        <a className="library-back" href="/library" onClick={event => onNavigate(event, "/library")}>Back to Models</a>
        <div className="library-pane-heading"><div><p className="library-step">2</p><h3>{model ? modelLabel(model) : (unlinked ? "Unlinked" : "Recipe inventory")}</h3></div><small>{countLabel(visibleRecipes.length, windowed)}</small></div>
        {visibleRecipes ? <div className="library-list">{visibleRecipes.length > 0 ? visibleRecipes.map(item => <RecipeEntry active={item.recipe_id === recipe?.recipe_id} catalogRecipe={publicByLocalRecipe.get(item.recipe_id)} key={item.recipe_id} onNavigate={onNavigate} onToggle={toggleCompare} recipe={item} selected={compareIds.includes(item.recipe_id)} selectionFull={selectionFull}/>) : <p className="library-placeholder">No recipes match this search.</p>}</div> : <p className="library-placeholder">Select a model to see all of its recipes.</p>}
      </section>

      <section className="library-pane library-detail" aria-label="Recipe detail">
        {route.kind === "recipe" && <a className="library-back" href={modelPath} onClick={event => onNavigate(event, modelPath)}>Back to {model ? `${modelLabel(model)} recipes` : unlinked ? "Unlinked recipes" : "Models"}</a>}
        {route.kind !== "recipe" && <p className="library-placeholder">Select a recipe to inspect its exact topology and authority.</p>}
        {route.kind === "recipe" && <>
          <div className="library-pane-heading"><div><p className="library-step">3</p><h3>{detail?.recipe.title ?? recipe?.title ?? "Recipe"}</h3></div></div>
          {detailLoading && <p role="status">Loading exact recipe authority…</p>}
          {detailError && <div className="fleet-error" role="alert"><p>{detailError}</p><button type="button" onClick={onRetryDetail}>Retry recipe detail</button></div>}
          {detail && publicByLocalRecipe.get(detail.recipe.recipe_id) && <RecipeReleaseContext onNavigate={onNavigate} recipe={publicByLocalRecipe.get(detail.recipe.recipe_id)!}/>}
          {detail && <LibraryRecipeAuthority
            api={api}
            detail={detail}
            onBusyChange={onBusyChange}
            onRefresh={onRefresh}
            policy={snapshot.freshness_policy}
            preferredNodeId={preferredNodeId}
          />}
        </>}
      </section>
    </div>}

    {viewMode === "compact" && <section className="library-compact" aria-label="Compact recipe list">
      <div className="library-compact-heading"><div><p className="fleet-kicker">Loaded window</p><h3>All recipes</h3></div><span>{visibleFlatRecipes.length} shown</span></div>
      <div className="library-compact-list">
        {visibleFlatRecipes.map(({model: itemModel, recipe: item}) => {
          const catalogRecipe = publicByLocalRecipe.get(item.recipe_id);
          return <article className="library-compact-row" key={item.recipe_id}>
            <div className="library-compact-primary"><a href={recipeLibraryPath(item.recipe_id)} onClick={event => onNavigate(event, recipeLibraryPath(item.recipe_id))}><strong>{item.title}</strong></a><span>{itemModel ? modelLabel(itemModel) : "Unlinked model"}</span></div>
            <div className="library-compact-facts"><span>{item.topology_name ? humanizeIdentifier(item.topology_name) : "No topology"}</span><span>{recipeStatus(item)}</span>{catalogRecipe ? <><QualificationBadge recipe={catalogRecipe}/><span>{releaseContextLabel(catalogRecipe)}</span><span>{catalogRecipe.node_count} {catalogRecipe.node_count === 1 ? "Spark" : "Sparks"}</span><span>{formatBytes(catalogRecipe.expected_download_bytes)} download</span><span>{formatBytes(catalogRecipe.maximum_runtime_memory_bytes_per_node)} memory / Spark</span></> : <span>No catalog release link</span>}<span>{item.capabilities.length ? item.capabilities.map(humanizeIdentifier).join(", ") : "No capabilities"}</span></div>
            <CompareControl disabled={selectionFull} recipe={item} selected={compareIds.includes(item.recipe_id)} onToggle={toggleCompare}/>
            <TechnicalDetails compact items={[
              {label: "Recipe ID", value: item.recipe_id},
              {label: "Recipe slug", value: item.slug},
              {label: "Revision ID", value: item.selected_revision?.id ?? ""},
              {label: "Content digest", value: item.selected_revision?.content_sha256 ?? ""},
            ]}/>
          </article>;
        })}
        {visibleFlatRecipes.length === 0 && <p className="library-placeholder">No recipes match this search.</p>}
      </div>
    </section>}

    {viewMode === "compare" && <>
      <section className="library-compare-picker" aria-label="Choose recipes to compare">
        <div className="library-compact-heading"><div><p className="fleet-kicker">Select up to three</p><h3>Comparison set</h3></div><span>{compareIds.length} selected</span></div>
        <div className="library-compare-picker-list">{visibleFlatRecipes.map(({model: itemModel, recipe: item}) => {
          const catalogRecipe = publicByLocalRecipe.get(item.recipe_id);
          return <label key={item.recipe_id} className="library-compare-option">
            <input type="checkbox" checked={compareIds.includes(item.recipe_id)} disabled={selectionFull && !compareIds.includes(item.recipe_id)} onChange={() => toggleCompare(item.recipe_id)}/>
            <span><strong>{item.title}</strong><small>{itemModel ? modelLabel(itemModel) : "Unlinked model"} · {item.topology_name ? humanizeIdentifier(item.topology_name) : "No topology"}</small>{catalogRecipe && <QualificationBadge recipe={catalogRecipe}/>}</span>
          </label>;
        })}</div>
      </section>
      <LibraryComparison api={api} publicRecipes={publicRecipes} recipes={compareRecipeSummaries} selectedIds={compareIds} onToggle={toggleCompare}/>
    </>}

    {viewMode !== "compare" && compareIds.length > 0 && <aside className="library-compare-tray" aria-label="Comparison tray">
      <div><strong>{compareIds.length} recipe{compareIds.length === 1 ? "" : "s"} selected</strong><span>{compareIds.map(id => compareRecipeSummaries.find(recipeItem => recipeItem.recipe_id === id)?.title).filter(Boolean).join(" · ")}</span></div>
      <button type="button" className="button" onClick={() => selectViewMode("compare")}>Compare now</button>
    </aside>}
  </div>;
}
