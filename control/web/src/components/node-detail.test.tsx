import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi, RichTelemetryPoint, TelemetryCapabilitiesResponse, TelemetryCurrentResponse, TelemetryHistory, TelemetryMetrics, TelemetryWorkloadsResponse, VisualFleetNode} from "../api/types";
import {sparklinePath} from "./sparkline";
import {NodeDetail} from "./node-detail";

const NOW = new Date("2026-08-15T12:00:00Z");

function node(): VisualFleetNode {
  return {
    id: "spk_0123456789abcdef0123456789abcdef", display_name: "Spark One", hostname: "spark-one.internal", lifecycle: "managed", labels: {rack: "left"},
    connection: {agent_state: "active", certificate_state: "valid", online_state: "online", offline_reason: null, last_seen_at: "2026-08-15T11:59:59Z", last_seen_age_seconds: 1},
    inventory: null,
    telemetry: null,
    installed: [], loaded: [],
    reservations: {disk_bytes: 0, unified_memory_bytes: 0, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 0},
    warnings: [{code: "telemetry.delayed", detail: "Telemetry delivery is delayed.", severity: "warning"}],
  };
}

function richHistoryMetrics(observedAt: string, values: {gpu: number; cpuTemperature: number; cpuUtilization: number | null; memory: number; power: number; storageRead: number; storageWrite: number; networkReceive: number | null; networkTransmit: number; decode: number; prefill: number; queue: number; ttft: number; e2e: number; itl: number}): TelemetryMetrics {
  type Scope = TelemetryMetrics["series"][number]["scope"];
  const sample = (key: string, scope: Scope, value: number | null, unit: string, context: Partial<TelemetryMetrics["series"][number]> = {}) => ({
    key, scope, value, unit, source: "spark-agent.fixture", measurement_kind: "measured" as const, observed_at: observedAt, received_at: observedAt,
    freshness: "fresh" as const, freshness_threshold_seconds: 6, support_status: value === null ? "unavailable" as const : "available" as const, reason: value === null ? "No sample was reported." : null, aggregation: "latest", ...context,
  });
  return {
    schema_version: 2,
    series: [
      sample("gpu.utilization_percent", "accelerator", values.gpu, "%", {device_id: "0"}),
      sample("gpu.power_watts", "accelerator", values.power, "W", {device_id: "0"}),
      sample("cpu.temperature_c", "node", values.cpuTemperature, "degC"),
      sample("cpu.utilization_percent", "node", values.cpuUtilization, "%"),
      sample("memory.available_bytes", "memory", values.memory, "bytes"),
      sample("memory.bandwidth_bytes_per_second", "memory", (300 + values.gpu) * 1024 ** 3, "bytes/s"),
      sample("storage.read_bytes_per_second", "storage", values.storageRead * 1024 ** 2, "bytes/s", {device_id: "nvme0n1"}),
      sample("storage.write_bytes_per_second", "storage", values.storageWrite * 1024 ** 2, "bytes/s", {device_id: "nvme0n1"}),
      sample("network.receive_bytes_per_second", "network", values.networkReceive, "bytes/s", {interface_name: "eth0"}),
      sample("network.transmit_bytes_per_second", "network", values.networkTransmit, "bytes/s", {interface_name: "eth0"}),
      sample("runtime.decode_tokens_per_second", "runtime", values.decode, "tokens/s", {run_id: "run-chat"}),
      sample("runtime.prefill_tokens_per_second", "runtime", values.prefill, "tokens/s", {run_id: "run-chat"}),
      sample("runtime.requests_waiting", "runtime", values.queue, "requests", {run_id: "run-chat"}),
      sample("runtime.ttft_p95_ms", "runtime", values.ttft, "ms", {run_id: "run-chat"}),
      sample("runtime.e2e_p95_ms", "runtime", values.e2e, "ms", {run_id: "run-chat"}),
      sample("runtime.itl_p95_ms", "runtime", values.itl, "ms", {run_id: "run-chat"}),
    ],
    capabilities: [], runtimes: [], workloads: [],
    provenance: {collector: "spark-agent", collector_version: "fixture-2", host_uptime_seconds: 7200, source_observed_at: observedAt},
  };
}

