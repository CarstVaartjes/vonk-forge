import type { ControlApi, MetricsSeriesResponse } from "../api/types";
import { useMetricRead } from "./use-metric-series";

/** One read: undefined while it loads (nothing is shown), null after it failed (Not reported). */
type Read = MetricsSeriesResponse | null | undefined;

/** The last hour of the per-model series a workload shows: three speeds and the engine's queue. */
export type WorkloadReads = {
  decode: Read;
  tokens: Read;
  requests: Read;
  running: Read;
  waiting: Read;
};

/** The reads of every workload, once per page, so a page and its fleet band never read a metric twice. */
export function useWorkloadReads(api: Pick<ControlApi, "metricsSeries">): WorkloadReads {
  return {
    decode: useMetricRead(api, "decode_throughput", "1h"),
    tokens: useMetricRead(api, "output_token_rate", "1h"),
    requests: useMetricRead(api, "request_rate", "1h"),
    running: useMetricRead(api, "requests_running", "1h"),
    waiting: useMetricRead(api, "requests_waiting", "1h"),
  };
}
