import { render, screen, waitFor } from "@testing-library/react";
import type { ControlApi, MetricSpec } from "../api/types";
import { EnginePanel } from "./engine-panel";

const spec = (metric: string, title: string, unit: string, basis = "measured") =>
  ({
    metric,
    title,
    unit,
    scope: "alias",
    basis,
    source: "engine",
    summary_total: false,
    fleet_sum: false,
    hint: "",
  }) as MetricSpec;
const SPECS = [
  spec("decode_throughput", "Decode throughput", "tokens_per_second"),
  spec("kv_cache_usage", "KV cache in use", "ratio"),
  spec("gateway_ttft_p95", "Time to first token, 95th percentile", "seconds", "estimated"),
];
const reply = (metric: string, alias: string) => ({
  metric,
  spec: SPECS.find((entry) => entry.metric === metric),
  range: "1h",
  start: 0,
  end: 100,
  step_seconds: 50,
  series: [
    {
      labels: { alias },
      points: [
        { timestamp: 0, value: 40 },
        { timestamp: 50, value: 42 },
      ],
      summary: { latest: 42, average: 41, peak: 42, total: null, coverage: 1 },
    },
  ],
});

test("draws the engine metrics this alias has samples for; others are not drawn", async () => {
  // Decode reports for coder, KV cache only for another alias, the gateway p95 read fails.
  const metricsSeries = vi.fn(async (metric: string) => {
    if (metric === "gateway_ttft_p95") throw new Error("down");
    return reply(metric, metric === "kv_cache_usage" ? "fast" : "coder");
  });
  const api = { metricsCatalogue: async () => SPECS, metricsSeries } as unknown as ControlApi;
  render(<EnginePanel api={api} alias="coder" title="GLM 5.3 Flash" />);
  const decode = await screen.findByRole("region", { name: "Decode throughput" });
  expect(decode).toHaveTextContent("42.0 tok/s");
  await waitFor(() =>
    expect(
      screen.getByRole("region", { name: "Time to first token, 95th percentile" }),
    ).toHaveTextContent("Not reported"),
  );
  expect(
    screen.getByRole("region", { name: "Time to first token, 95th percentile" }),
  ).toHaveTextContent("est.");
  expect(screen.queryByRole("region", { name: "KV cache in use" })).toBeNull();
});
