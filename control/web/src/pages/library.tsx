import {useCallback, useEffect, useMemo, useRef, useState} from "react";
import type {MouseEvent} from "react";
import type {ControlApi, LibraryViewRecipe, LibraryViewRecipeDetail, LibraryViewSnapshot, ModelLibrary, RecipeDetail, RecipeLibrary, VisualFleetSnapshot} from "../api/types";
import {LibraryBrowser} from "../components/library-browser";
import type {LibrarySubview} from "../components/library-browser";
import {LibraryNodeNamesProvider} from "../components/library-node-names";
import {nodeDisplayName} from "../lib/fleet";
import {libraryRoute} from "../lib/library-route";
import type {LibraryRoute} from "../lib/library-route";
import "./library.css";

function subview(path: string): LibrarySubview {
  const url = new URL(path, location.origin);
  if (url.pathname === "/library/cache") return "models";
  if (url.pathname === "/library/profiles") return "profiles";
  const view = url.searchParams.get("view");
  return view === "models" || view === "profiles" ? view : "recipes";
}
function tabPath(path: string, view: LibrarySubview): string {
  const url = new URL(path, location.origin);
  url.pathname = view === "profiles" ? "/library/profiles" : "/library";
  if (view === "models") url.searchParams.set("view", "models"); else url.searchParams.delete("view");
  return `${url.pathname}${url.search}`;
}

function viewRecipe(recipe: RecipeLibrary["recipes"][number]): LibraryViewRecipe {
  return {
    capabilities: recipe.usage,
    content_sha256: recipe.identity.content_sha256,
    description: recipe.identity.description,
    recipe_document: recipe.document,
    recipe_id: recipe.identity.recipe_id,
    recipe_revision_id: recipe.identity.recipe_revision_id,
    publisher: recipe.identity.publisher,
    slug: recipe.identity.slug,
    title: recipe.identity.title,
    topology_name: recipe.document.topology.name,
  };
}

function viewModel(model: ModelLibrary["models"][number], recipes: LibraryViewRecipe[]): LibraryViewSnapshot["models"][number] {
  return {
    model: {kind: "model", publisher: model.identity.publisher, slug: model.identity.slug, content_sha256: model.identity.content_sha256},
    model_document: model.document,
    model_capabilities: model.document.capabilities,
    local: model.local,
    family: model.family,
    version: model.version,
    quantization: model.quantization,
    usage: model.usage,
    recipes,
  };
}

async function loadAll<T extends {next_cursor: string | null}>(load: (cursor: string | undefined) => Promise<T>, signal: AbortSignal): Promise<T[]> {
  const pages: T[] = [];
  let cursor: string | undefined;
  const seen = new Set<string>();
  do {
    const page = await load(cursor);
    pages.push(page);
    cursor = page.next_cursor ?? undefined;
    if (cursor) { if (seen.has(cursor)) throw new Error("Library pagination cursor repeated"); seen.add(cursor); }
  } while (cursor && !signal.aborted);
  return pages;
}

export async function loadLibraryView(api: ControlApi, signal: AbortSignal): Promise<LibraryViewSnapshot> {
  const [modelPages, recipePages] = await Promise.all([
    loadAll(cursor => api.modelLibrary(cursor, signal), signal),
    loadAll(cursor => api.recipeLibrary(cursor, signal), signal),
  ]);
  const models = modelPages.flatMap(page => page.models);
  const recipes = recipePages.flatMap(page => page.recipes).map(viewRecipe);
  const modelRecipes = new Map<string, LibraryViewRecipe[]>();
  for (const recipe of recipes) {
    const source = recipePages.flatMap(page => page.recipes).find(item => item.identity.recipe_id === recipe.recipe_id && item.identity.recipe_revision_id === recipe.recipe_revision_id);
    for (const selector of source?.model_selectors ?? []) modelRecipes.set(selector, [...(modelRecipes.get(selector) ?? []), recipe]);
  }
  const matched = new Set([...modelRecipes.values()].flat().map(recipe => recipe.recipe_revision_id));
  return {
    schema_version: 2,
    generated_at: modelPages[0]?.generated_at ?? recipePages[0]?.generated_at ?? new Date().toISOString(),
    freshness_policy: modelPages[0]?.freshness_policy ?? recipePages[0]?.freshness_policy!,
    models: models.map(model => viewModel(model, modelRecipes.get(model.selector) ?? [])),
    unlinked_recipes: recipes.filter(recipe => !matched.has(recipe.recipe_revision_id)),
  };
}

function viewRecipeDetail(detail: RecipeDetail): LibraryViewRecipeDetail {
  const recipe: LibraryViewRecipe = {
    capabilities: detail.usage,
    content_sha256: detail.identity.content_sha256,
    description: detail.identity.description,
    recipe_document: detail.document,
    recipe_id: detail.identity.recipe_id,
    recipe_revision_id: detail.identity.recipe_revision_id,
    publisher: detail.identity.publisher,
    slug: detail.identity.slug,
    title: detail.identity.title,
    topology_name: detail.document.topology.name,
  };
  return {schema_version: 2, generated_at: detail.updated_at, definition: detail.document, recipe, model_documents: detail.model_documents, operational_state: {builds: [], installations: [], mappings: [], runs: []}, placement: [], reasons: [], topology: detail.document.topology};
}

