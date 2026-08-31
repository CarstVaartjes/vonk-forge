import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi, LibraryUninstallApplyInput} from "../api/types";
import {ApiError} from "../api/client";
import {LibraryModelDeletionDialog} from "./library-model-deletion-dialog";
import type {LibraryModelDeletionPlan} from "./library-model-deletion-dialog";

const GIB = 1024 ** 3;
const digest = "e".repeat(64);

function plan(overrides: Partial<LibraryModelDeletionPlan> = {}): LibraryModelDeletionPlan {
  return {
    active_run_count: 0,
    active_runs: [],
    allowed: true,
    blockers: [],
    bytes_removed: 120 * GIB,
    installations: [{installation_id: "installation-chat", installed_bytes: 120 * GIB, node_ids: ["node-alpha", "node-beta"], recipe_content_sha256: "a".repeat(64), recipe_id: "recipe-chat", recipe_revision_id: "revision-chat"}],
    model_title: "Qwen 3 BF16",
    model_version_sha256: digest,
    nodes: [
      {installation_ids: ["installation-chat"], installed_bytes: 60 * GIB, node_id: "node-alpha", recipe_ids: ["recipe-chat"]},
      {installation_ids: ["installation-chat"], installed_bytes: 60 * GIB, node_id: "node-beta", recipe_ids: ["recipe-chat"]},
    ],
    plan_digest: "plan-one",
    shared_cache_policy: "Only exact model files referenced solely by these installations are removed. Unrelated immutable caches stay on each Spark.",
    warnings: [],
    ...overrides,
  };
}

function operation(state = "succeeded") {
  return {id: "operation-delete", kind: "model-delete", owner_id: digest, state, plan_digest: "plan-one", nodes: ["node-alpha", "node-beta"], result: null};
}

function renderDialog(api: ControlApi, onRefresh = vi.fn(async () => undefined)) {
  return {onRefresh, ...render(<LibraryModelDeletionDialog
    api={api}
    modelTitle="Qwen 3 BF16"
    modelVersionSha256={digest}
    nodeNames={{"node-alpha": "Aurora", "node-beta": "Borealis"}}
    onClose={vi.fn()}
    onRefresh={onRefresh}
  />)};
}

afterEach(() => vi.restoreAllMocks());

test("blocks model deletion while showing exact active runs and multi-Spark impact", async () => {
  const preview = plan({
    active_run_count: 1,
    active_runs: [{alias: "chat", route_state: "published", run_id: "run-chat", state: "running"}],
    allowed: false,
    blockers: [{code: "model_delete.active_runs", detail: "Stop the complete run before deleting the model."}],
  });
  const api = {previewLibraryModelDeletion: vi.fn(async () => preview)} as unknown as ControlApi;
  renderDialog(api);

  const dialog = await screen.findByRole("dialog", {name: "Delete Qwen 3 BF16 from Sparks"});
  expect(within(dialog).getByText("1 recipe installation across 2 Sparks will be removed.")).toBeVisible();
  expect(within(dialog).getByRole("heading", {name: "1 active run blocks deletion"})).toBeVisible();
  expect(within(dialog).getByText("model_delete.active_runs")).toBeVisible();
  expect(within(dialog).getByText(/Only exact model files/)).toBeVisible();
  expect(within(dialog).getByRole("button", {name: "Delete model and dependent recipes"})).toBeDisabled();
});

test("keeps one request key across an ambiguous deletion retry and reports completion", async () => {
  const deleteLibraryModel = vi.fn()
    .mockRejectedValueOnce(new Error("response lost after dispatch"))
    .mockResolvedValueOnce(operation());
  const api = {previewLibraryModelDeletion: vi.fn(async () => plan()), deleteLibraryModel} as unknown as ControlApi;
  const user = userEvent.setup();
  const {onRefresh} = renderDialog(api);

  const dialog = await screen.findByRole("dialog", {name: "Delete Qwen 3 BF16 from Sparks"});
  await user.click(within(dialog).getByRole("checkbox"));
  await user.click(within(dialog).getByRole("button", {name: "Delete model and dependent recipes"}));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("response lost after dispatch");
  await user.click(within(dialog).getByRole("button", {name: "Retry deletion request"}));

  await waitFor(() => expect(deleteLibraryModel).toHaveBeenCalledTimes(2));
  expect(deleteLibraryModel.mock.calls[0][1].request_key).toBe(deleteLibraryModel.mock.calls[1][1].request_key);
  expect(deleteLibraryModel).toHaveBeenLastCalledWith(digest, {plan_digest: "plan-one", request_key: expect.any(String)}, expect.any(AbortSignal));
  expect(await within(dialog).findByRole("region", {name: "Delete model operation progress"})).toHaveTextContent("Operation complete");
  expect(onRefresh).toHaveBeenCalledTimes(1);
});

test("requires a fresh preview and request key after a stale-plan rejection", async () => {
  const keys = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"];
  vi.spyOn(crypto, "randomUUID").mockImplementation(() => keys.shift() as `${string}-${string}-${string}-${string}-${string}`);
  const previewLibraryModelDeletion = vi.fn(async () => plan());
  const deleteLibraryModel = vi.fn(async (_modelDigest: string, _input: LibraryUninstallApplyInput, _signal?: AbortSignal) => { throw new ApiError(409, "Control API returned 409: plan digest is stale"); });
  const api = {previewLibraryModelDeletion, deleteLibraryModel} as unknown as ControlApi;
  const user = userEvent.setup();
  renderDialog(api);

  const dialog = await screen.findByRole("dialog", {name: "Delete Qwen 3 BF16 from Sparks"});
  await user.click(within(dialog).getByRole("checkbox"));
  await user.click(within(dialog).getByRole("button", {name: "Delete model and dependent recipes"}));
  await user.click(await within(dialog).findByRole("button", {name: "Review fresh preview"}));

  await waitFor(() => expect(previewLibraryModelDeletion).toHaveBeenCalledTimes(2));
  expect(within(dialog).getByRole("checkbox")).not.toBeChecked();
  expect(deleteLibraryModel.mock.calls[0][1].request_key).toBe("00000000-0000-4000-8000-000000000001");
});
