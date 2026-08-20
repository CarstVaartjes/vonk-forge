import {act, render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi, TelemetryPoint, VisualFleetSnapshot} from "../api/types";
import {useFleetStream} from "./use-fleet-stream";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  readonly withCredentials = false;
  readyState = 0;
  closed = false;
  private listeners = new Map<string, Set<EventListener>>();

  constructor(url: string | URL) {
    this.url = String(url);
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener): void {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListener): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.closed = true;
    this.readyState = 2;
  }

  emit(type: string, data: unknown = undefined, lastEventId = ""): void {
    const event = type === "open" || type === "error"
      ? new Event(type)
      : new MessageEvent(type, {data: JSON.stringify(data), lastEventId});
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  listenerCount(): number {
    return Array.from(this.listeners.values()).reduce((count, listeners) => count + listeners.size, 0);
  }
}

function point(cpu: number): TelemetryPoint {
  return {
    id: `sample-${cpu}`,
    node_id: "node-a",
    boot_id: "00000000-0000-0000-0000-000000000001",
    sequence: cpu,
    observed_at: "2026-08-15T11:59:58Z",
    received_at: "2026-08-15T11:59:59Z",
    cpu_utilization_percent: cpu,
    load_average_1m: null,
    memory_total_bytes: null,
    memory_available_bytes: null,
    disk_total_bytes: null,
    disk_free_bytes: null,
    gpu_utilization_percent: null,
    gpu_memory_total_bytes: null,
    gpu_memory_free_bytes: null,
    temperature_c: null,
    power_watts: null,
    network_receive_bytes_per_second: null,
    network_transmit_bytes_per_second: null,
    gap_samples: 0,
    details: {accelerator_name: null, accelerator_performance_state: null},
  };
}

function snapshot(cursor: number, cpu = 10): VisualFleetSnapshot {
  return {
    schema_version: 1,
    event_cursor: cursor,
    generated_at: "2026-08-15T12:00:00Z",
    authority_revision: "a".repeat(64),
    nodes: [{
      id: "node-a",
      display_name: "Alpha",
      hostname: "alpha.internal",
      lifecycle: "managed",
      labels: {},
      connection: {agent_state: "active", certificate_state: "valid", online_state: "online", offline_reason: null, last_seen_at: "2026-08-15T11:59:59Z", last_seen_age_seconds: 1},
      inventory: null,
      telemetry: {age_seconds: 2, freshness: "live", sample: point(cpu)},
      installed: [],
      loaded: [],
      reservations: {disk_bytes: 0, unified_memory_bytes: 0, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 0},
      warnings: [],
    }],
  };
}

function api(visualFleet: ControlApi["visualFleet"]): ControlApi {
  return {visualFleet} as ControlApi;
}

function Probe({control}: {control: ControlApi}) {
  const fleet = useFleetStream(control);
  return <>
    <span data-testid="connection">{fleet.connection}</span>
    <span data-testid="cursor">{fleet.snapshot?.event_cursor ?? "none"}</span>
    <span data-testid="cpu">{fleet.snapshot?.nodes[0]?.telemetry?.sample.cpu_utilization_percent ?? "none"}</span>
    <span data-testid="error">{fleet.error}</span>
    <button type="button" onClick={fleet.retry}>Retry</button>
  </>;
}

