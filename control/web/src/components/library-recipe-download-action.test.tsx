import {act, render, screen} from "@testing-library/react";
import {vi} from "vitest";
import type {ControlApi} from "../api/types";
import {LibraryRecipeDownloadAction} from "./library-recipe-download-action";
import {ToastProvider} from "./toast";

const running = {kind: "recipe.image.availability.v2", id: "op-1", state: "running", progress: {phase: "pulling"}, children: [], blockers: [{code: "disk", detail: "Waiting for space"}]};

function setup(recipeCacheOperation: ReturnType<typeof vi.fn>) {
  const api = {downloadRecipe: vi.fn(async () => running), recipeCacheOperation, cancelRecipeOperation: vi.fn()} as unknown as ControlApi;
  render(<ToastProvider><LibraryRecipeDownloadAction api={api} selector="vonk-forge/x" missingModels={[]} onDownloaded={vi.fn()}/></ToastProvider>);
  return api;
}
const click = async () => { screen.getByRole("button", {name: "Download recipe"}).click(); await act(async () => { await Promise.resolve(); }); };
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };

afterEach(() => vi.useRealTimers());

test("a download that outlives the observation deadline stays running, not failed", async () => {
  vi.useFakeTimers();
  const recipeCacheOperation = vi.fn(async () => running);
  setup(recipeCacheOperation);
  await click();
  await advance(200_000);
  const observed = recipeCacheOperation.mock.calls.length;
  await advance(10_000);
  expect(recipeCacheOperation).toHaveBeenCalledTimes(observed);
  expect(screen.queryByText(/did not complete/)).toBeNull();
  expect(screen.getByText(/Observation ended/)).toBeVisible();
  expect(screen.getByRole("button", {name: "Downloading…"})).toBeDisabled();
  expect(screen.getByText("Waiting for space", {exact: false})).toBeVisible();
  expect(screen.getByRole("button", {name: /Cancel/})).toBeVisible();
  recipeCacheOperation.mockResolvedValue({...running, state: "succeeded"});
  await act(async () => screen.getByRole("button", {name: "Check status"}).click());
  await advance(1_000);
  expect(screen.getByText("Recipe downloaded.")).toBeVisible();
  expect(screen.getByRole("button", {name: "Download recipe"})).toBeEnabled();
});

test("a download keeps being observed through a temporary disconnection and then completes", async () => {
  vi.useFakeTimers();
  const recipeCacheOperation = vi.fn()
    .mockRejectedValueOnce(new TypeError("offline")).mockRejectedValueOnce(new TypeError("offline")).mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValue({...running, state: "succeeded"});
  setup(recipeCacheOperation);
  await click();
  await advance(1_000);
  expect(screen.getByText(/Reconnecting to the Controller/)).toBeVisible();
  expect(screen.getByRole("button", {name: "Downloading…"})).toBeDisabled();
  await advance(30_000);
  expect(recipeCacheOperation).toHaveBeenCalledTimes(4);
  expect(screen.queryByText(/Reconnecting/)).toBeNull();
  expect(screen.getByRole("button", {name: "Download recipe"})).toBeEnabled();
});

test("damaged terminal evidence is observed until the same operation is readable", async () => {
  vi.useFakeTimers();
  const recipeCacheOperation = vi.fn()
    .mockResolvedValueOnce({...running, state: "succeeded", progress: null, residue: {kind: "availability", subject: "op-1", reason: "persisted-state-damaged", note: "stored metadata is unreadable"}})
    .mockResolvedValue({...running, state: "succeeded", residue: null});
  setup(recipeCacheOperation);
  await click();
  await advance(1_000);
  expect(screen.getByRole("button", {name: "Observing…"})).toBeDisabled();
  expect(screen.getByText(/Stored operation evidence is unavailable/)).toBeVisible();
  expect(screen.queryByText("Recipe downloaded.")).toBeNull();
  expect(screen.queryByRole("button", {name: /Cancel/})).toBeNull();
  await advance(1_000);
  expect(recipeCacheOperation).toHaveBeenCalledTimes(2);
  expect(screen.getByText("Recipe downloaded.")).toBeVisible();
  expect(screen.getByRole("button", {name: "Download recipe"})).toBeEnabled();
});
