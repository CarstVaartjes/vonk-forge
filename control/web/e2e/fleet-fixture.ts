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
        gpu_clock: { graphics_mhz: 2200, sm_mhz: 2200 },
        uptime_seconds: 20 * 86400 + 21 * 3600,
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
      { memory_available_bytes: 5 * GIB, gpu_power_watts: null, uptime_seconds: null },
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
  await page.route("**/api/metrics/catalogue", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/metrics/series**", (route) => {
    const metric = new URL(route.request().url()).searchParams.get("metric") ?? "";
    const model = metric === "output_token_rate" || metric === "request_rate";
    const unit =
      metric === "output_token_rate"
        ? "tokens_per_second"
        : metric === "request_rate"
          ? "per_second"
          : "percent";
    const end = Math.floor(Date.now() / 1000);
    const points = Array.from({ length: 120 }, (_, i) => ({
      timestamp: end - (120 - i) * 15,
      value: 60 + Math.sin(i / 7) * 3,
    }));
    return route.fulfill({
      json: {
        metric,
        spec: {
          metric,
          title: model ? "Generated tokens per model" : metric,
          unit,
          scope: model ? "alias" : "node",
          basis: model ? "estimated" : "measured",
          source: model ? "gateway" : "spark",
          summary_total: model,
          fleet_sum: false,
          hint: "Counted by the gateway.",
        },
        range: "1h",
        start: end - 1800,
        end,
        step_seconds: 15,
        series: [
          {
            labels: model ? { alias: "coder" } : {},
            points,
            summary: {
              latest: 60,
              average: 60,
              peak: 63,
              total: model ? 1840000 : null,
              share: null,
              coverage: 1,
            },
          },
        ],
      },
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
