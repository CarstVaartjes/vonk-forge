import { render, screen, within } from "@testing-library/react";
import { FleetPulse } from "./fleet-pulse";
import type { MetricsSeriesResponse, VisualFleetNode } from "../api/types";

const GIB = 1024 ** 3;

function spark(id: string, sample: Record<string, unknown>, online = true) {
  return {
    id,
    display_name: id,
    hostname: id,
    labels: {},
    connection: { online_state: online ? "online" : "offline" },
    telemetry: online
      ? { freshness: "live", sample: { memory_total_bytes: 120 * GIB, ...sample } }
      : null,
    loaded: [],
  } as unknown as VisualFleetNode;
}
const fleet = [
  spark("a", {
    memory_available_bytes: 12 * GIB,
    gpu_process_memory_bytes: 100 * GIB,
    system_memory_bytes: 8 * GIB,
    gpu_power_watts: 38,
  }),
  spark("b", {
    memory_available_bytes: 16 * GIB,
    gpu_process_memory_bytes: 98 * GIB,
    system_memory_bytes: 6 * GIB,
    gpu_power_watts: 39,
  }),
  spark("c", {}, false),
];
const tokens = {
  spec: {
    metric: "output_token_rate",
    title: "Generated tokens per model",
    unit: "tokens_per_second",
    basis: "estimated",
  },
  series: [
    { labels: {}, points: [], summary: { total: 1_840_000 } },
    { labels: { alias: "coder" }, points: [], summary: { total: 1_000_000 } },
    { labels: { alias: "fast" }, points: [], summary: { total: 840_000 } },
  ],
} as unknown as MetricsSeriesResponse;
const tile = (name: string) => screen.getByRole("region", { name });

test("the band sums the online Sparks' readings and shows the Controller's fleet token total", () => {
  render(<FleetPulse tokens={tokens} nodes={fleet} />);
  expect(tile("GPU power")).toHaveTextContent("77.0W2 Sparks");
  expect(tile("Unified memory")).toHaveTextContent("212.0GiB of 240.0 GiB198.0 GiB in models");
  expect(
    within(tile("Unified memory")).getByRole("meter", { name: "Fleet unified memory" }),
  ).toHaveAttribute(
    "aria-valuetext",
    "Models 198.0 GiB, System 14.0 GiB, Free 28.0 GiB of 240.0 GiB",
  );
  expect(within(tile("Tokens, 24 h")).getByText("1.84")).toBeInTheDocument();
  expect(tile("Tokens, 24 h")).toHaveTextContent("≈1.84M generated");
  expect(tile("Output now")).toHaveTextContent("Not reported");
  expect(tile("GPU energy, 24 h")).toHaveTextContent("Not reported");
});

test("the 8 GiB warning is a Spark's: a fleet sum of low free memory shows no warning", () => {
  const low = [
    spark("a", {
      memory_available_bytes: 3 * GIB,
      gpu_process_memory_bytes: 100 * GIB,
      system_memory_bytes: 8 * GIB,
    }),
    spark("b", {
      memory_available_bytes: 3 * GIB,
      gpu_process_memory_bytes: 100 * GIB,
      system_memory_bytes: 8 * GIB,
    }),
  ];
  render(<FleetPulse tokens={null} nodes={low} />);
  expect(screen.queryByText(/Under \d GiB free/)).toBeNull();
});

test("without readings every figure says Not reported", () => {
  render(<FleetPulse tokens={null} nodes={[spark("c", {}, false)]} />);
  for (const name of ["GPU power", "Unified memory", "Tokens, 24 h"])
    expect(tile(name)).toHaveTextContent("Not reported");
});
