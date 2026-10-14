import { fireEvent, render, screen, within } from "@testing-library/react";
import { vi } from "vitest";
import type { VisualFleetSnapshot } from "../api/types";
import { libraryViewSnapshot } from "../test-fixtures/library";
import { countFleetFit, isYourFleet, LibrarySetupStrip, summarizeFleet } from "./library-setup";
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

test("the fleet-fit toggle counts the Controller's verdicts and sets --fits-fleet", () => {
  const onFiltersChange = vi.fn();
  const stamped = assessed(["ready", "blocked", "unavailable", null]);
  const counts = countFleetFit(stamped, EMPTY_LIBRARY_WORKCELL_FILTERS, "", "recipes");
  const withRecipe = stamped.filter((record) => record.recipe).length;
  expect(counts.ready + counts.blocked + counts.unknown).toBe(withRecipe);
  expect(counts.ready).toBeGreaterThan(0);
  expect(counts.unknown).toBeGreaterThan(0);
  render(
    <LibrarySetupStrip
      fleet={fleetOf([{ online: true, free: 2 ** 30 }])}
      records={stamped}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      query=""
      noun="recipes"
      onFiltersChange={onFiltersChange}
    />,
  );
  expect(screen.getByText(/assessed recipes fit it now/)).toHaveTextContent(
    `${counts.ready} of ${counts.ready + counts.blocked} assessed recipes fit it now. ${counts.unknown} awaiting assessment.`,
  );
  const toggle = screen.getByRole("button", { name: `Fits my fleet now: ${counts.ready} recipes` });
  expect(toggle).toHaveAttribute("aria-pressed", "false");
  fireEvent.click(toggle);
  expect(onFiltersChange).toHaveBeenCalledWith({ ...EMPTY_LIBRARY_WORKCELL_FILTERS, fits: true });
  // --fits-fleet excludes only proven non-fits; unknown evidence stays visible.
  const kept = filterLibraryRecipeRecords(
    stamped,
    { ...EMPTY_LIBRARY_WORKCELL_FILTERS, fits: true },
    "",
  ).filter((record) => record.recipe);
  expect(kept.length).toBe(counts.ready + counts.unknown);
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
  const expected = { ready: 0, blocked: 0, unknown: 0 };
  for (const states of byModel.values()) {
    if (states.has("ready")) expected.ready += 1;
    else if (states.has("unavailable") || states.has("unassessed")) expected.unknown += 1;
    else expected.blocked += 1;
  }
  expect(counts).toEqual(expected);
  const kept = new Set(
    filterLibraryRecipeRecords(stamped, { ...EMPTY_LIBRARY_WORKCELL_FILTERS, fits: true }, "").map(
      (record) => record.modelKey,
    ),
  );
  expect(kept.size).toBe(expected.ready + expected.unknown);
});

test("without any verdict the toggle is disabled and the strip says so", () => {
  render(
    <LibrarySetupStrip
      records={records}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      query=""
      noun="models"
      onFiltersChange={() => undefined}
    />,
  );
  expect(screen.getByText(/has not assessed placement/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Fits my fleet now: 0 models" })).toBeDisabled();
});
