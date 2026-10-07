import { parseContractJson, materialize, type WireNumber } from "../api/contract-numeric";
import type { VisualFleetNode } from "../api/types";
import {
  formatBytes,
  nodeDisplayName,
  nodeMemory,
  nodeRecipeUpdates,
  nodeSecondaryName,
  nodeStatus,
  offlineReasonLabel,
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
      observed_at: observedAt,
      received_at: observedAt,
      memory_total_bytes: 100,
      memory_available_bytes: memory,
      disk_total_bytes: 100,
      disk_free_bytes: 75,
      gpu_utilization_percent: 20,
      gpu_memory_total_bytes: 100,
      gpu_memory_free_bytes: memory - 10,
    },
  };
}

test.each([
  ["2026-08-15T11:59:54Z", "live"],
  ["2026-08-15T11:59:53.999Z", "delayed"],
  ["2026-08-15T11:59:40Z", "delayed"],
  ["2026-08-15T11:59:39.999Z", "stale"],
] as const)("derives freshness from the current clock at %s", (observedAt, expected) => {
  // Break caught: trusting snapshot age forever leaves a silent node online.
  expect(telemetryFreshnessAt(observedAt, NOW)).toBe(expected);
});

test("offline is independent of telemetry and names its reason", () => {
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
  expect(nodeStatus(offline, NOW)).toEqual({
    status: "offline",
    reasons: [offlineReasonLabel("certificate-expired")],
  });
});

test("an online Spark with old telemetry needs attention and says why, instead of getting its own status", () => {
  const fresh = node({ telemetry: telemetry("2026-08-15T11:59:58Z") });
  expect(nodeStatus(fresh, NOW)).toEqual({ status: "online", reasons: [] });
  for (const observedAt of ["2026-08-15T11:59:45Z", "2026-08-15T11:50:00Z"]) {
    const late = nodeStatus(node({ telemetry: telemetry(observedAt) }), NOW);
    expect(late.status).toBe("needs attention");
    expect(late.reasons).toHaveLength(1);
  }
});

test("a Controller warning on an otherwise live Spark also needs attention", () => {
  const warned = node({
    telemetry: telemetry("2026-08-15T11:59:58Z"),
    warnings: [
      {
        code: "inventory.stale",
        detail: "Disk is nearly full",
        severity: "warning",
      } as VisualFleetNode["warnings"][number],
    ],
  });
  expect(nodeStatus(warned, NOW)).toEqual({
    status: "needs attention",
    reasons: ["Disk is nearly full"],
  });
});

test("a Wi-Fi NAS route warning shows its recommendation as the next step", () => {
  const wifi = node({
    telemetry: telemetry("2026-08-15T11:59:58Z"),
    warnings: [
      {
        code: "network.nas-route-wifi-wired-port-down",
        detail: "Reaches the NAS over Wi-Fi (wlP9s9, 2.402 Gb/s link, shared airtime).",
        severity: "warning",
        recommendation:
          "Wired port enP7s7 has no link; connect it to the NAS network with a cable.",
      },
    ],
  });
  const { status, reasons } = nodeStatus(wifi, NOW);
  expect(status).toBe("needs attention");
  expect(reasons).toEqual([
    "Reaches the NAS over Wi-Fi (wlP9s9, 2.402 Gb/s link, shared airtime). Wired port enP7s7 has no link; connect it to the NAS network with a cable.",
  ]);
});

test("formats absent and invalid metrics as explicitly unreported", () => {
  // Break caught: null telemetry is rendered as zero capacity.
  expect(formatBytes(null)).toBe("Not reported");
  expect(formatBytes(Number.NaN)).toBe("Not reported");
  expect(formatBytes(80 * 1024 ** 3)).toBe("80.0 GiB");
});

test("keeps technical Spark identities out of the primary name", () => {
  const technical = node({
    display_name: "spk_0123456789abcdef0123456789abcdef",
    hostname: "carst-spark-3.internal",
  });
  expect(nodeDisplayName(technical)).toBe("Carst Spark 3");
  expect(nodeSecondaryName(technical)).toBe("carst-spark-3.internal");

  const labeled = node({ ...technical, labels: { name: "mia-lab-west" } });
  expect(nodeDisplayName(labeled)).toBe("Mia Lab West");

  const identityHostname = node({
    ...technical,
    hostname: `${technical.id}.internal`,
    labels: { role: "inference" },
  });
  expect(nodeDisplayName(identityHostname)).toBe("Inference Spark");
  expect(nodeSecondaryName(identityHostname)).toBeNull();
});

describe("recipe update notices", () => {
  const notice = {
    code: "recipe.update_available",
    severity: "info",
    running_revision_id: "a",
    newest_revision_id: "b",
    detail: "Update available: running GLM 1.6.0, newest 1.7.0 (2026-09-28). Reload to apply.",
  } as const;
  const run = {
    run_id: "run-1",
    title: "GLM",
    recipe_update: notice,
  } as unknown as VisualFleetNode["loaded"][number];

  it("is informational: the Spark stays online and the update is listed once per run", () => {
    const value = node({
      loaded: [run, { ...run, rank: 1 } as VisualFleetNode["loaded"][number]],
      warnings: [{ code: "recipe.update_available", detail: notice.detail, severity: "info" }],
    });
    expect(nodeStatus(value, NOW)).toEqual({ status: "online", reasons: [] });
    expect(nodeRecipeUpdates(value)).toEqual([
      { runId: "run-1", title: "GLM", detail: notice.detail },
    ]);
  });

  it("lists nothing for a run on the newest revision", () => {
    expect(
      nodeRecipeUpdates(
        node({ loaded: [{ ...run, recipe_update: null } as VisualFleetNode["loaded"][number]] }),
      ),
    ).toEqual([]);
  });
});

function wireInteger(token: string): WireNumber {
  return materialize(parseContractJson(token)) as WireNumber;
}

test("retains wide memory counters while deriving only a presentation ratio", () => {
  const total = wireInteger("18446744073709551615");
  const free = wireInteger("9223372036854775807");
  const observed = node({
    inventory: {
      host_memory_total_bytes: total,
      host_memory_free_bytes: free,
    } as VisualFleetNode["inventory"],
  });
  expect(nodeMemory(observed)).toEqual({ usedPercent: 50, totalBytes: total });
  expect(formatBytes(total)).toBe("17179869184.0 GiB");
  expect(formatBytes(wireInteger("1" + "0".repeat(400)))).toBe("1" + "0".repeat(400) + " B");
});
