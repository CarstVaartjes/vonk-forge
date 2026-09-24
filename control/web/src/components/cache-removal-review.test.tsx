import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {afterEach, vi} from "vitest";
import {ApiClient, ApiError} from "../api/client";
import type {ControlApi, RecipeOperatorResponse} from "../api/types";
import {cacheRemovalReview} from "../test-fixtures/cache-removal";
import {CacheRemovalProgress} from "./cache-removal-progress";
import {LibraryCacheAction} from "./library-cache-action";
import {LibraryRecipeRemoveAction} from "./library-recipe-remove-action";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

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
  const removeRecipe = vi.fn()
    .mockRejectedValueOnce(new ApiError(409, "Removal scope changed; review again"))
    .mockImplementationOnce(async (_selector: string, requestKey: string, withModel: boolean, reviewDigest: string) => ({
      action: "remove" as const,
      operation_id: "second-operation",
      request_key: requestKey,
      selector: second.selector,
      review_digest: reviewDigest,
      recipe_revision_id: second.target_identity,
      with_model: withModel,
      reclaimed_bytes: 0,
      schema_version: 2 as const,
      state: "queued" as const,
      progress: {phase: "queued"},
    }));
  const recipeCacheRequest = vi.fn();
  const api = {recipeRemovalReview, removeRecipe, recipeCacheRequest} as unknown as ControlApi;
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
    modelRemovalReview: vi.fn().mockResolvedValue(cacheRemovalReview({resource_kind: "model", selector: "example/model", target_identity: "d".repeat(64), with_model: null})),
    removeModelCache,
  } as unknown as ControlApi;
  render(<LibraryCacheAction api={api} selector="example/model" modelContentSha256={"e".repeat(64)} state="cached" onPrepared={() => undefined}/>);
  fireEvent.click(screen.getByRole("button", {name: "Remove from cache"}));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("selected content identity"));
  expect(screen.getByRole("button", {name: "Confirm remove"})).toBeDisabled();
  expect(removeModelCache).not.toHaveBeenCalled();
});

test("a review for another recipe selector cannot be confirmed", async () => {
  const review = cacheRemovalReview({selector: "publisher/other-recipe", with_model: false});
  const removeRecipe = vi.fn();
  const api = {recipeRemovalReview: vi.fn().mockResolvedValue(review), removeRecipe} as unknown as ControlApi;
  render(<LibraryRecipeRemoveAction api={api} selector="publisher/selected-recipe" onRemoved={() => undefined}/>);
  fireEvent.click(screen.getByRole("button", {name: "Remove recipe"}));
  fireEvent.click(screen.getByRole("button", {name: "Keep the model"}));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("another selector or resource"));
  expect(removeRecipe).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", {name: "Confirm remove"})).not.toBeInTheDocument();
});

test("queued and partial cleanup remain visible until the same owner succeeds", async () => {
  const review = cacheRemovalReview();
  const requestKey = "00000000-0000-4000-8000-000000000931" as `${string}-${string}-${string}-${string}-${string}`;
  const accepted: RecipeOperatorResponse = {
    action: "remove", operation_id: "accepted-cleanup", request_key: requestKey,
    selector: review.selector, review_digest: review.review_digest,
    recipe_revision_id: review.target_identity, with_model: false,
    reclaimed_bytes: 0, schema_version: 2,
    state: "queued", progress: {phase: "waiting for worker", completed_bytes: 0, total_bytes_known: false},
  };
  const removeRecipe = vi.fn().mockResolvedValue(accepted);
  vi.spyOn(crypto, "randomUUID").mockReturnValue(requestKey);
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
  const review = cacheRemovalReview({selector: initial.selector, target_identity: initial.recipe_revision_id, with_model: initial.with_model});
  const intent = {kind: "recipe" as const, selector: initial.selector, requestKey: initial.request_key, review};
  const api = {recipeCacheOperation: vi.fn().mockResolvedValue({...initial, operation_id: "another-operation", state: "succeeded"})} as unknown as ControlApi;
  const onComplete = vi.fn();
  render(<CacheRemovalProgress api={api} intent={intent} initial={initial} onComplete={onComplete} onDismiss={() => undefined} onRejected={() => undefined}/>);
  fireEvent.click(screen.getByRole("button", {name: "Refresh removal status"}));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("different removal operation"));
  expect(onComplete).not.toHaveBeenCalled();
  expect(screen.getByRole("status")).toHaveTextContent("Removal queued: accepted-cleanup");
});

