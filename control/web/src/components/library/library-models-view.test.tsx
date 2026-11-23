import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import { LifecycleState } from "../../api/vocabulary.generated";
import type { ControlApi } from "../../api/types";
import { cacheRemovalReview } from "../../test-fixtures/cache-removal";
import { libraryViewSnapshot } from "../../test-fixtures/library";
import { buildLibraryRecipeRecords, EMPTY_LIBRARY_WORKCELL_FILTERS } from "./library-workcell";
import { LibraryModelsView } from "./library-models-view";

const prepared = {
  action: "download" as const,
  phase: "complete",
  request_key: "request",
  selector: "vonk-forge/ling-3-flash",
  state: "succeeded" as const,
  transferred_bytes: 10,
  progress: { phase: "complete" },
};

function cacheApi() {
  return { prepareModelCache: vi.fn().mockResolvedValue(prepared) } as unknown as ControlApi;
}

const renderModels = (query = "", api: ControlApi = cacheApi()) =>
  render(
    <LibraryModelsView
      api={api}
      entries={buildLibraryRecipeRecords(libraryViewSnapshot)}
      modelInventory={libraryViewSnapshot.models}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      onFiltersChange={() => undefined}
      onNavigate={() => undefined}
      onQueryChange={() => undefined}
      onRefresh={async () => undefined}
      path="/library?view=models"
      query={query}
    />,
  );

test("groups model revisions and keeps recipe links and downloads bound to the selected revision", async () => {
  const current = libraryViewSnapshot.models.find((model) => model.recipes.length > 0)!;
  const older = {
    ...current,
    model: {
      ...current.model,
      slug: `${current.model.slug}-older`,
      content_sha256: "a".repeat(64),
    },
    recipes: [],
    version: "older",
    model_document: {
      ...current.model_document,
      identity: { ...current.model_document.identity, version: "older" },
    },
  };
  const api = cacheApi();
  render(
    <LibraryModelsView
      api={api}
      entries={[]}
      modelInventory={[older, current]}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      onFiltersChange={() => undefined}
      onNavigate={() => undefined}
      onQueryChange={() => undefined}
      onRefresh={async () => undefined}
      path="/library?view=models"
      query=""
    />,
  );

  const list = screen.getByLabelText("Model library");
  expect(within(list).getAllByRole("article")).toHaveLength(1);
  expect(screen.getByText("1 of 1 models · 2 variants")).toBeVisible();
  expect(within(list).getByRole("link", { name: current.recipes[0]!.title })).toBeVisible();
  const revisions = within(list).getByRole("combobox", { name: /Revision for / });
  expect(revisions).toHaveValue(
    `${current.model.publisher}/${current.model.slug}@${current.model.content_sha256}`,
  );
  fireEvent.change(revisions, {
    target: { value: `${older.model.publisher}/${older.model.slug}@${older.model.content_sha256}` },
  });
  expect(within(list).queryByRole("link", { name: current.recipes[0]!.title })).toBeNull();
  fireEvent.click(within(list).getByRole("button", { name: "Download" }));
  await waitFor(() =>
    expect(api.prepareModelCache).toHaveBeenCalledWith(
      `${older.model.publisher}/${older.model.slug}`,
      expect.any(String),
      expect.anything(),
    ),
  );
});

test("keeps different creators and quantizations separate even when their titles match", () => {
  const base = libraryViewSnapshot.models[0]!;
  const anotherCreator = { ...base, model: { ...base.model, publisher: "another-creator" } };
  const anotherQuantization = {
    ...base,
    model: { ...base.model, slug: `${base.model.slug}-q8` },
    quantization: "q8",
  };
  render(
    <LibraryModelsView
      api={cacheApi()}
      entries={[]}
      modelInventory={[base, anotherCreator, anotherQuantization]}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      onFiltersChange={() => undefined}
      onNavigate={() => undefined}
      onQueryChange={() => undefined}
      onRefresh={async () => undefined}
      path="/library?view=models"
      query=""
    />,
  );
  expect(within(screen.getByLabelText("Model library")).getAllByRole("article")).toHaveLength(3);
});

test("shows the verified model library with exact size and recipe counts", () => {
  renderModels();
  fireEvent.click(screen.getByRole("button", { name: /^Show more/ }));
  expect(screen.getByRole("heading", { name: "Models" })).toBeVisible();
  expect(screen.getByLabelText("Model library")).toBeVisible();
  expect(screen.getAllByText("None", { exact: true }).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/GiB/).length).toBeGreaterThan(0);
});

test("lists the recipes that use a model so a selected model leads to its recipes", () => {
  renderModels();
  expect(screen.getAllByRole("list", { name: /^Recipes using / }).length).toBeGreaterThan(0);
});

test("prepares the Controller cache for an uncached model", async () => {
  const api = cacheApi();
  renderModels("", api);
  const button = screen.getAllByRole("button", { name: "Download" })[0]!;
  fireEvent.click(button);
  await waitFor(() => expect(api.prepareModelCache).toHaveBeenCalledTimes(1));
  const [selector, requestKey] = (
    api.prepareModelCache as unknown as { mock: { calls: [string, string][] } }
  ).mock.calls[0]!;
  expect(selector).toMatch(/^[^/]+\/[^/]+$/);
  expect(requestKey).toMatch(/^[0-9a-f-]{36}$/);
});

describe("row actions follow the Controller's actions", () => {
  const base = libraryViewSnapshot.models[0]!;
  const row = (
    controller: "cached" | "not_cached" | "preparing",
    actions: ("download" | "remove")[],
  ) =>
    render(
      <LibraryModelsView
        api={cacheApi()}
        entries={[]}
        modelInventory={[
          {
            ...base,
            local: {
              ...base.local,
              controller,
              preparation: controller === "preparing" ? { state: LifecycleState.RUNNING } : null,
            },
            actions,
          },
        ]}
        filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
        onFiltersChange={() => undefined}
        onNavigate={() => undefined}
        onQueryChange={() => undefined}
        onRefresh={async () => undefined}
        path="/library?view=models"
        query=""
      />,
    );
  const download = () => screen.queryByRole("button", { name: "Download" });
  const remove = () => screen.queryByRole("button", { name: "Remove model from fleet" });

  test("absent shows Download only", () => {
    row("not_cached", ["download"]);
    expect(download()).toBeVisible();
    expect(remove()).toBeNull();
  });

  test("fully present and current shows Remove only", () => {
    row("cached", ["remove"]);
    expect(download()).toBeNull();
    expect(remove()).toBeVisible();
  });

  test("partial or stale shows both", () => {
    row("not_cached", ["download", "remove"]);
    expect(download()).toBeVisible();
    expect(remove()).toBeVisible();
  });

  test("a download in progress shows its state, not a button that cannot apply", () => {
    row("preparing", []);
    expect(download()).toBeNull();
    expect(screen.getByRole("button", { name: /^Downloading/ })).toBeDisabled();
    expect(remove()).toBeNull();
  });
});
