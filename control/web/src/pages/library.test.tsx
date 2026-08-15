import {act, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi} from "../api/types";
import {App} from "../app";
import {codeRecipe, fullLibraryDetail, librarySnapshot, minimalLibraryDetail, unlinkedRecipe} from "../test-fixtures/library";

afterEach(() => {
  history.replaceState(null, "", "/");
  vi.restoreAllMocks();
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
  expect(within(detail).getByText("qwen/3")).toBeVisible();
  expect(within(detail).getByText("openai.chat")).toBeVisible();
  expect(within(detail).getByText("tools")).toBeVisible();
  const topology = within(detail).getByRole("region", {name: "Topology and resources"});
  expect(within(topology).getByText("2 nodes · tensor_parallel · declared")).toBeVisible();
  expect(within(topology).getByText("Rank 0 · leader · endpoint owner")).toBeVisible();
  expect(within(topology).getByText("Rank 1 · worker")).toBeVisible();
  expect(within(topology).getByText("144.0 GiB startup memory total")).toBeVisible();
  expect(within(topology).getByText("140.0 GiB disk envelope total")).toBeVisible();
  expect(within(detail).getByText("Build succeeded")).toBeVisible();
  expect(within(detail).getByText("Installation installed")).toBeVisible();
  expect(within(detail).getByText("Visual recipe fields are bounded to the selected immutable revision.")).toBeVisible();
  expect(within(detail).getByRole("link", {name: "Source and build"})).toHaveAttribute("href", "/catalog/recipe-chat/source");
  expect(within(detail).getByRole("link", {name: "Cluster mapping"})).toHaveAttribute("href", "/catalog/recipe-chat/map");
  expect(within(detail).getByRole("link", {name: "Raw editor"})).toHaveAttribute("href", "/catalog/recipe-chat");

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
  // creates duplicate model groups instead of preserving family membership.
  history.replaceState(null, "", "/library/models/qwen%2F3");
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
  const recipes = screen.getByRole("region", {name: "Recipes for Qwen 3"});
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
      family: `family/${pageNumber}-${index}`,
      display_name: `Family ${pageNumber}-${index}`,
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
  expect(within(models).getByRole("link", {name: /Qwen 3/})).toBeVisible();
  const recipes = screen.getByRole("region", {name: "Recipes for Qwen 3"});
  expect(recipes.querySelectorAll(".library-row")).toHaveLength(50);
  expect(within(recipes).getByRole("link", {name: /Pinned Recipe/})).toBeVisible();
  expect(within(recipes).getByRole("link", {name: /Recipe 3-19/})).toBeVisible();

  await user.click(within(models).getByRole("link", {name: /Unlinked/}));
  const unlinked = screen.getByRole("region", {name: "Unlinked recipes"});
  expect(unlinked.querySelectorAll(".library-row")).toHaveLength(50);
  expect(within(unlinked).getByRole("link", {name: /Unlinked 3-19/})).toBeVisible();
});

test("changes URL selection only on activation and preserves drill-down history", async () => {
  // Break caught: focus selects or fetches a model/recipe, one-family recipes
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
  const qwen = within(models).getByRole("link", {name: /Qwen 3/});
  expect(within(models).getByText("2 recipes")).toBeVisible();
  expect(within(models).getByRole("link", {name: /Unlinked/})).toBeVisible();

  qwen.focus();
  expect(qwen).toHaveFocus();
  expect(location.pathname).toBe("/library");
  expect(libraryRecipe).not.toHaveBeenCalled();

  await user.keyboard("{Enter}");
  expect(location.pathname).toBe("/library/models/qwen%2F3");
  expect(pushState).toHaveBeenLastCalledWith(null, "", "/library/models/qwen%2F3");
  await waitFor(() => expect(heading).toHaveFocus());

  const recipes = screen.getByRole("region", {name: "Recipes for Qwen 3"});
  expect(within(recipes).getByRole("link", {name: /Qwen Chat/})).toBeVisible();
  expect(within(recipes).getByRole("link", {name: /Qwen Code/})).toBeVisible();
  expect(within(recipes).getByRole("link", {name: "Back to Models"})).toHaveAttribute("href", "/library");

  const chat = within(recipes).getByRole("link", {name: /Qwen Chat/});
  chat.focus();
  expect(location.pathname).toBe("/library/models/qwen%2F3");
  expect(libraryRecipe).not.toHaveBeenCalled();

  await user.keyboard("{Enter}");
  expect(location.pathname).toBe("/library/recipes/recipe-chat");
  expect(pushState).toHaveBeenLastCalledWith(null, "", "/library/recipes/recipe-chat");
  expect(await screen.findByRole("heading", {name: "Qwen Chat"})).toBeVisible();
  expect(screen.getByRole("link", {name: "Back to Qwen 3 recipes"})).toHaveAttribute("href", "/library/models/qwen%2F3");
  expect(libraryRecipe).toHaveBeenCalledTimes(1);

  act(() => {
    history.replaceState(null, "", "/library/models/qwen%2F3");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  expect(screen.getByRole("region", {name: "Recipes for Qwen 3"})).toBeVisible();
  await waitFor(() => expect(heading).toHaveFocus());
});

test("preserves the explicit unlinked-list parent through detail and Back navigation", async () => {
  // Break caught: a recipe without a model family loses its recipe list and
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

test("offers the advanced catalog workflow when the Library is empty", async () => {
  // Break caught: an empty local fixture produces a blank workspace with no
  // explicit route to create or import a recipe.
  history.replaceState(null, "", "/library");
  const empty = {...librarySnapshot, models: [], unlinked_recipes: []};
  render(<App api={{librarySnapshot: async () => empty} as unknown as ControlApi}/>);

  expect(await screen.findByRole("heading", {name: "No recipes in the Library"})).toBeVisible();
  expect(screen.getByRole("link", {name: "Open advanced catalog"})).toHaveAttribute("href", "/catalog");
});