test("a wrong-target success receipt is reconciled by key and never reported as complete", async () => {
  const review = cacheRemovalReview({selector: "publisher/recipe", with_model: false});
  const wrongReceipt: RecipeOperatorResponse = {
    action: "remove", operation_id: "wrong-target-operation", request_key: "00000000-0000-4000-8000-000000000932",
    selector: review.selector, review_digest: review.review_digest, with_model: false,
    recipe_revision_id: "different-revision", reclaimed_bytes: 10, schema_version: 2,
    state: "succeeded", progress: {phase: "completed", completed_bytes: 10, total_bytes_known: true},
  };
  vi.spyOn(crypto, "randomUUID").mockReturnValue(wrongReceipt.request_key as ReturnType<typeof crypto.randomUUID>);
  const removeRecipe = vi.fn().mockResolvedValue(wrongReceipt);
  const recipeCacheRequest = vi.fn().mockResolvedValue(wrongReceipt);
  const recipeRemovalReview = vi.fn().mockResolvedValue(review);
  const onRemoved = vi.fn();
  const api = {recipeRemovalReview, removeRecipe, recipeCacheRequest} as unknown as ControlApi;
  render(<LibraryRecipeRemoveAction api={api} selector={review.selector} onRemoved={onRemoved}/>);
  fireEvent.click(screen.getByRole("button", {name: "Remove recipe"}));
  fireEvent.click(screen.getByRole("button", {name: "Keep the model"}));
  fireEvent.click(await screen.findByRole("button", {name: "Confirm remove"}));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Removal receipt not yet confirmed"));
  expect(onRemoved).not.toHaveBeenCalled();
  expect(removeRecipe).toHaveBeenCalledTimes(1);
  expect(recipeCacheRequest).toHaveBeenCalledWith(removeRecipe.mock.calls[0]?.[1], expect.anything());
});

test.each(["disconnect", "timeout"])("an accepted POST with %s recovers through the real request lookup", async (failure) => {
  const review = cacheRemovalReview({selector: "publisher/recipe", with_model: false});
  const accepted: RecipeOperatorResponse = {
    action: "remove", operation_id: "recovered-operation", request_key: "00000000-0000-4000-8000-000000000933",
    selector: review.selector, review_digest: review.review_digest, with_model: false,
    recipe_revision_id: review.target_identity, reclaimed_bytes: 0, schema_version: 2,
    state: "queued", progress: {phase: "queued", completed_bytes: 0, total_bytes_known: false},
  };
  vi.spyOn(crypto, "randomUUID").mockReturnValue(accepted.request_key as ReturnType<typeof crypto.randomUUID>);
  const calls: {method: string; path: string; body?: unknown}[] = [];
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request
      ? input
      : new Request(new URL(String(input), location.origin), init);
    const url = new URL(request.url);
    const body = request.method === "POST" ? await request.clone().json() as unknown : undefined;
    calls.push({method: request.method, path: url.pathname + url.search, body});
    if (request.method === "GET" && url.pathname.endsWith("/remove-review")) {
      return new Response(JSON.stringify(review), {status: 200, headers: {"Content-Type": "application/json"}});
    }
    if (request.method === "POST" && url.pathname.endsWith("/remove")) {
      if (failure === "timeout") {
        return new Response(JSON.stringify({detail: "response timed out"}), {status: 408, headers: {"Content-Type": "application/json"}});
      }
      throw new TypeError("connection lost after Controller acceptance");
    }
    if (request.method === "GET" && url.pathname.endsWith("/requests/" + accepted.request_key)) {
      return new Response(JSON.stringify(accepted), {status: 200, headers: {"Content-Type": "application/json"}});
    }
    throw new Error(`unexpected request ${request.method} ${url.pathname}`);
  });
  const onRemoved = vi.fn();
  render(<LibraryRecipeRemoveAction api={new ApiClient()} selector={review.selector} onRemoved={onRemoved}/>);
  fireEvent.click(screen.getByRole("button", {name: "Remove recipe"}));
  fireEvent.click(screen.getByRole("button", {name: "Keep the model"}));
  fireEvent.click(await screen.findByRole("button", {name: "Confirm remove"}));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Removal queued: recovered-operation"));
  expect(onRemoved).not.toHaveBeenCalled();
  expect(calls.filter(call => call.method === "POST" && call.path.endsWith("/remove"))).toHaveLength(1);
  const requestLookup = calls.find(call => call.path.endsWith("/requests/" + accepted.request_key));
  expect(requestLookup?.method).toBe("GET");
  expect(calls[1]?.body).toMatchObject({
    request_key: accepted.request_key,
    review_digest: review.review_digest,
    with_model: false,
  });
});
