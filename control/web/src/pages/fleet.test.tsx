import {act, fireEvent, render, screen, within} from "@testing-library/react";
import type {ControlApi, TelemetryPoint, VisualFleetNode, VisualFleetSnapshot} from "../api/types";
import {App} from "../app";
import {ENROLLMENT_GRANT_TTL_SECONDS, FLEET_VIEW_STORAGE_KEY, FleetPage} from "./fleet";

const NOW = new Date("2026-08-15T12:00:00Z");
const GIB = 1024 ** 3;

class FakeEventSource extends EventTarget {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  closed = false;

  constructor(url: string | URL) {
    super();
    this.url = String(url);
    FakeEventSource.instances.push(this);
  }

  close(): void { this.closed = true; }

  emit(type: string, data?: unknown, lastEventId = ""): void {
    this.dispatchEvent(type === "open" || type === "error"
      ? new Event(type)
      : new MessageEvent(type, {data: JSON.stringify(data), lastEventId}));
  }
}

function sample(nodeId: string, observedAt: string, gpu = 10): TelemetryPoint {
  return {
    id: `${nodeId}-sample-${gpu}`, node_id: nodeId, boot_id: "00000000-0000-0000-0000-000000000001", sequence: gpu,
    observed_at: observedAt, received_at: observedAt,
    cpu_utilization_percent: 12, load_average_1m: 1,
    memory_total_bytes: 100 * GIB, memory_available_bytes: 80 * GIB,
    disk_total_bytes: 200 * GIB, disk_free_bytes: 120 * GIB,
    gpu_utilization_percent: gpu, gpu_memory_total_bytes: 100 * GIB, gpu_memory_free_bytes: 70 * GIB,
    temperature_c: 42, power_watts: 18,
    network_receive_bytes_per_second: 1024, network_transmit_bytes_per_second: 512,
    gap_samples: 0, details: {accelerator_name: "NVIDIA GB10", accelerator_performance_state: "P0"},
  };
}

function node(id: string, name: string, observedAt: string | null, offline = false): VisualFleetNode {
  return {
    id, display_name: name, hostname: `${id}.internal`, lifecycle: "managed", labels: {role: "inference"},
    connection: {agent_state: "active", certificate_state: offline ? "expired" : "valid", online_state: offline ? "offline" : "online", offline_reason: offline ? "certificate-expired" : null, last_seen_at: "2026-08-15T11:59:59Z", last_seen_age_seconds: 1},
    inventory: null,
    telemetry: observedAt ? {age_seconds: 0, freshness: "live", sample: sample(id, observedAt)} : null,
    installed: [], loaded: [],
    reservations: {disk_bytes: 0, unified_memory_bytes: 0, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 0},
    warnings: [],
  };
}

function snapshot(nodes: VisualFleetNode[], cursor = 5): VisualFleetSnapshot {
  return {schema_version: 1, event_cursor: cursor, generated_at: NOW.toISOString(), authority_revision: "a".repeat(64), nodes};
}

function control(
  visualFleet: ControlApi["visualFleet"],
  history: ControlApi["nodeTelemetryHistory"] = async (nodeId, start, end, resolution, maximumPoints) => ({schema_version: 1, node_id: nodeId, start, end, resolution, maximum_points: maximumPoints, points: []}),
  updateNodeProfile: ControlApi["updateNodeProfile"] = async (nodeId, input) => ({id: nodeId, display_name: input.display_name, hostname: `${nodeId}.internal`, ip_address: null}),
): ControlApi {
  return {
    visualFleet,
    nodeTelemetryHistory: history,
    updateNodeProfile,
    fleetProfiles: async () => ({profiles: []}),
  } as unknown as ControlApi;
}

async function flush(): Promise<void> {
  await act(async () => { await Promise.resolve(); });
}

function openFleetControls(): void {
  const summary = screen.getByText("Controls", {selector: "summary"});
  if (!summary.parentElement?.hasAttribute("open")) fireEvent.click(summary);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  history.replaceState(null, "", "/fleet");
  localStorage.clear();
});

