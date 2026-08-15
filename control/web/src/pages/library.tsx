import {useCallback, useEffect, useRef, useState} from "react";
import type {MouseEvent} from "react";
import type {LibraryApi, LibrarySnapshot} from "../api/types";
import type {LibraryRecipeDetail} from "../api/types";
import {LibraryBrowser} from "../components/library-browser";
import {libraryRoute} from "../lib/library-route";
import "./library.css";

function mergeSnapshot(current: LibrarySnapshot, next: LibrarySnapshot): LibrarySnapshot {
  const models = new Map(current.models.map(model => [model.family, {...model, recipes: [...model.recipes]}]));
  for (const model of next.models) {
    const existing = models.get(model.family);
    if (!existing) {
      models.set(model.family, {...model, recipes: [...model.recipes]});
      continue;
    }
    const recipeIds = new Set(existing.recipes.map(recipe => recipe.recipe_id));
    models.set(model.family, {
      ...existing,
      recipes: existing.recipes.concat(model.recipes.filter(recipe => !recipeIds.has(recipe.recipe_id))),
    });
  }
  const unlinkedIds = new Set(current.unlinked_recipes.map(recipe => recipe.recipe_id));
  return {
    ...current,
    models: [...models.values()],
    next_cursor: next.next_cursor,
    unlinked_recipes: current.unlinked_recipes.concat(next.unlinked_recipes.filter(recipe => !unlinkedIds.has(recipe.recipe_id))),
  };
}

export function LibraryPage({api, path, onNavigate}: {
  api: LibraryApi;
  path: string;
  onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void;
}) {
  const [snapshot, setSnapshot] = useState<LibrarySnapshot>();
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<LibraryRecipeDetail>();
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [paginationError, setPaginationError] = useState("");
  const loadMoreController = useRef<AbortController | undefined>(undefined);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void api.librarySnapshot(undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted) setSnapshot(value); })
      .catch(value => {
        if (!controller.signal.aborted) setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load Library");
      });
    return () => controller.abort();
  }, [api]);

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
      if (!controller.signal.aborted) setSnapshot(current => current ? mergeSnapshot(current, next) : next);
    } catch (value) {
      if (!controller.signal.aborted) setPaginationError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load more Library recipes");
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
      if (loadMoreController.current === controller) loadMoreController.current = undefined;
    }
  }

  const route = libraryRoute(path);
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
  }, [api, recipeId]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) heading.current?.focus(); });
    return () => { active = false; };
  }, [path]);

  return <div className="library-page">
    <header className="fleet-hero">
      <div>
        <p className="fleet-kicker">Model control</p>
        <h2 ref={heading} tabIndex={-1}>Library</h2>
        <p className="fleet-introduction">Choose a model, its exact recipe, and one complete placement group before reviewing any change.</p>
      </div>
    </header>
    {error && <section className="fleet-error" role="alert"><h3>Library unavailable</h3><p>{error}</p></section>}
    {!error && !snapshot && <section className="fleet-loading" role="status" aria-label="Loading Library"><span className="loading-orb" aria-hidden="true"/><div><h3>Opening Library</h3><p>Loading model, recipe, and placement authority…</p></div></section>}
    {snapshot && snapshot.models.length === 0 && snapshot.unlinked_recipes.length === 0 && <section className="fleet-empty"><h3>No recipes in the Library</h3><p>Create or import a recipe through the advanced catalog workflow.</p><a className="button" href="/catalog" onClick={event => onNavigate(event, "/catalog")}>Open advanced catalog</a></section>}
    {snapshot && (snapshot.models.length > 0 || snapshot.unlinked_recipes.length > 0) && <LibraryBrowser
      api={api}
      detail={detail}
      detailError={detailError}
      detailLoading={detailLoading}
      onNavigate={onNavigate}
      onRefresh={refreshDetail}
      route={route}
      snapshot={snapshot}
    />}
    {snapshot?.next_cursor && <div className="library-pagination">
      <button type="button" className="button secondary" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "Loading more recipes…" : "Load more Library recipes"}</button>
      {paginationError && <p role="alert">{paginationError}</p>}
    </div>}
  </div>;
}
