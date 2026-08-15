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

test("keeps an outstanding sparse requirement across a backward cursor-ahead reset", () => {
  const current = fleetStreamReducer(initialFleetStreamState, {type: "requested-snapshot", snapshot: snapshot(10)});
  const required = fleetStreamReducer(current, {type: "projection-refresh", cursor: 12});

  const reset = fleetStreamReducer(required, {type: "reset-snapshot", snapshot: snapshot(3), reason: "cursor-ahead"});

  expect(reset.snapshot?.event_cursor).toBe(3);
  expect(reset.requiredRefreshCursor).toBe(12);
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

test("rejects telemetry whose sample identity does not match the event node", () => {
  const base = fleetStreamReducer(initialFleetStreamState, {type: "requested-snapshot", snapshot: snapshot(10)});

  const mismatched = fleetStreamReducer(base, {
    type: "node-telemetry",
    cursor: 11,
    nodeId: "node-b",
    sample: sample(73),
    receivedAt: new Date("2026-08-15T12:00:00Z"),
  });

  expect(mismatched).toBe(base);
  expect(mismatched.snapshot?.event_cursor).toBe(10);
});

test("reconciles only telemetry warnings when a valid sample changes freshness", () => {
  const projected = snapshot(10);
  projected.nodes[0].warnings = [
    {code: "inventory.stale", detail: "Admission inventory is stale.", severity: "warning"},
    {code: "telemetry.missing", detail: "No telemetry sample is available.", severity: "warning"},
  ];
  const base = fleetStreamReducer(initialFleetStreamState, {type: "requested-snapshot", snapshot: projected});

  const delayed = fleetStreamReducer(base, {
    type: "node-telemetry",
    cursor: 11,
    nodeId: "node-a",
    sample: sample(73),
    receivedAt: new Date("2026-08-15T12:00:10Z"),
  });
  const stale = fleetStreamReducer(delayed, {
    type: "node-telemetry",
    cursor: 12,
    nodeId: "node-a",
    sample: sample(74),
    receivedAt: new Date("2026-08-15T12:00:30Z"),
  });

  expect(delayed.snapshot?.nodes[0].warnings).toEqual([
    {code: "inventory.stale", detail: "Admission inventory is stale.", severity: "warning"},
    {code: "telemetry.delayed", detail: "Telemetry delivery is delayed.", severity: "warning"},
  ]);
  expect(stale.snapshot?.nodes[0].warnings).toEqual([
    {code: "inventory.stale", detail: "Admission inventory is stale.", severity: "warning"},
    {code: "telemetry.stale", detail: "Telemetry is stale.", severity: "warning"},
  ]);
});

test("keeps sparse refresh requirements separate from the applied event cursor", () => {
  const base = fleetStreamReducer(initialFleetStreamState, {type: "requested-snapshot", snapshot: snapshot(20)});
  const recipe = fleetStreamReducer(base, {type: "projection-refresh", cursor: 21});
  const staleOperation = fleetStreamReducer(recipe, {type: "projection-refresh", cursor: 20});
  const operation = fleetStreamReducer(staleOperation, {type: "projection-refresh", cursor: 22});

  expect(recipe.snapshot?.event_cursor).toBe(20);
  expect(recipe.requiredRefreshCursor).toBe(21);
  expect(recipe.refreshRevision).toBe(1);
  expect(staleOperation).toBe(recipe);
  expect(operation.snapshot?.event_cursor).toBe(20);
  expect(operation.requiredRefreshCursor).toBe(22);
  expect(operation.refreshRevision).toBe(2);
});

test("only clears a sparse refresh requirement after a qualifying snapshot applies", () => {
  const base = fleetStreamReducer(initialFleetStreamState, {type: "requested-snapshot", snapshot: snapshot(20)});
  const required = fleetStreamReducer(base, {type: "projection-refresh", cursor: 22});
  const insufficient = fleetStreamReducer(required, {type: "requested-snapshot", snapshot: snapshot(21)});
  const telemetryAhead = fleetStreamReducer(insufficient, {
    type: "node-telemetry",
    cursor: 24,
    nodeId: "node-a",
    sample: sample(74),
    receivedAt: new Date("2026-08-15T12:00:00Z"),
  });
  const staleRest = fleetStreamReducer(telemetryAhead, {type: "requested-snapshot", snapshot: snapshot(22, 22)});
  const reconciled = fleetStreamReducer(staleRest, {type: "requested-snapshot", snapshot: snapshot(24, 84)});

  expect(insufficient.snapshot?.event_cursor).toBe(21);
  expect(insufficient.requiredRefreshCursor).toBe(22);
  expect(staleRest).toBe(telemetryAhead);
  expect(staleRest.requiredRefreshCursor).toBe(22);
  expect(reconciled.snapshot?.event_cursor).toBe(24);
  expect(reconciled.snapshot?.nodes[0].telemetry?.sample.cpu_utilization_percent).toBe(84);
  expect(reconciled.requiredRefreshCursor).toBeNull();
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
