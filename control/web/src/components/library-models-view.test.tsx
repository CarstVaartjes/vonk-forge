import {render, screen} from "@testing-library/react";
import {librarySnapshot} from "../test-fixtures/library";
import {buildLibraryRecipeRecords, EMPTY_LIBRARY_WORKCELL_FILTERS} from "./library-workcell";
import {LibraryModelsView} from "./library-models-view";

test("shows compact exact model rows and no-recipe models", async () => {
  render(<LibraryModelsView api={{} as never} entries={buildLibraryRecipeRecords(librarySnapshot)} modelInventory={librarySnapshot.models} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} path="/library?view=models" onQueryChange={() => undefined} query=""/>);
  expect(screen.getByRole("heading", {name: "Models"})).toBeVisible();
  expect(screen.getAllByText("No Recipe", {exact: false}).length).toBe(13);
});
