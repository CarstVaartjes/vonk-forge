import {useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {MouseEvent} from "react";
import type {CatalogApi, ControlApi, LibraryApi, LibraryModel, LibraryRecipeDetail, LibraryRecipeSummary, LibrarySnapshot, PublicRecipe} from "../api/types";
import {LibraryBrowser} from "../components/library-browser";
import {LibraryNodeNamesProvider} from "../components/library-node-names";
import {nodeDisplayName} from "../lib/fleet";
import {libraryRoute, modelVersionKey} from "../lib/library-route";
import type {LibraryRoute} from "../lib/library-route";
import "./library.css";

const LIBRARY_MODEL_WINDOW = 40;
const LIBRARY_RECIPE_WINDOW = 50;

type RouteParent =
  | {kind: "model"; model: Omit<LibraryModel, "recipes">; recipe: LibraryRecipeSummary}
  | {kind: "unlinked"; recipe: LibraryRecipeSummary};

function boundedItems<T>(items: T[], limit: number, key: (item: T) => string, pinnedKey?: string): {items: T[]; truncated: boolean} {
  if (items.length <= limit) return {items, truncated: false};
  const window = items.slice(-limit);
  const pinned = pinnedKey ? items.find(item => key(item) === pinnedKey) : undefined;
  if (pinned && !window.some(item => key(item) === pinnedKey)) window.splice(0, 1, pinned);
  return {items: window, truncated: true};
}

function mergeSnapshot(current: LibrarySnapshot | undefined, next: LibrarySnapshot, route: LibraryRoute): {snapshot: LibrarySnapshot; truncated: boolean} {
  const models = new Map((current?.models ?? []).map(model => [modelVersionKey(model.model), {...model, recipes: [...model.recipes]}]));
  for (const model of next.models) {
    const modelKey = modelVersionKey(model.model);
    const existing = models.get(modelKey);
    if (!existing) {
      models.set(modelKey, {...model, recipes: [...model.recipes]});
      continue;
    }
    const recipeIds = new Set(existing.recipes.map(recipe => recipe.recipe_id));
    models.set(modelKey, {
      ...existing,
      recipes: existing.recipes.concat(model.recipes.filter(recipe => !recipeIds.has(recipe.recipe_id))),
    });
  }
  let truncated = false;
  const recipeId = route.kind === "recipe" ? route.recipeId : undefined;
  const modelItems = [...models.values()].map(model => {
    const bounded = boundedItems(model.recipes, LIBRARY_RECIPE_WINDOW, recipe => recipe.recipe_id, recipeId);
    truncated ||= bounded.truncated;
    return {...model, recipes: bounded.items};
  });
  const recipeModel = modelItems.find(
    model => recipeId && model.recipes.some(recipe => recipe.recipe_id === recipeId),
  );
  const selectedModelKey = route.kind === "model" && !route.unlinked
    ? route.modelKey
    : recipeModel ? modelVersionKey(recipeModel.model) : undefined;
  const boundedModels = boundedItems(modelItems, LIBRARY_MODEL_WINDOW, model => modelVersionKey(model.model), selectedModelKey);
  truncated ||= boundedModels.truncated;

  const currentUnlinked = current?.unlinked_recipes ?? [];
  const unlinkedIds = new Set(currentUnlinked.map(recipe => recipe.recipe_id));
  const mergedUnlinked = currentUnlinked.concat(next.unlinked_recipes.filter(recipe => !unlinkedIds.has(recipe.recipe_id)));
  const boundedUnlinked = boundedItems(mergedUnlinked, LIBRARY_RECIPE_WINDOW, recipe => recipe.recipe_id, recipeId);
  truncated ||= boundedUnlinked.truncated;
  return {snapshot: {
    ...(current ?? next),
    models: boundedModels.items,
    next_cursor: next.next_cursor,
    unlinked_recipes: boundedUnlinked.items,
  }, truncated};
}

function routeParent(snapshot: LibrarySnapshot, recipeId: string): RouteParent | undefined {
  for (const model of snapshot.models) {
    const recipe = model.recipes.find(item => item.recipe_id === recipeId);
    if (recipe) {
      const {recipes: _recipes, ...parent} = model;
      return {kind: "model", model: parent, recipe};
    }
  }
  const recipe = snapshot.unlinked_recipes.find(item => item.recipe_id === recipeId);
  return recipe ? {kind: "unlinked", recipe} : undefined;
}

function restoreRouteParent(snapshot: LibrarySnapshot, recipeId: string, parent: RouteParent | undefined): LibrarySnapshot {
  if (!parent || routeParent(snapshot, recipeId)) return snapshot;
  if (parent.kind === "unlinked") {
    const unlinked = boundedItems(
      snapshot.unlinked_recipes.concat(parent.recipe),
      LIBRARY_RECIPE_WINDOW,
      recipe => recipe.recipe_id,
      recipeId,
    );
    return {...snapshot, unlinked_recipes: unlinked.items};
  }

  const existing = snapshot.models.find(model => modelVersionKey(model.model) === modelVersionKey(parent.model.model));
  const restoredModel = existing
    ? {
        ...existing,
        recipes: boundedItems(
          existing.recipes.concat(parent.recipe),
          LIBRARY_RECIPE_WINDOW,
          recipe => recipe.recipe_id,
          recipeId,
        ).items,
      }
    : {...parent.model, recipes: [parent.recipe]};
  const models = existing
    ? snapshot.models.map(model => modelVersionKey(model.model) === modelVersionKey(restoredModel.model) ? restoredModel : model)
    : snapshot.models.concat(restoredModel);
  return {
    ...snapshot,
    models: boundedItems(models, LIBRARY_MODEL_WINDOW, model => modelVersionKey(model.model), modelVersionKey(restoredModel.model)).items,
  };
}

export function LibraryPage({api, path, onBusyChange, onNavigate}: {
  api: LibraryApi;
  path: string;
  onBusyChange?(busy: boolean): void;
  onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void;
}) {
  const [snapshot, setSnapshot] = useState<LibrarySnapshot>();
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<LibraryRecipeDetail>();
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [paginationError, setPaginationError] = useState("");
  const [paginationWindowed, setPaginationWindowed] = useState(false);
  const [snapshotAttempt, setSnapshotAttempt] = useState(0);
  const [detailAttempt, setDetailAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [nodeDisplayNames, setNodeDisplayNames] = useState<Record<string, string>>({});
  const [publicRecipes, setPublicRecipes] = useState<PublicRecipe[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const loadMoreController = useRef<AbortController | undefined>(undefined);
  const routeParents = useRef(new Map<string, RouteParent>());
  const heading = useRef<HTMLHeadingElement>(null);
  const route = libraryRoute(path);

  useEffect(() => {
    const fleetApi = api as LibraryApi & Partial<Pick<ControlApi, "visualFleet">>;
    if (!fleetApi.visualFleet) return;
    const controller = new AbortController();
    void fleetApi.visualFleet(controller.signal)
      .then(value => {
        if (!controller.signal.aborted) setNodeDisplayNames(Object.fromEntries(value.nodes.map(node => [node.id, nodeDisplayName(node)])));
      })
      .catch(() => {
        if (!controller.signal.aborted) setNodeDisplayNames({});
      });
    return () => controller.abort();
  }, [api]);

  const catalogSupported = typeof (api as Partial<CatalogApi>).listPublicRecipes === "function";
  useEffect(() => {
    const catalogApi = api as LibraryApi & Partial<Pick<CatalogApi, "listPublicRecipes">>;
    if (!catalogApi.listPublicRecipes) {
      setPublicRecipes([]);
      setCatalogLoading(false);
      setCatalogError("");
      return;
    }
    const controller = new AbortController();
    setPublicRecipes([]);
    setCatalogLoading(true);
    setCatalogError("");
    void (async () => {
      try {
        const value = await catalogApi.listPublicRecipes?.(controller.signal);
        if (!controller.signal.aborted && value) setPublicRecipes(value.recipes);
      } catch (value) {
        if (!controller.signal.aborted) {
          setCatalogError(value instanceof Error ? value.message.slice(0, 256) : "Unable to check recipe updates");
        }
      } finally {
        if (!controller.signal.aborted) setCatalogLoading(false);
      }
    })();
    return () => controller.abort();
  }, [api, catalogAttempt]);

  useEffect(() => {
    const controller = new AbortController();
    setError("");
    setPaginationWindowed(false);
    void api.librarySnapshot(undefined, controller.signal)
      .then(value => {
        if (controller.signal.aborted) return;
        const bounded = mergeSnapshot(undefined, value, route);
        setSnapshot(bounded.snapshot);
        setPaginationWindowed(bounded.truncated);
      })
      .catch(value => {
        if (!controller.signal.aborted) setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load Library");
      });
    return () => controller.abort();
  }, [api, snapshotAttempt]);

  useEffect(() => () => loadMoreController.current?.abort(), []);

  async function loadMore() {
    const cursor = snapshot?.next_cursor;
    if (!cursor || loadingMore) return;
    const controller = new AbortController();
    loadMoreController.current?.abort();
    loadMoreController.current = controller;
    setLoadingMore(true);
    setPaginationError("");
    try {
      const next = await api.librarySnapshot(cursor, controller.signal);
      if (!controller.signal.aborted) {
        const bounded = mergeSnapshot(snapshot, next, route);
        setSnapshot(bounded.snapshot);
        if (bounded.truncated) setPaginationWindowed(true);
      }
    } catch (value) {
      if (!controller.signal.aborted) setPaginationError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load more Library recipes");
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
      if (loadMoreController.current === controller) loadMoreController.current = undefined;
    }
  }

  const recipeId = route.kind === "recipe" ? route.recipeId : undefined;
  const refreshDetail = useCallback(async (signal: AbortSignal) => {
    if (!recipeId || signal.aborted) return;
    try {
      const value = await api.libraryRecipe(recipeId, signal);
      if (signal.aborted) return;
      setDetail(value);
      setDetailError("");
    } catch (value) {
      if (!signal.aborted) setDetailError(value instanceof Error ? value.message.slice(0, 256) : "Unable to refresh recipe authority");
    }
  }, [api, recipeId]);
  useEffect(() => {
    setDetail(undefined);
    setDetailError("");
    if (!recipeId) { setDetailLoading(false); return; }
    const controller = new AbortController();
    setDetailLoading(true);
    void api.libraryRecipe(recipeId, controller.signal)
      .then(value => { if (!controller.signal.aborted) { setDetail(value); setDetailLoading(false); } })
      .catch(value => {
        if (!controller.signal.aborted) {
          setDetailError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load recipe");
          setDetailLoading(false);
        }
      });
    return () => controller.abort();
  }, [api, detailAttempt, recipeId]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) heading.current?.focus(); });
    return () => { active = false; };
  }, [path]);

  useEffect(() => {
    if (!snapshot || route.kind !== "recipe") return;
    const parent = routeParent(snapshot, route.recipeId);
    if (!parent) return;
    routeParents.current.delete(route.recipeId);
    routeParents.current.set(route.recipeId, parent);
    while (routeParents.current.size > LIBRARY_RECIPE_WINDOW) {
      const oldest = routeParents.current.keys().next().value;
      if (oldest === undefined) break;
      routeParents.current.delete(oldest);
    }
  }, [route, snapshot]);

  const browserSnapshot = snapshot && route.kind === "recipe"
    ? restoreRouteParent(snapshot, route.recipeId, routeParents.current.get(route.recipeId))
    : snapshot;
  const modelCount = snapshot?.models.length ?? 0;
  const linkedRecipeCount = snapshot?.models.reduce((total, model) => total + model.recipes.length, 0) ?? 0;
  const unlinkedRecipeCount = snapshot?.unlinked_recipes.length ?? 0;
  const recipeCount = linkedRecipeCount + unlinkedRecipeCount;
  const updateCount = new Set(publicRecipes.flatMap(recipe => recipe.local.status === "update-available" && recipe.local.recipe_id ? [recipe.local.recipe_id] : [])).size;
  const catalogLinkedCount = new Set(publicRecipes.flatMap(recipe => recipe.local.recipe_id ? [recipe.local.recipe_id] : [])).size;

  return <div className="library-page">
    <header className="fleet-hero">
      <div>
        <p className="fleet-kicker">Model control</p>
        <h1 ref={heading} tabIndex={-1}>Library</h1>
      <div className="library-toolbar-actions">
        <a href="/library/create" className="button secondary" onClick={event => onNavigate(event, "/library/create")}>Create custom recipe</a>
        <a href="/library/import" className="button secondary" onClick={event => onNavigate(event, "/library/import")}>Import public recipe</a>
      </div>
        <p className="fleet-introduction">Choose a model, its exact recipe, and one complete placement group before reviewing any change.</p>
      </div>
    </header>
    {snapshot && <>
      <section className="library-overview" aria-label="Library summary">
        <div className="library-stat library-stat-accent" role="group" aria-label={`${modelCount} model version${modelCount === 1 ? "" : "s"}`}><span>Model versions</span><strong>{modelCount}</strong><small>Exact immutable identities</small></div>
        <div className="library-stat" role="group" aria-label={`${recipeCount} recipes`}><span>Recipes in view</span><strong>{recipeCount}</strong><small>{paginationWindowed ? "Bounded loaded window" : "Available locally"}</small></div>
        <div className="library-stat" role="group" aria-label={`${linkedRecipeCount} linked`}><span>Linked recipes</span><strong>{linkedRecipeCount}</strong><small>Ready to choose a model</small></div>
        <div className={`library-stat${unlinkedRecipeCount > 0 ? " library-stat-warning" : ""}`} role="group" aria-label={`${unlinkedRecipeCount} needs a model version`}><span>Needs model version</span><strong>{unlinkedRecipeCount}</strong><small>{unlinkedRecipeCount > 0 ? "Review before install" : "Everything has an exact model"}</small></div>
        {catalogSupported && <div className={`library-stat${updateCount > 0 ? " library-stat-update" : ""}`} role="group" aria-label={catalogLoading ? "Checking catalog updates" : catalogError ? "Catalog update check unavailable" : `${updateCount} catalog update${updateCount === 1 ? "" : "s"} available`}><span>Catalog updates</span><strong>{catalogLoading ? "…" : catalogError ? "—" : updateCount}</strong><small>{catalogLoading ? "Checking immutable releases" : catalogError ? "Check unavailable" : updateCount > 0 ? "Across all catalog-linked recipes" : catalogLinkedCount > 0 ? `${catalogLinkedCount} local recipe${catalogLinkedCount === 1 ? "" : "s"} checked` : "No local catalog links"}</small></div>}
      </section>
      <div className="library-toolbar">
        <label className="library-search">
          <span>Find a model or recipe</span>
          <input type="search" aria-label="Search Library" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search names, slugs, capabilities…" />
        </label>
        <div className="library-search-meta" aria-live="polite">
          {query.trim() ? <><span>Filtering the loaded window</span><button type="button" className="button secondary" onClick={() => setQuery("")}>Clear Library search</button></> : <span>Browse exact versions and their accepted recipes</span>}
        </div>
      </div>
    </>}
    {error && <section className="fleet-error" role="alert"><h3>Library unavailable</h3><p>{error}</p><button type="button" onClick={() => setSnapshotAttempt(value => value + 1)}>Retry Library</button></section>}
    {!error && !snapshot && <section className="fleet-loading" role="status" aria-label="Loading Library"><span className="loading-orb" aria-hidden="true"/><div><h3>Opening Library</h3><p>Loading model, recipe, and placement authority…</p></div></section>}
    {snapshot && snapshot.models.length === 0 && snapshot.unlinked_recipes.length === 0 && <section className="fleet-empty library-empty" aria-label="Empty Library">
      <div className="library-empty-visual" aria-hidden="true"><span/><span/><span/></div>
      <h3>Bring your first recipe into the Library</h3>
      <p>Start with a reviewed public recipe, or create a custom runtime when your model needs a bespoke setup.</p>
      <div className="button-row">
        <a href="/library/import" className="button" onClick={event => onNavigate(event, "/library/import")}>Browse public recipes</a>
        <a href="/library/create" className="button secondary" onClick={event => onNavigate(event, "/library/create")}>Create custom recipe</a>
      </div>
    </section>}
    {browserSnapshot && (browserSnapshot.models.length > 0 || browserSnapshot.unlinked_recipes.length > 0) && <LibraryNodeNamesProvider names={nodeDisplayNames}><LibraryBrowser
        api={api}
        detail={detail}
        detailError={detailError}
        detailLoading={detailLoading}
        catalogError={catalogError}
        catalogLoading={catalogLoading}
        onNavigate={onNavigate}
        onBusyChange={onBusyChange}
        onRefresh={refreshDetail}
        onRetryDetail={() => setDetailAttempt(value => value + 1)}
        onRetryCatalog={() => setCatalogAttempt(value => value + 1)}
        publicRecipes={publicRecipes}
        query={query}
        route={route}
        snapshot={browserSnapshot}
        windowed={paginationWindowed}
      /></LibraryNodeNamesProvider>}
    {snapshot && (snapshot.next_cursor || paginationWindowed) && <div className="library-pagination">
      {paginationWindowed && <p role="status" aria-label="Bounded Library window">Showing up to {LIBRARY_RECIPE_WINDOW} recipes per list and {LIBRARY_MODEL_WINDOW} models. Earlier loaded rows leave this bounded window; selected context stays pinned. {snapshot.next_cursor ? "More server pages remain." : "No more server pages remain."}</p>}
      {snapshot.next_cursor && <button type="button" className="button secondary" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "Loading more recipes…" : "Load more Library recipes"}</button>}
      {paginationError && <p role="alert">{paginationError}</p>}
    </div>}
  </div>;
}
