import {act, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi} from "../api/types";
import {App} from "../app";
import {codeRecipe, fullLibraryDetail, librarySnapshot, minimalLibraryDetail, unlinkedRecipe} from "../test-fixtures/library";

const qwenModel = `qwen/3@${"e".repeat(64)}`;
const qwenModelPath = `/library/models/${encodeURIComponent(qwenModel)}`;

afterEach(() => {
  history.replaceState(null, "", "/");
  vi.restoreAllMocks();
});

test("summarizes the loaded Library window and filters recipes without changing the route", async () => {
  history.replaceState(null, "", "/library");
  const api = {librarySnapshot: async () => librarySnapshot} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  expect(await screen.findByRole("heading", {name: "Library"})).toBeVisible();
  const summary = screen.getByRole("region", {name: "Library summary"});
  expect(within(summary).getByRole("group", {name: "1 model version"})).toBeVisible();
  expect(within(summary).getByRole("group", {name: "3 recipes"})).toBeVisible();
  expect(within(summary).getByRole("group", {name: "2 linked"})).toBeVisible();
  expect(within(summary).getByRole("group", {name: "1 needs a model version"})).toBeVisible();

  await user.click(screen.getByRole("link", {name: /qwen\/3/}));

  const search = screen.getByRole("searchbox", {name: "Search Library"});
  await user.type(search, "Qwen Code");

  const models = screen.getByRole("region", {name: "Models"});
  expect(within(models).getByRole("link", {name: /qwen\/3/})).toBeVisible();
  const recipes = screen.getByRole("region", {name: /Recipes for/});
  expect(within(recipes).getByRole("link", {name: /Qwen Code/})).toBeVisible();
  expect(within(recipes).queryByRole("link", {name: /Qwen Chat/})).not.toBeInTheDocument();
  expect(screen.getByRole("button", {name: "Clear Library search"})).toBeVisible();

  await user.click(screen.getByRole("button", {name: "Clear Library search"}));
  expect(within(recipes).getByRole("link", {name: /Qwen Chat/})).toBeVisible();
  expect(history.state).toBeNull();
  expect(location.pathname).toBe(qwenModelPath);
});

