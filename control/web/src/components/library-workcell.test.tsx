import {render, screen} from "@testing-library/react";
import {buildLibraryRecipeRecords, filterLibraryRecipeRecords, EMPTY_LIBRARY_WORKCELL_FILTERS, LibraryWorkcell} from "./library-workcell";
import {libraryViewSnapshot} from "../test-fixtures/library";
import {modelKey} from "../lib/library-route";

test("builds the requested model inventory shape", () => {
  expect(libraryViewSnapshot.models).toHaveLength(92);
  expect(new Set(libraryViewSnapshot.models.flatMap(model => model.recipes.map(recipe => recipe.recipe_id)))).toHaveLength(85);
  expect(libraryViewSnapshot.models.filter(model => model.recipes.length === 0)).toHaveLength(13);
  const multiModelRecipeId = libraryViewSnapshot.models[0]!.recipes[0]!.recipe_id;
  expect(libraryViewSnapshot.models.flatMap(model => model.recipes).find(recipe => recipe.recipe_id === multiModelRecipeId)?.recipe_document.models).toHaveLength(2);
  const records = buildLibraryRecipeRecords(libraryViewSnapshot);
  expect(new Set(records.map(record => record.modelKey)).size).toBe(92);
  expect(records.filter(record => !record.recipe)).toHaveLength(13);
  expect(records.filter(record => record.recipe?.recipe_id === multiModelRecipeId)).toHaveLength(2);
  const ltx = libraryViewSnapshot.models.find(model => model.model.publisher === "lightricks" && model.model.slug === "ltx-2-gemma3-text-encoder-dfcc2108")!;
  expect(ltx.recipes).toHaveLength(4);
  for (const recipe of ltx.recipes) for (const selection of recipe.recipe_document.models) {
    const selectedModel = libraryViewSnapshot.models.find(model => model.model.content_sha256 === selection.model.content_sha256);
    expect(selectedModel).toBeDefined();
    expect(selection.files.every(file => selectedModel!.model_document.files.some(candidate => candidate.id === file.file_id))).toBe(true);
  }
});
test("filters by exact model identity", () => {
  const records = buildLibraryRecipeRecords(libraryViewSnapshot);
  const key = records[0]!.modelKey;
  expect(filterLibraryRecipeRecords(records, {...EMPTY_LIBRARY_WORKCELL_FILTERS, model: key}, "").every(record => record.modelKey === key)).toBe(true);
});
test("keeps URL-selected Models in the paired right pane", () => {
  const model = libraryViewSnapshot.models[79]!;
  const key = modelKey(model.model);
  render(<LibraryWorkcell api={{} as never} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} query="" route={{kind: "model", modelKey: key}} snapshot={libraryViewSnapshot}/>);
  expect(screen.getByText("No Recipe linked")).toBeVisible();
  expect(screen.getByLabelText("Recipes matching selected Model")).toHaveTextContent("No Recipe linked");
});

test("keeps same-Model Recipe variants together and shows creator attribution", () => {
  const model = libraryViewSnapshot.models.find(entry => entry.model.publisher === "lightricks" && entry.model.slug === "ltx-2-gemma3-text-encoder-dfcc2108")!;
  const key = modelKey(model.model);
  const records = buildLibraryRecipeRecords(libraryViewSnapshot).filter(record => record.modelKey === key && record.recipe);
  render(<LibraryWorkcell api={{} as never} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} query="" route={{kind: "model", modelKey: key}} snapshot={libraryViewSnapshot}/>);

  const pane = screen.getByLabelText("Recipes matching selected Model");
  expect(records).toHaveLength(4);
  expect(new Set(records.map(record => record.key)).size).toBe(records.length);
  expect(pane.querySelectorAll(":scope > ul > li")).toHaveLength(records.length);
  expect(pane).toHaveTextContent(records[0]!.recipe!.recipe_document.provenance.attribution[0]!);
  expect(pane).toHaveTextContent(records[0]!.recipe!.recipe_document.runtime.engine);
  expect(pane).toHaveTextContent(records[1]!.recipe!.recipe_document.runtime.engine);
});
