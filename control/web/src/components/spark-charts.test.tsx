import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ControlApi, MetricSpec } from "../api/types";
import { SparkCharts } from "./spark-charts";

const spec = (
  metric: MetricSpec["metric"],
  title: string,
  unit: MetricSpec["unit"],
): MetricSpec => ({
  metric,
  title,
  unit,
  scope: "node",
  basis: "measured",
  source: "spark",
  summary_total: false,
  fleet_sum: false,
  hint: "",
});
const SPECS = [
  spec("gpu_temperature", "GPU temperature", "celsius"),
  spec("gpu_utilization", "GPU utilisation", "percent"),
  spec("request_rate", "Requests per model", "per_second"),
];
const reply = (metric: string) => ({
  metric,
  spec: SPECS.find((entry) => entry.metric === metric),
  range: "24h",
  start: 0,
  end: 100,
  step_seconds: 50,
  series: [
    {
      labels: { node_id: "spk_1" },
      points: [
        { timestamp: 0, value: 60 },
        { timestamp: 50, value: 69 },
      ],
      summary: { latest: 69, average: 64.5, peak: 70, total: null, coverage: 1 },
    },
  ],
});

test("a Spark's grid draws the listed Spark metrics in order, with the Controller's now and summary", async () => {
  const metricsSeries = vi.fn(async (metric: string) => reply(metric));
  const api = { metricsCatalogue: async () => SPECS, metricsSeries } as unknown as ControlApi;
  render(<SparkCharts api={api} nodeId="spk_1" range="24h" color="var(--series-1)" />);
  const temperature = await screen.findByRole("region", { name: "GPU temperature" });
  expect(await within(temperature).findByText("24 h peak 70 °C")).toBeVisible();
  expect(temperature).toHaveTextContent("69 °C");
  expect(temperature).toHaveTextContent("warns at 85 °C");
  const titles = screen.getAllByRole("region").map((card) => card.getAttribute("aria-label"));
  expect(titles).toEqual(["GPU utilisation", "GPU temperature"]);
  expect(metricsSeries).toHaveBeenCalledWith(
    "gpu_temperature",
    "24h",
    "spk_1",
    expect.any(AbortSignal),
  );
});

test("a failed read says Not reported and keeps the other cards", async () => {
  const metricsSeries = vi.fn(async (metric: string) => {
    if (metric === "gpu_temperature") throw new Error("down");
    return reply(metric);
  });
  const api = { metricsCatalogue: async () => SPECS, metricsSeries } as unknown as ControlApi;
  render(<SparkCharts api={api} nodeId="spk_1" range="24h" color="var(--series-1)" />);
  const failed = await screen.findByRole("region", { name: "GPU temperature" });
  await waitFor(() => expect(within(failed).getByRole("status")).toHaveTextContent("Not reported"));
  expect(
    await within(screen.getByRole("region", { name: "GPU utilisation" })).findByText(
      "24 h average 64.5 %",
    ),
  ).toBeVisible();
});

test("a failed catalogue read says so, and Refresh metrics reads it again and heals", async () => {
  const metricsCatalogue = vi
    .fn()
    .mockRejectedValueOnce(new Error("down"))
    .mockResolvedValue(SPECS);
  const api = {
    metricsCatalogue,
    metricsSeries: async (metric: string) => reply(metric),
  } as unknown as ControlApi;
  render(<SparkCharts api={api} nodeId="spk_1" range="24h" color="var(--series-1)" />);
  expect(await screen.findByText("Metrics unavailable: down")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Refresh metrics" }));
  expect(await screen.findByRole("region", { name: "GPU temperature" })).toBeVisible();
  expect(screen.queryByText(/Metrics unavailable/)).toBeNull();
});