function history(start = "2026-08-15T11:00:00.000Z", end = "2026-08-15T12:00:00.000Z"): TelemetryHistory {
  return {
    schema_version: 1,
    node_id: node().id,
    start,
    end,
    resolution: "raw",
    maximum_points: 360,
    points: [{
      id: "sample-1", node_id: node().id, boot_id: "00000000-0000-0000-0000-000000000001", sequence: 1,
      observed_at: "2026-08-15T11:30:00Z", received_at: "2026-08-15T11:30:01Z",
      cpu_utilization_percent: 10, load_average_1m: 1,
      memory_total_bytes: 100, memory_available_bytes: 80,
      disk_total_bytes: 100, disk_free_bytes: 50,
      gpu_utilization_percent: 20, gpu_memory_total_bytes: 100, gpu_memory_free_bytes: 70,
      temperature_c: 40, power_watts: 18,
      network_receive_bytes_per_second: 1000, network_transmit_bytes_per_second: 500,
      gap_samples: 0, details: {accelerator_name: "NVIDIA GB10", accelerator_performance_state: "P0"},
      metrics: richHistoryMetrics("2026-08-15T11:30:00Z", {gpu: 20, cpuTemperature: 40, cpuUtilization: 10, memory: 80, power: 18, storageRead: 40, storageWrite: 8, networkReceive: 1000, networkTransmit: 500, decode: 100, prefill: 800, queue: 1, ttft: 200, e2e: 900, itl: 20}),
    }, {
      id: "sample-2", node_id: node().id, boot_id: "00000000-0000-0000-0000-000000000001", sequence: 2,
      observed_at: "2026-08-15T11:45:00Z", received_at: "2026-08-15T11:45:01Z",
      cpu_utilization_percent: null, load_average_1m: null,
      memory_total_bytes: 100, memory_available_bytes: 75,
      disk_total_bytes: null, disk_free_bytes: null,
      gpu_utilization_percent: 30, gpu_memory_total_bytes: null, gpu_memory_free_bytes: null,
      temperature_c: 42, power_watts: null,
      network_receive_bytes_per_second: null, network_transmit_bytes_per_second: null,
      gap_samples: 1, details: {accelerator_name: null, accelerator_performance_state: null},
      metrics: richHistoryMetrics("2026-08-15T11:45:00Z", {gpu: 30, cpuTemperature: 42, cpuUtilization: null, memory: 75, power: 19, storageRead: 44, storageWrite: 9, networkReceive: null, networkTransmit: 520, decode: 110, prefill: 820, queue: 2, ttft: 210, e2e: 920, itl: 21}),
    }],
  };
}

test("loads bounded history ranges and renders accessible summaries", async () => {
  const calls: Array<{end: string; maximum: number; resolution: string; signal?: AbortSignal; start: string}> = [];
  const control = {
    nodeTelemetryHistory: async (_nodeId: string, start: string, end: string, resolution: string, maximum: number, signal?: AbortSignal) => {
      calls.push({end, maximum, resolution, signal, start});
      return history(start, end);
    },
  } as unknown as ControlApi;
  const view = render(<NodeDetail api={control} node={node()} now={NOW} onClose={() => undefined}/>);

  expect(screen.getByRole("button", {name: "Close Spark One details"})).toHaveFocus();
  expect(await screen.findByRole("img", {name: "Spark One GPU utilization history"})).toHaveAccessibleDescription("Mean 25%; latest mean 30%; reported range 20% to 30%; 2 reported samples.");
  expect(screen.getByRole("img", {name: "Spark One Available memory history"})).toHaveAccessibleDescription(/75 B/);
  expect(screen.getByRole("img", {name: "Spark One CPU temperature history"})).toHaveAccessibleDescription(/42 °C/);
  expect(screen.getByText("GPU utilization")).toBeVisible();
  expect(screen.getByText("Available memory")).toBeVisible();
  expect(screen.getByText("CPU temperature")).toBeVisible();
  expect(screen.getAllByText("Scale 0%–100%")[0]).toBeVisible();
  expect(screen.getByText("Scale 0 B–80 B")).toBeVisible();
  expect(screen.getByText("Scale 0 °C–100 °C")).toBeVisible();
  expect(screen.getAllByText(/Mean 25%/).length).toBeGreaterThan(0);
  expect(calls[0]).toMatchObject({
    start: "2026-08-15T11:00:00.000Z",
    end: "2026-08-15T12:00:00.000Z",
    maximum: 60,
    resolution: "minute",
  });

  await userEvent.click(screen.getByRole("button", {name: "24 hours"}));
  await waitFor(() => expect(calls).toHaveLength(2));
  expect(calls[0].signal?.aborted).toBe(true);
  expect(calls[1]).toMatchObject({
    start: "2026-08-14T12:00:00.000Z",
    end: "2026-08-15T12:00:00.000Z",
    maximum: 1440,
    resolution: "minute",
  });
  expect(screen.getByRole("button", {name: "24 hours"})).toHaveAttribute("aria-pressed", "true");

  view.unmount();
  expect(calls[1].signal?.aborted).toBe(true);
});

