import {fireEvent, render, screen} from "@testing-library/react";
import {vi} from "vitest";
import {libraryViewSnapshot} from "../test-fixtures/library";
import {buildLibraryRecipeRecords, EMPTY_LIBRARY_WORKCELL_FILTERS} from "./library-workcell";
import {LibraryModelsView} from "./library-models-view";

const renderModels = (query = "") => render(<LibraryModelsView api={{} as never} entries={buildLibraryRecipeRecords(libraryViewSnapshot)} modelInventory={libraryViewSnapshot.models} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} path="/library?view=models" onQueryChange={() => undefined} query={query}/>);

test("shows the verified model library with exact size and recipe counts", () => {
  renderModels();
  expect(screen.getByRole("heading", {name: "Models"})).toBeVisible();
  expect(screen.getByLabelText("Model library")).toBeVisible();
  expect(screen.getAllByText("None", {exact: true}).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/GiB/).length).toBeGreaterThan(0);
});

test("filters models by exact selector and preserves the current route shape", () => {
  const onFiltersChange = vi.fn();
  render(<LibraryModelsView api={{} as never} entries={[]} modelInventory={[libraryViewSnapshot.models[0]!]} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={onFiltersChange} onNavigate={() => undefined} path="/library?view=models" onQueryChange={() => undefined} query=""/>);
  fireEvent.change(screen.getByRole("combobox", {name: "Filter exact model"}), {target: {value: libraryViewSnapshot.models[0]!.model.publisher + "/" + libraryViewSnapshot.models[0]!.model.slug + "@" + libraryViewSnapshot.models[0]!.model.content_sha256}});
  expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({model: expect.stringContaining("@")}));
});

test("searches model titles without invoking cache or update endpoints", () => {
  renderModels("Ling 3.0 Flash DSpark companion");
  expect(screen.getByRole("heading", {name: "Ling 3.0 Flash DSpark companion"})).toBeVisible();
  expect(screen.queryByRole("button", {name: /update/i})).toBeNull();
});
