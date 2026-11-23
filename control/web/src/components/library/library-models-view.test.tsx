import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import { CacheOperationKind } from "../../api/vocabulary.generated";
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

test("a model the Controller reports as preparing shows real progress and Cancel after a reload", async () => {
  const cancelModelOperation = vi.fn().mockResolvedValue({ state: "cancelled" });
  const operation = "11111111-1111-4111-8111-111111111111";
  const [first] = libraryViewSnapshot.models;
  const preparing = {
    ...first!,
    local: {
      controller: "preparing" as const,
      running_on: [],
      preparation: {
        operation_id: operation,
        state: "running" as const,
        phase: "downloading",
        completed_bytes: 256,
        total_bytes: 1024,
        blockers: [],
      },
    },
    actions: [CacheOperationKind.CANCEL, CacheOperationKind.FOLLOW],
  };
  render(
    <LibraryModelsView
      api={{ cancelModelOperation } as unknown as ControlApi}
      entries={[]}
      modelInventory={[preparing]}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      onFiltersChange={() => undefined}
      onNavigate={() => undefined}
      onQueryChange={() => undefined}
      onRefresh={async () => undefined}
      path="/library?view=models"
      query=""
    />,
  );
  // Bytes and a percentage from the Controller, never a bare "queued".
  expect(screen.getByRole("status")).toHaveTextContent("25%");
  expect(screen.queryByText("queued")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Cancel model download" }));
  fireEvent.click(await screen.findByRole("button", { name: /Confirm cancel model download/ }));
  await waitFor(() =>
    expect(cancelModelOperation).toHaveBeenCalledWith(operation, expect.any(String)),
  );
});

test("offers the same model filters as vonkctl model library", () => {
  renderModels();
  fireEvent.click(screen.getByText("Filters"));
  for (const name of [
    "Filter capabilities",
    "Filter family",
    "Filter version",
    "Filter quantization",
    "Filter publisher",
    "Filter alignment",
    "Filter exact model",
  ]) {
    expect(screen.getByRole("combobox", { name })).toBeVisible();
  }
});

test("offers the same server ordering controls as vonkctl model library", () => {
  // Break caught: the Models view keeps only the client-side filters, so its
  // order and recency can no longer match the request the Library page makes.
  const onFiltersChange = vi.fn();
  render(
    <LibraryModelsView
      api={cacheApi()}
      entries={[]}
      modelInventory={[libraryViewSnapshot.models[0]!]}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      onFiltersChange={onFiltersChange}
      onNavigate={() => undefined}
      onQueryChange={() => undefined}
      onRefresh={async () => undefined}
      path="/library?view=models"
      query=""
    />,
  );
  fireEvent.click(screen.getByText("Filters"));
  for (const name of ["Sort Library", "Filter updated"]) {
    expect(screen.getByRole("combobox", { name })).toBeVisible();
  }
  fireEvent.change(screen.getByRole("combobox", { name: "Sort Library" }), {
    target: { value: "name" },
  });
  expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({ sort: "name" }));
  fireEvent.change(screen.getByRole("combobox", { name: "Filter updated" }), {
    target: { value: "31d" },
  });
  expect(onFiltersChange).toHaveBeenCalledWith(expect.objectContaining({ updated: "31d" }));
});

test("filters models by exact selector and preserves the current route shape", () => {
  const onFiltersChange = vi.fn();
  render(
    <LibraryModelsView
      api={cacheApi()}
      entries={buildLibraryRecipeRecords(libraryViewSnapshot)}
      modelInventory={[libraryViewSnapshot.models[0]!]}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      onFiltersChange={onFiltersChange}
      onNavigate={() => undefined}
      onQueryChange={() => undefined}
      onRefresh={async () => undefined}
      path="/library?view=models"
      query=""
    />,
  );
  fireEvent.click(screen.getByText("Filters"));
  fireEvent.change(screen.getByRole("combobox", { name: "Filter exact model" }), {
    target: {
      value:
        libraryViewSnapshot.models[0]!.model.publisher +
        "/" +
        libraryViewSnapshot.models[0]!.model.slug +
        "@" +
        libraryViewSnapshot.models[0]!.model.content_sha256,
    },
  });
  expect(onFiltersChange).toHaveBeenCalledWith(
    expect.objectContaining({ model: expect.stringContaining("@") }),
  );
});

