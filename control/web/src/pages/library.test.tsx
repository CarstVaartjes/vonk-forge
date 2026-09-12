import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {App} from "../app";
import {modelLibrary, recipeLibrary} from "../test-fixtures/library";
import type {ControlApi} from "../api/types";
import {loadLibraryView} from "./library";
import {afterEach, vi} from "vitest";
import {ApiClient} from "../api/client";

afterEach(() => vi.unstubAllGlobals());

test("loads recipes for uncached models through the real API client", async () => {
  // The server defaults to locally cached models unless all_models is requested.
  // Stubbing ControlApi.recipeLibrary directly concealed the missing query flag.
  vi.stubGlobal("fetch", async (request: Request) => {
    const url = new URL(request.url);
    const body = url.pathname === "/api/model/library" ? {...modelLibrary, models: modelLibrary.models.map(model => ({...model, local: {...model.local, controller: "not_cached"}}))}
      : url.searchParams.get("all_models") === "true" ? recipeLibrary : {...recipeLibrary, recipes: []};
    return new Response(JSON.stringify(body), {status: 200, headers: {"Content-Type": "application/json"}});
  });
  const snapshot = await loadLibraryView(new ApiClient(), new AbortController().signal);
  expect(snapshot.models.some(model => model.recipes.length > 0)).toBe(true);
});

test("does not attach a recipe to another revision of the same model selector", async () => {
  const original = modelLibrary.models.find(model => recipeLibrary.recipes.some(recipe => recipe.model_selectors.includes(model.selector)))!;
  const changed = {...original, identity: {...original.identity, content_sha256: "f".repeat(64)}};
  const api = {modelLibrary: async () => ({...modelLibrary, models: [original, changed]}), recipeLibrary: async () => recipeLibrary} as unknown as ControlApi;
  const snapshot = await loadLibraryView(api, new AbortController().signal);
  expect(snapshot.models[0]!.recipes.length).toBeGreaterThan(0);
  expect(snapshot.models[1]!.recipes).toHaveLength(0);
});

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

type LibraryRequest = {cursor?: string; sort?: string; updatedSince?: string};

function pagedLibraryApi(calls: LibraryRequest[], api: Partial<ControlApi> = {}) {
  return {
    modelLibrary: async (cursor?: string, sort?: string, updatedSince?: string) => {
      calls.push({cursor, sort, updatedSince});
      return {...modelLibrary, next_cursor: cursor ? null : "page-2"};
    },
    recipeLibrary: async () => recipeLibrary,
    ...api,
  } as unknown as ControlApi;
}

test("restarts from the first page when the server sort changes", async () => {
  // Break caught: changing the sort only rewrites the URL, while the Library
  // keeps the cursor from the previous ordering (or never re-requests), so the
  // visible order is one page of the old order instead of the server's whole
  // library order.
  history.replaceState(null, "", "/library");
  const calls: LibraryRequest[] = [];
  render(<App api={pagedLibraryApi(calls)}/>);
  await waitFor(() => expect(calls.map(call => call.cursor)).toEqual([undefined, "page-2"]));

  fireEvent.change(screen.getByRole("combobox", {name: "Sort Library"}), {target: {value: "name"}});
  await waitFor(() => expect(calls.some(call => call.sort === "name")).toBe(true));
  const reload = calls.find(call => call.sort === "name")!;
  expect(reload.cursor).toBeUndefined();
  expect(reload.updatedSince).toBeUndefined();
});

test("restarts from the first page with the recency window as updated_since", async () => {
  // Break caught: the recency control changes the visible URL but the request
  // carries no updated_since (or reuses the old cursor), so the library shows
  // stale entries the operator asked to exclude.
  history.replaceState(null, "", "/library");
  const calls: LibraryRequest[] = [];
  render(<App api={pagedLibraryApi(calls)}/>);
  await waitFor(() => expect(calls.map(call => call.cursor)).toEqual([undefined, "page-2"]));

  fireEvent.change(screen.getByRole("combobox", {name: "Filter updated"}), {target: {value: "7d"}});
  await waitFor(() => expect(calls.some(call => call.updatedSince !== undefined)).toBe(true));
  const reload = calls.find(call => call.updatedSince !== undefined)!;
  expect(reload.cursor).toBeUndefined();
  const age = Date.now() - Date.parse(reload.updatedSince!);
  expect(age).toBeGreaterThan(6 * 24 * 3_600_000);
  expect(age).toBeLessThan(8 * 24 * 3_600_000);
});

test("reproduces a shared sort and recency link on the first request", async () => {
  // Break caught: the page stores the controls in the URL but seeds the request
  // only from component state, so opening a shared link drops the ordering and
  // shows the server default instead.
  history.replaceState(null, "", "/library?sort=name&updated=31d");
  const calls: LibraryRequest[] = [];
  render(<App api={pagedLibraryApi(calls)}/>);
  await waitFor(() => expect(calls.length).toBeGreaterThan(0));
  expect(calls[0]!.cursor).toBeUndefined();
  expect(calls[0]!.sort).toBe("name");
  const age = Date.now() - Date.parse(calls[0]!.updatedSince!);
  expect(age).toBeGreaterThan(30 * 24 * 3_600_000);
  expect(age).toBeLessThan(32 * 24 * 3_600_000);
});
