import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import type { VisualFleetSnapshot } from "../api/types";
import { libraryViewSnapshot } from "../test-fixtures/library";
import { isYourFleet, LibrarySetupStrip, summarizeFleet } from "./library-setup";
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

test("summarises the fleet from online nodes and their free memory", () => {
  expect(summarizeFleet(undefined)).toBeUndefined();
  const summary = summarizeFleet(
    fleetOf([{ online: true, free: 10 }, { online: true }, { online: false, free: 99 }]),
  );
  expect(summary).toEqual({ enrolled: 3, online: 2, freeMemoryBytes: 10 });
  expect(isYourFleet(summary, 2)).toBe(true);
  expect(isYourFleet(summary, 1)).toBe(false);
  expect(isYourFleet(summarizeFleet(fleetOf([{ online: false }])), 1)).toBe(false);
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
    />,
  );
  expect(screen.getByText(/of \d+ Sparks? online/)).toHaveTextContent(
    `${yours} of ${yours} Spark${yours === 1 ? "" : "s"} online`,
  );
  const tiles = within(screen.getByRole("group", { name: "Sparks per recipe" }));
  for (const value of counts) {
    const expected = filterLibraryRecipeRecords(
      records,
      { ...EMPTY_LIBRARY_WORKCELL_FILTERS, sparks: value },
      "",
    ).filter((record) => record.recipe).length;
    const tile = tiles.getByRole("button", {
      name: new RegExp(`^${value} Sparks?: ${expected} recipes`),
    });
    expect(tile).toHaveAttribute("aria-pressed", "false");
    expect(tile.getAttribute("data-fleet")).toBe(Number(value) === yours ? "yours" : null);
  }
  fireEvent.click(tiles.getByRole("button", { name: new RegExp(`^${yours} Sparks?:`) }));
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

test("family and engine chips carry counts that respect the other filters and toggle the URL facets", () => {
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
    />,
  );
  expect(screen.getByText(/Fleet state is not available yet/)).toBeVisible();
  const engines = within(screen.getByRole("group", { name: "Engine" }));
  const pressed = engines.getByRole("button", { pressed: true });
  expect(pressed).toHaveAccessibleName(new RegExp(`^${engine}: \\d+$`));
  fireEvent.click(pressed);
  expect(onFiltersChange).toHaveBeenCalledWith({ ...EMPTY_LIBRARY_WORKCELL_FILTERS, engine: "" });
  const families = within(screen.getByRole("group", { name: "Family" }));
  const familyCount = filterLibraryRecipeRecords(
    records,
    { ...EMPTY_LIBRARY_WORKCELL_FILTERS, engine, family },
    "",
  ).filter((record) => record.recipe).length;
  const title = withRecipe.modelDocument!.identity.family.title;
  fireEvent.click(families.getByRole("button", { name: `${title}: ${familyCount}` }));
  expect(onFiltersChange).toHaveBeenLastCalledWith({
    ...EMPTY_LIBRARY_WORKCELL_FILTERS,
    engine,
    family,
  });
});

test("a facet with nothing to offer stays out of the way", () => {
  render(
    <LibrarySetupStrip
      records={records.filter((record) => !record.recipe || !record.recipe.creator)}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      query=""
      noun="recipes"
      onFiltersChange={() => undefined}
    />,
  );
  expect(screen.queryByRole("group", { name: "Creator" })).not.toBeInTheDocument();
  expect(screen.getByRole("group", { name: "Engine" })).toBeVisible();
});

test("counts models instead of recipes on the Models tab", () => {
  render(
    <LibrarySetupStrip
      records={records}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      query=""
      noun="models"
      onFiltersChange={() => undefined}
    />,
  );
  const models = new Set(records.map((record) => record.modelKey)).size;
  expect(
    within(screen.getByRole("group", { name: "Sparks per recipe" })).getByRole("button", {
      name: `Any Spark count: ${models} models`,
    }),
  ).toBeVisible();
});
