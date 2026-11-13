import { useEffect, useState } from "react";
import type { ControlApi, Metric, MetricRange, MetricsSeriesResponse } from "../api/types";

const REFRESH_MS = 15_000;

/**
 * One metric's series as the Controller serves it, read again every 15 seconds. A failed read keeps the
 * last good one (the next read heals it); null until the first read succeeds.
 */
export function useMetricSeries(
  api: Pick<ControlApi, "metricsSeries">,
  metric: Metric,
  range: MetricRange,
): MetricsSeriesResponse | null {
  const [data, setData] = useState<MetricsSeriesResponse | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const read = () =>
      api.metricsSeries(metric, range, undefined, controller.signal).then(
        (response) => {
          if (!controller.signal.aborted) setData(response);
        },
        () => undefined,
      );
    void read();
    const timer = setInterval(() => void read(), REFRESH_MS);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [api, metric, range]);
  return data;
}
