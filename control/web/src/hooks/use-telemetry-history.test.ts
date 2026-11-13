import { act, renderHook } from "@testing-library/react";
import type { ControlApi, MetricsSeriesResponse, TelemetryPoint } from "../api/types";
import {
  appendSample,
  HISTORY_SECONDS,
  seedPoints,
  useTelemetryHistory,
} from "./use-telemetry-history";

const T0 = Date.parse("2026-11-12T12:00:00Z") / 1000;
const sample = (seconds: number, utilization: number | null = 50): TelemetryPoint =>
  ({
    observed_at: new Date((T0 + seconds) * 1000).toISOString(),
    gpu_utilization_percent: utilization,
    gpu_temperature_c: 60,
    gpu_power_watts: 25,
    gpu_clock: { graphics_mhz: 2000 },
  }) as TelemetryPoint;

test("points append, thin to one per 10 s, and drop after 30 minutes", () => {
  let points = appendSample([], sample(0));
  for (const at of [2, 4, 12, 14]) points = appendSample(points, sample(at, at));
  expect(points.map((point) => point.utilization)).toEqual([4, 14]);
  points = appendSample(points, sample(HISTORY_SECONDS + 40));
  expect(points.map((point) => point.t)).toEqual([T0 + HISTORY_SECONDS + 40]);
});

test("a silence in the stream leaves a gap marker of nulls, not a line", () => {
  const points = appendSample(appendSample([], sample(0)), sample(90));
  expect(points).toHaveLength(3);
  expect(points[1]).toMatchObject({
    utilization: null,
    temperature: null,
    power: null,
    clockMhz: null,
  });
});

const series = (values: Array<number | null>): MetricsSeriesResponse =>
  ({
    step_seconds: 60,
    series: [{ points: values.map((value, i) => ({ timestamp: T0 - 600 + i * 60, value })) }],
  }) as MetricsSeriesResponse;

test("the hour is read once, comes before the live points, and a stale stream is not live", async () => {
  const metricsSeries = vi.fn(async () => series([10, 20]));
  const api = { metricsSeries } as unknown as ControlApi;
  const at = (seconds: number) => new Date((T0 + seconds) * 1000);
  // The stream hands out one sample object per event, never a new one per render.
  const samples = new Map([0, 2, 12].map((seconds) => [seconds, sample(seconds)]));
  const { result, rerender } = renderHook(
    (props: { seconds: number; now: Date }) =>
      useTelemetryHistory({
        api,
        nodeId: "spk_a",
        telemetry: { age_seconds: 0, freshness: "live", sample: samples.get(props.seconds)! },
        now: props.now,
      }),
    { initialProps: { seconds: 0, now: at(1) } },
  );
  await act(async () => undefined);
  rerender({ seconds: 2, now: at(3) });
  rerender({ seconds: 12, now: at(13) });
  expect(metricsSeries).toHaveBeenCalledTimes(2);
  expect(result.current.points.map((point) => point.utilization)).toEqual([10, 20, null, 50, 50]);
  expect(result.current.live).toBe(true);
  rerender({ seconds: 12, now: at(60) });
  expect(result.current.live).toBe(false);
});

test("the seed leaves a gap where the Controller has a hole, and none between neighbouring steps", () => {
  const at = (...seconds: number[]): MetricsSeriesResponse =>
    ({
      step_seconds: 60,
      series: [{ points: seconds.map((s) => ({ timestamp: T0 + s, value: 1 })) }],
    }) as MetricsSeriesResponse;
  const points = seedPoints(at(0, 60, 400), at(0, 60, 400), []);
  expect(points.map((point) => point.utilization)).toEqual([1, 1, null, 1]);
});