afterEach(() => {
  history.replaceState(null, "", "/");
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("shows truthful cluster counts and live connection state", async () => {
  const live = node("node-a", "Alpha", "2026-08-15T11:59:58Z");
  live.installed = [
    {installation_id: "install-1", recipe_id: "recipe-1", recipe_revision_id: "revision-1", title: "Qwen", topology_name: "solo", expected_rank_count: 1, present_ranks: [0], member_node_ids: ["node-a"], rank: 0, role: "primary", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null},
    {installation_id: "install-2", recipe_id: "recipe-2", recipe_revision_id: "revision-2", title: "Vision", topology_name: "solo", expected_rank_count: 1, present_ranks: [], member_node_ids: [], rank: 0, role: "primary", rank_state: "installing", group_state: "partial", complete: false, degraded_reason: "missing-ranks"},
  ];
  live.loaded = [{run_id: "run-1", installation_id: "install-1", recipe_id: "recipe-1", recipe_revision_id: "revision-1", title: "Qwen", alias: "chat", expected_rank_count: 1, present_ranks: [0], member_node_ids: ["node-a"], rank: 0, role: "primary", rank_state: "running", rank_age_seconds: 2, rank_fresh: true, run_state: "running", route_state: "published", group_state: "healthy", healthy: true, degraded_reason: null}];
  render(<FleetPage api={control(async () => snapshot([
    live,
    node("node-b", "Beta", "2026-08-15T11:59:45Z"),
    node("node-c", "Gamma", null),
    node("node-d", "Delta", "2026-08-15T11:59:59Z", true),
  ]))}/>);
  await flush();

  const summary = screen.getByRole("region", {name: "Fleet summary"});
  for (const label of ["Live", "Delayed", "Stale", "Offline"]) {
    expect(within(within(summary).getByText(label).parentElement!).getByText("1")).toBeVisible();
  }
  expect(within(summary).getByText("70.0 GiB")).toBeVisible();
  expect(within(summary).queryByText("1 installed recipe")).not.toBeInTheDocument();
  expect(within(summary).queryByText("1 loaded recipe")).not.toBeInTheDocument();
  expect(within(summary).getByRole("button", {name: "3 active warnings"})).toBeVisible();
  expect(screen.getAllByRole("article")).toHaveLength(4);

  act(() => FakeEventSource.instances[0].emit("open"));
  expect(screen.getByText("Live connection")).toBeVisible();
  expect(screen.getByText("Fleet status: 1 live, 1 delayed, 1 stale, 1 offline; 1 installed recipe; 1 loaded recipe; 3 warnings.", {selector: "[aria-live='polite']"})).toBeInTheDocument();
});

test("labels partial and unknown unified capacity instead of implying a measured zero", async () => {
  const reporting = node("node-a", "Alpha", "2026-08-15T11:59:58Z");
  const missing = node("node-b", "Beta", "2026-08-15T11:59:58Z");
  missing.telemetry!.sample.gpu_memory_free_bytes = null;
  const view = render(<FleetPage api={control(async () => snapshot([reporting, missing]))}/>);
  await flush();

  let summary = screen.getByRole("region", {name: "Fleet summary"});
  expect(within(summary).getByText("70.0 GiB known")).toBeVisible();
  expect(within(summary).getByText("Partial · 1 of 2 live nodes reporting")).toBeInTheDocument();

  view.unmount();
  const unknown = node("node-c", "Gamma", "2026-08-15T11:59:58Z");
  unknown.telemetry!.sample.memory_available_bytes = null;
  unknown.telemetry!.sample.gpu_memory_free_bytes = null;
  render(<FleetPage api={control(async () => snapshot([unknown]))}/>);
  await flush();

  summary = screen.getByRole("region", {name: "Fleet summary"});
  expect(within(summary).getByText("Not reported")).toBeVisible();
  expect(within(summary).getByText("No live node reports both host and GPU free memory")).toBeInTheDocument();
  expect(within(summary).queryByText("0 B")).not.toBeInTheDocument();
});

test("switches between persisted compact and graphical topology views without exposing node IDs", async () => {
  const identity = "spk_0123456789abcdef0123456789abcdef";
  const alpha = node(identity, identity, "2026-08-15T11:59:58Z");
  alpha.hostname = "mia-lab-west.internal";
  alpha.loaded = [{run_id: "run-1", installation_id: "install-1", recipe_id: "recipe-1", recipe_revision_id: "revision-1", title: "DeepSeek pair", alias: "chat", expected_rank_count: 1, present_ranks: [0], member_node_ids: [identity], rank: 0, role: "leader", rank_state: "running", rank_age_seconds: 2, rank_fresh: true, run_state: "running", route_state: "published", group_state: "healthy", healthy: true, degraded_reason: null}];
  const api = control(async () => snapshot([alpha]));
  const view = render(<FleetPage api={api}/>);
  await flush();

  expect(screen.getByRole("article", {name: "Mia Lab West — Live"})).toBeVisible();
  expect(screen.queryByText(identity)).not.toBeInTheDocument();

  openFleetControls();
  fireEvent.click(screen.getByRole("button", {name: "Compact"}));
  expect(screen.getByRole("region", {name: "Fleet nodes compact table"})).toBeVisible();
  expect(localStorage.getItem(FLEET_VIEW_STORAGE_KEY)).toBe("compact");

  fireEvent.click(screen.getByRole("button", {name: "Topology"}));
  expect(screen.getByRole("region", {name: "Fleet topology"})).toBeVisible();
  expect(screen.getByText("DeepSeek pair")).toBeVisible();
  expect(screen.queryByText(identity)).not.toBeInTheDocument();
  expect(localStorage.getItem(FLEET_VIEW_STORAGE_KEY)).toBe("topology");

  view.unmount();
  render(<FleetPage api={api}/>);
  await flush();
  openFleetControls();
  expect(screen.getByRole("button", {name: "Topology"})).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("region", {name: "Fleet topology"})).toBeVisible();
});

test("accepts the legacy cards preference while naming and persisting the view as Detailed", async () => {
  localStorage.setItem(FLEET_VIEW_STORAGE_KEY, "cards");
  render(<FleetPage api={control(async () => snapshot([node("node-a", "Alpha", "2026-08-15T11:59:58Z")]))}/>);
  await flush();

  openFleetControls();
  expect(screen.getByRole("button", {name: "Detailed"})).toHaveAttribute("aria-pressed", "true");
  expect(screen.queryByRole("button", {name: "Cards"})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", {name: "Compact"}));
  fireEvent.click(screen.getByRole("button", {name: "Detailed"}));
  expect(localStorage.getItem(FLEET_VIEW_STORAGE_KEY)).toBe("detailed");
});

test("sorts attention first and combines friendly-name search with multi-select health filters", async () => {
  const live = node("node-a", "Alpha Studio", "2026-08-15T11:59:58Z");
  const delayed = node("node-b", "Beta Lab", "2026-08-15T11:59:45Z");
  const stale = node("node-c", "Gamma Suite", null);
  const offline = node("node-d", "Delta Rack", "2026-08-15T11:59:59Z", true);
  render(<FleetPage api={control(async () => snapshot([live, delayed, stale, offline]))}/>);
  await flush();

  openFleetControls();
  const detailed = screen.getByRole("region", {name: "Fleet nodes detailed"});
  expect(within(detailed).getAllByRole("article").map(article => article.getAttribute("aria-label"))).toEqual([
    "Delta Rack — Offline",
    "Gamma Suite — Stale",
    "Beta Lab — Delayed",
    "Alpha Studio — Live",
  ]);
  fireEvent.change(screen.getByRole("combobox", {name: "Sort"}), {target: {value: "name"}});
  expect(within(detailed).getAllByRole("article").map(article => article.getAttribute("aria-label"))).toEqual([
    "Alpha Studio — Live",
    "Beta Lab — Delayed",
    "Delta Rack — Offline",
    "Gamma Suite — Stale",
  ]);

  fireEvent.click(screen.getByRole("button", {name: "Show offline nodes"}));
  fireEvent.click(screen.getByRole("button", {name: "Show delayed nodes"}));
  expect(screen.getByText("Showing 2 of 4 Sparks", {selector: "[role='status']"})).toBeVisible();
  expect(screen.getByRole("article", {name: "Delta Rack — Offline"})).toBeVisible();
  expect(screen.getByRole("article", {name: "Beta Lab — Delayed"})).toBeVisible();
  expect(screen.queryByRole("article", {name: "Alpha Studio — Live"})).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", {name: "Clear filters"}));
  fireEvent.click(screen.getByRole("button", {name: "3 active warnings"}));
  expect(screen.getByText("Showing 3 of 4 Sparks", {selector: "[role='status']"})).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "Clear filters"}));
  const search = screen.getByRole("searchbox", {name: "Find a Spark"});
  fireEvent.change(search, {target: {value: "node-a.internal"}});
  expect(screen.getByRole("article", {name: "Alpha Studio — Live"})).toBeVisible();
  expect(screen.getByText("Showing 1 of 4 Sparks", {selector: "[role='status']"})).toBeVisible();
  fireEvent.change(search, {target: {value: "missing"}});
  expect(screen.getByRole("region", {name: "No matching Sparks"})).toBeVisible();
});

