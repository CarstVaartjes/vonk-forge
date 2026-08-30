import {act, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {afterEach, vi} from "vitest";
import type {ControlApi} from "../api/types";
import {ActivityPage, activityStatus} from "./activity";

const NOW = new Date("2026-08-15T12:00:00Z");
const REQUEST_ID = "f6e73ce3-3329-4ff4-b086-d8f87c879ce9";
const TARGET_ID = `spk_${"1".repeat(32)}`;
const TECHNICAL_TARGET_ID = `spk_${"2".repeat(32)}`;

const visualFleet = {
  schema_version: 1 as const,
  generated_at: "2026-08-15T12:00:00Z",
  authority_revision: "a".repeat(64),
  event_cursor: 1,
  nodes: [
    {id: TARGET_ID, display_name: "Mia Lab Spark", hostname: "spark-a", lifecycle: "ready", labels: {}, connection: {}, installed: [], loaded: [], inventory: null, reservations: {}, telemetry: null, warnings: []},
    {id: TECHNICAL_TARGET_ID, display_name: TECHNICAL_TARGET_ID, hostname: `${TECHNICAL_TARGET_ID}.local`, lifecycle: "ready", labels: {}, connection: {}, installed: [], loaded: [], inventory: null, reservations: {}, telemetry: null, warnings: []},
  ],
};

const emptyLibrary = {
  schema_version: 1 as const,
  generated_at: "2026-08-15T12:00:00Z",
  freshness_policy: {inventory_fresh_seconds: 300, telemetry_live_seconds: 6, telemetry_delayed_seconds: 20},
  models: [],
  unlinked_recipes: [],
  next_cursor: null,
};

function response() {
  return {events: [{
    request_id: REQUEST_ID,
    actor: "admin",
    action: "recipe.start",
    authority_revision: "a".repeat(64),
    occurred_at: "2026-08-15T11:59:00Z",
    targets: [TARGET_ID, TECHNICAL_TARGET_ID],
  }, {
    request_id: "audit-rejected",
    actor: "operator@example.test",
    action: "agent.enrollment.submit.rejected",
    occurred_at: "2026-08-15T11:30:00Z",
    targets: ["missing-object"],
  }, {
    request_id: "audit-review",
    actor: "admin",
    action: "agent.enrollment.submit.uncertain",
    targets: [],
  }]};
}

function api(
  loadAudit = vi.fn().mockResolvedValue(response()),
  loadJobs = vi.fn().mockResolvedValue({
    jobs: [{id: "operation-1", kind: "recipe-install", state: "running", created_at: "2026-08-15T11:58:00Z"}],
    next_cursor: null,
    total: 1,
  }),
  loadJob = vi.fn().mockResolvedValue({
    id: "operation-1",
    kind: "recipe-install",
    state: "running",
    authority_revision: "a".repeat(64),
    targets: [TARGET_ID],
    target_next_cursor: null,
    target_total: 1,
    current_attempt: 1,
    status_reason: null,
    reconciliation_id: null,
    operations: [],
    operation_next_cursor: null,
    operation_total: 1,
    progress: {completed: 0, failed: 0, running: 1, total: 1},
  }),
  resumeJob = vi.fn().mockResolvedValue({id: "operation-1", state: "queued"}),
): Pick<ControlApi, "audit" | "job" | "jobs" | "librarySnapshot" | "resumeJob" | "visualFleet"> {
  return {
    audit: loadAudit,
    job: loadJob,
    jobs: loadJobs,
    librarySnapshot: vi.fn().mockResolvedValue(emptyLibrary),
    resumeJob,
    visualFleet: vi.fn().mockResolvedValue(visualFleet),
  } as unknown as Pick<ControlApi, "audit" | "job" | "jobs" | "librarySnapshot" | "resumeJob" | "visualFleet">;
}

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

test("uses honest status labels for active and operator-blocked operations", () => {
  const base = {request_id: "operation", actor: "Vonk Forge", authority_revision: undefined, targets: []};
  expect(activityStatus({...base, action: "operation.reconcile.planned"})).toBe("in_progress");
  expect(activityStatus({...base, action: "operation.reconcile.waiting-for-operator"})).toBe("attention");
  expect(activityStatus({...base, action: "operation.reconcile.failed"})).toBe("unsuccessful");
  expect(activityStatus({...base, action: "operation.reconcile.expired"})).toBe("unsuccessful");
  expect(activityStatus({...base, action: "operation.reconcile.succeeded"})).toBe("recorded");
  expect(activityStatus({...base, action: "operation.reconcile.future-state"})).toBe("unknown");
});

test("renders friendly timeline labels, honest time metadata, and hidden copyable identifiers", async () => {
  const user = userEvent.setup();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {configurable: true, value: {writeText}});
  render(<ActivityPage api={api()} now={NOW}/>);

  expect(await screen.findByRole("heading", {name: "Started recipe"})).toBeVisible();
  expect(screen.getByRole("heading", {name: "Recipe Install · Running"})).toBeVisible();
  expect(screen.getAllByText("In progress").length).toBeGreaterThan(0);
  expect(screen.getByText("Spark enrollment needs review")).toBeVisible();
  expect(screen.getAllByText("Unsuccessful").length).toBeGreaterThan(0);
  const timestamp = screen.getByText(/1 minute ago/i).closest("time");
  expect(timestamp).toHaveAttribute("datetime", "2026-08-15T11:59:00Z");
  expect(timestamp).toHaveAttribute("title");
  expect(screen.getByText("Time not recorded")).toBeVisible();
  expect(screen.getByText(/Mia Lab Spark/)).toBeVisible();
  expect(screen.getByText(/Unnamed Spark/)).toBeVisible();
  expect(screen.getByText("1 historical target")).toBeVisible();
  expect(screen.queryByText("Unavailable object")).not.toBeInTheDocument();

  const hiddenId = screen.getByText(REQUEST_ID);
  expect(hiddenId).not.toBeVisible();
  expect(screen.getByText(TECHNICAL_TARGET_ID)).not.toBeVisible();
  const firstEvent = screen.getByRole("heading", {name: "Started recipe"}).closest("article")!;
  await user.click(within(firstEvent).getByText("Technical details"));
  expect(hiddenId).toBeVisible();
  expect(screen.getByText(TARGET_ID)).toBeVisible();
  await user.click(within(firstEvent).getByRole("button", {name: "Copy request id"}));
  expect(writeText).toHaveBeenCalledWith(REQUEST_ID);
  expect(await within(firstEvent).findByRole("button", {name: "Copy request id"})).toHaveTextContent("Copied");
  expect(within(firstEvent).getByText("Request ID copied")).toBeInTheDocument();
});

