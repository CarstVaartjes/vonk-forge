import {useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {MouseEvent} from "react";
import type {ControlApi, LibraryRecipeDetail, LibrarySnapshot, VisualFleetSnapshot} from "../api/types";
import {LibraryBrowser} from "../components/library-browser";
import type {LibrarySubview} from "../components/library-browser";
import {LibraryNodeNamesProvider} from "../components/library-node-names";
import {nodeDisplayName} from "../lib/fleet";
import {libraryRoute} from "../lib/library-route";
import type {LibraryRoute} from "../lib/library-route";
import "./library.css";

function subview(path: string): LibrarySubview {
  const url = new URL(path, location.origin);
  if (url.pathname === "/library/cache") return "cache";
  if (url.pathname === "/library/profiles") return "profiles";
  const view = url.searchParams.get("view");
  return view === "models" || view === "cache" || view === "profiles" ? view : "recipes";
}
function tabPath(path: string, view: LibrarySubview): string {
  const url = new URL(path, location.origin);
  url.pathname = view === "cache" ? "/library/cache" : view === "profiles" ? "/library/profiles" : "/library";
  if (view === "models") url.searchParams.set("view", "models"); else url.searchParams.delete("view");
  return `${url.pathname}${url.search}`;
}

export async function loadLibrarySnapshot(api: ControlApi, signal: AbortSignal): Promise<LibrarySnapshot> {
  let cursor: string | undefined;
  let first: LibrarySnapshot | undefined;
  const seenCursors = new Set<string>();
  const models = new Map<string, LibrarySnapshot["models"][number]>();
  const unlinked = new Map<string, LibrarySnapshot["unlinked_recipes"][number]>();
  do {
    const page = await api.librarySnapshot(cursor, signal);
    first ??= page;
    for (const model of page.models) {
      const key = `${model.model.publisher}/${model.model.slug}@${model.model.content_sha256}`;
      const existing = models.get(key);
      if (!existing) models.set(key, model);
      else {
        const recipes = new Map(existing.recipes.map(recipe => [recipe.recipe_revision_id, recipe]));
        for (const recipe of model.recipes) recipes.set(recipe.recipe_revision_id, recipe);
        models.set(key, {...existing, recipes: [...recipes.values()]});
      }
    }
    for (const recipe of page.unlinked_recipes) unlinked.set(recipe.recipe_revision_id, recipe);
    cursor = page.next_cursor ?? undefined;
    if (cursor) {
      if (seenCursors.has(cursor)) throw new Error("Library pagination cursor repeated");
      seenCursors.add(cursor);
    }
  } while (cursor);
  if (!first) throw new Error("Library returned no page");
  return {...first, models: [...models.values()], unlinked_recipes: [...unlinked.values()], next_cursor: null};
}

export function LibraryPage({api, onBusyChange, onNavigate, onNavigatePath, path}: {api: ControlApi; path: string; onBusyChange?(busy: boolean): void; onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void; onNavigatePath?(path: string, replace?: boolean): void}) {
  const [snapshot, setSnapshot] = useState<LibrarySnapshot>();
  const [fleet, setFleet] = useState<VisualFleetSnapshot>();
  const [error, setError] = useState("");
  const [fleetError, setFleetError] = useState("");
  const [detail, setDetail] = useState<LibraryRecipeDetail>();
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [fleetAttempt, setFleetAttempt] = useState(0);
  const [detailAttempt, setDetailAttempt] = useState(0);
  const [query, setQuery] = useState(() => new URL(path, location.origin).searchParams.get("q") ?? "");
  const [nodeNames, setNodeNames] = useState<Record<string, string>>({});
  const heading = useRef<HTMLHeadingElement>(null);
  const route = useMemo(() => libraryRoute(new URL(path, location.origin).pathname), [path]);
  const view = subview(path);
  const preferredNodeId = new URL(path, location.origin).searchParams.get("spark") ?? undefined;
  useEffect(() => setQuery(new URL(path, location.origin).searchParams.get("q") ?? ""), [path]);
  useEffect(() => {
    const controller = new AbortController(); setError("");
    void loadLibrarySnapshot(api, controller.signal).then(value => { if (!controller.signal.aborted) setSnapshot(value); }).catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load Library"); });
    return () => controller.abort();
  }, [api, attempt]);
  useEffect(() => {
    if (!api.visualFleet) return;
    const controller = new AbortController(); setFleetError("");
    void api.visualFleet(controller.signal).then(value => { if (!controller.signal.aborted) { setFleet(value); setNodeNames(Object.fromEntries(value.nodes.map(node => [node.id, nodeDisplayName(node)]))); } }).catch(value => { if (!controller.signal.aborted) setFleetError(value instanceof Error ? value.message : "Unable to load Spark state"); });
    return () => controller.abort();
  }, [api, fleetAttempt]);
  useEffect(() => {
    const recipeId = route.kind === "recipe" ? route.recipeId : undefined;
    if (!recipeId) { setDetail(undefined); setDetailError(""); setDetailLoading(false); return; }
    const controller = new AbortController(); setDetailLoading(true); setDetailError("");
    void api.libraryRecipe(recipeId, controller.signal).then(value => { if (!controller.signal.aborted) { setDetail(value); setDetailLoading(false); } }).catch(value => { if (!controller.signal.aborted) { setDetailError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load Recipe detail"); setDetailLoading(false); } });
    return () => controller.abort();
  }, [api, detailAttempt, route]);
  useEffect(() => {
    if (!snapshot || route.kind !== "model" || snapshot.models.some(model => `${model.model.publisher}/${model.model.slug}@${model.model.content_sha256}` === route.modelKey)) return;
    onNavigatePath?.("/library", true);
  }, [onNavigatePath, route, snapshot]);
  useEffect(() => { if (route.kind === "model" && typeof window !== "undefined" && window.innerWidth <= 760) return; queueMicrotask(() => heading.current?.focus()); }, [path, route.kind]);
  const updateQuery = useCallback((value: string) => { setQuery(value); if (!onNavigatePath) return; const url = new URL(path, location.origin); if (value) url.searchParams.set("q", value); else url.searchParams.delete("q"); onNavigatePath(`${url.pathname}${url.search}`, true); }, [onNavigatePath, path]);
  const contextualNavigate = useCallback((event: MouseEvent<HTMLAnchorElement>, nextPath: string) => { if (!preferredNodeId || !nextPath.startsWith("/library")) return onNavigate(event, nextPath); const url = new URL(nextPath, location.origin); url.searchParams.set("spark", preferredNodeId); onNavigate(event, `${url.pathname}${url.search}`); }, [onNavigate, preferredNodeId]);
  const refresh = useCallback(async (signal: AbortSignal) => { if (route.kind === "recipe") await api.libraryRecipe(route.recipeId, signal).then(value => { if (!signal.aborted) setDetail(value); }); if (!signal.aborted) { setAttempt(value => value + 1); setFleetAttempt(value => value + 1); } }, [api, route]);
  const names = nodeNames;
  return <div className="library-page">
    <header className="library-command-header"><div className="library-command-title"><h1 ref={heading} tabIndex={-1}>Library</h1><p>Choose a Model. Pair it with an exact Recipe, then run it on your Sparks.</p></div></header>
    {preferredNodeId && <aside className="library-spark-context" aria-label={`Managing Models on ${names[preferredNodeId] ?? preferredNodeId}`}><strong>{names[preferredNodeId] ?? preferredNodeId}</strong><span>Choose a compatible Recipe for this Spark.</span><a className="button secondary" href="/library" onClick={event => onNavigate(event, "/library")}>Exit Spark workspace</a></aside>}
    <nav className="library-subnav" aria-label="Library sections">{(["models", "recipes", "cache", "profiles"] as const).map(item => <a key={item} className={view === item ? "is-active" : undefined} aria-current={view === item ? "page" : undefined} href={tabPath(path, item)} onClick={event => onNavigate(event, tabPath(path, item))}>{item === "cache" ? "NAS cache" : item[0]!.toUpperCase() + item.slice(1)}</a>)}</nav>
    {error && <div className="library-error" role="alert"><span>{error}</span><button type="button" className="button secondary" onClick={() => setAttempt(value => value + 1)}>Retry Library</button></div>}
    {fleetError && <div className="library-error" role="status"><span>{fleetError}</span><button type="button" className="button secondary" onClick={() => setFleetAttempt(value => value + 1)}>Retry Sparks</button></div>}
    {snapshot && <LibraryNodeNamesProvider names={names}><LibraryBrowser api={api} detail={detail} detailError={detailError} detailLoading={detailLoading} fleet={fleet} onBusyChange={onBusyChange} onNavigate={contextualNavigate} onNavigatePath={onNavigatePath} onQueryChange={updateQuery} onRefresh={refresh} onRetryDetail={() => setDetailAttempt(value => value + 1)} path={path} query={query} route={route} snapshot={snapshot} subview={view}/></LibraryNodeNamesProvider>}
  </div>;
}
