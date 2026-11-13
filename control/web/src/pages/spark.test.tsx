vi.mock("../components/metrics-chart", () => ({ MetricsChart: () => null }));
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SparkPage } from "./spark";
import { ProjectionCode, ProfileReasonSeverity } from "../api/vocabulary.generated";
import type { ControlApi } from "../api/types";

let nodes: unknown[] = [];
vi.mock("../hooks/use-fleet-stream", () => ({
  useFleetStream: () => ({
    snapshot: { nodes },
    now: new Date(),
    loading: false,
    error: null,
    retry: vi.fn(),
  }),
}));

/** The Spark page for `shown`, which the mocked fleet stream carries; `api` holds only what a test calls. */
function show(shown: unknown, api: object = {}, onGone = vi.fn()) {
  nodes = [shown];
  const history = { metricsSeries: vi.fn().mockRejectedValue(new Error("no history")) };
  return render(
    <SparkPage
      api={{ ...history, ...api } as unknown as ControlApi}
      id="spk_1"
      onNavigate={vi.fn()}
      onGone={onGone}
    />,
  );
}

const node = {
  id: "spk_1",
  display_name: "Kitchen",
  hostname: "kitchen.local",
  ip_address: null,
  warnings: [],
  loaded: [],
  installed: [],
  connection: { online_state: "online", last_seen_at: "2026-09-27T10:00:00Z" },
};

