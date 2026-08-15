import type {MouseEvent} from "react";
import type {LibraryApi, LibraryRecipeDetail, LibraryRecipeSummary, LibrarySnapshot} from "../api/types";
import type {LibraryRoute} from "../lib/library-route";
import {modelLibraryPath, recipeLibraryPath, unlinkedLibraryPath} from "../lib/library-route";
import {LibraryRecipeAuthority} from "./library-recipe-detail";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;

function countLabel(count: number): string {
  return `${count} recipe${count === 1 ? "" : "s"}`;
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
    <small>{recipe.profiles.map(profile => `${profile.node_count} node`).join(" · ") || "No valid placement profile"}</small>
  </a>;
}

function selectedModel(snapshot: LibrarySnapshot, route: LibraryRoute) {
  if (route.kind === "model" && !route.unlinked) return snapshot.models.find(model => model.family === route.family);
  if (route.kind !== "recipe") return undefined;
  return snapshot.models.find(model => model.recipes.some(recipe => recipe.recipe_id === route.recipeId));
}

function selectedRecipe(snapshot: LibrarySnapshot, route: LibraryRoute): LibraryRecipeSummary | undefined {
  if (route.kind !== "recipe") return undefined;
  return snapshot.models.flatMap(model => model.recipes).concat(snapshot.unlinked_recipes)
    .find(recipe => recipe.recipe_id === route.recipeId);
}

export function LibraryBrowser({api, detail, detailError, detailLoading, onNavigate, onRefresh, route, snapshot}: {
  api: LibraryApi;
  detail?: LibraryRecipeDetail;
  detailError: string;
  detailLoading: boolean;
  onNavigate: Navigate;
  onRefresh(signal: AbortSignal): Promise<void>;
  route: LibraryRoute;
  snapshot: LibrarySnapshot;
}) {
  const model = selectedModel(snapshot, route);
  const recipe = selectedRecipe(snapshot, route);
  const recipes = route.kind === "model" && route.unlinked ? snapshot.unlinked_recipes : model?.recipes;
  const modelPath = model ? modelLibraryPath(model.family) : "/library";
  return <div className={`library-browser route-${route.kind}`}>
    <section className="library-pane library-models" aria-label="Models">
      <div className="library-pane-heading"><div><p className="library-step">1</p><h3>Models</h3></div><small>Derived from recipes</small></div>
      <div className="library-list">
        {snapshot.models.map(item => <a
          key={item.family}
          href={modelLibraryPath(item.family)}
          className="library-row"
          aria-current={model?.family === item.family ? "page" : undefined}
          onClick={event => onNavigate(event, modelLibraryPath(item.family))}
        ><strong>{item.display_name}</strong><span>{item.family}</span><small>{countLabel(item.recipes.length)}</small></a>)}
        {snapshot.unlinked_recipes.length > 0 && <a
          href={unlinkedLibraryPath()}
          className="library-row library-unlinked"
          aria-current={route.kind === "model" && route.unlinked ? "page" : undefined}
          onClick={event => onNavigate(event, unlinkedLibraryPath())}
        ><strong>Unlinked</strong><span>Recipes without a valid model family</span><small>{countLabel(snapshot.unlinked_recipes.length)}</small></a>}
      </div>
    </section>

    <section className="library-pane library-recipes" aria-label={model ? `Recipes for ${model.display_name}` : route.kind === "model" && route.unlinked ? "Unlinked recipes" : "Recipes"}>
      <a className="library-back" href="/library" onClick={event => onNavigate(event, "/library")}>Back to Models</a>
      <div className="library-pane-heading"><div><p className="library-step">2</p><h3>{model?.display_name ?? (route.kind === "model" && route.unlinked ? "Unlinked" : "Recipes")}</h3></div>{recipes && <small>{countLabel(recipes.length)}</small>}</div>
      {recipes ? <div className="library-list">{recipes.map(item => <RecipeLink key={item.recipe_id} onNavigate={onNavigate} recipe={item} selected={item.recipe_id === recipe?.recipe_id}/>)}</div> : <p className="library-placeholder">Select a model to see all of its recipes.</p>}
    </section>

    <section className="library-pane library-detail" aria-label="Recipe detail">
      {route.kind === "recipe" && <a className="library-back" href={modelPath} onClick={event => onNavigate(event, modelPath)}>Back to {model ? `${model.display_name} recipes` : "Models"}</a>}
      {route.kind !== "recipe" && <p className="library-placeholder">Select a recipe to inspect its exact topology and authority.</p>}
      {route.kind === "recipe" && <>
        <div className="library-pane-heading"><div><p className="library-step">3</p><h3>{detail?.recipe.title ?? recipe?.title ?? "Recipe"}</h3></div></div>
        {detailLoading && <p role="status">Loading exact recipe authority…</p>}
        {detailError && <p role="alert">{detailError}</p>}
        {detail && <LibraryRecipeAuthority api={api} detail={detail} onRefresh={onRefresh} policy={snapshot.freshness_policy}/>}
      </>}
    </section>
  </div>;
}
