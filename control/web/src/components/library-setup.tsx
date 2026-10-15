import { addWire, compareWire, formatWire, type WireNumber } from "../api/contract-numeric";
import type { VisualFleetSnapshot } from "../api/types";
import { formatBytes } from "../lib/fleet";
import type { LibraryRecipeRecord, LibraryWorkcellFilters } from "./library-workcell";
import {
  filterLibraryRecipeRecords,
  fleetFitRank,
  recipeCreator,
  recipeEngine,
  recordFamily,
  sparkLabel,
} from "./library-workcell";

// The Library opens the way the public site does: say what you have, see what
// runs on it. Here the Controller knows what you have, so the Spark tile that
// matches the live fleet is marked as yours, and every choice is the same
// filter the toolbar, the URL and `vonkctl recipe library` use (--sparks,
// --family, --engine, --creator, --fits-fleet). Counts are the records the
// other filters leave. "Fits my fleet now" is the Controller's own placement
// verdict against fresh capacity, carried on each recipe of the loaded page;
// unknown evidence is counted apart and never presented as a non-fit.

export type FleetSummary = {
  enrolled: number;
  online: number;
  freeMemoryBytes: WireNumber | null;
};

export function summarizeFleet(fleet: VisualFleetSnapshot | undefined): FleetSummary | undefined {
  if (!fleet) return undefined;
  const online = fleet.nodes.filter((node) => node.connection.online_state === "online");
  const samples = online.flatMap((node) => {
    const value = node.telemetry?.sample.memory_available_bytes;
    return value === null || value === undefined ? [] : [value as WireNumber];
  });
  return {
    enrolled: fleet.nodes.length,
    online: online.length,
    freeMemoryBytes: samples.length
      ? samples.reduce<WireNumber>((total, value) => addWire(total, value), 0)
      : null,
  };
}

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
    const key = noun === "models" ? record.modelKey : record.key;
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
    ? new Set(matching.map((record) => record.modelKey)).size
    : matching.filter((record) => record.recipe).length;
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
      const value = recordFamily(record);
      if (value) labels.set(value, record.modelDocument?.identity.family.title || value);
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

const SHOWN = 8;

function FacetStrip({
  label,
  facet,
  options,
  value,
  onChange,
}: {
  label: string;
  facet: Facet;
  options: FacetOption[];
  value: string;
  onChange(value: string): void;
}) {
  const selected = value.toLocaleLowerCase();
  if (options.length === 0) return null;
  const shown = options.slice(0, SHOWN);
  const current = options.find((option) => option.value.toLocaleLowerCase() === selected);
  if (current && !shown.includes(current)) shown.push(current);
  const rest = options.filter((option) => !shown.includes(option));
  return (
    <div className="library-setup-facet" role="group" aria-label={label}>
      <span>{label}</span>
      <div>
        {shown.map((option) => {
          const pressed = option.value.toLocaleLowerCase() === selected;
          return (
            <button
              key={option.value}
              type="button"
              aria-pressed={pressed}
              aria-label={`${option.label}: ${option.count}`}
              disabled={option.count === 0 && !pressed}
              onClick={() => onChange(pressed ? "" : option.value)}
            >
              {option.label}
              <small>{option.count}</small>
            </button>
          );
        })}
        {rest.length > 0 && (
          <select
            aria-label={`More ${facet === "family" ? "families" : `${facet}s`}`}
            value=""
            onChange={(event) => onChange(event.target.value)}
          >
            <option value="">{rest.length} more…</option>
            {rest.map((option) => (
              <option key={option.value} value={option.value} disabled={option.count === 0}>
                {option.label} ({option.count})
              </option>
            ))}
          </select>
        )}
      </div>
    </div>
  );
}

