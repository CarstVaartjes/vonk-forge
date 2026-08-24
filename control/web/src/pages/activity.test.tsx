import {render, screen, waitFor, within} from "@testing-library/react";
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
): Pick<ControlApi, "audit" | "jobs" | "librarySnapshot" | "visualFleet"> {
  return {
    audit: loadAudit,
    jobs: loadJobs,
    librarySnapshot: vi.fn().mockResolvedValue(emptyLibrary),
    visualFleet: vi.fn().mockResolvedValue(visualFleet),
  } as unknown as Pick<ControlApi, "audit" | "jobs" | "librarySnapshot" | "visualFleet">;
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
  expect(activityStatus({...base, action: "operation.reconcile.succeeded"})).toBe("recorded");
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
  expect(screen.getByText("Unavailable object")).toBeVisible();

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
});

test("filters by area, operator, status, and search with a recoverable empty state", async () => {
  const user = userEvent.setup();
  render(<ActivityPage api={api()} now={NOW}/>);
  await screen.findByRole("heading", {name: "Started recipe"});

  await user.selectOptions(screen.getByLabelText("Area"), "Sparks");
  expect(screen.queryByRole("heading", {name: "Started recipe"})).not.toBeInTheDocument();
  expect(screen.getByRole("heading", {name: "Rejected Spark enrollment"})).toBeVisible();

  await user.selectOptions(screen.getByLabelText("Status"), "attention");
  expect(screen.getByRole("heading", {name: "Spark enrollment needs review"})).toBeVisible();
  expect(screen.queryByRole("heading", {name: "Rejected Spark enrollment"})).not.toBeInTheDocument();

  await user.selectOptions(screen.getByLabelText("Operator"), "operator@example.test");
  const emptyState = screen.getByText("No matching activity").closest("section")!;
  expect(emptyState).toBeVisible();
  await user.click(within(emptyState).getByRole("button", {name: "Clear filters"}));
  expect(screen.getByRole("heading", {name: "Started recipe"})).toBeVisible();

  await user.type(screen.getByRole("searchbox", {name: "Search activity"}), TARGET_ID);
  expect(screen.getByRole("heading", {name: "Started recipe"})).toBeVisible();
  expect(screen.queryByRole("heading", {name: "Rejected Spark enrollment"})).not.toBeInTheDocument();
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

test("shows loading and retryable error states without inventing activity", async () => {
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
  expect(await screen.findByRole("alert")).toHaveTextContent("audit authority unavailable");
  await user.click(screen.getByRole("button", {name: "Try again"}));
  await waitFor(() => expect(screen.getByText("No activity in the loaded window")).toBeVisible());
  expect(load).toHaveBeenCalledTimes(2);
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
