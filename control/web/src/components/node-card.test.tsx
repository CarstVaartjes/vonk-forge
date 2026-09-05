import {render, screen, within} from "@testing-library/react";
import type {TelemetryHistory, VisualFleetNode} from "../api/types";
import {NodeCard} from "./node-card";

const GIB = 1024 ** 3;
const NOW = new Date("2026-08-15T12:00:00Z");

function completeNode(): VisualFleetNode {
  return {
    id: "spk_0123456789abcdef0123456789abcdef",
    display_name: "Spark One",
    hostname: "spark-one.internal",
    lifecycle: "managed",
    labels: {role: "inference", rack: "left"},
    connection: {agent_state: "active", certificate_state: "valid", online_state: "online", offline_reason: null, last_seen_at: "2026-08-15T11:59:59Z", last_seen_age_seconds: 1},
    inventory: {
      observed_at: "2026-08-15T11:59:50Z", received_at: "2026-08-15T11:59:51Z", age_seconds: 10, freshness: "fresh",
      disk_total_bytes: 200 * GIB, disk_free_bytes: 120 * GIB,
      host_memory_total_bytes: 100 * GIB, host_memory_free_bytes: 80 * GIB,
      gpu_memory_total_bytes: 100 * GIB, gpu_memory_free_bytes: 70 * GIB,
      gpu_count: 1, artifact_store_read_only: false, capabilities: ["runtime.vonk.v1"], fabric_address: "10.0.0.1", fabric_bandwidth_mbps: 100_000, nvidia_driver_version: "580.1", container_runtime_version: "1.2.3",
    },
    telemetry: {
      age_seconds: 2, freshness: "live", sample: {
        id: "00000000-0000-4000-8000-000000000001", node_id: "spk_0123456789abcdef0123456789abcdef", boot_id: "00000000-0000-0000-0000-000000000001", sequence: 2,
        observed_at: "2026-08-15T11:59:58Z", received_at: "2026-08-15T11:59:59Z",
        cpu_utilization_percent: 12.5, load_average_1m: 1.5,
        memory_total_bytes: 100 * GIB, memory_available_bytes: 80 * GIB,
        disk_total_bytes: 200 * GIB, disk_free_bytes: 120 * GIB,
        gpu_utilization_percent: 73, gpu_memory_total_bytes: 100 * GIB, gpu_memory_free_bytes: 70 * GIB,
        temperature_c: 42.5, power_watts: 18.25,
        network_receive_bytes_per_second: 1024, network_transmit_bytes_per_second: 512,
        gap_samples: 0, details: {accelerator_name: "NVIDIA GB10", accelerator_performance_state: "P0"},
      },
    },
    installed: [{
      installation_id: "install-1", recipe_id: "recipe-1", recipe_revision_id: "revision-1", title: "Qwen pair", topology_name: "pair", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: ["node-a", "node-b"], rank: 0, role: "leader", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
    }, {
      installation_id: "install-2", recipe_id: "recipe-2", recipe_revision_id: "revision-2", title: "Vision pair", topology_name: "pair", expected_rank_count: 2, present_ranks: [0], member_node_ids: ["node-a"], rank: 0, role: "leader", rank_state: "installed", group_state: "partial", complete: false, degraded_reason: "missing-ranks",
    }],
    loaded: [{
      run_id: "run-1", installation_id: "install-1", recipe_id: "recipe-1", recipe_revision_id: "revision-1", title: "Qwen pair", alias: "chat", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: ["node-a", "node-b"], rank: 0, role: "leader", rank_state: "running", rank_age_seconds: 2, rank_fresh: true, run_state: "running", route_state: "published", group_state: "healthy", healthy: true, degraded_reason: null,
    }, {
      run_id: "run-2", installation_id: "install-2", recipe_id: "recipe-2", recipe_revision_id: "revision-2", title: "Vision pair", alias: "vision", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: ["node-a", "node-b"], rank: 0, role: "leader", rank_state: "running", rank_age_seconds: 2, rank_fresh: true, run_state: "running", route_state: "failed", group_state: "degraded", healthy: false, degraded_reason: "route-not-published",
    }],
    reservations: {disk_bytes: 2 * GIB, unified_memory_bytes: GIB, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 1},
    warnings: [{code: "run.degraded", detail: "Vision pair route is not published.", severity: "error"}],
  };
}

