import { compareWire, formatWire, type WireNumber } from "../api/contract-numeric";
import type { VisualFleetSnapshot } from "../api/types";
import { freeMemoryText, summarizeFleet, type FleetSummary } from "../lib/fleet";
import { useCapabilityWords } from "./library-vocabulary";
import type { LibraryRecipeRecord, LibraryWorkcellFilters } from "./library-workcell";
import {
  filterLibraryRecipeRecords,
  fleetFitRank,
  LIBRARY_RECENCY_LABELS,
  LIBRARY_RECENCY_VALUES,
  LIBRARY_SORT_LABELS,
  LIBRARY_SORTS,
  libraryRecencyFromValue,
  librarySortFromValue,
  recipeCreator,
  recipeEngine,
  recordFamily,
  recordModelKey,
  sparkLabel,
} from "./library-workcell";

// The one Library toolbar (search, segmented Spark count, Fits fleet and Cached
// only toggles, a Filters disclosure). Every control sets the same filter the
// URL and `vonkctl recipe library` use (--sparks, --family, --engine,
// --creator, --fits-fleet); the URL-backed `filters` the caller owns is the
// only state. The Spark segment matching the live fleet is marked "yours".
// Counts are the records the other filters leave; Models are counted by the
// lineage fold key. "Fits fleet" is the Controller's own placement verdict
// against fresh capacity: checking it makes the page ask for verdicts, so it
// is never disabled for want of them; unknown evidence is counted apart and
// never presented as a non-fit.

type Facet = "family" | "engine" | "creator";
// Proven fits, proven non-fits, and verdicts the Controller could not give.
const NO_VERDICTS = { fits: 0, nonFit: 0, unknown: 0 };
type FleetFitCounts = typeof NO_VERDICTS;

/** Verdicts over the recipes (or models) the other filters leave.
 *
 * A model takes the best verdict among its recipes: it fits when one recipe
 * fits, and is a non-fit only when every assessed recipe is, which is the
 * same model the `fits` filter keeps or drops. */
export function countFleetFit(
  records: LibraryRecipeRecord[],
  filters: LibraryWorkcellFilters,
  query: string,
  noun: "models" | "recipes",
): FleetFitCounts {
  const open = filterLibraryRecipeRecords(records, { ...filters, fits: false }, query).filter(
    (record) => record.recipe,
  );
  const best = new Map<string, 0 | 1 | 2>();
  for (const record of open) {
    const rank = fleetFitRank(record.recipe!);
    const key = noun === "models" ? recordModelKey(record) : record.recipe!.recipe_id;
    if (key === null) continue;
    const current = best.get(key);
    if (current === undefined || rank > current) best.set(key, rank);
  }
  const byRank = [0, 0, 0];
  for (const rank of best.values()) byRank[rank] += 1;
  return { ...NO_VERDICTS, fits: byRank[2]!, unknown: byRank[1]!, nonFit: byRank[0]! };
}
type FacetOption = { value: string; label: string; count: number };

function countBy(
  records: LibraryRecipeRecord[],
  filters: LibraryWorkcellFilters,
  query: string,
  noun: "models" | "recipes",
): number {
  const matching = filterLibraryRecipeRecords(records, filters, query);
  return noun === "models"
    ? new Set(matching.flatMap((record) => recordModelKey(record) ?? [])).size
    : new Set(matching.flatMap((record) => (record.recipe ? [record.recipe.recipe_id] : []))).size;
}

function facetOptions(
  facet: Facet,
  records: LibraryRecipeRecord[],
  filters: LibraryWorkcellFilters,
  query: string,
  noun: "models" | "recipes",
): FacetOption[] {
  const labels = new Map<string, string>();
  for (const record of records) {
    if (facet === "family") {
      // The Controller titles each Model display by its Family (the release's
      // display/families), so one Family has one label.
      const value = recordFamily(record);
      if (value) labels.set(value, record.modelDisplay?.family_title || value);
    } else if (record.recipe) {
      const value = facet === "engine" ? recipeEngine(record.recipe) : recipeCreator(record.recipe);
      if (value) labels.set(value.toLocaleLowerCase(), value);
    }
  }
  const open = { ...filters, [facet]: "" };
  return [...labels.entries()]
    .map(([value, label]) => ({
      value,
      label,
      count: countBy(records, { ...open, [facet]: value }, query, noun),
    }))
    .sort(
      (a, b) => b.count - a.count || a.label.localeCompare(b.label, undefined, { numeric: true }),
    );
}

type Option = { value: string; label: string; disabled?: boolean };

