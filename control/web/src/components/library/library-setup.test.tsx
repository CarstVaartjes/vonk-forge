import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import type { VisualFleetSnapshot } from "../../api/types";
import { libraryViewSnapshot } from "../../test-fixtures/library";
import { summarizeFleet } from "../../lib/fleet";
import { countFleetFit, isYourFleet, LibrarySetupStrip } from "./library-setup";
import {
  buildLibraryRecipeRecords,
  EMPTY_LIBRARY_WORKCELL_FILTERS,
  filterLibraryRecipeRecords,
  recipeEngine,
  recordFamily,
} from "./library-workcell";

// Only the fields the strip reads; the generated FleetSnapshot has many more.
function fleetOf(nodes: { online: boolean; free?: number }[]): VisualFleetSnapshot {
  return {
    nodes: nodes.map((node, index) => ({
      id: `node-${index}`,
      connection: { online_state: node.online ? "online" : "offline" },
      telemetry: node.free === undefined ? null : { sample: { memory_available_bytes: node.free } },
    })),
  } as unknown as VisualFleetSnapshot;
}

const records = buildLibraryRecipeRecords(libraryViewSnapshot);

/** Stamp the Controller's fleet-fit verdict on the recipes, round-robin. */
function assessed(states: ("ready" | "blocked" | "unavailable" | null)[]) {
  let index = 0;
  return records.map((record) => {
    if (!record.recipe) return record;
    const state = states[index++ % states.length];
    if (state === null) return record;
    const check = {
      state,
      reasons:
        state === "ready"
          ? []
          : [
              {
                code: "x",
                detail: `${state} reason`,
                severity: "blocker" as const,
                scope: "operation" as const,
              },
            ],
    };
    return {
      ...record,
      recipe: {
        ...record.recipe,
        assessment: {
          observed_at: "2026-10-14T00:00:00Z",
          fleet_fit: check,
          cache: check,
          readiness: check,
        },
      },
    } as typeof record;
  });
}

/** Recipes are counted once, however many models list them. */
const uniqueRecipes = (list: typeof records) =>
  new Set(list.flatMap((record) => (record.recipe ? [record.recipe.recipe_id] : [])));

test("summarises the fleet from online nodes and their free memory", () => {
  expect(summarizeFleet(undefined)).toBeUndefined();
  const summary = summarizeFleet(
    fleetOf([{ online: true, free: 10 }, { online: true }, { online: false, free: 99 }]),
  );
  expect(summary).toEqual({ enrolled: 3, online: 2, reporting: 1, freeMemoryBytes: 10 });
  expect(isYourFleet(summary, 2)).toBe(true);
  expect(isYourFleet(summary, 1)).toBe(false);
  expect(isYourFleet(summarizeFleet(fleetOf([{ online: false }])), 1)).toBe(false);
});

test("the Sparks filter reads N or fewer: the largest count keeps every recipe", () => {
  const sizes = records.flatMap((record) =>
    record.recipe ? [Number(record.recipe.recipe_document.topology.node_count)] : [],
  );
  const withRecipe = records.filter((record) => record.recipe);
  const keep = (sparks: number) =>
    filterLibraryRecipeRecords(
      records,
      { ...EMPTY_LIBRARY_WORKCELL_FILTERS, sparks: String(sparks) },
      "",
    ).filter((record) => record.recipe).length;
  expect(keep(Math.max(...sizes))).toBe(withRecipe.length);
  expect(keep(Math.min(...sizes))).toBe(sizes.filter((size) => size === Math.min(...sizes)).length);
});

test("Spark tiles count the library, mark the operator's fleet, and set the same --sparks filter", () => {
  const onFiltersChange = vi.fn();
  const counts = [
    ...new Set(
      records.flatMap((record) =>
        record.recipe ? [String(record.recipe.recipe_document.topology.node_count)] : [],
      ),
    ),
  ].sort((a, b) => Number(a) - Number(b));
  expect(counts.length).toBeGreaterThan(0);
  const yours = Number(counts[0]);
  render(
    <LibrarySetupStrip
      fleet={fleetOf(Array.from({ length: yours }, () => ({ online: true, free: 2 ** 30 })))}
      records={records}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      query=""
      noun="recipes"
      onFiltersChange={onFiltersChange}
      onQueryChange={() => undefined}
    />,
  );
  expect(screen.getByText(/of \d+ Sparks? online/)).toHaveTextContent(
    `${yours} of ${yours} Spark${yours === 1 ? "" : "s"} online`,
  );
  const tiles = within(screen.getByRole("group", { name: "Sparks per recipe" }));
  for (const value of counts) {
    const expected = uniqueRecipes(
      filterLibraryRecipeRecords(records, { ...EMPTY_LIBRARY_WORKCELL_FILTERS, sparks: value }, ""),
    ).size;
    const tile = tiles.getByRole("button", {
      name: new RegExp(`^${value} Sparks? or fewer: ${expected} recipes`),
    });
    expect(tile).toHaveAttribute("aria-pressed", "false");
    expect(tile.getAttribute("data-fleet")).toBe(Number(value) === yours ? "yours" : null);
  }
  fireEvent.click(tiles.getByRole("button", { name: new RegExp(`^${yours} Sparks? or fewer:`) }));
  expect(onFiltersChange).toHaveBeenCalledWith({
    ...EMPTY_LIBRARY_WORKCELL_FILTERS,
    sparks: String(yours),
  });
  fireEvent.click(tiles.getByRole("button", { name: /^Any Spark count:/ }));
  expect(onFiltersChange).toHaveBeenLastCalledWith({
    ...EMPTY_LIBRARY_WORKCELL_FILTERS,
    sparks: "",
  });
});