export function LibraryPage({api, onBusyChange, onNavigate, onNavigatePath, path}: {api: ControlApi; path: string; onBusyChange?(busy: boolean): void; onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void; onNavigatePath?(path: string, replace?: boolean): void}) {
  const [snapshot, setSnapshot] = useState<LibraryViewSnapshot>();
  const [fleet, setFleet] = useState<VisualFleetSnapshot>();
  const [error, setError] = useState("");
  const [fleetError, setFleetError] = useState("");
  const [detail, setDetail] = useState<LibraryViewRecipeDetail>();
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
    void loadLibraryView(api, controller.signal).then(value => { if (!controller.signal.aborted) setSnapshot(value); }).catch(value => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load Library"); });
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
    void api.recipeDetail(recipeId, controller.signal).then(value => { if (!controller.signal.aborted) { setDetail(viewRecipeDetail(value)); setDetailLoading(false); } }).catch(value => { if (!controller.signal.aborted) { setDetailError(value instanceof Error ? value.message.slice(0, 256) : "Unable to load Recipe detail"); setDetailLoading(false); } });
    return () => controller.abort();
  }, [api, detailAttempt, route]);
  useEffect(() => {
    if (!snapshot || route.kind !== "model" || snapshot.models.some(model => `${model.model.publisher}/${model.model.slug}@${model.model.content_sha256}` === route.modelKey)) return;
    onNavigatePath?.("/library", true);
  }, [onNavigatePath, route, snapshot]);
  useEffect(() => { if (route.kind === "model" && typeof window !== "undefined" && window.innerWidth <= 760) return; queueMicrotask(() => heading.current?.focus()); }, [path, route.kind]);
  const updateQuery = useCallback((value: string) => { setQuery(value); if (!onNavigatePath) return; const url = new URL(path, location.origin); if (value) url.searchParams.set("q", value); else url.searchParams.delete("q"); onNavigatePath(`${url.pathname}${url.search}`, true); }, [onNavigatePath, path]);
  const contextualNavigate = useCallback((event: MouseEvent<HTMLAnchorElement>, nextPath: string) => { if (!preferredNodeId || !nextPath.startsWith("/library")) return onNavigate(event, nextPath); const url = new URL(nextPath, location.origin); url.searchParams.set("spark", preferredNodeId); onNavigate(event, `${url.pathname}${url.search}`); }, [onNavigate, preferredNodeId]);
  const refresh = useCallback(async (signal: AbortSignal) => { if (route.kind === "recipe") await api.recipeDetail(route.recipeId, signal).then(value => { if (!signal.aborted) setDetail(viewRecipeDetail(value)); }); if (!signal.aborted) { setAttempt(value => value + 1); setFleetAttempt(value => value + 1); } }, [api, route]);
  const names = nodeNames;
  return <div className="library-page">
    <header className="library-command-header"><div className="library-command-title"><h1 ref={heading} tabIndex={-1}>Library</h1><p>Choose a Model. Pair it with an exact Recipe, then run it on your Sparks.</p></div></header>
    {preferredNodeId && <aside className="library-spark-context" aria-label={`Managing Models on ${names[preferredNodeId] ?? preferredNodeId}`}><strong>{names[preferredNodeId] ?? preferredNodeId}</strong><span>Choose a compatible Recipe for this Spark.</span><a className="button secondary" href="/library" onClick={event => onNavigate(event, "/library")}>Exit Spark workspace</a></aside>}
    <nav className="library-subnav" aria-label="Library sections">{(["models", "recipes", "profiles"] as const).map(item => <a key={item} className={view === item ? "is-active" : undefined} aria-current={view === item ? "page" : undefined} href={tabPath(path, item)} onClick={event => onNavigate(event, tabPath(path, item))}>{item[0]!.toUpperCase() + item.slice(1)}</a>)}</nav>
    {error && <div className="library-error" role="alert"><span>{error}</span><button type="button" className="button secondary" onClick={() => setAttempt(value => value + 1)}>Retry Library</button></div>}
    {fleetError && <div className="library-error" role="status"><span>{fleetError}</span><button type="button" className="button secondary" onClick={() => setFleetAttempt(value => value + 1)}>Retry Sparks</button></div>}
    {snapshot && <LibraryNodeNamesProvider names={names}><LibraryBrowser api={api} detail={detail} detailError={detailError} detailLoading={detailLoading} fleet={fleet} onBusyChange={onBusyChange} onNavigate={contextualNavigate} onNavigatePath={onNavigatePath} onQueryChange={updateQuery} onRefresh={refresh} onRetryDetail={() => setDetailAttempt(value => value + 1)} path={path} query={query} route={route} snapshot={snapshot} subview={view}/></LibraryNodeNamesProvider>}
  </div>;
}
