import { render, screen, within } from "@testing-library/react";
import { EnergyCard } from "./energy-card";
import { TokensCard } from "./tokens-card";
import type { MetricsSeriesResponse, VisualFleetNode } from "../api/types";

const tokens = {
  spec: {
    metric: "output_token_rate",
    title: "Generated tokens per model",
    unit: "tokens_per_second",
    basis: "measured",
    hint: "Counted by the gateway; a Spark shows the models it serves.",
  },
  series: [
    { labels: {}, points: [], summary: { total: 1_840_000, share: null, coverage: 1 } },
    {
      labels: { alias: "fast" },
      points: [],
      summary: { total: 320_000, share: 0.17, coverage: 1 },
    },
    {
      labels: { alias: "coder" },
      points: [],
      summary: { total: 1_520_000, share: 0.83, coverage: 1 },
    },
  ],
} as unknown as MetricsSeriesResponse;
const nodes = [
  { id: "a", loaded: [{ alias: "coder", title: "GLM 5.3 Flash" }] },
] as unknown as VisualFleetNode[];

test("the Tokens card shows the total, the Controller's hint, and each model's share, largest first", () => {
  render(<TokensCard tokens={tokens} nodes={nodes} />);
  const card = screen.getByRole("region", { name: "Tokens" });
  expect(card).toHaveTextContent("1.84 Mgenerated");
  expect(card).toHaveTextContent("Counted by the gateway; a Spark shows the models it serves.");
  const rows = within(card).getAllByRole("row").slice(1);
  expect(rows[0]).toHaveTextContent("coderGLM 5.3 Flash1.52 M83%");
  expect(rows[1]).toHaveTextContent("fast320 k17%");
  expect(
    within(card).getByRole("meter", { name: "coder share of generated tokens" }),
  ).toHaveAttribute("aria-valuenow", "83");
});

test("without a token reading the Tokens card says Not reported and draws no table", () => {
  render(<TokensCard tokens={null} nodes={nodes} />);
  expect(screen.getByRole("region", { name: "Tokens" })).toHaveTextContent("Not reported");
  expect(screen.queryByRole("table")).toBeNull();
});

test("an empty range says No data in this range and a partial one says how much it covers", () => {
  const empty = { ...tokens, series: [] } as unknown as MetricsSeriesResponse;
  const { unmount } = render(<TokensCard tokens={empty} nodes={nodes} />);
  expect(screen.getByText("No data in this range")).toBeInTheDocument();
  unmount();
  const partial = {
    ...tokens,
    series: [
      { labels: {}, points: [], summary: { total: 1_000, share: null, coverage: 0.5 } },
      {
        labels: { alias: "coder" },
        points: [],
        summary: { total: 1_000, share: 1, coverage: 0.5 },
      },
    ],
  } as unknown as MetricsSeriesResponse;
  render(<TokensCard tokens={partial} nodes={nodes} />);
  expect(screen.getByText(/Data covers 50% of this range\./)).toBeInTheDocument();
});

test("the GPU energy card is named GPU energy, shows the Controller's total as an estimate and power now", () => {
  const response = (spec: Record<string, unknown>, line: Record<string, unknown>) =>
    ({
      spec,
      series: [{ labels: {}, points: [], summary: line }],
    }) as unknown as MetricsSeriesResponse;
  const energy = response(
    { basis: "estimated", unit: "watt_hours", hint: "GPU only. Estimate." },
    { latest: 77, total: 1500 },
  );
  const power = response({ basis: "measured", unit: "watts" }, { latest: 77 });
  const { unmount } = render(<EnergyCard energy={energy} power={power} />);
  const card = screen.getByRole("region", { name: "GPU energy" });
  expect(card).toHaveTextContent("≈1.50 kWhest.");
  expect(card).toHaveTextContent("GPU power now77.0 W");
  unmount();
  render(<EnergyCard energy={null} power={null} />);
  expect(screen.getByRole("region", { name: "GPU energy" })).toHaveTextContent("Not reported");
  expect(screen.getByText("GPU only, not the whole Spark.")).toBeInTheDocument();
});