test("one toolbar: Family, Engine and Creator live once, in the Filters disclosure", () => {
  const onFiltersChange = vi.fn();
  const withRecipe = records.find((record) => record.recipe)!;
  const engine = recipeEngine(withRecipe.recipe!);
  const family = recordFamily(withRecipe);
  render(
    <LibrarySetupStrip
      records={records}
      filters={{ ...EMPTY_LIBRARY_WORKCELL_FILTERS, engine }}
      query=""
      noun="recipes"
      onFiltersChange={onFiltersChange}
      onQueryChange={() => undefined}
    />,
  );
  expect(screen.getByText(/Fleet state is not available yet/)).toBeVisible();
  // No second zone: the old chip rows are gone, each filter is one select.
  for (const name of ["Family", "Engine", "Creator"])
    expect(screen.queryByRole("group", { name })).not.toBeInTheDocument();
  expect(screen.getAllByRole("combobox", { name: "Filter engine" })).toHaveLength(1);
  expect(screen.getByText("Filters · 1 active")).toBeVisible();
  const select = screen.getByRole("combobox", { name: "Filter engine" });
  expect(select).toHaveValue(engine.toLocaleLowerCase());
  fireEvent.change(select, { target: { value: "" } });
  expect(onFiltersChange).toHaveBeenCalledWith({ ...EMPTY_LIBRARY_WORKCELL_FILTERS, engine: "" });
  fireEvent.change(screen.getByRole("combobox", { name: "Filter family" }), {
    target: { value: family },
  });
  expect(onFiltersChange).toHaveBeenLastCalledWith({
    ...EMPTY_LIBRARY_WORKCELL_FILTERS,
    engine,
    family,
  });
});

test("search, Sparks, two toggles and one Filters disclosure make the whole toolbar", () => {
  const onQueryChange = vi.fn();
  render(
    <LibrarySetupStrip
      records={records}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      query=""
      noun="recipes"
      onFiltersChange={() => undefined}
      onQueryChange={onQueryChange}
    />,
  );
  const search = screen.getByRole("searchbox", { name: "Search Library" });
  fireEvent.change(search, { target: { value: "ltx" } });
  expect(onQueryChange).toHaveBeenCalledWith("ltx");
  expect(screen.getByRole("group", { name: "Sparks per recipe" })).toBeVisible();
  expect(screen.getByRole("checkbox", { name: "Fits fleet" })).toBeVisible();
  expect(screen.getByRole("checkbox", { name: "Cached only" })).toBeVisible();
  expect(screen.getAllByText(/^Filters/)).toHaveLength(1);
  expect(screen.queryByRole("heading", { name: "Start from your fleet" })).not.toBeInTheDocument();
});

test("counts Models, not quantization variants, and leaves lineage-less models out", () => {
  const first = records.find((record) => record.recipe && record.modelDisplay?.lineage)!;
  const variant = { ...first, key: `${first.key}:q4`, modelKey: `${first.modelKey}:q4` };
  const helper = { ...first, key: `${first.key}:helper`, modelKey: "helper", modelDisplay: null };
  render(
    <LibrarySetupStrip
      records={[...records, variant, helper]}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      query=""
      noun="models"
      onFiltersChange={() => undefined}
      onQueryChange={() => undefined}
    />,
  );
  // The fixture holds 92 Models; one more variant and one helper change nothing.
  expect(
    within(screen.getByRole("group", { name: "Sparks per recipe" })).getByRole("button", {
      name: "Any Spark count: 92 models",
    }),
  ).toBeVisible();
});

test("the fleet-fit toggle counts the Controller's verdicts and sets --fits-fleet", () => {
  const onFiltersChange = vi.fn();
  const stamped = assessed(["ready", "blocked", "unavailable", null]);
  const counts = countFleetFit(stamped, EMPTY_LIBRARY_WORKCELL_FILTERS, "", "recipes");
  expect(counts.fits + counts.nonFit + counts.unknown).toBe(uniqueRecipes(stamped).size);
  expect(counts.fits).toBeGreaterThan(0);
  expect(counts.unknown).toBeGreaterThan(0);
  render(
    <LibrarySetupStrip
      fleet={fleetOf([{ online: true, free: 2 ** 30 }])}
      records={stamped}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      query=""
      noun="recipes"
      onFiltersChange={onFiltersChange}
      onQueryChange={() => undefined}
    />,
  );
  const toggle = screen.getByRole("checkbox", { name: "Fits fleet" });
  expect(toggle).not.toBeChecked();
  expect(
    screen.getByText("22 of 44 assessed recipes fit now, 41 awaiting assessment", {
      exact: false,
    }),
  ).toBeVisible();
  fireEvent.click(toggle);
  expect(onFiltersChange).toHaveBeenCalledWith({ ...EMPTY_LIBRARY_WORKCELL_FILTERS, fits: true });
  // --fits-fleet excludes only proven non-fits; unknown evidence stays visible.
  const kept = filterLibraryRecipeRecords(
    stamped,
    { ...EMPTY_LIBRARY_WORKCELL_FILTERS, fits: true },
    "",
  );
  expect(uniqueRecipes(kept).size).toBe(counts.fits + counts.unknown);
  expect(kept.every((record) => record.recipe!.assessment?.fleet_fit.state !== "blocked")).toBe(
    true,
  );
});

