import {act, fireEvent, render, screen, within} from "@testing-library/react";
import type {ControlApi, TelemetryPoint, VisualFleetNode, VisualFleetSnapshot} from "../api/types";
import {ENROLLMENT_GRANT_TTL_SECONDS, FleetPage} from "./fleet";

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

function control(visualFleet: ControlApi["visualFleet"], history: ControlApi["nodeTelemetryHistory"] = async (nodeId, start, end, resolution, maximumPoints) => ({schema_version: 1, node_id: nodeId, start, end, resolution, maximum_points: maximumPoints, points: []})): ControlApi {
  return {visualFleet, nodeTelemetryHistory: history} as ControlApi;
}

async function flush(): Promise<void> {
  await act(async () => { await Promise.resolve(); });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
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
  expect(within(summary).getByText("1 installed recipe")).toBeVisible();
  expect(within(summary).getByText("1 loaded recipe")).toBeVisible();
  expect(within(summary).getByText("3 active warnings")).toBeVisible();
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
  expect(within(summary).getByText("Partial · 1 of 2 live nodes reporting")).toBeVisible();

  view.unmount();
  const unknown = node("node-c", "Gamma", "2026-08-15T11:59:58Z");
  unknown.telemetry!.sample.memory_available_bytes = null;
  unknown.telemetry!.sample.gpu_memory_free_bytes = null;
  render(<FleetPage api={control(async () => snapshot([unknown]))}/>);
  await flush();

  summary = screen.getByRole("region", {name: "Fleet summary"});
  expect(within(summary).getByText("Not reported")).toBeVisible();
  expect(within(summary).getByText("No live node reports both host and GPU free memory")).toBeVisible();
  expect(within(summary).queryByText("0 B")).not.toBeInTheDocument();
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

  expect(screen.getByText("73.0%", {selector: "[data-metric='gpu'] dd"})).toBeVisible();
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
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(visualFleet).toHaveBeenCalledTimes(2);
});
test("renders a complete shell-quoted Spark bootstrap command", async () => {
  const visualFleet = vi.fn().mockResolvedValue(snapshot([]));
  const api = control(visualFleet) as ControlApi;
  api.agents = vi.fn().mockResolvedValue({agents: []});
  api.enrollments = vi.fn().mockResolvedValue({enrollments: []});
  api.createEnrollmentGrant = vi.fn().mockResolvedValue({
    id: "grant-1", purpose: "new-node", token: "secret-token", expires_at: "2099-01-01T00:00:00Z",
    controller_endpoint: "https://controller.example.test:9443",
    enrollment_endpoint: "https://enrollment.example.test:9444",
    ca_fingerprint: "a".repeat(64),
  });
  render(<FleetPage api={api}/>);
  await flush();
  fireEvent.click(screen.getByRole("button", {name: "Add Spark"}));
  expect(screen.getByText(/generates its immutable/i)).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "Create one-time enrollment command"}));
  await flush();
  expect(api.createEnrollmentGrant).toHaveBeenCalledWith(ENROLLMENT_GRANT_TTL_SECONDS);
  const command = document.querySelector<HTMLElement>(".onboarding-command")!;
  expect(command).toBeVisible();
  expect(command.textContent).toBe([
    "sudo /usr/lib/vonk-forge/vonk-agent pair " + "\\",
    "  --enrollment 'https://enrollment.example.test:9444' " + "\\",
    "  --ca-sha256 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' " + "\\",
    "  --token-stdin",
  ].join("\n"));
  expect(command).not.toHaveTextContent("--config");
  expect(command).not.toHaveTextContent("--state-root");
  expect(command).not.toHaveTextContent("--ca-path");
});
