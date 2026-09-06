import {useMemo, useState} from "react";
import type {MouseEvent} from "react";
import type {ControlApi, LibraryRecipeDetail, LibrarySnapshot, VisualFleetSnapshot} from "../api/types";
import type {LibraryRoute} from "../lib/library-route";
import {LibraryCacheView} from "./library-cache-view";
import {LibraryModelsView} from "./library-models-view";
import {LibraryProfilesView} from "./library-profiles-view";
import {LibraryRecipeAuthority} from "./library-recipe-detail";
import {buildLibraryRecipeRecords, EMPTY_LIBRARY_WORKCELL_FILTERS, libraryFiltersFromSearch, libraryFiltersToSearch, LibraryWorkcell} from "./library-workcell";

export type LibrarySubview = "models" | "cache" | "profiles" | "recipes";

export function LibraryBrowser({api, detail, detailError, detailLoading, fleet, onBusyChange, onNavigate, onNavigatePath, onQueryChange, onRefresh, onRetryDetail, path, query, route, snapshot, subview}: {
  api: ControlApi; detail?: LibraryRecipeDetail; detailError: string; detailLoading: boolean; fleet?: VisualFleetSnapshot; onBusyChange?(busy: boolean): void; onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void; onNavigatePath?(path: string, replace?: boolean): void; onQueryChange(value: string): void; onRefresh(signal: AbortSignal): Promise<void>; onRetryDetail(): void; path: string; query: string; route: LibraryRoute; snapshot: LibrarySnapshot; subview: LibrarySubview;
}) {
  const [filters, setFilters] = useState(() => libraryFiltersFromSearch(new URL(path, location.origin).searchParams));
  const records = useMemo(() => buildLibraryRecipeRecords(snapshot), [snapshot]);
  function updateFilters(next: typeof filters) {
    setFilters(next);
    if (!onNavigatePath) return;
    const url = new URL(path, location.origin);
    onNavigatePath(`${url.pathname}?${libraryFiltersToSearch(next, url.searchParams).toString()}`, true);
  }
  if (subview === "cache") return <LibraryCacheView api={api} entries={records} modelInventory={snapshot.models} fleet={fleet} onBusyChange={onBusyChange} onNavigate={onNavigate} path={path}/>;
  if (subview === "profiles") return <LibraryProfilesView api={api} entries={records} fleet={fleet} onBusyChange={onBusyChange} onNavigate={onNavigate}/>;
  if (subview === "models") return <LibraryModelsView api={api} entries={records} fleet={fleet} filters={filters} modelInventory={snapshot.models} onFiltersChange={updateFilters} onNavigate={onNavigate} onNavigatePath={onNavigatePath} onQueryChange={onQueryChange} path={path} query={query}/>;
  if (route.kind === "recipe" && detail) return <LibraryRecipeAuthority api={api} detail={detail} snapshot={snapshot} onRefresh={onRefresh} onBusyChange={onBusyChange}/>;
  return <LibraryWorkcell api={api} detail={detail} detailError={detailError} detailLoading={detailLoading} fleet={fleet} filters={filters ?? EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={updateFilters} onNavigate={onNavigate} onQueryChange={onQueryChange} onRefresh={onRefresh} onRetryDetail={onRetryDetail} query={query} route={route} snapshot={snapshot}/>;
}