test("a model fits when one of its recipes does, matching what the filter keeps", () => {
  const stamped = assessed(["blocked", "ready", "blocked", "unavailable"]);
  const counts = countFleetFit(stamped, EMPTY_LIBRARY_WORKCELL_FILTERS, "", "models");
  const byModel = new Map<string, Set<string>>();
  for (const record of stamped) {
    if (!record.recipe) continue;
    const states = byModel.get(record.modelKey) ?? new Set();
    states.add(record.recipe.assessment?.fleet_fit.state ?? "unassessed");
    byModel.set(record.modelKey, states);
  }
  const expected = { fits: 0, nonFit: 0, unknown: 0 };
  for (const states of byModel.values()) {
    if (states.has("ready")) expected.fits += 1;
    else if (states.has("unavailable") || states.has("unassessed")) expected.unknown += 1;
    else expected.nonFit += 1;
  }
  expect(counts).toEqual(expected);
  const kept = new Set(
    filterLibraryRecipeRecords(stamped, { ...EMPTY_LIBRARY_WORKCELL_FILTERS, fits: true }, "").map(
      (record) => record.modelKey,
    ),
  );
  expect(kept.size).toBe(expected.fits + expected.unknown);
});

test("Fits fleet can be turned on before any verdict exists, so the page can ask for them", () => {
  const onFiltersChange = vi.fn();
  render(
    <LibrarySetupStrip
      records={records}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      query=""
      noun="models"
      onFiltersChange={onFiltersChange}
      onQueryChange={() => undefined}
    />,
  );
  expect(screen.getByText(/Placement not assessed yet/)).toBeVisible();
  const toggle = screen.getByRole("checkbox", { name: "Fits fleet" });
  expect(toggle).toBeEnabled();
  fireEvent.click(toggle);
  expect(onFiltersChange).toHaveBeenCalledWith({ ...EMPTY_LIBRARY_WORKCELL_FILTERS, fits: true });
});

test("a recipe used by several models is counted once", () => {
  const first = records.find((record) => record.recipe)!;
  const doubled = [...records, { ...first, key: `${first.key}:again`, modelKey: "other-model" }];
  const anyTile = (list: typeof records) => {
    const { unmount } = render(
      <LibrarySetupStrip
        records={list}
        filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
        query=""
        noun="recipes"
        onFiltersChange={() => undefined}
        onQueryChange={() => undefined}
      />,
    );
    const label = within(screen.getByRole("group", { name: "Sparks per recipe" }))
      .getByRole("button", { name: /^Any Spark count:/ })
      .getAttribute("aria-label");
    unmount();
    return label;
  };
  expect(anyTile(doubled)).toBe(anyTile(records));
});

test("the toolbar gives the online Sparks and their free memory in one quiet line", () => {
  const GIB = 1024 ** 3;
  const node = (id: string, online: boolean) => ({
    id,
    connection: { online_state: online ? "online" : "offline" },
    telemetry: online
      ? { sample: { memory_total_bytes: 100 * GIB, memory_available_bytes: 20 * GIB } }
      : null,
  });
  const fleet = {
    nodes: [node("a", true), node("b", true), node("c", false)],
  } as unknown as VisualFleetSnapshot;
  render(
    <LibrarySetupStrip
      fleet={fleet}
      records={records}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      query=""
      noun="recipes"
      onFiltersChange={vi.fn()}
      onQueryChange={() => undefined}
    />,
  );
  expect(
    screen.getByText(/2 of 3 Sparks online · 40\.0 GiB unified memory free/),
  ).toBeInTheDocument();
});

test("Exact model on the Recipes tab leaves out models that are not Models", () => {
  const first = records.find((record) => record.recipe && record.modelDisplay?.lineage)!;
  const helper = {
    ...first,
    key: `${first.key}:helper`,
    modelKey: "helper",
    modelTitle: "Helper draft",
    modelDisplay: null,
  };
  render(
    <LibrarySetupStrip
      records={[...records, helper]}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      query=""
      noun="recipes"
      onFiltersChange={() => undefined}
      onQueryChange={() => undefined}
    />,
  );
  fireEvent.click(screen.getByText("Filters"));
  const select = screen.getByRole("combobox", { name: "Filter exact model" });
  expect(within(select).queryByText(/Helper draft/)).toBeNull();
});
