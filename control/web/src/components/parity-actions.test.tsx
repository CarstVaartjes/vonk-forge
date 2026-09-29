import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {CancelOperation} from "./cancel-operation";
import {CatalogSyncStatusLine} from "./catalog-sync-status";
import {InstallationReconcile} from "./installation-reconcile";

test("cancel needs a confirmation and reuses one request key", async () => {
  const cancel = vi.fn().mockRejectedValueOnce(new Error("boom")).mockResolvedValue({});
  render(<CancelOperation what="download" consequence="c" cancel={cancel}/>);
  await userEvent.click(screen.getByRole("button", {name: "Cancel download"}));
  expect(cancel).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", {name: /Confirm cancel/}));
  await screen.findByRole("alert");
  await userEvent.click(screen.getByRole("button", {name: /Confirm cancel/}));
  await waitFor(() => expect(cancel).toHaveBeenCalledTimes(2));
  expect(cancel.mock.calls[0]![0]).toBe(cancel.mock.calls[1]![0]);
});

test("sync status treats a never-run sync as a state and lists problems", async () => {
  const never = render(<CatalogSyncStatusLine api={{catalogSyncStatus: async () => null}}/>);
  expect(await screen.findByRole("status")).toBeVisible();
  never.unmount();
  const status = {state: "partial", created_at: "2026-01-01T00:00:00Z", completed_at: "2026-01-01T00:01:00Z", commit: "a".repeat(40), expected_commit: null,
    imported_count: 1, updated_count: 0, unchanged_count: 2, skipped_count: 0, withdrawn_count: 0, total_count: 3, problems: [{code: "bad.recipe", detail: "broken"}], stale_recipes: [], last_error: null};
  render(<CatalogSyncStatusLine api={{catalogSyncStatus: async () => status as never}}/>);
  expect(await screen.findByText("bad.recipe: broken")).toBeVisible();
});

test("reconcile submits only after the plan is reviewed", async () => {
  const api = {previewInstallationReconcile: vi.fn().mockResolvedValue({allowed: true, phases: [{}], blockers: [], reclaimed_bytes: 0}), reconcileInstallation: vi.fn().mockResolvedValue({})};
  render(<InstallationReconcile api={api} installationId="i1"/>);
  expect(screen.queryByRole("button", {name: "Confirm reconcile"})).toBeNull();
  await userEvent.click(screen.getByRole("button", {name: "Review reconcile"}));
  await userEvent.click(await screen.findByRole("button", {name: "Confirm reconcile"}));
  await waitFor(() => expect(api.reconcileInstallation).toHaveBeenCalledWith("i1", expect.any(String)));
});