test("removing a Spark requires typing its name", async () => {
  const removeFleetNode = vi.fn().mockResolvedValue({ action: "remove", state: "done" });
  const onClose = vi.fn();
  show(node, { removeFleetNode }, onClose);
  fireEvent.click(await screen.findByRole("tab", { name: "Settings" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  const confirm = screen.getByRole("button", { name: "Remove Spark" });
  expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByLabelText(/Type/), { target: { value: "Kitchen" } });
  fireEvent.click(confirm);
  await waitFor(() => expect(removeFleetNode).toHaveBeenCalledWith("spk_1"));
  await waitFor(() => expect(onClose).toHaveBeenCalled());
});

test("recent logs load when the Logs tab opens", async () => {
  const fleetLogs = vi.fn().mockResolvedValue({
    entries: [
      {
        evidence_id: "e1",
        level: "error",
        source: "runtime",
        observed_at: "2026-09-27T10:00:00Z",
        message: "boom",
      },
    ],
  });
  show(node, { fleetLogs });
  fireEvent.click(await screen.findByRole("tab", { name: "Logs" }));
  expect(await screen.findByText(/boom/)).toBeVisible();
});

test("a Spark with a Controller warning is shown as needing attention with the reason", async () => {
  const warned = {
    ...node,
    warnings: [
      { code: "install.partial", detail: "Qwen is partly installed", severity: "warning" },
    ],
  };
  show(warned);
  expect(await screen.findByText("needs attention")).toBeVisible();
  expect(screen.getByText(/Qwen is partly installed/)).toBeVisible();
});

test("a running workload shows the recipe options it was started with", async () => {
  const running = {
    ...node,
    loaded: [
      {
        run_id: "r1",
        rank: 0,
        title: "GLM",
        healthy: true,
        option_choices: { verification: "adaptive-k" },
      },
    ],
  };
  show(running);
  const list = await screen.findByRole("list", { name: "Active recipe options" });
  expect(list).toHaveTextContent("adaptive-k");
});

test("a loading rank shows the swap it holds, and a running one shows none", async () => {
  const loading = {
    ...node,
    loaded: [
      { run_id: "r1", rank: 0, title: "GLM", healthy: null, load_swap_held_bytes: 35 * 1024 ** 3 },
      { run_id: "r2", rank: 0, title: "Qwen", healthy: true, load_swap_held_bytes: null },
    ],
  };
  show(loading);
  const list = await screen.findByRole("list", { name: "Swap held while loading" });
  expect(list).toHaveTextContent("GLM Swap held while loading: 35.0 GiB");
  expect(list).not.toHaveTextContent("Qwen");
});

test("the Overview shows the CPU clock next to the temperature", async () => {
  const measured = {
    ...node,
    telemetry: {
      freshness: "live",
      age_seconds: 1,
      sample: { cpu_frequency_avg_mhz: 2100, cpu_frequency_max_mhz: 3900, gpu_temperature_c: 84 },
    },
  };
  show(measured);
  expect(await screen.findByText(/2\.1 of 3\.9 GHz/)).toHaveTextContent("84 °C");
});

test("the Overview omits the CPU clock when the agent does not report it", async () => {
  show(node);
  expect(await screen.findByRole("heading", { name: "Kitchen" })).toBeVisible();
  expect(screen.queryByText("CPU clock")).toBeNull();
});

test("GPU readings distinguish hardware maximum, policy and the applied command receipt", async () => {
  const measured = {
    ...node,
    gpu_clock_desired: { min_mhz: 300, max_mhz: 1800 },
    gpu_clock_policy: { default_min_mhz: 300, default_max_mhz: 2200, hard_max_mhz: 2300 },
    telemetry: {
      freshness: "live",
      age_seconds: 1,
      sample: {
        gpu_clock: {
          graphics_mhz: 1700,
          sm_mhz: 1695,
          locked_range: { min_mhz: 300, max_mhz: 2200 },
          max_graphics_mhz: 2400,
          max_sm_mhz: 2300,
          throttle_reasons: 4,
        },
      },
    },
  };
  show(measured);
  expect(await screen.findByText(/Graphics 1700 \/ SM 1695 MHz/)).toHaveTextContent(
    "last applied 300–2200 MHz (helper receipt) · desired 300–1800 MHz",
  );
  expect(screen.getByText(/reported maximum graphics 2400/)).toHaveTextContent(
    "policy limit 2300 MHz · throttle bits 4",
  );
});

test("display reservation stays unknown and renders Controller advice", async () => {
  // Break caught: inventing a default carveout or dropping the next action.
  const detail = "Headless Spark display reserves 2147483648 bytes; minimum is 0.5 GB.";
  const recommendation =
    "Set Advanced → Advanced Menu → Display Reserved Memory to 0.5 GB (fixed); save and reboot.";
  show({
    ...node,
    warnings: [
      {
        code: ProjectionCode.HOST_DISPLAY_MEMORY,
        detail,
        recommendation,
        severity: ProfileReasonSeverity.WARNING,
      },
    ],
  });
  expect(await screen.findByText(`${detail} ${recommendation}`)).toBeVisible();
  expect(screen.getByText(/Reserved unknown · SoC unknown/)).toBeVisible();
});

test("the frame shows the Spark's own facts, or Not reported", () => {
  // Break caught: the agent version or uptime is guessed or shown blank when the Spark has not reported it.
  const first = show({
    ...node,
    agent_runtime: { semantic_version: "0.9.2" },
    telemetry: { freshness: "live", age_seconds: 1, sample: { uptime_seconds: 90_000 } },
  });
  const facts = screen.getByRole("list", { name: "Facts" });
  expect(facts).toHaveTextContent("Agent 0.9.2");
  expect(facts).toHaveTextContent("Uptime 1 d 1 h");
  first.unmount();
  show({ ...node, agent_runtime: null });
  expect(screen.getByRole("list", { name: "Facts" })).toHaveTextContent("Agent Not reported");
  expect(screen.getByRole("list", { name: "Facts" })).toHaveTextContent("Uptime Not reported");
});

test("an address that names no enrolled Spark says so", () => {
  nodes = [];
  render(<SparkPage api={{} as ControlApi} id="spk_gone" onNavigate={vi.fn()} onGone={vi.fn()} />);
  expect(screen.getByRole("heading", { name: "Spark not found" })).toBeVisible();
});

const GIB = 1024 ** 3;

test("the Overview shows the memory split and the GPU gauges from the live sample", () => {
  show({
    ...node,
    telemetry: {
      freshness: "live",
      age_seconds: 1,
      sample: {
        memory_total_bytes: 100 * GIB,
        memory_available_bytes: 10 * GIB,
        gpu_process_memory_bytes: 80 * GIB,
        system_memory_bytes: 10 * GIB,
        gpu_utilization_percent: 97,
        gpu_temperature_c: 69,
        gpu_clock: { graphics_mhz: 2200 },
      },
    },
  });
  expect(screen.getByRole("meter", { name: "Kitchen unified memory" })).toHaveAttribute(
    "aria-valuetext",
    "Models 80.0 GiB, System 10.0 GiB, Free 10.0 GiB of 100.0 GiB",
  );
  expect(screen.getByRole("meter", { name: "Temperature" })).toHaveAttribute(
    "aria-valuetext",
    "69 °C, warns at 85\u00a0°C, serious at 90\u00a0°C",
  );
  // No clock maximum is reported, so the number shows and no arc is invented.
  expect(screen.getByRole("meter", { name: "Clock" })).toHaveAttribute(
    "aria-valuetext",
    "2,200 MHz",
  );
});

test("a Spark that is not reporting live shows Not reported on every gauge", () => {
  show(node);
  for (const name of ["Utilisation", "Temperature", "Clock"])
    expect(screen.getByRole("meter", { name })).toHaveAttribute(
      "aria-valuetext",
      expect.stringMatching(/^Not reported/),
    );
});
