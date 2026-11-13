import { useEffect, useState } from "react";
import type { ControlApi, Metric, MetricRange, MetricsSeriesResponse } from "../api/types";

const REFRESH_MS = 15_000;

/**
 * One metric's series as the Controller serves it, read again every 15 seconds. null until a read
 * succeeds and after one fails (the next read heals it); a stale series is never shown as live.
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
        // A failed read is not a live reading: the figure says Not reported until a read succeeds.
        () => {
          if (!controller.signal.aborted) setData(null);
        },
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
