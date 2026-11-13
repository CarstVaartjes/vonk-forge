import type { ControlApi, MetricsSeriesResponse } from "../api/types";
import { useMetricSeries } from "./use-metric-series";

/** The last hour of the three per-model speeds a workload shows: engine decode, gateway tokens, requests. */
export type WorkloadReads = {
  decode: MetricsSeriesResponse | null;
  tokens: MetricsSeriesResponse | null;
  requests: MetricsSeriesResponse | null;
};

/** Each read is null while it loads and after it failed, so a workload says Not reported for it. */
export function useWorkloadReads(api: Pick<ControlApi, "metricsSeries">): WorkloadReads {
  return {
    decode: useMetricSeries(api, "decode_throughput", "1h"),
    tokens: useMetricSeries(api, "output_token_rate", "1h"),
    requests: useMetricSeries(api, "request_rate", "1h"),
  };
}