test("keeps selected details across views and clears them with safe focus when filtering hides the Spark", async () => {
  render(<FleetPage api={control(async () => snapshot([
    node("node-a", "Alpha", "2026-08-15T11:59:58Z"),
    node("node-b", "Beta", "2026-08-15T11:59:59Z", true),
  ]))}/>);
  await flush();

  openFleetControls();
  fireEvent.click(screen.getByRole("button", {name: "View Alpha details"}));
  await flush();
  fireEvent.click(screen.getByRole("button", {name: "Compact"}));
  expect(screen.getByRole("complementary", {name: "Alpha details"})).toBeVisible();

  const search = screen.getByRole("searchbox", {name: "Find a Spark"});
  search.focus();
  fireEvent.change(search, {target: {value: "Beta"}});
  expect(screen.queryByRole("complementary", {name: "Alpha details"})).not.toBeInTheDocument();
  expect(search).toHaveFocus();

  fireEvent.click(screen.getByRole("button", {name: "Clear filters"}));
  fireEvent.click(screen.getByRole("button", {name: "View Alpha details"}));
  await flush();
  fireEvent.click(screen.getByRole("button", {name: "Topology"}));
  expect(screen.getByRole("complementary", {name: "Alpha details"})).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "Close Alpha details"}));
  await flush();
  expect(search).toHaveFocus();
});

