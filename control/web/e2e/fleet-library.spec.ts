import AxeBuilder from "@axe-core/playwright";
import {expect, test, type Page, type Route} from "@playwright/test";
import {codeRecipe, fullLibraryDetail, librarySnapshot, minimalLibraryDetail, unlinkedRecipe} from "../src/test-fixtures/library";
import type {components} from "../src/api/generated";

const GIB = 1024 ** 3;
const nodeId = "spk_0123456789abcdef0123456789abcdef";
const borealisId = "spk_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const commit = "a".repeat(40);
const pairedRecipe = fullLibraryDetail.recipe;
const pairedModel = librarySnapshot.models.find(model => model.recipes.some(recipe => recipe.recipe_id === pairedRecipe.recipe_id))!;
const pairedRecipeId = pairedRecipe.recipe_id;
const pairedRecipeTitle = pairedRecipe.title;
const pairedModelKey = `${pairedModel.model.publisher}/${pairedModel.model.slug}@${pairedModel.model.content_sha256}`;

function canonicalRecipeDetail() {
  const detail = structuredClone(fullLibraryDetail);
  const placementNode = {
    artifact_reuse_bytes: 0, disk_free_after_bytes: 240 * GIB, disk_free_bytes: 320 * GIB, disk_required_bytes: 80 * GIB, disk_reserved_bytes: 0,
    endpoint_owner: true, fabric_address: "fabric://node-alpha", fabric_bandwidth_mbps: 25_000, inventory_age_seconds: 1, inventory_observed_at: "2026-09-06T12:00:00Z",
    memory_available_bytes: 100 * GIB, memory_free_after_bytes: 36 * GIB, memory_kind: "unified" as const, memory_required_bytes: 60 * GIB, memory_reserved_bytes: 4 * GIB,
    node_id: "node-alpha", rank: 0, role: "leader", telemetry_age_seconds: 1, telemetry_observed_at: "2026-09-06T12:00:00Z",
  };
  detail.placement = [{
    topology_name: detail.topology.name, node_count: 1, candidate_node_ids: ["node-alpha"],
    recommendations: [{
      eligible: true, group_complete: true, topology_name: detail.topology.name, node_ids: ["node-alpha"], nodes: [placementNode],
      preview_targets: [{kind: "run", input: {installation_id: "installation-chat"}}], load_state: "not_loaded", install_state: "complete", reasons: [],
      installation_ids: ["installation-chat"], mapping_id: null, ranking_scope: "bounded-advisory", recipe_build_id: null,
      recipe_revision_id: detail.recipe.recipe_revision_id, run_ids: [],
      score: {active_run_count: 0, artifact_reuse_bytes: 0, exact_install_complete: true, exact_install_partial: false, maximum_telemetry_age_seconds: 1, minimum_disk_headroom_bytes: 240 * GIB, minimum_memory_headroom_bytes: 36 * GIB},
    }],
    rejected_groups: [], rejected_nodes: [], evaluated_group_count: 1,
    evidence_counts: {builds: 0, mappings: 0, mapping_members: 0, installations: 1, installation_members: 1, runs: 0, run_members: 0, truncated_collections: []},
    limits: {}, reasons: [], rejected_evidence_truncated: false, search_complete: true,
  }];
  return detail;
}
const browserProblems = new WeakMap<Page, string[]>();
type LibraryFixtureState = {
  detailFailuresRemaining: number;
  empty: boolean;
  snapshotFailuresRemaining: number;
};
const libraryFixtures = new WeakMap<Page, LibraryFixtureState>();

async function expectNoSeriousAccessibilityViolations(page: Page) {
  const results = await new AxeBuilder({page}).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations, results.violations.map(value => `${value.id}: ${value.help}`).join("\n")).toEqual([]);
}

async function openFleetControls(page: Page) {
  const controls = page.locator(".fleet-controls-menu");
  if (!(await controls.getAttribute("open"))) await controls.locator("summary").click();
}

function telemetry(observedAt: string, sequence = 4, telemetryNodeId = nodeId): components["schemas"]["TelemetryPoint"] {
  return {
    id: `00000000-0000-4000-8000-${String(sequence).padStart(12, "0")}`,
    node_id: telemetryNodeId,
    boot_id: "00000000-0000-4000-8000-000000000001",
    observed_at: observedAt,
    received_at: observedAt,
    cpu_utilization_percent: 24.5,
    load_average_1m: 1.25,
    memory_total_bytes: 128 * GIB,
    memory_available_bytes: 92 * GIB,
    disk_total_bytes: 500 * GIB,
    disk_free_bytes: 320 * GIB,
    gpu_utilization_percent: 61,
    gpu_memory_total_bytes: 128 * GIB,
    gpu_memory_free_bytes: 84 * GIB,
    temperature_c: 43.5,
    power_watts: 156.6,
    network_receive_bytes_per_second: 2 * 1024 ** 2,
    network_transmit_bytes_per_second: 512 * 1024,
    gap_samples: 0,
    details: {accelerator_name: "NVIDIA GB10", accelerator_performance_state: "P0"},
  };
}

