import {fireEvent, render, screen, waitFor} from "@testing-library/react";
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
  const button = screen.getAllByRole("button", {name: "Prepare cache"})[0]!;
  fireEvent.click(button);
  await waitFor(() => expect(api.prepareModelCache).toHaveBeenCalledTimes(1));
  const [selector, requestKey] = (api.prepareModelCache as unknown as {mock: {calls: [string, string][]}}).mock.calls[0]!;
  expect(selector).toMatch(/^[^/]+\/[^/]+$/);
  expect(requestKey).toMatch(/^[0-9a-f-]{36}$/);
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