test("defaults card trends to 24h, supports the four bounded ranges, and appends the live sample", async () => {
  const alpha = node("node-a", "Alpha", "2026-08-15T11:59:58Z");
  const historyCalls: Array<{maximumPoints: number; resolution: string; start: string}> = [];
  const telemetryHistory: ControlApi["nodeTelemetryHistory"] = async (nodeId, start, end, resolution, maximumPoints) => {
    historyCalls.push({maximumPoints, resolution, start});
    return {
      schema_version: 1, node_id: nodeId, start, end, resolution, maximum_points: maximumPoints,
      points: [{...alpha.telemetry!.sample, id: "history-sample", gpu_utilization_percent: 41}],
    };
  };
  render(<FleetPage api={control(async () => snapshot([alpha]), telemetryHistory)}/>);
  await flush();
  openFleetControls();
  const range = screen.getByRole("combobox", {name: "Card trend range"});
  expect(range).toHaveValue("24h");
  expect(within(range).getAllByRole("option").map(option => option.textContent)).toEqual(["1h", "24h", "7d", "31d"]);
  expect(historyCalls[0]).toEqual({maximumPoints: 1440, resolution: "minute", start: "2026-08-14T12:00:00.000Z"});
  expect(screen.getByRole("img", {name: "GPU 24h trend"})).toHaveAccessibleDescription(/2 reported points/);

  act(() => FakeEventSource.instances[0].emit("node-telemetry", {schema_version: 1, node_id: "node-a", sample: sample("node-a", "2026-08-15T11:59:59Z", 73)}, "6"));

  expect(screen.getByRole("img", {name: "GPU 24h trend"})).toHaveAccessibleDescription(/Latest 73%/);
  fireEvent.change(range, {target: {value: "31d"}});
  await flush();
  expect(historyCalls.at(-1)).toEqual({maximumPoints: 2976, resolution: "fifteen-minute", start: "2026-07-15T12:00:00.000Z"});
  expect(screen.getByRole("img", {name: "GPU 31d trend"})).toBeVisible();
});