test("keeps summaries in sync with filters and offers a recoverable empty state", async () => {
  const user = userEvent.setup();
  render(<ActivityPage api={api()} now={NOW}/>);
  await screen.findByRole("heading", {name: "Started recipe"});

  const loadedSummary = screen.getByRole("region", {name: "Loaded activity summary"});
  expect(loadedSummary).toHaveTextContent("Summary of 4 loaded events");
  expect(within(loadedSummary).getByRole("img")).toHaveAccessibleName("1 recorded, 1 in progress, 1 need review, 1 unsuccessful, 0 unknown");

  await user.selectOptions(screen.getByLabelText("Area"), "Sparks");
  expect(screen.queryByRole("heading", {name: "Started recipe"})).not.toBeInTheDocument();
  expect(screen.getByRole("heading", {name: "Rejected Spark enrollment"})).toBeVisible();
  const areaSummary = screen.getByRole("region", {name: "Matching activity summary"});
  expect(areaSummary).toHaveTextContent("Summary of 2 matching events from 4 loaded");
  expect(within(areaSummary).getByRole("img")).toHaveAccessibleName("0 recorded, 0 in progress, 1 need review, 1 unsuccessful, 0 unknown");

  await user.selectOptions(screen.getByLabelText("Status"), "attention");
  expect(screen.getByRole("heading", {name: "Spark enrollment needs review"})).toBeVisible();
  expect(screen.queryByRole("heading", {name: "Rejected Spark enrollment"})).not.toBeInTheDocument();
  expect(screen.getByRole("region", {name: "Matching activity summary"})).toHaveTextContent("Summary of 1 matching event from 4 loaded");

  await user.selectOptions(screen.getByLabelText("Operator"), "operator@example.test");
  const emptyState = screen.getByText("No matching activity").closest("section")!;
  expect(emptyState).toBeVisible();
  expect(within(screen.getByRole("region", {name: "Matching activity summary"})).getByRole("img")).toHaveAccessibleName("0 recorded, 0 in progress, 0 need review, 0 unsuccessful, 0 unknown");
  await user.click(within(emptyState).getByRole("button", {name: "Clear filters"}));
  expect(screen.getByRole("heading", {name: "Started recipe"})).toBeVisible();

  await user.type(screen.getByRole("searchbox", {name: "Search activity"}), "Mia Lab Spark");
  expect(screen.getByRole("heading", {name: "Started recipe"})).toBeVisible();
  expect(screen.queryByRole("heading", {name: "Rejected Spark enrollment"})).not.toBeInTheDocument();
});

