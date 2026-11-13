import { useEffect, useState } from "react";
import type { ControlApi, Metric, MetricRange, MetricsSeriesResponse } from "../api/types";

const REFRESH_MS = 15_000;

/**
 * One metric's series as the Controller serves it (for one Spark when `node` is given), read again every
 * 15 seconds: undefined while the first read is out, null after a read failed (the next read heals it),
 * never a stale series shown as live.
 */
export function useMetricRead(
  api: Pick<ControlApi, "metricsSeries">,
  metric: Metric,
  range: MetricRange,
  node?: string,
): MetricsSeriesResponse | null | undefined {
  const [data, setData] = useState<MetricsSeriesResponse | null>();
  useEffect(() => {
    const controller = new AbortController();
    setData(undefined);
    const read = () =>
      api.metricsSeries(metric, range, node, controller.signal).then(
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
  }, [api, metric, range, node]);
  return data;
}

/** The series, or null while loading and after a failed read: for figures that say Not reported either way. */
export function useMetricSeries(
  api: Pick<ControlApi, "metricsSeries">,
  metric: Metric,
  range: MetricRange,
): MetricsSeriesResponse | null {
  return useMetricRead(api, metric, range) ?? null;
}
