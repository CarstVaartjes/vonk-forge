import type {TelemetryPoint, VisualFleetSnapshot} from "../api/types";
import {fleetStreamReducer, initialFleetStreamState} from "./fleet-stream-state";

function snapshot(cursor: number, cpu = 10): VisualFleetSnapshot {
  return {
    schema_version: 1,
    event_cursor: cursor,
    generated_at: "2026-08-15T12:00:00Z",
    repository_commit: "a".repeat(40),
    nodes: [{
      id: "node-a",
      display_name: "Alpha",
      hostname: "alpha.internal",
      lifecycle: "managed",
      labels: {},
      connection: {agent_state: "active", certificate_state: "valid", online_state: "online", offline_reason: null, last_seen_at: "2026-08-15T11:59:59Z", last_seen_age_seconds: 1},
      inventory: null,
      telemetry: {age_seconds: 1, freshness: "live", sample: sample(cpu)},
      installed: [],
      loaded: [],
      reservations: {disk_bytes: 0, unified_memory_bytes: 0, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 0},
      warnings: [],
    }, {
      id: "node-b",
      display_name: "Beta",
      hostname: "beta.internal",
      lifecycle: "managed",
      labels: {},
      connection: {agent_state: "active", certificate_state: "valid", online_state: "online", offline_reason: null, last_seen_at: "2026-08-15T11:59:59Z", last_seen_age_seconds: 1},
      inventory: null,
      telemetry: null,
      installed: [],
      loaded: [],
      reservations: {disk_bytes: 0, unified_memory_bytes: 0, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 0},
      warnings: [],
    }],
  };
}

function sample(cpu: number): TelemetryPoint {
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

test("orders requested snapshots by committed event cursor", () => {
  // Break caught: a slower poll overwrites a newer streamed snapshot.
  const current = fleetStreamReducer(initialFleetStreamState, {type: "requested-snapshot", snapshot: snapshot(10)});
  const stale = snapshot(8, 8);

  const next = fleetStreamReducer(current, {type: "requested-snapshot", snapshot: stale});

  expect(next).toBe(current);
  expect(next.snapshot?.event_cursor).toBe(10);
});

test("accepts an authoritative cursor-ahead reset that moves backward", () => {
  const current = fleetStreamReducer(initialFleetStreamState, {type: "requested-snapshot", snapshot: snapshot(10)});

  const reset = fleetStreamReducer(current, {type: "reset-snapshot", snapshot: snapshot(3), reason: "cursor-ahead"});

  expect(reset.snapshot?.event_cursor).toBe(3);
  expect(reset.lastResetReason).toBe("cursor-ahead");
});

test("patches one keyed node and ignores stale or duplicate telemetry increments", () => {
  const base = fleetStreamReducer(initialFleetStreamState, {type: "requested-snapshot", snapshot: snapshot(10)});
  const untouched = base.snapshot!.nodes[1];

  const patched = fleetStreamReducer(base, {type: "node-telemetry", cursor: 11, nodeId: "node-a", sample: sample(73), receivedAt: new Date("2026-08-15T12:00:00Z")});
  const duplicate = fleetStreamReducer(patched, {type: "node-telemetry", cursor: 11, nodeId: "node-a", sample: sample(99), receivedAt: new Date("2026-08-15T12:00:01Z")});

  expect(patched.snapshot?.nodes[0].telemetry?.sample.cpu_utilization_percent).toBe(73);
  expect(patched.snapshot?.nodes[1]).toBe(untouched);
  expect(patched.snapshot?.event_cursor).toBe(11);
  expect(duplicate).toBe(patched);
});

test("turns sparse recipe and operation events into coalescible refresh revisions", () => {
  const base = fleetStreamReducer(initialFleetStreamState, {type: "requested-snapshot", snapshot: snapshot(20)});
  const recipe = fleetStreamReducer(base, {type: "projection-refresh", cursor: 21});
  const staleOperation = fleetStreamReducer(recipe, {type: "projection-refresh", cursor: 20});
  const operation = fleetStreamReducer(staleOperation, {type: "projection-refresh", cursor: 22});

  expect(recipe.snapshot?.event_cursor).toBe(21);
  expect(recipe.refreshRevision).toBe(1);
  expect(staleOperation).toBe(recipe);
  expect(operation.snapshot?.event_cursor).toBe(22);
  expect(operation.refreshRevision).toBe(2);
});

test("exposes reconnect and recovery state without discarding the snapshot", () => {
  const ready = fleetStreamReducer(initialFleetStreamState, {type: "requested-snapshot", snapshot: snapshot(2)});
  const reconnecting = fleetStreamReducer(ready, {type: "stream-error"});
  const recovered = fleetStreamReducer(reconnecting, {type: "stream-open"});

  expect(reconnecting.connection).toBe("reconnecting");
  expect(reconnecting.snapshot).toBe(ready.snapshot);
  expect(recovered.connection).toBe("live");
  expect(recovered.error).toBe("");
});