test("edits the friendly name while exposing read-only Spark identity facts", async () => {
  const alpha = node("node-a", "Alpha", "2026-08-15T11:59:58Z");
  alpha.hostname = "spark-3542.internal";
  alpha.ip_address = "192.168.1.211";
  const updateNodeProfile = vi.fn(async (nodeId: string, input: {display_name: string}) => ({id: nodeId, display_name: input.display_name, hostname: alpha.hostname, ip_address: alpha.ip_address}));
  render(<FleetPage api={control(async () => snapshot([alpha]), undefined, updateNodeProfile as ControlApi["updateNodeProfile"])}/>);
  await flush();

  fireEvent.click(screen.getByRole("button", {name: "Edit Alpha"}));
  const dialog = screen.getByRole("dialog", {name: "Name this Spark"});
  expect(within(dialog).getByText("spark-3542.internal")).toBeVisible();
  expect(within(dialog).getByText("192.168.1.211")).toBeVisible();
  expect(within(dialog).getByText("node-a")).toBeVisible();
  fireEvent.change(within(dialog).getByRole("textbox", {name: /^Friendly name/}), {target: {value: "Studio Spark"}});
  fireEvent.click(within(dialog).getByRole("button", {name: "Save friendly name"}));
  await flush();

  expect(updateNodeProfile).toHaveBeenCalledWith("node-a", {display_name: "Studio Spark"});
  expect(screen.getByRole("article", {name: "Studio Spark — Live"})).toBeVisible();
  expect(screen.queryByRole("dialog", {name: "Name this Spark"})).not.toBeInTheDocument();
});

test("keeps selected detail and focus while a keyed node card updates", async () => {
  render(<FleetPage api={control(async () => snapshot([node("node-a", "Alpha", "2026-08-15T11:59:58Z")]))}/>);
  await flush();
  const stream = FakeEventSource.instances[0];
  fireEvent.click(screen.getByRole("button", {name: "View Alpha details"}));
  await flush();
  const close = screen.getByRole("button", {name: "Close Alpha details"});
  expect(close).toHaveFocus();

  act(() => stream.emit("node-telemetry", {schema_version: 1, node_id: "node-a", sample: sample("node-a", "2026-08-15T11:59:59Z", 73)}, "6"));

  expect(screen.getByRole("img", {name: "GPU 24h trend"})).toHaveAccessibleDescription(/Latest 73%/);
  expect(screen.getByRole("complementary", {name: "Alpha details"})).toBeVisible();
  expect(close).toHaveFocus();
});

test("turns a silent delayed node stale from its timestamp without an event", async () => {
  const aging = node("node-a", "Alpha", "2026-08-15T11:59:40Z");
  aging.warnings = [{code: "telemetry.delayed", detail: "Telemetry delivery is delayed.", severity: "warning"}];
  render(<FleetPage api={control(async () => snapshot([aging]))}/>);
  await flush();
  expect(screen.getByRole("article", {name: "Alpha — Delayed"})).toBeVisible();
  expect(screen.getByText("Telemetry delivery is delayed.")).toBeVisible();

  act(() => vi.advanceTimersByTime(1_000));

  expect(screen.getByRole("article", {name: "Alpha — Stale"})).toBeVisible();
  expect(screen.queryByText("Telemetry delivery is delayed.")).not.toBeInTheDocument();
  expect(screen.getByText("Telemetry is stale.")).toBeVisible();
});

test("offers retry after an initial error and then shows the empty Fleet state", async () => {
  const visualFleet = vi.fn()
    .mockRejectedValueOnce(new Error("Control API returned 503: projection unavailable"))
    .mockResolvedValueOnce(snapshot([]));
  render(<FleetPage api={control(visualFleet)}/>);
  await flush();

  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent("Fleet unavailable");
  expect(alert).toHaveTextContent("projection unavailable");
  fireEvent.click(screen.getByRole("button", {name: "Retry Fleet"}));
  await flush();

  expect(screen.getByRole("heading", {name: "No registered Fleet nodes"})).toBeVisible();
  expect(screen.getByRole("button", {name: "Add your first Spark"})).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(visualFleet).toHaveBeenCalledTimes(2);
});

