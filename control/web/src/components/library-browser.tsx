import type {MouseEvent} from "react";
import type {LibraryApi, LibraryRecipeDetail, LibraryRecipeSummary, LibrarySnapshot} from "../api/types";
import type {LibraryRoute} from "../lib/library-route";
import {modelLibraryPath, modelVersionKey, recipeLibraryPath, unlinkedLibraryPath} from "../lib/library-route";
import {LibraryRecipeAuthority} from "./library-recipe-detail";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;

function countLabel(count: number, windowed: boolean): string {
  return `${count} recipe${count === 1 ? "" : "s"}${windowed ? " shown" : ""}`;
}

function RecipeLink({onNavigate, recipe, selected}: {onNavigate: Navigate; recipe: LibraryRecipeSummary; selected: boolean}) {
  return <a
    href={recipeLibraryPath(recipe.recipe_id)}
    className="library-row"
    aria-current={selected ? "page" : undefined}
    onClick={event => onNavigate(event, recipeLibraryPath(recipe.recipe_id))}
  >
    <strong>{recipe.title}</strong>
    <span>{recipe.description}</span>
    <small>{recipe.topology_name ? `Topology ${recipe.topology_name}` : "No valid topology"}</small>
  </a>;
}

function modelLabel(model: LibrarySnapshot["models"][number]): string {
  return modelVersionKey(model.model);
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
  return `${modelVersionKey(model.model)} ${model.model.publisher} ${model.model.slug}`.toLocaleLowerCase();
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

export function LibraryBrowser({api, detail, detailError, detailLoading, onNavigate, onRefresh, onRetryDetail, query, route, snapshot, windowed}: {
  api: LibraryApi;
  detail?: LibraryRecipeDetail;
  detailError: string;
  detailLoading: boolean;
  onNavigate: Navigate;
  onRefresh(signal: AbortSignal): Promise<void>;
  onRetryDetail(): void;
  query: string;
  route: LibraryRoute;
  snapshot: LibrarySnapshot;
  windowed: boolean;
}) {
  const visibleSnapshot = filteredSnapshot(snapshot, query, route);
  const model = selectedModel(visibleSnapshot, route);
  const recipe = selectedRecipe(visibleSnapshot, route);
  const unlinked = route.kind === "model"
    ? route.unlinked
    : route.kind === "recipe" && !model && visibleSnapshot.unlinked_recipes.some(item => item.recipe_id === route.recipeId);
  const visibleRecipes = unlinked ? visibleSnapshot.unlinked_recipes : model?.recipes;
  const modelPath = model ? modelLibraryPath(modelVersionKey(model.model)) : unlinked ? unlinkedLibraryPath() : "/library";
  return <div className={`library-browser route-${route.kind}`}>
    <section className="library-pane library-models" aria-label="Models">
      <div className="library-pane-heading"><div><p className="library-step">1</p><h3>Models</h3></div><small>Derived from recipes</small></div>
      <div className="library-list">
        {visibleSnapshot.models.map(item => <a
          key={modelVersionKey(item.model)}
          href={modelLibraryPath(modelVersionKey(item.model))}
          className="library-row"
          aria-current={model && modelVersionKey(model.model) === modelVersionKey(item.model) ? "page" : undefined}
          onClick={event => onNavigate(event, modelLibraryPath(modelVersionKey(item.model)))}
        ><strong>{modelLabel(item)}</strong><span>Exact immutable model version</span><small>{countLabel(item.recipes.length, windowed)}</small></a>)}
        {visibleSnapshot.unlinked_recipes.length > 0 && <a
          href={unlinkedLibraryPath()}
          className="library-row library-unlinked"
          aria-current={unlinked ? "page" : undefined}
          onClick={event => onNavigate(event, unlinkedLibraryPath())}
        ><strong>Unlinked</strong><span>Recipes without a valid exact model version</span><small>{countLabel(snapshot.unlinked_recipes.length, windowed)}</small></a>}
      </div>
    </section>

    <section className="library-pane library-recipes" aria-label={model ? `Recipes for ${modelLabel(model)}` : unlinked ? "Unlinked recipes" : "Recipes"}>
      <a className="library-back" href="/library" onClick={event => onNavigate(event, "/library")}>Back to Models</a>
      <div className="library-pane-heading"><div><p className="library-step">2</p><h3>{model ? modelLabel(model) : (unlinked ? "Unlinked" : "Recipes")}</h3></div>{visibleRecipes && <small>{countLabel(visibleRecipes.length, windowed)}</small>}</div>
      {visibleRecipes ? <div className="library-list">{visibleRecipes.length > 0 ? visibleRecipes.map(item => <RecipeLink key={item.recipe_id} onNavigate={onNavigate} recipe={item} selected={item.recipe_id === recipe?.recipe_id}/>) : <p className="library-placeholder">No recipes match this search.</p>}</div> : <p className="library-placeholder">Select a model to see all of its recipes.</p>}
    </section>

    <section className="library-pane library-detail" aria-label="Recipe detail">
      {route.kind === "recipe" && <a className="library-back" href={modelPath} onClick={event => onNavigate(event, modelPath)}>Back to {model ? `${modelLabel(model)} recipes` : unlinked ? "Unlinked recipes" : "Models"}</a>}
      {route.kind !== "recipe" && <p className="library-placeholder">Select a recipe to inspect its exact topology and authority.</p>}
      {route.kind === "recipe" && <>
        <div className="library-pane-heading"><div><p className="library-step">3</p><h3>{detail?.recipe.title ?? recipe?.title ?? "Recipe"}</h3></div></div>
        {detailLoading && <p role="status">Loading exact recipe authority…</p>}
        {detailError && <div className="fleet-error" role="alert"><p>{detailError}</p><button type="button" onClick={onRetryDetail}>Retry recipe detail</button></div>}
        {detail && <LibraryRecipeAuthority api={api} detail={detail} onRefresh={onRefresh} policy={snapshot.freshness_policy}/>}
      </>}
    </section>
  </div>;
}
