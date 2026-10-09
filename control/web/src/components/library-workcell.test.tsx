import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {vi} from "vitest";
import type {ControlApi} from "../api/types";
import {activeFilterSummary, buildLibraryRecipeRecords, filterLibraryRecipeRecords, EMPTY_LIBRARY_WORKCELL_FILTERS, libraryFiltersFromSearch, libraryFiltersToSearch, LibraryWorkcell, recipeEngine} from "./library-workcell";
import {cacheRemovalReview} from "../test-fixtures/cache-removal";
import {libraryViewSnapshot} from "../test-fixtures/library";
import {recipeLibraryPath, modelKey} from "../lib/library-route";

test("renders every selected model's own recipe identities", () => {
  const model = libraryViewSnapshot.models.find(entry => entry.recipes.length > 1)!;
  render(<LibraryWorkcell api={{} as never} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} query="" route={{kind: "model", modelKey: modelKey(model.model)}} snapshot={libraryViewSnapshot}/>);
  const pane = screen.getByLabelText("Recipes matching selected Model");
  for (const recipe of model.recipes) {
    expect(pane.querySelector(`a[href="${recipeLibraryPath(recipe.recipe_id)}"]`)).toBeVisible();
  }
});
test("filters by exact model identity", () => {
  const records = buildLibraryRecipeRecords(libraryViewSnapshot);
  const key = records[0]!.modelKey;
  expect(filterLibraryRecipeRecords(records, {...EMPTY_LIBRARY_WORKCELL_FILTERS, model: key}, "").every(record => record.modelKey === key)).toBe(true);
});
test("shows the whole library by default and narrows to cached models on request", () => {
  // Break caught: the default hides everything that is not cached yet.
  const records = buildLibraryRecipeRecords(libraryViewSnapshot);
  expect(filterLibraryRecipeRecords(records, EMPTY_LIBRARY_WORKCELL_FILTERS, "")).toHaveLength(records.length);
  const cached = filterLibraryRecipeRecords(records, {...EMPTY_LIBRARY_WORKCELL_FILTERS, cached: true}, "");
  expect(cached.every(record => record.modelCached)).toBe(true);
  expect(cached.length).toBeLessThan(records.length);
});

test("an empty result names the filters that caused it, and none when none were set", () => {
  expect(activeFilterSummary(EMPTY_LIBRARY_WORKCELL_FILTERS, "")).toEqual([]);
  const summary = activeFilterSummary({...EMPTY_LIBRARY_WORKCELL_FILTERS, cached: true, usage: "chat"}, "qwen").join(" ");
  expect(summary).toContain("cached");
  expect(summary).toContain("chat");
  expect(summary).toContain("qwen");
});

test("keeps URL-selected Models in the paired right pane", () => {
  const model = libraryViewSnapshot.models[79]!;
  const key = modelKey(model.model);
  render(<LibraryWorkcell api={{} as never} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} query="" route={{kind: "model", modelKey: key}} snapshot={libraryViewSnapshot}/>);
  expect(screen.getByText("No Recipe linked")).toBeVisible();
  expect(screen.getByLabelText("Recipes matching selected Model")).toHaveTextContent("No Recipe linked");
});

test("removes a recipe only after an explicit model choice", async () => {
  let review = cacheRemovalReview();
  const recipeRemovalReview = vi.fn().mockImplementation(async (selector: string, withModel: boolean) => {
    review = cacheRemovalReview({selector, with_model: withModel});
    return review;
  });
  const removeRecipe = vi.fn().mockImplementation(async (selector: string, requestKey: string, withModel: boolean) => ({
    action: "remove", operation_id: "op", recipe_revision_id: review.target_identity,
    reclaimed_bytes: 0, request_key: requestKey, selector,
    with_model: withModel, state: "succeeded", progress: {phase: "complete"},
  }));
  const api = {removeRecipe, recipeRemovalReview} as unknown as ControlApi;
  const base = libraryViewSnapshot.models.find(entry => entry.recipes.length > 0)!;
  render(<LibraryWorkcell api={api} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} query="" route={{kind: "model", modelKey: modelKey(base.model)}} snapshot={libraryViewSnapshot}/>);

  // The Controller fails closed without a model decision, so the web asks for
  // one before issuing any request.
  fireEvent.click(screen.getAllByRole("button", {name: "Remove recipe"})[0]!);
  expect(removeRecipe).not.toHaveBeenCalled();
  fireEvent.click(screen.getAllByRole("button", {name: "Keep the model"})[0]!);
  await waitFor(() => expect(screen.getByLabelText("Cache removal review")).toHaveTextContent("42 bytes"));
  expect(removeRecipe).not.toHaveBeenCalled();
  expect(recipeRemovalReview).toHaveBeenCalledWith(expect.any(String), false, expect.anything());
  fireEvent.click(screen.getByRole("button", {name: "Confirm remove"}));
  await waitFor(() => expect(removeRecipe).toHaveBeenCalledTimes(1));
  expect(removeRecipe).toHaveBeenCalledWith(expect.any(String), expect.any(String), false, expect.anything());
  const [, , withModel] = (removeRecipe as unknown as {mock: {calls: [string, string, boolean][]}}).mock.calls[0]!;
  expect(withModel).toBe(false);
});