test("offers an individual agent upgrade from Spark detail", async () => {
  const onUpgrade = vi.fn();
  const control = {
    nodeTelemetryHistory: async () => history(),
  } as unknown as ControlApi;
  render(<NodeDetail api={control} node={node()} now={NOW} onClose={() => undefined} onUpgrade={onUpgrade}/>);

  await userEvent.click(screen.getByRole("button", {name: "Upgrade agent"}));

  expect(onUpgrade).toHaveBeenCalledOnce();
});

test("opens a Spark-scoped model management workspace", () => {
  const control = {nodeTelemetryHistory: async () => history()} as unknown as ControlApi;
  render(<NodeDetail api={control} node={node()} now={NOW} onClose={() => undefined}/>);

  expect(screen.getByRole("link", {name: "Download to NAS"})).toHaveAttribute(
    "href",
    `/library?spark=${encodeURIComponent(node().id)}`,
  );
});

test("stops a running model from the Spark inspector with an authority preview", async () => {
  const projected = node();
  projected.installed = [{
    installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen Chat", topology_name: "single", expected_rank_count: 1, present_ranks: [0], member_node_ids: [projected.id], rank: 0, role: "leader", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
  }];
  projected.loaded = [{
    run_id: "run-chat", installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen Chat", alias: "qwen-chat", expected_rank_count: 1, present_ranks: [0], member_node_ids: [projected.id], rank: 0, role: "leader", rank_state: "running", rank_age_seconds: 1, rank_fresh: true, run_state: "running", route_state: "published", group_state: "healthy", healthy: true, degraded_reason: null,
  }];
  const applyLibraryStop = vi.fn(async () => ({id: "operation-stop", kind: "recipe.stop", owner_id: "run-chat", state: "succeeded", plan_digest: "stop-plan", nodes: [projected.id], result: null}));
  const refresh = vi.fn(async () => { throw new Error("Fleet refresh temporarily unavailable"); });
  const control = {
    nodeTelemetryHistory: async () => history(),
    previewLibraryStop: async () => ({
      alias: "qwen-chat", allowed: true, authority_digest: "authority", blockers: [], installation_id: "install-chat",
      nodes: [{active_memory_reservation_bytes: 4, node_id: projected.id, rank: 0, reserved_memory_bytes: 8, role: "leader", state: "running"}],
      plan_digest: "stop-plan", recipe_revision_id: "revision-chat", route_digest: "route", route_generation: 1,
      route_state: "published", route_withdrawal: true, run_id: "run-chat", run_state: "running", total_active_memory_reservation_bytes: 4, warnings: [],
    }),
    applyLibraryStop,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<NodeDetail api={control} node={projected} now={NOW} onClose={() => undefined} onLifecycleRefresh={refresh}/>);

  expect(screen.getByText("Stop the active run before removing this recipe from this Spark.")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Stop Qwen Chat on this Spark"}));
  const dialog = await screen.findByRole("dialog", {name: "Review Stop"});
  expect(within(dialog).getByText("Published route will be withdrawn.")).toBeVisible();
  await user.click(within(dialog).getByRole("button", {name: "Stop selected run"}));

  expect(applyLibraryStop).toHaveBeenCalledWith("run-chat", {plan_digest: "stop-plan", request_key: expect.any(String)}, expect.any(AbortSignal));
  expect(await screen.findByRole("region", {name: "Stop operation progress"})).toHaveTextContent("Operation complete");
  expect(refresh).toHaveBeenCalled();
});

test("removes an idle recipe from its complete Spark group in place", async () => {
  const projected = node();
  projected.installed = [{
    installation_id: "install-pair", recipe_id: "recipe-pair", recipe_revision_id: "revision-pair", title: "Qwen Pair", topology_name: "pair", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [projected.id, "spark-two"], rank: 0, role: "leader", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
  }];
  const applyLibraryUninstall = vi.fn(async () => ({id: "operation-remove", kind: "recipe.uninstall", owner_id: "install-pair", state: "succeeded", plan_digest: "remove-plan", nodes: [projected.id, "spark-two"], result: null}));
  const control = {
    nodeTelemetryHistory: async () => history(),
    previewLibraryUninstall: async () => ({
      active_run_count: 0, active_runs: [], active_runs_truncated: false, allowed: true, blockers: [], bytes_removed: 120,
      consequences: {automatic_stop: false, catalog_retained: true, reinstall_required: true}, installation_authority_digest: "authority", installation_id: "install-pair", installation_state: "installed",
      nodes: [{installed_bytes: 60, node_id: projected.id, rank: 0, role: "leader", state: "installed"}, {installed_bytes: 60, node_id: "spark-two", rank: 1, role: "worker", state: "installed"}],
      original_plan_digest: "install-plan", plan_digest: "remove-plan", recipe_content: {}, recipe_content_sha256: "a".repeat(64), recipe_id: "recipe-pair", recipe_revision_id: "revision-pair", warnings: [],
    }),
    applyLibraryUninstall,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<NodeDetail api={control} node={projected} now={NOW} onClose={() => undefined}/>);

  await user.click(screen.getByRole("button", {name: "Remove Qwen Pair from 2 Sparks"}));
  const dialog = await screen.findByRole("dialog", {name: "Review Remove"});
  expect(within(dialog).getByText("The local catalog recipe is retained.")).toBeVisible();
  await user.click(within(dialog).getByRole("button", {name: "Remove selected installation"}));

  expect(applyLibraryUninstall).toHaveBeenCalledWith("install-pair", {plan_digest: "remove-plan", request_key: expect.any(String)}, expect.any(AbortSignal));
  expect(await screen.findByRole("region", {name: "Remove operation progress"})).toHaveTextContent("Operation complete");
});

test("shows structured node evidence and a retryable history error", async () => {
  let fail = true;
  const control = {
    nodeTelemetryHistory: async () => {
      if (fail) throw new Error("Control API returned 503: history unavailable");
      return history();
    },
  } as unknown as ControlApi;
  render(<NodeDetail api={control} node={node()} now={NOW} onClose={() => undefined}/>);

  expect(await screen.findByRole("alert")).toHaveTextContent("history unavailable");
  expect(screen.getByRole("complementary", {name: "Spark One details"})).toHaveTextContent("Telemetry delivery is delayed.");
  await userEvent.click(screen.getByText("Technical details"));
  expect(screen.getByText(node().id)).toBeVisible();
  expect(screen.getByText("valid")).toBeVisible();
  expect(screen.getByText("Seen 1 second ago")).toHaveAttribute("title");

  fail = false;
  await userEvent.click(screen.getByRole("button", {name: "Retry history"}));
  expect(await screen.findByRole("img", {name: "Spark One GPU utilization history"})).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("keeps the immutable identity in copyable technical details", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {configurable: true, value: {writeText}});
  const control = {nodeTelemetryHistory: async () => history()} as unknown as ControlApi;
  render(<NodeDetail api={control} node={node()} now={NOW} onClose={() => undefined}/>);

  expect(screen.queryByText(node().id)).not.toBeVisible();
  await userEvent.click(screen.getByText("Technical details"));
  await userEvent.click(screen.getByRole("button", {name: "Copy node ID"}));

  expect(writeText).toHaveBeenCalledWith(node().id);
  expect(await screen.findByText("Copied")).toBeVisible();
});

test("does not present the previous range as the newly selected history", async () => {
  let calls = 0;
  let resolveNext!: (value: TelemetryHistory) => void;
  const nextHistory = new Promise<TelemetryHistory>(resolve => { resolveNext = resolve; });
  const control = {
    nodeTelemetryHistory: async () => ++calls === 1 ? history() : nextHistory,
  } as unknown as ControlApi;
  render(<NodeDetail api={control} node={node()} now={NOW} onClose={() => undefined}/>);
  expect(await screen.findByRole("img", {name: "Spark One GPU utilization history"})).toBeVisible();

  await userEvent.click(screen.getByRole("button", {name: "24 hours"}));

  expect(screen.getByText("Loading bounded telemetry history…")).toBeVisible();
  expect(screen.queryByRole("img", {name: "Spark One GPU utilization history"})).not.toBeInTheDocument();
  resolveNext(history("2026-08-14T12:00:00.000Z"));
  expect(await screen.findByRole("img", {name: "Spark One GPU utilization history"})).toBeVisible();
});

test("selects an honest rollup resolution for long history windows", async () => {
  const calls: Array<{resolution: string; maximum: number; start: string; end: string}> = [];
  const control = {
    nodeTelemetryHistory: async (_nodeId: string, start: string, end: string, resolution: string, maximum: number) => {
      calls.push({end, maximum, resolution, start});
      return history(start, end);
    },
  } as unknown as ControlApi;
  render(<NodeDetail api={control} node={node()} now={NOW} onClose={() => undefined}/>);
  await screen.findByRole("img", {name: "Spark One GPU utilization history"});

  await userEvent.click(screen.getByRole("button", {name: "7 days"}));
  await waitFor(() => expect(calls).toHaveLength(2));
  expect(calls[1]).toMatchObject({resolution: "fifteen-minute", maximum: 672});
  expect(screen.getByText(/15-minute buckets across the full 7-day window/i)).toBeVisible();

  await userEvent.click(screen.getByRole("button", {name: "1 year"}));
  await waitFor(() => expect(calls).toHaveLength(3));
  expect(calls[2]).toMatchObject({resolution: "fifteen-minute", maximum: 1500});
  expect(screen.getByText(/newest 1,500 15-minute buckets within 1 year/i)).toBeVisible();
});

test("refreshes selected history after live telemetry without stealing focus", async () => {
  const calls: Array<{resolution: string; maximum: number; start: string; end: string}> = [];
  const control = {
    nodeTelemetryHistory: async (_nodeId: string, start: string, end: string, resolution: string, maximum: number) => {
      calls.push({end, maximum, resolution, start});
      return history(start, end);
    },
  } as unknown as ControlApi;
  const initial = node();
  const view = render(<NodeDetail api={control} node={initial} now={NOW} onClose={() => undefined}/>);
  await screen.findByRole("img", {name: "Spark One GPU utilization history"});

  await userEvent.click(screen.getByRole("button", {name: "7 days"}));
  await waitFor(() => expect(calls).toHaveLength(2));
  expect(screen.getByRole("button", {name: "7 days"})).toHaveFocus();

  const refreshed = node();
  refreshed.telemetry = {
    age_seconds: 0,
    freshness: "live",
    sample: history().points[1] as Extract<TelemetryHistory["points"][number], {id: string}>,
  };
  view.rerender(<NodeDetail api={control} node={refreshed} now={new Date("2026-08-15T12:01:00Z")} onClose={() => undefined}/>);

  await waitFor(() => expect(calls).toHaveLength(3));
  expect(calls[2]).toMatchObject({resolution: "fifteen-minute", maximum: 672});
  expect(screen.getByRole("button", {name: "7 days"})).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", {name: "7 days"})).toHaveFocus();
  view.unmount();
});

test("separates complete and healthy recipe evidence from transitional group state", async () => {
  const projected = node();
  projected.installed = [{
    installation_id: "install-complete", recipe_id: "recipe-1", recipe_revision_id: "revision-1", title: "Qwen pair", topology_name: "pair", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [projected.id, "node-b"], rank: 0, role: "leader", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
  }, {
    installation_id: "install-partial", recipe_id: "recipe-2", recipe_revision_id: "revision-2", title: "Vision pair", topology_name: "pair", expected_rank_count: 2, present_ranks: [0], member_node_ids: [projected.id], rank: 0, role: "leader", rank_state: "installing", group_state: "partial", complete: false, degraded_reason: "missing-ranks",
  }];
  projected.loaded = [{
    run_id: "run-healthy", installation_id: "install-complete", recipe_id: "recipe-1", recipe_revision_id: "revision-1", title: "Qwen pair", alias: "chat", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [projected.id, "node-b"], rank: 0, role: "leader", rank_state: "running", rank_age_seconds: 2, rank_fresh: true, run_state: "running", route_state: "published", group_state: "healthy", healthy: true, degraded_reason: null,
  }, {
    run_id: "run-degraded", installation_id: "install-partial", recipe_id: "recipe-2", recipe_revision_id: "revision-2", title: "Vision pair", alias: "vision", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [projected.id, "node-b"], rank: 0, role: "leader", rank_state: "stopping", rank_age_seconds: 2, rank_fresh: true, run_state: "stopping", route_state: "failed", group_state: "degraded", healthy: false, degraded_reason: "route-not-published",
  }];
  const control = {nodeTelemetryHistory: async () => history()} as unknown as ControlApi;

  render(<NodeDetail api={control} node={projected} now={NOW} onClose={() => undefined}/>);
  await screen.findByRole("img", {name: "Spark One GPU utilization history"});

  const installed = screen.getByRole("region", {name: "Installed recipes in Spark One details"});
  const installationState = screen.getByRole("region", {name: "Installation state in Spark One details"});
  const loaded = screen.getByRole("region", {name: "Loaded recipes in Spark One details"});
  const runState = screen.getByRole("region", {name: "Run state in Spark One details"});
  expect(installed).toHaveTextContent("Qwen pair");
  expect(installed).not.toHaveTextContent("Vision pair");
  expect(installationState).toHaveTextContent("Partial · 1 of 2 ranks · missing ranks");
  expect(installationState).toHaveTextContent("Group partial · Rank installing");
  expect(loaded).toHaveTextContent("Healthy · 2 of 2 ranks");
  expect(loaded).not.toHaveTextContent("Vision pair");
  expect(runState).toHaveTextContent("Degraded · 2 of 2 ranks · route not published");
  expect(runState).toHaveTextContent("Group degraded · Run stopping · Rank stopping · Route failed");
});

test("surfaces rich telemetry by scope with workload placement and unsupported evidence", async () => {
  const sample = history().points[0] as RichTelemetryPoint;
  sample.metrics = {
    schema_version: 2,
    series: [
      {key: "gpu.clock_sm_mhz", scope: "accelerator", device_id: "0", value: 1420, unit: "MHz", source: "nvidia-smi", measurement_kind: "measured", observed_at: sample.observed_at, received_at: sample.received_at, freshness: "fresh", freshness_threshold_seconds: 6, support_status: "available", reason: null, aggregation: "latest"},
      {key: "network.receive_bytes_per_second", scope: "network", interface_name: "eth0", value: 2400, unit: "bytes/s", source: "procfs", measurement_kind: "derived", observed_at: sample.observed_at, received_at: sample.received_at, freshness: "fresh", freshness_threshold_seconds: 20, support_status: "available", reason: null, aggregation: "counter_rate"},
    ],
    capabilities: [{key: "gpu.throttle_active", scope: "accelerator", device_id: "0", unit: "boolean", source: "nvidia-smi", measurement_kind: "derived", supported: false, freshness_threshold_seconds: 6, reason: "The collector cannot read throttle state on this driver."}],
    runtimes: [],
    workloads: [],
    provenance: {collector: "spark-agent", collector_version: "2.1.0", host_uptime_seconds: 7200, source_observed_at: sample.observed_at},
  };
  const current: TelemetryCurrentResponse = {schema_version: 2, node_id: node().id, observed_at: sample.observed_at, received_at: sample.received_at, freshness: "live", sample};
  const capabilities: TelemetryCapabilitiesResponse = {schema_version: 2, node_id: node().id, observed_at: sample.observed_at, received_at: sample.received_at, freshness: "live", capabilities: sample.metrics.capabilities};
  const workloads: TelemetryWorkloadsResponse = {
    schema_version: 2, node_id: node().id, observed_at: sample.observed_at, received_at: sample.received_at, freshness: "live",
    runtimes: [{run_id: "run-chat", engine_id: "engine-1", backend: "vllm", version: "0.9", endpoint: "https://spark-one.local/chat", model: "Qwen 3.5", model_version: "qwen-3-5", recipe_revision: "revision-chat", context_limit_tokens: 32768, serving_node_ids: [node().id, "spark-two"], ranks: [0, 1], readiness: "running", error: null, adapter: "openai-chat", adapter_version: "1", adapter_supported: true, adapter_reason: null}],
    workloads: [{request_id: "request-1", job_id: null, run_id: "run-chat", model: "Qwen 3.5", recipe_revision: "revision-chat", engine_id: "engine-1", state: "running", origin_node_id: node().id, executor_node_ids: [node().id, "spark-two"], created_at: sample.observed_at, started_at: sample.observed_at, ended_at: null, elapsed_seconds: 2.5, failure: null, title: "Chat request", progress_value: null, progress_max: null, eta_seconds: null, eta_source: null}],
  };
  const currentEndpoint = vi.fn(async () => current);
  const capabilitiesEndpoint = vi.fn(async () => capabilities);
  const workloadsEndpoint = vi.fn(async () => workloads);
  const control = {nodeTelemetryHistory: async () => history(), nodeTelemetryCurrent: currentEndpoint, nodeTelemetryCapabilities: capabilitiesEndpoint, nodeTelemetryWorkloads: workloadsEndpoint} as unknown as ControlApi;
  const user = userEvent.setup();
  render(<NodeDetail api={control} node={node()} now={NOW} onClose={() => undefined}/>);

  await user.click(screen.getByRole("tab", {name: "Metrics"}));
  expect((await screen.findAllByText("Gpu · Clock Sm Mhz"))[0]).toBeVisible();
  expect(screen.getAllByText("GPU 0").length).toBeGreaterThan(0);
  expect((await screen.findAllByText("Network · Receive Bytes Per Second"))[0]).toBeVisible();
  expect((await screen.findAllByText("Unsupported"))[0]).toBeVisible();
  expect(screen.getByText(/spark-agent 2.1.0/)).toBeVisible();
  expect(currentEndpoint).toHaveBeenCalledWith(node().id, expect.any(AbortSignal));
  expect(capabilitiesEndpoint).toHaveBeenCalledWith(node().id, expect.any(AbortSignal));

  await user.click(screen.getByRole("tab", {name: "Workloads"}));
  expect((await screen.findAllByText("Qwen 3.5"))[0]).toBeVisible();
  expect(screen.getByText(/ranks 0, 1/)).toBeVisible();
  expect(screen.getByText("Chat request")).toBeVisible();
  expect(workloadsEndpoint).toHaveBeenCalledWith(node().id, undefined, undefined, expect.any(AbortSignal));

  await user.click(screen.getByRole("tab", {name: "Services"}));
  expect(await screen.findByText("openai-chat")).toBeVisible();
  await user.click(screen.getByRole("tab", {name: "Events"}));
  expect(await screen.findByText(/Telemetry: Live/)).toBeVisible();
});

test("matches typed history identities and renders runtime history absent from current", async () => {
  const typedHistory = history();
  typedHistory.points.forEach((point, index) => {
    if ("temperature_c" in point) point.temperature_c = 90 + index;
  });
  const source = typedHistory.points[1] as RichTelemetryPoint;
  const currentPoint: RichTelemetryPoint = {
    ...source,
    metrics: {
      ...source.metrics!,
      series: source.metrics!.series.filter(series => series.key !== "runtime.itl_p95_ms"),
    },
  };
  const current: TelemetryCurrentResponse = {schema_version: 2, node_id: node().id, observed_at: currentPoint.observed_at, received_at: currentPoint.received_at, freshness: "live", sample: currentPoint};
  const capabilities: TelemetryCapabilitiesResponse = {schema_version: 2, node_id: node().id, observed_at: currentPoint.observed_at, received_at: currentPoint.received_at, freshness: "live", capabilities: []};
  const currentEndpoint = vi.fn(async () => current);
  const capabilitiesEndpoint = vi.fn(async () => capabilities);
  const control = {
    nodeTelemetryHistory: async () => typedHistory,
    nodeTelemetryCurrent: currentEndpoint,
    nodeTelemetryCapabilities: capabilitiesEndpoint,
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<NodeDetail api={control} node={node()} now={NOW} onClose={() => undefined}/>);

  await user.click(screen.getByRole("tab", {name: "Metrics"}));
  const cpuChart = await screen.findByRole("img", {name: /Cpu · Temperature C Node aggregate history for 1 hour/});
  expect(cpuChart.querySelector("path")).toHaveAttribute("d", sparklinePath([40, 42], 100, 32, [0, 100]));
  expect(cpuChart.querySelector("path")?.getAttribute("d")).not.toBe(sparklinePath([90, 91], 100, 32, [0, 100]));

  expect(await screen.findByRole("img", {name: /Runtime · Requests Waiting Run run-chat history for 1 hour/})).toBeVisible();
  expect(await screen.findByRole("img", {name: /Runtime · Ttft P95 Ms Run run-chat history for 1 hour/})).toBeVisible();
  expect(await screen.findByRole("img", {name: /Runtime · Decode Tokens Per Second Run run-chat history for 1 hour/})).toBeVisible();
  expect(await screen.findByRole("img", {name: /Runtime · Itl P95 Ms Run run-chat history for 1 hour/})).toBeVisible();

  let exportedBlob: Blob | undefined;
  vi.stubGlobal("URL", {
    createObjectURL: (blob: Blob) => { exportedBlob = blob; return "blob:telemetry-fixture"; },
    revokeObjectURL: () => undefined,
  });
  const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  try {
    await user.click(screen.getByRole("button", {name: "Export JSON"}));
    expect(exportedBlob).toBeDefined();
    const exported = JSON.parse(await exportedBlob!.text()) as {history: TelemetryHistory | null};
    expect(exported.history).toMatchObject({resolution: "raw", points: expect.arrayContaining([
      expect.objectContaining({metrics: expect.objectContaining({series: expect.arrayContaining([
        expect.objectContaining({key: "runtime.itl_p95_ms", scope: "runtime", run_id: "run-chat", unit: "ms"}),
      ])})}),
    ])});
  } finally {
    anchorClick.mockRestore();
    vi.unstubAllGlobals();
  }

  const unavailableCpu = await screen.findByRole("img", {name: /Cpu · Utilization Percent Node aggregate history for 1 hour unavailable/});
  expect(unavailableCpu).toHaveTextContent("No adjacent historical samples");
  expect(unavailableCpu.querySelector("svg")).toBeNull();
  expect(currentEndpoint).toHaveBeenCalledWith(node().id, expect.any(AbortSignal));
  expect(capabilitiesEndpoint).toHaveBeenCalledWith(node().id, expect.any(AbortSignal));
});