test("keeps Add Spark modal focus contained and restores the trigger on Escape", async () => {
  const api = control(async () => snapshot([])) as ControlApi;
  render(<FleetPage api={api}/>);
  await flush();

  const trigger = screen.getByRole("button", {name: "Add Spark"});
  fireEvent.click(trigger);
  const dialog = screen.getByRole("dialog", {name: "Add Spark"});
  const close = screen.getByRole("button", {name: "Close Add Spark"});
  const create = screen.getByRole("button", {name: "Create one-time enrollment command"});
  expect(close).toHaveFocus();
  expect(document.body).toHaveStyle({overflow: "hidden"});

  fireEvent.keyDown(close, {key: "Tab", shiftKey: true});
  expect(create).toHaveFocus();
  fireEvent.keyDown(create, {key: "Tab"});
  expect(close).toHaveFocus();
  fireEvent.keyDown(dialog, {key: "Escape"});
  await flush();

  expect(screen.queryByRole("dialog", {name: "Add Spark"})).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  expect(document.body.style.overflow).toBe("");
});

test("protects an in-flight or revealed enrollment grant from accidental dismissal", async () => {
  let releaseGrant!: (value: Awaited<ReturnType<ControlApi["createEnrollmentGrant"]>>) => void;
  const pendingGrant = new Promise<Awaited<ReturnType<ControlApi["createEnrollmentGrant"]>>>(resolve => { releaseGrant = resolve; });
  const api = control(async () => snapshot([])) as ControlApi;
  api.createEnrollmentGrant = vi.fn(() => pendingGrant);
  const onBusyChange = vi.fn();
  render(<FleetPage api={api} onBusyChange={onBusyChange}/>);
  await flush();

  fireEvent.click(screen.getByRole("button", {name: "Add Spark"}));
  const dialog = screen.getByRole("dialog", {name: "Add Spark"});
  const create = screen.getByRole("button", {name: "Create one-time enrollment command"});
  fireEvent.click(create);
  fireEvent.click(create);

  expect(api.createEnrollmentGrant).toHaveBeenCalledTimes(1);
  expect(dialog).toHaveAttribute("aria-busy", "true");
  expect(screen.getByRole("button", {name: "Close Add Spark"})).toBeDisabled();
  expect(screen.getByRole("button", {name: "Cancel"})).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent("Keep this window open");
  fireEvent.keyDown(dialog, {key: "Escape"});
  fireEvent.mouseDown(document.querySelector(".library-dialog-backdrop")!);
  expect(screen.getByRole("dialog", {name: "Add Spark"})).toBeVisible();
  const beforeUnload = new Event("beforeunload", {cancelable: true});
  dispatchEvent(beforeUnload);
  expect(beforeUnload.defaultPrevented).toBe(true);
  expect(onBusyChange).toHaveBeenLastCalledWith(true);

  await act(async () => releaseGrant({
    id: "grant-locked", purpose: "new-node", token: "short-lived-secret", expires_at: "2026-08-15T12:15:00Z",
    controller_endpoint: "https://controller.example.test:9443",
    enrollment_endpoint: "https://enrollment.example.test:9444",
    ca_fingerprint: "b".repeat(64),
    installer_url: "https://install.vonkforge.ai/spark",
    controller_address: "192.168.1.231",
    service_hostnames: ["controller.example.test", "enrollment.example.test"],
  }));

  expect(dialog).not.toHaveAttribute("aria-busy");
  fireEvent.click(screen.getByRole("button", {name: "Close Add Spark"}));
  expect(screen.getByText("Discard this one-time grant?")).toBeVisible();
  expect(screen.getByRole("button", {name: "Keep grant open"})).toHaveFocus();
  expect(screen.getByText("short-lived-secret")).toBeVisible();
  fireEvent.keyDown(dialog, {key: "Escape"});
  expect(screen.queryByText("Discard this one-time grant?")).not.toBeInTheDocument();
  fireEvent.mouseDown(document.querySelector(".library-dialog-backdrop")!);
  expect(screen.getByText("Discard this one-time grant?")).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "Discard grant"}));

  expect(screen.queryByRole("dialog", {name: "Add Spark"})).not.toBeInTheDocument();
  expect(onBusyChange).toHaveBeenLastCalledWith(false);
});

