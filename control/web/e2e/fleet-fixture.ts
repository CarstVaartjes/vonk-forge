import type { Page } from "@playwright/test";
import { createHash } from "node:crypto";
import type { components } from "../src/api/generated";
import { stringifyContractJson } from "../src/api/contract-numeric";

/** A complete typed transfer, consumed by the real browser NDJSON reader. */
export async function serveEmptyFleet(page: Page) {
  await serveSnapshot(page, {
    event_cursor: 0,
    generated_at: new Date().toISOString(),
    authority_revision: "a".repeat(64),
    nodes: [],
  });
}

async function serveSnapshot(page: Page, snapshot: components["schemas"]["FleetSnapshot"]) {
  const bytes = Buffer.from(stringifyContractJson(snapshot), "utf8");
  const transfer_id = "00000000-0000-4000-8000-000000000001";
  const records: components["schemas"]["ObservationTransferRecord"][] = [
    { type: "start", transfer_id, resource: "fleet", encoding: "base64-canonical-json-utf8-v1" },
    { type: "chunk", transfer_id, ordinal: 0, data: bytes.toString("base64") },
    {
      type: "complete",
      transfer_id,
      chunks: 1,
      bytes: bytes.byteLength,
      sha256: createHash("sha256").update(bytes).digest("hex"),
    },
  ];
  const body = records.map((record) => stringifyContractJson(record) + "\n").join("");
  await page.route("**/api/fleet", (route) =>
    route.fulfill({ contentType: "application/x-vonk-observation+ndjson", body }),
  );
  // These layout journeys have no subsequent Fleet changes to observe.
  await page.route("**/api/fleet/stream", (route) => route.fulfill({ status: 204 }));
}

const GIB = 1024 ** 3;
const SPARK_IDS = [1, 2, 3].map((n) => `spk_${String(n).padStart(32, "0")}`);

