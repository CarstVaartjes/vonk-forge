import {render, screen} from "@testing-library/react";
import {App} from "../app";
import {modelLibrary, recipeLibrary} from "../test-fixtures/library";
import type {ControlApi} from "../api/types";
import {loadLibraryView} from "./library";

const libraryApi = {modelLibrary: async () => modelLibrary, recipeLibrary: async () => recipeLibrary};

test("renders the canonical model and recipe workcell", async () => {
  history.replaceState(null, "", "/library");
  render(<App api={libraryApi as unknown as ControlApi}/>);
  expect(await screen.findByRole("heading", {name: "Library"})).toBeVisible();
  expect(screen.getByLabelText("Models")).toBeVisible();
  expect(screen.getByLabelText("Recipes matching selected Model")).toBeVisible();
  expect(screen.queryByRole("button", {name: /sync|import|prepare catalog/i})).not.toBeInTheDocument();
  expect(screen.queryByText(/sync|import|prepare catalog/i)).not.toBeInTheDocument();
});
test("keeps the exact model filter addressable", async () => {
  history.replaceState(null, "", "/library?view=models");
  render(<App api={libraryApi as unknown as ControlApi}/>);
  expect(await screen.findByRole("heading", {name: "Models"})).toBeVisible();
  expect(screen.getByRole("combobox", {name: "Filter exact model"})).toBeVisible();
});
test("merges paged Library responses beyond the first 100 Models", async () => {
  const extraModels = Array.from({length: 9}, (_, index) => ({...modelLibrary.models[0]!, selector: `paged-model-${index}`, identity: {...modelLibrary.models[0]!.identity, slug: `paged-model-${index}`, content_sha256: `${index}`.repeat(64).slice(0, 64)}}));
  const calls: Array<string | undefined> = [];
  const api = {modelLibrary: async (cursor?: string) => { calls.push(cursor); return cursor ? {...modelLibrary, models: extraModels, next_cursor: null} : {...modelLibrary, next_cursor: "page-2"}; }, recipeLibrary: async () => recipeLibrary} as unknown as ControlApi;
  const merged = await loadLibraryView(api, new AbortController().signal);
  expect(merged.models).toHaveLength(101);
  expect(calls).toEqual([undefined, "page-2"]);
});
