import {render, screen} from "@testing-library/react";
import {App} from "../app";
import {librarySnapshot} from "../test-fixtures/library";
import type {ControlApi} from "../api/types";
import {loadLibrarySnapshot} from "./library";

test("renders the canonical model and recipe workcell", async () => {
  history.replaceState(null, "", "/library");
  render(<App api={{librarySnapshot: async () => librarySnapshot} as unknown as ControlApi}/>);
  expect(await screen.findByRole("heading", {name: "Library"})).toBeVisible();
  expect(screen.getByLabelText("Models")).toBeVisible();
  expect(screen.getByLabelText("Recipes matching selected Model")).toBeVisible();
  expect(screen.queryByRole("button", {name: /sync|import|prepare catalog/i})).not.toBeInTheDocument();
  expect(screen.queryByText(/sync|import|prepare catalog/i)).not.toBeInTheDocument();
});
test("keeps the exact model filter addressable", async () => {
  history.replaceState(null, "", "/library?view=models");
  render(<App api={{librarySnapshot: async () => librarySnapshot} as unknown as ControlApi}/>);
  expect(await screen.findByRole("heading", {name: "Models"})).toBeVisible();
  expect(screen.getByRole("combobox", {name: "Filter exact model"})).toBeVisible();
});
test("merges paged Library responses beyond the first 100 Models", async () => {
  const extraModels = Array.from({length: 9}, (_, index) => ({...librarySnapshot.models[0]!, model: {...librarySnapshot.models[0]!.model, slug: `paged-model-${index}`, content_sha256: `${index}`.repeat(64).slice(0, 64)}}));
  const calls: Array<string | undefined> = [];
  const api = {librarySnapshot: async (cursor?: string) => { calls.push(cursor); return cursor ? {...librarySnapshot, models: extraModels, next_cursor: null} : {...librarySnapshot, next_cursor: "page-2"}; }} as unknown as ControlApi;
  const merged = await loadLibrarySnapshot(api, new AbortController().signal);
  expect(merged.models).toHaveLength(101);
  expect(calls).toEqual([undefined, "page-2"]);
});
