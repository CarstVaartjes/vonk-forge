import {useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {MouseEvent} from "react";
import type {ControlApi, LibraryApi, LibraryModel, LibraryRecipeDetail, LibrarySnapshot, ManagedCatalogSyncSummary, PublicRecipe, VisualFleetSnapshot} from "../api/types";
import {LibraryBrowser} from "../components/library-browser";
import type {LibrarySubview} from "../components/library-browser";
import {LibraryNodeNamesProvider} from "../components/library-node-names";
import {nodeDisplayName} from "../lib/fleet";
import {libraryRoute, modelVersionKey} from "../lib/library-route";
import type {LibraryRoute} from "../lib/library-route";
import "./library.css";

const LIBRARY_RECIPE_WINDOW = 50;
const LIBRARY_REPOSITORY_REFRESH_MS = 60_000;

type RouteParent =
  | {kind: "model"; model: Omit<LibraryModel, "recipes">; recipe: LibrarySnapshot["models"][number]["recipes"][number]};

type ManagedCatalogSyncApi = {
  managedRecipeCatalogSyncStatus?(signal?: AbortSignal): Promise<ManagedCatalogSyncSummary>;
};

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
  return {snapshot: {
    ...(current ?? next),
    models: modelItems,
    next_cursor: next.next_cursor,
    unlinked_recipes: next.unlinked_recipes,
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
  return undefined;
}

function restoreRouteParent(snapshot: LibrarySnapshot, recipeId: string, parent: RouteParent | undefined): LibrarySnapshot {
  if (!parent || routeParent(snapshot, recipeId)) return snapshot;
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
    models,
  };
}

function librarySubview(path: string): LibrarySubview {
  const parsed = new URL(path, location.origin);
  if (parsed.pathname === "/library/cache") return "cache";
  if (parsed.pathname === "/library/profiles") return "profiles";
  const value = parsed.searchParams.get("view");
  return value === "models" || value === "cache" || value === "profiles" || value === "recipes" ? value : "recipes";
}

function libraryTabPath(path: string, view: LibrarySubview): string {
  const parsed = new URL(path, location.origin);
  parsed.pathname = view === "cache" ? "/library/cache" : view === "profiles" ? "/library/profiles" : "/library";
  parsed.searchParams.delete("view");
  if (view !== "recipes") parsed.searchParams.set("view", view);
  return `${parsed.pathname}${parsed.search}`;
}

export function LibraryPage({api, onBusyChange, onNavigate, onNavigatePath, path}: {
  api: ControlApi;
  path: string;
  onBusyChange?(busy: boolean): void;
  onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void;
  onNavigatePath?(path: string, replace?: boolean): void;
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
  const initialQuery = new URL(path, location.origin).searchParams.get("q") ?? "";
  const [query, setQuery] = useState(initialQuery);
  const [nodeDisplayNames, setNodeDisplayNames] = useState<Record<string, string>>({});
  const [fleet, setFleet] = useState<VisualFleetSnapshot>();
  const [fleetError, setFleetError] = useState("");
  const [fleetAttempt, setFleetAttempt] = useState(0);
  const [publicRecipes, setPublicRecipes] = useState<PublicRecipe[]>([]);
  const [catalogRepository, setCatalogRepository] = useState("");
  const [catalogCommit, setCatalogCommit] = useState("");
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const [syncError, setSyncError] = useState("");
  const [syncSummary, setSyncSummary] = useState<ManagedCatalogSyncSummary>();
  const loadMoreController = useRef<AbortController | undefined>(undefined);
  const routeParents = useRef(new Map<string, RouteParent>());
  const heading = useRef<HTMLHeadingElement>(null);
  const parsedPath = new URL(path, location.origin);
  const route = libraryRoute(parsedPath.pathname);
  const subview = librarySubview(path);
  const preferredNodeId = parsedPath.searchParams.get("spark") ?? undefined;
  const preferredNodeName = preferredNodeId ? nodeDisplayNames[preferredNodeId] ?? preferredNodeId : undefined;
  useEffect(() => {
    setQuery(new URL(path, location.origin).searchParams.get("q") ?? "");
  }, [path]);
  const updateQuery = useCallback((value: string) => {
    setQuery(value);
    if (!onNavigatePath) return;
    const next = new URL(path, location.origin);
    if (value) next.searchParams.set("q", value); else next.searchParams.delete("q");
    onNavigatePath(`${next.pathname}${next.search}`, true);
  }, [onNavigatePath, path]);
  const contextualNavigate = useCallback((event: MouseEvent<HTMLAnchorElement>, nextPath: string) => {
    if (!preferredNodeId || !nextPath.startsWith("/library")) {
      onNavigate(event, nextPath);
      return;
    }
    const next = new URL(nextPath, location.origin);
    next.searchParams.set("spark", preferredNodeId);
    onNavigate(event, `${next.pathname}${next.search}`);
  }, [onNavigate, preferredNodeId]);

  useEffect(() => {
    const fleetApi = api as LibraryApi & Partial<Pick<ControlApi, "visualFleet">>;
    if (!fleetApi.visualFleet) {
      setFleet(undefined);
      setFleetError("");
      return;
    }
    const controller = new AbortController();
    setFleetError("");
    void fleetApi.visualFleet(controller.signal)
      .then(value => {
        if (!controller.signal.aborted) {
          setFleet(value);
          setNodeDisplayNames(Object.fromEntries(value.nodes.map(node => [node.id, nodeDisplayName(node)])));
        }
      })
      .catch(value => {
        if (!controller.signal.aborted) {
          setFleet(undefined);
          setFleetError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load live Spark state");
          setNodeDisplayNames({});
        }
      });
    return () => controller.abort();
  }, [api, fleetAttempt]);

  const catalogSupported = typeof api.listPublicRecipes === "function";
  useEffect(() => {
    const catalogApi = api;
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
        if (!controller.signal.aborted && value) {
          setPublicRecipes(value.recipes);
          setCatalogRepository(value.repository);
          setCatalogCommit(value.commit);
        }
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
    const refreshWhenVisible = () => {
      if (document.visibilityState !== "visible") return;
      setCatalogAttempt(value => value + 1);
      setSnapshotAttempt(value => value + 1);
    };
    const timer = window.setInterval(refreshWhenVisible, LIBRARY_REPOSITORY_REFRESH_MS);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    window.addEventListener("focus", refreshWhenVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
      window.removeEventListener("focus", refreshWhenVisible);
    };
  }, []);

  const managedSyncApi = api as LibraryApi & ManagedCatalogSyncApi;
  const syncStatusAvailable = typeof managedSyncApi.managedRecipeCatalogSyncStatus === "function";
  useEffect(() => {
    if (!syncStatusAvailable || !managedSyncApi.managedRecipeCatalogSyncStatus) return;
    const controller = new AbortController();
    let timer = 0;
    async function refreshStatus() {
      try {
        const value = await managedSyncApi.managedRecipeCatalogSyncStatus?.(controller.signal);
        if (!value || controller.signal.aborted) return;
        setSyncSummary(value);
        setSyncError("");
        if (value.state === "syncing") timer = window.setTimeout(() => void refreshStatus(), 2000);
      } catch (value) {
        if (controller.signal.aborted) return;
        const detail = value instanceof Error ? value.message.slice(0, 256) : "Unable to read automatic sync status";
        if (!/returned 404|sync_not_found/i.test(detail)) setSyncError(`Automatic sync status unavailable: ${detail}`);
      }
    }
    void refreshStatus();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [managedSyncApi, syncStatusAvailable]);

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
  const refreshLibraryAuthority = useCallback(async (signal: AbortSignal) => {
    await refreshDetail(signal);
    if (signal.aborted) return;
    setSnapshotAttempt(value => value + 1);
    setFleetAttempt(value => value + 1);
  }, [refreshDetail]);
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
  const repositoryRecipeIds = new Set(publicRecipes.flatMap(recipe => recipe.local.recipe_id ? [recipe.local.recipe_id] : []));
  const modelCount = snapshot?.models.length ?? 0;
  const linkedRecipeCount = snapshot?.models.reduce((total, model) => total + model.recipes.filter(recipe => repositoryRecipeIds.has(recipe.recipe_id)).length, 0) ?? 0;
  const recipeCount = publicRecipes.length;
  const catalogLinkedCount = repositoryRecipeIds.size;

  return <div className="library-page">
    <header className="library-command-header">
      <div className="library-command-title">
        <h1 ref={heading} tabIndex={-1}>Library</h1>
        <p>Choose a model. Run it on your Sparks.</p>
      </div>
    </header>
    {preferredNodeId && <aside className="library-spark-context" aria-label={`Managing models on ${preferredNodeName}`}>
      <div><span>Individual Spark workspace</span><strong>{preferredNodeName}</strong><p>Choose a model and recipe. Compatible placement groups containing this Spark are selected first, then the Controller runs the selected group and reports durable progress.</p></div>
      <a className="button secondary" href="/library" onClick={event => onNavigate(event, "/library")}>Exit Spark workspace</a>
    </aside>}
    <nav className="library-subnav" aria-label="Library sections">
      {(["models", "recipes", "cache", "profiles"] as const).map(view => <a key={view} className={subview === view ? "is-active" : undefined} aria-current={subview === view ? "page" : undefined} href={libraryTabPath(path, view)} onClick={event => onNavigate(event, libraryTabPath(path, view))}>{view === "models" ? "Models" : view === "recipes" ? "Recipes" : view === "cache" ? "NAS cache" : "Profiles"}</a>)}
    </nav>
    {snapshot && subview === "recipes" && <>
      <section className="library-command-bar" aria-label="Library command bar">
        <div className="library-overview" role="region" aria-label="Library summary">
          <div className="library-stat library-stat-accent" role="group" aria-label={`${modelCount} model version${modelCount === 1 ? "" : "s"}`}><strong>{modelCount}</strong><span>models</span></div>
          <div className="library-stat" role="group" aria-label={`${recipeCount} recipes`}><strong>{recipeCount}</strong><span>{paginationWindowed ? "recipes shown" : "recipes"}</span></div>
          <div className="library-stat" role="group" aria-label={`${linkedRecipeCount} linked`}><strong>{linkedRecipeCount}</strong><span>linked</span></div>
          {catalogSupported && <div className="library-stat" role="group" aria-label={catalogLoading ? "Refreshing repository recipes" : catalogError ? "Repository unavailable" : `${publicRecipes.length} repository recipes`} title={catalogLoading ? "Refreshing repository recipes" : catalogError ? "Repository unavailable" : `${catalogLinkedCount} recipes available locally`}><strong>{catalogLoading ? "…" : catalogError ? "—" : publicRecipes.length}</strong><span>repository</span></div>}
        </div>
        <label className="library-search">
          <span className="visually-hidden">Find a model or recipe</span>
          <input type="search" aria-label="Search Library" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search names, slugs, capabilities…" />
        </label>
        <div className="library-search-meta" aria-live="polite">
          {query.trim() ? <><span>Filtering recipe rows</span><button type="button" className="button secondary" onClick={() => setQuery("")}>Clear Library search</button></> : <span>{publicRecipes.length || recipeCount} recipe{(publicRecipes.length || recipeCount) === 1 ? "" : "s"} ready to inspect</span>}
        </div>
      </section>
    </>}
    {error && <section className="fleet-error" role="alert"><h3>Library unavailable</h3><p>{error}</p><button type="button" onClick={() => setSnapshotAttempt(value => value + 1)}>Retry Library</button></section>}
    {!error && !snapshot && <section className="fleet-loading" role="status" aria-label="Loading Library"><span className="loading-orb" aria-hidden="true"/><div><h3>Opening Library</h3><p>Loading model, recipe, and placement authority…</p></div></section>}
    {snapshot && !catalogLoading && !catalogError && publicRecipes.length === 0 && <section className="fleet-empty library-empty" aria-label="Empty Library">
      <div className="library-empty-visual" aria-hidden="true"><span/><span/><span/></div>
      <h3>No recipes available</h3>
      <p>The repository does not currently expose an authored recipe. Add one in vonk-forge-recipes, then refresh the repository view.</p>
      <button type="button" className="button" onClick={() => setCatalogAttempt(value => value + 1)}>Refresh repository</button>
    </section>}
    {browserSnapshot && (subview !== "recipes" || catalogLoading || catalogError || publicRecipes.length > 0) && <LibraryNodeNamesProvider names={nodeDisplayNames}><LibraryBrowser
        api={api}
        detail={detail}
        detailError={detailError}
        detailLoading={detailLoading}
        catalogError={catalogError}
        catalogLoading={catalogLoading}
        catalogRepository={catalogRepository}
        catalogCommit={catalogCommit}
        fleet={fleet}
        fleetError={fleetError}
        onNavigate={contextualNavigate}
        onNavigatePath={onNavigatePath}
        onQueryChange={updateQuery}
        onBusyChange={onBusyChange}
        onRefresh={refreshLibraryAuthority}
        onRetryDetail={() => setDetailAttempt(value => value + 1)}
        onRetryCatalog={() => setCatalogAttempt(value => value + 1)}
        onRetryFleet={() => setFleetAttempt(value => value + 1)}
        path={path}
        publicRecipes={publicRecipes}
        preferredNodeId={preferredNodeId}
        query={query}
        route={route}
        snapshot={browserSnapshot}
        subview={subview}
        syncError={syncError}
        syncSummary={syncSummary}
        windowed={paginationWindowed}
      /></LibraryNodeNamesProvider>}
    {snapshot && (snapshot.next_cursor || paginationWindowed) && <div className="library-pagination">
      {paginationWindowed && <p role="status" aria-label="Bounded Library window">Showing up to {LIBRARY_RECIPE_WINDOW} recipes per model. The canonical model inventory remains available while recipe rows stay bounded. {snapshot.next_cursor ? "More server pages remain." : "No more server pages remain."}</p>}
      {snapshot.next_cursor && <button type="button" className="button secondary" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "Loading more recipes…" : "Load more Library recipes"}</button>}
      {paginationError && <p role="alert">{paginationError}</p>}
    </div>}
  </div>;
}