test("refreshes the whole cached recipe set with an explicit scope", async () => {
  const updateRecipes = vi.fn().mockResolvedValue({action: "update", children: [], state: "succeeded"});
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
  for (const name of ["Filter usage", "Filter family", "Filter version", "Filter quantization", "Filter creator", "Filter alignment", "Filter Sparks", "Sort Library", "Filter updated"]) {
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
  expect(new Set(records.map(record => record.key)).size).toBe(records.length);
  expect(screen.getAllByRole("button", {name: "Remove recipe"})).toHaveLength(records.length);
  expect(pane).toHaveTextContent(records[0]!.recipe!.recipe_document.provenance.attribution[0]!);
  expect(pane).toHaveTextContent(records[0]!.recipe!.recipe_document.runtime.engine);
  expect(pane).toHaveTextContent(records[1]!.recipe!.recipe_document.runtime.engine);
});

test("engine and creator narrow recipes, are read from the URL, and show as badges", () => {
  const records = buildLibraryRecipeRecords(libraryViewSnapshot).filter(record => record.recipe);
  const recipe = records[0]!.recipe!;
  const engine = recipeEngine(recipe);
  const creator = "MiaAI-Lab";
  const tagged = records.map((record, index) => index % 2 === 0 ? {...record, recipe: {...record.recipe!, creator}} : {...record, recipe: {...record.recipe!, creator: "tonyd2wild", engine: "sglang"}});
  const byCreator = filterLibraryRecipeRecords(tagged, {...EMPTY_LIBRARY_WORKCELL_FILTERS, creator: "miaai-lab"}, "");
  expect(byCreator.length).toBe(Math.ceil(tagged.length / 2));
  expect(byCreator.every(record => record.recipe!.creator === creator)).toBe(true);
  const byEngine = filterLibraryRecipeRecords(tagged, {...EMPTY_LIBRARY_WORKCELL_FILTERS, engine: "sglang"}, "");
  expect(byEngine.every(record => recipeEngine(record.recipe!) === "sglang")).toBe(true);
  expect(byEngine.length).toBeGreaterThan(0);
  expect(filterLibraryRecipeRecords(tagged, {...EMPTY_LIBRARY_WORKCELL_FILTERS, creator, engine: "sglang"}, "").every(record => record.recipe!.creator === creator && recipeEngine(record.recipe!) === "sglang")).toBe(true);
  expect(engine).toBeTruthy();

  const filters = libraryFiltersFromSearch(new URLSearchParams("engine=vllm&creator=MiaAI-Lab"));
  expect(filters).toMatchObject({engine: "vllm", creator: "MiaAI-Lab"});
  expect(libraryFiltersToSearch(filters).toString()).toBe("engine=vllm&creator=MiaAI-Lab");
  expect(activeFilterSummary(filters, "")).toEqual(["engine: vllm", "creator: MiaAI-Lab"]);
});

test("a recipe row shows engine, creator and Spark count badges", () => {
  const base = libraryViewSnapshot.models.find(entry => entry.recipes.length > 0)!;
  const snapshot = {...libraryViewSnapshot, models: libraryViewSnapshot.models.map(entry => entry === base ? {...entry, recipes: entry.recipes.map(recipe => ({...recipe, creator: "MiaAI-Lab"}))} : entry)};
  render(<LibraryWorkcell api={{} as never} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} query="" route={{kind: "model", modelKey: modelKey(base.model)}} snapshot={snapshot}/>);
  const pane = screen.getByLabelText("Recipes matching selected Model");
  expect(pane.querySelector('[data-badge="engine"]')).toBeTruthy();
  expect(pane.querySelector('[data-badge="creator"]')).toHaveTextContent("MiaAI-Lab");
  expect(pane.querySelector('[data-badge="sparks"]')).toHaveTextContent(/^\d+ Sparks?$/);
  expect(screen.getByRole("combobox", {name: "Filter engine"})).toBeVisible();
});