test("keeps the timeline chronological unless attention-first sorting is selected", async () => {
  const loadJobs = vi.fn().mockResolvedValue({
    jobs: [
      {id: "operation-unknown", kind: "recipe-install", state: "future-state", created_at: "2026-08-15T12:01:00Z"},
      {id: "operation-recorded", kind: "recipe-install", state: "succeeded", created_at: "2026-08-15T12:02:00Z"},
    ],
    next_cursor: null,
    total: 2,
  });
  const user = userEvent.setup();
  render(<ActivityPage api={api(vi.fn().mockResolvedValue(response()), loadJobs)} now={NOW}/>);

  const timeline = await screen.findByRole("list", {name: "Activity timeline"});
  expect(within(timeline).getAllByRole("heading").slice(0, 2).map(heading => heading.textContent)).toEqual(["Recipe Install · Completed", "Recipe Install · Future State"]);
  const unknownEvent = screen.getByRole("heading", {name: "Recipe Install · Future State"}).closest("article")!;
  expect(within(unknownEvent).getByText("Unknown state")).toHaveClass("status-pill-neutral");

  await user.selectOptions(screen.getByLabelText("Sort"), "attention");
  expect(within(timeline).getAllByRole("heading").slice(0, 2).map(heading => heading.textContent)).toEqual(["Spark enrollment needs review", "Rejected Spark enrollment"]);
});

test("switches to the responsive table view and persists that preference", async () => {
  const user = userEvent.setup();
  const first = render(<ActivityPage api={api()} now={NOW}/>);
  await screen.findByRole("heading", {name: "Started recipe"});

  await user.click(screen.getByRole("button", {name: "Table"}));
  expect(screen.getByRole("button", {name: "Table"})).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("table", {name: "Recorded operator and system activity"})).toBeVisible();
  expect(localStorage.getItem("vonk.activity.view")).toBe("table");

  first.unmount();
  render(<ActivityPage api={api()} now={NOW}/>);
  expect(await screen.findByRole("table", {name: "Recorded operator and system activity"})).toBeVisible();
});

test("loads truthful operation progress and resumes only an operator-waiting job", async () => {
  const waiting = {
    id: "operation-1",
    kind: "recipe-install",
    state: "waiting-for-operator",
    authority_revision: "a".repeat(64),
    targets: [TARGET_ID],
    target_next_cursor: null,
    target_total: 1,
    current_attempt: 2,
    status_reason: "inspect worker logs",
    reconciliation_id: "reconcile-1",
    operations: [{id: "step-1", graph_operation_id: null, node_id: TARGET_ID, kind: "distribute", state: "failed", attempt: 2, progress: {phase: "verify"}, updated_at: "2026-08-15T11:59:30Z"}],
    operation_next_cursor: null,
    operation_total: 1,
    progress: {completed: 0, failed: 1, running: 0, total: 1},
  };
  const queued = {...waiting, state: "queued", status_reason: null, progress: {completed: 0, failed: 0, running: 1, total: 1}};
  const loadJob = vi.fn().mockResolvedValueOnce(waiting).mockResolvedValueOnce(queued);
  const resumeJob = vi.fn().mockResolvedValue({id: "operation-1", state: "queued"});
  const user = userEvent.setup();
  render(<ActivityPage api={api(undefined, undefined, loadJob, resumeJob)} now={NOW}/>);

  await screen.findByRole("heading", {name: "Recipe Install · Running"});
  await user.click(screen.getByText("View operation progress"));

  expect(await screen.findByText("inspect worker logs")).toBeVisible();
  expect(screen.getByRole("region", {name: "Operation progress"})).toHaveTextContent("Failed1");
  expect(screen.getAllByText("Mia Lab Spark").some(element => element.closest(".activity-job-body"))).toBe(true);
  expect(screen.getByText("Phase: verify")).toBeVisible();
  expect(screen.getByRole("button", {name: "Resume operation"})).toBeVisible();
  expect(screen.queryByText("step-1")).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", {name: "Resume operation"}));
  await waitFor(() => expect(resumeJob).toHaveBeenCalledWith("operation-1"));
  expect(await screen.findByText("Operation resumed and current details reloaded.")).toBeVisible();
  expect(screen.getByRole("heading", {name: "Recipe Install · Queued"})).toBeVisible();
  expect(screen.queryByRole("button", {name: "Resume operation"})).not.toBeInTheDocument();
  expect(loadJob).toHaveBeenCalledTimes(2);
});

