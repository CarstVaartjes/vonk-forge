import type {TelemetryMetrics} from "../api/types";

export function telemetryMetrics(
  observedAt = "2026-08-15T11:59:58Z",
): TelemetryMetrics {
  return {
    schema_version: 2,
    series: [],
    capabilities: [],
    runtimes: [],
    workloads: [],
    provenance: {
      collector: "spark-agent",
      collector_version: "fixture-2",
      host_uptime_seconds: 7200,
      source_observed_at: observedAt,
    },
  };
}
