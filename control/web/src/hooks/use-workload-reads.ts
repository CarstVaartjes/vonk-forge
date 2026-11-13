import type { ControlApi, Metric, MetricsSeriesResponse } from "../api/types";
import { useMetricSeries } from "./use-metric-series";

/** The figures beside a workload's hero, each the engine's metric for it. */
export const ENGINE_FIGURES = [
  { title: "Prefill", metric: "prefill_throughput" },
  { title: "First token", metric: "ttft_mean" },
  { title: "KV cache in use", metric: "kv_cache_usage" },
  { title: "Prompt cache hits", metric: "prefix_cache_hit_rate" },
  { title: "Draft tokens accepted", metric: "spec_decode_acceptance" },
] as const satisfies readonly { title: string; metric: Metric }[];

/** The last hour of the per-model series a workload shows: three speeds and the engine figures, in `ENGINE_FIGURES` order. */
export type WorkloadReads = {
  decode: MetricsSeriesResponse | null;
  tokens: MetricsSeriesResponse | null;
  requests: MetricsSeriesResponse | null;
  figures: (MetricsSeriesResponse | null)[];
};

/** Each read is null while it loads and after it failed, so a workload says Not reported for it. */
export function useWorkloadReads(api: Pick<ControlApi, "metricsSeries">): WorkloadReads {
  return {
    decode: useMetricSeries(api, "decode_throughput", "1h"),
    tokens: useMetricSeries(api, "output_token_rate", "1h"),
    requests: useMetricSeries(api, "request_rate", "1h"),
    figures: [
      useMetricSeries(api, ENGINE_FIGURES[0].metric, "1h"),
      useMetricSeries(api, ENGINE_FIGURES[1].metric, "1h"),
      useMetricSeries(api, ENGINE_FIGURES[2].metric, "1h"),
      useMetricSeries(api, ENGINE_FIGURES[3].metric, "1h"),
      useMetricSeries(api, ENGINE_FIGURES[4].metric, "1h"),
    ],
  };
}