function richTelemetryMetrics(observedAt: string, telemetryNodeId = nodeId): components["schemas"]["TelemetryMetrics"] {
  const series = (key: string, scope: components["schemas"]["TelemetrySeries"]["scope"], value: number | string | boolean | null, unit: string, context: Partial<components["schemas"]["TelemetrySeries"]> = {}): components["schemas"]["TelemetrySeries"] => ({
    key, scope, value, unit, source: "spark.telemetry.fixture", measurement_kind: "measured", observed_at: observedAt, received_at: observedAt,
    freshness: "fresh", freshness_threshold_seconds: 6, support_status: "available", aggregation: "latest", node_id: telemetryNodeId, ...context,
  });
  const capability = (key: string, scope: components["schemas"]["TelemetryCapability"]["scope"], unit: string, supported = true, reason: string | null = null, context: Partial<components["schemas"]["TelemetryCapability"]> = {}): components["schemas"]["TelemetryCapability"] => ({
    key, scope, unit, source: "spark.telemetry.fixture", measurement_kind: "measured", freshness_threshold_seconds: 6, supported, reason, node_id: telemetryNodeId, ...context,
  });
  return {
    schema_version: 2,
    series: [
      series("gpu.utilization_percent", "accelerator", 61, "%", {device_id: "0"}),
      series("gpu.clock_sm_mhz", "accelerator", 1680, "MHz", {device_id: "0"}),
      series("gpu.throttle_active", "accelerator", false, "boolean", {device_id: "0"}),
      series("gpu.power_watts", "accelerator", 68.4, "W", {device_id: "0"}),
      series("gpu.temperature_c", "accelerator", 43.5, "degC", {device_id: "0"}),
      series("gpu.process_memory_bytes", "accelerator", 18.5 * GIB, "bytes", {device_id: "0", process_id: 4021, process_name: "vllm"}),
      series("cpu.temperature_c", "node", 51.2, "degC"),
      series("cpu.utilization_percent", "node", 24.5, "%"),
      series("cpu.load_average_1m", "node", 2.1, "load"),
      series("cpu.power_watts", "node", 88.2, "W"),
      series("memory.available_bytes", "memory", 92 * GIB, "bytes"),
      series("memory.total_bytes", "memory", 128 * GIB, "bytes"),
      series("memory.bandwidth_bytes_per_second", "memory", 312.5 * GIB, "bytes/s"),
      series("storage.read_bytes_per_second", "storage", 74.1 * 1024 ** 2, "bytes/s", {device_id: "nvme0n1"}),
      series("storage.write_bytes_per_second", "storage", 12.7 * 1024 ** 2, "bytes/s", {device_id: "nvme0n1"}),
      series("network.receive_bytes_per_second", "network", 2 * 1024 ** 2, "bytes/s", {interface_name: "eth0"}),
      series("network.transmit_bytes_per_second", "network", 512 * 1024, "bytes/s", {interface_name: "eth0"}),
      series("runtime.decode_tokens_per_second", "runtime", 112.4, "tokens/s", {run_id: "run-chat"}),
      series("runtime.prefill_tokens_per_second", "runtime", 841.7, "tokens/s", {run_id: "run-chat"}),
      series("runtime.kv_cache_usage_percent", "runtime", 42.8, "%", {run_id: "run-chat"}),
      series("runtime.requests_waiting", "runtime", 2, "requests", {run_id: "run-chat"}),
      series("runtime.ttft_p95_ms", "runtime", 184, "ms", {run_id: "run-chat"}),
      series("runtime.e2e_p95_ms", "runtime", 921, "ms", {run_id: "run-chat"}),
      series("runtime.itl_p95_ms", "runtime", 24.2, "ms", {run_id: "run-chat"}),
      series("runtime.prefix_cache_hit_percent", "runtime", 91, "%", {run_id: "run-chat"}),
      series("runtime.mtp_acceptance_percent", "runtime", 78, "%", {run_id: "run-chat"}),
      series("runtime.preemptions_total", "runtime", 0, "count", {run_id: "run-chat"}),
    ],
    capabilities: [
      capability("gpu.utilization_percent", "accelerator", "%", true, null, {device_id: "0"}),
      capability("gpu.clock_sm_mhz", "accelerator", "MHz", true, null, {device_id: "0"}),
      capability("gpu.throttle_active", "accelerator", "boolean", true, null, {device_id: "0"}),
      capability("gpu.power_watts", "accelerator", "W", true, null, {device_id: "0"}),
      capability("gpu.temperature_c", "accelerator", "degC", true, null, {device_id: "0"}),
      capability("gpu.process_memory_bytes", "accelerator", "bytes", true, null, {device_id: "0", process_id: 4021, process_name: "vllm"}),
      capability("cpu.temperature_c", "node", "degC"),
      capability("cpu.utilization_percent", "node", "%"),
      capability("cpu.load_average_1m", "node", "load"),
      capability("cpu.power_watts", "node", "W"),
      capability("memory.available_bytes", "memory", "bytes"),
      capability("memory.total_bytes", "memory", "bytes"),
      capability("memory.bandwidth_bytes_per_second", "memory", "bytes/s"),
      capability("storage.read_bytes_per_second", "storage", "bytes/s", true, null, {device_id: "nvme0n1"}),
      capability("storage.write_bytes_per_second", "storage", "bytes/s", true, null, {device_id: "nvme0n1"}),
      capability("network.receive_bytes_per_second", "network", "bytes/s", true, null, {interface_name: "eth0"}),
      capability("network.transmit_bytes_per_second", "network", "bytes/s", true, null, {interface_name: "eth0"}),
      capability("runtime.decode_tokens_per_second", "runtime", "tokens/s", true, null, {run_id: "run-chat"}),
      capability("runtime.prefill_tokens_per_second", "runtime", "tokens/s", true, null, {run_id: "run-chat"}),
      capability("runtime.kv_cache_usage_percent", "runtime", "%", true, null, {run_id: "run-chat"}),
      capability("runtime.requests_waiting", "runtime", "requests", true, null, {run_id: "run-chat"}),
      capability("runtime.ttft_p95_ms", "runtime", "ms", true, null, {run_id: "run-chat"}),
      capability("runtime.e2e_p95_ms", "runtime", "ms", true, null, {run_id: "run-chat"}),
      capability("runtime.itl_p95_ms", "runtime", "ms", true, null, {run_id: "run-chat"}),
      capability("runtime.prefix_cache_hit_percent", "runtime", "%", true, null, {run_id: "run-chat"}),
      capability("runtime.mtp_acceptance_percent", "runtime", "%", true, null, {run_id: "run-chat"}),
      capability("runtime.preemptions_total", "runtime", "count", true, null, {run_id: "run-chat"}),
      capability("gpu.power_limit_watts", "accelerator", "W", false, "This Spark does not expose configured power limits.", {device_id: "0"}),
    ],
    runtimes: [{run_id: "run-chat", engine_id: "engine-qwen", backend: "vllm", version: "0.8.5", endpoint: "http://aurora.fixture.invalid:8000", model: "Qwen 3", model_version: "qwen/3", recipe_revision: "revision-chat", context_limit_tokens: 32768, serving_node_ids: [nodeId, borealisId], ranks: [0, 1], readiness: "running", error: null, adapter: "openai-chat", adapter_version: "2", adapter_supported: true, adapter_reason: null}],
    workloads: [{run_id: "run-chat", request_id: "req-42", job_id: null, model: "Qwen 3", recipe_revision: "revision-chat", engine_id: "engine-qwen", state: "running", origin_node_id: nodeId, executor_node_ids: [nodeId, borealisId], created_at: observedAt, started_at: observedAt, ended_at: null, elapsed_seconds: 4.2, failure: null, title: "Interactive request", progress_value: null, progress_max: null, eta_seconds: null, eta_source: null}],
    provenance: {collector: "vonk-controller", collector_version: "fixture-2", host_uptime_seconds: 98231, source_observed_at: observedAt},
  };
}

function richTelemetry(observedAt: string, sequence = 5, telemetryNodeId = nodeId): components["schemas"]["TelemetryPoint"] {
  return {...telemetry(observedAt, sequence, telemetryNodeId), metrics: richTelemetryMetrics(observedAt, telemetryNodeId)};
}

type HistoryMetricValues = {
  gpu: number;
  gpuTemperature: number;
  gpuPower: number;
  cpuTemperature: number;
  cpuUtilization: number;
  cpuPower: number;
  memoryAvailable: number;
  memoryBandwidth: number;
  storageRead: number;
  storageWrite: number;
  networkReceive: number;
  networkTransmit: number;
  decode: number;
  prefill: number;
  queue: number;
  ttft: number;
  e2e: number;
  itl: number;
};

function setRichHistoryValue(point: components["schemas"]["TelemetryPoint"], key: string, value: number, context: Partial<components["schemas"]["TelemetrySeries"]> = {}) {
  const series = point.metrics?.series.find(item => item.key === key && item.device_id === (context.device_id ?? item.device_id) && item.interface_name === (context.interface_name ?? item.interface_name) && item.run_id === (context.run_id ?? item.run_id));
  if (!series) throw new Error(`Fixture metric ${key} is missing`);
  series.value = value;
}

function richHistoryPoint(observedAt: string, sequence: number, values: HistoryMetricValues, telemetryNodeId = nodeId): components["schemas"]["TelemetryPoint"] {
  const point = richTelemetry(observedAt, sequence, telemetryNodeId);
  setRichHistoryValue(point, "gpu.utilization_percent", values.gpu, {device_id: "0"});
  setRichHistoryValue(point, "gpu.temperature_c", values.gpuTemperature, {device_id: "0"});
  setRichHistoryValue(point, "gpu.power_watts", values.gpuPower, {device_id: "0"});
  setRichHistoryValue(point, "cpu.temperature_c", values.cpuTemperature);
  setRichHistoryValue(point, "cpu.utilization_percent", values.cpuUtilization);
  setRichHistoryValue(point, "cpu.power_watts", values.cpuPower);
  setRichHistoryValue(point, "memory.available_bytes", values.memoryAvailable);
  setRichHistoryValue(point, "memory.bandwidth_bytes_per_second", values.memoryBandwidth * GIB);
  setRichHistoryValue(point, "storage.read_bytes_per_second", values.storageRead * 1024 ** 2, {device_id: "nvme0n1"});
  setRichHistoryValue(point, "storage.write_bytes_per_second", values.storageWrite * 1024 ** 2, {device_id: "nvme0n1"});
  setRichHistoryValue(point, "network.receive_bytes_per_second", values.networkReceive, {interface_name: "eth0"});
  setRichHistoryValue(point, "network.transmit_bytes_per_second", values.networkTransmit, {interface_name: "eth0"});
  setRichHistoryValue(point, "runtime.decode_tokens_per_second", values.decode, {run_id: "run-chat"});
  setRichHistoryValue(point, "runtime.prefill_tokens_per_second", values.prefill, {run_id: "run-chat"});
  setRichHistoryValue(point, "runtime.requests_waiting", values.queue, {run_id: "run-chat"});
  setRichHistoryValue(point, "runtime.ttft_p95_ms", values.ttft, {run_id: "run-chat"});
  setRichHistoryValue(point, "runtime.e2e_p95_ms", values.e2e, {run_id: "run-chat"});
  setRichHistoryValue(point, "runtime.itl_p95_ms", values.itl, {run_id: "run-chat"});
  point.gpu_utilization_percent = values.gpu;
  point.temperature_c = values.gpuTemperature;
  point.power_watts = values.gpuPower + values.cpuPower;
  point.cpu_utilization_percent = values.cpuUtilization;
  point.memory_available_bytes = values.memoryAvailable;
  point.network_receive_bytes_per_second = values.networkReceive;
  point.network_transmit_bytes_per_second = values.networkTransmit;
  return point;
}

