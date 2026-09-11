import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {vi} from "vitest";
import type {ControlApi} from "../api/types";
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

test("removes a recipe only after an explicit model choice", async () => {
  const removeRecipe = vi.fn().mockResolvedValue({action: "remove", operation_id: "op", recipe_revision_id: "rev", reclaimed_bytes: 0, request_key: "k", schema_version: 2, selector: "s", state: "succeeded", progress: {phase: "complete"}});
  const api = {removeRecipe} as unknown as ControlApi;
  const base = libraryViewSnapshot.models.find(entry => entry.recipes.length > 0)!;
  render(<LibraryWorkcell api={api} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} query="" route={{kind: "model", modelKey: modelKey(base.model)}} snapshot={libraryViewSnapshot}/>);

  // The Controller fails closed without a model decision, so the web asks for
  // one before issuing any request.
  fireEvent.click(screen.getAllByRole("button", {name: "Remove recipe"})[0]!);
  expect(removeRecipe).not.toHaveBeenCalled();
  fireEvent.click(screen.getAllByRole("button", {name: "Keep the model"})[0]!);
  await waitFor(() => expect(removeRecipe).toHaveBeenCalledTimes(1));
  const [, , withModel] = (removeRecipe as unknown as {mock: {calls: [string, string, boolean][]}}).mock.calls[0]!;
  expect(withModel).toBe(false);
});

test("refreshes the whole cached recipe set with an explicit scope", async () => {
  const updateRecipes = vi.fn().mockResolvedValue({action: "update", schema_version: 2, updates: []});
  const api = {updateRecipes} as unknown as ControlApi;
  const base = libraryViewSnapshot.models.find(entry => entry.recipes.length > 0)!;
  render(<LibraryWorkcell api={api} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} query="" route={{kind: "model", modelKey: modelKey(base.model)}} snapshot={libraryViewSnapshot}/>);

  fireEvent.click(screen.getByRole("button", {name: "Update cached recipes"}));
  await waitFor(() => expect(updateRecipes).toHaveBeenCalledTimes(1));
  const [all, selectors] = (updateRecipes as unknown as {mock: {calls: [boolean, string[]][]}}).mock.calls[0]!;
  expect(all).toBe(true);
  expect(selectors).toEqual([]);
});

test("offers the same recipe filters as vonkctl recipe library", () => {
  const model = libraryViewSnapshot.models.find(entry => entry.recipes.length > 0)!;
  render(<LibraryWorkcell api={{} as never} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} query="" route={{kind: "model", modelKey: modelKey(model.model)}} snapshot={libraryViewSnapshot}/>);
  for (const name of ["Filter usage", "Filter family", "Filter version", "Filter quantization", "Filter creator", "Filter alignment", "Filter Sparks"]) {
    expect(screen.getByRole("combobox", {name})).toBeVisible();
  }
});

test("offers to cache the missing models when downloading a recipe", () => {
  const base = libraryViewSnapshot.models.find(entry => entry.recipes.length > 0)!;
  const selector = `${base.model.publisher}/${base.model.slug}`;
  const snapshot = {
    ...libraryViewSnapshot,
    models: libraryViewSnapshot.models.map(entry => entry === base
      ? {...entry, recipes: entry.recipes.map(recipe => ({...recipe, model_selectors: [selector]}))}
      : entry),
  };
  render(<LibraryWorkcell api={{} as never} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} query="" route={{kind: "model", modelKey: modelKey(base.model)}} snapshot={snapshot}/>);
  expect(screen.getAllByRole("button", {name: /Download recipe and 1 missing model/}).length).toBeGreaterThan(0);
  expect(screen.getAllByText(new RegExp(`Also caches ${selector}`)).length).toBeGreaterThan(0);
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
