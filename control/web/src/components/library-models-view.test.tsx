import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {librarySnapshot} from "../test-fixtures/library";
import {buildLibraryRecipeRecords, EMPTY_LIBRARY_WORKCELL_FILTERS} from "./library-workcell";
import {LibraryModelsView} from "./library-models-view";

test("shows compact exact model rows and no-recipe models", async () => {
  render(<LibraryModelsView api={{} as never} entries={buildLibraryRecipeRecords(librarySnapshot)} modelInventory={librarySnapshot.models} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} path="/library?view=models" onQueryChange={() => undefined} query=""/>);
  expect(screen.getByRole("heading", {name: "Models"})).toBeVisible();
  expect(screen.getAllByText("No Recipe", {exact: false}).length).toBe(13);
});

test("surfaces ambiguous exact model update candidates without switching the pinned row", async () => {
  const model = librarySnapshot.models[0]!;
  render(<LibraryModelsView api={{modelCacheUpdates: async () => ({schema_version: 2, total: 1, updates: [{schema_version: 2, artifact_set_sha256: "a".repeat(64), latest_model_content_sha256: "b".repeat(64), latest_recipe_revision_sha256: null, model_update_available: true, model_update_ambiguous: true, model_update_candidates: [{publisher: "same-publisher", slug: "same-lineage", variant: "bf16", format: "safetensors", model_content_sha256: "b".repeat(64)}, {publisher: "same-publisher", slug: "same-lineage", variant: "int4", format: "safetensors", model_content_sha256: "c".repeat(64)}], model_update_from: null, model_update_to: null, model_content_sha256: model.model.content_sha256, recipe_revision_sha256: null, recipe_update_available: false, updated_at: null}], next_cursor: null})} as never} entries={buildLibraryRecipeRecords(librarySnapshot)} modelInventory={[model]} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} path="/library?view=models" onQueryChange={() => undefined} query=""/>);
  await waitFor(() => expect(screen.getByText("Model update needs a choice")).toBeVisible());
  expect(screen.getByText(/this row stays pinned to/)).toHaveTextContent(model.model.content_sha256);
  expect(screen.getByText(/same-publisher\/same-lineage\/bf16\/safetensors/)).toBeVisible();
});

test("checks upstream only on request and explains newer revisions without changing downloads", async () => {
  const model = librarySnapshot.models[0]!;
  const checks: boolean[] = [];
  const modelCacheUpdates = async (_signal: AbortSignal, checkUpstream: boolean) => {
    checks.push(checkUpstream);
    return {updates: [{model_content_sha256: model.model.content_sha256, upstream_revisions: checkUpstream ? [{repository: "creator/model", status: "update-available", pinned_revision: "a".repeat(40), latest_revision: "b".repeat(40)}] : []}]};
  };
  render(<LibraryModelsView api={{modelCacheUpdates} as never} entries={[]} modelInventory={[model]} filters={EMPTY_LIBRARY_WORKCELL_FILTERS} onFiltersChange={() => undefined} onNavigate={() => undefined} path="/library?view=models" onQueryChange={() => undefined} query=""/>);
  await waitFor(() => expect(screen.getByRole("button", {name: "Check for updates"})).toBeEnabled());
  expect(checks).toEqual([false]);
  fireEvent.click(screen.getByRole("button", {name: "Check for updates"}));
  await screen.findByText(/New upstream version/);
  expect(checks).toEqual([false, true]);
  expect(screen.getByText(/Update the model in the catalog/)).toBeVisible();
});
