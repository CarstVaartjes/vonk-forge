import type {VisualFleetNode, VisualFleetSnapshot} from "../api/types";
import {
  formatBytes,
  formatMetric,
  installationGroupLabel,
  nodeOperationalState,
  nodeWarningsAt,
  offlineReasonLabel,
  runGroupLabel,
  summarizeFleet,
  telemetryFreshnessAt,
} from "./fleet";

const NOW = new Date("2026-08-15T12:00:00Z");

function node(overrides: Partial<VisualFleetNode> = {}): VisualFleetNode {
  return {
    id: "spk_0123456789abcdef0123456789abcdef",
    display_name: "Spark One",
    hostname: "spark-one.internal",
    lifecycle: "managed",
    labels: {},
    connection: {
      agent_state: "active",
      certificate_state: "valid",
      online_state: "online",
      offline_reason: null,
      last_seen_at: "2026-08-15T11:59:58Z",
      last_seen_age_seconds: 2,
    },
    inventory: null,
    telemetry: null,
    installed: [],
    loaded: [],
    reservations: {
      disk_bytes: 0,
      unified_memory_bytes: 0,
      host_memory_bytes: 0,
      gpu_memory_bytes: 0,
      port_count: 0,
    },
    warnings: [],
    ...overrides,
  };
}

function telemetry(observedAt: string, memory = 80): NonNullable<VisualFleetNode["telemetry"]> {
  return {
    age_seconds: 0,
    freshness: "live",
    sample: {
      id: "00000000-0000-4000-8000-000000000001",
      node_id: "spk_0123456789abcdef0123456789abcdef",
      boot_id: "00000000-0000-0000-0000-000000000001",
      sequence: 1,
      observed_at: observedAt,
      received_at: observedAt,
      cpu_utilization_percent: 10,
      load_average_1m: 1,
      memory_total_bytes: 100,
      memory_available_bytes: memory,
      disk_total_bytes: 100,
      disk_free_bytes: 75,
      gpu_utilization_percent: 20,
      gpu_memory_total_bytes: 100,
      gpu_memory_free_bytes: memory - 10,
      temperature_c: 42,
      power_watts: 18,
      network_receive_bytes_per_second: 1024,
      network_transmit_bytes_per_second: 512,
      gap_samples: 0,
      details: {accelerator_name: "NVIDIA GB10", accelerator_performance_state: "P0"},
    },
  };
}

test.each([
  ["2026-08-15T11:59:54Z", "live"],
  ["2026-08-15T11:59:53.999Z", "delayed"],
  ["2026-08-15T11:59:40Z", "delayed"],
  ["2026-08-15T11:59:39.999Z", "stale"],
] as const)("derives freshness from the current clock at %s", (observedAt, expected) => {
  // Break caught: trusting snapshot age forever leaves a silent node live.
  expect(telemetryFreshnessAt(observedAt, NOW)).toBe(expected);
});

test("keeps agent offline independent from telemetry freshness", () => {
  const offline = node({
    connection: {
      agent_state: "active",
      certificate_state: "expired",
      online_state: "offline",
      offline_reason: "certificate-expired",
      last_seen_at: "2026-08-15T11:59:59Z",
      last_seen_age_seconds: 1,
    },
    telemetry: telemetry("2026-08-15T11:59:59Z"),
  });

  expect(nodeOperationalState(offline, NOW)).toBe("offline");
  expect(offlineReasonLabel("certificate-expired")).toBe("Certificate expired");
  expect(offlineReasonLabel("last-seen-in-future")).toBe("Agent clock is ahead");
  expect(offlineReasonLabel(null)).toBe("Offline reason unavailable");
});

test("formats absent and invalid metrics as explicitly unreported", () => {
  // Break caught: null telemetry is rendered as zero utilization or capacity.
  expect(formatMetric(null, value => `${value}%`)).toBe("Not reported");
  expect(formatMetric(Number.NaN, value => `${value}%`)).toBe("Not reported");
  expect(formatMetric(12.25, value => `${value.toFixed(1)}%`)).toBe("12.3%");
  expect(formatBytes(null)).toBe("Not reported");
  expect(formatBytes(80 * 1024 ** 3)).toBe("80.0 GiB");
});

test("labels installation and running groups from complete group evidence", () => {
  const installed = {
    installation_id: "10000000-0000-4000-8000-000000000001",
    recipe_id: "20000000-0000-4000-8000-000000000001",
    recipe_revision_id: "30000000-0000-4000-8000-000000000001",
    title: "Qwen pair",
    topology_name: "pair",
    expected_rank_count: 2,
    present_ranks: [0],
    member_node_ids: ["node-a"],
    rank: 0,
    role: "leader",
    rank_state: "installed" as const,
    group_state: "partial" as const,
    complete: false,
    degraded_reason: "missing-ranks" as const,
  };
  const loaded = {
    run_id: "40000000-0000-4000-8000-000000000001",
    installation_id: installed.installation_id,
    recipe_id: installed.recipe_id,
    recipe_revision_id: installed.recipe_revision_id,
    title: installed.title,
    alias: "chat",
    expected_rank_count: 2,
    present_ranks: [0, 1],
    member_node_ids: ["node-a", "node-b"],
    rank: 0,
    role: "leader",
    rank_state: "running" as const,
    rank_age_seconds: 2,
    rank_fresh: true,
    run_state: "running" as const,
    route_state: "failed" as const,
    group_state: "degraded" as const,
    healthy: false,
    degraded_reason: "route-not-published" as const,
  };

  expect(installationGroupLabel(installed)).toBe("Partial · 1 of 2 ranks · missing ranks");
  expect(runGroupLabel(loaded)).toBe("Degraded · 2 of 2 ranks · route not published");
  expect(installationGroupLabel({...installed, present_ranks: [0, 1], group_state: "installed", complete: true, degraded_reason: null})).toBe("Complete · 2 of 2 ranks");
  expect(runGroupLabel({...loaded, route_state: "published", group_state: "healthy", healthy: true, degraded_reason: null})).toBe("Healthy · 2 of 2 ranks");
});