test("searches model titles without invoking update endpoints", () => {
  renderModels("Ling 3.0 Flash DSpark companion");
  expect(screen.getByRole("heading", { name: "Ling 3.0 Flash DSpark companion" })).toBeVisible();
  expect(screen.queryByRole("button", { name: /update/i })).toBeNull();
});

test("counts Models, not quantization variants, and bounds the list with Show more", () => {
  const base = libraryViewSnapshot.models[0]!;
  const quantized = {
    ...base,
    model: { ...base.model, slug: `${base.model.slug}-q8` },
    quantization: "q8",
  };
  const more = Array.from({ length: 45 }, (_, index) => ({
    ...base,
    model: { ...base.model, slug: `bulk-${index}` },
    model_document: {
      ...base.model_document,
      identity: {
        ...base.model_document.identity,
        model: { ...base.model_document.identity.model, slug: `bulk-${index}` },
      },
    },
    model_display: {
      ...base.model_display!,
      lineage: { ...base.model_display!.lineage!, model: `bulk-${index}` },
    },
  }));
  render(
    <LibraryModelsView
      api={cacheApi()}
      entries={[]}
      modelInventory={[base, quantized, ...more]}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      onFiltersChange={() => undefined}
      onNavigate={() => undefined}
      onQueryChange={() => undefined}
      onRefresh={async () => undefined}
      path="/library?view=models"
      query=""
    />,
  );
  expect(screen.getByText("46 of 46 models · 47 variants")).toBeVisible();
  const list = screen.getByLabelText("Model library");
  expect(within(list).getAllByRole("article")).toHaveLength(40);
  fireEvent.click(screen.getByRole("button", { name: "Show more (7 more rows)" }));
  expect(within(list).getAllByRole("article")).toHaveLength(47);
});

test("a model with no lineage is not a Model: no Model link, named in the header", () => {
  const base = libraryViewSnapshot.models[0]!;
  const helper = {
    ...base,
    model: { ...base.model, slug: "helper-draft" },
    model_document: {
      ...base.model_document,
      identity: {
        ...base.model_document.identity,
        model: { ...base.model_document.identity.model, slug: "helper-draft" },
      },
    },
    model_display: null,
    recipes: [],
  };
  render(
    <LibraryModelsView
      api={cacheApi()}
      entries={[]}
      modelInventory={[base, helper]}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      onFiltersChange={() => undefined}
      onNavigate={() => undefined}
      onQueryChange={() => undefined}
      onRefresh={async () => undefined}
      path="/library?view=models"
      query=""
    />,
  );
  expect(screen.getByText("1 of 1 models · 1 variants · 1 other models")).toBeVisible();
  const heading = screen.getByRole("heading", { name: /helper-draft/ });
  expect(heading.closest("a")).toBeNull();
});

describe("row actions follow the Controller's actions", () => {
  const base = libraryViewSnapshot.models[0]!;
  const row = (
    controller: "cached" | "not_cached" | "preparing",
    actions: (typeof CacheOperationKind.DOWNLOAD | typeof CacheOperationKind.REMOVE)[],
    state: "succeeded" | "running" | null = controller === "preparing" ? "running" : null,
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
              preparation: state ? { state } : null,
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
    row("not_cached", [CacheOperationKind.DOWNLOAD]);
    expect(download()).toBeVisible();
    expect(remove()).toBeNull();
  });

  test("fully present and current shows Remove only", () => {
    row("cached", [CacheOperationKind.REMOVE]);
    expect(download()).toBeNull();
    expect(remove()).toBeVisible();
  });

  test("partial or stale shows both", () => {
    row("not_cached", [CacheOperationKind.DOWNLOAD, CacheOperationKind.REMOVE]);
    expect(download()).toBeVisible();
    expect(remove()).toBeVisible();
  });

  test("a cached model with a succeeded preparation still shows Remove only", () => {
    row("cached", [CacheOperationKind.REMOVE], "succeeded");
    expect(download()).toBeNull();
    expect(remove()).toBeVisible();
  });

  test("an idle partial set can be resumed and removed", () => {
    row("preparing", [CacheOperationKind.DOWNLOAD, CacheOperationKind.REMOVE], null);
    expect(download()).toBeEnabled();
    expect(remove()).toBeVisible();
  });

  test("a download in progress shows its state, not a button that cannot apply", () => {
    row("preparing", []);
    expect(download()).toBeNull();
    expect(screen.getByRole("button", { name: /^Downloading/ })).toBeDisabled();
    expect(remove()).toBeNull();
  });
});
