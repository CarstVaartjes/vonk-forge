import { act, renderHook } from "@testing-library/react";
import type { ControlApi, MetricSpec } from "../api/types";
import { useMetricCatalogue } from "./use-metric-catalogue";

const spec = { metric: "gpu_utilization", title: "GPU utilisation" } as MetricSpec;

test("the catalogue is read, a failure is its message, and a new retry reads again", async () => {
  const metricsCatalogue = vi
    .fn()
    .mockResolvedValueOnce([spec])
    .mockRejectedValueOnce(new Error("Temporarily unavailable"));
  const api = { metricsCatalogue } as unknown as Pick<ControlApi, "metricsCatalogue">;
  const { result, rerender } = renderHook(({ retry }) => useMetricCatalogue(api, retry), {
    initialProps: { retry: 0 },
  });
  expect(result.current).toBeUndefined();
  await act(async () => undefined);
  expect(result.current).toEqual([spec]);
  rerender({ retry: 1 });
  await act(async () => undefined);
  expect(result.current).toMatch(/Temporarily unavailable/);
  expect(metricsCatalogue).toHaveBeenCalledTimes(2);
});