test("summarizes live delayed stale and offline nodes without treating null as capacity", () => {
  const completeInstallation: VisualFleetNode["installed"][number] = {
    installation_id: "install-1", recipe_id: "recipe-1", recipe_revision_id: "revision-1", title: "Qwen", topology_name: "pair", expected_rank_count: 1, present_ranks: [0], member_node_ids: ["node-a"], rank: 0, role: "primary", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
  };
  const healthyRun: VisualFleetNode["loaded"][number] = {
    run_id: "run-1", installation_id: "install-1", recipe_id: "recipe-1", recipe_revision_id: "revision-1", title: "Qwen", alias: "chat", expected_rank_count: 1, present_ranks: [0], member_node_ids: ["node-a"], rank: 0, role: "primary", rank_state: "running", rank_age_seconds: 2, rank_fresh: true, run_state: "running", route_state: "published", group_state: "healthy", healthy: true, degraded_reason: null,
  };
  const snapshot: VisualFleetSnapshot = {
    schema_version: 1,
    event_cursor: 8,
    generated_at: NOW.toISOString(),
    repository_commit: "a".repeat(40),
    nodes: [
      node({
        telemetry: telemetry("2026-08-15T11:59:58Z", 80),
        installed: [completeInstallation, {...completeInstallation, installation_id: "install-partial", complete: false, group_state: "partial", degraded_reason: "missing-ranks"}],
        loaded: [healthyRun, {...healthyRun, run_id: "run-degraded", healthy: false, group_state: "degraded", route_state: "failed", degraded_reason: "route-not-published"}],
      }),
      node({id: "node-b", telemetry: telemetry("2026-08-15T11:59:45Z", 60)}),
      node({id: "node-c", telemetry: null}),
      node({id: "node-d", connection: {...node().connection, online_state: "offline", offline_reason: "stale"}, telemetry: telemetry("2026-08-15T11:59:59Z", 90)}),
    ],
  };

  expect(summarizeFleet(snapshot, NOW)).toEqual({
    delayed: 1,
    installedRecipes: 1,
    live: 1,
    loadedRecipes: 1,
    offline: 1,
    stale: 1,
    total: 4,
    unifiedCapacity: "known",
    unifiedAvailableBytes: 70,
    unifiedReportingNodes: 1,
    warnings: 3,
  });
});

test("counts unique active warning conditions without duplicating projected freshness", () => {
  const delayed = node({
    id: "node-delayed",
    telemetry: telemetry("2026-08-15T11:59:45Z"),
    warnings: [
      {code: "telemetry.delayed", detail: "Telemetry delivery is delayed.", severity: "warning"},
      {code: "inventory.stale", detail: "Admission inventory is stale.", severity: "warning"},
    ],
  });
  const offline = node({
    id: "node-offline",
    connection: {...node().connection, online_state: "offline", offline_reason: "stale"},
    telemetry: telemetry("2026-08-15T11:59:30Z"),
    warnings: [
      {code: "node.offline", detail: "The authenticated agent is not currently online.", severity: "warning"},
      {code: "telemetry.stale", detail: "Telemetry is stale.", severity: "warning"},
    ],
  });
  const snapshot: VisualFleetSnapshot = {schema_version: 1, event_cursor: 1, generated_at: NOW.toISOString(), repository_commit: "a".repeat(40), nodes: [delayed, offline]};

  expect(summarizeFleet(snapshot, NOW).warnings).toBe(4);
});

test("marks unified live capacity partial or unknown instead of presenting an unmeasured zero", () => {
  const reporting = node({id: "node-reporting", telemetry: telemetry("2026-08-15T11:59:58Z", 80)});
  const missingGpu = telemetry("2026-08-15T11:59:58Z", 60);
  missingGpu.sample.gpu_memory_free_bytes = null;
  const partialSnapshot: VisualFleetSnapshot = {schema_version: 1, event_cursor: 1, generated_at: NOW.toISOString(), repository_commit: "a".repeat(40), nodes: [reporting, node({id: "node-missing", telemetry: missingGpu})]};
  const unknownSnapshot: VisualFleetSnapshot = {...partialSnapshot, nodes: [node({id: "node-missing", telemetry: missingGpu})]};

  expect(summarizeFleet(partialSnapshot, NOW)).toMatchObject({
    live: 2,
    unifiedAvailableBytes: 70,
    unifiedCapacity: "partial",
    unifiedReportingNodes: 1,
  });
  expect(summarizeFleet(unknownSnapshot, NOW)).toMatchObject({
    live: 1,
    unifiedAvailableBytes: null,
    unifiedCapacity: "unknown",
    unifiedReportingNodes: 0,
  });
});

test("recomputes telemetry warnings from the clock without contradicting non-telemetry warnings", () => {
  const aging = node({
    telemetry: telemetry("2026-08-15T11:59:40Z"),
    warnings: [
      {code: "inventory.stale", detail: "Admission inventory is stale.", severity: "warning"},
      {code: "telemetry.delayed", detail: "Telemetry delivery is delayed.", severity: "warning"},
    ],
  });

  expect(nodeWarningsAt(aging, new Date("2026-08-15T12:00:01Z"))).toEqual([
    {code: "inventory.stale", detail: "Admission inventory is stale.", severity: "warning"},
    {code: "telemetry.stale", detail: "Telemetry is stale.", severity: "warning"},
  ]);
});
