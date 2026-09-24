import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {vi} from "vitest";
import type {ControlApi, RecipeOperatorResponse} from "../api/types";
import {cacheRemovalReview} from "../test-fixtures/cache-removal";
import {CacheRemovalProgress} from "./cache-removal-progress";
import {LibraryCacheAction} from "./library-cache-action";
import {LibraryRecipeRemoveAction} from "./library-recipe-remove-action";

test("a protected recipe displays the owner blocker and cannot be removed", async () => {
  const review = cacheRemovalReview({
    references: [{classification: "saved-reference", asset_kind: "runtime-image", asset_sha256: "a".repeat(64), reason: "saved-profile", owner_kind: "profile", owner_id: "saved-profile", state: "saved", detail: "Profile keeps this image ready"}],
    blockers: [{code: "artifact.referenced", detail: "Image is used by saved-profile", retryable: false, recovery_actions: []}],
  });
  const removeRecipe = vi.fn();
  const api = {recipeRemovalReview: vi.fn().mockResolvedValue(review), removeRecipe} as unknown as ControlApi;
  render(<LibraryRecipeRemoveAction api={api} selector={review.selector} onRemoved={() => undefined}/>);
  fireEvent.click(screen.getByRole("button", {name: "Remove recipe"}));
  fireEvent.click(screen.getByRole("button", {name: "Keep the model"}));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Image is used by saved-profile"));
  expect(screen.getByLabelText("Cache removal review")).toHaveTextContent("Profile keeps this image ready");
  expect(screen.getByRole("button", {name: "Confirm remove"})).toBeDisabled();
  expect(removeRecipe).not.toHaveBeenCalled();
});

test("a refused review requires a fresh review and a new explicit confirmation", async () => {
  const first = cacheRemovalReview();
  const second = cacheRemovalReview({review_digest: "c".repeat(64)});
  const recipeRemovalReview = vi.fn().mockResolvedValueOnce(first).mockResolvedValueOnce(second);
  const removeRecipe = vi.fn().mockRejectedValueOnce(new Error("Removal scope changed; review again"))
    .mockResolvedValueOnce({selector: first.selector, state: "queued"});
  const api = {recipeRemovalReview, removeRecipe} as unknown as ControlApi;
  render(<LibraryRecipeRemoveAction api={api} selector={first.selector} onRemoved={() => undefined}/>);
  fireEvent.click(screen.getByRole("button", {name: "Remove recipe"}));
  fireEvent.click(screen.getByRole("button", {name: "Keep the model"}));
  fireEvent.click(await screen.findByRole("button", {name: "Confirm remove"}));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Removal scope changed"));
  expect(screen.queryByRole("button", {name: "Confirm remove"})).not.toBeInTheDocument();
  expect(removeRecipe).toHaveBeenCalledTimes(1);
  expect(recipeRemovalReview).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", {name: "Keep the model"}));
  await screen.findByRole("button", {name: "Confirm remove"});
  expect(removeRecipe).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", {name: "Confirm remove"}));
  await waitFor(() => expect(removeRecipe).toHaveBeenCalledTimes(2));
  expect(removeRecipe.mock.calls[1]?.[3]).toBe(second.review_digest);
});

test("a model revision changed since Library display cannot be silently removed", async () => {
  const removeModelCache = vi.fn();
  const api = {
    modelRemovalReview: vi.fn().mockResolvedValue(cacheRemovalReview({resource_kind: "model", target_identity: "d".repeat(64), with_model: null})),
    removeModelCache,
  } as unknown as ControlApi;
  render(<LibraryCacheAction api={api} selector="example/model" modelContentSha256={"e".repeat(64)} state="cached" onPrepared={() => undefined}/>);
  fireEvent.click(screen.getByRole("button", {name: "Remove from cache"}));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Model identity changed"));
  expect(screen.getByRole("button", {name: "Confirm remove"})).toBeDisabled();
  expect(removeModelCache).not.toHaveBeenCalled();
});

test("queued and partial cleanup remain visible until the same owner succeeds", async () => {
  const review = cacheRemovalReview();
  const accepted = {
    action: "remove", operation_id: "accepted-cleanup", request_key: "original-key",
    selector: review.selector, review_digest: review.review_digest,
    state: "queued", progress: {phase: "waiting for worker"},
  };
  const removeRecipe = vi.fn().mockResolvedValue(accepted);
  const recipeCacheOperation = vi.fn()
    .mockResolvedValueOnce({...accepted, state: "partial", progress: {phase: "waiting for model child"}})
    .mockResolvedValueOnce({...accepted, state: "succeeded", progress: {phase: "complete"}});
  const onRemoved = vi.fn();
  const api = {recipeRemovalReview: vi.fn().mockResolvedValue(review), removeRecipe, recipeCacheOperation} as unknown as ControlApi;
  render(<LibraryRecipeRemoveAction api={api} selector={review.selector} onRemoved={onRemoved}/>);
  fireEvent.click(screen.getByRole("button", {name: "Remove recipe"}));
  fireEvent.click(screen.getByRole("button", {name: "Keep the model"}));
  fireEvent.click(await screen.findByRole("button", {name: "Confirm remove"}));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Removal queued: accepted-cleanup"));
  expect(onRemoved).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", {name: "Remove recipe"})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", {name: "Refresh removal status"}));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Removal partial"));
  expect(screen.getByLabelText("Cache removal progress")).toHaveTextContent("waiting for model child");
  expect(onRemoved).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name: "Refresh removal status"}));
  await waitFor(() => expect(onRemoved).toHaveBeenCalledTimes(1));
  expect(recipeCacheOperation).toHaveBeenNthCalledWith(1, accepted.operation_id, expect.anything());
  expect(recipeCacheOperation).toHaveBeenNthCalledWith(2, accepted.operation_id, expect.anything());
  expect(removeRecipe).toHaveBeenCalledTimes(1);
});


test("an observation of another operation cannot complete accepted cleanup", async () => {
  const initial: RecipeOperatorResponse = {
    action: "remove", operation_id: "accepted-cleanup", request_key: "original-key",
    selector: "example/recipe", review_digest: "b".repeat(64), with_model: false,
    recipe_revision_id: "exact-revision", reclaimed_bytes: 0, schema_version: 2,
    state: "queued", progress: {phase: "queued", completed_bytes: 0, total_bytes_known: false},
  };
  const api = {recipeCacheOperation: vi.fn().mockResolvedValue({...initial, operation_id: "another-operation", state: "succeeded"})} as unknown as ControlApi;
  const onComplete = vi.fn();
  render(<CacheRemovalProgress api={api} kind="recipe" initial={initial} onComplete={onComplete} onDismiss={() => undefined}/>);
  fireEvent.click(screen.getByRole("button", {name: "Refresh removal status"}));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("different removal operation"));
  expect(onComplete).not.toHaveBeenCalled();
  expect(screen.getByRole("status")).toHaveTextContent("Removal queued: accepted-cleanup");
});
