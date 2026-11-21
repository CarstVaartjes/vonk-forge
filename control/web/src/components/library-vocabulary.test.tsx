import { readFileSync } from "node:fs";
import { vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { LibraryVocabulary } from "../api/types";
import type { ControlApi } from "../api/types";
import { App } from "../app";
import { libraryViewSnapshot, modelLibrary, recipeLibrary } from "../test-fixtures/library";
import { LibraryModelsView } from "./library-models-view";
import { EMPTY_LIBRARY_WORKCELL_FILTERS } from "./library-workcell";

const wording = JSON.parse(
  readFileSync("../../tests/fixtures/library-vocabulary-wording.json", "utf8"),
) as { vocabulary: LibraryVocabulary };

test("without a vocabulary the names show", () => {
  const model = { ...libraryViewSnapshot.models[0]!, usage: ["chat"] };
  const { container } = render(
    <LibraryModelsView
      api={{} as ControlApi}
      entries={[]}
      modelInventory={[model]}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      onFiltersChange={() => undefined}
      onNavigate={() => undefined}
      onQueryChange={() => undefined}
      onRefresh={async () => undefined}
      path="/library?view=models"
      query=""
    />,
  );
  expect(container.querySelector(".library-model-badges span")?.textContent).toBe("chat");
});

test("the Library page fetches the served vocabulary once and shows its labels", async () => {
  // The default Recipes view labels each recipe's own capabilities.
  const name = recipeLibrary.recipes.flatMap((recipe) => recipe.usage)[0]!;
  const libraryVocabulary = vi.fn(async () => ({
    ...wording.vocabulary,
    capabilities: [
      { name, label: "A published label", description: "Meaning.", category: "language" },
    ],
  }));
  history.replaceState(null, "", "/library");
  render(
    <App
      api={
        {
          modelLibrary: async () => modelLibrary,
          recipeLibrary: async () => recipeLibrary,
          libraryVocabulary,
        } as unknown as ControlApi
      }
    />,
  );
  expect((await screen.findAllByText(/A published label/)).length).toBeGreaterThan(0);
  expect(libraryVocabulary).toHaveBeenCalledTimes(1);
});
