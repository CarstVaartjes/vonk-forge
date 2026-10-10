import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  const { container } = render(<SeriesChart data={data} unit="%" />);
  expect(screen.getByRole("img", { name: "Time series in %" })).toBeVisible();
  expect(container.querySelector('path[stroke-width="2"]')).toHaveAttribute("d", "M50,125 L580,20");
  expect(screen.getByText("node_id: Spark A · %")).toBeVisible();
});

test("range changes request Spark samples and a fresh read recovers from failure", async () => {
  const metricsSeries = vi
    .fn()
    .mockResolvedValueOnce(data)
    .mockRejectedValueOnce(new Error("Temporarily unavailable, retry"))
    .mockResolvedValue(data);
  render(<MetricsChart api={{ metricsSeries } as unknown as ControlApi} node="spk_abc" />);
  await screen.findByRole("img");
  expect(metricsSeries).toHaveBeenCalledWith(
    "gpu_utilization",
    "6h",
    "spk_abc",
    expect.any(AbortSignal),
  );
  fireEvent.change(screen.getByRole("combobox", { name: "Range" }), { target: { value: "24h" } });
  await screen.findByText(/Metrics unavailable: Temporarily unavailable/);
  expect(metricsSeries).toHaveBeenLastCalledWith(
    "gpu_utilization",
    "24h",
    "spk_abc",
    expect.any(AbortSignal),
  );
  expect(screen.queryByRole("img")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Refresh metrics" }));
  await screen.findByRole("img");
});

test("range change aborts observation and a late response cannot replace current samples", async () => {
  let finish: (value: MetricsSeriesResponse) => void = () => {};
  const metricsSeries = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    )
    .mockResolvedValue(data);
  render(<MetricsChart api={{ metricsSeries } as unknown as ControlApi} />);
  fireEvent.change(screen.getByRole("combobox", { name: "Range" }), { target: { value: "1h" } });
  await screen.findByRole("img");
  expect(metricsSeries.mock.calls[0]?.[3].aborted).toBe(true);
  finish({ ...data, series: [] });
  await waitFor(() => expect(screen.getByRole("img")).toBeVisible());
});