test("explains an unresolved legacy agent upgrade without guessing its failure stage", async () => {
  const expectedBinary = "b".repeat(64);
  const expectedBuild = `sha256:${"c".repeat(64)}`;
  const oldBinary = "d".repeat(64);
  const oldBuild = `sha256:${"e".repeat(64)}`;
  const nextAction = "Keep the rollout paused and inspect the Spark package-helper and dpkg recovery state before resuming. When ready, Resume queues the retry behind a new safety delay; it does not dispatch immediately. Do not advance to another Spark until this Spark reports the exact target identity.";
  const loadJobs = vi.fn().mockResolvedValue({
    jobs: [{id: "upgrade-1", kind: "agent-upgrade", state: "waiting-for-operator", created_at: "2026-08-15T11:58:00Z"}],
    next_cursor: null,
    total: 1,
  });
  const loadJob = vi.fn().mockResolvedValue({
    id: "upgrade-1",
    kind: "agent-upgrade",
    state: "waiting-for-operator",
    authority_revision: "a".repeat(64),
    targets: [TARGET_ID],
    target_next_cursor: null,
    target_total: 1,
    current_attempt: 1,
    status_reason: "The exact target identity was not proven.",
    reconciliation_id: null,
    operations: [{id: "upgrade-step", graph_operation_id: null, node_id: TARGET_ID, kind: "agent.upgrade.v1", state: "waiting-for-operator", attempt: 2, progress: null, updated_at: "2026-08-15T11:59:30Z"}],
    operation_next_cursor: null,
    operation_total: 1,
    progress: {completed: 0, failed: 0, running: 0, total: 1},
    agent_upgrade_diagnostics: {
      expected_identity: {version: "0.1.0~dev.350+g15f9faf7c5bf", binary_digest: expectedBinary, build_digest: expectedBuild},
      targets: [{
        node_id: TARGET_ID,
        state: "waiting-for-operator",
        attempts: 2,
        target_proven: false,
        observed_identity: {version: "0.1.0", binary_digest: oldBinary, build_digest: oldBuild},
        raw_reason: "agent upgrade request is invalid",
        retry_not_before: null,
        retry_queued: false,
      }],
      legacy_generic_ambiguous: true,
      next_action: nextAction,
      operator_summary: "The exact target identity was not proven.",
    },
  });
  const user = userEvent.setup();
  render(<ActivityPage api={api(undefined, loadJobs, loadJob)} now={NOW}/>);

  await screen.findByRole("heading", {name: "Agent Upgrade · Waiting for operator"});
  await user.click(screen.getByText("View operation progress"));

  const diagnosis = await screen.findByRole("region", {name: "Agent upgrade diagnosis"});
  expect(diagnosis).toHaveTextContent("0.1.0~dev.350+g15f9faf7c5bf");
  expect(diagnosis).toHaveTextContent("2 install attempts · exact target not reported");
  expect(diagnosis).toHaveTextContent("Observed version0.1.0");
  expect(diagnosis).toHaveTextContent("Legacy helper response is ambiguous");
  expect(diagnosis).toHaveTextContent("does not prove that authorization or download failed");
  expect(within(diagnosis).queryByText("Retry not before")).not.toBeInTheDocument();
  expect(screen.getByText(nextAction)).toBeVisible();
  const queueRetry = screen.getByRole("button", {name: "Queue retry after inspection"});
  expect(queueRetry).toBeVisible();
  expect(screen.queryByText("Current attempt")).not.toBeInTheDocument();
  expect(screen.getByText("agent upgrade request is invalid")).not.toBeVisible();
  await user.click(within(diagnosis).getByText("Raw helper evidence"));
  expect(screen.getByText("agent upgrade request is invalid")).toBeVisible();
  await user.click(queueRetry);
  expect(await screen.findByText("Retry queued. It will not dispatch before the reported retry time.")).toBeVisible();
});

