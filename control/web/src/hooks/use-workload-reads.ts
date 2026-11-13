import type { ControlApi, Metric, MetricSpec, MetricsSeriesResponse } from "../api/types";
import { useMetricCatalogue } from "./use-metric-catalogue";
import { useMetricRead } from "./use-metric-series";

/** One read: undefined while it loads (nothing is shown), null after it failed (Not reported). */
type Read = MetricsSeriesResponse | null | undefined;

/** The engine metrics shown beside a hero, in order; their titles come from the catalogue. */
export const ENGINE_FIGURES = [
  "prefill_throughput",
  "ttft_mean",
  "kv_cache_usage",
  "prefix_cache_hit_rate",
  "spec_decode_acceptance",
] as const satisfies readonly Metric[];

/** The last hour of the per-model series a workload shows: two speeds and the engine's queue. */
export type WorkloadReads = {
  tokens: Read;
  requests: Read;
  running: Read;
  waiting: Read;
  /** The `ENGINE_FIGURES` reads, in the same order. */
  figures: Read[];
  /** The catalogue (empty while it loads or when it failed): a metric it does not list is not shown. */
  specs: MetricSpec[];
};

/** The reads of every workload, once per page, so a page and its fleet band never read a metric twice. */
export function useWorkloadReads(
  api: Pick<ControlApi, "metricsSeries" | "metricsCatalogue">,
): WorkloadReads {
  const catalogue = useMetricCatalogue(api);
  return {
    tokens: useMetricRead(api, "output_token_rate", "1h"),
    requests: useMetricRead(api, "request_rate", "1h"),
    running: useMetricRead(api, "requests_running", "1h"),
    waiting: useMetricRead(api, "requests_waiting", "1h"),
    figures: [
      useMetricRead(api, ENGINE_FIGURES[0], "1h"),
      useMetricRead(api, ENGINE_FIGURES[1], "1h"),
      useMetricRead(api, ENGINE_FIGURES[2], "1h"),
      useMetricRead(api, ENGINE_FIGURES[3], "1h"),
      useMetricRead(api, ENGINE_FIGURES[4], "1h"),
    ],
    specs: Array.isArray(catalogue) ? catalogue : [],
  };
}