test("blocks primary and browser-history navigation while enrollment credentials are unresolved", async () => {
  let releaseGrant!: (value: Awaited<ReturnType<ControlApi["createEnrollmentGrant"]>>) => void;
  const pendingGrant = new Promise<Awaited<ReturnType<ControlApi["createEnrollmentGrant"]>>>(resolve => { releaseGrant = resolve; });
  const api = control(async () => snapshot([])) as ControlApi;
  api.createEnrollmentGrant = vi.fn(() => pendingGrant);
  api.librarySnapshot = vi.fn().mockResolvedValue({schema_version: 1, generated_at: NOW.toISOString(), freshness_policy: {inventory_fresh_seconds: 300, telemetry_live_seconds: 6, telemetry_delayed_seconds: 20}, models: [], unlinked_recipes: [], next_cursor: null});
  render(<App api={api}/>);
  await flush();

  fireEvent.click(screen.getByRole("button", {name: "Add Spark"}));
  fireEvent.click(screen.getByRole("button", {name: "Create one-time enrollment command"}));
  const libraryLink = screen.getByRole("link", {name: "Library"});
  expect(libraryLink).toHaveAttribute("aria-disabled", "true");
  fireEvent.click(libraryLink);
  expect(location.pathname).toBe("/fleet");
  expect(screen.getByRole("dialog", {name: "Add Spark"})).toBeVisible();

  act(() => {
    history.replaceState(null, "", "/library");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  expect(location.pathname).toBe("/fleet");
  expect(screen.getByRole("dialog", {name: "Add Spark"})).toBeVisible();

  await act(async () => releaseGrant({
    id: "grant-nav", purpose: "new-node", token: "navigation-secret", expires_at: "2026-08-15T12:15:00Z",
    controller_endpoint: "https://controller.example.test:9443",
    enrollment_endpoint: "https://enrollment.example.test:9444",
    ca_fingerprint: "c".repeat(64),
    installer_url: "https://install.vonkforge.ai/spark",
    controller_address: null,
    service_hostnames: ["controller.example.test", "enrollment.example.test"],
  }));
  fireEvent.click(screen.getByRole("button", {name: "I saved these values — Done"}));
  expect(libraryLink).not.toHaveAttribute("aria-disabled");
  fireEvent.click(libraryLink);
  expect(location.pathname).toBe("/library");
});

test("renders the one-command Spark installer with enrollment inputs", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", {...navigator, clipboard: {writeText}});
  const visualFleet = vi.fn().mockResolvedValue(snapshot([]));
  const api = control(visualFleet) as ControlApi;
  api.agents = vi.fn().mockResolvedValue({agents: []});
  api.enrollments = vi.fn().mockResolvedValue({enrollments: []});
  api.createEnrollmentGrant = vi.fn().mockResolvedValue({
    id: "grant-1", purpose: "new-node", token: "secret-token", expires_at: "2026-08-15T12:15:00Z",
    controller_endpoint: "https://controller.example.test:9443",
    enrollment_endpoint: "https://enrollment.example.test:9444",
    ca_fingerprint: "a".repeat(64),
    installer_url: "https://install.vonkforge.ai/spark",
    controller_address: "192.168.1.231",
    service_hostnames: ["controller.example.test", "enrollment.example.test"],
  });
  render(<FleetPage api={api}/>);
  await flush();
  fireEvent.click(screen.getByRole("button", {name: "Add Spark"}));
  expect(screen.getByText(/generates its immutable/i)).toBeVisible();
  expect(screen.getByText("Create grant").parentElement).toHaveTextContent("Generate a one-time authorization here.");
  fireEvent.click(screen.getByRole("button", {name: "Create one-time enrollment command"}));
  await flush();
  expect(api.createEnrollmentGrant).toHaveBeenCalledWith(ENROLLMENT_GRANT_TTL_SECONDS);
  expect(document.querySelector(".grant-success")).toHaveFocus();
  const command = document.querySelector<HTMLElement>(".onboarding-command")!;
  expect(command).toBeVisible();
  expect(command).toHaveTextContent("curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS=192.168.1.231 sh -s -- --enroll");
  expect(command).not.toHaveTextContent("sudo");
  expect(command).not.toHaveTextContent("vonk-agent pair");
  expect(screen.getByText("Expires in 15m 00s")).toBeVisible();
  expect(screen.getByText(/Keep these setup values private/)).toBeVisible();
  expect(screen.getByText("https://controller.example.test:9443")).toBeVisible();
  expect(screen.getByText("https://enrollment.example.test:9444")).toBeVisible();
  expect(screen.getByText("secret-token")).toBeVisible();
  expect(screen.getByText("NAS LAN address")).toBeVisible();
  expect(screen.getByText("192.168.1.231")).toBeVisible();
  expect(screen.getAllByRole("button", {name: /^Copy /})).toHaveLength(6);
  fireEvent.click(screen.getByRole("button", {name: "Copy installer command"}));
  await flush();
  expect(writeText).toHaveBeenCalledWith("curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS=192.168.1.231 sh -s -- --enroll");
  expect(screen.getAllByRole("status").some(status => status.textContent === "Copied")).toBe(true);
  fireEvent.click(screen.getByRole("button", {name: "I saved these values — Done"}));
  expect(screen.queryByRole("dialog", {name: "Add Spark"})).not.toBeInTheDocument();
});

test("re-enrollment uses the controller channel and generic enrollment mode", async () => {
  const nodeId = "spk_0123456789abcdef0123456789abcdef";
  const alpha = node(nodeId, "Spark Alpha", "2026-08-15T11:59:58Z");
  const api = control(async () => snapshot([alpha])) as ControlApi;
  api.createReenrollmentGrant = vi.fn().mockResolvedValue({
    id: "grant-reenroll", purpose: "re-enroll", token: "replacement-secret", expires_at: "2026-08-15T12:15:00Z",
    controller_endpoint: "https://controller.example.test:9443",
    enrollment_endpoint: "https://enrollment.example.test:9444",
    ca_fingerprint: "d".repeat(64),
    installer_url: "https://install.vonkforge.ai/dev/spark",
    controller_address: "192.168.1.231",
    service_hostnames: ["controller.example.test", "enrollment.example.test"],
  });
  render(<FleetPage api={api}/>);
  await flush();

  fireEvent.click(screen.getByRole("button", {name: "View Spark Alpha details"}));
  await flush();
  fireEvent.click(screen.getByRole("button", {name: "Re-enroll"}));
  expect(
    screen.getByText(/retires stale local credential pointers automatically/i),
  ).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "Create one-time enrollment command"}));
  await flush();

  expect(api.createReenrollmentGrant).toHaveBeenCalledWith(nodeId, ENROLLMENT_GRANT_TTL_SECONDS);
  expect(document.querySelector(".onboarding-command")).toHaveTextContent(
    "curl -fsSL https://install.vonkforge.ai/dev/spark | VONK_CONTROLLER_ADDRESS=192.168.1.231 sh -s -- --enroll",
  );
});

test("offers unbound re-enrollment after the controller has lost its node rows", async () => {
  const api = control(async () => snapshot([])) as ControlApi;
  api.createReenrollmentGrant = vi.fn().mockResolvedValue({
    id: "grant-recovery", purpose: "re-enroll", token: "recovery-secret", expires_at: "2026-08-15T12:15:00Z",
    controller_endpoint: "https://controller.example.test:9443",
    enrollment_endpoint: "https://enrollment.example.test:9444",
    ca_fingerprint: "e".repeat(64),
    installer_url: "https://install.vonkforge.ai/dev/spark",
    controller_address: null,
    service_hostnames: [],
  });
  render(<FleetPage api={api}/>);
  await flush();

  openFleetControls();
  fireEvent.click(screen.getByRole("button", {name: "Re-enroll Spark"}));
  expect(screen.getByText(/Spark's locally held node identity/i)).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "Create one-time enrollment command"}));
  await flush();

  expect(api.createReenrollmentGrant).toHaveBeenCalledWith(undefined, ENROLLMENT_GRANT_TTL_SECONDS);
  expect(document.querySelector(".onboarding-command")).toHaveTextContent(
    "curl -fsSL https://install.vonkforge.ai/dev/spark | sh -s -- --enroll",
  );
});