test("polls a safety-delayed helper retry and shows specific recovery guidance", async () => {
  let intervalCallback: (() => void) | undefined;
  vi.spyOn(window, "setInterval").mockImplementation(handler => {
    intervalCallback = handler as () => void;
    return 1;
  });
  const nextAction = "Wait for the controller-managed retry behind its safety delay; it will not dispatch before the reported retry time. Do not manually resume the rollout again.";
  const detail = {
    id: "upgrade-1", kind: "agent-upgrade", state: "waiting-for-operator", authority_revision: "a".repeat(64), targets: [TARGET_ID], target_next_cursor: null, target_total: 1, current_attempt: 1, status_reason: "agent upgrade helper is unavailable", reconciliation_id: null,
    operations: [{id: "upgrade-step", graph_operation_id: null, node_id: TARGET_ID, kind: "agent.upgrade.v1", state: "waiting-for-operator", attempt: 2, progress: null, updated_at: "2026-08-15T11:59:30Z"}], operation_next_cursor: null, operation_total: 1, progress: {completed: 0, failed: 0, running: 0, total: 1},
    agent_upgrade_diagnostics: {
      expected_identity: {version: "0.1.0~dev.350+g15f9faf7c5bf", binary_digest: "b".repeat(64), build_digest: `sha256:${"c".repeat(64)}`},
      targets: [{node_id: TARGET_ID, state: "waiting-for-operator", attempts: 2, target_proven: false, observed_identity: {version: "0.1.0", binary_digest: "d".repeat(64), build_digest: `sha256:${"e".repeat(64)}`}, raw_reason: "agent upgrade helper is unavailable", retry_not_before: "2026-08-15T12:04:00Z", retry_queued: true}],
      legacy_generic_ambiguous: false, next_action: nextAction, operator_summary: null,
    },
  };
  const loadJob = vi.fn().mockResolvedValue(detail);
  const loadJobs = vi.fn().mockResolvedValue({jobs: [{id: "upgrade-1", kind: "agent-upgrade", state: "waiting-for-operator", created_at: "2026-08-15T11:58:00Z"}], next_cursor: null, total: 1});
  const user = userEvent.setup();
  render(<ActivityPage api={api(undefined, loadJobs, loadJob)} now={NOW}/>);

  await screen.findByRole("heading", {name: "Agent Upgrade · Waiting for operator"});
  await user.click(screen.getByText("View operation progress"));
  expect(await screen.findByText("Retry queued behind safety delay")).toBeVisible();
  expect(screen.getByText(nextAction)).toBeVisible();
  expect(screen.getByText("Controller retry not before")).toBeVisible();
  expect(screen.getByText("Updates automatically while this operation is active.")).toBeVisible();
  expect(screen.queryByRole("button", {name: "Queue retry after inspection"})).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("button", {name: "Refresh details"})).toBeEnabled());
  expect(intervalCallback).toBeDefined();
  await act(async () => intervalCallback?.());
  await waitFor(() => expect(loadJob).toHaveBeenCalledTimes(2));
});

test("shows a retryable operation-detail failure without offering resume", async () => {
  const loadJob = vi.fn().mockRejectedValueOnce(new Error("operation projection unavailable")).mockResolvedValueOnce({
    id: "operation-1", kind: "recipe-install", state: "failed", authority_revision: "a".repeat(64), targets: [], target_next_cursor: null, target_total: 0, current_attempt: 1, status_reason: "worker exited", reconciliation_id: null, operations: [], operation_next_cursor: null, operation_total: 0, progress: {completed: 0, failed: 0, running: 0, total: 0},
  });
  const user = userEvent.setup();
  render(<ActivityPage api={api(undefined, undefined, loadJob)} now={NOW}/>);

  await screen.findByRole("heading", {name: "Recipe Install · Running"});
  await user.click(screen.getByText("View operation progress"));
  expect(await screen.findByRole("alert")).toHaveTextContent("operation projection unavailable");
  expect(screen.queryByRole("button", {name: "Resume operation"})).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", {name: "Try again"}));
  expect(await screen.findByText("worker exited")).toBeVisible();
  expect(loadJob).toHaveBeenCalledTimes(2);
});

test("keeps available operations visible when audit loading fails and retries all sources", async () => {
  let calls = 0;
  const load = vi.fn().mockImplementation(async () => {
    calls += 1;
    if (calls === 1) throw new Error("audit authority unavailable");
    return {events: []};
  });
  const user = userEvent.setup();
  const noJobs = vi.fn().mockResolvedValue({jobs: [], next_cursor: null, total: 0});
  render(<ActivityPage api={api(load, noJobs)} now={NOW}/>);

  expect(screen.getByText("Loading activity…")).toBeVisible();
  expect(await screen.findByRole("alert")).toHaveTextContent("Audit history could not be loaded (audit authority unavailable). Showing operations only.");
  expect(screen.getByRole("region", {name: "Activity history coverage"})).toHaveTextContent("Audit history is unavailable and 0 of 0 operations");
  await user.click(screen.getByRole("button", {name: "Retry all sources"}));
  await waitFor(() => expect(screen.getByText("No activity in the loaded window")).toBeVisible());
  expect(screen.queryByText("Some activity could not be loaded")).not.toBeInTheDocument();
  expect(load).toHaveBeenCalledTimes(2);
});

