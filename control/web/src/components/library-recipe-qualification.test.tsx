import { render, screen, within } from "@testing-library/react";
import type { RecipeQualification } from "../api/types";
import { modelKey, recipeLibraryPath } from "../lib/library-route";
import { libraryViewSnapshot, minimalLibraryDetail } from "../test-fixtures/library";
import { LibraryRecipeAuthority } from "./library-recipe-detail";
import { EMPTY_LIBRARY_WORKCELL_FILTERS, LibraryWorkcell } from "./library-workcell";

const identity = { content_sha256: "a".repeat(64), required_sparks: 2 };
const working: RecipeQualification = {
  status: "Working",
  required_sparks: 2,
  last_pass: {
    identity,
    outcome: "passed",
    cause_code: "qualification.hardware_pass",
    cause_summary: "Hardware checks passed",
    tested_at: "2026-11-01T10:00:00Z",
    platform: { controller_version: "1.2.3", spark_count: 2, topology: "multi" },
  },
};
const failed: RecipeQualification = {
  status: "Failed: recipe",
  required_sparks: 2,
  last_pass: working.last_pass,
  last_failure: {
    identity,
    outcome: "failed",
    blame: "recipe",
    cause_code: "recipe.runtime_oom",
    cause_summary: "Recipe runtime exceeded available memory; needs fix",
    tested_at: "2026-11-03T10:00:00Z",
    platform: { controller_version: "1.2.3", spark_count: 2, topology: "multi" },
  },
};

test("each recipe row shows its published status and newest evidence; none is not yet verified", () => {
  const base = libraryViewSnapshot.models.find((entry) => entry.recipes.length > 2)!;
  const statuses = [working, failed, null];
  const recipes = base.recipes.map((recipe, index) => ({
    ...recipe,
    qualification: statuses[index] ?? null,
  }));
  render(
    <LibraryWorkcell
      api={{} as never}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      onFiltersChange={() => undefined}
      onNavigate={() => undefined}
      onQueryChange={() => undefined}
      query=""
      route={{ kind: "model", modelKey: modelKey(base.model) }}
      snapshot={{
        ...libraryViewSnapshot,
        models: libraryViewSnapshot.models.map((entry) =>
          entry === base ? { ...entry, recipes } : entry,
        ),
      }}
    />,
  );
  const pane = screen.getByLabelText("Recipes matching selected Model");
  const row = (index: number) =>
    pane.querySelector<HTMLElement>(`a[href="${recipeLibraryPath(recipes[index]!.recipe_id)}"]`)!;

  expect(row(0).querySelector('[data-badge="qualification"]')).toHaveTextContent("Working");
  expect(within(row(0)).getByText(/Last passed/)).toHaveTextContent(/on 2 Sparks$/);

  expect(row(1).querySelector('[data-badge="qualification"]')).toHaveTextContent("Failed: recipe");
  // The failure is newer than the pass, so the failure reason is shown.
  expect(within(row(1)).getByText(/Last failure:/)).toHaveTextContent(
    "Recipe runtime exceeded available memory; needs fix",
  );

  expect(row(2).querySelector('[data-badge="qualification"]')).toHaveTextContent(
    "Not yet verified",
  );
  expect(row(2).querySelector("[data-qualification-evidence]")).toBeNull();
});

test("the recipe detail shows the status and last pass, and an unknown status as not yet verified", () => {
  const detail = (qualification: unknown) => ({
    ...minimalLibraryDetail,
    recipe: {
      ...minimalLibraryDetail.recipe,
      qualification: qualification as RecipeQualification,
    },
  });
  const { unmount } = render(<LibraryRecipeAuthority api={{} as never} detail={detail(working)} />);
  const heading = document.getElementById("recipe-detail-heading")!.parentElement!;
  expect(heading.querySelector('[data-badge="qualification"]')).toHaveTextContent("Working");
  expect(within(heading).getByText(/Last passed/)).toHaveTextContent(/on 2 Sparks$/);
  unmount();

  render(
    <LibraryRecipeAuthority
      api={{} as never}
      detail={detail({ status: "Something newer", required_sparks: 1 })}
    />,
  );
  expect(document.querySelector('[data-badge="qualification"]')).toHaveTextContent(
    "Not yet verified",
  );
});