function FilterSelect({
  label,
  name,
  value,
  all,
  options,
  onChange,
}: {
  label: string;
  name: string;
  value: string;
  all: string;
  options: Option[];
  onChange(value: string): void;
}) {
  return (
    <label>
      {label}
      <select aria-label={name} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">{all}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

const sorted = (values: Iterable<string>): string[] => [...new Set(values)].sort();

/** The one filter toolbar of the Library. Every control reads and writes the
 * same URL-backed `filters` the caller owns (and `vonkctl ... library` flags
 * mirror); nothing is kept here but the open/closed state of the disclosure. */
export function LibrarySetupStrip({
  fleet,
  records,
  filters,
  query,
  noun,
  onFiltersChange,
  onQueryChange,
}: {
  fleet?: VisualFleetSnapshot;
  records: LibraryRecipeRecord[];
  filters: LibraryWorkcellFilters;
  query: string;
  noun: "models" | "recipes";
  onFiltersChange(filters: LibraryWorkcellFilters): void;
  onQueryChange(value: string): void;
}) {
  const words = useCapabilityWords();
  const summary = summarizeFleet(fleet);
  const sparkCounts = sorted(
    records.flatMap((record) =>
      record.recipe ? [formatWire(record.recipe.recipe_document.topology.node_count)] : [],
    ),
  ).sort((a, b) => Number(a) - Number(b));
  const openSparks = { ...filters, sparks: "" };
  const tiles = sparkCounts.map((value) => ({
    value,
    count: countBy(records, { ...openSparks, sparks: value }, query, noun),
    yours: summary !== undefined && summary.online > 0 && Number(value) === summary.online,
  }));
  const anyCount = countBy(records, openSparks, query, noun);
  const fit = countFleetFit(records, filters, query, noun);
  // Placement state (not qualification): only ready or blocked are verdicts; an unavailable fit is unknown.
  const assessed = fit.fits + fit.nonFit > 0;
  const fleetLine =
    summary === undefined
      ? "Fleet state is not available yet"
      : `${summary.online} of ${summary.enrolled} Spark${summary.enrolled === 1 ? "" : "s"} online` +
        (freeMemoryText(summary) === null ? "" : ` · ${freeMemoryText(summary)}`);
  const fitLine = assessed
    ? `${fit.fits} of ${fit.fits + fit.nonFit} assessed ${noun} fit now` +
      (fit.unknown > 0 ? `, ${fit.unknown} awaiting assessment` : "")
    : "Placement not assessed yet; Fits fleet asks the Controller";
  const facet = (name: Facet) => facetOptions(name, records, filters, query, noun);
  const asOptions = (options: FacetOption[]): Option[] =>
    options.map((option) => ({
      value: option.value,
      label: `${option.label} (${option.count})`,
      disabled: option.count === 0,
    }));
  const lower = (value: string) => value.toLocaleLowerCase();
  const inDisclosure = [
    filters.family,
    filters.engine,
    filters.creator,
    filters.quantization,
    filters.usage,
    filters.version,
    filters.alignment,
    filters.publisher,
    filters.model,
    filters.updated === "any" ? "" : filters.updated,
    filters.sort === "updated" ? "" : filters.sort,
  ].filter(Boolean).length;
  const set = (patch: Partial<LibraryWorkcellFilters>) => onFiltersChange({ ...filters, ...patch });
  return (
    <section className="library-toolbar" aria-label="Find models and recipes">
      <div className="library-toolbar-row">
        <label className="library-toolbar-search">
          <span className="sr-only">Find models or recipes</span>
          <input
            type="search"
            aria-label={noun === "models" ? "Search models" : "Search Library"}
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="Search names, slugs, capabilities"
          />
        </label>
        <div className="library-toolbar-sparks">
          <span aria-hidden="true">Sparks</span>
          <div className="segmented" role="group" aria-label="Sparks per recipe">
            {tiles.map((tile) => {
              const pressed = filters.sparks === tile.value;
              return (
                <button
                  key={tile.value}
                  type="button"
                  aria-pressed={pressed}
                  aria-label={`${sparkLabel(Number(tile.value))} or fewer: ${tile.count} ${noun}${tile.yours ? " (your fleet)" : ""}`}
                  data-fleet={tile.yours ? "yours" : undefined}
                  disabled={tile.count === 0 && !pressed}
                  onClick={() => set({ sparks: pressed ? "" : tile.value })}
                >
                  {tile.value}
                  {tile.yours && <small aria-hidden="true">yours</small>}
                  <small className="segment-count" aria-hidden="true">
                    {tile.count}
                  </small>
                </button>
              );
            })}
            <button
              type="button"
              aria-pressed={filters.sparks === ""}
              aria-label={`Any Spark count: ${anyCount} ${noun}`}
              onClick={() => set({ sparks: "" })}
            >
              Any
              <small className="segment-count" aria-hidden="true">
                {anyCount}
              </small>
            </button>
          </div>
        </div>
        <label className="library-toggle">
          <input
            type="checkbox"
            checked={filters.fits}
            title="Keep what the Controller can place on fresh fleet capacity; unknown verdicts stay visible (vonkctl recipe library --fits-fleet)"
            onChange={(event) => set({ fits: event.target.checked })}
          />
          Fits fleet
        </label>
        <label className="library-toggle">
          <input
            type="checkbox"
            checked={filters.cached}
            onChange={(event) => set({ cached: event.target.checked })}
          />
          Cached only
        </label>
      </div>
      <details className="library-filters">
        <summary>Filters{inDisclosure > 0 ? ` · ${inDisclosure} active` : ""}</summary>
        <div className="library-filters-grid">
          <FilterSelect
            label="Family"
            name="Filter family"
            all="All families"
            value={filters.family}
            options={asOptions(facet("family"))}
            onChange={(family) => set({ family })}
          />
          <FilterSelect
            label="Engine"
            name="Filter engine"
            all="All engines"
            value={lower(filters.engine)}
            options={asOptions(facet("engine"))}
            onChange={(engine) => set({ engine })}
          />
          <FilterSelect
            label="Creator"
            name="Filter creator"
            all="All creators"
            value={lower(filters.creator)}
            options={asOptions(facet("creator"))}
            onChange={(creator) => set({ creator })}
          />
          <FilterSelect
            label="Quantization"
            name="Filter quantization"
            all="All quantization"
            value={filters.quantization}
            options={sorted(records.flatMap((r) => r.modelDocument?.format.quantization || [])).map(
              (value) => ({ value, label: value }),
            )}
            onChange={(quantization) => set({ quantization })}
          />
          <FilterSelect
            label="Capabilities"
            name="Filter capabilities"
            all="All capabilities"
            value={filters.usage}
            options={sorted(
              records.flatMap((r) =>
                noun === "models" ? (r.modelCapabilities ?? []) : r.capabilities,
              ),
            ).map((value) => ({
              value,
              label: words.label(value),
            }))}
            onChange={(usage) => set({ usage })}
          />
          <FilterSelect
            label="Version"
            name="Filter version"
            all="All versions"
            value={filters.version}
            options={sorted(records.flatMap((r) => r.modelDocument?.identity.version || [])).map(
              (value) => ({ value, label: value }),
            )}
            onChange={(version) => set({ version })}
          />
          <FilterSelect
            label="Publisher"
            name="Filter publisher"
            all="All publishers"
            value={filters.publisher}
            options={sorted(records.flatMap((r) => r.model?.publisher ?? [])).map((value) => ({
              value,
              label: value,
            }))}
            onChange={(publisher) => set({ publisher })}
          />
          <FilterSelect
            label="Alignment"
            name="Filter alignment"
            all="All alignments"
            value={filters.alignment}
            options={sorted(records.flatMap((r) => r.recipe?.recipe_display?.alignment ?? [])).map(
              (value) => ({ value, label: value }),
            )}
            onChange={(alignment) => set({ alignment })}
          />
          <FilterSelect
            label="Exact model"
            name="Filter exact model"
            all="All models"
            value={filters.model}
            options={[
              ...new Map(
                records
                  .filter((r) => noun === "models" || recordModelKey(r) !== null)
                  .map((r) => [
                    r.modelKey,
                    `${r.modelTitle} · ${r.modelDocument?.identity.version ?? ""} · ${r.model?.content_sha256.slice(0, 8) ?? ""}`,
                  ]),
              ).entries(),
            ].map(([value, label]) => ({ value, label }))}
            onChange={(model) => set({ model })}
          />
          <FilterSelect
            label="Updated"
            name="Filter updated"
            all={LIBRARY_RECENCY_LABELS.any}
            value={filters.updated === "any" ? "" : filters.updated}
            options={LIBRARY_RECENCY_VALUES.filter((value) => value !== "any").map((value) => ({
              value,
              label: LIBRARY_RECENCY_LABELS[value],
            }))}
            onChange={(updated) => set({ updated: libraryRecencyFromValue(updated) })}
          />
          <label>
            Sort
            <select
              aria-label="Sort Library"
              value={filters.sort}
              onChange={(event) => set({ sort: librarySortFromValue(event.target.value) })}
            >
              {LIBRARY_SORTS.map((value) => (
                <option key={value} value={value}>
                  {LIBRARY_SORT_LABELS[value]}
                </option>
              ))}
            </select>
          </label>
        </div>
      </details>
      <p className="library-toolbar-fleet">
        {fleetLine}. {fitLine}.
      </p>
    </section>
  );
}

// Exposed for tests: the comparison the tiles use to mark the operator's fleet.
export function isYourFleet(summary: FleetSummary | undefined, sparks: WireNumber): boolean {
  return summary !== undefined && summary.online > 0 && compareWire(sparks, summary.online) === 0;
}