function richHistoryRollup(first: components["schemas"]["TelemetryPoint"], last: components["schemas"]["TelemetryPoint"], start: string, end: string, resolution: string): components["schemas"]["TelemetryRollupPoint"] {
  const metrics = Object.fromEntries((first.metrics?.series ?? []).flatMap(series => {
    const counterpart = last.metrics?.series.find(item => item.key === series.key && item.scope === series.scope && item.unit === series.unit && item.device_id === series.device_id && item.process_id === series.process_id && item.interface_name === series.interface_name && item.run_id === series.run_id);
    const values = [series.value, counterpart?.value].filter((value): value is number => typeof value === "number" && Number.isFinite(value));
    if (values.length === 0) return [];
    const minimum = Math.min(...values);
    const maximum = Math.max(...values);
    return [[`${series.key}:${series.device_id ?? series.interface_name ?? series.run_id ?? "node"}`, {
      count: values.length, minimum, mean: values.reduce((total, value) => total + value, 0) / values.length, maximum,
      key: series.key, scope: series.scope, device_id: series.device_id, process_id: series.process_id, process_name: series.process_name,
      interface_name: series.interface_name, run_id: series.run_id, unit: series.unit, source: series.source, measurement_kind: series.measurement_kind, aggregation: "mean",
    } satisfies components["schemas"]["TelemetryMetricSummary"]]];
  }));
  return {node_id: first.node_id, resolution: resolution === "fifteen-minute" ? "fifteen-minute" : "minute", bucket_start: start, bucket_end: end, source_sample_count: first === last ? 1 : 2, gap_samples: 0, metrics};
}

function localSnapshot(): components["schemas"]["FleetSnapshot"] {
  const observedAt = new Date().toISOString();
  return {
    schema_version: 1,
    event_cursor: 12,
    generated_at: observedAt,
    authority_revision: commit,
    nodes: [{
      id: nodeId,
      display_name: nodeId,
      hostname: "aurora.fixture.invalid",
      ip_address: "192.168.1.211",
      lifecycle: "managed",
      labels: {role: "inference"},
      connection: {agent_state: "active", certificate_state: "valid", online_state: "online", offline_reason: null, last_seen_at: observedAt, last_seen_age_seconds: 0},
      inventory: null,
      telemetry: {age_seconds: 0, freshness: "live", sample: richTelemetry(observedAt, 5)},
      installed: [{
        installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", topology_name: "pair", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, borealisId], rank: 0, role: "leader", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
      }],
      loaded: [{
        run_id: "run-chat", installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", alias: "chat", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, borealisId], rank: 0, role: "leader", rank_state: "running", rank_age_seconds: 1, rank_fresh: true, run_state: "running", route_state: "failed", group_state: "degraded", healthy: false, degraded_reason: "route-not-published",
      }, {
        run_id: "run-aurora", installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Aurora solo", alias: "fast-chat", expected_rank_count: 1, present_ranks: [0], member_node_ids: [nodeId], rank: 0, role: "primary", rank_state: "running", rank_age_seconds: 1, rank_fresh: true, run_state: "running", route_state: "published", group_state: "healthy", healthy: true, degraded_reason: null,
      }],
      reservations: {disk_bytes: 2 * GIB, unified_memory_bytes: 4 * GIB, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 1},
      warnings: [{code: "run.degraded", detail: "The Qwen pair route is not published.", severity: "warning"}],
    }, {
      id: borealisId,
      display_name: borealisId,
      hostname: "borealis.fixture.invalid",
      ip_address: "192.168.1.212",
      lifecycle: "managed",
      labels: {role: "inference"},
      connection: {agent_state: "retired", certificate_state: "expired", online_state: "offline", offline_reason: "certificate-expired", last_seen_at: null, last_seen_age_seconds: null},
      inventory: null,
      telemetry: null,
      installed: [{
        installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", topology_name: "pair", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, borealisId], rank: 1, role: "worker", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
      }],
      loaded: [{
        run_id: "run-chat", installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", alias: "chat", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, borealisId], rank: 1, role: "worker", rank_state: "lost", rank_age_seconds: 0, rank_fresh: false, run_state: "lost", route_state: "failed", group_state: "degraded", healthy: false, degraded_reason: "rank-not-running",
      }],
      reservations: {disk_bytes: 0, unified_memory_bytes: 0, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 0},
      warnings: [{code: "node.offline", detail: "Certificate renewal is required.", severity: "error"}, {code: "telemetry.missing", detail: "No telemetry sample is available.", severity: "warning"}, {code: "run.degraded", detail: "The Qwen pair has an unhealthy member rank.", severity: "warning"}],
    }],
  };
}