/** A live Spark with the D12 readings; `over` and `sample` change what differs. */
function spark(
  n: number,
  name: string,
  over: Record<string, unknown>,
  sample: Record<string, unknown>,
): components["schemas"]["FleetNode"] {
  const now = new Date().toISOString();
  return {
    id: SPARK_IDS[n - 1],
    display_name: name,
    hostname: name,
    labels: {},
    lifecycle: "managed",
    agent_runtime: {
      architecture: "linux-arm64",
      binary_digest: "a".repeat(64),
      build_digest: `sha256:${"b".repeat(64)}`,
      semantic_version: "4.7.1",
    },
    connection: {
      agent_state: "active",
      certificate_state: "valid",
      online_state: "online",
      offline_reason: null,
      last_seen_at: now,
      last_seen_age_seconds: 2,
    },
    inventory: null,
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
    telemetry: {
      age_seconds: 2,
      freshness: "live",
      sample: {
        id: `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
        node_id: SPARK_IDS[n - 1],
        boot_id: "00000000-0000-4000-8000-0000000000ff",
        observed_at: now,
        received_at: now,
        memory_total_bytes: Math.round(123.2 * GIB),
        memory_available_bytes: Math.round(10.4 * GIB),
        gpu_process_memory_bytes: Math.round(105.9 * GIB),
        system_memory_bytes: Math.round(6.9 * GIB),
        disk_total_bytes: Math.round(3686.4 * GIB),
        disk_free_bytes: Math.round(412.3 * GIB),
        gpu_memory_total_bytes: Math.round(123.2 * GIB),
        gpu_memory_free_bytes: 10 * GIB,
        gpu_utilization_percent: 97,
        gpu_temperature_c: 69,
        gpu_power_watts: 38.04,
        gpu_clock: { graphics_mhz: 2200, sm_mhz: 2200, throttle_reasons: 4 },
        uptime_seconds: 20 * 86400 + 21 * 3600,
        board_temperature_max_c: 61,
        nvme_temperature_c: 58,
        nic_temperature_c: 70,
        fan_rpm_max: 3210,
        ...sample,
      },
    },
    ...over,
  } as components["schemas"]["FleetNode"];
}

const run = (rank: number) => ({
  alias: "coder",
  expected_rank_count: 2,
  group_state: "healthy",
  healthy: true,
  installation_id: "inst-1",
  member_node_ids: [SPARK_IDS[0], SPARK_IDS[1]],
  present_ranks: [0, 1],
  rank,
  rank_age_seconds: 1,
  rank_fresh: true,
  rank_state: "running",
  recipe_id: "recipe-1",
  recipe_revision_id: "rev-1",
  role: "worker",
  route_state: "published",
  run_id: "run-glm",
  run_state: "running",
  title: "GLM 5.3 Flash",
});

type Spec = components["schemas"]["MetricSpec"];

/**
 * Each metric the fixture serves, as the Controller's catalogue describes it, with the latest value of each
 * of its series (by Spark id or alias) and the 24 h total of a metric that has one. Fleet lines, totals and
 * shares are summed from these, as the Controller does.
 */
const [S1, S2] = SPARK_IDS as [string, string, string];
const COUNTED = "Counted by the gateway; a Spark shows the models it serves.";
const METRICS: { spec: Spec; latest: Record<string, number>; total?: Record<string, number> }[] = [
  {
    spec: spec(
      "gpu_utilization",
      "GPU utilisation",
      "percent",
      "node",
      "measured",
      "spark",
      "Share of time the GPU was busy.",
    ),
    latest: { [S1]: 90, [S2]: 88 },
  },
  {
    spec: spec(
      "gpu_temperature",
      "GPU temperature",
      "celsius",
      "node",
      "measured",
      "spark",
      "GPU die temperature.",
    ),
    latest: { [S1]: 69, [S2]: 69 },
  },
  {
    spec: spec(
      "gpu_power",
      "GPU power",
      "watts",
      "node",
      "measured",
      "spark",
      "GPU draw only; the whole Spark's power is not readable on GB10.",
      { fleet: true },
    ),
    latest: { [S1]: 38, [S2]: 39 },
  },
  {
    spec: spec(
      "gpu_energy",
      "GPU energy",
      "watt_hours",
      "node",
      "estimated",
      "spark",
      "GPU only. Estimate: GPU power summed over the samples the Spark reported.",
      { total: true, fleet: true },
    ),
    latest: { [S1]: 38, [S2]: 39 },
    total: { [S1]: 912, [S2]: 936 },
  },
  {
    spec: spec(
      "request_rate",
      "Requests per model",
      "per_second",
      "alias",
      "estimated",
      "gateway",
      COUNTED,
      { total: true, fleet: true },
    ),
    latest: { coder: 60 },
    total: { coder: 1_840_000 },
  },
  {
    spec: spec(
      "output_token_rate",
      "Generated tokens per model",
      "tokens_per_second",
      "alias",
      "estimated",
      "gateway",
      COUNTED,
      { total: true, fleet: true },
    ),
    latest: { coder: 60 },
    total: { coder: 1_840_000 },
  },
  {
    spec: spec(
      "decode_throughput",
      "Decode throughput",
      "tokens_per_second",
      "alias",
      "measured",
      "engine",
      "Tokens generated per second, all requests together. Only engines whose metric names are verified report; others show Not reported.",
      { total: true, fleet: true },
    ),
    latest: { coder: 41.2 },
    total: { coder: 1_200_000 },
  },
  {
    spec: spec(
      "kv_cache_usage",
      "KV cache in use",
      "ratio",
      "alias",
      "measured",
      "engine",
      "Share of the KV cache pool holding live requests. Only engines whose metric names are verified report; others show Not reported.",
    ),
    latest: { coder: 0.42 },
  },
];

function spec(
  metric: string,
  title: string,
  unit: string,
  scope: string,
  basis: string,
  source: string,
  hint: string,
  flags: { total?: boolean; fleet?: boolean } = {},
): Spec {
  return {
    metric,
    title,
    unit,
    scope,
    basis,
    source,
    summary_total: flags.total ?? false,
    fleet_sum: flags.fleet ?? false,
    hint,
  } as Spec;
}

/** The series of one read: every series of the metric, or only the node's, plus the fleet line when it is an unfiltered read. */
function seriesResponse(metric: string, range: string, node: string | null) {
  const entry = METRICS.find((item) => item.spec.metric === metric);
  const end = Math.floor(Date.now() / 1000);
  const base = { metric, range, start: end - 1800, end, step_seconds: 15 };
  if (!entry) {
    // A valid metric the catalogue does not list: the read succeeds with nothing in it.
    return {
      ...base,
      spec: spec(metric, metric, "count", "node", "measured", "spark", ""),
      series: [],
    };
  }
  const { spec: described, latest, total } = entry;
  // A node read sees its own series, or the models it serves; a Spark that is not in the run serves none.
  const served = (key: string) =>
    node === null || key === node || (described.scope === "alias" && [S1, S2].includes(node));
  const keys = Object.keys(latest).filter(served);
  const cap =
    described.unit === "percent" ? 100 : described.unit === "ratio" ? 1 : Number.POSITIVE_INFINITY;
  const sum = (values: Record<string, number>) =>
    keys.reduce((all, key) => all + (values[key] ?? 0), 0);
  const fleetTotal = total ? sum(total) : null;
  const line = (
    labels: Record<string, string>,
    value: number,
    own: number | null,
    share: number | null,
  ) => ({
    labels,
    points: Array.from({ length: 120 }, (_, i) => ({
      timestamp: end - (120 - i) * 15,
      value: Math.min(cap, value * (1 + Math.sin(i / 7) * 0.05)),
    })),
    summary: {
      latest: value,
      average: value,
      peak: Math.min(cap, value * 1.05),
      total: own,
      share,
      coverage: 1,
    },
  });
  const own = keys.map((key) =>
    line(
      described.scope === "alias" ? { alias: key } : { node_id: key },
      latest[key] as number,
      total ? (total[key] as number) : null,
      total && fleetTotal ? (total[key] as number) / fleetTotal : null,
    ),
  );
  // The Controller's sum leads, only on a read of the whole fleet.
  const fleet =
    described.fleet_sum && node === null
      ? [line({}, sum(latest), fleetTotal, fleetTotal === null ? null : 1)]
      : [];
  return { ...base, spec: described, series: [...fleet, ...own] };
}

/**
 * An Overview fleet for layout and accessibility checks: one workload on two Sparks (the D12 memory
 * capture), an idle Spark, a firing alert, gateway series and a few operations.
 */
export async function serveOverviewFleet(page: Page) {
  const nodes = [
    spark(1, "spark-3542", { loaded: [run(0)] }, {}),
    spark(
      2,
      "spark-2297",
      { loaded: [run(1)] },
      { gpu_utilization_percent: 95, gpu_power_watts: 39 },
    ),
    spark(
      3,
      "spark-0007",
      {},
      {
        // Idle: nothing loaded, so almost nothing is held for models.
        memory_available_bytes: Math.round(115.8 * GIB),
        gpu_process_memory_bytes: Math.round(0.3 * GIB),
        system_memory_bytes: Math.round(7.1 * GIB),
        gpu_utilization_percent: 0,
        gpu_power_watts: null,
        uptime_seconds: null,
      },
    ),
  ];
  await serveSnapshot(page, {
    event_cursor: 1,
    generated_at: new Date().toISOString(),
    authority_revision: "a".repeat(64),
    nodes,
    attention: [
      {
        name: "AgentBehind",
        severity: "warning",
        summary: "spark-3542 is one agent release behind (4.7.1).",
        labels: {},
        active_at: new Date().toISOString(),
      },
    ],
  } as components["schemas"]["FleetSnapshot"]);
  await page.route("**/api/metrics/catalogue", (route) =>
    route.fulfill({ json: METRICS.map((entry) => entry.spec) }),
  );
  await page.route("**/api/metrics/series**", (route) => {
    const query = new URL(route.request().url()).searchParams;
    return route.fulfill({
      json: seriesResponse(
        query.get("metric") ?? "",
        query.get("range") ?? "1h",
        query.get("node"),
      ),
    });
  });
  await page.route("**/api/operations**", (route) =>
    route.fulfill({
      json: {
        operations: [
          {
            id: "op-1",
            kind: "recipe.start",
            state: "succeeded",
            attempt: 1,
            created_at: new Date(Date.now() - 300_000).toISOString(),
            node_ids: [SPARK_IDS[0], SPARK_IDS[1]],
          },
        ],
        next_cursor: null,
        total: 1,
      },
    }),
  );
}