test("clears dynamic filters that are no longer available after refresh", async () => {
  const loadAudit = vi.fn().mockResolvedValueOnce(response()).mockResolvedValueOnce({events: []});
  const user = userEvent.setup();
  render(<ActivityPage api={api(loadAudit)} now={NOW}/>);
  await screen.findByRole("heading", {name: "Started recipe"});

  await user.selectOptions(screen.getByLabelText("Operator"), "admin");
  expect(screen.getByLabelText("Operator")).toHaveValue("admin");
  await user.click(screen.getByRole("button", {name: "Refresh activity"}));

  await waitFor(() => expect(loadAudit).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByLabelText("Operator")).toHaveValue(""));
  expect(screen.getByRole("heading", {name: "Recipe Install · Running"})).toBeVisible();
});

test("resets pagination busy state when a new activity load supersedes load-more", async () => {
  const never = new Promise<never>(() => undefined);
  const firstJobs = vi.fn()
    .mockResolvedValueOnce({jobs: [{id: "operation-1", kind: "recipe-install", state: "running", created_at: "2026-08-15T11:58:00Z"}], next_cursor: "older", total: 2})
    .mockReturnValueOnce(never);
  const firstApi = api(vi.fn().mockResolvedValue({events: []}), firstJobs);
  const secondApi = api(vi.fn().mockResolvedValue({events: []}), vi.fn().mockResolvedValue({jobs: [], next_cursor: null, total: 0}));
  const user = userEvent.setup();
  const view = render(<ActivityPage api={firstApi} now={NOW}/>);
  await screen.findByRole("heading", {name: "Recipe Install · Running"});
  await user.click(screen.getByRole("button", {name: "Load older operations"}));
  expect(screen.getByRole("button", {name: "Loading older operations…"})).toBeDisabled();

  view.rerender(<ActivityPage api={secondApi} now={NOW}/>);
  await waitFor(() => expect(screen.getByText("No activity in the loaded window")).toBeVisible());
  expect(screen.getByRole("button", {name: "Refresh activity"})).toBeEnabled();
});

test("shows a full retryable error only when both primary activity sources fail", async () => {
  const user = userEvent.setup();
  const loadAudit = vi.fn().mockRejectedValue(new Error("audit unavailable"));
  const loadJobs = vi.fn().mockRejectedValue(new Error("operations unavailable"));
  render(<ActivityPage api={api(loadAudit, loadJobs)} now={NOW}/>);

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Unable to load audit history or operations");
  expect(screen.queryByRole("region", {name: "Activity history coverage"})).not.toBeInTheDocument();
  await user.click(within(alert).getByRole("button", {name: "Try again"}));
  expect(loadAudit).toHaveBeenCalledTimes(2);
  expect(loadJobs).toHaveBeenCalledTimes(2);
});

test("discloses bounded API windows and loads older operations when a cursor is available", async () => {
  const loadJobs = vi.fn()
    .mockResolvedValueOnce({jobs: [{id: "operation-new", kind: "recipe-install", state: "running", created_at: "2026-08-15T11:58:00Z"}], next_cursor: "older-page", total: 2})
    .mockResolvedValueOnce({jobs: [{id: "operation-old", kind: "recipe-stop", state: "succeeded", created_at: "2026-08-14T11:58:00Z"}], next_cursor: null, total: 2});
  const user = userEvent.setup();
  render(<ActivityPage api={api(vi.fn().mockResolvedValue({events: []}), loadJobs)} now={NOW}/>);

  expect(await screen.findByRole("region", {name: "Activity history coverage"})).toHaveTextContent("Loaded 0 audit records from the latest-100 API window and 1 of 2 operations");
  await user.click(screen.getByRole("button", {name: "Load older operations"}));

  expect(await screen.findByRole("heading", {name: "Recipe Stop · Completed"})).toBeVisible();
  expect(screen.getByRole("region", {name: "Activity history coverage"})).toHaveTextContent("2 of 2 operations");
  expect(loadJobs).toHaveBeenNthCalledWith(2, "older-page");
  expect(screen.queryByRole("button", {name: "Load older operations"})).not.toBeInTheDocument();
});