async function installLocalFleetFixture(page: Page) {
  const snapshot = localSnapshot();
  const libraryState: LibraryFixtureState = {detailFailuresRemaining: 0, empty: false, snapshotFailuresRemaining: 0};
  libraryFixtures.set(page, libraryState);
  await page.route("**/api/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
  await page.route("**/api/artifact-jobs/capabilities", route => route.fulfill({json: {schema_version: 1, transport: {max_input_files: 32, max_input_file_bytes: 512 * 1024 ** 2, max_input_total_bytes: 1024 ** 3, max_output_files: 32, max_output_file_bytes: 1024 ** 3, max_output_total_bytes: 2 * 1024 ** 3, max_timeout_seconds: 3600, reserved_input_names: ["manifest.json"]}, storage: {max_stored_bytes: 4 * 1024 ** 3, used_bytes: 0, remaining_bytes: 4 * 1024 ** 3}}}));
  await page.route("**/api/fleet/stream", route => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    headers: {"Cache-Control": "no-cache"},
    body: `retry: 60000\nid: ${snapshot.event_cursor}\nevent: fleet-snapshot\ndata: ${JSON.stringify({schema_version: 1, reset_reason: "initial", snapshot})}\n\n`,
  }));
  await page.route("**/api/fleet", route => route.fulfill({json: snapshot}));
  await page.route("**/api/nodes/*/profile", async route => {
    const nodeId = route.request().url().split("/").at(-2) ?? "";
    const input = await route.request().postDataJSON() as {display_name: string};
    const node = snapshot.nodes.find(item => item.id === nodeId);
    return route.fulfill({json: {
      id: nodeId,
      display_name: input.display_name,
      hostname: node?.hostname ?? "",
      ip_address: node?.ip_address ?? null,
    }});
  });
  const cacheStorage = {schema_version: 2, total_bytes: 1_000, free_bytes: 700, reserve_bytes: 100, available_bytes: 600, unique_used_bytes: 300, in_flight_bytes: 0, protected_bytes: 100, reclaimable_bytes: 200};
  const emptyCacheInventory = {schema_version: 2, source_policy: "nas-first", entries: [], storage: cacheStorage, total: 0, next_cursor: null};
  const emptyRecipeAvailability: components["schemas"]["RecipeImageAvailabilityListResponse"] = {schema_version: 2, total: 0, operations: [], next_cursor: null};
  await page.route("**/api/model-cache", route => route.fulfill({json: emptyCacheInventory}));
  await page.route("**/api/model-cache?*", route => route.fulfill({json: emptyCacheInventory}));
  const emptyModelUpdates = {schema_version: 2, source_policy: "nas-first", total: 0, updates: [], next_cursor: null};
  await page.route("**/api/model-cache/updates", route => route.fulfill({json: emptyModelUpdates}));
  await page.route("**/api/model-cache/updates?*", route => route.fulfill({json: emptyModelUpdates}));
  await page.route("**/api/library/recipe-image-availability", route => route.fulfill({json: emptyRecipeAvailability}));
  await page.route("**/api/library/recipe-image-availability?*", route => route.fulfill({json: emptyRecipeAvailability}));
  const librarySnapshotRoute = (route: Route) => {
    if (libraryState.snapshotFailuresRemaining > 0) {
      libraryState.snapshotFailuresRemaining -= 1;
      return route.fulfill({status: 200, contentType: "application/json", body: "{"});
    }
    const body = libraryState.empty ? {...librarySnapshot, models: [], unlinked_recipes: []} : librarySnapshot;
    return route.fulfill({json: body});
  };
  await page.route("**/api/library", librarySnapshotRoute);
  await page.route("**/api/library?*", librarySnapshotRoute);
  await page.route("**/api/library/recipes/recipe-chat", route => {
    if (libraryState.detailFailuresRemaining > 0) {
      libraryState.detailFailuresRemaining -= 1;
      return route.fulfill({status: 200, contentType: "application/json", body: "{"});
    }
    return route.fulfill({json: fullLibraryDetail});
  });
  await page.route(`**/api/library/recipes/${pairedRecipeId}`, route => {
    if (libraryState.detailFailuresRemaining > 0) {
      libraryState.detailFailuresRemaining -= 1;
      return route.fulfill({status: 200, contentType: "application/json", body: "{"});
    }
    return route.fulfill({json: canonicalRecipeDetail()});
  });
  await page.route("**/api/library/recipes/recipe-code", route => route.fulfill({json: {
    ...fullLibraryDetail,
    recipe: {...fullLibraryDetail.recipe, recipe_id: codeRecipe.recipe_id, slug: codeRecipe.slug, title: codeRecipe.title, description: codeRecipe.description},
  }}));
  await page.route("**/api/library/recipes/recipe-unlinked", route => route.fulfill({json: {
    ...minimalLibraryDetail,
    recipe: {
      recipe_id: unlinkedRecipe.recipe_id,
      slug: unlinkedRecipe.slug,
      title: unlinkedRecipe.title,
      description: unlinkedRecipe.description,
      source_kind: unlinkedRecipe.source_kind,
    },
  }}));
  await page.route("**/api/library/recipes/*", route => {
    const recipeId = new URL(route.request().url()).pathname.split("/").at(-1);
    if (recipeId !== pairedRecipeId) return route.fallback();
    if (libraryState.detailFailuresRemaining > 0) {
      libraryState.detailFailuresRemaining -= 1;
      return route.fulfill({status: 200, contentType: "application/json", body: "{"});
    }
    return route.fulfill({json: canonicalRecipeDetail()});
  });
  await page.route("**/api/jobs/job-load*", route => route.fulfill({json: {
    id: "job-load", kind: "run", state: "failed", authority_revision: commit, current_attempt: 1,
    operation_total: 2, operations: [], progress: {completed: 1, failed: 1, running: 0, total: 2},
    target_total: 2, targets: ["node-alpha", "node-beta"],
  }}));
  await page.route("**/api/nodes/*/telemetry/current", route => {
    const url = new URL(route.request().url());
    const requestedNodeId = url.pathname.split("/").at(-3) ?? nodeId;
    const observedAt = new Date().toISOString();
    return route.fulfill({json: {schema_version: 2, node_id: requestedNodeId, observed_at: observedAt, received_at: observedAt, freshness: "live", sample: richTelemetry(observedAt, 5, requestedNodeId)}});
  });
  await page.route("**/api/nodes/*/telemetry/capabilities", route => {
    const url = new URL(route.request().url());
    const requestedNodeId = url.pathname.split("/").at(-3) ?? nodeId;
    const observedAt = new Date().toISOString();
    return route.fulfill({json: {schema_version: 2, node_id: requestedNodeId, observed_at: observedAt, received_at: observedAt, freshness: "live", capabilities: richTelemetryMetrics(observedAt, requestedNodeId).capabilities}});
  });
  await page.route("**/api/nodes/*/telemetry/workloads", route => {
    const url = new URL(route.request().url());
    const requestedNodeId = url.pathname.split("/").at(-3) ?? nodeId;
    const observedAt = new Date().toISOString();
    const metrics = richTelemetryMetrics(observedAt, requestedNodeId);
    return route.fulfill({json: {schema_version: 2, node_id: requestedNodeId, observed_at: observedAt, received_at: observedAt, freshness: "live", run_id: null, state: null, runtimes: metrics.runtimes, workloads: metrics.workloads}});
  });
  await page.route("**/api/nodes/*/telemetry?*", route => {
    const url = new URL(route.request().url());
    const start = url.searchParams.get("start") ?? snapshot.generated_at;
    const end = url.searchParams.get("end") ?? snapshot.generated_at;
    const resolution = url.searchParams.get("resolution") ?? "raw";
    const maximumPoints = Number(url.searchParams.get("maximum_points") ?? 360);
    const first = richHistoryPoint(start, 1, {
      gpu: 42, gpuTemperature: 48, gpuPower: 60, cpuTemperature: 50, cpuUtilization: 18, cpuPower: 80,
      memoryAvailable: 100 * GIB, memoryBandwidth: 290, storageRead: 64, storageWrite: 10,
      networkReceive: 1.5 * 1024 ** 2, networkTransmit: 400 * 1024, decode: 96, prefill: 700,
      queue: 1, ttft: 220, e2e: 950, itl: 22,
    });
    const last = richHistoryPoint(end, 2, {
      gpu: 72, gpuTemperature: 55, gpuPower: 68.4, cpuTemperature: 51.2, cpuUtilization: 32, cpuPower: 88.2,
      memoryAvailable: 92 * GIB, memoryBandwidth: 312.5, storageRead: 74.1, storageWrite: 12.7,
      networkReceive: 2 * 1024 ** 2, networkTransmit: 512 * 1024, decode: 112.4, prefill: 841.7,
      queue: 2, ttft: 184, e2e: 921, itl: 24.2,
    });
    const bucketSeconds = resolution === "fifteen-minute" ? 15 * 60 : 60;
    const firstBucketEnd = new Date(new Date(start).getTime() + bucketSeconds * 1_000).toISOString();
    const lastBucketStart = new Date(new Date(end).getTime() - bucketSeconds * 1_000).toISOString();
    const points = resolution === "raw"
      ? [first, last]
      : [richHistoryRollup(first, first, start, firstBucketEnd, resolution), richHistoryRollup(last, last, lastBucketStart, end, resolution)];
    return route.fulfill({json: {schema_version: 1, node_id: nodeId, start, end, resolution, maximum_points: maximumPoints, points, metadata: {requested_start: start, requested_end: end, actual_start: start, actual_end: end, requested_resolution: resolution, actual_resolution: resolution, timezone: "UTC", point_count: points.length, coverage_seconds: 3600, gap_samples: 0, downsampled: resolution !== "raw"}}});
  });
}

test.beforeEach(async ({page}) => {
  const problems: string[] = [];
  browserProblems.set(page, problems);
  page.on("console", message => {
    if (["error", "warning"].includes(message.type())) problems.push(`${message.type()}: ${message.text()}`);
  });
  page.on("pageerror", error => problems.push(`pageerror: ${error.message}`));
  await installLocalFleetFixture(page);
});

test.afterEach(async ({page}) => {
  expect(browserProblems.get(page)).toEqual([]);
});

test("Fleet Detailed view and bounded history are keyboard-accessible with local evidence", async ({page}, testInfo) => {
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto("/fleet");

  await expect(page.getByRole("heading", {name: "Fleet", exact: true})).toBeVisible();
  const fleetSummary = page.getByRole("region", {name: "Fleet summary"});
  await expect(fleetSummary.getByText("Live", {exact: true})).toBeVisible();
  await expect(fleetSummary.getByText("Offline", {exact: true})).toBeVisible();
  await expect(page.getByRole("heading", {name: "Spark roster"})).toBeVisible();
  await expect(page.getByRole("article", {name: /Aurora —/})).toBeVisible();
  const aurora = page.getByRole("article", {name: /Aurora — (Live|Delayed)/});
  await expect(aurora).toContainText("NVIDIA GB10 · P0");
  await expect(aurora.getByRole("img", {name: "GPU 24h trend"})).toBeVisible();
  await expect(aurora.locator(".node-workload-summary")).toContainText("Aurora solo");
  await expect(aurora).toContainText("The Qwen pair route is not published.");
  const borealis = page.getByRole("article", {name: "Borealis — Offline"});
  await expect(borealis).toContainText("Certificate expired");
  await expect(borealis.locator(".node-workload-summary")).toContainText("Qwen pair");
  await expect(borealis.getByRole("list", {name: /The Qwen pair has an unhealthy member rank/})).toBeVisible();
  await page.screenshot({path: testInfo.outputPath("fleet-detailed-desktop.png"), fullPage: true});

  const detailButton = aurora.getByRole("button", {name: "View Aurora details"});
  await detailButton.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("complementary", {name: "Aurora details"})).toBeVisible();
  await expect(page.getByRole("button", {name: "Close Aurora details"})).toBeFocused();
  await expect(page.getByRole("img", {name: "Aurora GPU utilization history"})).toHaveAccessibleDescription(/2 reported buckets/);
  await expectNoSeriousAccessibilityViolations(page);
  await page.getByRole("button", {name: "24 hours"}).click();
  await expect(page.getByRole("button", {name: "24 hours"})).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("link", {name: "Download to NAS"}).click();
  await expect(page).toHaveURL(new RegExp(`/library\\?spark=${nodeId}$`));
  await expect(page.getByRole("complementary", {name: "Managing models on Aurora"})).toBeVisible();
});

