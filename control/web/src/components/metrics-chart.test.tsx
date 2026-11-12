import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ControlApi, MetricsSeriesResponse } from "../api/types";
import { MetricsChart, SeriesChart } from "./metrics-chart";

const data: MetricsSeriesResponse = {
  metric: "gpu_utilization",
  range: "6h",
  start: 100,
  end: 200,
  step_seconds: 15,
  series: [
    {
      labels: { node_id: "Spark A" },
      points: [
        { timestamp: 100, value: 20 },
        { timestamp: 200, value: 80 },
      ],
    },
  ],
};

test("renders the measured series, axes and identity instead of an empty chart", () => {
  const { container } = render(
    <SeriesChart data={data} unit="%" names={{ "Spark A": "Kitchen" }} />,
  );
  expect(screen.getByRole("img", { name: "Time series in %" })).toBeVisible();
  expect(container.querySelector('path[stroke-width="2"]')).toHaveAttribute("d", "M50,125 L580,20");
  expect(screen.getByText("Kitchen · %")).toBeVisible();
});

const stable = (value: number): MetricsSeriesResponse => ({
  ...data,
  series: [
    {
      labels: { node_id: "spk_a" },
      points: [
        { timestamp: 100, value },
        { timestamp: 200, value },
      ],
    },
  ],
});

test("shows memory in GiB with Spark names and the certificate as a time left", async () => {
  // Break caught: raw bytes or node ids reach the page, or the certificate stays a chart of seconds.
  const metricsSeries = vi.fn(async (metric: string) =>
    metric === "host_memory_used"
      ? stable(64 * 1024 ** 3)
      : metric === "certificate_expiry"
        ? stable(12 * 86400 + 600)
        : stable(10),
  );
  render(
    <MetricsChart api={{ metricsSeries } as unknown as ControlApi} names={{ spk_a: "Kitchen" }} />,
  );

  const memory = await screen.findByRole("region", { name: "Unified memory used" });
  await within(memory).findByText("Kitchen · GiB");
  expect(memory).toHaveTextContent("64 GiB");
  expect(await screen.findByText("Kitchen: certificate expires in 12 days")).toBeVisible();
  expect(screen.getByRole("region", { name: "Requests per model" })).toBeVisible();
});

test("an empty series says so and one failed tile leaves the others", async () => {
  // Break caught: a failed or empty read blanks every tile.
  const metricsSeries = vi.fn(async (metric: string) => {
    if (metric === "gpu_temperature") throw new Error("Temporarily unavailable");
    return metric === "gpu_utilization" ? { ...data, series: [] } : stable(40);
  });
  render(<MetricsChart api={{ metricsSeries } as unknown as ControlApi} />);

  const gpu = await screen.findByRole("region", { name: "GPU utilisation" });
  await within(gpu).findByText("No samples in this range");
  const temperature = screen.getByRole("region", { name: "GPU temperature" });
  await within(temperature).findByText(/Metrics unavailable: Temporarily unavailable/);
  await within(screen.getByRole("region", { name: "Unified memory used" })).findByRole("img");
});

test("one range switch re-reads every tile for the Spark", async () => {
  const metricsSeries = vi.fn(async () => data);
  render(<MetricsChart api={{ metricsSeries } as unknown as ControlApi} node="spk_abc" />);
  await screen.findAllByRole("img");
  fireEvent.change(screen.getByRole("combobox", { name: "Range" }), { target: { value: "24h" } });
  await waitFor(() =>
    expect(metricsSeries).toHaveBeenCalledWith(
      "gpu_temperature",
      "24h",
      "spk_abc",
      expect.any(AbortSignal),
    ),
  );
  expect(metricsSeries).not.toHaveBeenCalledWith(
    "request_rate",
    expect.anything(),
    "spk_abc",
    expect.anything(),
  );
});
