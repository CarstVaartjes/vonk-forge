import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ControlApi, MetricSpec, MetricsSeriesResponse } from "../api/types";
import { MetricsChart, SeriesChart } from "./metrics-chart";

const spec = (
  metric: MetricSpec["metric"],
  title: string,
  unit: MetricSpec["unit"],
  extra: Partial<MetricSpec> = {},
): MetricSpec => ({
  metric,
  title,
  unit,
  scope: "node",
  basis: "measured",
  source: "spark",
  summary_total: false,
  hint: "",
  ...extra,
});
const model = {
  scope: "alias",
  basis: "estimated",
  source: "gateway",
  summary_total: true,
} as const;
const SPECS: Record<string, MetricSpec> = {
  gpu_utilization: spec("gpu_utilization", "GPU utilisation", "percent"),
  host_memory_used: spec("host_memory_used", "Unified memory used", "bytes"),
  gpu_temperature: spec("gpu_temperature", "GPU temperature", "celsius"),
  request_rate: spec("request_rate", "Requests per model", "per_second", model),
  output_token_rate: spec("output_token_rate", "Generated tokens per model", "tokens_per_second", {
    ...model,
    hint: "Counted by the gateway.",
  }),
  certificate_expiry: spec("certificate_expiry", "Certificate expiry", "seconds"),
};
const summary = { latest: 80, average: 50, peak: 90, total: null, coverage: 1 };

/** The Controller answers with each metric's own spec; the fake does the same. */
const fakeApi = (metricsSeries: (metric: string, ...rest: never[]) => unknown) =>
  ({
    metricsCatalogue: async () => Object.values(SPECS),
    metricsSeries: (metric: string, ...rest: never[]) =>
      Promise.resolve(metricsSeries(metric, ...rest)).then((data) => ({
        ...(data as MetricsSeriesResponse),
        spec: SPECS[metric],
      })),
  }) as unknown as ControlApi;

const data: MetricsSeriesResponse = {
  metric: "gpu_utilization",
  spec: SPECS.gpu_utilization,
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
      summary,
    },
  ],
};

test("renders the measured series, axes and identity instead of an empty chart", () => {
  const { container } = render(<SeriesChart data={data} names={{ "Spark A": "Kitchen" }} />);
  expect(screen.getByRole("img", { name: /^GPU utilisation, 6h: Kitchen now 80 %/ })).toBeVisible();
  expect(container.querySelectorAll("path.chart-line")).toHaveLength(1);
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
      summary,
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
  render(<MetricsChart api={fakeApi(metricsSeries)} names={{ spk_a: "Kitchen" }} />);

  const memory = await screen.findByRole("region", { name: "Unified memory used" });
  await within(memory).findByText("Kitchen · GiB");
  expect(memory).toHaveTextContent("60 GiB");
  expect(await screen.findByText("Kitchen: certificate expires in 12 days")).toBeVisible();
  expect(screen.getByRole("region", { name: "Requests per model" })).toBeVisible();
});

test("shows generated tokens per model as an estimate with the Controller's summary", async () => {
  // Break caught: the gateway's tokens look measured, or the page recomputes what the Controller owns.
  const tokens = { latest: 42.5, average: 40, peak: 61.2, total: 12400, coverage: 1 };
  const metricsSeries = vi.fn(async (metric: string) =>
    metric === "output_token_rate"
      ? {
          ...stable(42.5),
          series: [{ ...stable(42.5).series[0], labels: { alias: "glm" }, summary: tokens }],
        }
      : stable(10),
  );
  const { rerender } = render(<MetricsChart api={fakeApi(metricsSeries)} />);

  const tile = await screen.findByRole("region", { name: "Generated tokens per model" });
  await within(tile).findByText("glm · tok/s");
  expect(tile).toHaveTextContent(
    "glm: now 42.5 tok/s · average 40 tok/s · peak 61.2 tok/s · total 12400 (estimate)",
  );
  rerender(<MetricsChart api={fakeApi(metricsSeries)} node="spk_abc" />);
  await waitFor(() =>
    expect(metricsSeries).toHaveBeenCalledWith(
      "output_token_rate",
      "6h",
      "spk_abc",
      expect.any(AbortSignal),
    ),
  );
});

test("an empty series says so and one failed tile leaves the others", async () => {
  // Break caught: a failed or empty read blanks every tile.
  const metricsSeries = vi.fn(async (metric: string) => {
    if (metric === "gpu_temperature") throw new Error("Temporarily unavailable");
    return metric === "gpu_utilization" ? { ...data, series: [] } : stable(40);
  });
  render(<MetricsChart api={fakeApi(metricsSeries)} />);

  const gpu = await screen.findByRole("region", { name: "GPU utilisation" });
  await within(gpu).findByText("No samples in this range");
  const temperature = screen.getByRole("region", { name: "GPU temperature" });
  await within(temperature).findByText(/Metrics unavailable: Temporarily unavailable/);
  await within(screen.getByRole("region", { name: "Unified memory used" })).findByRole("img");
});

