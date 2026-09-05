import {useEffect, useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {ControlApi, LibraryRecipeDetail, LibrarySnapshot, ManagedCatalogSyncSummary, PublicRecipe, VisualFleetSnapshot} from "../api/types";
import type {LibraryRoute} from "../lib/library-route";
import {LibraryRecipeAuthority} from "./library-recipe-detail";
import {LibraryCacheView} from "./library-cache-view";
import {LibraryModelsView} from "./library-models-view";
import {LibraryProfilesView} from "./library-profiles-view";
import {applyManagedCatalogWithdrawals, buildLibraryRecipeRecords, libraryFiltersFromSearch, libraryFiltersToSearch, LibraryWorkcell} from "./library-workcell";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;

export type LibrarySubview = "models" | "cache" | "profiles" | "recipes";

function runSwitchController(api: ControlApi): Pick<ControlApi, "previewRecipeRunSwitch" | "applyRecipeRunSwitch" | "getRecipeRunSwitchOperation"> | undefined {
  // Browser fixture adapters may intentionally expose only the older lifecycle
  // actions. The generated production ControlApi always includes this trio.
  if (!("previewRecipeRunSwitch" in api) || !("applyRecipeRunSwitch" in api) || !("getRecipeRunSwitchOperation" in api)) return undefined;
  return api;
}

export function LibraryBrowser({api, catalogCommit, catalogError, catalogLoading, catalogRepository, detail, detailError, detailLoading, fleet, fleetError, onBusyChange, onNavigate, onNavigatePath, onQueryChange, onRefresh, onRetryCatalog, onRetryDetail, onRetryFleet, path, preferredNodeId, publicRecipes, query, route, snapshot, subview, syncError, syncSummary, windowed}: {
  api: ControlApi;
  catalogCommit?: string;
  catalogError: string;
  catalogLoading: boolean;
  catalogRepository?: string;
  detail?: LibraryRecipeDetail;
  detailError: string;
  detailLoading: boolean;
  fleet?: VisualFleetSnapshot;
  fleetError: string;
  onBusyChange?(busy: boolean): void;
  onNavigate: Navigate;
  onNavigatePath?(path: string, replace?: boolean): void;
  onQueryChange(value: string): void;
  onRefresh(signal: AbortSignal): Promise<void>;
  onRetryCatalog(): void;
  onRetryDetail(): void;
  onRetryFleet(): void;
  path: string;
  preferredNodeId?: string;
  publicRecipes: PublicRecipe[];
  query: string;
  route: LibraryRoute;
  snapshot: LibrarySnapshot;
  subview: LibrarySubview;
  syncError: string;
  syncSummary?: ManagedCatalogSyncSummary;
  windowed: boolean;
}) {
  const [filters, setFilters] = useState(() => libraryFiltersFromSearch(new URL(path, location.origin).searchParams));
  const records = useMemo(() => applyManagedCatalogWithdrawals(buildLibraryRecipeRecords(snapshot, publicRecipes), fleet, syncSummary?.withdrawn_recipes ?? []), [fleet, publicRecipes, snapshot, syncSummary?.withdrawn_recipes]);
  const selectedRecord = route.kind === "recipe" ? records.find(record => record.recipe?.recipe_id === route.recipeId) : undefined;
  const runApi = useMemo(() => runSwitchController(api), [api]);
  const publicByLocalRecipe = useMemo(() => new Map(publicRecipes.flatMap(item => item.local.recipe_id ? [[item.local.recipe_id, item] as const] : [])), [publicRecipes]);
  useEffect(() => {
    setFilters(libraryFiltersFromSearch(new URL(path, location.origin).searchParams));
  }, [path]);

  function updateFilters(next: typeof filters) {
    setFilters(next);
    if (!onNavigatePath) return;
    const nextUrl = new URL(path, location.origin);
    libraryFiltersToSearch(next, nextUrl.searchParams);
    onNavigatePath(`${nextUrl.pathname}${nextUrl.search}`, true);
  }

  return <div className="library-browser-shell">
    {catalogLoading && <p className="library-catalog-state" role="status">Refreshing recipes from the repository…</p>}
    {catalogError && <div className="library-catalog-state is-error" role="alert"><span>Repository catalog unavailable: {catalogError}</span><button type="button" className="button secondary" onClick={onRetryCatalog}>Retry repository</button></div>}
    {subview === "models" && <LibraryModelsView api={api} entries={records} fleet={fleet} filters={filters} onFiltersChange={updateFilters} onNavigate={onNavigate} onQueryChange={onQueryChange} query={query}/>}
    {subview === "cache" && <LibraryCacheView api={api} entries={records} fleet={fleet} onBusyChange={onBusyChange} onNavigate={onNavigate}/>}
    {subview === "profiles" && <LibraryProfilesView api={api} entries={records} fleet={fleet} initialCreate={new URL(path, location.origin).searchParams.get("profile") === "new"} initialProfileId={new URL(path, location.origin).searchParams.get("profile") ?? undefined} onBusyChange={onBusyChange} onNavigate={onNavigate}/>}
    {subview === "recipes" && <LibraryWorkcell
        api={api}
        catalogCommit={catalogCommit}
        catalogRepository={catalogRepository}
        detail={detail}
        detailContent={route.kind === "recipe" && detail ? <LibraryRecipeAuthority api={api} runApi={runApi} catalogRecipe={publicByLocalRecipe.get(detail.recipe.recipe_id)} detail={detail} modelVersionSha256={selectedRecord?.model?.content_sha256} nodeNames={Object.fromEntries(fleet?.nodes.map(node => [node.id, node.display_name || node.hostname]) ?? [])} onBusyChange={onBusyChange} onRefresh={onRefresh} policy={snapshot.freshness_policy} preferredNodeId={preferredNodeId}/> : undefined}
        detailError={detailError}
        detailLoading={detailLoading}
        fleet={fleet}
        fleetError={fleetError}
        filters={filters}
        onBusyChange={onBusyChange}
        onFiltersChange={updateFilters}
        onNavigate={onNavigate}
        onRefresh={onRefresh}
        onRetryDetail={onRetryDetail}
        onRetryFleet={onRetryFleet}
        publicRecipes={publicRecipes}
        query={query}
        route={route}
        snapshot={snapshot}
        syncError={syncError}
        syncSummary={syncSummary}
        windowed={windowed}
      />}
  </div>;
}
