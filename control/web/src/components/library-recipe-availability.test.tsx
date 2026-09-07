import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {expect, test, vi} from "vitest";
import {minimalLibraryDetail} from "../test-fixtures/library";
import {LibraryRecipeAvailability} from "./library-recipe-availability";

const progress = (phase: string, completed_bytes = 0, total_bytes: number | null = null) => ({phase, completed_bytes, total_bytes, total_bytes_known: total_bytes !== null, bytes_per_second: null, eta_seconds: null, checkpoint: null, members: []});

function operation(state: "queued" | "running" | "failed" = "running") {
  return {
    schema_version: 2,
    id: "recipe-operation-1",
    request_id: "request-1",
    kind: "recipe.image.availability.v2",
    state,
    attempt: 1,
    recipe_revision_id: minimalLibraryDetail.recipe.recipe_revision_id,
    recipe_content_sha256: "a".repeat(64),
    progress: progress("prepare", 10, 100),
    children: [
      {kind: "model-cache", id: "model-child-1", state: state === "failed" ? "failed" : "running", artifact_set_sha256: "b".repeat(64), plan_digest: "c".repeat(64), progress: progress("download", 5, 50), failure: state === "failed" ? {code: "access_required", detail: "Hugging Face access is required.", recovery_actions: ["open_model_access", "configure_hf_token", "check_access_and_resume"], retryable: false, retry_time: null, retry_after_seconds: null, log_excerpt: null, required_bytes: null, free_bytes: null, shortfall_bytes: null} : null},
      {kind: "runtime-image", id: "image-child-1", state: "running", progress: progress("build"), failure: null},
    ],
    failure: null,
    result: null,
    actions: [],
    created_at: "2026-09-06T12:00:00Z",
    updated_at: "2026-09-06T12:00:00Z",
  };
}

test("hydrates the exact recipe operation after reload and keeps both members visible", async () => {
  const active = operation();
  const list = vi.fn(async () => ({schema_version: 2, operations: [active], total: 1, next_cursor: null}));
  render(<LibraryRecipeAvailability api={{recipeAvailabilityList: list, recipeAvailabilityStart: vi.fn(async () => active), recipeAvailabilityOperation: vi.fn(async () => active)} as never} detail={minimalLibraryDetail}/>);
  expect(await screen.findByText("Model files")).toBeVisible();
  expect(screen.getByText("Runtime image")).toBeVisible();
  expect(screen.getByText(/Exact revision/)).toHaveTextContent(minimalLibraryDetail.recipe.recipe_revision_id);
  expect(list).toHaveBeenCalledWith(minimalLibraryDetail.recipe.recipe_revision_id, undefined, undefined, expect.any(AbortSignal));
});

test("starts one aggregate operation and uses the explicit Model access resume route", async () => {
  const failed = operation("failed");
  const started = {...failed, state: "running" as const};
  const list = vi.fn()
    .mockResolvedValueOnce({schema_version: 2, operations: [], total: 0, next_cursor: null})
    .mockResolvedValueOnce({schema_version: 2, operations: [started], total: 1, next_cursor: null});
  const start = vi.fn(async () => started);
  const checkAccess = vi.fn(async () => ({schema_version: 2, id: "model-child-1", request_key: "request-2", kind: "download", state: "queued", attempt: 2, artifact_set_sha256: "b".repeat(64), plan_digest: "c".repeat(64), progress: {schema_version: 2, phase: "queued", completed_artifacts: 0, total_artifacts: 1, downloaded_bytes: 0, expected_bytes: 50, total_bytes_known: true, current_artifact_key: null, bytes_per_second: null, eta_seconds: null, members: []}, failure: null, result: null, created_at: "2026-09-06T12:00:00Z", updated_at: "2026-09-06T12:01:00Z", completed_at: null}));
  const first = render(<LibraryRecipeAvailability api={{recipeAvailabilityList: list, recipeAvailabilityStart: start, recipeAvailabilityOperation: vi.fn(async () => started), checkModelCacheAccessAndResume: checkAccess} as never} detail={minimalLibraryDetail}/>);
  fireEvent.click(await screen.findByRole("button", {name: "Make available"}));
  await waitFor(() => expect(start).toHaveBeenCalledWith({request_key: expect.stringMatching(/^[0-9a-f-]{36}$/), recipe_revision_id: minimalLibraryDetail.recipe.recipe_revision_id, force: false}));
  expect(await screen.findByText("Model files")).toBeVisible();
  // The failed child is supplied by the operation returned by the list rehydrate.
  first.unmount();
  const failedList = vi.fn(async () => ({schema_version: 2, operations: [failed], total: 1, next_cursor: null}));
  render(<LibraryRecipeAvailability api={{recipeAvailabilityList: failedList, recipeAvailabilityStart: vi.fn(async () => failed), recipeAvailabilityOperation: vi.fn(async () => failed), checkModelCacheAccessAndResume: checkAccess} as never} detail={minimalLibraryDetail}/>);
  fireEvent.click(await screen.findByRole("button", {name: "Check access and resume"}));
  await waitFor(() => expect(checkAccess).toHaveBeenCalledWith("model-child-1", expect.objectContaining({schema_version: 2, artifact_set_sha256: "b".repeat(64), plan_digest: "c".repeat(64), request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)})));
});

test("offers a live status refresh after a durable status load error", async () => {
  const list = vi.fn()
    .mockRejectedValueOnce(new Error("Controller temporarily unavailable"))
    .mockResolvedValueOnce({schema_version: 2, operations: [], total: 0, next_cursor: null});
  render(<LibraryRecipeAvailability api={{recipeAvailabilityList: list, recipeAvailabilityStart: vi.fn(async () => operation()), recipeAvailabilityOperation: vi.fn(async () => operation())} as never} detail={minimalLibraryDetail}/>);
  await screen.findByRole("button", {name: "Refresh availability status"});
  fireEvent.click(screen.getByRole("button", {name: "Refresh availability status"}));
  await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
  expect(await screen.findByRole("button", {name: "Make available"})).toBeVisible();
});