test("one range switch re-reads every tile for the Spark, models included", async () => {
  const metricsSeries = vi.fn(async () => data);
  render(<MetricsChart api={fakeApi(metricsSeries)} node="spk_abc" />);
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
  expect(metricsSeries).toHaveBeenCalledWith(
    "request_rate",
    "24h",
    "spk_abc",
    expect.any(AbortSignal),
  );
});

test("an expired certificate says expired, never 'expires in now'", async () => {
  // Break caught: a zero or negative time left reads "certificate expires in now".
  const metricsSeries = vi.fn(async (metric: string) =>
    metric === "certificate_expiry" ? stable(0) : stable(10),
  );
  render(<MetricsChart api={fakeApi(metricsSeries)} names={{ spk_a: "Kitchen" }} />);

  expect(await screen.findByText("Kitchen: certificate expired")).toBeVisible();
  expect(screen.queryByText(/expires in/)).toBeNull();
});

test("a Spark has the same colour in every tile, by its place in the snapshot order", async () => {
  // Break caught: colour follows the order series arrive in, so a Spark changes colour between tiles.
  const both = (first: string, second: string): MetricsSeriesResponse => ({
    ...data,
    series: [first, second].map((id) => ({
      labels: { node_id: id },
      points: [
        { timestamp: 100, value: 1 },
        { timestamp: 200, value: 2 },
      ],
      summary,
    })),
  });
  const metricsSeries = vi.fn(async (metric: string) =>
    metric === "gpu_temperature" ? both("spk_b", "spk_a") : both("spk_a", "spk_b"),
  );
  render(
    <MetricsChart api={fakeApi(metricsSeries)} names={{ spk_a: "Kitchen", spk_b: "Garage" }} />,
  );

  const color = async (tile: string, name: string) => {
    const region = await screen.findByRole("region", { name: tile });
    const entry = await within(region).findByText(new RegExp(`^${name} ·`));
    return (entry as HTMLElement).style.color;
  };
  expect(await color("GPU utilisation", "Kitchen")).toBe("var(--series-1)");
  expect(await color("GPU temperature", "Kitchen")).toBe("var(--series-1)");
  expect(await color("GPU temperature", "Garage")).toBe("var(--series-2)");
});

test("range changes request Spark samples and a fresh read recovers from failure", async () => {
  // Break caught: a failed read for the new range is never retried, or Refresh keeps the error.
  let failing = false;
  const metricsSeries = vi.fn(async (_metric: string, range: string) => {
    if (failing && range === "24h") throw new Error("Temporarily unavailable, retry");
    return data;
  });
  render(<MetricsChart api={fakeApi(metricsSeries)} node="spk_abc" />);
  await screen.findAllByRole("img");
  expect(metricsSeries).toHaveBeenCalledWith(
    "gpu_utilization",
    "6h",
    "spk_abc",
    expect.any(AbortSignal),
  );
  failing = true;
  fireEvent.change(screen.getByRole("combobox", { name: "Range" }), { target: { value: "24h" } });
  await screen.findAllByText(/Metrics unavailable: Temporarily unavailable/);
  expect(screen.queryByRole("img")).toBeNull();
  failing = false;
  fireEvent.click(screen.getByRole("button", { name: "Refresh metrics" }));
  await screen.findAllByRole("img");
  expect(screen.queryByText(/Metrics unavailable/)).toBeNull();
});

test("range change aborts the old read and a late response cannot replace current samples", async () => {
  // Break caught: a slow answer for the old range overwrites the samples of the range now shown.
  const late: Array<(value: MetricsSeriesResponse) => void> = [];
  const metricsSeries = vi.fn(
    (_metric: string, range: string, _node: unknown, _signal: AbortSignal) =>
      range === "6h"
        ? new Promise<MetricsSeriesResponse>((resolve) => {
            late.push(resolve);
          })
        : Promise.resolve(data),
  );
  render(<MetricsChart api={fakeApi(metricsSeries)} />);
  fireEvent.change(screen.getByRole("combobox", { name: "Range" }), { target: { value: "1h" } });
  await screen.findAllByRole("img");
  const stale = metricsSeries.mock.calls.filter((call) => call[1] === "6h");
  expect(stale.length).toBeGreaterThan(0);
  expect(stale.every((call) => call[3].aborted)).toBe(true);
  for (const finish of late) finish({ ...data, series: [] });
  await waitFor(() => expect(screen.queryByText("No samples in this range")).toBeNull());
  expect(screen.getAllByRole("img").length).toBeGreaterThan(0);
});