async function flush(): Promise<void> {
  await act(async () => { await Promise.resolve(); });
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("uses same-origin EventSource and reconciles increments and backward resets", async () => {
  render(<Probe control={api(async () => snapshot(5))}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  expect(stream.url).toBe("/api/v1/fleet/stream");
  expect(screen.getByTestId("cursor")).toHaveTextContent("5");
  act(() => stream.emit("open"));
  expect(screen.getByTestId("connection")).toHaveTextContent("live");

  act(() => stream.emit("node-telemetry", {schema_version: 1, node_id: "node-a", sample: point(73)}, "6"));
  expect(screen.getByTestId("cpu")).toHaveTextContent("73");
  act(() => stream.emit("node-telemetry", {schema_version: 1, node_id: "node-a", sample: point(99)}, "6"));
  expect(screen.getByTestId("cpu")).toHaveTextContent("73");
  act(() => stream.emit("node-telemetry", {schema_version: 1, node_id: "node-b", sample: point(88)}, "7"));
  expect(screen.getByTestId("cursor")).toHaveTextContent("6");
  act(() => stream.emit("node-telemetry", {schema_version: 1, node_id: "node-a", sample: point(77)}, "7"));
  expect(screen.getByTestId("cpu")).toHaveTextContent("77");

  act(() => stream.emit("fleet-snapshot", {schema_version: 1, reset_reason: "cursor-ahead", snapshot: snapshot(2, 22)}, "2"));
  expect(screen.getByTestId("cursor")).toHaveTextContent("2");
  expect(screen.getByTestId("cpu")).toHaveTextContent("22");
});

test("keeps one native EventSource across browser-managed Last-Event-ID reconnects", async () => {
  render(<Probe control={api(async () => snapshot(5))}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  act(() => {
    stream.emit("open");
    stream.emit("node-telemetry", {schema_version: 1, node_id: "node-a", sample: point(66)}, "6");
    stream.emit("error");
  });
  expect(screen.getByTestId("connection")).toHaveTextContent("reconnecting");

  act(() => {
    stream.emit("open");
    stream.emit("node-telemetry", {schema_version: 1, node_id: "node-a", sample: point(77)}, "7");
  });

  expect(FakeEventSource.instances).toHaveLength(1);
  expect(stream.closed).toBe(false);
  expect(screen.getByTestId("connection")).toHaveTextContent("live");
  expect(screen.getByTestId("cursor")).toHaveTextContent("7");
  expect(screen.getByTestId("cpu")).toHaveTextContent("77");
});

test("coalesces sparse recipe and operation refresh signals", async () => {
  vi.useFakeTimers();
  const visualFleet = vi.fn()
    .mockResolvedValueOnce(snapshot(5))
    .mockResolvedValueOnce(snapshot(7, 70));
  render(<Probe control={api(visualFleet)}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  act(() => {
    stream.emit("recipe-state", {schema_version: 1, projection_refresh_required: true}, "6");
    stream.emit("operation-state", {schema_version: 1, projection_refresh_required: true}, "7");
    vi.advanceTimersByTime(100);
  });
  await flush();

  expect(visualFleet).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("cursor")).toHaveTextContent("7");
  expect(screen.getByTestId("cpu")).toHaveTextContent("70");
});

test("authoritative cursor-ahead reset cancels old sparse retries and starts a new timeline", async () => {
  vi.useFakeTimers();
  const visualFleet = vi.fn()
    .mockResolvedValueOnce(snapshot(10))
    .mockResolvedValueOnce(snapshot(100, 100));
  render(<Probe control={api(visualFleet)}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  act(() => {
    stream.emit("recipe-state", {schema_version: 1, projection_refresh_required: true}, "100");
    stream.emit("fleet-snapshot", {schema_version: 1, reset_reason: "cursor-ahead", snapshot: snapshot(20, 20)}, "20");
    stream.emit("node-telemetry", {schema_version: 1, node_id: "node-a", sample: point(77)}, "21");
    vi.advanceTimersByTime(10_000);
  });
  await flush();

  expect(visualFleet).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("cursor")).toHaveTextContent("21");
  expect(screen.getByTestId("cpu")).toHaveTextContent("77");
});

test("ignores an old-timeline sparse response already in flight across an authoritative reset", async () => {
  vi.useFakeTimers();
  let resolveOldTimeline!: (value: VisualFleetSnapshot) => void;
  const oldTimelineRequest = new Promise<VisualFleetSnapshot>(resolve => { resolveOldTimeline = resolve; });
  const visualFleet = vi.fn()
    .mockResolvedValueOnce(snapshot(10))
    .mockReturnValueOnce(oldTimelineRequest)
    .mockResolvedValueOnce(snapshot(22, 22));
  render(<Probe control={api(visualFleet)}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  act(() => {
    stream.emit("recipe-state", {schema_version: 1, projection_refresh_required: true}, "100");
    vi.advanceTimersByTime(100);
  });
  expect(visualFleet).toHaveBeenCalledTimes(2);

  act(() => {
    stream.emit("fleet-snapshot", {schema_version: 1, reset_reason: "cursor-ahead", snapshot: snapshot(20, 20)}, "20");
    stream.emit("node-telemetry", {schema_version: 1, node_id: "node-a", sample: point(77)}, "21");
    stream.emit("operation-state", {schema_version: 1, projection_refresh_required: true}, "22");
  });
  await act(async () => resolveOldTimeline(snapshot(100, 100)));
  act(() => vi.advanceTimersByTime(100));
  await flush();

  expect(visualFleet).toHaveBeenCalledTimes(3);
  expect(screen.getByTestId("cursor")).toHaveTextContent("22");
  expect(screen.getByTestId("cpu")).toHaveTextContent("22");
  act(() => vi.advanceTimersByTime(10_000));
  await flush();
  expect(visualFleet).toHaveBeenCalledTimes(3);
});

test("retries a failed sparse refresh until the required cursor is reconciled", async () => {
  vi.useFakeTimers();
  const visualFleet = vi.fn()
    .mockResolvedValueOnce(snapshot(5))
    .mockRejectedValueOnce(new Error("temporary projection failure"))
    .mockResolvedValueOnce(snapshot(6, 60));
  render(<Probe control={api(visualFleet)}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  act(() => {
    stream.emit("recipe-state", {schema_version: 1, projection_refresh_required: true}, "6");
    vi.advanceTimersByTime(100);
  });
  await flush();
  expect(visualFleet).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("cursor")).toHaveTextContent("5");

  act(() => vi.advanceTimersByTime(1_000));
  await flush();

  expect(visualFleet).toHaveBeenCalledTimes(3);
  expect(screen.getByTestId("cursor")).toHaveTextContent("6");
  expect(screen.getByTestId("cpu")).toHaveTextContent("60");
});

test("retries when concurrent telemetry makes a sparse REST response stale", async () => {
  vi.useFakeTimers();
  let resolveRefresh!: (value: VisualFleetSnapshot) => void;
  const pendingRefresh = new Promise<VisualFleetSnapshot>(resolve => { resolveRefresh = resolve; });
  const visualFleet = vi.fn()
    .mockResolvedValueOnce(snapshot(5))
    .mockReturnValueOnce(pendingRefresh)
    .mockResolvedValueOnce(snapshot(7, 70));
  render(<Probe control={api(visualFleet)}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  act(() => {
    stream.emit("operation-state", {schema_version: 1, projection_refresh_required: true}, "6");
    vi.advanceTimersByTime(100);
  });
  expect(visualFleet).toHaveBeenCalledTimes(2);
  act(() => stream.emit("node-telemetry", {schema_version: 1, node_id: "node-a", sample: point(77)}, "7"));
  await act(async () => resolveRefresh(snapshot(6, 66)));

  expect(screen.getByTestId("cursor")).toHaveTextContent("7");
  expect(screen.getByTestId("cpu")).toHaveTextContent("77");
  act(() => vi.advanceTimersByTime(1_000));
  await flush();

  expect(visualFleet).toHaveBeenCalledTimes(3);
  expect(screen.getByTestId("cursor")).toHaveTextContent("7");
  expect(screen.getByTestId("cpu")).toHaveTextContent("70");
});

test("polls once per ten seconds while reconnecting and stops on stream recovery", async () => {
  vi.useFakeTimers();
  const visualFleet = vi.fn()
    .mockResolvedValueOnce(snapshot(5))
    .mockResolvedValueOnce(snapshot(6, 60));
  render(<Probe control={api(visualFleet)}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  act(() => stream.emit("error"));
  expect(screen.getByTestId("connection")).toHaveTextContent("reconnecting");
  act(() => vi.advanceTimersByTime(10_000));
  await flush();
  expect(screen.getByTestId("connection")).toHaveTextContent("polling");
  expect(screen.getByTestId("cursor")).toHaveTextContent("6");

  act(() => stream.emit("open"));
  expect(screen.getByTestId("connection")).toHaveTextContent("live");
  act(() => vi.advanceTimersByTime(20_000));
  await flush();
  expect(visualFleet).toHaveBeenCalledTimes(2);
});

test("periodically reconciles server-derived state while the SSE connection stays live", async () => {
  vi.useFakeTimers();
  const visualFleet = vi.fn()
    .mockResolvedValueOnce(snapshot(5))
    .mockResolvedValueOnce(snapshot(6, 60));
  render(<Probe control={api(visualFleet)}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  act(() => stream.emit("open"));
  act(() => vi.advanceTimersByTime(30_000));
  await flush();

  expect(visualFleet).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("cursor")).toHaveTextContent("6");
  expect(screen.getByTestId("connection")).toHaveTextContent("live");
});

test("does not overlap periodic live reconciliation requests", async () => {
  vi.useFakeTimers();
  const pending = new Promise<VisualFleetSnapshot>(() => undefined);
  const visualFleet = vi.fn()
    .mockResolvedValueOnce(snapshot(5))
    .mockReturnValueOnce(pending);
  render(<Probe control={api(visualFleet)}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  act(() => stream.emit("open"));
  act(() => vi.advanceTimersByTime(90_000));

  expect(visualFleet).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("connection")).toHaveTextContent("live");
});

test("uses polling fallback when EventSource is unavailable", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("EventSource", undefined);
  const visualFleet = vi.fn()
    .mockResolvedValueOnce(snapshot(5))
    .mockResolvedValueOnce(snapshot(6));

  render(<Probe control={api(visualFleet)}/>);
  await flush();
  expect(screen.getByTestId("connection")).toHaveTextContent("reconnecting");
  act(() => vi.advanceTimersByTime(10_000));
  await flush();

  expect(screen.getByTestId("connection")).toHaveTextContent("polling");
  expect(visualFleet).toHaveBeenCalledTimes(2);
});

test("does not let an older in-flight poll overwrite a newer stream increment", async () => {
  vi.useFakeTimers();
  let resolvePoll!: (value: VisualFleetSnapshot) => void;
  const pendingPoll = new Promise<VisualFleetSnapshot>(resolve => { resolvePoll = resolve; });
  const visualFleet = vi.fn()
    .mockResolvedValueOnce(snapshot(5))
    .mockReturnValueOnce(pendingPoll);
  render(<Probe control={api(visualFleet)}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  act(() => {
    stream.emit("error");
    vi.advanceTimersByTime(10_000);
  });
  act(() => stream.emit("node-telemetry", {schema_version: 1, node_id: "node-a", sample: point(77)}, "7"));
  await act(async () => resolvePoll(snapshot(6, 66)));

  expect(screen.getByTestId("cursor")).toHaveTextContent("7");
  expect(screen.getByTestId("cpu")).toHaveTextContent("77");
});

test("retries initial errors with a fresh EventSource", async () => {
  const visualFleet = vi.fn()
    .mockRejectedValueOnce(new Error("Control API returned 503"))
    .mockResolvedValueOnce(snapshot(4));
  render(<Probe control={api(visualFleet)}/>);
  await flush();

  expect(screen.getByTestId("error")).toHaveTextContent("Control API returned 503");
  const first = FakeEventSource.instances[0];
  await userEvent.click(screen.getByRole("button", {name: "Retry"}));
  await flush();

  expect(first.closed).toBe(true);
  expect(FakeEventSource.instances).toHaveLength(2);
  expect(screen.getByTestId("cursor")).toHaveTextContent("4");
});

test("cleans EventSource listeners, timers, and in-flight requests on unmount", () => {
  vi.useFakeTimers();
  let signal: AbortSignal | undefined;
  const pending = new Promise<VisualFleetSnapshot>(() => undefined);
  const view = render(<Probe control={api(async candidate => {
    signal = candidate;
    return pending;
  })}/>);
  const stream = FakeEventSource.instances[0];
  act(() => stream.emit("error"));

  view.unmount();

  expect(stream.closed).toBe(true);
  expect(stream.listenerCount()).toBe(0);
  expect(signal?.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});

test("aborts and stops an in-flight periodic live reconciliation on unmount", async () => {
  vi.useFakeTimers();
  let reconciliationSignal: AbortSignal | undefined;
  const pending = new Promise<VisualFleetSnapshot>(() => undefined);
  const visualFleet = vi.fn()
    .mockResolvedValueOnce(snapshot(5))
    .mockImplementationOnce(async candidate => {
      reconciliationSignal = candidate;
      return pending;
    });
  const view = render(<Probe control={api(visualFleet)}/>);
  await flush();
  const stream = FakeEventSource.instances[0];

  act(() => stream.emit("open"));
  act(() => vi.advanceTimersByTime(30_000));
  expect(visualFleet).toHaveBeenCalledTimes(2);

  view.unmount();
  act(() => vi.advanceTimersByTime(90_000));

  expect(reconciliationSignal?.aborted).toBe(true);
  expect(visualFleet).toHaveBeenCalledTimes(2);
  expect(vi.getTimerCount()).toBe(0);
});