test("Fleet cards default to 24h trends and expose editable friendly identity", async ({page}) => {
  await page.goto("/fleet");

  await openFleetControls(page);
  const range = page.getByRole("combobox", {name: "Card trend range"});
  await expect(range).toHaveValue("24h");
  await expect(range.getByRole("option")).toHaveText(["1h", "24h", "7d", "31d"]);
  await page.getByRole("button", {name: "Close Fleet controls"}).click();

  await page.getByRole("button", {name: "Edit Aurora"}).click();
  const dialog = page.getByRole("dialog", {name: "Name this Spark"});
  await expect(dialog.getByText("aurora.fixture.invalid")).toBeVisible();
  await expect(dialog.getByText("192.168.1.211")).toBeVisible();
  await expect(dialog.getByText(nodeId)).toBeVisible();
  await dialog.getByRole("textbox", {name: "Friendly name"}).fill("Studio Spark");
  await dialog.getByRole("button", {name: "Save friendly name"}).click();
  await expect(page.getByRole("article", {name: /Studio Spark —/})).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("Fleet discovery searches friendly names and combines actionable health filters", async ({page}) => {
  await page.goto("/fleet");
  const search = page.getByRole("searchbox", {name: "Find a Spark"});
  await expect(search).toBeVisible();
  await expect(page.getByRole("group", {name: "Filter Fleet by health"})).toBeVisible();
  await openFleetControls(page);
  await expect(page.getByRole("button", {name: "Detailed"})).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", {name: "Close Fleet controls"}).click();

  await search.fill("Borealis");
  await expect(page.getByRole("article", {name: "Borealis — Offline"})).toBeVisible();
  await expect(page.getByRole("article", {name: /Aurora —/})).toHaveCount(0);
  await expect(page.getByRole("status").filter({hasText: "Showing 1 of 2 Sparks"})).toBeVisible();
  await page.getByRole("button", {name: "Clear filters"}).click();

  await page.getByRole("button", {name: "Show offline nodes"}).click();
  await expect(page.getByRole("checkbox", {name: "Offline 1"})).toBeChecked();
  await page.getByRole("checkbox", {name: /Live 1/}).check();
  await expect(page.getByRole("status").filter({hasText: "Showing 2 of 2 Sparks"})).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("Node history and lifecycle controls work on desktop and mobile", async ({page}, testInfo) => {
  for (const width of [1280, 360]) {
    await page.setViewportSize({width, height: width === 360 ? 800 : 900});
    await page.goto("/fleet");
    await page.getByRole("button", {name: "View Aurora details"}).click();
    await expect(page.getByRole("button", {name: "Close Aurora details"})).toBeFocused();
    const detail = page.getByRole("complementary", {name: "Aurora details"});
    await expect(detail.getByRole("link", {name: "Download to NAS"})).toBeVisible();
    await expect(detail.getByRole("button", {name: "Stop Aurora solo on this Spark"})).toBeVisible();
    await expect(detail.getByText("Stop all 2 active runs before removing this recipe from all 2 Sparks.")).toBeVisible();
    await testInfo.attach(`fleet-spark-lifecycle-${width}.png`, {body: await detail.screenshot(), contentType: "image/png"});

    await page.getByRole("button", {name: "7 days"}).click();
    await expect(page.getByRole("button", {name: "7 days"})).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByText(/Showing 15-minute buckets across the full 7-day window/)).toBeVisible();
    await expect(page.getByRole("img", {name: "Aurora GPU utilization history"})).toHaveAccessibleDescription(/reported buckets/);

    await page.getByRole("button", {name: "1 year"}).click();
    await expect(page.getByText(/Showing newest 1,500 15-minute buckets within 1 year/)).toBeVisible();
    await expect.poll(() => page.evaluate(() => ({
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))).toEqual({body: width, document: width, viewport: width});
  }
});

test("Spark metrics use typed history identities and keep the focused detail usable", async ({page}, testInfo) => {
  for (const [width, height] of [[1280, 900], [360, 800]] as const) {
    await page.setViewportSize({width, height});
    await page.goto("/fleet");
    await page.screenshot({path: testInfo.outputPath(`fleet-first-${width}.png`)});
    await page.getByRole("button", {name: "View Aurora details"}).click();
    const detail = page.getByRole("complementary", {name: "Aurora details"});
    await expect(detail.getByRole("button", {name: "Close Aurora details"})).toBeFocused();
    await detail.getByRole("tab", {name: "Metrics"}).click();
    const metrics = detail.getByRole("tabpanel", {name: "Metrics"});
    await metrics.scrollIntoViewIfNeeded();
    await expect(metrics.getByRole("heading", {name: "Metrics"})).toBeVisible();
    await expect(metrics.getByRole("img", {name: /Gpu · Utilization Percent GPU 0 history for 1 hour/})).toBeVisible();
    await expect(metrics.getByRole("img", {name: /Cpu · Temperature C Node aggregate history for 1 hour/})).toBeVisible();
    await expect(metrics.getByRole("img", {name: /Runtime · Requests Waiting Run run-chat history for 1 hour/})).toBeVisible();
    await expect(metrics.getByRole("img", {name: /Runtime · Ttft P95 Ms Run run-chat history for 1 hour/})).toBeVisible();
    await expect(metrics.getByRole("img", {name: /Runtime · Decode Tokens Per Second Run run-chat history for 1 hour/})).toBeVisible();
    await expect(metrics.getByRole("img", {name: /Runtime · Itl P95 Ms Run run-chat history for 1 hour/})).toBeVisible();
    await expect(metrics.getByText("Last observed").first()).toBeVisible();
    await expect.poll(() => page.evaluate(() => ({body: document.body.scrollWidth, document: document.documentElement.scrollWidth, viewport: innerWidth}))).toEqual({body: width, document: width, viewport: width});
    await page.screenshot({path: testInfo.outputPath(`spark-metrics-${width}.png`)});
  }
});

test("Fleet has no document overflow from phone through large desktop", async ({page}) => {
  await page.goto("/fleet");
  await page.getByRole("button", {name: "View Aurora details"}).click();

  for (const width of [360, 768, 1280, 1920]) {
    await page.setViewportSize({width, height: width === 360 ? 800 : 900});
    await expect.poll(() => page.evaluate(() => ({
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))).toEqual({body: width, document: width, viewport: width});
  }

  await page.setViewportSize({width: 360, height: 800});
  await expect(page.locator(".node-detail")).toHaveCSS("position", "static");
  await page.setViewportSize({width: 1920, height: 900});
  await expect(page.locator(".node-detail")).toHaveCSS("position", "static");
  await page.getByRole("button", {name: "Close Aurora details"}).click();
  const columns = await page.locator(".node-grid").evaluate(element => getComputedStyle(element).gridTemplateColumns.split(" ").length);
  expect(columns).toBeGreaterThanOrEqual(2);
});

test("Fleet compact and topology views persist, reflow, and keep technical IDs out of browse views", async ({page}, testInfo) => {
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto("/fleet");
  await openFleetControls(page);

  const mainBounds = await page.locator("#main-content").boundingBox();
  const controlsBounds = await page.locator(".fleet-controls-popover").boundingBox();
  expect(mainBounds).not.toBeNull();
  expect(controlsBounds).not.toBeNull();
  expect(controlsBounds!.x).toBeGreaterThanOrEqual(mainBounds!.x);
  expect(controlsBounds!.x + controlsBounds!.width).toBeLessThanOrEqual(1280);

  const commandRows = await page.locator(".fleet-command-header").evaluate(element => {
    const bounds = (selector: string) => {
      const child = element.querySelector(selector);
      if (!(child instanceof HTMLElement)) throw new Error(`Missing ${selector}`);
      const box = child.getBoundingClientRect();
      return {bottom: box.bottom, top: box.top};
    };
    return {actions: bounds(".fleet-command-actions"), summary: bounds(".fleet-command-summary")};
  });
  expect(commandRows.summary.top).toBeGreaterThanOrEqual(commandRows.actions.bottom);

  await page.getByRole("button", {name: "Topology"}).click();
  await page.getByRole("button", {name: "Close Fleet controls"}).click();

  await expect(page.getByRole("region", {name: "Fleet topology"})).toBeVisible();
  await expect(page.getByRole("button", {name: /View Aurora details/})).toBeVisible();
  await expect(page.getByText("Qwen pair", {exact: true}).first()).toBeVisible();
  await expect(page.getByText(nodeId, {exact: true})).toHaveCount(0);
  await expectNoSeriousAccessibilityViolations(page);
  await page.screenshot({path: testInfo.outputPath("fleet-topology-desktop.png"), fullPage: true});

  await page.reload();
  await openFleetControls(page);
  await expect(page.getByRole("button", {name: "Topology"})).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", {name: "Compact"}).click();
  await expect(page.getByRole("region", {name: "Fleet nodes compact table"})).toBeVisible();

  for (const width of [320, 360, 760, 1280]) {
    await page.setViewportSize({width, height: width <= 360 ? 800 : 900});
    await expect.poll(() => page.evaluate(() => ({
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))).toEqual({body: width, document: width, viewport: width});
  }
  await page.setViewportSize({width: 360, height: 800});
  await expect(page.getByRole("button", {name: "Close Fleet controls"})).toBeVisible();
  await page.getByRole("button", {name: "Close Fleet controls"}).click();
  await expect(page.locator(".fleet-controls-popover")).toBeHidden();
  await expect(page.locator(".fleet-controls-menu > summary")).toBeFocused();
  await expect(page.getByRole("heading", {name: "Fleet"})).toBeVisible();
  await expect(page.locator(".workload-matrix-scroll")).toHaveCount(0);
  const mobileRoster = page.getByRole("region", {name: "Fleet nodes compact table"});
  await expect(mobileRoster).toBeVisible();
  await expect(mobileRoster).toContainText("Aurora");
  await expect(mobileRoster).toContainText("Borealis");
  await expectNoSeriousAccessibilityViolations(page);
  await page.screenshot({path: testInfo.outputPath("fleet-compact-mobile.png"), fullPage: true});
});

test("Fleet resilient-state headings remain plain and scannable", async ({page}, testInfo) => {
  await page.goto("/fleet");
  await page.getByRole("button", {name: "View Aurora details"}).click();
  const detail = await page.locator(".node-detail-heading").evaluate(element => element.outerHTML);

  await page.getByRole("searchbox", {name: "Find a Spark"}).fill("no-such-spark");
  const filtered = await page.locator(".fleet-filter-empty").evaluate(element => element.outerHTML);

  const empty = {...localSnapshot(), nodes: []};
  await page.route("**/api/fleet/stream", route => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    body: `id: ${empty.event_cursor}\nevent: fleet-snapshot\ndata: ${JSON.stringify({schema_version: 1, reset_reason: "initial", snapshot: empty})}\n\n`,
  }));
  await page.route("**/api/fleet", route => route.fulfill({json: empty}));
  await page.reload();
  const emptyState = await page.locator(".fleet-empty").evaluate(element => element.outerHTML);

  await page.route("**/api/fleet/stream", route => route.fulfill({status: 503, body: "stream unavailable"}));
  await page.route("**/api/fleet", route => route.fulfill({status: 503, json: {detail: "projection unavailable"}}));
  await page.reload();
  const errorState = await page.locator(".fleet-error").evaluate(element => element.outerHTML);
  browserProblems.set(page, []);

  for (const state of [detail, filtered, emptyState, errorState]) {
    expect(state).not.toContain("fleet-kicker");
    expect(state).not.toContain("node-eyebrow");
  }

  await page.setContent(`<main class="state-evidence"><header><h1>Fleet resilient states</h1><p>Plain headings keep recovery and inspection states direct.</p></header><section><h2>Connection failure</h2>${errorState}</section><section><h2>Registered Fleet is empty</h2>${emptyState}</section><section><h2>Filters return no Sparks</h2>${filtered}</section><section><h2>Selected Spark detail</h2>${detail}</section></main>`);
  await page.addStyleTag({path: "src/styles.css"});
  await page.addStyleTag({content: `body{padding:32px;background:#07100d}.state-evidence{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;max-width:1180px;margin:auto}.state-evidence>header{grid-column:1/-1}.state-evidence>header h1{margin:0;font-size:32px}.state-evidence>header p{color:var(--text-muted)}.state-evidence>section{min-width:0;padding:18px;border:1px solid var(--border);border-radius:14px;background:#0c1815}.state-evidence>section>h2{margin:0 0 12px;color:var(--text-subtle);font-size:13px}.state-evidence .fleet-error,.state-evidence .fleet-empty,.state-evidence .fleet-filter-empty{margin:0}.state-evidence .node-detail-heading{padding:16px;border:1px solid var(--border);border-radius:12px;background:var(--surface-panel)}@media(max-width:760px){.state-evidence{grid-template-columns:1fr}}`});
  await page.screenshot({path: testInfo.outputPath("fleet-resilient-states.png"), fullPage: true});
});

test("Add Spark preserves an in-flight and revealed one-time grant until an explicit decision", async ({page}) => {
  let releaseGrant!: () => void;
  const grantGate = new Promise<void>(resolve => { releaseGrant = resolve; });
  let grantRequests = 0;
  await page.route("**/api/agents/enrollments/grants", async route => {
    grantRequests += 1;
    await grantGate;
    await route.fulfill({status: 201, json: {
      id: "grant-e2e", purpose: "new-node", token: "short-lived-e2e-secret", expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
      controller_endpoint: "https://controller.fixture.invalid:9443",
      enrollment_endpoint: "https://enrollment.fixture.invalid:9444",
      ca_fingerprint: "d".repeat(64),
      installer_url: "https://install.vonkforge.ai/dev/spark",
      controller_address: "192.168.1.231",
      service_hostnames: ["controller.fixture.invalid", "enrollment.fixture.invalid"],
    }});
  });
  await page.goto("/fleet");
  await page.getByRole("button", {name: "Add Spark"}).click();
  const dialog = page.getByRole("dialog", {name: "Add Spark"});
  await dialog.getByRole("button", {name: "Create one-time enrollment command"}).click();
  await expect.poll(() => grantRequests).toBe(1);

  await expect(dialog).toHaveAttribute("aria-busy", "true");
  await expect(dialog.getByRole("button", {name: "Close Add Spark"})).toBeDisabled();
  await expect(dialog.getByRole("button", {name: "Cancel"})).toBeDisabled();
  await page.keyboard.press("Escape");
  await page.locator(".library-dialog-backdrop").evaluate(element => element.dispatchEvent(new MouseEvent("mousedown", {bubbles: true})));
  await expect(dialog).toBeVisible();
  await page.getByRole("link", {name: "Library"}).dispatchEvent("click");
  await expect(page).toHaveURL(/\/fleet$/);

  releaseGrant();
  await expect(dialog.getByText("short-lived-e2e-secret")).toBeVisible();
  await expect(dialog).not.toHaveAttribute("aria-busy");
  await dialog.getByRole("button", {name: "Close Add Spark"}).click();
  await expect(dialog.getByText("Discard this one-time grant?")).toBeVisible();
  await expect(dialog.getByRole("button", {name: "Keep grant open"})).toBeFocused();
  await expectNoSeriousAccessibilityViolations(page);
  await dialog.getByRole("button", {name: "Keep grant open"}).click();
  await expect(dialog.getByText("Discard this one-time grant?")).toBeHidden();
  await dialog.getByRole("button", {name: "I saved these values — Done"}).click();
  await expect(dialog).toBeHidden();
  await page.getByRole("link", {name: "Library"}).click();
  await expect(page).toHaveURL(/\/library$/);
});

test("Library separates installation capacity from load memory admission", async ({page}, testInfo) => {
  const blocked = canonicalRecipeDetail();
  const group = blocked.placement[0].recommendations[0];
  group.eligible = false;
  group.reasons = [
    {code: "run.insufficient_memory", detail: "Run would leave 1073741824 bytes on node-alpha, below the 4000000000-byte floor.", severity: "error"},
    {code: "run.insufficient_memory", detail: "Run would leave 1073741824 bytes on node-beta, below the 4000000000-byte floor.", severity: "error"},
  ];
  blocked.placement[0].rejected_groups = [];
  blocked.placement[0].rejected_nodes = [];
  await page.unroute(`**/api/library/recipes/${pairedRecipeId}`);
  await page.unroute("**/api/library/recipes/*");
  await page.route(`**/api/library/recipes/${pairedRecipeId}`, route => route.fulfill({json: blocked}));
  await page.setViewportSize({width: 1280, height: 900});

  await page.goto(`/library/recipes/${pairedRecipeId}`);

  const placement = page.getByRole("region", {name: "Complete placement groups"});
  await expect(placement.getByText("1 Sparks · 1 installable")).toBeVisible();
  const blocker = placement.locator(".placement-load-blocked-summary");
  await expect(blocker).toContainText("Installable, but cannot be loaded");
  await expect(blocker).toContainText("1.0 GiB");
  await expect(blocker).not.toContainText("run.insufficient_memory");
  await expect(placement.getByText("Unavailable placement evidence").locator("..")).not.toHaveAttribute("open");
  const selector = placement.getByRole("button", {name: "Select complete group Spark node"});
  await selector.click();
  await expect(placement.getByRole("button", {name: "Review Load"})).toHaveCount(0);
  await expect(placement.locator(".placement-group")).not.toContainText("run.insufficient_memory");
  await expectNoSeriousAccessibilityViolations(page);
  await testInfo.attach("installable-load-blocked.png", {body: await placement.screenshot(), contentType: "image/png"});
});

test("Library retries a failed snapshot and recipe detail request", async ({page}) => {
  const state = libraryFixtures.get(page)!;
  state.snapshotFailuresRemaining = 1;
  await page.goto(`/library?view=models&model=${encodeURIComponent(pairedModelKey)}`);
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("button", {name: "Retry Library"}).click();
  await expect(page.getByRole("region", {name: "Models"})).toBeVisible();

  state.detailFailuresRemaining = 1;
  await page.locator(".library-subnav").getByRole("link", {name: "Recipes", exact: true}).click();
  await expect(page.getByLabel("Recipes matching selected Model")).toContainText(pairedRecipeTitle);
  await page.goto(`/library/recipes/${pairedRecipeId}`);
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("button", {name: "Retry recipe detail"}).click();
  await expect(page.locator(".library-recipe-detail")).toBeVisible();
});

test("Library route changes restore heading focus and browser back state", async ({page}) => {
  await page.goto(`/library?view=models&model=${encodeURIComponent(pairedModelKey)}`);
  await expect(page.getByRole("region", {name: "Models"})).toBeVisible();
  await page.locator(".library-subnav").getByRole("link", {name: "Recipes", exact: true}).click();
  await expect(page).toHaveURL(new RegExp(`/library\\?model=${encodeURIComponent(pairedModelKey)}`));
  await expect(page.getByLabel("Recipes matching selected Model")).toContainText(pairedRecipeTitle);
  await page.goto(`/library/recipes/${pairedRecipeId}`);
  await expect(page).toHaveURL(new RegExp(`/library/recipes/${pairedRecipeId}$`));
  await expect(page.getByRole("heading", {name: "Library", exact: true})).toBeFocused();

  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/library\\?model=${encodeURIComponent(pairedModelKey)}`));
  await expect(page.getByLabel("Model and recipe list")).toBeVisible();
  await page.locator(".library-subnav").getByRole("link", {name: "Models", exact: true}).click();
  await expect(page).toHaveURL(new RegExp(`/library\\?(?:model=${encodeURIComponent(pairedModelKey)}&view=models|view=models&model=${encodeURIComponent(pairedModelKey)})`));
  await expect(page.getByRole("heading", {name: "Library", exact: true})).toBeFocused();
});

test("Recipe Make available uses durable aggregate progress, access resume, and image-only force", async ({page}) => {
  const revisionId = fullLibraryDetail.recipe.recipe_revision_id;
  const modelDigest = "b".repeat(64);
  const imageDigest = "c".repeat(64);
  let stage: "empty" | "running" | "failed" | "resumed" | "forced" = "empty";
  const starts: Record<string, unknown>[] = [];
  const progress = (phase: string, completedBytes: number, totalBytes: number, step?: string) => ({phase, completed_bytes: completedBytes, total_bytes: totalBytes, total_bytes_known: true, bytes_per_second: 10, eta_seconds: Math.max(0, Math.ceil((totalBytes - completedBytes) / 10)), checkpoint: null, ...(step ? {step} : {})});
  const operation = (current: typeof stage) => ({
    schema_version: 2, id: "recipe-availability-operation", request_id: "request-availability", kind: "recipe.image.availability.v2", state: current === "failed" ? "failed" : "running", attempt: current === "forced" ? 2 : 1,
    recipe_revision_id: revisionId, recipe_content_sha256: "a".repeat(64), progress: progress(current === "forced" ? "build" : "prepare", 40, 100, current === "forced" ? "Rebuilding runtime image" : undefined),
    children: [
      {kind: "model-cache", id: "model-child-1", state: current === "failed" ? "failed" : current === "forced" ? "succeeded" : "running", artifact_set_sha256: modelDigest, plan_digest: "d".repeat(64), progress: progress("download", 40, 100), failure: current === "failed" ? {code: "access_required", detail: "Hugging Face access is required for the exact Model files.", recovery_actions: ["open_model_access", "configure_hf_token", "check_access_and_resume"], retryable: false, retry_time: null, retry_after_seconds: null, log_excerpt: null, required_bytes: null, free_bytes: null, shortfall_bytes: null} : null},
      {kind: "runtime-image", id: "image-child-1", state: "running", progress: progress("build", 20, 100, "Building runtime image"), failure: null},
    ],
    failure: null, result: null, actions: [], created_at: "2026-09-06T12:00:00Z", updated_at: "2026-09-06T12:00:00Z",
  });
  await page.route("**/api/library/recipe-image-availability?*", async route => route.fulfill({json: {schema_version: 2, total: stage === "empty" ? 0 : 1, operations: stage === "empty" ? [] : [operation(stage)], next_cursor: null}}));
  await page.route("**/api/library/recipe-image-availability", async route => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = await route.request().postDataJSON() as Record<string, unknown>;
    starts.push(body);
    stage = body.force === true ? "forced" : "running";
    return route.fulfill({status: 202, json: operation(stage)});
  });
  await page.route("**/api/library/recipe-image-availability/recipe-availability-operation", route => route.fulfill({json: operation(stage)}));
  await page.route("**/api/model-cache/operations/model-child-1/check-access-and-resume", async route => {
    stage = "resumed";
    return route.fulfill({status: 202, json: {schema_version: 2, id: "model-child-1", kind: "download", state: "queued", attempt: 2, request_key: "request-resume", artifact_set_sha256: modelDigest, plan_digest: "d".repeat(64), progress: {schema_version: 2, phase: "queued", completed_artifacts: 1, total_artifacts: 2, downloaded_bytes: 40, expected_bytes: 100, total_bytes_known: true, current_artifact_key: "weights", bytes_per_second: null, eta_seconds: null, members: []}, failure: null, result: null, created_at: "2026-09-06T12:00:00Z", updated_at: "2026-09-06T12:02:00Z", completed_at: null}});
  });

  await page.goto(`/library/recipes/${pairedRecipeId}`);
  const availability = page.getByRole("region", {name: "Make Recipe available"});
  await expect(availability.getByRole("button", {name: "Make available"})).toBeVisible();
  await availability.getByRole("button", {name: "Make available"}).click();
  await expect(availability.getByRole("list", {name: "Availability members"})).toContainText("Model files");
  await expect(availability.getByRole("list", {name: "Availability members"})).toContainText("Runtime image");
  await expect(availability.getByText("40 B / 100 B").first()).toBeVisible();
  expect(starts).toEqual([{request_key: expect.any(String), recipe_revision_id: revisionId, force: false}]);

  await page.reload();
  await expect(availability.getByText("Preparing exact Recipe on NAS")).toBeVisible();
  stage = "failed";
  await page.reload();
  await expect(availability.getByRole("button", {name: "Check access and resume"})).toBeVisible();
  await availability.getByRole("button", {name: "Check access and resume"}).click();
  await expect(availability.getByText("Preparing exact Recipe on NAS")).toBeVisible();
  await availability.locator("summary").filter({hasText: "More actions"}).click();
  await availability.getByRole("button", {name: "Rebuild image"}).click();
  await expect.poll(() => starts.at(-1)?.force).toBe(true);
  expect(starts).toHaveLength(2);
  await expect(availability.locator('[data-member-kind="model-cache"]')).toContainText("Succeeded");
  await expect(availability.locator('[data-member-kind="runtime-image"]')).toContainText("Runtime image");
});

test("Recipe progress keeps updating after image failure and recovers from a polling error", async ({page}) => {
  const progress = (bytes: number): components["schemas"]["OperationProgress"] => ({phase: "download", completed_bytes: bytes, total_bytes: 100, total_bytes_known: true});
  const operation = (bytes: number): components["schemas"]["RecipeImageAvailabilityResponse"] => ({
    schema_version: 2, id: "continuing-download", request_id: "prepare-request", kind: "recipe.image.availability.v2", state: "failed", attempt: 1,
    recipe_revision_id: fullLibraryDetail.recipe.recipe_revision_id, recipe_content_sha256: "a".repeat(64),
    progress: progress(bytes), created_at: "2026-09-09T08:00:00Z", updated_at: "2026-09-09T08:00:01Z",
    children: [
      {kind: "model-cache", id: "download-child", model_content_digests: [], state: bytes === 100 ? "succeeded" : "running", progress: progress(bytes)},
      {kind: "runtime-image", id: "failed-image", model_content_digests: [], state: "failed", progress: {...progress(0), phase: "build"}},
    ],
  });
  let polls = 0;
  await page.route("**/api/library/recipe-image-availability?*", route => route.fulfill({json: {schema_version: 2, total: 1, operations: [operation(40)]}}));
  await page.route("**/api/library/recipe-image-availability/continuing-download", route => {
    polls++;
    return polls === 1 ? route.fulfill({status: 503, json: {detail: "Temporarily unavailable"}}) : route.fulfill({json: operation(polls === 2 ? 75 : 100)});
  });
  await page.goto(`/library/recipes/${pairedRecipeId}`);
  const availability = page.getByRole("region", {name: "Make Recipe available"});
  const model = availability.locator('[data-member-kind="model-cache"]');
  await expect(model).toContainText("40 B / 100 B");
  await expect(availability.getByRole("status")).toContainText("progress updates automatically");
  await expect(availability.getByText("Availability status could not be loaded.")).toBeVisible();
  await expect(model).toContainText("75 B / 100 B");
  await expect(availability.getByText("Availability status could not be loaded.")).not.toBeVisible();
  await expect(model).toContainText("100 B / 100 B");
  await expect(model).toContainText("Succeeded");
  await expect(availability.getByRole("status")).not.toBeVisible();
  await page.waitForTimeout(1_500);
  expect(polls).toBe(3);
  expect(browserProblems.get(page)).toEqual(["error: Failed to load resource: the server responded with a status of 503 (Service Unavailable)"]);
  browserProblems.set(page, []);
});

test("Library pairs exact model selection with matching recipes and downloads an unlinked Model", async ({page}, testInfo) => {
  const linked = librarySnapshot.models.find(model => model.recipes.length > 0)!;
  const unlinked = librarySnapshot.models.find(model => model.recipes.length === 0)!;
  const modelKey = (model: typeof linked) => `${model.model.publisher}/${model.model.slug}@${model.model.content_sha256}`;
  const cacheOperation = {
    schema_version: 2, id: "model-download-operation", attempt: 1, request_key: "00000000-0000-4000-8000-000000000801", kind: "download", state: "running", artifact_set_sha256: "f".repeat(64), plan_digest: "model-download-plan",
    progress: {schema_version: 2, phase: "downloading", completed_artifacts: 1, total_artifacts: 2, downloaded_bytes: 100, expected_bytes: 200, current_artifact_key: "weights"}, result: null, last_error: null, created_at: "2026-09-06T00:00:00Z", updated_at: "2026-09-06T00:00:01Z", completed_at: null,
  };
  await page.route("**/api/model-cache/download-preview", async route => route.fulfill({json: {schema_version: 2, artifact_set_sha256: "f".repeat(64), plan_digest: "model-download-plan", source_policy: "nas-first", artifact_count: 2, expected_bytes: 200, already_cached_bytes: 0, new_bytes: 200, blockers: [], warnings: []}}));
  await page.route("**/api/model-cache/download", route => route.fulfill({status: 202, json: cacheOperation}));
  await page.route("**/api/model-cache/operations/*", route => route.fulfill({json: cacheOperation}));
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto(`/library?model=${encodeURIComponent(modelKey(linked))}`);
  const paired = page.getByLabel("Model and recipe list");
  await expect(paired).toBeVisible();
  const recipes = page.getByLabel("Recipes matching selected model");
  await expect(recipes).toContainText(linked.recipes[0]!.title);
  const workcellBox = await page.locator(".library-workcell").boundingBox();
  const pairedBox = await paired.boundingBox();
  const contentBox = await page.locator(".content-frame").boundingBox();
  expect(workcellBox).not.toBeNull();
  expect(pairedBox).not.toBeNull();
  expect(contentBox).not.toBeNull();
  expect(pairedBox!.width).toBeGreaterThan(900);
  expect(Math.abs(pairedBox!.x - workcellBox!.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(pairedBox!.x - contentBox!.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(pairedBox!.width - workcellBox!.width)).toBeLessThanOrEqual(2);
  await expectNoSeriousAccessibilityViolations(page);
  await page.screenshot({path: testInfo.outputPath("library-model-recipe-paired-desktop.png"), fullPage: false});

  await page.setViewportSize({width: 360, height: 800});
  await page.reload();
  const mobileRecipes = page.getByLabel("Recipes matching selected model");
  await expect(mobileRecipes).toContainText(linked.recipes[0]!.title);
  const mobileHeaderBox = await page.locator(".app-header").boundingBox();
  const mobileRecipesBox = await mobileRecipes.boundingBox();
  expect(mobileRecipesBox?.y ?? 0).toBeGreaterThanOrEqual((mobileHeaderBox?.height ?? 0) - 1);
  expect(mobileRecipesBox?.y ?? Number.POSITIVE_INFINITY).toBeLessThan(800);
  await page.getByRole("heading", {name: "Library", exact: true}).focus();
  await expectNoSeriousAccessibilityViolations(page);
  await page.screenshot({path: testInfo.outputPath("library-model-recipe-paired-mobile.png"), fullPage: false});

  await page.goto(`/library?view=models&model=${encodeURIComponent(modelKey(unlinked))}`);
  const modelInventory = page.getByLabel("Exact model inventory");
  const orphanRow = modelInventory.locator(".library-model-row").filter({hasText: unlinked.model_document.identity.model.title}).first();
  await expect(orphanRow).toContainText("No Recipe");
  const previewRequest = page.waitForRequest(request => request.url().endsWith("/api/model-cache/download-preview"));
  await orphanRow.getByRole("button", {name: "Make available"}).click();
  expect((await previewRequest).postDataJSON()).toMatchObject({schema_version: 2, model_content_sha256: unlinked.model.content_sha256});
  await expect(page.getByText(/Downloading to NAS/)).toBeVisible();
});

test("Library retries a transient Model cache operation without restarting its transfer", async ({page}) => {
  const model = librarySnapshot.models.find(item => item.recipes.length > 0)!;
  const modelKey = `${model.model.publisher}/${model.model.slug}@${model.model.content_sha256}`;
  const failedId = "model-download-failed";
  const replacementId = "model-download-retry";
  const failed = {
    schema_version: 2, id: failedId, attempt: 1, request_key: "00000000-0000-4000-8000-000000000811", kind: "download", state: "failed", artifact_set_sha256: "f".repeat(64), plan_digest: "model-download-plan",
    progress: {schema_version: 2, phase: "failed", completed_artifacts: 1, total_artifacts: 2, downloaded_bytes: 100, expected_bytes: 200, current_artifact_key: "weights"}, failure: {code: "transfer_failed", detail: "temporary transfer failure", retryable: true, recovery_actions: ["retry"]}, result: null, last_error: "temporary transfer failure", created_at: "2026-09-06T00:00:00Z", updated_at: "2026-09-06T00:00:01Z", completed_at: "2026-09-06T00:00:01Z",
  };
  const replacement = {...failed, id: replacementId, state: "succeeded", request_key: "00000000-0000-4000-8000-000000000812", failure: null, result: null, last_error: null, progress: {...failed.progress, phase: "completed", completed_artifacts: 2, downloaded_bytes: 200}, updated_at: "2026-09-06T00:00:02Z", completed_at: "2026-09-06T00:00:02Z"};
  let downloadCalls = 0;
  let retryBody: Record<string, unknown> | undefined;
  await page.unroute("**/api/model-cache/download");
  await page.unroute("**/api/model-cache/operations/*");
  await page.route("**/api/model-cache/download-preview", route => route.fulfill({json: {schema_version: 2, artifact_set_sha256: "f".repeat(64), plan_digest: "model-download-plan", source_policy: "nas-first", artifact_count: 2, expected_bytes: 200, already_cached_bytes: 100, new_bytes: 100, blockers: [], warnings: []}}));
  await page.route("**/api/model-cache/download", async route => { downloadCalls += 1; return route.fulfill({status: 202, json: failed}); });
  await page.route(`**/api/model-cache/operations/${failedId}/retry`, async route => { retryBody = await route.request().postDataJSON() as Record<string, unknown>; return route.fulfill({status: 202, json: replacement}); });
  await page.route(`**/api/model-cache/operations/${failedId}`, route => route.fulfill({json: failed}));
  await page.route(`**/api/model-cache/operations/${replacementId}`, route => route.fulfill({json: replacement}));
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto(`/library?view=models&model=${encodeURIComponent(modelKey)}`);

  const row = page.getByLabel("Exact model inventory").locator(".library-model-row").first();
  await row.getByRole("button", {name: "Make available"}).click();
  await expect(row.getByRole("button", {name: "Retry download"}).first()).toBeVisible();
  await row.getByRole("button", {name: "Retry download"}).first().click();
  await expect.poll(() => retryBody).toMatchObject({schema_version: 2, request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)});
  expect(downloadCalls).toBe(1);
  await expect(row.getByRole("button", {name: "Available on NAS"})).toBeVisible();
});
