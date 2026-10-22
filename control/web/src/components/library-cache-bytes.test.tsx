import { render, screen } from "@testing-library/react";
import type { ControlApi, LibraryViewModel } from "../api/types";
import { libraryViewSnapshot } from "../test-fixtures/library";
import { LibraryModelsView } from "./library-models-view";
import { EMPTY_LIBRARY_WORKCELL_FILTERS } from "./library-workcell";

test("shows physical unique/shared bytes and the owner retaining a previous revision", () => {
  // Catches preserving the JSON accounting while hiding why old bytes remain in the cache.
  const model: LibraryViewModel = {
    ...libraryViewSnapshot.models[0]!,
    model_bytes: 12,
    cache_usage: {
      disk_bytes: 12,
      unique_bytes: 3,
      shared_bytes: 9,
      retained_revisions: [
        { content_sha256: "a".repeat(64), unique_bytes: 2, owners: ["installation owner-1"] },
      ],
    },
  };
  render(
    <LibraryModelsView
      api={{} as ControlApi}
      entries={[]}
      modelInventory={[model]}
      filters={EMPTY_LIBRARY_WORKCELL_FILTERS}
      onFiltersChange={() => undefined}
      onNavigate={() => undefined}
      onQueryChange={() => undefined}
      onRefresh={async () => undefined}
      path="/library?view=models"
      query=""
    />,
  );
  expect(screen.getByText("12 B")).toBeVisible();
  expect(screen.getByText("3 B")).toBeVisible();
  expect(screen.getByText("9 B")).toBeVisible();
  expect(screen.getByText(/Previous revision retained because installation owner-1/)).toBeVisible();
  expect(screen.getByText(/2 B unique; rechecked every 30s/)).toBeVisible();
});
