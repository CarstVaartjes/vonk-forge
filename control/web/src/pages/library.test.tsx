import {act, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi} from "../api/types";
import {App} from "../app";
import {fullLibraryDetail, librarySnapshot, minimalLibraryDetail} from "../test-fixtures/library";

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