test("renders the complete node telemetry hierarchy and distinct recipe groups", () => {
  render(<NodeCard node={completeNode()} now={NOW} selected={false} onEdit={() => undefined} onSelect={() => undefined}/>);
  const card = screen.getByRole("article", {name: "Spark One — Live"});

  expect(within(card).getByText("NVIDIA GB10 · P0")).toBeVisible();
  expect(within(card).getByText("73%" )).toBeVisible();
  expect(within(card).getByRole("meter", {name: "Unified memory in use"})).toHaveValue(30 * GIB);
  expect(within(card).getAllByText("70.0 GiB available of 100.0 GiB")[0]).toBeVisible();
  expect(within(card).queryByText("Host memory")).not.toBeInTheDocument();
  expect(within(card).queryByText("GPU memory")).not.toBeInTheDocument();
  expect(within(card).getByText("120.0 GiB free / 200.0 GiB")).toBeVisible();
  expect(within(card).getByText("12.5% · load 1.50")).toBeVisible();
  expect(within(card).getByText("42.5 °C")).toBeVisible();
  expect(within(card).getByText("18.3 W")).toBeVisible();
  expect(within(card).getByText("↓ 1.0 KiB/s · ↑ 512 B/s")).toBeVisible();
  expect(within(card).getByText("Updated 2 seconds ago")).toBeVisible();
  expect(within(card).getByText("Updated 2 seconds ago")).toHaveAttribute("title");

  const workloads = within(card).getByLabelText("Workloads on Spark One");
  expect(workloads).toHaveTextContent("Current work");
  expect(workloads).toHaveTextContent("Local recipes");
  expect(within(workloads).getAllByText("Qwen pair")).toHaveLength(2);
  expect(within(workloads).getAllByText("Vision pair")).toHaveLength(2);
  expect(workloads).toHaveTextContent("vision · degraded");
  expect(workloads).toHaveTextContent("partial");
  expect(within(card).getByText("Vision pair route is not published.")).toBeVisible();
});

test("renders offline certificate reasons and absent metrics honestly", () => {
  const node = completeNode();
  node.connection = {...node.connection, certificate_state: "expired", online_state: "offline", offline_reason: "certificate-expired"};
  node.telemetry = null;
  node.inventory = null;
  node.installed = [];
  node.loaded = [];
  node.warnings = [];

  render(<NodeCard node={node} now={NOW} selected={false} onEdit={() => undefined} onSelect={() => undefined}/>);
  const card = screen.getByRole("article", {name: "Spark One — Offline"});

  expect(within(card).getByText("Certificate expired")).toBeVisible();
  expect(within(card).getAllByText("Not reported").length).toBeGreaterThanOrEqual(6);
  expect(within(card).queryByText("0.0%")).not.toBeInTheDocument();
  expect(within(card).getByText("No local recipe reported")).toBeVisible();
  expect(within(card).getByText("No active model reported")).toBeVisible();
});

test("uses a friendly hostname fallback and renders an accessible live trend", () => {
  const projected = completeNode();
  projected.display_name = projected.id;
  projected.hostname = "mia-lab-west.internal";

  const history: TelemetryHistory = {
    schema_version: 1,
    node_id: projected.id,
    start: "2026-08-15T11:00:00Z",
    end: "2026-08-15T12:00:00Z",
    resolution: "raw",
    maximum_points: 60,
    points: [18, 42].map((gpu, index) => ({...projected.telemetry!.sample, id: `00000000-0000-4000-8000-00000000000${index + 2}`, sequence: index + 3, gpu_utilization_percent: gpu})),
  };
  render(<NodeCard node={projected} now={NOW} selected={false} history={history} onEdit={() => undefined} onSelect={() => undefined}/>);

  const card = screen.getByRole("article", {name: "Mia Lab West — Live"});
  expect(within(card).getByRole("heading", {name: "Mia Lab West"})).toBeVisible();
  expect(within(card).queryByText(projected.id)).not.toBeInTheDocument();
  expect(within(card).getByRole("img", {name: "GPU 24h trend"})).toHaveAccessibleDescription(/Latest 73%/);
  expect(within(card).getByRole("button", {name: "Edit Mia Lab West"})).toBeVisible();
});
