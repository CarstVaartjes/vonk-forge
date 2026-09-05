import {useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {LibraryApi, LibraryRecipeDetail, LibrarySnapshot, ManagedCatalogSyncSummary, PublicRecipe, VisualFleetSnapshot} from "../api/types";
import type {LibraryRoute} from "../lib/library-route";
import {LibraryRecipeAuthority} from "./library-recipe-detail";
import {EMPTY_LIBRARY_WORKCELL_FILTERS, LibraryWorkcell} from "./library-workcell";

type Navigate = (event: MouseEvent<HTMLAnchorElement>, path: string) => void;

export function LibraryBrowser({api, catalogCommit, catalogError, catalogLoading, catalogRepository, detail, detailError, detailLoading, fleet, fleetError, onBusyChange, onNavigate, onRefresh, onRetryCatalog, onRetryDetail, onRetryFleet, preferredNodeId, publicRecipes, query, route, snapshot, syncError, syncSummary, windowed}: {
  api: LibraryApi;
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
  onRefresh(signal: AbortSignal): Promise<void>;
  onRetryCatalog(): void;
  onRetryDetail(): void;
  onRetryFleet(): void;
  preferredNodeId?: string;
  publicRecipes: PublicRecipe[];
  query: string;
  route: LibraryRoute;
  snapshot: LibrarySnapshot;
  syncError: string;
  syncSummary?: ManagedCatalogSyncSummary;
  windowed: boolean;
}) {
  const [filters, setFilters] = useState(EMPTY_LIBRARY_WORKCELL_FILTERS);
  const publicByLocalRecipe = useMemo(() => new Map(publicRecipes.flatMap(item => item.local.recipe_id ? [[item.local.recipe_id, item] as const] : [])), [publicRecipes]);

  return <div className="library-browser-shell">
    {catalogLoading && <p className="library-catalog-state" role="status">Refreshing recipes from the repository…</p>}
    {catalogError && <div className="library-catalog-state is-error" role="alert"><span>Repository catalog unavailable: {catalogError}</span><button type="button" className="button secondary" onClick={onRetryCatalog}>Retry repository</button></div>}
    <LibraryWorkcell
      api={api}
      catalogCommit={catalogCommit}
      catalogRepository={catalogRepository}
      detail={detail}
      detailContent={route.kind === "recipe" && detail ? <LibraryRecipeAuthority api={api} catalogRecipe={publicByLocalRecipe.get(detail.recipe.recipe_id)} detail={detail} onBusyChange={onBusyChange} onRefresh={onRefresh} policy={snapshot.freshness_policy} preferredNodeId={preferredNodeId}/> : undefined}
      detailError={detailError}
      detailLoading={detailLoading}
      fleet={fleet}
      fleetError={fleetError}
      filters={filters}
      onBusyChange={onBusyChange}
      onFiltersChange={setFilters}
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
    />
  </div>;
}
