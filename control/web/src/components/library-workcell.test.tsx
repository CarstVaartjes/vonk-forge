import {render, screen} from "@testing-library/react";
import {buildLibraryRecipeRecords, filterLibraryRecipeRecords, EMPTY_LIBRARY_WORKCELL_FILTERS, LibraryWorkcell} from "./library-workcell";
import {librarySnapshot} from "../test-fixtures/library";
import {modelVersionKey} from "../lib/library-route";

test("builds the requested model inventory shape", () => {
  const records = buildLibraryRecipeRecords(librarySnapshot);
  expect(new Set(records.map(record => record.modelKey)).size).toBe(92);
  expect(records.filter(record => !record.recipe)).toHaveLength(13);
  expect(records.filter(record => record.recipe?.recipe_id === "recipe-1")).toHaveLength(2);
});
test("filters by exact model identity", () => {
  const records = buildLibraryRecipeRecords(librarySnapshot);
  const key = records[0]!.modelKey;
  expect(filterLibraryRecipeRecords(records, {...EMPTY_LIBRARY_WORKCELL_FILTERS, model: key}, "").every(record => record.modelKey === key)).toBe(true);
});
test("keeps URL-selected Models in the paired right pane", () => {
  const model = librarySnapshot.models[79]!;
  const modelKey = modelVersionKey(model.model);
  render(<LibraryWorkcell api={{} as never} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} query="" route={{kind: "model", modelKey}} snapshot={librarySnapshot}/>);
  expect(screen.getByText("No Recipe linked")).toBeVisible();
  expect(screen.getByLabelText("Recipes matching selected Model")).toHaveTextContent("No Recipe linked");
});
