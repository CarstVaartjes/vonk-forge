import {useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {LibraryApi, LibraryRecipeDetail, LibraryRecipeSummary, LibrarySnapshot} from "../api/types";
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

function CompareControl({disabled, recipe, selected, onToggle}: {
  disabled: boolean;
  recipe: LibraryRecipeSummary;
  selected: boolean;
  onToggle(recipeId: string): void;
}) {
  return <label className="library-compare-control">
    <input type="checkbox" checked={selected} disabled={disabled && !selected} onChange={() => onToggle(recipe.recipe_id)}/>
    <span>{selected ? "Selected" : "Compare"}</span>
    <span className="visually-hidden"> {recipe.title}</span>
  </label>;
}

function RecipeEntry({active, onNavigate, onToggle, recipe, selected, selectionFull}: {
  active: boolean;
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
    </a>
    <div className="library-row-tools">
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

export function LibraryBrowser({api, detail, detailError, detailLoading, onBusyChange, onNavigate, onRefresh, onRetryDetail, query, route, snapshot, windowed}: {
  api: LibraryApi;
  detail?: LibraryRecipeDetail;
  detailError: string;
  detailLoading: boolean;
  onBusyChange?(busy: boolean): void;
  onNavigate: Navigate;
  onRefresh(signal: AbortSignal): Promise<void>;
  onRetryDetail(): void;
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
  const visibleRecipes = unlinked ? visibleSnapshot.unlinked_recipes : model?.recipes;
  const modelPath = model ? modelLibraryPath(modelVersionKey(model.model)) : unlinked ? unlinkedLibraryPath() : "/library";
  const selectionFull = compareIds.length >= 3;

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

    {viewMode === "browse" && <div className={`library-browser route-${route.kind}`}>
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

      <section className="library-pane library-recipes" aria-label={model ? `Recipes for ${modelLabel(model)}` : unlinked ? "Unlinked recipes" : "Recipes"}>
        <a className="library-back" href="/library" onClick={event => onNavigate(event, "/library")}>Back to Models</a>
        <div className="library-pane-heading"><div><p className="library-step">2</p><h3>{model ? modelLabel(model) : (unlinked ? "Unlinked" : "Recipes")}</h3></div>{visibleRecipes && <small>{countLabel(visibleRecipes.length, windowed)}</small>}</div>
        {visibleRecipes ? <div className="library-list">{visibleRecipes.length > 0 ? visibleRecipes.map(item => <RecipeEntry active={item.recipe_id === recipe?.recipe_id} key={item.recipe_id} onNavigate={onNavigate} onToggle={toggleCompare} recipe={item} selected={compareIds.includes(item.recipe_id)} selectionFull={selectionFull}/>) : <p className="library-placeholder">No recipes match this search.</p>}</div> : <p className="library-placeholder">Select a model to see all of its recipes.</p>}
      </section>

      <section className="library-pane library-detail" aria-label="Recipe detail">
        {route.kind === "recipe" && <a className="library-back" href={modelPath} onClick={event => onNavigate(event, modelPath)}>Back to {model ? `${modelLabel(model)} recipes` : unlinked ? "Unlinked recipes" : "Models"}</a>}
        {route.kind !== "recipe" && <p className="library-placeholder">Select a recipe to inspect its exact topology and authority.</p>}
        {route.kind === "recipe" && <>
          <div className="library-pane-heading"><div><p className="library-step">3</p><h3>{detail?.recipe.title ?? recipe?.title ?? "Recipe"}</h3></div></div>
          {detailLoading && <p role="status">Loading exact recipe authority…</p>}
          {detailError && <div className="fleet-error" role="alert"><p>{detailError}</p><button type="button" onClick={onRetryDetail}>Retry recipe detail</button></div>}
          {detail && <LibraryRecipeAuthority api={api} detail={detail} onBusyChange={onBusyChange} onRefresh={onRefresh} policy={snapshot.freshness_policy}/>}
        </>}
      </section>
    </div>}

    {viewMode === "compact" && <section className="library-compact" aria-label="Compact recipe list">
      <div className="library-compact-heading"><div><p className="fleet-kicker">Loaded window</p><h3>All recipes</h3></div><span>{visibleFlatRecipes.length} shown</span></div>
      <div className="library-compact-list">
        {visibleFlatRecipes.map(({model: itemModel, recipe: item}) => <article className="library-compact-row" key={item.recipe_id}>
          <div className="library-compact-primary"><a href={recipeLibraryPath(item.recipe_id)} onClick={event => onNavigate(event, recipeLibraryPath(item.recipe_id))}><strong>{item.title}</strong></a><span>{itemModel ? modelLabel(itemModel) : "Unlinked model"}</span></div>
          <div className="library-compact-facts"><span>{item.topology_name ? humanizeIdentifier(item.topology_name) : "No topology"}</span><span>{recipeStatus(item)}</span><span>{item.capabilities.length ? item.capabilities.map(humanizeIdentifier).join(", ") : "No capabilities"}</span></div>
          <CompareControl disabled={selectionFull} recipe={item} selected={compareIds.includes(item.recipe_id)} onToggle={toggleCompare}/>
          <TechnicalDetails compact items={[
            {label: "Recipe ID", value: item.recipe_id},
            {label: "Recipe slug", value: item.slug},
            {label: "Revision ID", value: item.selected_revision?.id ?? ""},
            {label: "Content digest", value: item.selected_revision?.content_sha256 ?? ""},
          ]}/>
        </article>)}
        {visibleFlatRecipes.length === 0 && <p className="library-placeholder">No recipes match this search.</p>}
      </div>
    </section>}

    {viewMode === "compare" && <>
      <section className="library-compare-picker" aria-label="Choose recipes to compare">
        <div className="library-compact-heading"><div><p className="fleet-kicker">Select up to three</p><h3>Comparison set</h3></div><span>{compareIds.length} selected</span></div>
        <div className="library-compare-picker-list">{visibleFlatRecipes.map(({model: itemModel, recipe: item}) => <label key={item.recipe_id} className="library-compare-option">
          <input type="checkbox" checked={compareIds.includes(item.recipe_id)} disabled={selectionFull && !compareIds.includes(item.recipe_id)} onChange={() => toggleCompare(item.recipe_id)}/>
          <span><strong>{item.title}</strong><small>{itemModel ? modelLabel(itemModel) : "Unlinked model"} · {item.topology_name ? humanizeIdentifier(item.topology_name) : "No topology"}</small></span>
        </label>)}</div>
      </section>
      <LibraryComparison api={api} recipes={compareRecipeSummaries} selectedIds={compareIds} onToggle={toggleCompare}/>
    </>}

    {viewMode !== "compare" && compareIds.length > 0 && <aside className="library-compare-tray" aria-label="Comparison tray">
      <div><strong>{compareIds.length} recipe{compareIds.length === 1 ? "" : "s"} selected</strong><span>{compareIds.map(id => compareRecipeSummaries.find(recipeItem => recipeItem.recipe_id === id)?.title).filter(Boolean).join(" · ")}</span></div>
      <button type="button" className="button" onClick={() => selectViewMode("compare")}>Compare now</button>
    </aside>}
  </div>;
}