test("shows visual recipe truth and selects only one complete placement group on activation", async () => {
  // Break caught: the UI reduces placement to node checkboxes, hides stale or
  // reservation evidence, calls a bounded search optimal, or omits typed
  // authority reasons and immutable recipe/resource detail.
  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: async () => fullLibraryDetail,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const detail = await screen.findByRole("region", {name: "Qwen Chat recipe authority"});
  expect(within(detail).getByText("Immutable revision 3")).toBeVisible();
  expect(within(detail).getByText(`qwen/qwen3@${"e".repeat(64)}`)).toBeVisible();
  expect(within(detail).getByText(`vonk-forge/vllm-openai@${"f".repeat(64)}`)).toBeVisible();
  expect(within(detail).getByText(`vonk-forge/python-312-cuda@${"1".repeat(64)}`)).toBeVisible();
  const identity = within(detail).getByRole("region", {name: "Recipe identity"});
  expect(within(identity).getByText("Model version")).toBeVisible();
  expect(within(identity).getByText("Execution harness")).toBeVisible();
  expect(within(identity).getByText("Runtime distribution")).toBeVisible();
  expect(within(detail).getByRole("region", {name: "Lifecycle overview"})).toBeVisible();
  const topology = within(detail).getByRole("region", {name: "Topology and resources"});
  expect(within(topology).getByText("2 nodes · tensor_parallel")).toBeVisible();
  expect(within(topology).getByText("Rank 0 · leader · endpoint owner")).toBeVisible();
  expect(within(topology).getByText("Rank 1 · worker")).toBeVisible();
  expect(within(topology).getByText("144.0 GiB startup memory total")).toBeVisible();
  expect(within(topology).getByText("140.0 GiB disk envelope total")).toBeVisible();
  expect(within(detail).getByText("Build succeeded")).toBeVisible();
  expect(within(detail).getByText("Installation installed")).toBeVisible();
  expect(within(detail).getByText("Visual recipe fields are bounded to the selected immutable revision.")).toBeVisible();
  expect(within(detail).queryByRole("link", {name: "Source and build"})).not.toBeInTheDocument();
  expect(within(detail).queryByRole("link", {name: "Cluster mapping"})).not.toBeInTheDocument();
  expect(within(detail).queryByRole("link", {name: "Raw editor"})).not.toBeInTheDocument();

  const groups = within(detail).getByRole("region", {name: "Complete placement groups"});
  const select = within(groups).getByRole("button", {name: "Select complete group node-alpha and node-beta"});
  expect(select).toHaveAttribute("aria-pressed", "false");
  select.focus();
  expect(select).toHaveFocus();
  expect(select).toHaveAttribute("aria-pressed", "false");
  expect(within(groups).queryByRole("checkbox")).not.toBeInTheDocument();

  await user.keyboard(" ");
  expect(select).toHaveAttribute("aria-pressed", "true");
  const selected = select.closest("article")!;
  expect(within(selected).getByText("Complete group · Installed · Not loaded")).toBeVisible();
  expect(within(selected).getByText("Rank 0 · leader")).toBeVisible();
  expect(within(selected).getByText("Rank 1 · worker")).toBeVisible();
  expect(within(selected).getByText("Inventory fresh · 10s")).toBeVisible();
  expect(within(selected).getByText("Telemetry live · 2s")).toBeVisible();
  expect(within(selected).getByText("Telemetry delayed · 12s")).toBeVisible();
  expect(within(selected).getAllByText("5.0 GiB disk reserved", {exact: false})).toHaveLength(2);
  expect(within(selected).getAllByText("4.0 GiB memory reserved", {exact: false})).toHaveLength(2);
  expect(within(selected).getByText("40.0 GiB of exact artifacts can be reused.")).toBeVisible();
  const boundedSearch = within(groups).getByRole("note");
  expect(within(boundedSearch).getByText(/stopped after 512 complete groups/)).toBeVisible();
  expect(within(boundedSearch).getByText(/not a globally optimal placement/)).toBeVisible();
  expect(within(groups).getByText("Admission inventory is stale for this complete group.")).toBeInTheDocument();
  expect(within(groups).getByText("Live capacity evidence is stale for this complete group.")).toBeInTheDocument();
  expect(within(groups).getByText("Admission inventory has not been reported.")).toBeInTheDocument();

  const placementPosition = detail.compareDocumentPosition(groups);
  const topologyPosition = groups.compareDocumentPosition(topology);
  expect(placementPosition & Node.DOCUMENT_POSITION_CONTAINED_BY).toBeTruthy();
  expect(topologyPosition & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const unavailable = within(groups).getByText("Unavailable placement evidence").closest("details");
  expect(unavailable).not.toHaveAttribute("open");
  const actions = within(selected).getByRole("region", {name: "Selected group actions"});
  const evidence = selected.querySelector(".placement-evidence")!;
  expect(actions.compareDocumentPosition(evidence) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("loads and merges cursor pages without splitting model or unlinked recipe groups", async () => {
  // Break caught: a many-recipe Library silently stops at the first page or
  // creates duplicate exact model-version groups across cursor pages.
  history.replaceState(null, "", qwenModelPath);
  const firstPage = {
    ...librarySnapshot,
    models: [{...librarySnapshot.models[0], recipes: [librarySnapshot.models[0].recipes[0]]}],
    unlinked_recipes: [],
    next_cursor: "page-2",
  };
  const secondPage = {
    ...librarySnapshot,
    models: [{...librarySnapshot.models[0], recipes: [codeRecipe]}],
    unlinked_recipes: [unlinkedRecipe],
    next_cursor: null,
  };
  const librarySnapshotRequest = vi.fn(async (cursor?: string) => cursor === "page-2" ? secondPage : firstPage);
  const api = {librarySnapshot: librarySnapshotRequest} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const models = await screen.findByRole("region", {name: "Models"});
  expect(within(models).getByText("1 recipe")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Load more Library recipes"}));

  await waitFor(() => expect(librarySnapshotRequest).toHaveBeenCalledWith("page-2", expect.any(AbortSignal)));
  expect(within(models).getByText("2 recipes")).toBeVisible();
  const recipes = screen.getByRole("region", {name: `Recipes for ${qwenModel}`});
  expect(within(recipes).getByRole("link", {name: /Qwen Chat/})).toBeVisible();
  expect(within(recipes).getByRole("link", {name: /Qwen Code/})).toBeVisible();
  expect(within(models).getByRole("link", {name: /Unlinked/})).toBeVisible();
  expect(screen.queryByRole("button", {name: "Load more Library recipes"})).not.toBeInTheDocument();
});

test("bounds repeated cursor pages while pinning selected and unlinked navigation context", async () => {
  // Break caught: every cursor page remains mounted, so model/recipe DOM grows
  // without limit or a capped merge silently drops the active route context.
  history.replaceState(null, "", "/library/recipes/recipe-pinned");
  const pinned = {...codeRecipe, recipe_id: "recipe-pinned", slug: "pinned", title: "Pinned Recipe"};
  const page = (pageNumber: number) => {
    const linked = Array.from({length: 20}, (_, index) => pageNumber === 0 && index === 0
      ? pinned
      : {...codeRecipe, recipe_id: `recipe-${pageNumber}-${index}`, slug: `recipe-${pageNumber}-${index}`, title: `Recipe ${pageNumber}-${index}`});
    const unlinked = Array.from({length: 20}, (_, index) => ({
      ...unlinkedRecipe,
      recipe_id: `unlinked-${pageNumber}-${index}`,
      slug: `unlinked-${pageNumber}-${index}`,
      title: `Unlinked ${pageNumber}-${index}`,
    }));
    const extraModels = Array.from({length: 20}, (_, index) => ({
      model: {kind: "model-version" as const, publisher: "fixture", slug: `model-${pageNumber}-${index}`, content_sha256: `${pageNumber}`.repeat(64)},
      page_local: true,
      recipes: [{...codeRecipe, recipe_id: `family-recipe-${pageNumber}-${index}`, slug: `family-${pageNumber}-${index}`, title: `Family recipe ${pageNumber}-${index}`}],
    }));
    return {
      ...librarySnapshot,
      models: [{...librarySnapshot.models[0], recipes: linked}, ...extraModels],
      unlinked_recipes: unlinked,
      next_cursor: pageNumber < 3 ? `page-${pageNumber + 1}` : null,
    };
  };
  const librarySnapshotRequest = vi.fn(async (cursor?: string) => page(cursor ? Number(cursor.slice(5)) : 0));
  const api = {
    librarySnapshot: librarySnapshotRequest,
    libraryRecipe: async () => ({...minimalLibraryDetail, recipe: {...minimalLibraryDetail.recipe, recipe_id: pinned.recipe_id, slug: pinned.slug, title: pinned.title}}),
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  await screen.findByRole("region", {name: "Pinned Recipe recipe authority"});
  for (let pageNumber = 1; pageNumber <= 3; pageNumber += 1) {
    await user.click(screen.getByRole("button", {name: "Load more Library recipes"}));
    await waitFor(() => expect(librarySnapshotRequest).toHaveBeenCalledTimes(pageNumber + 1));
  }

  const notice = screen.getByRole("status", {name: "Bounded Library window"});
  expect(notice).toHaveTextContent("Showing up to 50 recipes per list and 40 models");
  expect(notice).toHaveTextContent("No more server pages");
  expect(screen.queryByRole("button", {name: "Load more Library recipes"})).not.toBeInTheDocument();

  const models = screen.getByRole("region", {name: "Models"});
  expect(within(models).getAllByRole("link").length).toBeLessThanOrEqual(41);
  expect(within(models).getByRole("link", {name: new RegExp(qwenModel)})).toBeVisible();
  const recipes = screen.getByRole("region", {name: `Recipes for ${qwenModel}`});
  expect(recipes.querySelectorAll(".library-row")).toHaveLength(50);
  expect(within(recipes).getByRole("link", {name: /Pinned Recipe/})).toBeVisible();
  expect(within(recipes).getByRole("link", {name: /Recipe 3-19/})).toBeVisible();

  await user.click(within(models).getByRole("link", {name: /Unlinked/}));
  const unlinked = screen.getByRole("region", {name: "Unlinked recipes"});
  expect(unlinked.querySelectorAll(".library-row")).toHaveLength(50);
  expect(within(unlinked).getByRole("link", {name: /Unlinked 3-19/})).toBeVisible();
});

test("restores an evicted recipe parent when browser Back revisits bounded history", async () => {
  // Break caught: loading more while B is active can evict visited A; browser
  // Back then loses A's model list, selected row, and parent-specific Back link.
  history.replaceState(null, "", qwenModelPath);
  const recipe = (id: string, title: string) => ({
    ...codeRecipe,
    recipe_id: id,
    slug: id,
    title,
  });
  const recipeA = recipe("recipe-a", "Recipe A");
  const recipeB = recipe("recipe-b", "Recipe B");
  const firstRecipes = [recipeA, recipeB, ...Array.from({length: 48}, (_, index) => recipe(`first-${index}`, `First ${index}`))];
  const nextRecipes = Array.from({length: 50}, (_, index) => recipe(`next-${index}`, `Next ${index}`));
  const firstPage = {
    ...librarySnapshot,
    models: [{...librarySnapshot.models[0], recipes: firstRecipes}],
    unlinked_recipes: [],
    next_cursor: "page-2",
  };
  const secondPage = {
    ...librarySnapshot,
    models: [{...librarySnapshot.models[0], recipes: nextRecipes}],
    unlinked_recipes: [],
    next_cursor: null,
  };
  const librarySnapshotRequest = vi.fn(async (cursor?: string) => cursor === "page-2" ? secondPage : firstPage);
  const api = {
    librarySnapshot: librarySnapshotRequest,
    libraryRecipe: async (recipeId: string) => {
      const selected = recipeId === recipeA.recipe_id ? recipeA : recipeB;
      return {
        ...minimalLibraryDetail,
        recipe: {
          ...minimalLibraryDetail.recipe,
          recipe_id: selected.recipe_id,
          slug: selected.slug,
          title: selected.title,
        },
      };
    },
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  let recipes = await screen.findByRole("region", {name: `Recipes for ${qwenModel}`});
  await user.click(within(recipes).getByRole("link", {name: /Recipe A/}));
  await screen.findByRole("region", {name: "Recipe A recipe authority"});
  await user.click(within(recipes).getByRole("link", {name: /Recipe B/}));
  await screen.findByRole("region", {name: "Recipe B recipe authority"});
  await user.click(screen.getByRole("button", {name: "Load more Library recipes"}));
  await waitFor(() => expect(librarySnapshotRequest).toHaveBeenCalledWith("page-2", expect.any(AbortSignal)));

  recipes = screen.getByRole("region", {name: `Recipes for ${qwenModel}`});
  expect(recipes.querySelectorAll(".library-row")).toHaveLength(50);
  expect(within(recipes).queryByRole("link", {name: /Recipe A/})).not.toBeInTheDocument();

  act(() => {
    history.replaceState(null, "", "/library/recipes/recipe-a");
    dispatchEvent(new PopStateEvent("popstate"));
  });

  await screen.findByRole("region", {name: "Recipe A recipe authority"});
  recipes = screen.getByRole("region", {name: `Recipes for ${qwenModel}`});
  expect(recipes.querySelectorAll(".library-row")).toHaveLength(50);
  expect(within(recipes).getByRole("link", {name: /Recipe A/})).toHaveAttribute("aria-current", "page");
  expect(screen.getByRole("link", {name: `Back to ${qwenModel} recipes`})).toHaveAttribute("href", qwenModelPath);
});

test("changes URL selection only on activation and preserves drill-down history", async () => {
  // Break caught: focus selects or fetches a model/recipe, one-version recipes
  // collapse into one row, unlinked recipes disappear, or navigation replaces
  // browser history instead of creating addressable selections.
  history.replaceState(null, "", "/library");
  const libraryRecipe = vi.fn(async () => minimalLibraryDetail);
  const pushState = vi.spyOn(history, "pushState");
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const heading = await screen.findByRole("heading", {name: "Library"});
  const models = await screen.findByRole("region", {name: "Models"});
  const qwen = within(models).getByRole("link", {name: new RegExp(qwenModel)});
  expect(within(models).getByText("2 recipes")).toBeVisible();
  expect(within(models).getByRole("link", {name: /Unlinked/})).toBeVisible();

  qwen.focus();
  expect(qwen).toHaveFocus();
  expect(location.pathname).toBe("/library");
  expect(libraryRecipe).not.toHaveBeenCalled();

  await user.keyboard("{Enter}");
  expect(location.pathname).toBe(qwenModelPath);
  expect(pushState).toHaveBeenLastCalledWith(null, "", qwenModelPath);
  await waitFor(() => expect(heading).toHaveFocus());

  const recipes = screen.getByRole("region", {name: `Recipes for ${qwenModel}`});
  expect(within(recipes).getByRole("link", {name: /Qwen Chat/})).toBeVisible();
  expect(within(recipes).getByRole("link", {name: /Qwen Code/})).toBeVisible();
  expect(within(recipes).getByRole("link", {name: "Back to Models"})).toHaveAttribute("href", "/library");

  const chat = within(recipes).getByRole("link", {name: /Qwen Chat/});
  chat.focus();
  expect(location.pathname).toBe(qwenModelPath);
  expect(libraryRecipe).not.toHaveBeenCalled();

  await user.keyboard("{Enter}");
  expect(location.pathname).toBe("/library/recipes/recipe-chat");
  expect(pushState).toHaveBeenLastCalledWith(null, "", "/library/recipes/recipe-chat");
  expect(await screen.findByRole("heading", {name: "Qwen Chat"})).toBeVisible();
  expect(screen.getByRole("link", {name: `Back to ${qwenModel} recipes`})).toHaveAttribute("href", qwenModelPath);
  expect(libraryRecipe).toHaveBeenCalledTimes(1);

  act(() => {
    history.replaceState(null, "", qwenModelPath);
    dispatchEvent(new PopStateEvent("popstate"));
  });
  expect(screen.getByRole("region", {name: `Recipes for ${qwenModel}`})).toBeVisible();
  await waitFor(() => expect(heading).toHaveFocus());
});

test("preserves the explicit unlinked-list parent through detail and Back navigation", async () => {
  // Break caught: a recipe without an exact model version loses its recipe list and
  // returns to the Library root instead of its addressable unlinked parent.
  history.replaceState(null, "", "/library");
  const unlinkedDetail = {
    ...minimalLibraryDetail,
    recipe: {
      recipe_id: unlinkedRecipe.recipe_id,
      slug: unlinkedRecipe.slug,
      title: unlinkedRecipe.title,
      description: unlinkedRecipe.description,
      source_kind: unlinkedRecipe.source_kind,
    },
  };
  const api = {
    librarySnapshot: async () => librarySnapshot,
    libraryRecipe: async () => unlinkedDetail,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);

  const models = await screen.findByRole("region", {name: "Models"});
  await user.click(within(models).getByRole("link", {name: /Unlinked/}));
  expect(location.pathname).toBe("/library/models/~unlinked");
  const recipes = screen.getByRole("region", {name: "Unlinked recipes"});
  await user.click(within(recipes).getByRole("link", {name: /Custom Runtime/}));

  expect(location.pathname).toBe("/library/recipes/recipe-unlinked");
  expect(screen.getByRole("region", {name: "Unlinked recipes"})).toBeInTheDocument();
  const back = screen.getByRole("link", {name: "Back to Unlinked recipes"});
  expect(back).toHaveAttribute("href", "/library/models/~unlinked");
  await user.click(back);
  expect(location.pathname).toBe("/library/models/~unlinked");
  expect(screen.getByRole("region", {name: "Unlinked recipes"})).toBeVisible();
});

test("recovers in place from snapshot and recipe-detail request errors", async () => {
  // Break caught: a transient local authority error strands the operator on a
  // dead-end alert and requires an unrelated route or full-page navigation.
  history.replaceState(null, "", "/library");
  const librarySnapshotRequest = vi.fn()
    .mockRejectedValueOnce(new Error("fixture snapshot unavailable"))
    .mockResolvedValue(librarySnapshot);
  const snapshotApi = {librarySnapshot: librarySnapshotRequest} as unknown as ControlApi;
  const user = userEvent.setup();
  const first = render(<App api={snapshotApi}/>);

  expect(await screen.findByRole("alert")).toHaveTextContent("fixture snapshot unavailable");
  await user.click(screen.getByRole("button", {name: "Retry Library"}));
  expect(await screen.findByRole("region", {name: "Models"})).toBeVisible();
  expect(librarySnapshotRequest).toHaveBeenCalledTimes(2);
  first.unmount();

  history.replaceState(null, "", "/library/recipes/recipe-chat");
  const libraryRecipe = vi.fn()
    .mockRejectedValueOnce(new Error("fixture detail unavailable"))
    .mockResolvedValue(fullLibraryDetail);
  render(<App api={{librarySnapshot: async () => librarySnapshot, libraryRecipe} as unknown as ControlApi}/>);

  const detailAlert = await screen.findByRole("alert");
  expect(detailAlert).toHaveTextContent("fixture detail unavailable");
  await user.click(screen.getByRole("button", {name: "Retry recipe detail"}));
  await waitFor(() => expect(libraryRecipe).toHaveBeenCalledTimes(2));
  expect(await screen.findByRole("region", {name: "Qwen Chat recipe authority"})).toBeVisible();
});

test("keeps the empty Library state inside the reduced workspace", async () => {
  // Break caught: an empty local fixture still advertises a deleted workflow
  // instead of keeping the operator in the Library.
  history.replaceState(null, "", "/library");
  const empty = {...librarySnapshot, models: [], unlinked_recipes: []};
  render(<App api={{librarySnapshot: async () => empty} as unknown as ControlApi}/>);

  expect(await screen.findByRole("heading", {name: "No recipes in the Library"})).toBeVisible();
  expect(screen.getByText("Recipes will appear here after they are added to the local library authority.")).toBeVisible();
  expect(screen.queryByRole("link", {name: "Open advanced catalog"})).not.toBeInTheDocument();
});
test("offers custom recipe authoring with validation and save", async () => {
  history.replaceState(null, "", "/library");
  const createCatalogRecipe = vi.fn(async (input: {slug: string; document: Record<string, unknown>}) => ({recipe_id: "custom-1", revision_number: 1, lifecycle: "draft", slug: input.slug, document: input.document}));
  const api = {librarySnapshot: async () => ({...librarySnapshot, models: [], unlinked_recipes: []}), createCatalogRecipe} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);
  await user.click(await screen.findByRole("button", {name: "Create custom recipe"}));
  await user.type(screen.getByRole("textbox", {name: "Recipe slug"}), "custom-service");
  await user.click(screen.getByRole("button", {name: "Validate recipe"}));
  expect(screen.getByRole("status", {name: "Recipe validation"})).toHaveTextContent("Recipe document valid");
  await user.click(screen.getByRole("button", {name: "Save custom recipe"}));
  expect(createCatalogRecipe).toHaveBeenCalledWith(expect.objectContaining({slug: "custom-service", document: expect.any(Object)}));
  expect(await screen.findByRole("status", {name: "Recipe authoring"})).toHaveTextContent("Recipe saved");
});

test("previews a public recipe import with exact identity and persists only after confirmation", async () => {
  history.replaceState(null, "", "/library");
  const previewPublicRecipe = vi.fn(async () => ({publisher: "vonk", slug: "service", title: "Service", description: "", tags: [], uri: "vonk://catalog/vonk/service@sha256:" + "a".repeat(64), content_sha256: "a".repeat(64), source: "global"}));
  const importPublicRecipe = vi.fn(async () => ({recipe_id: "remote-1", revision_number: 4, lifecycle: "draft", slug: "service"}));
  const api = {librarySnapshot: async () => ({...librarySnapshot, models: [], unlinked_recipes: []}), listPublicRecipes: async () => ({repository: "CarstVaartjes/vonk-forge-recipes", commit: "a".repeat(40), recipes: []}), previewPublicRecipe, importPublicRecipe} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);
  await user.click(await screen.findByRole("button", {name: "Import public recipe"}));
  await user.type(screen.getByRole("textbox", {name: "Public recipe URI"}), "vonk://catalog/vonk/service@sha256:" + "a".repeat(64));
  await user.click(screen.getByRole("button", {name: "Preview public import"}));
  const preview = await screen.findByRole("region", {name: "Public recipe import preview"});
  expect(within(preview).getByText("vonk/service")).toBeVisible();
  expect(within(preview).getByText("sha256:" + "a".repeat(64))).toBeVisible();
  await user.click(within(preview).getByRole("button", {name: "Import reviewed recipe"}));
  expect(importPublicRecipe).toHaveBeenCalledWith(expect.stringContaining("vonk://catalog/vonk/service"), "a".repeat(64));
});

test("loads the current default catalog recipes when public import opens", async () => {
  history.replaceState(null, "", "/library");
  const uri = "vonk://catalog/vonk-forge/qwen@sha256:" + "b".repeat(64);
  const listPublicRecipes = vi.fn(async () => ({
    repository: "CarstVaartjes/vonk-forge-recipes",
    commit: "c".repeat(40),
    recipes: [{publisher: "vonk-forge", slug: "qwen", title: "Qwen 3.5 · vLLM · single Spark", description: "A fast language recipe.", tags: ["qwen", "vllm"], uri, content_sha256: "b".repeat(64)}],
  }));
  const api = {librarySnapshot: async () => ({...librarySnapshot, models: [], unlinked_recipes: []}), listPublicRecipes} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<App api={api}/>);
  await user.click(await screen.findByRole("button", {name: "Import public recipe"}));

  expect(listPublicRecipes).toHaveBeenCalledTimes(1);
  const select = await screen.findByRole("combobox", {name: "Default catalog recipe"});
  expect(within(select).getByRole("option", {name: /Qwen 3\.5/})).toBeVisible();
  await user.selectOptions(select, uri);
  expect(screen.getByRole("textbox", {name: "Public recipe URI"})).toHaveValue(uri);
});