export function LibrarySetupStrip({
  fleet,
  records,
  filters,
  query,
  noun,
  onFiltersChange,
}: {
  fleet?: VisualFleetSnapshot;
  records: LibraryRecipeRecord[];
  filters: LibraryWorkcellFilters;
  query: string;
  noun: "models" | "recipes";
  onFiltersChange(filters: LibraryWorkcellFilters): void;
}) {
  const summary = summarizeFleet(fleet);
  const sparkCounts = [
    ...new Set(
      records.flatMap((record) =>
        record.recipe ? [formatWire(record.recipe.recipe_document.topology.node_count)] : [],
      ),
    ),
  ].sort((a, b) => Number(a) - Number(b));
  const openSparks = { ...filters, sparks: "" };
  const tiles = sparkCounts.map((value) => ({
    value,
    count: countBy(records, { ...openSparks, sparks: value }, query, noun),
    yours: summary !== undefined && summary.online > 0 && Number(value) === summary.online,
  }));
  const anyCount = countBy(records, openSparks, query, noun);
  const fit = countFleetFit(records, filters, query, noun);
  const assessed = fit.fits + fit.nonFit > 0;
  return (
    <section className="library-setup" aria-label="Start from your fleet">
      <header>
        <div>
          <h3>Start from your fleet</h3>
          <p>
            {summary === undefined
              ? "Fleet state is not available yet; pick a Spark count to narrow the Library."
              : `${summary.online} of ${summary.enrolled} Spark${summary.enrolled === 1 ? "" : "s"} online` +
                (summary.freeMemoryBytes === null
                  ? "."
                  : ` · ${formatBytes(summary.freeMemoryBytes)} unified memory free.`)}
            {assessed
              ? ` ${fit.fits} of ${fit.fits + fit.nonFit} assessed ${noun} fit it now.`
              : " The Controller has not assessed placement for this page yet."}
            {fit.unknown > 0 && assessed ? ` ${fit.unknown} awaiting assessment.` : ""}
          </p>
        </div>
        <button
          type="button"
          className="library-setup-fit"
          aria-pressed={filters.fits}
          aria-label={`Fits my fleet now: ${fit.fits} ${noun}`}
          disabled={fit.fits === 0 && !filters.fits}
          title="Keep what the Controller can place on fresh fleet capacity; unknown verdicts stay visible (vonkctl recipe library --fits-fleet)"
          onClick={() => onFiltersChange({ ...filters, fits: !filters.fits })}
        >
          Fits my fleet now
          <small>{fit.fits}</small>
        </button>
      </header>
      <div className="library-setup-tiles" role="group" aria-label="Sparks per recipe">
        {tiles.map((tile) => {
          const pressed = filters.sparks === tile.value;
          return (
            <button
              key={tile.value}
              type="button"
              aria-pressed={pressed}
              aria-label={`${sparkLabel(Number(tile.value))}: ${tile.count} ${noun}${tile.yours ? " (your fleet)" : ""}`}
              data-fleet={tile.yours ? "yours" : undefined}
              disabled={tile.count === 0 && !pressed}
              onClick={() => onFiltersChange({ ...filters, sparks: pressed ? "" : tile.value })}
            >
              <strong>{tile.value}</strong>
              <span>{sparkLabel(Number(tile.value))}</span>
              <small>
                {tile.count} {noun}
                {tile.yours ? " · your fleet" : ""}
              </small>
            </button>
          );
        })}
        <button
          type="button"
          aria-pressed={filters.sparks === ""}
          aria-label={`Any Spark count: ${anyCount} ${noun}`}
          onClick={() => onFiltersChange({ ...filters, sparks: "" })}
        >
          <strong>Any</strong>
          <span>Every count</span>
          <small>
            {anyCount} {noun}
          </small>
        </button>
      </div>
      <FacetStrip
        label="Family"
        facet="family"
        options={facetOptions("family", records, filters, query, noun)}
        value={filters.family}
        onChange={(value) => onFiltersChange({ ...filters, family: value })}
      />
      <FacetStrip
        label="Engine"
        facet="engine"
        options={facetOptions("engine", records, filters, query, noun)}
        value={filters.engine}
        onChange={(value) => onFiltersChange({ ...filters, engine: value })}
      />
      <FacetStrip
        label="Creator"
        facet="creator"
        options={facetOptions("creator", records, filters, query, noun)}
        value={filters.creator}
        onChange={(value) => onFiltersChange({ ...filters, creator: value })}
      />
    </section>
  );
}

// Exposed for tests: the comparison the tiles use to mark the operator's fleet.
export function isYourFleet(summary: FleetSummary | undefined, sparks: WireNumber): boolean {
  return summary !== undefined && summary.online > 0 && compareWire(sparks, summary.online) === 0;
}
