import {useCallback, useEffect, useRef, useState} from "react";
import type {MouseEvent} from "react";
import type {LibraryApi, LibrarySnapshot} from "../api/types";
import type {LibraryRecipeDetail} from "../api/types";
import {LibraryBrowser} from "../components/library-browser";
import {libraryRoute} from "../lib/library-route";

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
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void api.librarySnapshot(undefined, controller.signal)
      .then(setSnapshot)
      .catch(value => {
        if (!controller.signal.aborted) setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load Library");
      });
    return () => controller.abort();
  }, [api]);

  const route = libraryRoute(path);
  const recipeId = route.kind === "recipe" ? route.recipeId : undefined;
  const refreshDetail = useCallback(async () => {
    if (!recipeId) return;
    try {
      const value = await api.libraryRecipe(recipeId);
      setDetail(value);
      setDetailError("");
    } catch (value) {
      setDetailError(value instanceof Error ? value.message.slice(0, 256) : "Unable to refresh recipe authority");
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
  </div>;
}
