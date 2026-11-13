import { render, screen, within } from "@testing-library/react";
import { EnergyCard } from "./energy-card";
import { TokensCard } from "./tokens-card";
import type { MetricsSeriesResponse, VisualFleetNode } from "../api/types";

const tokens = {
  spec: {
    metric: "output_token_rate",
    title: "Generated tokens per model",
    unit: "tokens_per_second",
    basis: "estimated",
    hint: "Counted by the gateway; a Spark shows the models it serves.",
  },
  series: [
    { labels: { alias: "fast" }, points: [], summary: { total: 320_000 } },
    { labels: { alias: "coder" }, points: [], summary: { total: 1_520_000 } },
  ],
} as unknown as MetricsSeriesResponse;
const nodes = [
  { id: "a", loaded: [{ alias: "coder", title: "GLM 5.3 Flash" }] },
] as unknown as VisualFleetNode[];

test("the Tokens card shows the total as an estimate, the Controller's hint, and each model's share, largest first", () => {
  render(<TokensCard tokens={tokens} nodes={nodes} />);
  const card = screen.getByRole("region", { name: "Tokens" });
  expect(card).toHaveTextContent("≈1.84 Mgeneratedest.");
  expect(card).toHaveTextContent("Counted by the gateway; a Spark shows the models it serves.");
  const rows = within(card).getAllByRole("row").slice(1);
  expect(rows[0]).toHaveTextContent("coderGLM 5.3 Flash≈1.52 M83%");
  expect(rows[1]).toHaveTextContent("fast≈320 k17%");
  expect(
    within(card).getByRole("meter", { name: "coder share of generated tokens" }),
  ).toHaveAttribute("aria-valuenow", "83");
});

test("without a token reading the Tokens card says Not reported and draws no table", () => {
  render(<TokensCard tokens={null} nodes={nodes} />);
  expect(screen.getByRole("region", { name: "Tokens" })).toHaveTextContent("Not reported");
  expect(screen.queryByRole("table")).toBeNull();
});

test("the GPU energy card is named GPU energy, says Not reported until a metric exists, and shows power now", () => {
  const live = [
    {
      connection: { online_state: "online" },
      telemetry: { freshness: "live", sample: { gpu_power_watts: 38 } },
    },
    {
      connection: { online_state: "online" },
      telemetry: { freshness: "live", sample: { gpu_power_watts: 39 } },
    },
  ] as unknown as VisualFleetNode[];
  render(<EnergyCard nodes={live} />);
  const card = screen.getByRole("region", { name: "GPU energy" });
  expect(card).toHaveTextContent("Not reported");
  expect(card).toHaveTextContent("GPU only, not the whole Spark.");
  expect(card).toHaveTextContent("GPU power now77.0 W");
});
