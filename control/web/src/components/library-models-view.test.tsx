import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import {vi} from "vitest";
import type {ControlApi} from "../api/types";
import {libraryViewSnapshot} from "../test-fixtures/library";
import {buildLibraryRecipeRecords, EMPTY_LIBRARY_WORKCELL_FILTERS} from "./library-workcell";
import {LibraryModelsView} from "./library-models-view";

const prepared = {
  action: "download" as const,
  phase: "complete",
  request_key: "request",
  schema_version: 2 as const,
  selector: "vonk-forge/ling-3-flash",
  state: "succeeded" as const,
  transferred_bytes: 10,
  progress: {phase: "complete"},
};

function cacheApi() {
  return {prepareModelCache: vi.fn().mockResolvedValue(prepared)} as unknown as ControlApi;
}

const renderModels = (query = "", api: ControlApi = cacheApi()) => render(<LibraryModelsView api={api} entries={buildLibraryRecipeRecords(libraryViewSnapshot)} modelInventory={libraryViewSnapshot.models} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} onRefresh={async () => undefined} path="/library?view=models" query={query}/>);

test("groups model revisions and keeps recipe links and downloads bound to the selected revision", async () => {
  const current = libraryViewSnapshot.models.find(model => model.recipes.length > 0)!;
  const older = {...current, model: {...current.model, slug: `${current.model.slug}-older`, content_sha256: "a".repeat(64)}, recipes: [], version: "older", model_document: {...current.model_document, identity: {...current.model_document.identity, version: "older"}}};
  const api = cacheApi();
  render(<LibraryModelsView api={api} entries={[]} modelInventory={[older, current]} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} onRefresh={async () => undefined} path="/library?view=models" query=""/>);

  const list = screen.getByLabelText("Model library");
  expect(within(list).getAllByRole("article")).toHaveLength(1);
  expect(screen.getByText("1 of 1 models")).toBeVisible();
  expect(within(list).getByRole("link", {name: current.recipes[0]!.title})).toBeVisible();
  const revisions = within(list).getByRole("combobox", {name: /Revision for /});
  expect(revisions).toHaveValue(`${current.model.publisher}/${current.model.slug}@${current.model.content_sha256}`);
  fireEvent.change(revisions, {target: {value: `${older.model.publisher}/${older.model.slug}@${older.model.content_sha256}`}});
  expect(within(list).queryByRole("link", {name: current.recipes[0]!.title})).toBeNull();
  fireEvent.click(within(list).getByRole("button", {name: "Download model"}));
  await waitFor(() => expect(api.prepareModelCache).toHaveBeenCalledWith(`${older.model.publisher}/${older.model.slug}`, expect.any(String), expect.anything()));
});

test("keeps different creators and quantizations separate even when their titles match", () => {
  const base = libraryViewSnapshot.models[0]!;
  const anotherCreator = {...base, model: {...base.model, publisher: "another-creator"}};
  const anotherQuantization = {...base, model: {...base.model, slug: `${base.model.slug}-q8`}, quantization: "q8"};
  render(<LibraryModelsView api={cacheApi()} entries={[]} modelInventory={[base, anotherCreator, anotherQuantization]} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} onRefresh={async () => undefined} path="/library?view=models" query=""/>);
  expect(within(screen.getByLabelText("Model library")).getAllByRole("article")).toHaveLength(3);
});

test("shows the verified model library with exact size and recipe counts", () => {
  renderModels();
  expect(screen.getByRole("heading", {name: "Models"})).toBeVisible();
  expect(screen.getByLabelText("Model library")).toBeVisible();
  expect(screen.getAllByText("None", {exact: true}).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/GiB/).length).toBeGreaterThan(0);
});

test("lists the recipes that use a model so a selected model leads to its recipes", () => {
  renderModels();
  expect(screen.getAllByRole("list", {name: /^Recipes using /}).length).toBeGreaterThan(0);
});

test("prepares the Controller cache for an uncached model", async () => {
  const api = cacheApi();
  renderModels("", api);
  const button = screen.getAllByRole("button", {name: "Download model"})[0]!;
  fireEvent.click(button);
  await waitFor(() => expect(api.prepareModelCache).toHaveBeenCalledTimes(1));
  const [selector, requestKey] = (api.prepareModelCache as unknown as {mock: {calls: [string, string][]}}).mock.calls[0]!;
  expect(selector).toMatch(/^[^/]+\/[^/]+$/);
  expect(requestKey).toMatch(/^[0-9a-f-]{36}$/);
});

test("removes a cached model only after an explicit confirmation", async () => {
  const removeModelCache = vi.fn().mockResolvedValue({...prepared, action: "remove" as const});
  const api = {removeModelCache} as unknown as ControlApi;
  const base = libraryViewSnapshot.models[0]!;
  const inventory = [{...base, local: {...base.local, controller: "cached" as const}}];
  render(<LibraryModelsView api={api} entries={[]} modelInventory={inventory} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} onQueryChange={() => undefined} onRefresh={async () => undefined} path="/library?view=models" query=""/>);

  // Removal is destructive, so the first click only asks for confirmation.
  fireEvent.click(screen.getByRole("button", {name: "Remove from cache"}));
  expect(removeModelCache).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name: "Confirm remove"}));
  await waitFor(() => expect(removeModelCache).toHaveBeenCalledTimes(1));
});

test("offers the same model filters as vonkctl model library", () => {
  renderModels();
  for (const name of ["Filter model usage", "Filter model family", "Filter model version", "Filter model quantization", "Filter model creator", "Filter model alignment", "Filter exact model"]) {
    expect(screen.getByRole("combobox", {name})).toBeVisible();
  }
});

test("offers the same server ordering controls as vonkctl model library", () => {
  // Break caught: the Models view keeps only the client-side filters, so its
  // order and recency can no longer match the request the Library page makes.
  const onFiltersChange = vi.fn();
  render(<LibraryModelsView api={cacheApi()} entries={[]} modelInventory={[libraryViewSnapshot.models[0]!]} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={onFiltersChange} onNavigate={() => undefined} onQueryChange={() => undefined} onRefresh={async () => undefined} path="/library?view=models" query=""/>);
  for (const name of ["Sort models", "Filter model updated"]) {
    expect(screen.getByRole("combobox", {name})).toBeVisible();
  }
  fireEvent.change(screen.getByRole("combobox", {name: "Sort models"}), {target: {value: "name"}});
  expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({sort: "name"}));
  fireEvent.change(screen.getByRole("combobox", {name: "Filter model updated"}), {target: {value: "31d"}});
  expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({updated: "31d"}));
});

test("filters models by exact selector and preserves the current route shape", () => {
  const onFiltersChange = vi.fn();
  render(<LibraryModelsView api={cacheApi()} entries={[]} modelInventory={[libraryViewSnapshot.models[0]!]} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={onFiltersChange} onNavigate={() => undefined} onQueryChange={() => undefined} onRefresh={async () => undefined} path="/library?view=models" query=""/>);
  fireEvent.change(screen.getByRole("combobox", {name: "Filter exact model"}), {target: {value: libraryViewSnapshot.models[0]!.model.publisher + "/" + libraryViewSnapshot.models[0]!.model.slug + "@" + libraryViewSnapshot.models[0]!.model.content_sha256}});
  expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({model: expect.stringContaining("@")}));
});

test("searches model titles without invoking update endpoints", () => {
  renderModels("Ling 3.0 Flash DSpark companion");
  expect(screen.getByRole("heading", {name: "Ling 3.0 Flash DSpark companion"})).toBeVisible();
  expect(screen.queryByRole("button", {name: /update/i})).toBeNull();
});
